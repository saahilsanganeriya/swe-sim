"""`IndexStore`: the writer of the index and its SQL `IndexReader` (interfaces §2).

Contract
- Built from a SQLAlchemy URL (`postgresql+psycopg://...` in production; `sqlite://` in unit tests). `pool_pre_ping`
  is on; the URL is never logged or repr'd.
- `migrate()` runs the Alembic revisions under `swesim/index/migrations` to head inside one transaction;
  `ensure_migrated(settings)` is the worker's start-up entry point (False when disabled; otherwise migrate under
  the schema advisory lock, idempotent, one log line with the head revision).
- Every write is an upsert keyed by the table's primary key (`INSERT ... ON CONFLICT DO UPDATE` on Postgres and
  SQLite): running it twice leaves one row; `updated_at` bumps, `created_at` keeps the first value.
- `advisory_lock(key)` is `pg_advisory_xact_lock(hashtext(key))` held for a transaction on Postgres and a
  process-local lock on SQLite. It replaces `registry.json.lock`, which GCS FUSE cannot honour across instances.
- `rebuild_from_layout()` projects every file into rows first (reads outside the transaction), then truncates every
  table and inserts the projection in one transaction, so readers never see a half-built index. The tables the
  pipeline's files do not describe (workflows: the workflows' own reports and the Temporal reconciler; operator_actions:
  the console's audit; candidate_events: stage/decision receipts; index_rebuilds) are rebuilt from the immutable row
  snapshots under `Layout.history_dir` (`swesim.storage.history`), which every writer publishes *before* its SQL upsert:
  a fresh database rebuilt from the bucket equals the live one. The returned `IndexRebuildRow` carries the row counts
  after the rebuild, the per-table diff against before, and every skipped id with its reason; it is itself snapshotted
  before it is inserted.
- Reads return the row models; invalid input (unknown table, unknown order_by/where column) raises `ValueError`.
"""
from __future__ import annotations

import logging
import threading
import warnings
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from pydantic import BaseModel, SecretStr
from sqlalchemy.pool import StaticPool
from ulid import ULID

from swesim.index import history as H
from swesim.index import models as M
from swesim.index import project as P
from swesim.index import reports as R
from swesim.index import schema as S
from swesim.settings import Settings, get_settings
from swesim.storage.history import persist_row
from swesim.storage.layout import Layout

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
# delete order (children first); the last four are rebuilt from the immutable history snapshots, the rest from the pipeline's files
REBUILT_TABLES: tuple[str, ...] = ("exchanges", "costs", "scores", "trials", "candidates", "discoveries", "scenarios",
                                   "candidate_events", "operator_actions", "workflows", "index_rebuilds")
HISTORY_TABLES: tuple[str, ...] = ("workflows", "operator_actions", "index_rebuilds", "candidate_events")
HISTORY_LOCK = "swesim.index.history-and-rebuild"
BATCH = 500
RANGES: dict[str, tuple[str, str]] = {"started_from": ("started_at", ">="), "started_to": ("started_at", "<="),
                                      "judged_from": ("judged_at", ">="), "judged_to": ("judged_at", "<="),
                                      "at_from": ("at", ">="), "at_to": ("at", "<=")}
CONTROL_FIELDS: frozenset[str] = frozenset({"offset", "limit", "order_by", "desc", "q", "latest_only", "min_n", "where"})
DEFAULT_ORDER: dict[str, str] = {"scenarios": "scenario_id", "candidates": "instance_id", "trials": "started_at", "scores": "scenario_id",
                                 "workflows": "started_at", "costs": "at", "operator_actions": "at", "index_rebuilds": "started_at",
                                 "exchanges": "seq", "discoveries": "discovered_at", "candidate_events": "occurred_at"}
T = TypeVar("T", bound=BaseModel)


class UnsupportedDialect(ValueError):
    """Only PostgreSQL (production) and SQLite (tests) are supported."""


def alembic_config() -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def make_engine(url: str) -> sa.Engine:
    """Engine for the URL; in-memory SQLite shares one connection so every thread sees the same database."""
    backend = sa.make_url(url).get_backend_name()
    if backend not in ("postgresql", "sqlite"):
        raise UnsupportedDialect(f"the index supports postgresql and sqlite, not {backend!r}")
    if backend == "sqlite":
        # SQLite is the unit-test dialect only; it converts Decimal through float and SQLAlchemy says so once per process.
        warnings.filterwarnings("ignore", message=r".*does \*not\* support Decimal objects natively.*")
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
        return sa.create_engine(url, **kwargs)
    return sa.create_engine(url, pool_pre_ping=True)


def _insert_for(dialect: str):
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise UnsupportedDialect(f"the index supports postgresql and sqlite, not {dialect!r}")
    return insert


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


MIGRATION_LOCK_KEY = "swesim.index.schema"


def current_revision(engine: sa.Engine) -> str | None:
    """The Alembic revision the database is at; None before the first migration."""
    if "alembic_version" not in sa.inspect(engine).get_table_names():
        return None
    with engine.connect() as conn:
        return conn.execute(sa.text("select version_num from alembic_version")).scalar_one_or_none()


def ensure_migrated(settings: Settings | None = None) -> bool:
    """Bring the index schema to head at process start (the control worker calls this; Cloud SQL has no laptop path).
    False when the index is disabled. Otherwise runs `migrate()` under the schema advisory lock so concurrent workers
    take turns and the second finds nothing to do; logs one line with the dialect and head revision, never the URL."""
    s = settings or get_settings()
    store = IndexStore.from_settings(s)
    if store is None:
        log.info("index: disabled (SWESIM_DATABASE_URL not set); schema not checked")
        return False
    with store.advisory_lock(MIGRATION_LOCK_KEY):
        before = current_revision(store.engine)
        store.migrate()
        after = current_revision(store.engine)
    log.info("index: schema at %s on %s%s", after, store.engine.dialect.name, "" if before == after else f" (migrated from {before or 'empty'})")
    return True


class IndexStore:
    _local_locks: dict[str, threading.Lock] = {}
    _local_guard = threading.Lock()

    def __init__(self, url: str | SecretStr, *, settings: Settings | None = None):
        raw = url.get_secret_value() if isinstance(url, SecretStr) else url
        self.engine = make_engine(raw)
        self._settings = settings
        self._insert = _insert_for(self.engine.dialect.name)

    def __repr__(self) -> str:  # never the URL
        return f"IndexStore(dialect={self.engine.dialect.name})"

    @property
    def settings(self) -> Settings:
        return self._settings or get_settings()

    @classmethod
    def from_settings(cls, s: Settings) -> IndexStore | None:
        if s.database_url is None:
            return None
        return cls(s.database_url, settings=s)

    def migrate(self) -> None:
        """`alembic upgrade head` on this store's engine, in one transaction."""
        cfg = alembic_config()
        with self.engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")

    # ------------------------------------------------------------------ writes
    def _upsert(self, table: sa.Table, rows: Iterable[BaseModel], conn: sa.Connection | None = None) -> int:
        pk = [c.name for c in table.primary_key.columns]
        now = M.utcnow()
        by_key: dict[tuple, dict] = {}
        for row in rows:
            values = row.model_dump(mode="python")
            values["updated_at"] = now
            by_key[tuple(values[k] for k in pk)] = values  # one statement never touches the same key twice
        values_list = list(by_key.values())
        if not values_list:
            return 0

        def run(c: sa.Connection) -> None:
            for i in range(0, len(values_list), BATCH):
                stmt = self._insert(table).values(values_list[i:i + BATCH])
                set_ = {col.name: getattr(stmt.excluded, col.name) for col in table.columns if col.name not in pk and col.name != "created_at"}
                c.execute(stmt.on_conflict_do_update(index_elements=pk, set_=set_))

        if conn is not None:
            run(conn)
        else:
            with self.engine.begin() as c:
                run(c)
        return len(values_list)

    def upsert_scenario(self, row: M.ScenarioRow) -> None:
        self._upsert(S.scenarios, [row])

    def upsert_scenarios(self, rows: Iterable[M.ScenarioRow]) -> int:
        return self._upsert(S.scenarios, rows)

    def upsert_candidate(self, row: M.CandidateRow) -> None:
        self._upsert(S.candidates, [row])

    def upsert_candidates(self, rows: Iterable[M.CandidateRow]) -> int:
        return self._upsert(S.candidates, rows)

    def upsert_discovery(self, row: M.DiscoveryRow) -> None:
        """Preserve the first immutable observation; replaying its activity cannot rewrite history."""
        with self.locked_transaction(HISTORY_LOCK) as conn:
            stmt = self._insert(S.discoveries).values(**row.model_dump(mode="python"))
            conn.execute(stmt.on_conflict_do_nothing(index_elements=S.PRIMARY_KEYS["discoveries"]))

    def upsert_trial(self, row: M.TrialRow) -> None:
        self._upsert(S.trials, [row])

    def upsert_exchanges(self, rows: Iterable[M.ExchangeRow]) -> int:
        return self._upsert(S.exchanges, rows)

    def upsert_score(self, row: M.ScoreRow) -> None:
        self._upsert(S.scores, [row])

    def upsert_scores(self, rows: Iterable[M.ScoreRow]) -> int:
        return self._upsert(S.scores, rows)

    # ------------------------------------------------------------------ history-backed writes (snapshot first, SQL second)
    def history_layout(self) -> Layout:
        """Where the immutable row snapshots live: the record root (`Settings.workspace`, the bucket mount when hosted)."""
        return Layout(root=self.settings.workspace)

    def _publish(self, row: BaseModel) -> None:
        """The durable half of a history-backed write. Raises before any SQL is touched when the record cannot be written,
        so a row is never visible in the disposable index without its snapshot in the record."""
        persist_row(self.history_layout(), row, self._settings)

    def upsert_workflow(self, row: M.WorkflowRow) -> None:
        """Snapshot the report as made, then let a server-observed terminal state win over a late `running` hook (the same
        precedence `swesim.index.history.choose_workflow` applies when the snapshots are reprojected)."""
        from swesim.index.history import choose_workflow
        self._publish(row)
        with self.locked_transaction(HISTORY_LOCK) as conn:
            data = conn.execute(sa.select(S.workflows).where(S.workflows.c.workflow_id == row.workflow_id,
                                                           S.workflows.c.run_id == row.run_id)).mappings().first()
            chosen = choose_workflow(M.WorkflowRow.model_validate(dict(data)) if data else None, row)
            self._upsert(S.workflows, [chosen], conn)

    def add_cost(self, row: M.CostRow) -> None:
        self._upsert(S.costs, [row])

    def add_costs(self, rows: Iterable[M.CostRow]) -> int:
        return self._upsert(S.costs, rows)

    def add_operator_action(self, row: M.OperatorActionRow) -> None:
        self._publish(row)
        self._upsert(S.operator_actions, [row])

    def upsert_candidate_event(self, row: M.CandidateEventRow) -> None:
        self._publish(row)
        self._upsert(S.candidate_events, [row])

    @contextmanager
    def locked_transaction(self, key: str) -> Iterator[sa.Connection]:
        """One transaction holding the advisory lock on the same connection it writes with (Postgres); the process-local
        lock around a transaction on SQLite."""
        if self.engine.dialect.name == "postgresql":
            with self.engine.begin() as conn:
                conn.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
                yield conn
        else:
            with self.advisory_lock(key):
                with self.engine.begin() as conn:
                    yield conn

    # ------------------------------------------------------------------ locking
    @contextmanager
    def advisory_lock(self, key: str) -> Iterator[None]:
        """Exclusive across every process sharing the database (Postgres); across threads of this process (SQLite)."""
        if self.engine.dialect.name == "postgresql":
            with self.engine.begin() as conn:
                conn.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
                yield
            return
        with IndexStore._local_guard:
            lock = IndexStore._local_locks.setdefault(key, threading.Lock())
        with lock:
            yield

    # ------------------------------------------------------------------ rebuild
    def _counts(self, conn: sa.Connection) -> dict[str, int]:
        return {name: int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()) for name, table in S.TABLES.items()}

    def rebuild_from_layout(self, layout: Layout, registry_path: Path | None = None, *, origin: str | None = None) -> M.IndexRebuildRow:
        settings = self.settings
        origin = origin or settings.origin
        started = M.utcnow()
        registry = P.project_registry(layout, registry_path)
        skipped: dict[str, list[dict]] = {"scenarios": [], "candidates": [], "discoveries": [], "trials": [], "scores": []}
        scenario_rows: list[M.ScenarioRow] = []
        score_rows: list[M.ScoreRow] = []
        for sid in P.scenario_ids(layout, registry):
            try:
                scenario_rows.extend(P.project_scenario_versions(layout, sid, registry=registry, origin=origin, now=started))
            except ValueError as e:
                skipped["scenarios"].append({"id": sid, "reason": str(e)[:300]})
            try:
                score_rows.extend(P.project_scores(layout, sid, origin=origin, now=started))
            except ValueError as e:
                skipped["scores"].append({"id": sid, "reason": str(e)[:300]})
        candidate_rows: list[M.CandidateRow] = []
        for key in P.source_keys(layout):
            try:
                candidate_rows.extend(P.project_candidates(layout, key, registry=registry, origin=origin, now=started))
            except ValueError as e:
                skipped["candidates"].append({"id": key, "reason": str(e)[:300]})
        discovery_rows: list[M.DiscoveryRow] = []
        for key in P.discovery_source_keys(layout):
            try:
                discovery_rows.extend(P.project_discoveries(layout, key, origin=origin))
            except ValueError as e:
                skipped["discoveries"].append({"id": key, "reason": str(e)[:300]})
        trial_rows: list[M.TrialRow] = []
        exchange_rows: list[M.ExchangeRow] = []
        cost_rows: list[M.CostRow] = []
        try:
            cost_rows.extend(P.project_workflow_costs(layout, origin=origin, now=started))
        except ValueError as e:
            skipped["costs"] = [{"id": str(layout.costs_dir()), "reason": str(e)[:300]}]
        for tid in P.trial_ids(layout):
            try:
                t, ex, cs = P.project_trial(layout, tid, origin=origin, now=started)
            except ValueError as e:
                skipped["trials"].append({"id": tid, "reason": str(e)[:300]})
                continue
            trial_rows.append(t)
            exchange_rows.extend(ex)
            cost_rows.extend(cs)
        # The history-backed tables: every snapshot the writers published, reprojected (a corrupt snapshot is a skip, not a guess).
        history_rows: dict[str, list[BaseModel]] = {}
        for name, project in (("workflows", H.workflow_rows), ("operator_actions", H.action_rows),
                              ("index_rebuilds", H.rebuild_rows), ("candidate_events", H.candidate_events)):
            try:
                history_rows[name] = list(project(layout, settings))
            except ValueError as e:
                history_rows[name] = []
                skipped[name] = [{"id": layout.relative(layout.history_dir(name)), "reason": str(e)[:300]}]
        projected = {"scenarios": len(scenario_rows), "candidates": len(candidate_rows), "trials": len(trial_rows), "exchanges": len(exchange_rows),
                     "scores": len(score_rows), "costs": len(cost_rows), "discoveries": len(discovery_rows),
                     **{name: len(rows) for name, rows in history_rows.items()}}
        with self.locked_transaction(HISTORY_LOCK) as conn:
            before = self._counts(conn)
            for name in REBUILT_TABLES:
                conn.execute(sa.delete(S.TABLES[name]))
            self._upsert(S.scenarios, scenario_rows, conn)
            self._upsert(S.candidates, candidate_rows, conn)
            self._upsert(S.discoveries, discovery_rows, conn)
            self._upsert(S.trials, trial_rows, conn)
            self._upsert(S.exchanges, exchange_rows, conn)
            self._upsert(S.scores, score_rows, conn)
            self._upsert(S.costs, cost_rows, conn)
            for name in HISTORY_TABLES:
                self._upsert(S.TABLES[name], history_rows[name], conn)
            after = self._counts(conn)
            after["index_rebuilds"] += 1  # this row
            # The diff answers "did the index drift from the record?": the rebuild log's own growth (+1 per rebuild) is not drift.
            row = M.IndexRebuildRow(
                rebuild_id=str(ULID()), started_at=started, finished_at=M.utcnow(), counts={**after, "skipped": skipped, "projected": projected},
                diff={name: {"before": before[name], "after": after[name], "delta": after[name] - before[name]} for name in after if name != "index_rebuilds"},
                origin=origin, worker_image_digest=settings.worker_image_digest)
            # This rebuild's own row is history too: its snapshot is published (into the layout being rebuilt) before it is
            # inserted, and a failed publication rolls the whole rebuild back, so the next fresh database rebuilds it as well.
            persist_row(layout, row, self._settings)
            self._upsert(S.index_rebuilds, [row], conn)
        return row

    # ------------------------------------------------------------------ reads
    def _rows(self, stmt: sa.Select, model: type[T]) -> list[T]:
        with self.engine.connect() as conn:
            return [model.model_validate(dict(r._mapping)) for r in conn.execute(stmt)]

    def _one(self, stmt: sa.Select, model: type[T]) -> T | None:
        rows = self._rows(stmt.limit(1), model)
        return rows[0] if rows else None

    def _count(self, table: sa.Table, conds: list) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(sa.select(sa.func.count()).select_from(table).where(*conds)).scalar_one())

    @staticmethod
    def _conditions(table: sa.Table, f: BaseModel) -> list:
        conds: list = []
        data = f.model_dump(exclude_none=True)
        for name, value in data.items():
            if name in CONTROL_FIELDS:
                continue
            if name in RANGES:
                col, op = RANGES[name]
                if col in table.c:
                    conds.append(table.c[col] >= value if op == ">=" else table.c[col] <= value)
                continue
            if name not in table.c:
                continue
            conds.append(table.c[name].in_(value) if isinstance(value, list) else table.c[name] == value)
        q = data.get("q")
        if q:
            text_cols = [c for c in table.columns if isinstance(c.type, sa.Text)]
            conds.append(sa.or_(*[c.ilike(f"%{q}%") for c in text_cols]))
        return conds

    @staticmethod
    def _order(table: sa.Table, order_by: str | None, default: str, desc: bool):
        if order_by and order_by not in table.c:
            raise ValueError(f"unknown order_by column {order_by!r}")
        col = table.c[order_by or default]
        return (col.desc() if desc else col.asc()).nulls_last()

    def _page(self, table: sa.Table, model: type[T], f: M.PageFilter, conds: list, default_order: str) -> M.Page[T]:
        stmt = sa.select(table).where(*conds).order_by(self._order(table, f.order_by, default_order, f.desc)).offset(f.offset).limit(f.limit)
        return M.Page[T](items=self._rows(stmt, model), total=self._count(table, conds), offset=f.offset, limit=f.limit)

    def scenarios(self, f: M.ScenarioFilter) -> M.Page[M.ScenarioRow]:
        t = S.scenarios
        conds = self._conditions(t, f)
        if f.latest_only:
            latest = sa.select(t.c.scenario_id, sa.func.max(t.c.version).label("v")).group_by(t.c.scenario_id).subquery()
            conds.append(sa.tuple_(t.c.scenario_id, t.c.version).in_(sa.select(latest.c.scenario_id, latest.c.v)))
        return self._page(t, M.ScenarioRow, f, conds, DEFAULT_ORDER["scenarios"])

    def scenario(self, scenario_id: str, version: int | None = None) -> M.ScenarioRow | None:
        t = S.scenarios
        stmt = sa.select(t).where(t.c.scenario_id == scenario_id)
        if version is not None:
            stmt = stmt.where(t.c.version == version)
        return self._one(stmt.order_by(t.c.version.desc()), M.ScenarioRow)

    def scenario_versions(self, scenario_id: str) -> list[M.ScenarioRow]:
        t = S.scenarios
        return self._rows(sa.select(t).where(t.c.scenario_id == scenario_id).order_by(t.c.version.asc()), M.ScenarioRow)

    def candidates(self, f: M.CandidateFilter) -> M.Page[M.CandidateRow]:
        return self._page(S.candidates, M.CandidateRow, f, self._conditions(S.candidates, f), DEFAULT_ORDER["candidates"])

    def reject_histogram(self, source_key: str | None = None) -> dict[str, int]:
        """Read only indexed evidence; one latest discovery snapshot per source and one gate per rejected candidate."""
        table = S.discoveries
        rank = sa.func.row_number().over(partition_by=table.c.source_key,
                                        order_by=(table.c.discovered_at.desc(), table.c.discovery_id.desc())).label("snapshot_rank")
        latest = sa.select(table, rank).subquery()
        discovery = sa.select(*(latest.c[col.name] for col in table.columns)).where(latest.c.snapshot_rank == 1)
        candidates = sa.select(S.candidates).where(S.candidates.c.status == "rejected", S.candidates.c.gate.is_not(None))
        if source_key is not None:
            discovery = discovery.where(latest.c.source_key == source_key)
            candidates = candidates.where(S.candidates.c.source_key == source_key)
        return R.reject_histogram(self._rows(discovery, M.DiscoveryRow), self._rows(candidates, M.CandidateRow))

    def funnel(self, source_key: str | None = None) -> list[M.FunnelStep]:
        t = S.candidates
        stmt = sa.select(t) if source_key is None else sa.select(t).where(t.c.source_key == source_key)
        return R.funnel_steps(self._rows(stmt, M.CandidateRow))

    def trials(self, f: M.TrialFilter) -> M.Page[M.TrialRow]:
        return self._page(S.trials, M.TrialRow, f, self._conditions(S.trials, f), DEFAULT_ORDER["trials"])

    def trial(self, trial_id: str) -> M.TrialRow | None:
        return self._one(sa.select(S.trials).where(S.trials.c.trial_id == trial_id), M.TrialRow)

    def exchanges(self, trial_id: str, evaluator: bool) -> list[M.ExchangeRow]:
        t = S.exchanges
        rows = self._rows(sa.select(t).where(t.c.trial_id == trial_id).order_by(t.c.seq.asc()), M.ExchangeRow)
        return rows if evaluator else [R.blank_exchange(r) for r in rows]

    def scores(self, f: M.ScoreFilter) -> list[M.ScoreRow]:
        t = S.scores
        conds = self._conditions(t, f) + [t.c.n >= f.min_n]
        stmt = sa.select(t).where(*conds).order_by(t.c.scenario_id, t.c.condition, t.c.brief_profile, t.c.model, t.c.run_config_hash).limit(f.limit)
        return self._rows(stmt, M.ScoreRow)

    def leaderboard(self, f: M.LeaderboardFilter) -> list[M.LeaderboardRow]:
        t = S.trials
        conds = [t.c.purpose == f.purpose, t.c.status == "completed", t.c.outcome_code.in_(sorted(R.AGENT_OUTCOMES))]
        for name in ("condition", "brief_profile", "model", "provider", "scenario_id"):
            values = getattr(f, name)
            if values:
                conds.append(t.c[name].in_(values))
        trials = self._rows(sa.select(t).where(*conds), M.TrialRow)
        scenarios = self._rows(sa.select(S.scenarios).where(S.scenarios.c.validation_status == "valid"), M.ScenarioRow)
        return R.compute_leaderboard(trials, scenarios, f)

    def workflows(self, f: M.WorkflowFilter) -> M.Page[M.WorkflowRow]:
        return self._page(S.workflows, M.WorkflowRow, f, self._conditions(S.workflows, f), DEFAULT_ORDER["workflows"])

    def workflow(self, workflow_id: str) -> M.WorkflowRow | None:
        t = S.workflows
        return self._one(sa.select(t).where(t.c.workflow_id == workflow_id).order_by(t.c.started_at.desc()), M.WorkflowRow)

    def costs(self, f: M.CostFilter) -> list[M.CostRow]:
        t = S.costs
        return self._rows(sa.select(t).where(*self._conditions(t, f)).order_by(t.c.at.desc(), t.c.cost_id.desc()).limit(f.limit), M.CostRow)

    def spend(self) -> list[M.SpendRow]:
        t = S.costs
        stmt = (sa.select(t.c.provider, t.c.model, sa.func.count(), sa.func.sum(t.c.input_tokens), sa.func.sum(t.c.output_tokens), sa.func.sum(t.c.usd))
                .group_by(t.c.provider, t.c.model))
        with self.engine.connect() as conn:
            totals = [(p, m, int(n), int(i or 0), int(o or 0), P.dec(u) or Decimal(0)) for p, m, n, i, o, u in conn.execute(stmt)]
        totals.sort(key=lambda r: (-r[5], r[0], r[1]))
        return R.spend_rows(totals, self.settings.spend_cap_usd)

    def operator_actions(self, f: M.PageFilter) -> M.Page[M.OperatorActionRow]:
        return self._page(S.operator_actions, M.OperatorActionRow, f, self._conditions(S.operator_actions, f), DEFAULT_ORDER["operator_actions"])

    def table(self, name: str, f: M.TableFilter) -> M.Page[dict]:
        if name not in S.TABLES:
            raise ValueError(f"unknown table {name!r}")
        t = S.TABLES[name]
        unknown = [k for k in f.where if k not in t.c]
        if unknown:
            raise ValueError(f"unknown column(s) for {name}: {unknown}")
        conds = [t.c[k].is_(None) if v is None else t.c[k] == v for k, v in f.where.items()]
        if f.q:
            conds.append(sa.or_(*[c.ilike(f"%{f.q}%") for c in t.columns if isinstance(c.type, sa.Text)]))
        stmt = sa.select(t).where(*conds).order_by(self._order(t, f.order_by, DEFAULT_ORDER[name], f.desc)).offset(f.offset).limit(f.limit)
        with self.engine.connect() as conn:
            items = [{k: jsonable(v) for k, v in r._mapping.items()} for r in conn.execute(stmt)]
        return M.Page[dict](items=items, total=self._count(t, conds), offset=f.offset, limit=f.limit)

    def rebuilds(self) -> list[M.IndexRebuildRow]:
        t = S.index_rebuilds
        return self._rows(sa.select(t).order_by(t.c.started_at.desc()), M.IndexRebuildRow)
