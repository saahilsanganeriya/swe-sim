"""``FileIndexReader``: the ``IndexReader`` protocol (interfaces §2) implemented over the workspace files.

This is the console stream's local shim for running without the Postgres index (laptop mirror, tests) until the storage
stream's ``swesim.index.reads.FileIndexReader`` is merged; ``swesim.dashboard.access.reader_for`` prefers the storage
implementation whenever ``swesim.index`` is importable.

Contract
- Every method returns the spec's row models built from the record files (``scenarios/<sid>/*``, ``trials/<tid>/*``,
  ``candidates/*``, ``registry.json``); nothing is invented: a value the files do not carry is ``None``.
- Attempt directories whose lineage cannot be determined (no condition/model in any file) are not projected into
  ``TrialRow`` (the row model requires them); they are listed in ``unprojected`` so the Trials page can show them.
- Reads are defensive (a malformed file is an empty state, never an exception) and cached for ``TTL`` seconds so a page
  that makes several reader calls scans the workspace once.
- ``add_operator_action`` appends to ``evidence/console/actions.jsonl`` (the audit trail when the index is disabled).
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from swesim.dashboard import rows as R
from swesim.stages.score import OUTCOME_NAMES, UNRESOLVED_FILES, VALID, count_exchanges, normalize_record, outcome_code, unresolved_attempt

TRIAL_RE = re.compile(r"^[a-f0-9]{24}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
TTL = 3.0
STAGES: tuple[str, ...] = ("mined", "judged", "probed", "drafted", "approved", "compiled", "validated", "registered")
SCENARIO_FILES = ("scenario.json", "scenario.draft.json", "draft.meta.json", "judge.json", "approval.json", "compile.json", "validation.json",
                  "difficulty.json", "audit.json", "scores.json", "review.json")
TRIAL_STATUS = {"completed": "completed", "finished": "completed", "running": "running", "runner-error": "runner-error", "launch-failed": "launch-failed",
                "requested": "planned", "planned": "planned", "cancelled": "cancelled", "unit-failed": "unit-failed"}


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def _dt(value) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), UTC)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            try:
                return datetime.fromtimestamp(float(value), UTC)
            except ValueError:
                return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _mtime(p: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, UTC)
    except OSError:
        return None


def _dec(v) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except (ValueError, ArithmeticError):
        return None
    return d if d.is_finite() else None


def _wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = successes / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def _page(items: list, f) -> Any:
    offset, limit = getattr(f, "offset", 0) or 0, getattr(f, "limit", 100) or 100
    return R.Page(items=items[offset:offset + limit], total=len(items), offset=offset, limit=limit)


def _matches(row: dict, f, keys: Iterable[str]) -> bool:
    for k in keys:
        want = getattr(f, k, None)
        if want in (None, [], ()):
            continue
        have = row.get(k)
        if isinstance(want, (list, tuple, set, frozenset)):
            if have not in want and str(have) not in {str(w) for w in want}:
                return False
        elif have != want:
            return False
    q = getattr(f, "q", None)
    if q:
        blob = json.dumps(row, default=str).lower()
        if q.lower() not in blob:
            return False
    return True


def _in_window(value, f, prefix: str) -> bool:
    """``f.<prefix>_from`` <= value <= ``f.<prefix>_to`` (either bound optional); a value without a timestamp fails a bounded window."""
    lo, hi = getattr(f, f"{prefix}_from", None), getattr(f, f"{prefix}_to", None)
    if not lo and not hi:
        return True
    dt = _dt(value)
    if dt is None:
        return False
    return (lo is None or dt >= _dt(lo)) and (hi is None or dt <= _dt(hi))


def _order(items: list, f, default_key: str) -> list:
    key = getattr(f, "order_by", None) or default_key
    desc = bool(getattr(f, "desc", True))

    def sk(row):
        v = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
        if isinstance(v, datetime):
            v = v.timestamp()
        return (v is None, v if isinstance(v, (int, float, Decimal)) else str(v) if v is not None else "")
    try:
        return sorted(items, key=sk, reverse=desc)
    except TypeError:
        return items


class FileIndexReader:
    """IndexReader over the files. ``settings`` supplies ``workspace``, ``registry_path``, ``origin`` and ``spend_cap_usd``."""

    def __init__(self, root: Path, registry_path: Path | None = None, *, origin: str = "laptop", spend_cap_usd: Decimal | None = None):
        self.root = Path(root)
        self.registry_path = Path(registry_path) if registry_path else self.root / "registry.json"
        self.origin = origin
        self.spend_cap_usd = spend_cap_usd
        self.unprojected: list[dict] = []
        self._cache: dict[str, tuple[float, Any]] = {}

    # ------------------------------------------------------------------ caching
    def _cached(self, name: str, build):
        hit = self._cache.get(name)
        now = time.monotonic()
        if hit and now - hit[0] < TTL:
            return hit[1]
        value = build()
        self._cache[name] = (now, value)
        return value

    def invalidate(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------------ registry
    def _registry(self) -> dict:
        reg = read_json(self.registry_path).get("scenarios") if self.registry_path.exists() else {}
        return reg if isinstance(reg, dict) else {}

    # ------------------------------------------------------------------ scenarios
    def _scenario_row(self, sdir: Path, sid: str, entry: dict | None, archived: bool, version_hint: int | None) -> R.ScenarioRow | None:
        spec = read_json(sdir / "scenario.json") or read_json(sdir / "scenario.draft.json") or {}
        if not isinstance(spec, dict):
            spec = {}
        if not spec and not entry:
            return None
        approval = read_json(sdir / "approval.json")
        compiled = read_json(sdir / "compile.json")
        validation = read_json(sdir / "validation.json")
        meta = read_json(sdir / "draft.meta.json")
        review = read_json(sdir / "review.json")
        difficulty = read_json(sdir / "difficulty.json")
        audit = read_json(sdir / "audit.json")
        archived_json = read_json(sdir / "archived.json")
        version = version_hint or spec.get("version") or approval.get("version") or (entry or {}).get("version") or 1
        if archived:
            status = "archived"
        elif review.get("decision") == "reject" and not approval:
            status = "rejected"
        elif validation.get("status") in ("valid", "invalid"):
            status = validation["status"]
        elif compiled:
            status = "validating"
        elif approval:
            status = "approved"
        else:
            status = "drafting"
        spec_hash = approval.get("spec_hash") or compiled.get("spec_hash") or validation.get("spec_hash") or meta.get("spec_hash") or (entry or {}).get("spec_hash")
        stamps = [t for t in (_mtime(sdir / n) for n in SCENARIO_FILES) if t is not None]
        created = _dt(spec.get("created_at")) or _dt(meta.get("created_at")) or (min(stamps) if stamps else datetime.now(UTC))
        updated = max(stamps) if stamps else created
        try:
            return R.ScenarioRow(
                scenario_id=sid, version=int(version), spec_hash=spec_hash, source=str(spec.get("source") or (entry or {}).get("source") or "unknown"),
                source_ref=str(spec.get("source_ref") or (entry or {}).get("source_ref") or ""), validation_status=status,
                compiled_hash=compiled.get("compiled_hash") or validation.get("compiled_hash") or (entry or {}).get("compiled_hash"),
                image_digest=compiled.get("image_digest") or validation.get("image_digest") or (entry or {}).get("image_digest"),
                difficulty_tier=difficulty.get("tier") if difficulty.get("tier") in ("saturated", "informative", "broken") else None,
                difficulty_screen={k: difficulty.get(k) for k in ("model", "hidden", "full", "labelled_at")} if difficulty else None,
                task_sense=audit or None, registry_json=entry if isinstance(entry, dict) else None,
                archived_at=_dt(archived_json.get("archived_at")) if archived_json else (updated if archived else None),
                created_at=created, updated_at=updated, workflow_id=None, run_id=None, worker_image_digest=None, origin=self.origin)
        except ValueError:  # a file with an unexpected value: the scenario is not projected rather than misreported
            return None

    def _all_scenarios(self) -> list[R.ScenarioRow]:
        def build():
            reg = self._registry()
            out: list[R.ScenarioRow] = []
            sdirs = self.root / "scenarios"
            seen: set[str] = set()
            if sdirs.is_dir():
                for sdir in sorted(p for p in sdirs.iterdir() if p.is_dir() and ID_RE.fullmatch(p.name)):
                    seen.add(sdir.name)
                    row = self._scenario_row(sdir, sdir.name, reg.get(sdir.name), False, None)
                    if row:
                        out.append(row)
                    arch = sdir / "archive"
                    if arch.is_dir():
                        for vdir in sorted(p for p in arch.iterdir() if p.is_dir() and re.fullmatch(r"v\d+", p.name)):
                            arow = self._scenario_row(vdir, sdir.name, None, True, int(vdir.name[1:]))
                            if arow and not (row and arow.version == row.version):
                                out.append(arow)
            for sid, entry in sorted(reg.items()):
                if sid not in seen and ID_RE.fullmatch(sid) and isinstance(entry, dict):
                    row = self._scenario_row(sdirs / sid, sid, entry, False, None)
                    if row:
                        out.append(row)
            return out
        return self._cached("scenarios", build)

    def scenarios(self, f) -> Any:
        rows = self._all_scenarios()
        if getattr(f, "latest_only", False):
            rows = [r for r in rows if r.validation_status != "archived"]
        rows = [r for r in rows if _matches(r.model_dump(mode="json"), f, ("scenario_id", "validation_status", "source", "difficulty_tier", "origin"))]
        rows = _order([r.model_dump() for r in rows], f, "updated_at")
        return _page([R.ScenarioRow.model_validate(r) for r in rows], f)

    def scenario(self, scenario_id: str, version: int | None = None) -> R.ScenarioRow | None:
        rows = [r for r in self._all_scenarios() if r.scenario_id == scenario_id]
        if version is not None:
            return next((r for r in rows if r.version == version), None)
        current = [r for r in rows if r.validation_status != "archived"]
        return current[0] if current else (max(rows, key=lambda r: r.version) if rows else None)

    def scenario_versions(self, scenario_id: str) -> list[R.ScenarioRow]:
        return sorted((r for r in self._all_scenarios() if r.scenario_id == scenario_id), key=lambda r: r.version)

    # ------------------------------------------------------------------ candidates
    def _all_candidates(self) -> tuple[list[R.CandidateRow], dict[str, Counter]]:
        def build():
            cdir = self.root / "candidates"
            rows: dict[tuple[str, str], dict] = {}
            reject_hist: dict[str, Counter] = {}
            if not cdir.is_dir():
                return [], reject_hist
            scen_by_ref = {(r.source_ref or r.scenario_id): r for r in self._all_scenarios() if r.validation_status != "archived"}
            for path in sorted(cdir.glob("*.jsonl")):
                name = path.name
                if name.startswith("probe_"):
                    continue
                if name.endswith(".judged.probed.jsonl"):
                    key, stage = name[: -len(".judged.probed.jsonl")], "probed"
                elif name.endswith(".judged.jsonl"):
                    key, stage = name[: -len(".judged.jsonl")], "judged"
                else:
                    key, stage = name[: -len(".jsonl")], "mined"
                mtime = _mtime(path) or datetime.now(UTC)
                for line in path.read_text(errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(raw, dict) or not raw.get("instance_id"):
                        continue
                    iid = str(raw["instance_id"])
                    cur = rows.setdefault((iid, key), {"instance_id": iid, "source_key": key, "source": str(raw.get("source") or key.split("--")[0]),
                                                        "repo": raw.get("repo"), "pr_number": None, "pr_kind": raw.get("pr_kind"), "stage": "mined",
                                                        "status": "pending", "gate": None, "reasons": [], "judged_at": None, "scenario_id": None,
                                                        "version": None, "created_at": mtime, "updated_at": mtime})
                    m = re.search(r"-(\d+)$", iid)
                    cur["pr_number"] = int(m.group(1)) if m else None
                    cur["updated_at"] = max(cur["updated_at"], mtime)
                    if stage == "judged" or (stage == "probed" and isinstance(raw.get("judge"), dict)):
                        judge = raw.get("judge") if isinstance(raw.get("judge"), dict) else {}
                        keep = judge.get("keep")
                        cur["stage"] = "judged" if STAGES.index(cur["stage"]) < STAGES.index("judged") else cur["stage"]
                        cur["judged_at"] = mtime
                        if keep is False:
                            cur.update(status="rejected", gate="judge", reasons=[str(judge.get("rationale") or "judge rejected")][:1])
                        elif keep:
                            cur["status"] = "kept"
                    if stage == "probed":
                        probe = raw.get("probe") if isinstance(raw.get("probe"), dict) else {}
                        cur["stage"] = "probed" if STAGES.index(cur["stage"]) < STAGES.index("probed") else cur["stage"]
                        if probe and probe.get("reproducible") is False:
                            cur.update(status="rejected", gate="probe", reasons=[str(probe.get("reason") or "not reproducible")])
                        elif cur["status"] != "rejected":
                            cur["status"] = "kept"
                    # the scenario, when the candidate got that far
                    scen = scen_by_ref.get(iid) or scen_by_ref.get(iid.replace("__", "--"))
                    if scen is not None and cur["status"] != "rejected":
                        cur["scenario_id"], cur["version"] = scen.scenario_id, scen.version
                        st = scen.validation_status
                        cur["stage"] = {"drafting": "drafted", "approved": "approved", "validating": "compiled", "valid": "registered" if scen.registry_json else "validated",
                                        "invalid": "validated", "rejected": "drafted"}.get(st, cur["stage"])
                        if st == "invalid":
                            cur.update(status="rejected", gate="validation", reasons=["validation reported invalid"])
                        elif st == "rejected":
                            cur.update(status="rejected", gate="review", reasons=["rejected at review"])
                        else:
                            cur["status"] = "kept"
                for rej in cdir.glob(f"{key}.rejects.json"):
                    hist = read_json(rej)
                    if isinstance(hist, dict):
                        reject_hist[key] = Counter({str(k): int(v) for k, v in hist.items() if isinstance(v, int)})
            out = []
            for cur in rows.values():
                try:
                    out.append(R.CandidateRow(**cur, workflow_id=None, run_id=None, worker_image_digest=None, origin=self.origin))
                except ValueError:
                    continue
            return out, reject_hist
        return self._cached("candidates", build)

    def candidates(self, f) -> Any:
        rows, _hist = self._all_candidates()
        items = [r for r in rows if _matches(r.model_dump(mode="json"), f, ("source_key", "instance_id", "source", "repo", "stage", "status", "gate", "scenario_id"))
                 and _in_window(r.judged_at, f, "judged")]
        items = _order([r.model_dump() for r in items], f, "updated_at")
        return _page([R.CandidateRow.model_validate(r) for r in items], f)

    def funnel(self, source_key: str | None = None) -> list:
        rows, hist = self._all_candidates()
        rows = [r for r in rows if source_key is None or r.source_key == source_key]
        steps = []
        for i, stage in enumerate(STAGES):
            reached = [r for r in rows if STAGES.index(r.stage) >= i]
            here = [r for r in rows if r.stage == stage]
            kept = sum(1 for r in reached if r.stage != stage or r.status == "kept")
            rejected = sum(1 for r in here if r.status == "rejected")
            error = sum(1 for r in here if r.status == "error")
            pending = sum(1 for r in here if r.status == "pending")
            if stage == "mined":  # PRs rejected at the mining gates never became rows; the rejects histogram carries them
                rejected += sum(sum(h.values()) for k, h in hist.items() if source_key is None or k == source_key)
            steps.append(R.FunnelStep(stage=stage, kept=kept, rejected=rejected, error=error, pending=pending))
        return steps

    def reject_histogram(self, source_key: str | None = None) -> dict[str, int]:
        """Mining-gate rejects per gate (from ``<key>.rejects.json``) plus per-row gates; not part of the protocol."""
        rows, hist = self._all_candidates()
        out: Counter = Counter()
        for k, h in hist.items():
            if source_key is None or k == source_key:
                out.update(h)
        for r in rows:
            if r.status == "rejected" and r.gate and (source_key is None or r.source_key == source_key):
                out[r.gate] += 1
        return dict(out.most_common())

    # ------------------------------------------------------------------ trials
    def _trial_dirs(self) -> list[Path]:
        trials = self.root / "trials"
        if not trials.is_dir():
            return []
        return sorted(p for p in trials.iterdir() if p.is_dir() and TRIAL_RE.fullmatch(p.name)
                      and any((p / n).exists() for n in ("record.json", *UNRESOLVED_FILES)))

    def _trial_row(self, d: Path, versions: dict[str, int | None]) -> tuple[R.TrialRow | None, dict]:
        status_file = read_json(d / "status.json")
        raw = read_json(d / "record.json") if (d / "record.json").exists() else {}
        has_record = bool(raw)
        rec = normalize_record(raw, d) if has_record else {}
        u = ({} if has_record else unresolved_attempt(d)) or {}
        request = read_json(d / "request.json")
        launch = read_json(d / "launch.json")
        rc = rec.get("run_config") or request.get("run_config") or u.get("run_config") or {}
        costs = read_json(d / "costs.json", default=[])
        audit = read_json(d / "audit.json")
        pick = lambda *vals: next((v for v in vals if v not in (None, "")), None)  # noqa: E731
        scenario_id = pick(rec.get("scenario_id"), request.get("scenario_id"), u.get("scenario_id"))
        condition = pick(rec.get("condition"), request.get("condition"), u.get("condition"))
        model = pick(rec.get("model"), request.get("model"), rc.get("model"), u.get("model"))
        profile = pick(rec.get("brief_profile"), rc.get("brief_profile"), u.get("brief_profile"))
        purpose = pick(rec.get("purpose"), rc.get("purpose"), u.get("purpose"))
        spec_hash = pick(rec.get("spec_hash"), request.get("spec_hash"), u.get("spec_hash"))
        rch = pick(rec.get("run_config_hash"), request.get("run_config_hash"), u.get("run_config_hash"))
        attempt = pick(rec.get("attempt_index"), request.get("attempt_index"), u.get("attempt_index"))
        raw_status = str(pick(status_file.get("status"), u.get("status"), "completed" if has_record else "unresolved"))
        if has_record:
            status = "completed"
        elif raw_status.startswith("launch-") and raw_status != "launch-failed":
            status = "launched"
        else:
            status = TRIAL_STATUS.get(raw_status, "unresolved")
        code = outcome_code(rec) if rec.get("reward") else None
        reward = (rec.get("reward") or {}) if has_record else {}
        report = rec.get("report") or {}
        usd = sum((_dec(c.get("usd")) or Decimal(0)) for c in costs if isinstance(c, dict)) if isinstance(costs, list) and costs else None
        toks_in = sum(int(c.get("input_tokens") or 0) for c in costs if isinstance(c, dict)) if isinstance(costs, list) and costs else None
        toks_out = sum(int(c.get("output_tokens") or 0) for c in costs if isinstance(c, dict)) if isinstance(costs, list) and costs else None
        started = _dt(status_file.get("started_at")) or _dt(rec.get("started_at")) or _dt(u.get("started_at")) or _dt(launch.get("started_at"))
        finished = _dt(status_file.get("finished_at")) or _dt(rec.get("finished_at")) or _dt(u.get("finished_at"))
        stamps = [t for t in (_mtime(d / n) for n in ("record.json", "status.json", "request.json", "launch.json", "runner-error.json")) if t]
        created = started or (min(stamps) if stamps else datetime.now(UTC))
        updated = max(stamps) if stamps else created
        payload = dict(
            trial_id=d.name, scenario_id=scenario_id, version=versions.get(scenario_id), spec_hash=spec_hash, condition=condition,
            brief_profile=profile, model=model, provider=str(model).split("/")[0] if model else None, run_config_hash=rch,
            attempt_index=attempt, purpose=purpose, status=status, outcome_code=code, reward=_dec(reward.get("reward")),
            success=(bool(reward["success"]) if reward.get("success") is not None else None),
            integrity_signal_count=len(report.get("integrity_signals") or []) if has_record else None,
            questions_asked=count_exchanges(rec, "ask") if has_record else None,
            confirmations=(count_exchanges(rec, "confirm") + count_exchanges(rec, "review")) if has_record else None,
            asked_before_implementing=audit.get("asked_before_implementing") if isinstance(audit, dict) and audit else None,
            started_at=started, finished_at=finished, launcher=launch.get("launcher") if launch.get("launcher") in ("systemd", "cloudrun", "subprocess") else None,
            job_name=launch.get("handle") or launch.get("unit"), cost_usd=usd, input_tokens=toks_in, output_tokens=toks_out,
            stakeholder_model=(rc.get("stakeholder") or {}).get("model_id") or request.get("stakeholder_model"),
            stakeholder_prompt_version=(rc.get("stakeholder") or {}).get("prompt_version"),
            created_at=created, updated_at=updated, workflow_id=request.get("workflow_id"), run_id=None,
            worker_image_digest=rec.get("worker_image_digest"), origin=str(rec.get("origin") or self.origin))
        extras = {"raw_status": raw_status, "error_type": u.get("error_type"), "source_file": u.get("source_file"), "outcome": OUTCOME_NAMES.get(code) if code else None,
                  "simulator_validity": report.get("simulator_validity"), "transcript_binding_valid": report.get("transcript_binding_valid"),
                  "label": (read_json(d / "label.json", default=None) or {}).get("label"), "compiled_hash": pick(rec.get("compiled_hash"), rc.get("compiled_hash"), u.get("compiled_hash")),
                  "image_digest": report.get("image_digest"), "trial_dir": rec.get("trial_dir"), "task_dir": rec.get("task_dir")}
        try:
            return R.TrialRow(**payload), extras
        except ValueError as e:
            missing = [k for k in ("scenario_id", "condition", "brief_profile", "model", "spec_hash", "run_config_hash", "attempt_index", "purpose") if payload.get(k) in (None, "")]
            self.unprojected.append({"trial_id": d.name, "status": raw_status, "missing": missing, "source_file": u.get("source_file"),
                                     "error": type(e).__name__})
            return None, extras

    def _all_trials(self) -> tuple[list[R.TrialRow], dict[str, dict]]:
        def build():
            self.unprojected = []
            versions = {r.scenario_id: r.version for r in self._all_scenarios() if r.validation_status != "archived"}
            out, extras = [], {}
            for d in self._trial_dirs():
                row, ex = self._trial_row(d, versions)
                if row is not None:
                    out.append(row)
                    extras[row.trial_id] = ex
            return out, extras
        return self._cached("trials", build)

    def trial_extras(self, trial_id: str) -> dict:
        """File-derived facts the row model does not carry (outcome name, label, unresolved source file); not protocol."""
        return self._all_trials()[1].get(trial_id, {})

    def trials(self, f) -> Any:
        rows, _ = self._all_trials()
        items = [r for r in rows if _matches(r.model_dump(mode="json"), f, ("trial_id", "scenario_id", "version", "spec_hash", "model", "provider", "condition",
                                                                              "brief_profile", "status", "outcome_code", "purpose", "origin", "launcher"))
                 and (getattr(f, "success", None) is None or r.success == f.success) and _in_window(r.started_at or r.created_at, f, "started")]
        items = _order([r.model_dump() for r in items], f, "started_at")
        return _page([R.TrialRow.model_validate(r) for r in items], f)

    def trial(self, trial_id: str) -> R.TrialRow | None:
        return next((r for r in self._all_trials()[0] if r.trial_id == trial_id), None)

    # ------------------------------------------------------------------ exchanges
    def exchanges(self, trial_id: str, evaluator: bool) -> list[R.ExchangeRow]:
        d = self.root / "trials" / trial_id
        if not TRIAL_RE.fullmatch(trial_id) or not d.is_dir():
            return []
        rec = read_json(d / "record.json")
        transcript = (rec.get("transcript") if isinstance(rec, dict) else None) or read_json(d / "transcript.json")
        audit = read_json(d / "audit.json")
        by_seq: dict[int, dict] = {}
        for k in ("questions", "replies"):
            for item in (audit.get(k) or []) if isinstance(audit, dict) else []:
                if isinstance(item, dict) and isinstance(item.get("seq"), int):
                    by_seq[item["seq"]] = item
        out: list[R.ExchangeRow] = []
        last_exchange = "ask"
        for i, e in enumerate((transcript or {}).get("events") or []):
            if not isinstance(e, dict) or e.get("kind") != "message":
                continue
            role = "agent" if e.get("role") == "user" else "stakeholder" if e.get("role") == "assistant" else "system"
            exchange = e.get("exchange") or last_exchange
            exchange = exchange if exchange in ("ask", "confirm", "review", "system") else "system"
            last_exchange = exchange
            seq = e.get("seq") if isinstance(e.get("seq"), int) else i
            try:
                row = R.ExchangeRow(trial_id=trial_id, seq=seq, role=role, exchange=exchange, ts=_dt(e.get("ts")), request_id=e.get("request_id"),
                                    patience_after=e.get("patience_remaining") if isinstance(e.get("patience_remaining"), int) else None,
                                    text_public=str(e.get("content") or ""), disclosed_fact_ids=[str(x) for x in e.get("disclosed_fact_ids") or []],
                                    regenerated=e.get("regenerated") if isinstance(e.get("regenerated"), bool) else None,
                                    redacted_terms=[str(x) for x in e.get("redacted_terms") or []], audit=by_seq.get(seq),
                                    created_at=_dt(e.get("ts")) or datetime.now(UTC), updated_at=_dt(e.get("ts")) or datetime.now(UTC),
                                    workflow_id=None, run_id=None, worker_image_digest=None, origin=self.origin)
            except ValueError:
                continue
            out.append(row if evaluator else R.ExchangeRow.model_validate(R.public_exchange(row)))
        return out

    # ------------------------------------------------------------------ scores and leaderboard
    def _all_scores(self) -> list[R.ScoreRow]:
        def build():
            out = []
            trials = {t.trial_id: t for t in self._all_trials()[0]}
            for s in self._all_scenarios():
                if s.validation_status == "archived":
                    continue
                p = self.root / "scenarios" / s.scenario_id / "scores.json"
                groups = read_json(p, default=None)
                if not isinstance(groups, dict):
                    continue
                computed = _mtime(p) or datetime.now(UTC)
                for key, g in groups.items():
                    if not isinstance(g, dict) or not g.get("condition") or not g.get("model"):
                        continue
                    tids = [t for t in g.get("trial_ids") or [] if isinstance(t, str)]
                    q = sum((trials[t].questions_asked or 0) for t in tids if t in trials)
                    c = sum((trials[t].confirmations or 0) for t in tids if t in trials)
                    try:
                        out.append(R.ScoreRow(scenario_id=s.scenario_id, spec_hash=s.spec_hash or "", condition=g["condition"], brief_profile=str(g.get("brief_profile") or ""),
                                              model=str(g["model"]), run_config_hash=str(g.get("run_config_hash") or key.split("|")[-1]), n=int(g.get("n_valid") or g.get("n") or 0),
                                              success_count=int(g.get("success_count") or 0), pass_at_k=_dec(g.get("pass_at_k")), pass_pow_k=_dec(g.get("pass_pow_k")),
                                              mean_reward=_dec(g.get("mean_reward")), questions_asked=q, confirmations=c, computed_at=computed,
                                              created_at=computed, updated_at=computed, workflow_id=None, run_id=None, worker_image_digest=None, origin=self.origin))
                    except ValueError:
                        continue
            return out
        return self._cached("scores", build)

    def scores(self, f) -> list[R.ScoreRow]:
        out = [r for r in self._all_scores() if _matches(r.model_dump(mode="json"), f, ("scenario_id", "spec_hash", "condition", "brief_profile", "model", "run_config_hash"))
               and r.n >= int(getattr(f, "min_n", 0) or 0)]
        return out[: int(getattr(f, "limit", 500) or 500)]

    def leaderboard_gate(self, include_saturated: bool = False) -> list[dict]:
        """Per current scenario: which §9.1 gates pass (valid, informative/saturated, task-sense coherent + fair_tests); not protocol."""
        out = []
        for s in self._all_scenarios():
            if s.validation_status == "archived":
                continue
            ts = s.task_sense if isinstance(s.task_sense, dict) else {}
            checks = {"valid": s.validation_status == "valid",
                      "difficulty": s.difficulty_tier == "informative" or (include_saturated and s.difficulty_tier == "saturated"),
                      "task_sense": bool(ts.get("coherent")) and bool(ts.get("fair_tests"))}
            out.append({"scenario_id": s.scenario_id, "version": s.version, "validation_status": s.validation_status, "difficulty_tier": s.difficulty_tier,
                        "task_sense": {k: ts.get(k) for k in ("coherent", "fair_tests", "plausible_withholding")} if ts else None,
                        "checks": checks, "eligible": all(checks.values()), "source": s.source})
        return out

    def leaderboard(self, f) -> list:
        """Cells model × condition × brief_profile for one purpose over the gated scenarios (interfaces §9.1); ``min_n`` applies per cell."""
        include_sat = bool(getattr(f, "include_saturated", False))
        eligible = {g["scenario_id"] for g in self.leaderboard_gate(include_sat) if g["eligible"]}
        wanted_src = getattr(f, "source", None)
        if wanted_src:
            srcs = {s.scenario_id: s.source for s in self._all_scenarios()}
            eligible = {sid for sid in eligible if srcs.get(sid) in wanted_src}
        if getattr(f, "scenario_id", None):
            eligible &= set(f.scenario_id)
        purpose = getattr(f, "purpose", "comparison") or "comparison"
        trials = [t for t in self._all_trials()[0] if t.scenario_id in eligible and t.status == "completed" and t.outcome_code in VALID and t.purpose == purpose]
        for key in ("brief_profile", "condition", "model", "provider"):
            want = getattr(f, key, None)
            if want:
                trials = [t for t in trials if getattr(t, key) in want]
        cells: dict[tuple[str, str, str], list[R.TrialRow]] = {}
        for t in trials:
            cells.setdefault((t.model, t.condition, t.brief_profile), []).append(t)
        min_n = int(getattr(f, "min_n", 1) or 1)
        out = []
        for (model, cond, prof), ts in sorted(cells.items()):
            n = len(ts)
            if n < min_n:
                continue
            succ = sum(1 for t in ts if t.success)
            cost = sum((t.cost_usd for t in ts if t.cost_usd is not None), Decimal(0)) if any(t.cost_usd is not None for t in ts) else None
            out.append(R.LeaderboardRow(model=model, provider=model.split("/")[0], condition=cond, brief_profile=prof, purpose=purpose, n=n,
                                        scenarios=len({t.scenario_id for t in ts}), success_count=succ,
                                        pass_rate=(Decimal(succ) / Decimal(n)).quantize(Decimal("0.001")),
                                        mean_reward=(sum((t.reward or Decimal(0)) for t in ts) / n).quantize(Decimal("0.001")),
                                        questions_asked=sum(t.questions_asked or 0 for t in ts), confirmations=sum(t.confirmations or 0 for t in ts),
                                        cost_usd=cost, cost_per_success=(cost / succ).quantize(Decimal("0.0001")) if cost is not None and succ else None))
        return out

    # ------------------------------------------------------------------ workflows (files carry none)
    def workflows(self, f) -> Any:
        return _page([], f)

    def workflow(self, workflow_id: str) -> None:
        return None

    # ------------------------------------------------------------------ costs, spend
    def _all_costs(self) -> list[R.CostRow]:
        def build():
            out = []
            for d in self._trial_dirs():
                items = read_json(d / "costs.json", default=[])
                if not isinstance(items, list):
                    continue
                for i, c in enumerate(items):
                    if not isinstance(c, dict):
                        continue
                    at = _dt(c.get("at")) or _mtime(d / "costs.json") or datetime.now(UTC)
                    try:
                        out.append(R.CostRow(cost_id=str(c.get("cost_id") or f"{d.name}-{i}"), trial_id=c.get("trial_id") or d.name, workflow_id=c.get("workflow_id"),
                                             role=c.get("role") or "solver", provider=str(c.get("provider") or str(c.get("model") or "").split("/")[0]), model=str(c.get("model") or ""),
                                             input_tokens=int(c.get("input_tokens") or 0), output_tokens=int(c.get("output_tokens") or 0),
                                             cached_tokens=c.get("cached_tokens"), usd=_dec(c.get("usd")) or Decimal(0), price_source=str(c.get("price_source") or "unknown"),
                                             at=at, created_at=at, updated_at=at, run_id=None, worker_image_digest=None, origin=self.origin))
                    except (ValueError, TypeError):
                        continue
            return out
        return self._cached("costs", build)

    def costs(self, f) -> list[R.CostRow]:
        out = [r for r in self._all_costs() if _matches(r.model_dump(mode="json"), f, ("trial_id", "workflow_id", "role", "provider", "model")) and _in_window(r.at, f, "at")]
        out.sort(key=lambda r: r.at, reverse=True)
        return out[: int(getattr(f, "limit", 500) or 500)]

    def spend(self) -> list:
        agg: dict[tuple[str, str], dict] = {}
        for c in self._all_costs():
            a = agg.setdefault((c.provider, c.model), {"usd": Decimal(0), "input_tokens": 0, "output_tokens": 0, "records": 0})
            a["usd"] += c.usd
            a["input_tokens"] += c.input_tokens
            a["output_tokens"] += c.output_tokens
            a["records"] += 1
        cap = self.spend_cap_usd
        return [R.SpendRow(provider=p, model=m, cap_usd=cap, share_of_cap=(a["usd"] / cap).quantize(Decimal("0.0001")) if cap else None, **a)
                for (p, m), a in sorted(agg.items())]

    # ------------------------------------------------------------------ operator actions
    @property
    def actions_path(self) -> Path:
        return self.root / "evidence" / "console" / "actions.jsonl"

    def operator_actions(self, f) -> Any:
        out = []
        if self.actions_path.exists():
            for line in self.actions_path.read_text(errors="replace").splitlines():
                try:
                    out.append(R.OperatorActionRow.model_validate_json(line))
                except ValueError:
                    continue
        out = _order([r.model_dump() for r in out], f, "at")
        return _page([R.OperatorActionRow.model_validate(r) for r in out], f)

    def add_operator_action(self, row) -> None:
        self.actions_path.parent.mkdir(parents=True, exist_ok=True)
        with self.actions_path.open("a") as fh:
            fh.write(row.model_dump_json() + "\n")

    # ------------------------------------------------------------------ generic table, rebuilds
    def table(self, name: str, f) -> Any:
        if name not in R.TABLES:
            raise ValueError(f"unknown table {name!r}; known: {', '.join(R.TABLES)}")
        if name == "scenarios":
            rows = [r.model_dump(mode="json") for r in self._all_scenarios()]
        elif name == "candidates":
            rows = [r.model_dump(mode="json") for r in self._all_candidates()[0]]
        elif name == "trials":
            rows = [r.model_dump(mode="json") for r in self._all_trials()[0]]
        elif name == "exchanges":
            rows = [R.public_exchange(x) for t in self._all_trials()[0] for x in self.exchanges(t.trial_id, evaluator=False)]
        elif name == "scores":
            rows = [r.model_dump(mode="json") for r in self._all_scores()]
        elif name == "costs":
            rows = [r.model_dump(mode="json") for r in self._all_costs()]
        elif name == "operator_actions":
            rows = [r.model_dump(mode="json") for r in self.operator_actions(R.PageFilter(limit=500)).items]
        else:
            rows = []
        where = dict(getattr(f, "where", None) or {})
        rows = [r for r in rows if all(str(r.get(k)) == str(v) for k, v in where.items()) and _matches(r, f, ())]
        rows = _order(rows, f, "updated_at" if rows and "updated_at" in rows[0] else "created_at")
        return _page(rows, f)

    def rebuilds(self) -> list:
        return []
