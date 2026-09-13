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


def table_from_model(name: str, model: type[BaseModel], pk: tuple[str, ...], metadata: sa.MetaData, *,
                     indexes: tuple[tuple[str, ...], ...] = ()) -> sa.Table:
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
    for cols in indexes:
        sa.Index(f"ix_{name}_{'_'.join(cols)}", *[table.c[c] for c in cols])
    return table


metadata = sa.MetaData()

ROW_MODELS: dict[str, type[BaseModel]] = {
    "scenarios": M.ScenarioRow, "candidates": M.CandidateRow, "trials": M.TrialRow, "exchanges": M.ExchangeRow,
    "scores": M.ScoreRow, "workflows": M.WorkflowRow, "costs": M.CostRow, "operator_actions": M.OperatorActionRow,
    "index_rebuilds": M.IndexRebuildRow,
}
PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "scenarios": ("scenario_id", "version"), "candidates": ("instance_id", "source_key"), "trials": ("trial_id",),
    "exchanges": ("trial_id", "seq"), "scores": ("scenario_id", "spec_hash", "condition", "brief_profile", "model", "run_config_hash"),
    "workflows": ("workflow_id", "run_id"), "costs": ("cost_id",), "operator_actions": ("action_id",), "index_rebuilds": ("rebuild_id",),
}
INDEXES: dict[str, tuple[tuple[str, ...], ...]] = {
    "scenarios": (("validation_status",), ("source",)),
    "candidates": (("source_key",), ("stage", "status"), ("scenario_id",)),
    "trials": (("scenario_id",), ("model",), ("status",), ("started_at",), ("spec_hash", "condition", "brief_profile", "model", "run_config_hash")),
    "exchanges": (),
    "scores": (("scenario_id",),),
    "workflows": (("type", "status"), ("started_at",), ("parent_workflow_id",)),
    "costs": (("trial_id",), ("workflow_id",), ("provider", "model"), ("at",)),
    "operator_actions": (("at",),),
    "index_rebuilds": (("started_at",),),
}

TABLES: dict[str, sa.Table] = {
    name: table_from_model(name, model, PRIMARY_KEYS[name], metadata, indexes=INDEXES[name]) for name, model in ROW_MODELS.items()
}
scenarios, candidates, trials, exchanges, scores, workflows, costs, operator_actions, index_rebuilds = (
    TABLES["scenarios"], TABLES["candidates"], TABLES["trials"], TABLES["exchanges"], TABLES["scores"], TABLES["workflows"],
    TABLES["costs"], TABLES["operator_actions"], TABLES["index_rebuilds"])
