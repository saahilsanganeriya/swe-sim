"""Validate a compiled scenario on Modal: nop, oracle, every control patch and the reachability probe.

Writes workspace/scenarios/<id>/validation.json. A scenario is `valid` only when every control earns its expected
outcome, base/gold test evidence is complete, and no gold patch line or gold object is reachable in the agent image.
Infrastructure outcomes (collection/simulator/evaluator failure) never count as a defeated attack.
"""
from __future__ import annotations

import json
from pathlib import Path

from swesim import harbor_cli
from swesim.models import ControlPatch, RunConfig, ScenarioSpec
from swesim.paths import Workspace
from swesim.stages.compile import compile_scenario

PATCH_AGENT = "swesim.agents.patch_agent:PatchAgent"
OUTCOME_NAMES = {1: "valid-correct", 2: "valid-incorrect", 3: "agent-invalid-submission", 4: "agent-runtime-failure",
                 5: "collection-failure", 6: "simulator-failure", 7: "evaluator-failure"}
CONTROL_OUTCOMES = {1, 2, 3, 4}  # a control only counts when the pipeline itself worked


def evaluate_controls(results: dict[str, dict], controls: dict) -> tuple[bool, list[str]]:
    """Compare recorded rewards with the spec's expectations. `controls` is `spec.controls.model_dump()`."""
    failures: list[str] = []
    required = {"oracle", "nop"} | {f"plausible-wrong:{c['name']}" for c in controls["plausible_wrong"]} \
        | {f"alt-correct:{c['name']}" for c in controls["alternative_correct"]} | {f"tamper:{c['name']}" for c in controls["tamper"]}
    missing = sorted(required - results.keys())
    if missing: return False, [f"missing control results: {', '.join(missing)}"]
    for name in sorted(required):
        if results[name].get("outcome_code") not in CONTROL_OUTCOMES: failures.append(f"{name}: infrastructure is not a passing control (outcome_code {results[name].get('outcome_code')})")
        if not isinstance(results[name].get("reward"), (int, float)) or isinstance(results[name].get("reward"), bool): failures.append(f"{name}: missing numeric reward")
    if results["oracle"].get("success") != 1 or results["oracle"].get("reward") != 1: failures.append("oracle: not fully correct")
    if results["nop"].get("reward", 1) != 0: failures.append("nop: reward != 0")
    for c in controls["plausible_wrong"]:
        r = results[f"plausible-wrong:{c['name']}"]
        if r.get("success") != c.get("expected_success", 0): failures.append(f"plausible-wrong:{c['name']}: success {r.get('success')}")
        if c.get("expected_reward_max") is not None and isinstance(r.get("reward"), (int, float)) and r["reward"] > c["expected_reward_max"] + 1e-9:
            failures.append(f"plausible-wrong:{c['name']}: reward {r.get('reward')} > max {c['expected_reward_max']}")
    for c in controls["alternative_correct"]:
        if results[f"alt-correct:{c['name']}"].get("success") != 1: failures.append(f"alt-correct:{c['name']}: success != 1")
    for c in controls["tamper"]:
        r = results[f"tamper:{c['name']}"]
        if c.get("cls") == "incorrect-exploit" and r.get("reward", 1) != 0: failures.append(f"tamper:{c['name']}: exploit earned reward {r.get('reward')}")
        if c.get("cls") == "benign-edit" and r.get("success") != 1: failures.append(f"tamper:{c['name']}: benign edit failed")
    return (not failures), failures


def gold_lines(oracle_diff: str) -> list[str]:
    """Up to three substantial lines the oracle adds; the probe greps the agent image for them."""
    adds = [line[1:].strip() for line in oracle_diff.splitlines() if line.startswith("+") and not line.startswith("+++") and len(line.strip()) > 20]
    return adds[:3]


def _expected(c: ControlPatch) -> dict:
    return {k: v for k, v in (("expected_success", c.expected_success), ("expected_reward_max", c.expected_reward_max),
                              ("expected_reward", c.expected_reward), ("cls", c.cls)) if v is not None}


def _exception_info(trial_dir: Path):
    try: return json.loads((trial_dir / "result.json").read_text()).get("exception_info")
    except (OSError, ValueError): return None


def validate_scenario(ws: Workspace, spec: ScenarioSpec, run_config: RunConfig, stakeholder_url: str) -> dict:
    from swesim.agents.patch_agent import parse_probe_output  # harbor import kept off the module path

    sdir = ws.scenario_dir(spec.id); comp = compile_scenario(ws, spec, run_config, stakeholder_url)
    task = comp.task_dirs["full"]; chash = json.loads(comp.compile_json.read_text())["compiled_hash"]
    jobs = ws.jobs_dir / "validate" / spec.id / chash[:16]
    results: dict[str, dict] = {}; trial_dirs: dict[str, str] = {}; failures: list[str] = []
    expected = {"nop": {"reward": 0}, "oracle": {"success": 1, "reward": 1}}
    plan: list[tuple[str, str, dict | None]] = [("nop", "nop", None), ("oracle", "oracle", None)]
    for prefix, group in (("plausible-wrong", spec.controls.plausible_wrong), ("alt-correct", spec.controls.alternative_correct), ("tamper", spec.controls.tamper)):
        for c in group:
            plan.append((f"{prefix}:{c.name}", PATCH_AGENT, {"patch": str((task / "controls" / f"{c.name}.diff").resolve())})); expected[f"{prefix}:{c.name}"] = _expected(c)
    launch_failed = False
    for name, agent, kwargs in plan:
        try:
            td = harbor_cli.run_task(task, agent, jobs, f"{spec.id}--{name.replace(':', '-')}", agent_kwargs=kwargs)
        except harbor_cli.HarborRunError as e:
            failures.append(f"{name}: launch failed: {e}"); launch_failed = True; break  # systemic; do not burn the remaining launches
        trial_dirs[name] = str(td); results[name] = dict(harbor_cli.read_reward(td)); results[name]["report"] = harbor_cli.read_report(td)
        if _exception_info(td): failures.append(f"{name}: control agent failed before a clean submission")
    probe = {"raw": "", "gold_hits": [], "all_refs": None, "reflog_entries": None, "gold_object_reachable": False}; probe_txt = ""
    if not launch_failed:
        try:
            probe_dir = harbor_cli.run_task(task, PATCH_AGENT, jobs, f"{spec.id}--probe",
                                            agent_kwargs={"probe": "true", "gold_sha": spec.environment.gold_commit or "",
                                                          "gold_lines": json.dumps(gold_lines((task / "solution" / "oracle.diff").read_text()))})
        except harbor_cli.HarborRunError as e:
            failures.append(f"probe: launch failed: {e}")
        else:
            trial_dirs["probe"] = str(probe_dir); p = probe_dir / "agent" / "probe.txt"
            probe_txt = p.read_text() if p.exists() else ""
            parsed = parse_probe_output(probe_txt)
            probe = {"raw": probe_txt[-2000:], "gold_hits": parsed["gold_line_hits"], "all_refs": parsed["all_refs"],
                     "reflog_entries": parsed["reflog_entries"], "gold_object_reachable": parsed["gold_object_reachable"]}
    ok, control_failures = evaluate_controls(results, spec.controls.model_dump()); failures.extend(control_failures)
    base = {t["id"]: t["observed"] for t in results.get("nop", {}).get("report", {}).get("per_test", [])}
    gold = {t["id"]: t["observed"] for t in results.get("oracle", {}).get("report", {}).get("per_test", [])}
    wanted = set(spec.contract.fail_to_pass) | set(spec.contract.pass_to_pass)
    if set(base) != wanted or any(base.get(t) not in {"failed", "error"} for t in spec.contract.fail_to_pass) or any(base.get(t) != "passed" for t in spec.contract.pass_to_pass):
        failures.append("base must execute every target as failed/error and every regression as passed")
    if set(gold) != wanted or any(gold[t] != "passed" for t in wanted): failures.append("gold exact-id manifest failed")
    if not probe_txt or "HITS_BEGIN" not in probe_txt or "HITS_END" not in probe_txt: failures.append("missing reachability evidence")
    if probe["gold_hits"]: failures.append(f"gold patch lines reachable in agent image: {probe['gold_hits'][:3]}")
    controls_out = {k: {**{kk: vv for kk, vv in v.items() if kk != "report"}, "outcome": OUTCOME_NAMES.get(v.get("outcome_code")), "expected": expected.get(k, {})}
                    for k, v in results.items()}
    out = {"status": "valid" if not failures else "invalid", "failures": failures, "expected_base": base, "expected_gold": gold,
           "controls": controls_out, "probe": probe, "trial_dirs": trial_dirs, "spec_hash": spec.spec_hash, "source": spec.source.value,
           "source_ref": spec.source_ref, "environment_provider": "modal", "image_digest": comp.image_digest, "compiled_hash": chash,
           "run_config_hash": run_config.run_config_hash}
    sdir.mkdir(parents=True, exist_ok=True); (sdir / "validation.json").write_text(json.dumps(out, indent=2))
    return out
