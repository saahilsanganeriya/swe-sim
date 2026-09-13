"""Validate a compiled scenario on Modal: nop, oracle, every control patch and the reachability probe.

Writes workspace/scenarios/<id>/validation.json. A scenario is `valid` only when every control earns its expected
outcome, base/gold test evidence is complete, and no gold patch line or gold object is reachable in the agent image.
Infrastructure outcomes (collection/simulator/evaluator failure) never count as a defeated attack.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from swesim import harbor_cli
from swesim.models import ControlPatch, RunConfig, ScenarioSpec
from swesim.paths import Workspace
from swesim.stages.compile import compile_scenario, verify_compiled_artifacts

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
        r = results[f"alt-correct:{c['name']}"]
        if r.get("success") != 1: failures.append(f"alt-correct:{c['name']}: success != 1")
        elif r.get("reward") != 1: failures.append(f"alt-correct:{c['name']}: reward {r.get('reward')} != 1")
    for c in controls["tamper"]:
        r = results[f"tamper:{c['name']}"]
        if c.get("cls") == "incorrect-exploit" and r.get("reward", 1) != 0: failures.append(f"tamper:{c['name']}: exploit earned reward {r.get('reward')}")
        if c.get("cls") == "benign-edit" and r.get("success") != 1: failures.append(f"tamper:{c['name']}: benign edit failed")
    # A declared exact reward is checked for every control category (in addition to the class rules above).
    for prefix, key in (("plausible-wrong", "plausible_wrong"), ("alt-correct", "alternative_correct"), ("tamper", "tamper")):
        for c in controls[key]:
            r = results[f"{prefix}:{c['name']}"]
            if c.get("expected_reward") is not None and isinstance(r.get("reward"), (int, float)) and abs(r["reward"] - c["expected_reward"]) > 1e-9:
                msg = f"{prefix}:{c['name']}: reward {r['reward']} != expected {c['expected_reward']}"
                if msg not in failures and not any(f.startswith(f"{prefix}:{c['name']}: ") and "reward" in f for f in failures): failures.append(msg)
    return (not failures), failures


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_NON_DISTINCTIVE = re.compile(r"^(#|import\s|from\s+\S+\s+import\b|\"\"\"|'''|(return|pass|break|continue)\b\s*(None)?\s*$)")


def is_distinctive(line: str) -> bool:
    """An added line worth grepping for: > 30 chars stripped, contains an identifier, not a bare import/return/pass/comment/docstring."""
    s = line.strip()
    return len(s) > 30 and not _NON_DISTINCTIVE.match(s) and bool(_IDENT.search(s))


def _informative(removed: str) -> bool:
    """A removed line worth matching (whole line, after strip): >= 8 chars with an identifier, not punctuation only.
    Shorter/common lines such as `else:` would suppress genuine post-fix copies."""
    s = removed.strip()
    return len(s) >= 8 and bool(_IDENT.search(s)) and not re.fullmatch(r"[\W_]*", s)


def _header_target(text: str) -> bool:
    t = text.strip(); return t == "/dev/null" or t.startswith(("a/", "b/", '"a/', '"b/'))


def gold_probes(oracle_diff: str, max_probes: int = 6, per_hunk: int = 2) -> list[dict]:
    """Distinctive added lines paired with their hunk's removed lines: [{"line", "removed": [...], "path", "new"}].

    A base copy of a modified file (e.g. build/lib/...) contains the hunk's removed lines as whole lines; a leaked
    post-fix copy does not, so the probe counts a file only when it holds the added line and none of the removed ones.
    Pure-addition hunks keep the plain grep; for an existing file the probe first checks whether the line already
    exists at the base path (then no copy is evidence) and the host skips that path. `new` marks `--- /dev/null`
    files, which have no base copy anywhere, so nothing is skipped for them."""
    lines = oracle_diff.splitlines()
    probes: list[dict] = []; path: str | None = None; new = False; added: list[str] = []; removed: list[str] = []; open_hunk = False

    def flush():
        nonlocal added, removed
        if open_hunk:
            rem = [r.strip() for r in removed if _informative(r)]
            for line in [a.strip() for a in added if is_distinctive(a)][:per_hunk]:
                if len(probes) < max_probes and not any(p["line"] == line for p in probes):
                    probes.append({"line": line, "removed": rem, "path": path, "new": new})
        added, removed = [], []

    i = 0
    while i < len(lines):
        raw = lines[i]; nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if raw.startswith("diff --git "): flush(); open_hunk = False; new = False; i += 1; continue
        # `--- `/`+++ ` form a file header outside a hunk, or inside one only with canonical a/ b/ /dev/null targets
        # (otherwise they are removed/added content lines that happen to start with `-- ` / `++ `).
        if raw.startswith("--- ") and nxt.startswith("+++ ") and (not open_hunk or (_header_target(raw[4:]) and _header_target(nxt[4:]))):
            flush(); new = raw[4:].strip() == "/dev/null"; target = nxt[4:].strip()
            path = target[2:] if target.startswith("b/") else (None if target == "/dev/null" else target)
            open_hunk = True; i += 2; continue
        if raw.startswith("@@"): flush(); open_hunk = True; i += 1; continue
        if open_hunk:
            if raw.startswith("+"): added.append(raw[1:])
            elif raw.startswith("-"): removed.append(raw[1:])
        i += 1
    flush()
    return probes


def gold_lines(oracle_diff: str) -> list[str]:
    """The distinctive added lines the probe greps for (see gold_probes for the pairing)."""
    return [p["line"] for p in gold_probes(oracle_diff)]


def _expected(c: ControlPatch) -> dict:
    return {k: v for k, v in (("expected_success", c.expected_success), ("expected_reward_max", c.expected_reward_max),
                              ("expected_reward", c.expected_reward), ("cls", c.cls)) if v is not None}


def _exception_info(trial_dir: Path):
    try: return json.loads((trial_dir / "result.json").read_text()).get("exception_info")
    except (OSError, ValueError): return None


def validate_scenario(ws: Workspace, spec: ScenarioSpec, run_config: RunConfig, stakeholder_url: str) -> dict:
    from swesim.agents.patch_agent import parse_probe_output  # harbor import kept off the module path

    sdir = ws.scenario_dir(spec.id); comp = compile_scenario(ws, spec, run_config, stakeholder_url)
    task = comp.task_dirs["full"]; compile_record = json.loads(comp.compile_json.read_text()); chash = compile_record["compiled_hash"]
    verify_compiled_artifacts(task, compile_record, "full")
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
    probes = gold_probes((task / "solution" / "oracle.diff").read_text())
    probe = {"raw": "", "gold_hits": [], "raw_hits": [], "filtered_hits": [], "probes": probes, "all_refs": None, "reflog_entries": None,
             "gold_object_reachable": False, "hits_block_found": False}; probe_txt = ""
    if not launch_failed:
        try:
            probe_dir = harbor_cli.run_task(task, PATCH_AGENT, jobs, f"{spec.id}--probe",
                                            agent_kwargs={"probe": "true", "gold_sha": spec.environment.gold_commit or "", "gold_lines": json.dumps(probes)})
        except harbor_cli.HarborRunError as e:
            failures.append(f"probe: launch failed: {e}")
        else:
            trial_dirs["probe"] = str(probe_dir); p = probe_dir / "agent" / "probe.txt"
            probe_txt = p.read_text() if p.exists() else ""
            parsed = parse_probe_output(probe_txt, probes)
            probe = {"raw": probe_txt[-2000:], "gold_hits": parsed["gold_line_hits"], "raw_hits": parsed["raw_hits"], "filtered_hits": parsed["filtered_hits"],
                     "probes": probes, "all_refs": parsed["all_refs"], "reflog_entries": parsed["reflog_entries"],
                     "gold_object_reachable": parsed["gold_object_reachable"], "hits_block_found": parsed["hits_block_found"]}
    if not probes: probe["note"] = "line probe skipped: no distinctive added lines in the oracle diff"  # not a clean scan
    ok, control_failures = evaluate_controls(results, spec.controls.model_dump()); failures.extend(control_failures)
    base = {t["id"]: t["observed"] for t in results.get("nop", {}).get("report", {}).get("per_test", [])}
    gold = {t["id"]: t["observed"] for t in results.get("oracle", {}).get("report", {}).get("per_test", [])}
    wanted = set(spec.contract.fail_to_pass) | set(spec.contract.pass_to_pass)
    if set(base) != wanted or any(base.get(t) not in {"failed", "error"} for t in spec.contract.fail_to_pass) or any(base.get(t) != "passed" for t in spec.contract.pass_to_pass):
        failures.append("base must execute every target as failed/error and every regression as passed")
    if set(gold) != wanted or any(gold[t] != "passed" for t in wanted): failures.append("gold exact-id manifest failed")
    if not probe["hits_block_found"]: failures.append("missing reachability evidence")
    if probe["gold_object_reachable"]: failures.append("gold commit object reachable in agent image (history not stripped)")
    if probe["gold_hits"]: failures.append(f"gold patch lines reachable in agent image: {probe['gold_hits'][:3]}")  # filtered hits only; raw hits are recorded
    controls_out = {k: {**{kk: vv for kk, vv in v.items() if kk != "report"}, "outcome": OUTCOME_NAMES.get(v.get("outcome_code")), "expected": expected.get(k, {})}
                    for k, v in results.items()}
    out = {"status": "valid" if not failures else "invalid", "failures": failures, "expected_base": base, "expected_gold": gold,
           "controls": controls_out, "probe": probe, "trial_dirs": trial_dirs, "spec_hash": spec.spec_hash, "source": spec.source.value,
           "source_ref": spec.source_ref, "environment_provider": "modal", "image_digest": comp.image_digest, "compiled_hash": chash,
           "run_config_hash": run_config.run_config_hash}
    sdir.mkdir(parents=True, exist_ok=True); (sdir / "validation.json").write_text(json.dumps(out, indent=2))
    return out
