"""SQLAlchemy 2.0 metadata of the index: one `Table` per row model, derived from the model so the two cannot drift.

Contract
- `TABLES[name]` is the table for `ROW_MODELS[name]`; columns are the model's fields in order, provenance last.
- Types: `str`/Literal -> Text; `int` -> Integer; `bool` -> Boolean; `Decimal` -> Numeric(12,6) for money
  (`usd`, `cost_usd`) and Numeric(8,6) for rates (everything else Decimal); datetimes -> `TZDateTime`
  (TIMESTAMP WITH TIME ZONE; UTC on the way in, tz-aware on the way out, also on SQLite); dict/list -> JSON.
  A Decimal column outside those two lists is a programming error and fails at import.
- Nullability follows the annotation (`X | None`); primary-key columns are never nullable.
- The Alembic revision under `migrations/` is generated from this metadata; a test asserts they still agree.
"""
from __future__ import annotations

import types
import typing
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.types import TypeDecorator

from swesim.index import models as M

MONEY_COLUMNS: frozenset[str] = frozenset({"usd", "cost_usd"})
RATE_COLUMNS: frozenset[str] = frozenset({"reward", "pass_at_k", "pass_pow_k", "mean_reward"})
PROVENANCE_COLUMNS: tuple[str, ...] = tuple(M.Provenance.model_fields)


class TZDateTime(TypeDecorator):
    """timestamptz that is always tz-aware in Python. Values are normalised to UTC before binding (SQLite stores no
    offset, so the stored wall-clock is UTC by construction) and re-labelled UTC when a dialect returns them naive."""

    impl = sa.TIMESTAMP(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("index timestamps must be tz-aware datetimes")
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _unwrap(annotation) -> tuple[object, bool]:
    """(base annotation, nullable)"""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
        raise TypeError(f"unsupported union {annotation}")
    return annotation, False


def column_type(name: str, base) -> sa.types.TypeEngine:
    origin = typing.get_origin(base)
    if origin is typing.Literal:
        return sa.Text()
    if base is str:
        return sa.Text()
    if base is bool:
        return sa.Boolean()
    if base is int:
        return sa.Integer()
    if base is Decimal:
        if name in MONEY_COLUMNS:
            return sa.Numeric(12, 6)
        if name in RATE_COLUMNS:
            return sa.Numeric(8, 6)
        raise TypeError(f"Decimal column {name!r} is neither money nor a rate; add it to the schema's lists")
    if base is datetime or getattr(base, "__name__", "") == "AwareDatetime":
        return TZDateTime()
    if base in (dict, list) or origin in (dict, list):
        return sa.JSON()
    raise TypeError(f"no column type for {name!r}: {base!r}")


MAX_IDENTIFIER = 63  # PostgreSQL truncates longer names silently; SQLAlchemy refuses them at DDL time


def table_from_model(name: str, model: type[BaseModel], pk: tuple[str, ...], metadata: sa.MetaData, *,
                     indexes: dict[str, tuple[str, ...]] | None = None) -> sa.Table:
    """`indexes` maps an explicit index name (kept under 63 characters) to its columns."""
    # Column order: primary key, then the entity's own fields, then the remaining provenance columns.
    entity = [f for f in model.model_fields if f not in PROVENANCE_COLUMNS and f not in pk]
    provenance = [f for f in PROVENANCE_COLUMNS if f not in pk]
    columns: list[sa.Column] = []
    for field in [*pk, *entity, *provenance]:
        base, nullable = _unwrap(model.model_fields[field].annotation)
        is_pk = field in pk
        if is_pk and nullable:
            raise TypeError(f"primary key column {name}.{field} must not be optional")
        columns.append(sa.Column(field, column_type(field, base), primary_key=is_pk, nullable=nullable and not is_pk))
    table = sa.Table(name, metadata, *columns)
    for index_name, cols in (indexes or {}).items():
        if len(index_name) > MAX_IDENTIFIER:
            raise ValueError(f"index name {index_name!r} exceeds {MAX_IDENTIFIER} characters")
        sa.Index(index_name, *[table.c[c] for c in cols])
    return table


metadata = sa.MetaData()

ROW_MODELS: dict[str, type[BaseModel]] = {
    "scenarios": M.ScenarioRow, "candidates": M.CandidateRow, "trials": M.TrialRow, "exchanges": M.ExchangeRow,
    "scores": M.ScoreRow, "workflows": M.WorkflowRow, "costs": M.CostRow, "operator_actions": M.OperatorActionRow,
    "index_rebuilds": M.IndexRebuildRow, "discoveries": M.DiscoveryRow,
}
PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "scenarios": ("scenario_id", "version"), "candidates": ("instance_id", "source_key"), "trials": ("trial_id",),
    "exchanges": ("trial_id", "seq"), "scores": ("scenario_id", "spec_hash", "condition", "brief_profile", "model", "run_config_hash"),
    "workflows": ("workflow_id", "run_id"), "costs": ("cost_id",), "operator_actions": ("action_id",), "index_rebuilds": ("rebuild_id",),
    "discoveries": ("source_key", "discovery_id"),
}
INDEXES: dict[str, dict[str, tuple[str, ...]]] = {
    "scenarios": {"ix_scenarios_validation_status": ("validation_status",), "ix_scenarios_source": ("source",)},
    "candidates": {"ix_candidates_source_key": ("source_key",), "ix_candidates_stage_status": ("stage", "status"), "ix_candidates_scenario_id": ("scenario_id",)},
    "trials": {"ix_trials_scenario_id": ("scenario_id",), "ix_trials_model": ("model",), "ix_trials_status": ("status",), "ix_trials_started_at": ("started_at",),
               # the score group key (interfaces §2 `scores` pk minus scenario_id): what the leaderboard and score pages join on
               "ix_trials_lineage": ("spec_hash", "condition", "brief_profile", "model", "run_config_hash")},
    "exchanges": {},
    "scores": {"ix_scores_scenario_id": ("scenario_id",)},
    "workflows": {"ix_workflows_type_status": ("type", "status"), "ix_workflows_started_at": ("started_at",), "ix_workflows_parent_workflow_id": ("parent_workflow_id",)},
    "costs": {"ix_costs_trial_id": ("trial_id",), "ix_costs_workflow_id": ("workflow_id",), "ix_costs_provider_model": ("provider", "model"), "ix_costs_at": ("at",)},
    "operator_actions": {"ix_operator_actions_at": ("at",)},
    "index_rebuilds": {"ix_index_rebuilds_started_at": ("started_at",)},
    "discoveries": {"ix_discoveries_source_observed": ("source_key", "discovered_at")},
}

TABLES: dict[str, sa.Table] = {
    name: table_from_model(name, model, PRIMARY_KEYS[name], metadata, indexes=INDEXES[name]) for name, model in ROW_MODELS.items()
}
scenarios, candidates, trials, exchanges, scores, workflows, costs, operator_actions, index_rebuilds = (
    TABLES["scenarios"], TABLES["candidates"], TABLES["trials"], TABLES["exchanges"], TABLES["scores"], TABLES["workflows"],
    TABLES["costs"], TABLES["operator_actions"], TABLES["index_rebuilds"])
discoveries = TABLES["discoveries"]
