"""Index activities (interfaces §2): Temporal activities on the `swesim-control` queue that keep the index current.

Contract
- Eight activities, names exact (`swesim.workflows.queues.QUEUE_OF` routes them to CONTROL): `index_scenario_activity`,
  `index_candidate_activity`, `index_trial_activity`, `index_score_activity`, `index_workflow_activity`,
  `index_rebuild_activity`, `index_discovery_activity`, `index_candidate_event_activity`. `ALL` lists them for the worker.
- `index_candidate_event_activity` is the candidate's decision receipt (P1-7): the workflow reports the gate it stopped
  or passed at (`phase`, `stage`, `status`, `gate`, `reasons`, `terminal`, ids); the receipt is published as an immutable
  snapshot in the record *before* SQL, even when the index is disabled, and the candidate row is reprojected from files
  plus receipts so a rejected candidate is never shown as kept. Its id is content-derived: a retry is idempotent.
- Each reads the files through `Layout`, projects rows with `swesim.index.project`, upserts them, and returns
  `{"indexed": True, "rows": n}`. Idempotent: the same payload twice leaves the same rows.
- When the index is disabled (`Settings.database_url` unset), index-only activities return
  `{"indexed": False, "reason": "index disabled"}`. Discovery still freezes its immutable file snapshot and requires
  a valid source payload; it also returns the snapshot's discovery_id.
- A payload or record that cannot identify a row raises `ApplicationError(type="projection", non_retryable=True)`;
  transient database errors propagate unchanged so Temporal retries them (`retries.HOOK`).
- Payloads carry ids and artifact references. Discovery additionally carries provenance (workflow/run, band and time).
  `index_workflow_activity` also carries state: the workflow itself is the source of
  Temporal state, so its payload carries the row's fields (`workflow_id, run_id, type, status, params_summary,
  failure, parent_workflow_id`, optionally `started_at, closed_at, task_queue, cost_usd, search_attributes`).
- The database work is synchronous SQLAlchemy; the activities run it in a thread so the worker's event loop stays free.
  One `IndexStore` per `Settings` object is kept (`store_for`); `reset_store()` drops it (tests).
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from swesim.index import models as M
from swesim.index import project as P
from swesim.index.store import IndexStore
from swesim.paths import atomic_json
from swesim.settings import Settings, get_settings
from swesim.storage.layout import Layout

DISABLED: dict[str, Any] = {"indexed": False, "reason": "index disabled"}
WORKFLOW_STATUSES: tuple[str, ...] = M.WorkflowStatus.__args__
_cache: tuple[Settings, IndexStore] | None = None


def store_for(settings: Settings | None = None) -> IndexStore | None:
    """One `IndexStore` per `Settings` instance (`get_settings()` caches one; `reset_settings()` yields a new one and
    therefore a new store). None when the index is disabled."""
    global _cache
    s = settings or get_settings()
    if s.database_url is None:
        return None
    if _cache is None or _cache[0] is not s:
        _cache = (s, IndexStore.from_settings(s))
    return _cache[1]


def reset_store() -> None:
    global _cache
    _cache = None


def _projection_error(msg: str) -> ApplicationError:
    return ApplicationError(msg, type="projection", non_retryable=True)


def _require(p: Any, *keys: str) -> list[Any]:
    if not isinstance(p, dict):
        raise _projection_error(f"payload must be a dict, got {type(p).__name__}")
    missing = [k for k in keys if p.get(k) in (None, "")]
    if missing:
        raise _projection_error(f"payload lacks {missing}")
    return [p[k] for k in keys]


def _layout(s: Settings) -> Layout:
    return Layout(root=s.workspace)


# ----------------------------------------------------------------------------- synchronous bodies (tested directly)

def index_scenario(p: dict, settings: Settings | None = None) -> dict:
    """`{"scenario_id", "version"}`: one version when given, else the current one plus every archived version."""
    s = settings or get_settings()
    store = store_for(s)
    if store is None:
        return dict(DISABLED)
    (sid,) = _require(p, "scenario_id")
    version = p.get("version")
    layout = _layout(s)
    registry = P.project_registry(layout, s.registry_path)
    try:
        if version is None:
            rows = P.project_scenario_versions(layout, sid, registry=registry, origin=s.origin)
        else:
            rows = [P.project_scenario(layout, sid, int(version), registry=registry, origin=s.origin)]
    except (ValueError, TypeError) as e:  # ProjectionError, LayoutError, ValidationError, a non-integer version
        raise _projection_error(f"scenario {sid}: {e}") from e
    return {"indexed": True, "rows": store.upsert_scenarios(rows)}


def index_candidate(p: dict, settings: Settings | None = None) -> dict:
    """`{"source_key", "instance_id"}`: the one candidate row of that source key."""
    s = settings or get_settings()
    store = store_for(s)
    if store is None:
        return dict(DISABLED)
    source_key, instance_id = _require(p, "source_key", "instance_id")
    layout = _layout(s)
    try:
        events = P.candidate_events_by_identity(layout, s)
        rows = [r for r in P.project_candidates(layout, source_key, registry=P.project_registry(layout, s.registry_path), origin=s.origin, events=events)
                if r.instance_id == instance_id]
    except ValueError as e:
        raise _projection_error(f"candidates {source_key}: {e}") from e
    if not rows:
        raise _projection_error(f"candidate {instance_id} is not in the {source_key} files")
    return {"indexed": True, "rows": store.upsert_candidates(rows)}


CANDIDATE_EVENT_PAYLOAD: tuple[str, ...] = ("candidates_path", "scenario_id", "spec_hash", "result_status", "gate")


def candidate_event_row(p: dict, settings: Settings) -> M.CandidateEventRow:
    """The receipt a workflow's payload describes; `occurred_at` defaults to now (workflows pass `workflow.now()`)."""
    source_key, instance_id, phase, stage, status, workflow_id, run_id = _require(p, "source_key", "instance_id", "phase", "stage", "status",
                                                                                 "workflow_id", "run_id")
    reasons = [str(r) for r in (p.get("reasons") or []) if r is not None] if isinstance(p.get("reasons"), list) else []
    fields = {"source_key": str(source_key), "instance_id": str(instance_id), "phase": str(phase), "stage": stage, "status": status,
              "reasons": reasons, "terminal": bool(p.get("terminal", True)), "workflow_id": str(workflow_id), "run_id": str(run_id),
              "version": P._int(p.get("version")), **{k: P._str(p.get(k)) for k in CANDIDATE_EVENT_PAYLOAD}}
    occurred = P.aware(p.get("occurred_at")) or M.utcnow()
    try:
        return M.CandidateEventRow(event_id=M.candidate_event_identity(fields), occurred_at=occurred, created_at=occurred, updated_at=occurred,
                                   worker_image_digest=settings.worker_image_digest, origin=settings.origin, **fields)
    except ValueError as e:
        raise _projection_error(f"candidate event {source_key}/{instance_id}: {e}") from e


def index_candidate_event(p: dict, settings: Settings | None = None) -> dict:
    """Publish one candidate decision receipt (record first, SQL second) and reproject the candidate's row."""
    from swesim.storage.history import persist_row
    s = settings or get_settings()
    row = candidate_event_row(p, s)
    layout = _layout(s)
    persist_row(layout, row, s)
    store = store_for(s)
    if store is None:
        return {**DISABLED, "event_id": row.event_id}
    store.upsert_candidate_event(row)
    rows = 1
    try:
        events = P.candidate_events_by_identity(layout, s)
        candidates = [r for r in P.project_candidates(layout, row.source_key, registry=P.project_registry(layout, s.registry_path), origin=s.origin, events=events)
                      if r.instance_id == row.instance_id]
    except ValueError:  # the candidate's source files are not (yet) in this copy of the record: the receipt alone is indexed
        candidates = []
    rows += store.upsert_candidates(candidates)
    return {"indexed": True, "rows": rows, "event_id": row.event_id, "stage": row.stage, "status": row.status}


def index_discovery(p: dict, settings: Settings | None = None) -> dict:
    """Freeze discovery provenance before indexing, including sources with zero candidates and SQL-disabled runs.

    Payload: source_key, artifact_sha256, workflow_id, run_id, optional repo/band/discovered_at. A replay reads its
    immutable snapshot even if another discovery has replaced the flat rejects file in the meantime.
    """
    s = settings or get_settings()
    (key,) = _require(p, "source_key")
    layout = _layout(s)
    store = store_for(s)
    expected = p.get("artifact_sha256")
    identity = M.discovery_identity(key, expected, p.get("workflow_id"), p.get("run_id")) if expected else None
    try:
        artifact = layout.resolve_artifact(p["artifact_path"]) if p.get("artifact_path") is not None else None
        # On the hosted stack the database serializes publication of the same immutable observation across workers.
        with store.advisory_lock(f"swesim.discovery:{key}:{identity}") if store is not None else nullcontext():
            path = layout.discovery_snapshot(key, identity) if identity else None
            if path is not None and path.is_file():
                row = P.read_discovery_snapshot(layout, key, identity)
            else:
                row = P.project_discovery(layout, key, origin=s.origin, workflow_id=p.get("workflow_id"), run_id=p.get("run_id"),
                                          artifact_sha256=expected, worker_image_digest=s.worker_image_digest,
                                          artifact_path=artifact, discovered_at=p.get("discovered_at"), repo=p.get("repo"), band=p.get("band"))
                if row is None:
                    raise P.ProjectionError("discovery rejects file is missing")
                path = layout.discovery_snapshot(key, row.discovery_id)
                if path.is_file():
                    row = P.read_discovery_snapshot(layout, key, row.discovery_id)
                else:
                    atomic_json(path, row.model_dump(mode="json"))
        if store is not None:
            store.upsert_discovery(row)
    except (ValueError, TypeError) as exc:
        raise _projection_error(f"discovery {key}: {exc}") from exc
    return {"indexed": True, "rows": 1, "discovery_id": row.discovery_id} if store is not None else {**DISABLED, "discovery_id": row.discovery_id}


def index_trial(p: dict, settings: Settings | None = None) -> dict:
    """`{"trial_id"}`: the trial row plus its exchanges and priced costs from the record."""
    s = settings or get_settings()
    store = store_for(s)
    if store is None:
        return dict(DISABLED)
    (tid,) = _require(p, "trial_id")
    try:
        from swesim.costs import repair_stakeholder_costs
        repair_stakeholder_costs(_layout(s), tid)
        row, exchanges, costs = P.project_trial(_layout(s), tid, origin=s.origin)
    except ValueError as e:
        raise _projection_error(f"trial {tid}: {e}") from e
    store.upsert_trial(row)
    n = 1 + store.upsert_exchanges(exchanges) + store.add_costs(costs)
    return {"indexed": True, "rows": n, "status": row.status}


def index_score(p: dict, settings: Settings | None = None) -> dict:
    """`{"scenario_id"}`: every group of the scenario's scores.json."""
    s = settings or get_settings()
    store = store_for(s)
    if store is None:
        return dict(DISABLED)
    (sid,) = _require(p, "scenario_id")
    try:
        rows = P.project_scores(_layout(s), sid, origin=s.origin)
    except ValueError as e:
        raise _projection_error(f"scores {sid}: {e}") from e
    return {"indexed": True, "rows": store.upsert_scores(rows)}


def index_workflow(p: dict, settings: Settings | None = None) -> dict:
    """The workflow's own report of itself. `started_at` is kept from the existing row when the payload has none;
    a terminal status without `closed_at` closes the row now."""
    s = settings or get_settings()
    store = store_for(s)
    if store is None:
        return dict(DISABLED)
    workflow_id, run_id, type_, status = _require(p, "workflow_id", "run_id", "type", "status")
    if status not in WORKFLOW_STATUSES:
        raise _projection_error(f"workflow {workflow_id}: unknown status {status!r}")
    existing = store.workflow(str(workflow_id))
    if existing is not None and existing.run_id != str(run_id):
        existing = None
    now = M.utcnow()
    started_at = P.aware(p.get("started_at")) or (existing.started_at if existing else None) or now
    closed_at = P.aware(p.get("closed_at"))
    if closed_at is None and status != "running":
        closed_at = (existing.closed_at if existing else None) or now
    params = p.get("params_summary")
    attrs = p.get("search_attributes")
    try:
        row = M.WorkflowRow(
            workflow_id=str(workflow_id), run_id=str(run_id), type=str(type_), parent_workflow_id=P._str(p.get("parent_workflow_id")), status=status,
            params_summary=params if isinstance(params, dict) else {}, failure=P._str(p.get("failure")), started_at=started_at, closed_at=closed_at,
            task_queue=P._str(p.get("task_queue")), cost_usd=P.dec(p.get("cost_usd")), search_attributes=attrs if isinstance(attrs, dict) else {},
            lifecycle_source=p.get("lifecycle_source") or "workflow",
            created_at=existing.created_at if existing else now, updated_at=now, worker_image_digest=s.worker_image_digest, origin=s.origin)
    except ValueError as e:
        raise _projection_error(f"workflow {workflow_id}: {e}") from e
    store.upsert_workflow(row)
    return {"indexed": True, "rows": 1}


def index_rebuild(p: dict, settings: Settings | None = None) -> dict:
    """`{}` (optionally `{"origin"}`): truncate-and-reproject every file-backed table. The result carries the counts
    and diff; skipped ids are capped at 20 per table to keep the payload small."""
    s = settings or get_settings()
    store = store_for(s)
    if store is None:
        return dict(DISABLED)
    origin = p.get("origin") if isinstance(p, dict) else None
    if origin is not None and origin not in P.ORIGINS:
        raise _projection_error(f"unknown origin {origin!r}")
    row = store.rebuild_from_layout(_layout(s), s.registry_path, origin=origin)
    counts = {k: v for k, v in row.counts.items() if k not in ("skipped", "projected")}
    skipped = {k: v[:20] for k, v in row.counts.get("skipped", {}).items()}
    return {"indexed": True, "rows": sum(counts.get(t, 0) for t in ("scenarios", "candidates", "trials", "exchanges", "scores", "costs")),
            "rebuild_id": row.rebuild_id, "counts": counts, "diff": row.diff, "skipped": skipped,
            "skipped_total": {k: len(v) for k, v in row.counts.get("skipped", {}).items()}}


# ----------------------------------------------------------------------------- Temporal activities

@activity.defn
async def index_scenario_activity(p: dict) -> dict:
    return await asyncio.to_thread(index_scenario, p)


@activity.defn
async def index_candidate_activity(p: dict) -> dict:
    return await asyncio.to_thread(index_candidate, p)


@activity.defn
async def index_trial_activity(p: dict) -> dict:
    return await asyncio.to_thread(index_trial, p)


@activity.defn
async def index_score_activity(p: dict) -> dict:
    return await asyncio.to_thread(index_score, p)


@activity.defn
async def index_workflow_activity(p: dict) -> dict:
    return await asyncio.to_thread(index_workflow, p)


@activity.defn
async def index_rebuild_activity(p: dict) -> dict:
    return await asyncio.to_thread(index_rebuild, p)


@activity.defn
async def index_discovery_activity(p: dict) -> dict:
    return await asyncio.to_thread(index_discovery, p)


@activity.defn
async def index_candidate_event_activity(p: dict) -> dict:
    return await asyncio.to_thread(index_candidate_event, p)


ALL = [index_scenario_activity, index_candidate_activity, index_trial_activity, index_score_activity, index_workflow_activity,
       index_rebuild_activity, index_discovery_activity, index_candidate_event_activity]
