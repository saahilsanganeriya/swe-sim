"""Score stage: outcome counts, pass@k, pass^k and interaction counts per frozen run configuration.

Reads ``workspace/trials/<trial_id>/record.json`` (a TrialRecord dump; lineage fields may be flat or
under ``run_config``; sibling files fill gaps), groups attempts by ``condition|brief_profile|run_config_hash``
-- ``run_config_hash`` already binds the actual model, purpose and build, so calibration and comparison
runs can never share a group -- and writes ``workspace/scenarios/<id>/scores.json``.

Scaffold identity (B-P2-5): the hash used for grouping is the *effective* one. A record whose ``run_config``
predates ``RunConfig.agent_config`` but whose ``report.agent_config_sha256`` proves the trial ran with the
interactive scaffold is regrouped under the hash its run config would carry today (``effective_run_config_hash``),
so legacy scaffold trials pool with new scaffold trials and never with default-agent trials. Trial ids and the
recorded ``run_config_hash`` are never rewritten; each group lists ``regrouped_trials`` and
``recorded_run_config_hashes`` so the split is auditable.

Denominators (integrator ruling 2026-09-12): outcomes 1-4 are agent-attributable trial results and all
count in n for pass@k / pass^k; only 1 (valid-correct) counts as a success, 2-4 (valid-incorrect,
agent-invalid-submission, agent-runtime-failure) count as failures. Infrastructure outcomes 5-7 are excluded
from n and appear only in the outcome counts. Metrics with insufficient samples (n < k, or n == 0) are
``null``; k is never silently reduced.
"""
from __future__ import annotations

import contextlib
import json
from math import comb
from pathlib import Path

from swesim.paths import Workspace, atomic_json

OUTCOME_NAMES = {1: "valid-correct", 2: "valid-incorrect", 3: "agent-invalid-submission", 4: "agent-runtime-failure",
                 5: "collection-failure", 6: "simulator-failure", 7: "evaluator-failure"}
VALID = {1, 2, 3, 4}  # agent-attributable results: in the pass@k denominator; only 1 is a success
AGENT_FAILURE = {3, 4}  # subset of VALID, reported separately as n_agent_failures
INFRA = {5, 6, 7}  # excluded from n; counted in outcomes / n_infra_failures
PURPOSES = ("calibration", "comparison", "demonstration")
# Files that make a record-less trial directory an "unresolved attempt" instead of nothing at all.
UNRESOLVED_FILES = ("status.json", "runner-error.json", "request.json", "launch.json")
UNKNOWN_GROUP = "unknown-config"
# RunConfig fields a record may carry flat (TrialRecord dataclass) or nested under ``run_config``.
_LINEAGE_FIELDS = ("model", "purpose", "brief_profile", "compiled_hash")
# Scaffold identity as run.scaffold_identity records it in RunConfig.agent_config: prefix + sha256(agent_config.yaml)[:16].
SCAFFOLD_PREFIX = "mini-swe-agent.interactive:"


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
    """Agent-initiated exchanges of one kind (``ask``, ``confirm`` or ``review``) recorded by the stakeholder service."""
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
        # A drifted run_config dump must not crash the loader; score_scenario rejects the missing lineage.
        with contextlib.suppress(Exception):
            from swesim.models import RunConfig
            out["run_config_hash"] = RunConfig.model_validate(rc).run_config_hash
    if trial_dir is not None:
        spec_json = _read_json(trial_dir / "spec.json") if not (out.get("spec_hash") and out.get("scenario_id")) else None
        if spec_json:
            out.setdefault("scenario_id", spec_json.get("id"))
            if not out.get("spec_hash"):
                with contextlib.suppress(Exception):  # unreadable frozen spec: lineage stays missing
                    from swesim.models import ScenarioSpec
                    out["spec_hash"] = ScenarioSpec.model_validate(spec_json).spec_hash
        for name in ("reward", "report", "transcript"):
            if not out.get(name):
                out[name] = _read_json(trial_dir / f"{name}.json", default=out.get(name) or {})
        status_file = _read_json(trial_dir / "status.json")
        if status_file is not None:
            out["status_file"] = status_file
        out["label"] = _read_json(trial_dir / "label.json")
    else:
        out.setdefault("label", None)
    out["scaffold"] = scaffold_identity(out)
    return out


def _run_config_hash_of(rc: dict) -> str | None:
    with contextlib.suppress(Exception):
        from swesim.models import RunConfig
        return RunConfig.model_validate(rc).run_config_hash
    return None


def _report_scaffold(rec: dict) -> str:
    """Scaffold identity derived from ``report.agent_config_sha256`` (the digest the run stage has always written for
    interactive scaffold trials); empty when the report records none."""
    sha = (rec.get("report") or {}).get("agent_config_sha256")
    return SCAFFOLD_PREFIX + sha[:16] if isinstance(sha, str) and sha else ""


def scaffold_identity(rec: dict) -> str:
    """The agent scaffold a trial actually ran with: ``run_config.agent_config`` when the run recorded it, else the identity
    derived from the report digest. Empty string = the agent's packaged defaults (no scaffold)."""
    rc = rec.get("run_config") or {}
    if isinstance(rc, dict) and rc.get("agent_config"):
        return str(rc["agent_config"])
    return _report_scaffold(rec)


def effective_run_config_hash(rec: dict) -> tuple[str | None, bool]:
    """``(hash to group by, regrouped)``. The recorded ``run_config_hash`` stands unless the run config lacks
    ``agent_config`` while the report proves a scaffold: then the hash is recomputed with the derived identity, which is
    exactly the hash a current run with that scaffold carries. A record without a usable run config keeps its recorded
    hash (nothing to derive from)."""
    rc = rec.get("run_config") or {}
    ident = scaffold_identity(rec)
    if isinstance(rc, dict) and rc and not rc.get("agent_config") and ident:
        derived = _run_config_hash_of({**rc, "agent_config": ident})
        if derived:
            return derived, derived != rec.get("run_config_hash")
    return rec.get("run_config_hash"), False


def _task_metadata(trial_dir: Path) -> dict:
    """``[metadata]`` of the materialized ``task/task.toml`` (the run stage writes run_config_hash, compiled_hash,
    condition and brief_profile there before launching; it survives when the run dies before record.json)."""
    path = trial_dir / "task" / "task.toml"
    if not path.exists():
        return {}
    with contextlib.suppress(Exception):
        import tomllib
        meta = tomllib.loads(path.read_text()).get("metadata") or {}
        return meta if isinstance(meta, dict) else {}
    return {}


def _run_config_for(trial_dir: Path) -> tuple[dict, str | None, str | None]:
    """(run_config, run_config_hash, source) for an attempt without a record: ``run_config.json`` when present,
    else ``request.json``'s ``run_config`` (Temporal launches), else the task.toml metadata (hash + condition)."""
    rc = _read_json(trial_dir / "run_config.json")
    if isinstance(rc, dict) and rc:
        return rc, _run_config_hash_of(rc), "run_config.json"
    req = _read_json(trial_dir / "request.json")
    if isinstance(req, dict) and isinstance(req.get("run_config"), dict) and req["run_config"]:
        return req["run_config"], req.get("run_config_hash") or _run_config_hash_of(req["run_config"]), "request.json"
    meta = _task_metadata(trial_dir)
    if meta.get("run_config_hash"):
        return ({k: meta[k] for k in ("condition", "brief_profile", "compiled_hash") if meta.get(k)},
                str(meta["run_config_hash"]), "task/task.toml")
    return {}, None, None


def unresolved_attempt(trial_dir: Path) -> dict | None:
    """Describe an attempt directory that has no usable ``record.json`` from whatever files exist, so it cannot
    vanish from the score tables. None when none of ``UNRESOLVED_FILES`` (or an unreadable record.json) is present.

    ``status`` is taken from runner-error.json (Temporal runner crash), else status.json (``error`` on the run
    stage's unknown-exception path, ``running`` for a launch whose unit vanished), else launch.json, else
    ``requested`` (request.json only)."""
    present = [n for n in UNRESOLVED_FILES if (trial_dir / n).exists()]
    record_path = trial_dir / "record.json"
    unreadable_record = record_path.exists() and not isinstance(_read_json(record_path), dict)
    if not present and not unreadable_record:
        return None
    status_file = _read_json(trial_dir / "status.json") or {}
    runner_error = _read_json(trial_dir / "runner-error.json") or {}
    request = _read_json(trial_dir / "request.json") or {}
    launch = _read_json(trial_dir / "launch.json") or {}
    if unreadable_record:
        status, error_type, source = "unreadable-record", "unreadable-record", "record.json"
    elif runner_error:
        status, error_type, source = "runner-error", runner_error.get("type") or "runner-error", "runner-error.json"
    elif status_file.get("status"):
        status, error_type, source = str(status_file["status"]), status_file.get("error"), "status.json"
    elif launch.get("phase") == "failed":
        status, error_type, source = "launch-failed", "launch-failed", "launch.json"
    elif launch:
        status, error_type, source = f"launch-{launch.get('phase') or 'unknown'}", None, "launch.json"
    else:
        status, error_type, source = "requested", None, "request.json"
    rc, rch, rc_source = _run_config_for(trial_dir)
    spec_json = _read_json(trial_dir / "spec.json") or {}
    meta = _task_metadata(trial_dir)
    return {
        "trial_id": trial_dir.name, "unresolved": True, "status": status, "error_type": error_type,
        "source_file": source, "source_files": present + (["record.json"] if unreadable_record else []),
        "started_at": status_file.get("started_at"), "finished_at": status_file.get("finished_at"),
        "scenario_id": request.get("scenario_id") or spec_json.get("id"), "spec_hash": request.get("spec_hash"),
        "condition": rc.get("condition") or request.get("condition") or meta.get("condition"),
        "brief_profile": rc.get("brief_profile") or meta.get("brief_profile"),
        "model": rc.get("model") or request.get("model"), "purpose": rc.get("purpose"),
        "compiled_hash": rc.get("compiled_hash") or meta.get("compiled_hash"),
        "run_config": rc or None, "run_config_hash": rch, "run_config_source": rc_source,
        "attempt_index": request.get("attempt_index"),
        "reward": {}, "report": {}, "transcript": {}, "label": _read_json(trial_dir / "label.json"),
    }


def _belongs(rec: dict, ws: Workspace, scenario_id: str, cache: dict) -> bool:
    """Scenario attribution: record/request/spec id first; for unresolved attempts without an id, the compiled hash
    of the scenario's build or its spec hash."""
    if rec.get("scenario_id") is not None:
        return rec["scenario_id"] == scenario_id
    if not rec.get("unresolved"):
        return False
    if "compiled_hash" not in cache:
        sdir = ws.scenario_dir(scenario_id)
        cache["compiled_hash"] = (_read_json(sdir / "compile.json") or {}).get("compiled_hash")
        cache["spec_hash"] = None
        spec_json = _read_json(sdir / "scenario.json")
        if spec_json:
            with contextlib.suppress(Exception):
                from swesim.models import ScenarioSpec
                cache["spec_hash"] = ScenarioSpec.model_validate(spec_json).spec_hash
    return bool((rec.get("compiled_hash") and rec["compiled_hash"] == cache["compiled_hash"])
                or (rec.get("spec_hash") and rec["spec_hash"] == cache["spec_hash"]))


def load_trial_records(ws: Workspace, scenario_id: str | None = None, include_unresolved: bool = True) -> list[dict]:
    """Every attempt under ``workspace/trials`` (normalized), optionally for one scenario.

    Finished attempts come from ``record.json``. Attempts that ended without one (run stage unknown-exception path:
    ``status.json`` with ``status: error``; Temporal runner: ``runner-error.json``; a launch that left ``status.json``
    at ``running`` or only ``request.json`` / ``launch.json``) are yielded as unresolved entries (``unresolved: True``)
    so they never vanish. An unresolved attempt is attributed to a scenario by its request/spec id, else by the
    scenario's compiled hash or spec hash; unattributable ones appear only in the unfiltered listing.
    """
    recs: list[dict] = []
    trials = ws.root / "trials"
    if not trials.is_dir():
        return recs
    cache: dict = {}
    for d in sorted(p for p in trials.iterdir() if p.is_dir()):
        raw = _read_json(d / "record.json") if (d / "record.json").exists() else None
        if isinstance(raw, dict) and raw:
            rec = normalize_record(raw, d)
        else:
            if not include_unresolved:
                continue
            rec = unresolved_attempt(d)
            if rec is None:
                continue
        if scenario_id is not None and not _belongs(rec, ws, scenario_id, cache):
            continue
        recs.append(rec)
    return recs


def group_key(rec: dict) -> str:
    """``condition|brief_profile|<effective run_config_hash>`` (see ``effective_run_config_hash``)."""
    ehash, _ = effective_run_config_hash(rec)
    return f"{rec['condition']}|{rec.get('brief_profile') or 'natural'}|{ehash if ehash else rec['run_config_hash']}"


def _single(values: set, what: str, key: str):
    if len(values) > 1:
        raise ValueError(f"refusing to pool {what} {sorted(map(str, values))} in group {key}")
    return next(iter(values)) if values else None


def _attempt_order(r: dict):
    return (r.get("attempt_index") if isinstance(r.get("attempt_index"), int) else 1 << 30, str(r.get("trial_id") or ""))


def summarize(records: list[dict], k: int) -> dict:
    """Per-group metrics. Groups are ``condition|brief_profile|run_config_hash``; a group with more than one model or
    purpose is a lineage bug and raises rather than pooling.

    Unresolved attempts (``unresolved: True``, no record.json) are reported per group as ``attempts_unresolved`` with a
    per-status breakdown and a detail list; they never enter ``n``, ``success_count``, pass@k or ``mean_reward``.
    Those without a recoverable run config fall under the ``unknown-config`` group."""
    groups: dict[str, list[dict]] = {}
    pending: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for r in records:
        tid = r.get("trial_id")
        if tid:
            if tid in seen:
                raise ValueError(f"duplicate attempt {tid}")
            seen.add(tid)
        if r.get("unresolved"):
            key = group_key(r) if r.get("run_config_hash") and r.get("condition") else UNKNOWN_GROUP
            pending.setdefault(key, []).append(r)
            continue
        try:
            key = group_key(r)
        except KeyError as e:
            raise ValueError(f"record {tid or '<no trial_id>'} lacks {e.args[0]}") from e
        groups.setdefault(key, []).append(r)
    out: dict[str, dict] = {}
    for key in sorted(set(groups) | set(pending)):
        recs = sorted(groups.get(key, []), key=_attempt_order)
        unres = sorted(pending.get(key, []), key=_attempt_order)
        codes = [outcome_code(r) for r in recs]
        valid = [r for r, code in zip(recs, codes) if code in VALID]
        n = len(valid)
        c = sum(1 for r in valid if int((r.get("reward") or {}).get("success", 0) or 0) == 1)
        counts: dict[str, int] = {}
        for code in codes:
            counts[OUTCOME_NAMES[code]] = counts.get(OUTCOME_NAMES[code], 0) + 1
        labels: dict[str, int] = {}
        for r in recs + unres:
            name = (r.get("label") or {}).get("label") if isinstance(r.get("label"), dict) else None
            if name:
                labels[name] = labels.get(name, 0) + 1
        cfg_k = {(r.get("run_config") or {}).get("k") for r in recs} - {None}
        # reward.json must agree with itself: success == 1 exactly when outcome_code == 1. `success` still drives
        # pass@k (contract); disagreement is flagged per trial so a skewed success_count is never silent.
        inconsistent = [r.get("trial_id") or f"#{i}" for i, (r, code) in enumerate(zip(recs, codes))
                        if (int((r.get("reward") or {}).get("success", 0) or 0) == 1) != (code == 1)]
        by_status: dict[str, int] = {}
        for r in unres:
            by_status[str(r.get("status") or "unknown")] = by_status.get(str(r.get("status") or "unknown"), 0) + 1
        if key == UNKNOWN_GROUP:
            condition = profile = rch = None
            model = purpose = scaffold = None  # attempts with no recoverable run config: nothing to bind them to
            regrouped: list = []
        else:
            condition, profile, rch = key.split("|", 2)
            model = _single({r.get("model") for r in recs + unres} - {None, ""}, "models", key)
            purpose = _single({r.get("purpose") for r in recs + unres} - {None, ""}, "purposes", key)
            # The key already binds the effective scaffold; the report digests must agree with it (a run_config claiming one
            # scaffold while the report proves another is a lineage bug, never pooled).
            scaffold = _single({scaffold_identity(r) for r in recs} | {s for r in recs if (s := _report_scaffold(r))}, "scaffolds", key) or None
            regrouped = [r.get("trial_id") for r in recs if effective_run_config_hash(r)[1]]
        out[key] = {
            "condition": condition, "brief_profile": profile, "run_config_hash": rch, "model": model, "purpose": purpose,
            "scaffold": scaffold, "regrouped_trials": regrouped,
            "recorded_run_config_hashes": sorted({r.get("run_config_hash") for r in recs if r.get("run_config_hash")}),
            "requested_k": k, "configured_k": _single(cfg_k, "configured k values", key),
            "estimator": "without-replacement",
            "n": n, "n_valid": n, "n_total": len(recs),
            "attempts_total": len(recs) + len(unres), "attempts_unresolved": len(unres),
            "unresolved_by_status": by_status,
            "unresolved": [{"trial_id": r.get("trial_id"), "status": r.get("status"), "error_type": r.get("error_type"),
                            "source_file": r.get("source_file"), "started_at": r.get("started_at"),
                            "finished_at": r.get("finished_at")} for r in unres],
            "n_agent_failures": sum(1 for code in codes if code in AGENT_FAILURE),
            "n_infra_failures": sum(1 for code in codes if code in INFRA),
            "success_count": c, "inconsistent_reward": inconsistent,
            "pass_at_k": pass_at_k(n, c, k), "pass_pow_k": pass_pow_k(n, c, k),
            "mean_reward": (sum(float((r.get("reward") or {}).get("reward", 0) or 0) for r in valid) / n) if n else None,
            "outcomes": counts,
            "questions_asked": [count_exchanges(r, "ask") for r in valid],
            "reviews": [count_exchanges(r, "review") for r in valid],
            "reviews_requested": [count_exchanges(r, "review") for r in valid],
            "confirmations": [count_exchanges(r, "confirm") for r in valid],
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
        if rec.get("unresolved"):
            continue  # no record: reported as an unresolved attempt, never as a scored trial
        if rec.get("trial_id") and not (rec.get("spec_hash") and rec.get("run_config_hash")):
            raise ValueError(f"missing frozen lineage for trial {rec['trial_id']}")
    atomic_json(out, summarize(recs, k))
    return out
