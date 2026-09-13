"""`FileIndexReader`: the `IndexReader` protocol over the files, for laptops without Postgres and for offline tests.

Contract
- Every answer is projected from the files on each call through `swesim.index.project` (no cache: the record is
  the truth and the laptop record is small). A directory that cannot be projected is logged and skipped, never a
  failure of the whole listing.
- Filters, ordering, paging, the leaderboard gate and evaluator blanking behave exactly as in `IndexStore`.
- `workflows()`, `operator_actions()`, `rebuilds()` and the `candidate_events` table read the immutable row snapshots
  under `evidence/history/` (`swesim.index.history`), the same files `IndexStore.rebuild_from_layout` reprojects;
  workflows also fold in Harbor job directories (`jobs/runs/*`) and operator actions the console's file-mode log
  `evidence/console/actions.jsonl`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from swesim.index import history as H
from swesim.index import models as M
from swesim.index import project as P
from swesim.index import reports as R
from swesim.index.schema import TABLES
from swesim.settings import Settings, get_settings
from swesim.storage.layout import Layout

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
CONTROL_FIELDS: frozenset[str] = frozenset({"offset", "limit", "order_by", "desc", "q", "latest_only", "min_n", "where"})
RANGES: dict[str, tuple[str, str]] = {"started_from": ("started_at", ">="), "started_to": ("started_at", "<="),
                                      "judged_from": ("judged_at", ">="), "judged_to": ("judged_at", "<="),
                                      "at_from": ("at", ">="), "at_to": ("at", "<=")}
TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "scenarios": ("scenario_id", "source", "source_ref", "spec_hash", "compiled_hash"),
    "candidates": ("instance_id", "repo", "scenario_id", "gate", "source_key"),
    "discoveries": ("source_key", "discovery_id", "source", "repo", "band", "identity_kind", "workflow_id", "run_id"),
    "trials": ("trial_id", "scenario_id", "model", "spec_hash", "run_config_hash", "job_name", "workflow_id"),
    "scores": ("scenario_id", "model", "spec_hash", "run_config_hash"),
    "workflows": ("workflow_id", "run_id", "type", "failure", "parent_workflow_id"),
    "costs": ("trial_id", "workflow_id", "model", "provider"),
    "operator_actions": ("actor", "action", "target", "workflow_id"),
    "index_rebuilds": ("rebuild_id",),
    "exchanges": ("trial_id", "text_public", "request_id"),
    "candidate_events": ("source_key", "instance_id", "scenario_id", "phase", "gate", "workflow_id", "run_id"),
}
DEFAULT_ORDER: dict[str, str] = {"scenarios": "scenario_id", "candidates": "instance_id", "trials": "started_at", "scores": "scenario_id",
                                 "workflows": "started_at", "costs": "at", "operator_actions": "at", "index_rebuilds": "started_at",
                                 "exchanges": "seq", "discoveries": "discovered_at", "candidate_events": "occurred_at"}


def matches(row: BaseModel, f: BaseModel, text_fields: tuple[str, ...]) -> bool:
    """Equality lists, scalars, ranges and free text of a filter against one row (the SQL reader's WHERE, in Python)."""
    data = f.model_dump(exclude_none=True)
    for name, value in data.items():
        if name in CONTROL_FIELDS:
            continue
        if name in RANGES:
            field, op = RANGES[name]
            actual = getattr(row, field, None)
            if actual is None or (op == ">=" and actual < value) or (op == "<=" and actual > value):
                return False
            continue
        if not hasattr(row, name):
            continue
        actual = getattr(row, name)
        if isinstance(value, list):
            if actual not in value:
                return False
        elif actual != value:
            return False
    q = data.get("q")
    if q:
        needle = str(q).lower()
        if not any(needle in str(getattr(row, field)).lower() for field in text_fields if getattr(row, field, None) is not None):
            return False
    return True


def order(rows: list[T], order_by: str | None, default: str, desc: bool) -> list[T]:
    """Sort by one field, None values last whatever the direction; an unknown field is invalid input."""
    if not rows:
        return rows
    key = order_by or default
    if key not in type(rows[0]).model_fields:
        raise ValueError(f"unknown order_by column {key!r}")
    present = [r for r in rows if getattr(r, key) is not None]
    absent = [r for r in rows if getattr(r, key) is None]
    present.sort(key=lambda r: getattr(r, key), reverse=desc)
    return present + absent


def page(rows: list[T], f: M.PageFilter, default_order: str) -> M.Page[T]:
    ordered = order(rows, f.order_by, default_order, f.desc)
    return M.Page[T](items=ordered[f.offset:f.offset + f.limit], total=len(ordered), offset=f.offset, limit=f.limit)


def jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return json.loads(value.model_dump_json())
    return value


class FileIndexReader:
    def __init__(self, layout: Layout, registry_path: Path | None = None, *, settings: Settings | None = None):
        self.layout = layout
        self.registry_path = Path(registry_path) if registry_path is not None else layout.registry_path()
        self._settings = settings

    # ------------------------------------------------------------------ projection with skip logging
    @property
    def settings(self) -> Settings:
        return self._settings or get_settings()

    def _registry(self) -> dict[str, dict]:
        return P.project_registry(self.layout, self.registry_path)

    def _scenario_rows(self) -> list[M.ScenarioRow]:
        registry = self._registry()
        rows: list[M.ScenarioRow] = []
        for sid in P.scenario_ids(self.layout, registry):
            try:
                rows.extend(P.project_scenario_versions(self.layout, sid, registry=registry, origin=self.settings.origin))
            except ValueError as e:  # ProjectionError, LayoutError, ValidationError: the listing goes on without this scenario
                log.warning("scenario %s not projected: %s", sid, e)
        return rows

    def _candidate_rows(self, source_key: str | None = None) -> list[M.CandidateRow]:
        registry = self._registry()
        rows: list[M.CandidateRow] = []
        try:
            events = P.candidate_events_by_identity(self.layout, self._settings)
        except ValueError as e:
            log.warning("candidate receipts not projected: %s", e)
            events = {}
        for key in ([source_key] if source_key else P.source_keys(self.layout)):
            try:
                rows.extend(P.project_candidates(self.layout, key, registry=registry, origin=self.settings.origin, events=events))
            except ValueError as e:
                log.warning("candidates %s not projected: %s", key, e)
        return rows

    def _discovery_rows(self, source_key: str | None = None) -> list[M.DiscoveryRow]:
        rows: list[M.DiscoveryRow] = []
        for key in [source_key] if source_key is not None else P.discovery_source_keys(self.layout):
            try:
                rows.extend(P.project_discoveries(self.layout, key, origin=self.settings.origin))
            except ValueError as exc:
                log.warning("discovery %s not projected: %s", key, exc)
        return rows

    def _trials(self) -> list[tuple[M.TrialRow, list[M.ExchangeRow], list[M.CostRow]]]:
        out = []
        for tid in P.trial_ids(self.layout):
            try:
                out.append(P.project_trial(self.layout, tid, origin=self.settings.origin))
            except ValueError as e:
                log.warning("trial %s not projected: %s", tid, e)
        return out

    def _trial(self, trial_id: str) -> tuple[M.TrialRow, list[M.ExchangeRow], list[M.CostRow]] | None:
        try:
            return P.project_trial(self.layout, trial_id, origin=self.settings.origin)
        except ValueError:
            return None

    def _score_rows(self) -> list[M.ScoreRow]:
        rows: list[M.ScoreRow] = []
        for sid in P.scenario_ids(self.layout, self._registry()):
            try:
                rows.extend(P.project_scores(self.layout, sid, origin=self.settings.origin))
            except ValueError as e:
                log.warning("scores of %s not projected: %s", sid, e)
        return rows

    def _cost_rows(self) -> list[M.CostRow]:
        rows = [c for _t, _ex, cs in self._trials() for c in cs]
        try:
            rows.extend(P.project_workflow_costs(self.layout, origin=self.settings.origin))
        except ValueError as e:
            log.warning("workflow costs not projected: %s", e)
        return rows

    def _history(self, project, *args, **kwargs) -> list:
        """One history projection; a corrupt snapshot directory is logged and read as empty, never a console failure."""
        try:
            return list(project(self.layout, self._settings, *args, **kwargs))
        except ValueError as e:
            log.warning("history not projected (%s): %s", getattr(project, "__name__", project), e)
            return []

    def _action_rows(self) -> list[M.OperatorActionRow]:
        return self._history(H.action_rows)

    def _workflow_rows(self) -> list[M.WorkflowRow]:
        return self._history(H.workflow_rows, legacy=P.project_workflows_from_jobs(self.layout, origin=self.settings.origin))

    def _rebuild_rows(self) -> list[M.IndexRebuildRow]:
        return self._history(H.rebuild_rows)

    def _candidate_event_rows(self) -> list[M.CandidateEventRow]:
        return self._history(H.candidate_events)

    # ------------------------------------------------------------------ IndexReader
    def scenarios(self, f: M.ScenarioFilter) -> M.Page[M.ScenarioRow]:
        rows = self._scenario_rows()
        if f.latest_only:
            latest: dict[str, M.ScenarioRow] = {}
            for r in rows:
                if r.scenario_id not in latest or r.version > latest[r.scenario_id].version:
                    latest[r.scenario_id] = r
            rows = list(latest.values())
        return page([r for r in rows if matches(r, f, TEXT_FIELDS["scenarios"])], f, DEFAULT_ORDER["scenarios"])

    def scenario(self, scenario_id: str, version: int | None = None) -> M.ScenarioRow | None:
        try:
            return P.project_scenario(self.layout, scenario_id, version, registry=self._registry(), origin=self.settings.origin)
        except ValueError:
            return None

    def scenario_versions(self, scenario_id: str) -> list[M.ScenarioRow]:
        try:
            return P.project_scenario_versions(self.layout, scenario_id, registry=self._registry(), origin=self.settings.origin)
        except ValueError:
            return []

    def candidates(self, f: M.CandidateFilter) -> M.Page[M.CandidateRow]:
        keys = f.source_key if f.source_key and len(f.source_key) == 1 else None
        rows = self._candidate_rows(keys[0] if keys else None)
        return page([r for r in rows if matches(r, f, TEXT_FIELDS["candidates"])], f, DEFAULT_ORDER["candidates"])

    def funnel(self, source_key: str | None = None) -> list[M.FunnelStep]:
        return R.funnel_steps(self._candidate_rows(source_key))

    def reject_histogram(self, source_key: str | None = None) -> dict[str, int]:
        return R.reject_histogram(self._discovery_rows(source_key), self._candidate_rows(source_key))

    def trials(self, f: M.TrialFilter) -> M.Page[M.TrialRow]:
        rows = [t for t, _ex, _c in self._trials()]
        if f.source:  # `source` lives on the scenario: keep the trials whose scenario has one of the wanted sources
            wanted = {s.scenario_id for s in self._scenario_rows() if s.source in f.source}
            rows = [r for r in rows if r.scenario_id in wanted]
        return page([r for r in rows if matches(r, f, TEXT_FIELDS["trials"])], f, DEFAULT_ORDER["trials"])

    def trial(self, trial_id: str) -> M.TrialRow | None:
        found = self._trial(trial_id)
        return found[0] if found else None

    def exchanges(self, trial_id: str, evaluator: bool) -> list[M.ExchangeRow]:
        found = self._trial(trial_id)
        if not found:
            return []
        rows = sorted(found[1], key=lambda r: r.seq)
        return rows if evaluator else [R.blank_exchange(r) for r in rows]

    def scores(self, f: M.ScoreFilter) -> list[M.ScoreRow]:
        rows = [r for r in self._score_rows() if r.n >= f.min_n and matches(r, f, TEXT_FIELDS["scores"])]
        rows.sort(key=lambda r: (r.scenario_id, r.condition, r.brief_profile, r.model, r.run_config_hash))
        return rows[: f.limit]

    def leaderboard(self, f: M.LeaderboardFilter) -> list[M.LeaderboardRow]:
        return R.compute_leaderboard([t for t, _ex, _c in self._trials()], self._scenario_rows(), f)

    def workflows(self, f: M.WorkflowFilter) -> M.Page[M.WorkflowRow]:
        return page([r for r in self._workflow_rows() if matches(r, f, TEXT_FIELDS["workflows"])], f, DEFAULT_ORDER["workflows"])

    def workflow(self, workflow_id: str) -> M.WorkflowRow | None:
        rows = [r for r in self._workflow_rows() if r.workflow_id == workflow_id]
        return max(rows, key=lambda r: r.started_at) if rows else None

    def costs(self, f: M.CostFilter) -> list[M.CostRow]:
        rows = [c for c in self._cost_rows() if matches(c, f, TEXT_FIELDS["costs"])]
        rows.sort(key=lambda r: (r.at, r.cost_id), reverse=True)
        return rows[: f.limit]

    def spend(self) -> list[M.SpendRow]:
        return R.spend_rows(R.cost_totals(self._cost_rows()), self.settings.spend_cap_usd)

    def operator_actions(self, f: M.PageFilter) -> M.Page[M.OperatorActionRow]:
        return page([r for r in self._action_rows() if matches(r, f, TEXT_FIELDS["operator_actions"])], f, DEFAULT_ORDER["operator_actions"])

    def table(self, name: str, f: M.TableFilter) -> M.Page[dict]:
        if name not in TABLES:
            raise ValueError(f"unknown table {name!r}")
        columns = {c.name for c in TABLES[name].columns}
        unknown = [k for k in f.where if k not in columns]
        if unknown:
            raise ValueError(f"unknown column(s) for {name}: {unknown}")
        if f.order_by and f.order_by not in columns:
            raise ValueError(f"unknown order_by column {f.order_by!r}")
        rows = self._table_rows(name)
        kept = [r for r in rows if all(getattr(r, k) == v for k, v in f.where.items())
                and matches(r, M.PageFilter(q=f.q), TEXT_FIELDS.get(name, ()))]
        paged = page(kept, M.PageFilter(offset=f.offset, limit=f.limit, order_by=f.order_by, desc=f.desc), DEFAULT_ORDER[name])
        return M.Page[dict](items=[jsonable(r) for r in paged.items], total=paged.total, offset=paged.offset, limit=paged.limit)

    def _table_rows(self, name: str) -> list[BaseModel]:
        if name == "scenarios":
            return list(self._scenario_rows())
        if name == "candidates":
            return list(self._candidate_rows())
        if name == "discoveries":
            return list(self._discovery_rows())
        if name == "trials":
            return [t for t, _ex, _c in self._trials()]
        if name == "exchanges":
            return [e for _t, ex, _c in self._trials() for e in ex]
        if name == "costs":
            return list(self._cost_rows())
        if name == "scores":
            return list(self._score_rows())
        if name == "workflows":
            return list(self._workflow_rows())
        if name == "operator_actions":
            return list(self._action_rows())
        if name == "index_rebuilds":
            return list(self._rebuild_rows())
        if name == "candidate_events":
            return list(self._candidate_event_rows())
        return []

    def rebuilds(self) -> list[M.IndexRebuildRow]:
        return self._rebuild_rows()
