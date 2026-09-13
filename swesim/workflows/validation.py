"""Per-control validation (brief §2.8 item 3): the Harbor runs of `swesim.stages.validate.validate_scenario` split into one
activity per control plus one evaluation, so one infrastructure failure retries one control and the console shows
per-control progress.

Contract
- `control_names(spec)` / `control_plan(spec, task_dir)` list the runs validate_scenario makes, in its order: nop, oracle,
  plausible-wrong:<n>, alt-correct:<n>, tamper:<n>, then the reachability `probe`; the plan carries each run's agent and kwargs.
- `run_control(ws, spec, name, compiled_hash)` launches exactly one of them through `harbor_cli.run_task` (which reconciles an
  existing job dir instead of relaunching) under the jobs dir validate_scenario uses (`jobs/validate/<sid>/<compiled_hash[:16]>`)
  and returns paths and the small reward summary; `HarborRunError` propagates (retryable infrastructure).
- `assemble_validation(...)` reproduces validate_scenario's verdict from the recorded trial dirs (evaluate_controls, the base and
  gold manifests, the reachability probe) and writes the same validation.json (plus `validated_by`). The rules come from
  `swesim.stages.validate` where they are public; `expected_for` and `exception_info` mirror its two private helpers until
  validate.py exports them (asked of the integrator).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from swesim import harbor_cli
from swesim.models import ControlPatch, ScenarioSpec
from swesim.paths import Workspace, atomic_json
from swesim.stages import validate as V

PROBE = "probe"
GROUPS = (("plausible-wrong", "plausible_wrong"), ("alt-correct", "alternative_correct"), ("tamper", "tamper"))


@dataclass(frozen=True)
class Control:
    name: str
    agent: str
    agent_kwargs: dict | None


def expected_for(c: ControlPatch) -> dict:
    """validate._expected: the declared expectations of one control patch."""
    return {k: v for k, v in (("expected_success", c.expected_success), ("expected_reward_max", c.expected_reward_max),
                              ("expected_reward", c.expected_reward), ("cls", c.cls)) if v is not None}


def exception_info(trial_dir: Path):
    """validate._exception_info: Harbor's exception_info for a trial, None when the result is missing or unreadable."""
    try:
        return json.loads((Path(trial_dir) / "result.json").read_text()).get("exception_info")
    except (OSError, ValueError):
        return None


def control_names(spec: ScenarioSpec) -> list[str]:
    names = ["nop", "oracle"]
    for prefix, attr in GROUPS:
        names += [f"{prefix}:{c.name}" for c in getattr(spec.controls, attr)]
    return [*names, PROBE]


def expectations(spec: ScenarioSpec) -> dict[str, dict]:
    expected = {"nop": {"reward": 0}, "oracle": {"success": 1, "reward": 1}}
    for prefix, attr in GROUPS:
        for c in getattr(spec.controls, attr):
            expected[f"{prefix}:{c.name}"] = expected_for(c)
    return expected


def control_plan(spec: ScenarioSpec, task_dir: Path) -> list[Control]:
    plan = [Control("nop", "nop", None), Control("oracle", "oracle", None)]
    for prefix, attr in GROUPS:
        for c in getattr(spec.controls, attr):
            plan.append(Control(f"{prefix}:{c.name}", V.PATCH_AGENT, {"patch": str((task_dir / "controls" / f"{c.name}.diff").resolve())}))
    probes = V.gold_probes((task_dir / "solution" / "oracle.diff").read_text())
    plan.append(Control(PROBE, V.PATCH_AGENT, {"probe": "true", "gold_sha": spec.environment.gold_commit or "", "gold_lines": json.dumps(probes)}))
    return plan


def jobs_dir(ws: Workspace, scenario_id: str, compiled_hash: str) -> Path:
    return ws.jobs_dir / "validate" / scenario_id / compiled_hash[:16]


def job_name(scenario_id: str, control: str) -> str:
    return f"{scenario_id}--{control.replace(':', '-')}"


def run_control(ws: Workspace, spec: ScenarioSpec, name: str, compiled_hash: str) -> dict:
    """One control run; the same job dir as validate_scenario, so a rerun reconciles instead of relaunching."""
    task = ws.task_dir(spec.id, "full")
    control = next((c for c in control_plan(spec, task) if c.name == name), None)
    if control is None:
        raise ValueError(f"{name!r} is not a control of {spec.id}; controls: {control_names(spec)}")
    td = harbor_cli.run_task(task, control.agent, jobs_dir(ws, spec.id, compiled_hash), job_name(spec.id, name), agent_kwargs=control.agent_kwargs)
    reward = harbor_cli.read_reward(td) if name != PROBE else {}
    return {"control": name, "trial_dir": str(td), "outcome_code": reward.get("outcome_code"), "reward": reward.get("reward"),
            "success": reward.get("success"), "agent_failed": exception_info(td) is not None}


def _probe_block(task: Path, probe_dir: str | None) -> tuple[dict, list[str]]:
    from swesim.agents.patch_agent import parse_probe_output  # harbor import kept off the module path

    probes = V.gold_probes((task / "solution" / "oracle.diff").read_text())
    probe = {"raw": "", "gold_hits": [], "raw_hits": [], "filtered_hits": [], "probes": probes, "all_refs": None, "reflog_entries": None,
             "gold_object_reachable": False, "hits_block_found": False}
    if probe_dir:
        p = Path(probe_dir) / "agent" / "probe.txt"; text = p.read_text() if p.exists() else ""
        parsed = parse_probe_output(text, probes)
        probe.update(raw=text[-2000:], gold_hits=parsed["gold_line_hits"], raw_hits=parsed["raw_hits"], filtered_hits=parsed["filtered_hits"],
                     all_refs=parsed["all_refs"], reflog_entries=parsed["reflog_entries"], gold_object_reachable=parsed["gold_object_reachable"],
                     hits_block_found=parsed["hits_block_found"])
    if not probes:
        probe["note"] = "line probe skipped: no distinctive added lines in the oracle diff"  # not a clean scan
    failures: list[str] = []
    if not probe["hits_block_found"]: failures.append("missing reachability evidence")
    if probe["gold_object_reachable"]: failures.append("gold commit object reachable in agent image (history not stripped)")
    if probe["gold_hits"]: failures.append(f"gold patch lines reachable in agent image: {probe['gold_hits'][:3]}")  # filtered hits only
    return probe, failures


def assemble_validation(ws: Workspace, spec: ScenarioSpec, trial_dirs: dict[str, str], compiled_hash: str | None, image_digest: str | None,
                        run_config_hash: str | None) -> dict:
    """validate_scenario's verdict over recorded control runs; writes scenarios/<sid>/validation.json and returns it."""
    task = ws.task_dir(spec.id, "full"); expected = expectations(spec)
    results: dict[str, dict] = {}; failures: list[str] = []
    for name in control_names(spec):
        td = trial_dirs.get(name)
        if name == PROBE or td is None:
            continue  # a missing control shows up in evaluate_controls as "missing control results"
        results[name] = dict(harbor_cli.read_reward(Path(td))); results[name]["report"] = harbor_cli.read_report(Path(td))
        if exception_info(Path(td)) is not None:
            failures.append(f"{name}: control agent failed before a clean submission")
    probe, probe_failures = _probe_block(task, trial_dirs.get(PROBE))
    _ok, control_failures = V.evaluate_controls(results, spec.controls.model_dump()); failures.extend(control_failures)
    base = {t["id"]: t["observed"] for t in results.get("nop", {}).get("report", {}).get("per_test", [])}
    gold = {t["id"]: t["observed"] for t in results.get("oracle", {}).get("report", {}).get("per_test", [])}
    wanted = set(spec.contract.fail_to_pass) | set(spec.contract.pass_to_pass)
    if set(base) != wanted or any(base.get(t) not in {"failed", "error"} for t in spec.contract.fail_to_pass) or any(base.get(t) != "passed" for t in spec.contract.pass_to_pass):
        failures.append("base must execute every target as failed/error and every regression as passed")
    if set(gold) != wanted or any(gold[t] != "passed" for t in wanted):
        failures.append("gold exact-id manifest failed")
    failures.extend(probe_failures)
    controls_out = {k: {**{kk: vv for kk, vv in v.items() if kk != "report"}, "outcome": V.OUTCOME_NAMES.get(v.get("outcome_code")), "expected": expected.get(k, {})}
                    for k, v in results.items()}
    out = {"status": "valid" if not failures else "invalid", "failures": failures, "expected_base": base, "expected_gold": gold,
           "controls": controls_out, "probe": probe, "trial_dirs": dict(trial_dirs), "spec_hash": spec.spec_hash, "source": spec.source.value,
           "source_ref": spec.source_ref, "environment_provider": "modal", "image_digest": image_digest, "compiled_hash": compiled_hash,
           "run_config_hash": run_config_hash, "validated_by": "per-control activities"}
    atomic_json(ws.scenario_dir(spec.id) / "validation.json", out)
    return out
