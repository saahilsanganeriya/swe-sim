"""Projection of the record's files into index rows, shared by `IndexStore.rebuild_from_layout`, `FileIndexReader`
and the index activities. This is the only place that knows which file feeds which column.

Contract
- Pure reads: nothing here writes. Every path comes from `Layout`.
- A missing or malformed file is an empty state, never an exception, *except* when the identity of a row cannot be
  established (a trial without scenario id / spec hash / run config hash, a scenario without a source, a scores
  group without a spec hash): that raises `ProjectionError(ValueError)` so the caller can record the skip instead
  of inventing a row. Nullable columns stay None when the files do not say. A scenario that nothing identifies *yet*
  (no spec file and no registry entry: the candidate is still being judged, probed or drafted) raises the subclass
  `NoScenarioFiles`, which a hook reports as "nothing to index yet" rather than as a failure.
- Money is `Decimal` (through `dec`), never float. Times are tz-aware UTC (through `aware`): epoch numbers and ISO
  strings are accepted; a naive ISO string is taken as UTC.
- `origin` is the file's own label when it carries one, else the caller's (`Settings.origin`).
- Trial status from files: record.json / status.json `completed|finished` -> completed, `running` -> running,
  `error` -> runner-error; runner-error.json -> runner-error; launch.json `failed` -> launch-failed; launch.json
  without status.json or record.json -> unresolved; request.json only -> planned.
- `TrialRow.failure` (runner-error | launch-failed | unresolved rows only, `FAILURE_STATUSES`): the operator-facing reason
  nothing was graded, from runner-error.json `error` (else `type`), else launch.json `error`, else status.json `error`;
  None when no file says. A graded or live row never carries one.
- Scenario validation status from files: scenario.json + validation.json valid -> valid; validation.json not valid
  -> invalid; a reject decision -> rejected; compile.json -> validating; approval.json -> compiling; else drafting;
  `archive/v<n>/` -> archived (one row per version).
- Candidate rows (`project_candidates`): one per instance of a source key, at the last stage its current version reached,
  with a terminal status there: `kept` only at the last stage, `rejected`/`error` at a gate, `pending` when kept but never
  advanced (reason in `reasons[0]`); archived versions ride along in `earlier_versions` as `superseded` (or their own
  rejection). The legacy per-instance probe files (`candidates/probe_<instance>.*.jsonl`) belong to the instance's
  source, not to a source key of their own. A receipt of the same version (`candidate_events`) takes precedence.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple, get_args

from pydantic import ValidationError

from swesim.index import models as M
from swesim.models import ID_RE
from swesim.settings import Origin, get_settings
from swesim.stages.score import count_exchanges, normalize_record, outcome_code, unresolved_attempt
from swesim.storage.layout import SEGMENT_RE, TRIAL_ID_RE, Layout

ORIGINS: tuple[str, ...] = Origin.__args__
LAUNCHERS: tuple[str, ...] = ("systemd", "cloudrun", "subprocess")
PURPOSES: tuple[str, ...] = M.Purpose.__args__
TIERS: tuple[str, ...] = M.DifficultyTier.__args__
CONDITIONS: tuple[str, ...] = ("full", "hidden", "interactive")
SCENARIO_FILES: tuple[str, ...] = ("scenario.json", "scenario.draft.json", "draft.meta.json", "judge.json", "approval.json", "compile.json",
                                   "validation.json", "difficulty.json", "audit.json", "review.json", "archived.json")
TRIAL_STATUS: dict[str, str] = {"completed": "completed", "finished": "completed", "running": "running", "error": "runner-error",
                                "runner-error": "runner-error", "launch-failed": "launch-failed", "unit-failed": "unit-failed",
                                "cancelled": "cancelled", "unresolved": "unresolved", "requested": "planned", "planned": "planned",
                                "launched": "launched", "unreadable-record": "unresolved"}
PROBE_REJECTS: tuple[str, ...] = ("not-reproducible-offline", "probe-failed")
_PR_RE = re.compile(r"-(\d+)$")


class ProjectionError(ValueError):
    """The files do not identify a row; the caller records the skip rather than guessing."""


class NoScenarioFiles(ProjectionError):
    """Nothing identifies the scenario yet: no spec file (`scenario.json` / `scenario.draft.json`) and no registry entry.
    An expected state while the candidate workflow is judging, probing or drafting; `reason` is the short operator-facing text."""

    def __init__(self, scenario_id: str, reason: str) -> None:
        super().__init__(f"{scenario_id}: {reason}")
        self.scenario_id = scenario_id
        self.reason = reason


# ----------------------------------------------------------------------------- primitives

def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def aware(value: Any) -> datetime | None:
    """tz-aware UTC datetime from an epoch number or an ISO-8601 string (naive strings are UTC); None otherwise."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return None
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        s = value.strip()
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                return aware(float(s))
            except ValueError:
                return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)
    return None


def dec(value: Any) -> Decimal | None:
    """Finite Decimal from an int, float, str or Decimal; None for anything else (never a float in a row)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
        return d if d.is_finite() else None
    return None


def mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except OSError:
        return None


def provider_of(model: str) -> str:
    """The prefix of a model id before the first `/` (`openrouter/deepseek/deepseek-v3.2` -> `openrouter`)."""
    return model.split("/", 1)[0] if "/" in model else model


def scenario_id_for(instance_id: str) -> str | None:
    """The drafter's rule: lower-case, `__` -> `--`; None when the result is not a valid scenario id."""
    sid = str(instance_id).lower().replace("__", "--")
    return sid if ID_RE.fullmatch(sid) else None


def pr_number_of(instance_id: str) -> int | None:
    m = _PR_RE.search(str(instance_id))
    return int(m.group(1)) if m else None


def _origin(value: Any) -> str | None:
    return value if value in ORIGINS else None


def _default_origin(origin: str | None) -> str:
    return origin or get_settings().origin


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _first(*sources: dict, key: str, allowed: tuple[str, ...] | None = None) -> str | None:
    for src in sources:
        v = src.get(key)
        if isinstance(v, str) and v and (allowed is None or v in allowed):
            return v
    return None


# ----------------------------------------------------------------------------- registry and scenarios

def project_registry(layout: Layout, registry_path: Path | None = None) -> dict[str, dict]:
    """`registry.json`'s `scenarios` map (id -> entry); empty when absent or malformed."""
    data = read_json(registry_path or layout.registry_path(), {})
    scenarios = data.get("scenarios") if isinstance(data, dict) else None
    return {k: v for k, v in scenarios.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(scenarios, dict) else {}


def _scenario_files(d: Path) -> tuple[set[str], dict[str, dict]]:
    """(names present, name -> dict body) for one scenario directory (or archive directory)."""
    present: set[str] = set()
    files: dict[str, dict] = {}
    for name in SCENARIO_FILES:
        p = d / name
        if p.is_file():
            present.add(name)
        files[name] = _dict(read_json(p)) if name in present else {}
    return present, files


def scenario_version_of(files: dict[str, dict]) -> int | None:
    for src in (files.get("scenario.json", {}), files.get("scenario.draft.json", {}), files.get("draft.meta.json", {}), files.get("approval.json", {})):
        v = _int(src.get("version"))
        if v is not None and v >= 1:
            return v
    return None


def validation_status_of(present: set[str], files: dict[str, dict]) -> str:
    validation, approval, review = files["validation.json"], files["approval.json"], files["review.json"]
    if "scenario.json" in present and validation.get("status") == "valid":
        return "valid"
    if "validation.json" in present and validation.get("status") != "valid":
        return "invalid"
    if approval.get("approved") is False or review.get("decision") == "reject":
        return "rejected"
    if "compile.json" in present:
        return "validating"
    if "approval.json" in present:
        return "compiling"
    return "drafting"


def _registry_status(entry: dict) -> str:
    status = entry.get("validation_status")
    return status if status in M.ValidationStatus.__args__ else "drafting"


def scenario_ids(layout: Layout, registry: dict[str, dict] | None = None) -> list[str]:
    """Every scenario directory plus every registered id (a registered scenario may be missing from this copy)."""
    ids: set[str] = set()
    sdirs = layout.root / "scenarios"
    if sdirs.is_dir():
        ids.update(p.name for p in sdirs.iterdir() if p.is_dir() and ID_RE.fullmatch(p.name))
    ids.update(sid for sid in (registry or {}) if ID_RE.fullmatch(sid))
    return sorted(ids)


def project_scenario(layout: Layout, scenario_id: str, version: int | None = None, *, registry: dict[str, dict] | None = None,
                     origin: str | None = None, now: datetime | None = None) -> M.ScenarioRow:
    """One scenario version. `version=None` is the current one; an archived version reads `archive/v<version>/`. A version
    requested while the files record none yet (the candidate workflow indexes `version: 1` after every stage, from judging on)
    is the version in progress, not an unknown archive."""
    registry = project_registry(layout) if registry is None else registry
    origin = _default_origin(origin)
    now = now or M.utcnow()
    sdir = layout.scenario_dir(scenario_id)
    entry = registry.get(scenario_id)
    present, files = _scenario_files(sdir)
    current = scenario_version_of(files) or (_int(entry.get("version")) if entry else None)
    archived_at: datetime | None = None
    entry_json: dict | None = entry
    adir = layout.scenario_archive_dir(scenario_id, version) if version is not None and version != current else None
    archived = adir is not None and adir.is_dir()
    if archived:
        present, files = _scenario_files(adir)
        status = "archived"
        archived_at = aware(files["archived.json"].get("at")) or mtime(adir / "archived.json") or mtime(adir)
        row_version = version
        entry_json = None
    elif adir is not None and current is not None:
        raise ProjectionError(f"{scenario_id}: no version {version} (current {current})")
    else:
        if not present and entry is None:
            raise NoScenarioFiles(scenario_id, "no scenario files yet")
        status = validation_status_of(present, files) if present else _registry_status(entry or {})
        row_version = current or version or 1  # ScenarioSpec.version defaults to 1
    spec = files["scenario.json"] or files["scenario.draft.json"]
    source = _str(spec.get("source")) or _str((entry or {}).get("source"))
    source_ref = _str(spec.get("source_ref")) or _str((entry or {}).get("source_ref"))
    if not source or not source_ref:
        if not spec and entry is None and not archived:  # only judge.json / draft.meta.json so far: the draft is not written yet
            raise NoScenarioFiles(scenario_id, "no scenario spec yet")
        raise ProjectionError(f"{scenario_id}: source and source_ref are unknown")
    approval, compiled, validation, meta = files["approval.json"], files["compile.json"], files["validation.json"], files["draft.meta.json"]
    spec_hash = _first(approval, compiled, validation, meta, key="spec_hash") or (_str((entry or {}).get("spec_hash")) if entry_json is not None else None)
    difficulty = files["difficulty.json"]
    tier = difficulty.get("tier") if difficulty.get("tier") in TIERS else None
    screen = {k: difficulty.get(k) for k in ("model", "hidden", "full", "labelled_at")} if difficulty else None
    return M.ScenarioRow(
        scenario_id=scenario_id, version=row_version, spec_hash=spec_hash, source=source, source_ref=source_ref, validation_status=status,
        compiled_hash=_first(compiled, key="compiled_hash") or (_str((entry_json or {}).get("compiled_hash"))),
        image_digest=_first(compiled, validation, key="image_digest") or (_str((entry_json or {}).get("image_digest"))),
        difficulty_tier=tier, difficulty_screen=screen, task_sense=files["audit.json"] or None, registry_json=entry_json, archived_at=archived_at,
        created_at=now, updated_at=now,
        workflow_id=_first(validation, compiled, approval, key="workflow_id"), run_id=_first(validation, compiled, approval, key="run_id"),
        worker_image_digest=_first(validation, compiled, approval, key="worker_image_digest"),
        origin=_first(validation, compiled, approval, key="origin", allowed=ORIGINS) or origin)


def project_scenario_versions(layout: Layout, scenario_id: str, *, registry: dict[str, dict] | None = None, origin: str | None = None,
                              now: datetime | None = None) -> list[M.ScenarioRow]:
    """The current version followed by every archived version (ascending)."""
    registry = project_registry(layout) if registry is None else registry
    rows = [project_scenario(layout, scenario_id, registry=registry, origin=origin, now=now)]
    archive = layout.scenario_dir(scenario_id) / "archive"
    if archive.is_dir():
        versions = sorted(int(p.name[1:]) for p in archive.iterdir() if p.is_dir() and re.fullmatch(r"v[1-9][0-9]*", p.name))
        for v in versions:
            if v != rows[0].version:
                rows.append(project_scenario(layout, scenario_id, v, registry=registry, origin=origin, now=now))
    rows.sort(key=lambda r: r.version)
    return rows


# ----------------------------------------------------------------------------- candidates

def discovery_source_keys(layout: Layout) -> list[str]:
    """Sources with discovery evidence, even when every PR was rejected before candidate creation."""
    keys = {p.name[:-len(".rejects.json")] for p in layout.candidates_dir().glob("*.rejects.json")}
    if layout.discovery_dir().is_dir():
        keys.update(p.name for p in layout.discovery_dir().iterdir() if p.is_dir())
    return sorted(key for key in keys if SEGMENT_RE.fullmatch(key) and key not in (".", ".."))


def project_discovery(layout: Layout, source_key: str, *, origin: str | None = None, workflow_id: str | None = None,
                      run_id: str | None = None, worker_image_digest: str | None = None, discovered_at: datetime | str | None = None,
                      artifact_sha256: str | None = None, artifact_path: Path | str | None = None,
                      repo: str | None = None, band: M.DiscoveryBand | None = None) -> M.DiscoveryRow | None:
    """One observation of the authoritative rejects file; supplied workflow identity requires its expected digest."""
    path = layout.resolve_artifact(artifact_path if artifact_path is not None else layout.candidates_file(source_key, "rejects"))
    if not path.name.endswith(".rejects.json"):
        raise ProjectionError("discovery artifact must be a rejects JSON file")
    if (workflow_id or run_id) and not artifact_sha256:
        raise ProjectionError("discovery workflow provenance requires the expected artifact SHA256")
    try:
        with path.open("rb") as stream:
            data = stream.read()
            observed = datetime.fromtimestamp(os.fstat(stream.fileno()).st_mtime, UTC)
    except FileNotFoundError:
        return None
    digest = hashlib.sha256(data).hexdigest()
    if artifact_sha256 is not None and digest != artifact_sha256:
        raise ProjectionError("discovery rejects artifact changed since the discovery activity")
    try:
        counts = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ProjectionError(f"discovery rejects file is not valid JSON: {source_key}") from exc
    source, _, slug = source_key.partition("--")
    stem, _, suffix = slug.rpartition("--")
    inferred_band = suffix if suffix in get_args(M.DiscoveryBand) else None
    if inferred_band is not None:
        slug = stem
        if band is not None and band != inferred_band:
            raise ProjectionError("discovery band differs from its source key")
        band = inferred_band
    inferred_repo = slug.replace("__", "/", 1) if source == "github-repo" and "__" in slug else None
    if repo is not None and inferred_repo is not None and repo != inferred_repo:
        raise ProjectionError("discovery repository differs from its source key")
    return M.DiscoveryRow(
        source_key=source_key, discovery_id=M.discovery_identity(source_key, digest, workflow_id, run_id),
        source=source, repo=repo or inferred_repo, band=band, identity_kind="workflow" if workflow_id or run_id else "legacy",
        rejects=counts, artifact_path=layout.relative(path), artifact_sha256=digest, discovered_at=discovered_at or observed,
        created_at=discovered_at or observed, updated_at=discovered_at or observed, workflow_id=workflow_id, run_id=run_id,
        worker_image_digest=worker_image_digest, origin=_default_origin(origin))


def read_discovery_snapshot(layout: Layout, source_key: str, discovery_id: str) -> M.DiscoveryRow:
    """A snapshot must agree with its content identity and layout location."""
    path = layout.discovery_snapshot(source_key, discovery_id)
    try:
        row = M.DiscoveryRow.model_validate_json(path.read_text())
    except (OSError, ValueError) as exc:
        raise ProjectionError(f"invalid discovery snapshot {source_key}/{discovery_id}") from exc
    artifact = layout.resolve_artifact(row.artifact_path)
    if row.source_key != source_key or row.discovery_id != discovery_id or not artifact.name.endswith(".rejects.json"):
        raise ProjectionError("discovery snapshot identity differs from its layout")
    return row


def project_discoveries(layout: Layout, source_key: str | None = None, *, origin: str | None = None) -> list[M.DiscoveryRow]:
    """Immutable history plus unrecorded legacy rejects files; never invent a second observation of a snapshotted file."""
    rows: list[M.DiscoveryRow] = []
    for key in [source_key] if source_key is not None else discovery_source_keys(layout):
        snapshots = [read_discovery_snapshot(layout, key, path.stem) for path in sorted(layout.discovery_dir(key).glob("*.json"))]
        current = project_discovery(layout, key, origin=origin)
        if current is not None and not any(row.artifact_sha256 == current.artifact_sha256 for row in snapshots):
            snapshots.append(current)
        rows.extend(snapshots)
    return rows

PROBE_FILE_PREFIX = "probe_"  # candidates/probe_<instance>.jsonl: the legacy chain's per-instance probe files, joined to the instance's source
PENDING_REASONS: tuple[str, ...] = ("not-selected", "cap-cut", "workflow-stopped", "in-progress", "unknown")
NEGATIVE: frozenset[str] = frozenset({"rejected", "error"})
SPEC_STAGE_FILES: frozenset[str] = frozenset({"scenario.draft.json", "draft.meta.json", "approval.json", "compile.json", "validation.json", "scenario.json"})
LAST_STAGE = M.FUNNEL_ORDER[-1]


def source_keys(layout: Layout) -> list[str]:
    """Source keys with a mined file (`<key>.jsonl`). Derived files (`.judged`, `.probed`) and the legacy per-instance probe files
    (`probe_<instance>.jsonl`, joined to the instance's own source by `project_candidates`) are not keys."""
    cdir = layout.candidates_dir()
    if not cdir.is_dir():
        return []
    keys = set()
    for p in cdir.glob("*.jsonl"):
        name = p.name[: -len(".jsonl")]
        if ".judged" in name or name.endswith(".probed") or name.startswith(PROBE_FILE_PREFIX) or not SEGMENT_RE.fullmatch(name):
            continue
        keys.add(name)
    return sorted(keys)


def _jsonl_rows(path: Path) -> dict[str, dict]:
    """instance_id -> row (last occurrence wins); malformed lines and rows without an id are ignored."""
    rows: dict[str, dict] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("instance_id"), str) and row["instance_id"]:
            rows[row["instance_id"]] = row
    return rows


CandidateEvents = dict[tuple[str, str], list[M.CandidateEventRow]]


def candidate_events_by_identity(layout: Layout, settings=None) -> CandidateEvents:
    """Every candidate receipt (`swesim.index.history.candidate_events`) keyed by (source_key, instance_id), oldest first.
    Read once per listing/rebuild and passed to `project_candidates`; a corrupt receipt directory is a `ValueError`."""
    from swesim.index.history import candidate_events
    out: CandidateEvents = {}
    for event in candidate_events(layout, settings):
        out.setdefault((event.source_key, event.instance_id), []).append(event)
    return out


class CandidatePlans(NamedTuple):
    """What the frozen candidate plans under `evidence/workflows/<wf>/` say (`swesim.workflows.batching`): `offered` maps
    (source_key, instance_id) -> {"selected": bool, "workflow_dir": str} for every row of a source snapshot
    (`<n>.candidates.jsonl`), selected when the plan's `items` list it, cut by the source limit otherwise; `workflows`
    maps a workflow id and `dirs` its plan directory name (`sha256(workflow_id)[:32]`) to the workflow's status."""

    offered: dict[tuple[str, str], dict]
    workflows: dict[str, str]
    dirs: dict[str, str]


def candidate_plans(layout: Layout, settings=None) -> CandidatePlans:
    """Read once per listing/rebuild and passed to `project_candidates`; an unreadable file contributes nothing."""
    offered: dict[tuple[str, str], dict] = {}
    root = layout.root / "evidence" / "workflows"
    if root.is_dir():
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            for plan_file in sorted(d.glob("*.plan.json")):
                items = _dict(read_json(plan_file)).get("items")
                if not isinstance(items, list) or not all(isinstance(i, dict) and _str(i.get("instance_id")) for i in items):
                    continue  # a trial plan, or not a plan
                chosen = {(str(i.get("source_key")), i["instance_id"]) for i in items}
                key_of_snapshot = {Path(str(i.get("candidates_path"))).name: str(i.get("source_key")) for i in items if i.get("candidates_path")}
                for snapshot in sorted(d.glob("*.candidates.jsonl")):
                    key = key_of_snapshot.get(snapshot.name)
                    if key is None:
                        continue  # no plan item names this snapshot: its source key is unknown
                    for iid in _jsonl_rows(snapshot):
                        prior = offered.get((key, iid))
                        selected = (key, iid) in chosen
                        if prior is None or (selected and not prior["selected"]):
                            offered[(key, iid)] = {"selected": selected, "workflow_dir": d.name}
    workflows: dict[str, str] = {}
    try:
        from swesim.index.history import workflow_rows
        for w in sorted(workflow_rows(layout, settings), key=lambda w: (w.started_at, w.run_id)):
            if workflows.get(w.workflow_id) != "running":  # any run still open keeps the workflow running
                workflows[w.workflow_id] = w.status
    except (OSError, ValueError):
        workflows = {}
    dirs = {hashlib.sha256(wid.encode()).hexdigest()[:32]: status for wid, status in workflows.items()}
    return CandidatePlans(offered, workflows, dirs)


def _instance_probe_rows(layout: Layout, iid: str) -> tuple[dict, datetime | None]:
    """The legacy chain's per-instance probe files, `candidates/probe_<instance>.jsonl` (the judged row it probed) and
    `probe_<instance>.probed.jsonl` (the outcome); the outcome wins. (merged row, newest file time)."""
    if not SEGMENT_RE.fullmatch(iid):
        return {}, None
    merged: dict = {}
    at: datetime | None = None
    for name in (f"{PROBE_FILE_PREFIX}{iid}.jsonl", f"{PROBE_FILE_PREFIX}{iid}.probed.jsonl"):
        path = layout.candidates_dir() / name
        per = _jsonl_rows(path).get(iid)
        if per:
            merged.update(per)
            at = mtime(path) or at
    return merged, at


def project_candidates(layout: Layout, source_key: str, *, registry: dict[str, dict] | None = None, origin: str | None = None,
                       now: datetime | None = None, events: CandidateEvents | None = None, plans: CandidatePlans | None = None
                       ) -> list[M.CandidateRow]:
    """Exactly one row per instance of one source key: the furthest stage its current version reached and the terminal
    decision there (`CandidateRow`). Joins the source's mined/judged/probed files, the per-candidate files under
    `candidates/judged/` and the legacy per-instance probe files (`probe_<instance>.*.jsonl`); `events` are the candidates'
    decision receipts (`candidate_events_by_identity`) and `plans` the frozen candidate plans (`candidate_plans`), both read
    here when not given. A receipt of the current version takes precedence over what the files suggest."""
    registry = project_registry(layout) if registry is None else registry
    origin = _default_origin(origin)
    now = now or M.utcnow()
    events = candidate_events_by_identity(layout) if events is None else events
    plans = candidate_plans(layout) if plans is None else plans
    mined_path = layout.candidates_file(source_key, "mined")
    judged_path = layout.candidates_file(source_key, "judged")
    probed_path = layout.candidates_file(source_key, "probed")
    mined, judged, probed = _jsonl_rows(mined_path), _jsonl_rows(judged_path), _jsonl_rows(probed_path)
    judged_dir = layout.candidates_dir() / "judged"
    rows: list[M.CandidateRow] = []
    for iid in sorted(set(mined) | set(judged) | set(probed)):
        sid = scenario_id_for(iid)
        row = {**mined.get(iid, {}), **judged.get(iid, {}), **probed.get(iid, {})}
        judged_at = mtime(judged_path) if iid in judged else None
        per, at = _instance_probe_rows(layout, iid)
        if per:
            row.update(per)
            judged_at = at or judged_at
        if sid is not None and judged_dir.is_dir():
            for suffix in (".judged.jsonl", ".judged.probed.jsonl"):
                per = _jsonl_rows(judged_dir / f"{sid}{suffix}").get(iid)
                if per:
                    row.update(per)
                    judged_at = mtime(judged_dir / f"{sid}{suffix}") or judged_at
        rows.append(_candidate_row(layout, source_key, iid, sid, row, judged_at, registry, origin, now, events.get((source_key, iid), []),
                                   plans=plans, has_judged=bool(judged)))
    return rows


def _further(a: str, b: str) -> bool:
    """Funnel stage `a` is later than `b`."""
    return a in M.FUNNEL_ORDER and b in M.FUNNEL_ORDER and M.FUNNEL_ORDER.index(a) > M.FUNNEL_ORDER.index(b)


def _file_decision(row: dict, error_file: dict) -> tuple[str, str, str | None, list[str]]:
    """(stage, status, gate, reasons) the candidate files prove up to probing."""
    judge = row.get("judge") if isinstance(row.get("judge"), dict) else None
    probe = row.get("probe") if isinstance(row.get("probe"), dict) else None
    stage, status, gate, reasons = "mined", "pending", None, []
    if judge is not None:
        keep = judge.get("keep")
        stage = "judged"
        status = "kept" if keep is True else "rejected" if keep is False else "pending"
        if keep is False:
            gate, reasons = "judge", [str(row.get("reject_reason") or judge.get("reject_reason") or "judge did not keep the candidate")]
        elif keep is not True:
            reasons = ["judge gave no decision"]
    if error_file:
        stage, status, gate, reasons = "probed", "error", "probe", [str(error_file.get("error") or "probe error")]
    elif row.get("reject_reason") in PROBE_REJECTS:
        stage, status, gate = "probed", "rejected", "probe"
        reasons = [str(row["reject_reason"])] + ([str(probe["error"])] if probe and probe.get("error") else [])
    elif probe is not None and probe.get("error"):
        stage, status, gate, reasons = "probed", "error", "probe", [str(probe["error"])]
    elif probe is not None and status == "kept" and (probe.get("statuses") or probe.get("stages") or row.get("probe_workflow")):
        stage = "probed"
    return stage, status, gate, reasons


def _scenario_decision(present: set[str], files: dict[str, dict], *, registered: bool) -> tuple[str, str, str | None, list[str]] | None:
    """(stage, status, gate, reasons) the scenario files of one version prove from drafting on; None without a spec-stage file.
    `registered` says a valid validation counts as registered (the current version: a registry entry exists; an archived
    version: registration follows a valid validation without a further gate, `workflows.py` validate -> register)."""
    if not (present & SPEC_STAGE_FILES):
        return None
    review, approval, validation = files["review.json"], files["approval.json"], files["validation.json"]
    if review.get("decision") == "reject":
        return "approved", "rejected", "approval", [str(review.get("note") or "rejected in review")]
    if approval.get("approved") is False:
        return "approved", "rejected", "approval", [str(approval.get("note") or "not approved")]
    stage = "drafted"
    if "approval.json" in present:
        stage = "approved"
    if "compile.json" in present:
        stage = "compiled"
    if "validation.json" in present:
        stage = "validated"
        if validation.get("status") != "valid":
            return stage, "rejected", "validation", [str(f) for f in (validation.get("failures") or [])][:10] or ["validation not valid"]
        if registered:
            stage = LAST_STAGE
    return stage, "kept", None, []


def _latest_receipt(events, version: int | None) -> M.CandidateEventRow | None:
    """The newest receipt of one version (a receipt without a version, or any receipt when the version is unknown)."""
    mine = [e for e in events if version is None or e.version is None or e.version == version]
    return max(mine, key=lambda e: (e.occurred_at, e.event_id), default=None)


def _apply_receipt(latest: M.CandidateEventRow | None, decision: tuple[str, str, str | None, list[str]]) -> tuple[str, str, str | None, list[str]]:
    """The workflow's own receipt of its last decision: a terminal rejection/error is the truth about this version even when its
    scenario directory holds files from a later stage (an auto-approval reject leaves approval-stage files; a human reject or
    approval timeout leaves a draft). A kept receipt never lowers what the files show, only raises the stage."""
    stage, status, gate, reasons = decision
    if latest is None:
        return decision
    if latest.terminal and latest.status in NEGATIVE:
        return latest.stage, latest.status, latest.gate, list(latest.reasons)
    if status == "kept" and _further(latest.stage, stage):
        stage = latest.stage
    return stage, status, gate, reasons


def _pending_reason(stage: str, has_judged: bool, plans: CandidatePlans | None, source_key: str, iid: str, workflow_id: str | None) -> str:
    """Why a candidate kept at `stage` never advanced: `cap-cut` when a frozen plan offered it and the source limit left it out;
    `workflow-stopped` / `in-progress` from the owning workflow's status (its receipt's id, else its plan directory);
    `cap-cut` for a mined row of a source the legacy whole-file judge capped; `not-selected` for a judged row the legacy
    pipeline never reached; else `unknown`."""
    plan = plans.offered.get((source_key, iid)) if plans is not None else None
    if plan is not None and not plan["selected"]:
        return "cap-cut"
    status = plans.workflows.get(workflow_id) if plans is not None and workflow_id else None
    if status is None and plan is not None:
        status = plans.dirs.get(plan["workflow_dir"])
    if status is not None:
        return "in-progress" if status == "running" else "workflow-stopped"
    if plan is not None:
        return "unknown"
    if stage == "mined":
        return "cap-cut" if has_judged else "unknown"
    if stage == "judged":
        return "not-selected"
    return "unknown"


def _earlier_versions(layout: Layout, sid: str, current: int, events) -> list[dict] | None:
    """The archived versions below `current` (`archive/v<n>/`), each at the last stage its files (or its receipts) prove, with
    status `superseded` unless that version was itself rejected or errored."""
    archive = layout.scenario_dir(sid) / "archive"
    if not archive.is_dir():
        return None
    out: list[dict] = []
    for adir in sorted(archive.iterdir(), key=lambda p: p.name):
        if not adir.is_dir() or not re.fullmatch(r"v[1-9][0-9]*", adir.name):
            continue
        v = int(adir.name[1:])
        if v >= current:
            continue
        present, files = _scenario_files(adir)
        decision = _scenario_decision(present, files, registered=True) or (("judged", "kept", None, []) if "judge.json" in present else None)
        if decision is None:
            continue  # nothing archived says what this version reached
        stage, status, gate, reasons = _apply_receipt(_latest_receipt(events, v), decision)
        if status not in NEGATIVE:
            status = "superseded"
        out.append({"version": v, "stage": stage, "status": status, "gate": gate, "reasons": reasons})
    return out or None


def _candidate_row(layout: Layout, source_key: str, iid: str, sid: str | None, row: dict, judged_at: datetime | None,
                   registry: dict[str, dict], origin: str, now: datetime, events: list[M.CandidateEventRow] = (), *,
                   plans: CandidatePlans | None = None, has_judged: bool = False) -> M.CandidateRow:
    error_file = _dict(read_json(layout.candidates_dir() / f"{iid}.error.json")) if SEGMENT_RE.fullmatch(iid) else {}
    stage, status, gate, reasons = _file_decision(row, error_file)
    present: set[str] = set()
    files: dict[str, dict] = {}
    entry = registry.get(sid) if sid else None
    if sid is not None:
        present, files = _scenario_files(layout.scenario_dir(sid))
    current = (scenario_version_of(files) if files else None) or (_int(entry.get("version")) if entry else None)
    if present and status not in NEGATIVE:
        decided = _scenario_decision(present, files, registered=entry is not None)
        if decided is not None:
            stage, status, gate, reasons = decided
    scenario_id = sid if (present or entry is not None) else None
    version = current
    latest = _latest_receipt(events, current)
    workflow_id = run_id = candidates_path = None
    if latest is not None:
        workflow_id, run_id, candidates_path = latest.workflow_id, latest.run_id, latest.candidates_path
        scenario_id, version = latest.scenario_id or scenario_id, latest.version or version
        stage, status, gate, reasons = _apply_receipt(latest, (stage, status, gate, reasons))
    # Terminal status: `kept` means it advanced, so a candidate kept at any stage but the last never advanced -> pending, with why.
    if status == "kept" and stage != LAST_STAGE:
        status, gate, reasons = "pending", None, [_pending_reason(stage, has_judged, plans, source_key, iid, workflow_id)]
    elif status == "pending" and not reasons:
        reasons = [_pending_reason(stage, has_judged, plans, source_key, iid, workflow_id)]
    earlier = _earlier_versions(layout, sid, current, events) if sid is not None and current is not None and current > 1 else None
    source = _str(row.get("source")) or ("swebench-annotated" if "swebench" in source_key else source_key.split("--", 1)[0])
    discovery = _dict(row.get("discovery"))
    return M.CandidateRow(
        instance_id=iid, source_key=source_key, source=source, repo=_str(row.get("repo")), pr_number=pr_number_of(iid),
        pr_kind=_str(discovery.get("kind")), stage=stage, status=status, gate=gate, reasons=reasons, judged_at=judged_at,
        scenario_id=scenario_id, version=version, candidates_path=candidates_path, earlier_versions=earlier,
        created_at=now, updated_at=now, workflow_id=workflow_id, run_id=run_id, origin=origin)


# ----------------------------------------------------------------------------- trials

TRIAL_EVIDENCE: tuple[str, ...] = ("record.json", "status.json", "runner-error.json", "request.json", "launch.json")
# Statuses that mean "nothing was graded": the console shows their reason (`TrialRow.failure`) and its "graded only" chip hides them.
FAILURE_STATUSES: tuple[str, ...] = ("runner-error", "launch-failed", "unresolved")
# (file, keys) in the order a reason is looked for; the first non-empty value wins.
_FAILURE_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (("runner-error.json", ("error", "message", "type")), ("launch.json", ("error",)), ("status.json", ("error",)))


def trial_ids(layout: Layout) -> list[str]:
    """Trial directories that hold any evidence (`record.json` or one of the files an unresolved attempt leaves);
    an empty directory is not an attempt."""
    trials = layout.root / "trials"
    if not trials.is_dir():
        return []
    return sorted(p.name for p in trials.iterdir()
                  if p.is_dir() and TRIAL_ID_RE.fullmatch(p.name) and any((p / name).exists() for name in TRIAL_EVIDENCE))


def _message(value: Any) -> str | None:
    """A human-readable error out of whatever shape a writer used: a string, a number, or a dict with message/error/type."""
    if isinstance(value, dict):
        return next((m for m in (_message(value.get(k)) for k in ("message", "error", "type")) if m), None)
    if value is None or isinstance(value, (bool, list)):
        return None
    return str(value).strip() or None


def trial_failure(d: Path) -> str | None:
    """The reason an attempt was never graded, from the files the runner and the launcher leave (`_FAILURE_SOURCES`);
    None when none of them carries one. Pure read; the console truncates for the table cell, the row keeps the full text."""
    for name, keys in _FAILURE_SOURCES:
        data = _dict(read_json(d / name))
        found = next((m for m in (_message(data.get(k)) for k in keys) if m), None)
        if found:
            return found
    return None


def _success(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def project_trial(layout: Layout, trial_id: str, *, origin: str | None = None, now: datetime | None = None
                  ) -> tuple[M.TrialRow, list[M.ExchangeRow], list[M.CostRow]]:
    """The trial row plus its exchanges (from the frozen transcript) and priced cost records (from costs.json)."""
    origin = _default_origin(origin)
    now = now or M.utcnow()
    d = layout.trial_dir(trial_id)
    if not d.is_dir():
        raise ProjectionError(f"{trial_id}: no trial directory")
    raw = read_json(d / "record.json") if (d / "record.json").exists() else None
    has_record = isinstance(raw, dict) and bool(raw)
    request, launch, status_file = _dict(read_json(d / "request.json")), _dict(read_json(d / "launch.json")), _dict(read_json(d / "status.json"))
    if has_record:
        rec = normalize_record(raw, d)
        status_raw = rec.get("status") or status_file.get("status") or "completed"
    else:
        u = unresolved_attempt(d)
        if u is None:
            raise ProjectionError(f"{trial_id}: no record.json and no launch evidence")
        rec = u
        if launch and not status_file and not (d / "runner-error.json").exists():
            status_raw = "launch-failed" if launch.get("phase") == "failed" else "unresolved"
        else:
            status_raw = u["status"]
    status = TRIAL_STATUS.get(str(status_raw).lower(), "unresolved")
    rc = _dict(rec.get("run_config")) or _dict(request.get("run_config"))
    identity = {
        "scenario_id": rec.get("scenario_id") or request.get("scenario_id"),
        "spec_hash": rec.get("spec_hash") or request.get("spec_hash"),
        "condition": rec.get("condition") or rc.get("condition") or request.get("condition"),
        "model": rec.get("model") or rc.get("model") or request.get("model"),
        "run_config_hash": rec.get("run_config_hash") or request.get("run_config_hash"),
        "attempt_index": rec.get("attempt_index", request.get("attempt_index")),
    }
    missing = [k for k, v in identity.items() if v in (None, "")]
    if missing:
        raise ProjectionError(f"{trial_id}: cannot identify the trial from its files; missing {missing}")
    if identity["condition"] not in CONDITIONS:
        raise ProjectionError(f"{trial_id}: unknown condition {identity['condition']!r}")
    brief_profile = rec.get("brief_profile") or rc.get("brief_profile") or ("natural" if identity["condition"] == "full" else None)
    if brief_profile not in ("natural", "withheld"):
        raise ProjectionError(f"{trial_id}: unknown brief profile {brief_profile!r}")
    purpose = rec.get("purpose") or rc.get("purpose") or ("demonstration" if rc else None)  # RunConfig's default
    if purpose not in PURPOSES:
        raise ProjectionError(f"{trial_id}: unknown purpose {purpose!r}")
    spec_json = _dict(read_json(d / "spec.json"))
    reward, report = _dict(rec.get("reward")), _dict(rec.get("report"))
    transcript = _dict(rec.get("transcript")) or _dict(read_json(d / "transcript.json"))
    audit = _dict(read_json(d / "audit.json"))
    signals = report.get("integrity_signals")
    workflow_id = _first(rec, request, launch, key="workflow_id")
    run_id = _first(rec, request, launch, key="run_id")
    row_origin = _first(rec, launch, request, key="origin", allowed=ORIGINS) or origin
    stakeholder = _dict(rc.get("stakeholder"))
    from swesim.index.cohorts import semantic_config
    # Money and tokens: costs.json (every role) when it has records, else the record's own solver account.
    costs, tokens_in, tokens_out, cost_usd = project_costs(trial_id, d / "costs.json", origin=row_origin, now=now, workflow_id=workflow_id)
    if not costs:
        cost_usd, tokens_in, tokens_out = dec(rec.get("cost_usd")), _int(rec.get("input_tokens")), _int(rec.get("output_tokens"))
    started = aware(status_file.get("started_at")) or aware(rec.get("started_at")) or aware(launch.get("started_at"))
    finished = aware(status_file.get("finished_at")) or aware(rec.get("finished_at"))
    row = M.TrialRow(
        trial_id=trial_id, scenario_id=str(identity["scenario_id"]), version=_int(spec_json.get("version")), spec_hash=str(identity["spec_hash"]),
        condition=identity["condition"], brief_profile=brief_profile, model=str(identity["model"]), provider=provider_of(str(identity["model"])),
        run_config_hash=str(identity["run_config_hash"]), attempt_index=int(identity["attempt_index"]), purpose=purpose, status=status,
        outcome_code=outcome_code(rec) if has_record and reward else None,
        reward=dec(reward.get("reward")) if has_record and reward else None,
        success=_success(reward.get("success")) if has_record and reward else None,
        integrity_signal_count=len(signals) if isinstance(signals, list) else None,
        questions_asked=count_exchanges(rec, "ask") if has_record else None,
        confirmations=count_exchanges(rec, "confirm") if has_record else None,
        asked_before_implementing=audit.get("asked_before_implementing") if isinstance(audit.get("asked_before_implementing"), bool) else None,
        started_at=started, finished_at=finished,
        launcher=launch.get("launcher") if launch.get("launcher") in LAUNCHERS else None,
        job_name=_str(launch.get("handle")) or _str(launch.get("unit")),
        cost_usd=cost_usd, input_tokens=tokens_in, output_tokens=tokens_out,
        stakeholder_model=_str(stakeholder.get("model_id")) or _str(request.get("stakeholder_model")),
        stakeholder_prompt_version=_str(stakeholder.get("prompt_version")),
        evaluation_config=semantic_config(rc, report),
        failure=trial_failure(d) if status in FAILURE_STATUSES else None,
        created_at=now, updated_at=now, workflow_id=workflow_id, run_id=run_id,
        worker_image_digest=_first(rec, launch, key="worker_image_digest") or _first(launch, key="image_digest"), origin=row_origin)
    exchanges = project_exchanges(trial_id, transcript, audit, origin=row_origin, now=now, workflow_id=workflow_id, run_id=run_id)
    return row, exchanges, costs


def project_exchanges(trial_id: str, transcript: dict, audit: dict | None = None, *, origin: str, now: datetime | None = None,
                      workflow_id: str | None = None, run_id: str | None = None) -> list[M.ExchangeRow]:
    """Message events of a frozen transcript as rows; `seq` is the event's when every event has a distinct one."""
    now = now or M.utcnow()
    events = [e for e in (transcript.get("events") or []) if isinstance(e, dict) and e.get("kind") == "message"]
    seqs = [_int(e.get("seq")) for e in events]
    use_seq = all(s is not None for s in seqs) and len(set(seqs)) == len(seqs)
    audit = audit or {}
    questions = {q["seq"]: q for q in audit.get("questions") or [] if isinstance(q, dict) and _int(q.get("seq")) is not None}
    replies = {r["seq"]: r for r in audit.get("replies") or [] if isinstance(r, dict) and _int(r.get("seq")) is not None}
    rows: list[M.ExchangeRow] = []
    for i, e in enumerate(events):
        seq = seqs[i] if use_seq else i
        role_raw = e.get("role")
        role = "agent" if role_raw == "user" else "stakeholder" if role_raw == "assistant" else "system"
        kind = e.get("exchange") if e.get("exchange") in ("ask", "confirm", "review") else "system"
        content = e.get("content")
        text = content if isinstance(content, str) else json.dumps(content) if content is not None else ""
        by_role = questions if role == "agent" else replies if role == "stakeholder" else {}
        rows.append(M.ExchangeRow(
            trial_id=trial_id, seq=seq, role=role, exchange=kind, ts=aware(e.get("ts")), request_id=_str(e.get("request_id")),
            patience_after=_int(e.get("patience_remaining")) if role == "stakeholder" else None, text_public=text,
            disclosed_fact_ids=[str(x) for x in (e.get("disclosed_fact_ids") or []) if x is not None] if isinstance(e.get("disclosed_fact_ids"), list) else [],
            regenerated=e.get("regenerated") if isinstance(e.get("regenerated"), bool) else None,
            redacted_terms=[str(x) for x in (e.get("redacted_terms") or []) if x is not None] if isinstance(e.get("redacted_terms"), list) else [],
            audit=by_role.get(seq), created_at=now, updated_at=now, workflow_id=workflow_id, run_id=run_id, origin=origin))
    return rows


def project_costs(trial_id: str, path: Path, *, origin: str, now: datetime | None = None, workflow_id: str | None = None
                  ) -> tuple[list[M.CostRow], int | None, int | None, Decimal | None]:
    """(cost rows, input tokens, output tokens, usd) from `costs.json`, a list of CostRecord dicts. Tokens are summed
    over every record; `usd` over the priced ones (None when no record is priced). A record whose price is unknown
    (`usd` null, `price_source` says why) is a row with `usd` None. A file that is not a list of records raises
    `ProjectionError`; an absent file gives `([], None, None, None)`."""
    now = now or M.utcnow()
    if not path.exists():
        return [], None, None, None
    data = read_json(path)
    if not isinstance(data, list):
        raise ProjectionError(f"{path}: costs.json must be a list of cost records")
    rows: list[M.CostRow] = []
    tokens_in = tokens_out = 0
    usd_total: Decimal | None = None
    for i, item in enumerate(data):
        row = _cost_row(item, path, i, origin=origin, now=now, trial_id=trial_id, workflow_id=workflow_id)
        rows.append(row)
        tokens_in += row.input_tokens
        tokens_out += row.output_tokens
        if row.usd is not None:
            usd_total = (usd_total or Decimal(0)) + row.usd
    return rows, tokens_in, tokens_out, usd_total


def _cost_row(item: Any, path: Path, i: int, *, origin: str, now: datetime, trial_id: str | None = None, workflow_id: str | None = None) -> M.CostRow:
    if not isinstance(item, dict):
        raise ProjectionError(f"{path}[{i}]: not a cost record")
    fields = {k: v for k, v in item.items() if k in M.CostRow.model_fields}
    if trial_id is not None:
        fields.setdefault("trial_id", trial_id)
    if fields.get("workflow_id") is None and workflow_id is not None:
        fields["workflow_id"] = workflow_id
    fields["origin"] = _origin(fields.get("origin")) or origin
    fields.setdefault("created_at", now)
    fields["updated_at"] = now
    try:
        return M.CostRow(**fields)
    except ValidationError as e:
        raise ProjectionError(f"{path}[{i}]: {e.error_count()} invalid field(s): {[err['loc'] for err in e.errors()]}") from e


def project_workflow_costs(layout: Layout, *, origin: str | None = None, now: datetime | None = None,
                           workflow_id: str | None = None) -> list[M.CostRow]:
    """Cost records of workflows without a trial (`Layout.workflow_costs_path`, written by `swesim.costs.record_cost`).
    A file that is not a list of records raises `ProjectionError`."""
    origin = _default_origin(origin)
    now = now or M.utcnow()
    cdir = layout.costs_dir()
    if not cdir.is_dir():
        return []
    rows: list[M.CostRow] = []
    paths = [layout.workflow_costs_path(workflow_id)] if workflow_id is not None else sorted(cdir.glob("*.json"))
    for path in paths:
        if not path.exists():
            continue
        workflow_id = path.name[: -len(".json")]
        data = read_json(path)
        if not isinstance(data, list):
            raise ProjectionError(f"{path}: costs file must be a list of cost records")
        rows.extend(_cost_row(item, path, i, origin=origin, now=now, workflow_id=workflow_id) for i, item in enumerate(data))
    return rows


# ----------------------------------------------------------------------------- scores

def _spec_hash_of_trials(layout: Layout, trial_ids_: list) -> str | None:
    for tid in trial_ids_:
        if not isinstance(tid, str) or not TRIAL_ID_RE.fullmatch(tid):
            continue
        for name in ("record.json", "request.json"):
            h = _str(_dict(read_json(layout.trial_file(tid, name))).get("spec_hash"))
            if h:
                return h
    return None


def _sum_ints(values: Any) -> int:
    return sum(v for v in values if isinstance(v, int) and not isinstance(v, bool)) if isinstance(values, list) else 0


def project_scores(layout: Layout, scenario_id: str, *, origin: str | None = None, now: datetime | None = None) -> list[M.ScoreRow]:
    """One row per group of `scores.json`; the `unknown-config` group (no lineage) has no row."""
    origin = _default_origin(origin)
    now = now or M.utcnow()
    path = layout.scenario_file(scenario_id, "scores.json")
    groups = read_json(path) if path.exists() else None
    if not isinstance(groups, dict):
        return []
    computed_at = mtime(path) or now
    _present, files = _scenario_files(layout.scenario_dir(scenario_id))
    fallback = _first(files["approval.json"], files["compile.json"], files["validation.json"], key="spec_hash")
    rows: list[M.ScoreRow] = []
    for key, g in sorted(groups.items()):
        if not isinstance(g, dict) or not all(_str(g.get(k)) for k in ("condition", "brief_profile", "run_config_hash", "model")):
            continue
        if g["condition"] not in CONDITIONS:
            raise ProjectionError(f"{scenario_id} scores[{key}]: unknown condition {g['condition']!r}")
        spec_hash = _spec_hash_of_trials(layout, g.get("trial_ids") or []) or fallback
        if not spec_hash:
            raise ProjectionError(f"{scenario_id} scores[{key}]: spec_hash unknown")
        rows.append(M.ScoreRow(
            scenario_id=scenario_id, spec_hash=spec_hash, condition=g["condition"], brief_profile=g["brief_profile"], model=g["model"],
            run_config_hash=g["run_config_hash"], n=_int(g.get("n")) or 0, success_count=_int(g.get("success_count")) or 0,
            requested_k=_int(g.get("requested_k")), pass_at_k=dec(g.get("pass_at_k")), pass_pow_k=dec(g.get("pass_pow_k")), mean_reward=dec(g.get("mean_reward")),
            questions_asked=_sum_ints(g.get("questions_asked")), confirmations=_sum_ints(g.get("confirmations")), computed_at=computed_at,
            created_at=now, updated_at=now, origin=origin))
    return rows


# ----------------------------------------------------------------------------- Harbor job directories

def project_workflows_from_jobs(layout: Layout, *, origin: str | None = None, now: datetime | None = None) -> list[M.WorkflowRow]:
    """What `jobs/runs/<name>/{config,result,lock}.json` say about each Harbor job: not Temporal state, but the file
    reader has nothing better. `started_at` is the lock file's UTC `created_at` (Harbor's result timestamps are naive
    local time; only their difference is used)."""
    origin = _default_origin(origin)
    now = now or M.utcnow()
    runs = layout.jobs_dir("runs")
    if not runs.is_dir():
        return []
    rows: list[M.WorkflowRow] = []
    for d in sorted(p for p in runs.iterdir() if p.is_dir()):
        result, config, lock = _dict(read_json(d / "result.json")), _dict(read_json(d / "config.json")), _dict(read_json(d / "lock.json"))
        if not result and not config:
            continue
        stats = _dict(result.get("stats"))
        total, completed = _int(result.get("n_total_trials")), _int(stats.get("n_completed_trials"))
        if _int(stats.get("n_errored_trials")):
            status = "failed"
        elif _int(stats.get("n_cancelled_trials")):
            status = "cancelled"
        elif total is not None and completed == total:
            status = "completed"
        else:
            status = "running"
        started = aware(lock.get("created_at")) or mtime(d / "config.json") or now
        rs, rf = aware(result.get("started_at")), aware(result.get("finished_at"))
        closed = started + (rf - rs) if rs and rf and rf >= rs else None
        agents = config.get("agents") or []
        agent = agents[0] if agents and isinstance(agents[0], dict) else {}
        tasks = config.get("tasks") or []
        task = tasks[0] if tasks and isinstance(tasks[0], dict) else {}
        failure = None
        for ev in _dict(stats.get("evals")).values():
            exceptions = _dict(_dict(ev).get("exception_stats"))
            if exceptions:
                failure = ", ".join(sorted(exceptions))
                break
        rows.append(M.WorkflowRow(
            workflow_id=d.name, run_id=_str(result.get("id")) or "harbor-job", type="harbor-job", parent_workflow_id=None, status=status,
            params_summary={"agent": agent.get("name"), "model": agent.get("model_name"), "task": task.get("path"),
                            "trial_id": d.name if TRIAL_ID_RE.fullmatch(d.name) else None},
            failure=failure, started_at=started, closed_at=closed if status != "running" else None, task_queue=None, cost_usd=None,
            search_attributes={}, created_at=now, updated_at=now, origin=origin))
    return rows


__all__ = ["ProjectionError", "NoScenarioFiles", "aware", "dec", "mtime", "provider_of", "scenario_id_for", "pr_number_of", "read_json",
           "project_registry", "scenario_ids", "scenario_version_of", "validation_status_of", "project_scenario", "project_scenario_versions",
           "source_keys", "discovery_source_keys", "project_discovery", "project_discoveries", "read_discovery_snapshot",
           "project_candidates", "candidate_events_by_identity", "candidate_plans", "CandidatePlans", "trial_ids", "project_trial", "project_exchanges", "project_costs", "project_workflow_costs",
           "project_scores", "project_workflows_from_jobs"]
