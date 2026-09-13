"""Export stage: one ``workspace/exports/<trial_id>.episode.json`` manifest per finished trial.

``policy_visible`` holds only what the agent actually observed: the instruction text, the ATIF trajectory
from Harbor's ``agent/trajectory.json`` and the stakeholder replies as they appeared in the agent's own tool
observations. The stakeholder service's generated transcript (with fact annotations and tool audit) is
evidence for the evaluator and lives under ``evaluator_only``: a generated reply is not proof the policy
received it. Interactive episodes stay ``simulator_validity: unadjudicated`` and are never marked
``training_eligible`` until adjudicated.
"""
from __future__ import annotations

import json
from pathlib import Path

from swesim.models import Discovery, ScenarioSpec
from swesim.paths import Workspace, atomic_json
from swesim.stages.score import count_exchanges, load_trial_records, message_events

SHIMS = ("ask-user", "request-review")


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _observations(step: dict) -> dict[str | None, str]:
    """Map tool_call id -> observation text for one ATIF step (positional fallback when ids are absent)."""
    results = (step.get("observation") or {}).get("results") or []
    out: dict[str | None, str] = {}
    for i, res in enumerate(results):
        if not isinstance(res, dict):
            continue
        content = res.get("content")
        text = content if isinstance(content, str) else json.dumps(content) if content is not None else ""
        out[res.get("source_call_id") or f"#{i}"] = text
    return out


def delivered_replies(traj: dict) -> list[dict]:
    """Stakeholder replies the policy actually saw: shim invocations in the agent's tool calls paired with the
    observation returned for that call. Empty when there is no trajectory."""
    replies = []
    for step in traj.get("steps") or []:
        if not isinstance(step, dict) or step.get("source", "agent") != "agent":
            continue
        obs = _observations(step)
        for i, call in enumerate(step.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            args = call.get("arguments")
            command = args.get("command") if isinstance(args, dict) and isinstance(args.get("command"), str) else None
            text = command if command is not None else (args if isinstance(args, str) else json.dumps(args or {}))
            shim = next((s for s in SHIMS if s in text), None)
            if not shim:
                continue
            call_id = call.get("tool_call_id")
            observation = obs.get(call_id) if call_id in obs else obs.get(f"#{i}")
            replies.append({"step_id": step.get("step_id"), "ts": step.get("timestamp"), "shim": shim, "command": text,
                            "observation": observation})
    return replies


def _instruction(rec: dict, tdir: Path) -> str | None:
    for p in (Path(rec["task_dir"]) / "instruction.md" if rec.get("task_dir") else None, tdir / "task" / "instruction.md"):
        if p is not None and p.exists():
            return p.read_text()
    return None


def _spec_for(rec: dict, tdir: Path, ws: Workspace, scenario_id: str) -> ScenarioSpec:
    for p in (tdir / "spec.json", ws.scenario_dir(scenario_id) / "scenario.json"):
        if p.exists():
            return ScenarioSpec.model_validate_json(p.read_text())
    raise ValueError(f"no frozen spec for trial {rec.get('trial_id')}")


def episode_manifest(ws: Workspace, rec: dict, scenario_id: str) -> dict:
    tdir = ws.root / "trials" / rec["trial_id"]
    spec = _spec_for(rec, tdir, ws, scenario_id)
    traj: dict = {}
    trial_dir = Path(rec["trial_dir"]) if rec.get("trial_dir") else None
    if trial_dir is not None:
        traj = _read_json(trial_dir / "agent" / "trajectory.json", default={}) or {}
    report = rec.get("report") or {}
    transcript = rec.get("transcript") or {}
    audit = message_events(rec)
    interactive = rec["condition"] == "interactive"
    simulator_validity = report.get("simulator_validity") or ("unadjudicated" if interactive else "n/a")
    eligible = bool(traj.get("steps")) and report.get("training_eligible") is not False
    if interactive:
        eligible = eligible and report.get("transcript_binding_valid") is True and simulator_validity == "ok"
    replies = delivered_replies(traj)
    label = rec.get("label") if isinstance(rec.get("label"), dict) else None
    return {
        "trial_id": rec["trial_id"], "scenario_id": spec.id, "family_id": spec.source_ref, "condition": rec["condition"],
        "brief_profile": rec.get("brief_profile"), "model": rec.get("model"),
        "policy_visible": {"instruction": _instruction(rec, tdir), "agent_trajectory": traj,
                           "stakeholder_replies": replies, "stakeholder_replies_source": "agent-trajectory-observations"},
        "training_eligible": eligible,
        "evaluator_only": {"ledger": [f.model_dump(mode="json") for f in spec.ledger],
                           "author_only_fact_ids": [f.id for f in spec.ledger if f.discovery == Discovery.AUTHOR_ONLY],
                           "oracle_asset": spec.controls.oracle, "test_assets": list(spec.contract.test_assets),
                           "report": report, "stakeholder_audit": audit, "reviewer_label": label},
        "lineage": {"spec_hash": spec.spec_hash, "run_config_hash": rec.get("run_config_hash"),
                    "compiled_hash": rec.get("compiled_hash"), "image_digest": report.get("image_digest"),
                    "purpose": rec.get("purpose"), "patch_sha256": report.get("patch_sha256"),
                    "transcript_hash": transcript.get("transcript_hash"), "harbor_trial_dir": rec.get("trial_dir"),
                    "attempt_index": rec.get("attempt_index")},
        "reward": rec.get("reward") or {}, "simulator_validity": simulator_validity,
        "interaction": {"patience_start": transcript.get("patience_start"), "patience_end": transcript.get("patience_end"),
                        "questions": count_exchanges(rec, "ask"), "reviews": count_exchanges(rec, "review"),
                        "questions_observed_by_policy": sum(1 for r in replies if r["shim"] == "ask-user"),
                        "reviews_observed_by_policy": sum(1 for r in replies if r["shim"] == "request-review")},
    }


def export_scenario(ws: Workspace, scenario_id: str) -> list[Path]:
    ws.scenario_dir(scenario_id)  # validates the id
    outs: list[Path] = []
    (ws.root / "exports").mkdir(parents=True, exist_ok=True)
    for rec in load_trial_records(ws, scenario_id):
        if not rec.get("trial_id"):
            continue
        p = ws.root / "exports" / f"{rec['trial_id']}.episode.json"
        atomic_json(p, episode_manifest(ws, rec, scenario_id))
        outs.append(p)
    return outs
