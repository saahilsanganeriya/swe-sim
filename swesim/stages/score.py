"""Score stage: outcome counts, pass@k, pass^k and interaction counts per frozen run configuration.

Reads ``workspace/trials/<trial_id>/record.json`` (a TrialRecord dump; lineage fields may be flat or
under ``run_config``; sibling files fill gaps), groups attempts by ``condition|brief_profile|run_config_hash``
-- ``run_config_hash`` already binds the actual model, purpose and build, so calibration and comparison
runs can never share a group -- and writes ``workspace/scenarios/<id>/scores.json``.

Denominators: only outcomes 1 (valid-correct) and 2 (valid-incorrect) are attempts for pass@k / pass^k.
Agent failures (3, 4) and infrastructure failures (5, 6, 7) are reported as counts only. Metrics with
insufficient samples (n < k, or n == 0) are ``null``; k is never silently reduced.
"""
from __future__ import annotations

import json
from math import comb
from pathlib import Path

from swesim.paths import Workspace, atomic_json

OUTCOME_NAMES = {1: "valid-correct", 2: "valid-incorrect", 3: "agent-invalid-submission", 4: "agent-runtime-failure",
                 5: "collection-failure", 6: "simulator-failure", 7: "evaluator-failure"}
VALID = {1, 2}
AGENT_FAILURE = {3, 4}
INFRA = {5, 6, 7}
PURPOSES = ("calibration", "comparison", "demonstration")
# RunConfig fields a record may carry flat (TrialRecord dataclass) or nested under ``run_config``.
_LINEAGE_FIELDS = ("model", "purpose", "brief_profile", "compiled_hash")


def _check_counts(n: int, c: int, k: int) -> None:
    if not 0 <= c <= n or k < 1:
        raise ValueError("invalid pass metric counts")


def pass_at_k(n: int, c: int, k: int) -> float | None:
    """1 - C(n-c, k) / C(n, k): probability at least one of k draws (without replacement) succeeds. None if n < k."""
    _check_counts(n, c, k)
    if n == 0 or k > n:
        return None
    return 1.0 - comb(n - c, k) / comb(n, k)


def pass_pow_k(n: int, c: int, k: int) -> float | None:
    """C(c, k) / C(n, k): probability all k draws (without replacement) succeed. None if n < k."""
    _check_counts(n, c, k)
    if n < k:
        return None
    return comb(c, k) / comb(n, k)


def _read_json(path: Path | None, default=None):
    if path is None:
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def outcome_code(rec: dict) -> int:
    """Numeric outcome from reward.json; anything missing or unknown is attributed to the evaluator (7)."""
    try:
        code = int((rec.get("reward") or {}).get("outcome_code", 7))
    except (TypeError, ValueError):
        return 7
    return code if code in OUTCOME_NAMES else 7


def message_events(rec: dict) -> list[dict]:
    return [e for e in (rec.get("transcript") or {}).get("events", []) if isinstance(e, dict) and e.get("kind") == "message"]


def count_exchanges(rec: dict, exchange: str) -> int:
    """Agent-initiated exchanges of one kind (``ask`` or ``review``) recorded by the stakeholder service."""
    return sum(1 for e in message_events(rec) if e.get("role") == "user" and e.get("exchange") == exchange)


def normalize_record(rec: dict, trial_dir: Path | None = None) -> dict:
    """Flatten a TrialRecord dump so every consumer sees the same keys.

    Lineage (``run_config_hash``, ``model``, ``purpose``, ``brief_profile``, ``compiled_hash``, ``spec_hash``,
    ``scenario_id``) is taken from flat fields first, then from the embedded ``run_config``, then from the
    sibling ``run_config.json`` / ``spec.json`` written with the run. Missing keys stay missing; callers decide
    whether that is fatal. ``label`` comes from the reviewer's ``label.json`` (None when absent).
    """
    out = dict(rec)
    rc = out.get("run_config") or _read_json(trial_dir / "run_config.json" if trial_dir else None) or {}
    if rc:
        out.setdefault("run_config", rc)
    for key in _LINEAGE_FIELDS:
        if out.get(key) in (None, "") and rc.get(key) not in (None, ""):
            out[key] = rc[key]
    if not out.get("run_config_hash") and rc:
        try:
            from swesim.models import RunConfig
            out["run_config_hash"] = RunConfig.model_validate(rc).run_config_hash
        except Exception:  # noqa: BLE001 - a drifted dump must not crash the loader; scoring rejects it below
            pass
    if trial_dir is not None:
        spec_json = _read_json(trial_dir / "spec.json") if not (out.get("spec_hash") and out.get("scenario_id")) else None
        if spec_json:
            out.setdefault("scenario_id", spec_json.get("id"))
            if not out.get("spec_hash"):
                try:
                    from swesim.models import ScenarioSpec
                    out["spec_hash"] = ScenarioSpec.model_validate(spec_json).spec_hash
                except Exception:  # noqa: BLE001
                    pass
        for name in ("reward", "report", "transcript"):
            if not out.get(name):
                out[name] = _read_json(trial_dir / f"{name}.json", default=out.get(name) or {})
        status_file = _read_json(trial_dir / "status.json")
        if status_file is not None:
            out["status_file"] = status_file
        out["label"] = _read_json(trial_dir / "label.json")
    else:
        out.setdefault("label", None)
    return out


def load_trial_records(ws: Workspace, scenario_id: str | None = None) -> list[dict]:
    """All finished trial records under ``workspace/trials`` (normalized), optionally for one scenario."""
    recs = []
    for rec_path in sorted((ws.root / "trials").glob("*/record.json")):
        raw = _read_json(rec_path)
        if not isinstance(raw, dict):
            continue
        rec = normalize_record(raw, rec_path.parent)
        if scenario_id is not None and rec.get("scenario_id") != scenario_id:
            continue
        recs.append(rec)
    return recs


def group_key(rec: dict) -> str:
    return f"{rec['condition']}|{rec.get('brief_profile') or 'natural'}|{rec['run_config_hash']}"


def _single(values: set, what: str, key: str):
    if len(values) > 1:
        raise ValueError(f"refusing to pool {what} {sorted(map(str, values))} in group {key}")
    return next(iter(values)) if values else None


def summarize(records: list[dict], k: int) -> dict:
    """Per-group metrics. Groups are ``condition|brief_profile|run_config_hash``; a group with more than one
    model or purpose is a lineage bug and raises rather than pooling."""
    groups: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for r in records:
        tid = r.get("trial_id")
        if tid:
            if tid in seen:
                raise ValueError(f"duplicate attempt {tid}")
            seen.add(tid)
        try:
            key = group_key(r)
        except KeyError as e:
            raise ValueError(f"record {tid or '<no trial_id>'} lacks {e.args[0]}") from e
        groups.setdefault(key, []).append(r)
    out: dict[str, dict] = {}
    for key, recs in groups.items():
        recs = sorted(recs, key=lambda r: (r.get("attempt_index") if isinstance(r.get("attempt_index"), int) else 1 << 30,
                                           str(r.get("trial_id") or "")))
        codes = [outcome_code(r) for r in recs]
        valid = [r for r, code in zip(recs, codes) if code in VALID]
        n = len(valid)
        c = sum(1 for r in valid if int((r.get("reward") or {}).get("success", 0) or 0) == 1)
        counts: dict[str, int] = {}
        for code in codes:
            counts[OUTCOME_NAMES[code]] = counts.get(OUTCOME_NAMES[code], 0) + 1
        labels: dict[str, int] = {}
        for r in recs:
            name = (r.get("label") or {}).get("label") if isinstance(r.get("label"), dict) else None
            if name:
                labels[name] = labels.get(name, 0) + 1
        cfg_k = {(r.get("run_config") or {}).get("k") for r in recs} - {None}
        condition, profile, rch = key.split("|", 2)
        out[key] = {
            "condition": condition, "brief_profile": profile, "run_config_hash": rch,
            "model": _single({r.get("model") for r in recs} - {None, ""}, "models", key),
            "purpose": _single({r.get("purpose") for r in recs} - {None, ""}, "purposes", key),
            "requested_k": k, "configured_k": _single(cfg_k, "configured k values", key),
            "estimator": "without-replacement",
            "n": n, "n_valid": n, "n_total": len(recs),
            "n_agent_failures": sum(1 for code in codes if code in AGENT_FAILURE),
            "n_infra_failures": sum(1 for code in codes if code in INFRA),
            "success_count": c,
            "pass_at_k": pass_at_k(n, c, k), "pass_pow_k": pass_pow_k(n, c, k),
            "mean_reward": (sum(float((r.get("reward") or {}).get("reward", 0) or 0) for r in valid) / n) if n else None,
            "outcomes": counts,
            "questions_asked": [count_exchanges(r, "ask") for r in valid],
            "reviews": [count_exchanges(r, "review") for r in valid],
            "reviews_requested": [count_exchanges(r, "review") for r in valid],
            "patience_end": [(r.get("transcript") or {}).get("patience_end") for r in valid],
            # Diff-only pilot: the review snapshot is unverified, so this stays null until a trusted protocol exists.
            "reviewed_equals_submitted": [(r.get("report") or {}).get("reviewed_equals_submitted") for r in valid],
            "labels": labels,
            "trial_ids": [r.get("trial_id") for r in recs],
        }
    return out


def score_scenario(ws: Workspace, scenario_id: str, k: int = 3) -> Path:
    """Write ``workspace/scenarios/<id>/scores.json`` for every finished trial of the scenario."""
    out = ws.scenario_dir(scenario_id) / "scores.json"  # validates the id
    recs = load_trial_records(ws, scenario_id)
    for rec in recs:
        if rec.get("trial_id") and not (rec.get("spec_hash") and rec.get("run_config_hash")):
            raise ValueError(f"missing frozen lineage for trial {rec['trial_id']}")
    atomic_json(out, summarize(recs, k))
    return out
