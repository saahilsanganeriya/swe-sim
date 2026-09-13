"""Export stage: one ``workspace/exports/<trial_id>.episode.json`` manifest per finished trial.

``policy_visible`` holds only what the agent actually observed: the instruction text, the ATIF trajectory
from Harbor's ``agent/trajectory.json`` and, for interactive trials, the stakeholder replies as they
appeared in the agent's own tool observations (a shim actually invoked as a command, not merely mentioned).
The stakeholder service's generated transcript (with fact annotations and tool audit) is evidence for the
evaluator and lives under ``evaluator_only``: a generated reply is not proof the policy received it.

Lineage comes from the frozen record: ``lineage.spec_hash`` is the record's; the spec used to fill
``evaluator_only`` is the trial's frozen ``spec.json`` when present, else the scenario's current
``scenario.json`` with ``lineage.spec_mismatch`` set when its hash differs. Interactive episodes stay
``simulator_validity: unadjudicated`` and are never ``training_eligible`` until adjudicated; episodes with
infrastructure outcomes (5-7) or a spec mismatch are never training-eligible in any condition.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from swesim.models import Discovery, ScenarioSpec
from swesim.paths import Workspace, atomic_json
from swesim.stages.score import (
    VALID,
    count_exchanges,
    load_trial_records,
    message_events,
    outcome_code,
)

SHIMS = ("ask-user", "confirm-behavior", "request-review")
# First token of a command segment, with an optional path prefix, followed by end or whitespace.
_SHIM_RE = re.compile(r"^\s*(?:\S*/)?(ask-user|confirm-behavior|request-review)(?:\s+(.*))?$", re.DOTALL)
_SEGMENTS = re.compile(r"&&|\|\||;|\n")


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def shim_invocation(command: str) -> str | None:
    """Name of the stakeholder shim a shell command actually invokes, or None.

    Anchored to the first token of a command segment (segments split on ``&&``, ``||``, ``;`` and newlines, so
    ``cd /app && ask-user 'q'`` counts), allowing a path prefix (``/usr/local/bin/ask-user``). Mentions inside
    arguments (``cat /usr/local/bin/ask-user``, ``grep request-review README.md``) and bare ``--help``/``-h``
    calls are not invocations.
    """
    for segment in _SEGMENTS.split(command or ""):
        m = _SHIM_RE.match(segment)
        if m and (m.group(2) or "").strip() not in ("--help", "-h"):
            return m.group(1)
    return None


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
    """Stakeholder replies the policy actually saw: shim commands in the agent's tool calls paired with the
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
            shim = shim_invocation(text)
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


def _spec_for(rec: dict, tdir: Path, ws: Workspace, scenario_id: str) -> tuple[ScenarioSpec, str]:
    """(spec, source): the trial's frozen ``spec.json`` when present, else the scenario's current ``scenario.json``."""
    frozen = tdir / "spec.json"
    if frozen.exists():
        return ScenarioSpec.model_validate_json(frozen.read_text()), "trial-spec.json"
    current = ws.scenario_dir(scenario_id) / "scenario.json"
    if current.exists():
        return ScenarioSpec.model_validate_json(current.read_text()), "scenario.json"
    raise ValueError(f"no frozen spec for trial {rec.get('trial_id')}")


def episode_manifest(ws: Workspace, rec: dict, scenario_id: str) -> dict:
    if not rec.get("spec_hash"):
        raise ValueError(f"missing frozen lineage for trial {rec.get('trial_id')}")
    tdir = ws.root / "trials" / rec["trial_id"]
    spec, spec_source = _spec_for(rec, tdir, ws, scenario_id)
    spec_mismatch = spec.spec_hash != rec["spec_hash"]
    traj: dict = {}
    trial_dir = Path(rec["trial_dir"]) if rec.get("trial_dir") else None
    if trial_dir is not None:
        traj = _read_json(trial_dir / "agent" / "trajectory.json", default={}) or {}
    report = rec.get("report") or {}
    transcript = rec.get("transcript") or {}
    audit = message_events(rec)
    interactive = rec["condition"] == "interactive"
    simulator_validity = report.get("simulator_validity") or ("unadjudicated" if interactive else "n/a")
    # Training eligibility: a real trajectory, an agent-attributable outcome (1-4), a spec that matches the record,
    # no explicit veto from the run stage, and for interactive trials a bound transcript adjudicated "ok".
    eligible = (bool(traj.get("steps")) and outcome_code(rec) in VALID and not spec_mismatch
                and report.get("training_eligible") is not False)
    if interactive:
        eligible = eligible and report.get("transcript_binding_valid") is True and simulator_validity == "ok"
    replies = delivered_replies(traj) if interactive else []
    label = rec.get("label") if isinstance(rec.get("label"), dict) else None
    return {
        "trial_id": rec["trial_id"], "scenario_id": rec.get("scenario_id") or spec.id, "family_id": spec.source_ref,
        "condition": rec["condition"], "brief_profile": rec.get("brief_profile"), "model": rec.get("model"),
        "policy_visible": {"instruction": _instruction(rec, tdir), "agent_trajectory": traj,
                           "stakeholder_replies": replies, "stakeholder_replies_source": "agent-trajectory-observations"},
        "training_eligible": eligible,
        "evaluator_only": {"ledger": [f.model_dump(mode="json") for f in spec.ledger],
                           "author_only_fact_ids": [f.id for f in spec.ledger if f.discovery == Discovery.AUTHOR_ONLY],
                           "oracle_asset": spec.controls.oracle, "test_assets": list(spec.contract.test_assets),
                           "report": report, "stakeholder_audit": audit, "reviewer_label": label},
        "lineage": {"spec_hash": rec["spec_hash"], "spec_source": spec_source, "spec_mismatch": spec_mismatch,
                    "loaded_spec_hash": spec.spec_hash, "run_config_hash": rec.get("run_config_hash"),
                    "compiled_hash": rec.get("compiled_hash"), "image_digest": report.get("image_digest"),
                    "purpose": rec.get("purpose"), "patch_sha256": report.get("patch_sha256"),
                    "transcript_hash": transcript.get("transcript_hash"), "harbor_trial_dir": rec.get("trial_dir"),
                    "attempt_index": rec.get("attempt_index")},
        "reward": rec.get("reward") or {}, "simulator_validity": simulator_validity,
        "interaction": {"patience_start": transcript.get("patience_start"), "patience_end": transcript.get("patience_end"),
                        "questions": count_exchanges(rec, "ask"), "reviews": count_exchanges(rec, "review"),
                        "confirmations": count_exchanges(rec, "confirm"),
                        "questions_observed_by_policy": sum(1 for r in replies if r["shim"] == "ask-user"),
                        "reviews_observed_by_policy": sum(1 for r in replies if r["shim"] == "request-review"),
                        "confirmations_observed_by_policy": sum(1 for r in replies if r["shim"] == "confirm-behavior")},
    }


def export_scenario(ws: Workspace, scenario_id: str) -> list[Path]:
    ws.scenario_dir(scenario_id)  # validates the id
    outs: list[Path] = []
    (ws.root / "exports").mkdir(parents=True, exist_ok=True)
    for rec in load_trial_records(ws, scenario_id):
        if not rec.get("trial_id") or rec.get("unresolved"):
            continue  # unresolved attempts have no observations or reward to export
        p = ws.root / "exports" / f"{rec['trial_id']}.episode.json"
        atomic_json(p, episode_manifest(ws, rec, scenario_id))
        outs.append(p)
    return outs
