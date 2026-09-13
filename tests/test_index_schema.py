"""Index data model (interfaces §2): one Pydantic row model per table, one SQLAlchemy table per model, agreeing field
for field; money is Decimal / numeric(12,6), rates numeric(8,6), times tz-aware timestamptz; Alembic revision
0001_index_v1 creates exactly the metadata and is idempotent."""
from __future__ import annotations

import types
import typing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from swesim.index import models as M
from swesim.index import schema as S
from swesim.index.store import IndexStore, alembic_config

PROVENANCE = ("created_at", "updated_at", "workflow_id", "run_id", "worker_image_digest", "origin")
EXPECTED_PK = {
    "scenarios": ("scenario_id", "version"), "candidates": ("instance_id", "source_key"), "trials": ("trial_id",),
    "exchanges": ("trial_id", "seq"), "scores": ("scenario_id", "spec_hash", "condition", "brief_profile", "model", "run_config_hash"),
    "workflows": ("workflow_id", "run_id"), "costs": ("cost_id",), "operator_actions": ("action_id",), "index_rebuilds": ("rebuild_id",),
}
NOW = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)


def _optional(annotation) -> bool:
    origin = typing.get_origin(annotation)
    return origin in (typing.Union, types.UnionType) and type(None) in typing.get_args(annotation)


def test_every_row_model_has_a_table_with_the_same_columns_and_nullability():
    assert set(S.TABLES) == set(EXPECTED_PK)
    for name, table in S.TABLES.items():
        model = S.ROW_MODELS[name]
        fields = model.model_fields
        assert set(fields) == {c.name for c in table.columns}, name
        assert set(PROVENANCE) <= set(fields), name
        for field, info in fields.items():
            col = table.c[field]
            assert col.nullable == (_optional(info.annotation) and not col.primary_key), f"{name}.{field}"
        assert tuple(c.name for c in table.primary_key.columns) == EXPECTED_PK[name], name


def test_column_types_follow_the_data_model_bar():
    for name, table in S.TABLES.items():
        model = S.ROW_MODELS[name]
        for field, info in model.model_fields.items():
            col = table.c[field]
            base = next((a for a in typing.get_args(info.annotation) if a is not type(None)), info.annotation) if _optional(info.annotation) else info.annotation
            if base is Decimal:
                assert isinstance(col.type, sa.Numeric), f"{name}.{field}"
                if field in ("usd", "cost_usd"):
                    assert (col.type.precision, col.type.scale) == (12, 6), f"{name}.{field}"
                else:
                    assert (col.type.precision, col.type.scale) == (8, 6), f"{name}.{field}"
            elif base is datetime or getattr(base, "__name__", "") == "AwareDatetime":
                assert isinstance(col.type, S.TZDateTime) and col.type.impl.timezone is True, f"{name}.{field}"
            elif typing.get_origin(base) in (dict, list) or base in (dict, list):
                assert isinstance(col.type, sa.JSON), f"{name}.{field}"
            elif base is bool:
                assert isinstance(col.type, sa.Boolean), f"{name}.{field}"
            elif base is int:
                assert isinstance(col.type, sa.Integer), f"{name}.{field}"
            else:
                assert isinstance(col.type, sa.Text), f"{name}.{field}"


def _trial(**over) -> M.TrialRow:
    base = dict(trial_id="a" * 24, scenario_id="s-1", version=1, spec_hash="h" * 64, condition="interactive", brief_profile="withheld",
                model="anthropic/claude-haiku-4-5-20251001", provider="anthropic", run_config_hash="r" * 64, attempt_index=0,
                purpose="comparison", status="completed", origin="laptop")
    return M.TrialRow(**{**base, **over})


def test_rows_are_strict_money_is_decimal_and_times_are_aware():
    row = _trial(reward=0.5, cost_usd="0.012345", started_at="2026-09-13T08:00:00+00:00")
    assert row.reward == Decimal("0.5") and isinstance(row.reward, Decimal)
    assert row.cost_usd == Decimal("0.012345") and row.started_at == NOW
    assert row.created_at.tzinfo is not None and row.updated_at.tzinfo is not None
    with pytest.raises(ValidationError):
        _trial(started_at=datetime(2026, 9, 13, 8, 0))  # naive
    with pytest.raises(ValidationError):
        _trial(extra="x")
    with pytest.raises(ValidationError):
        _trial(status="done")
    with pytest.raises(ValidationError):
        M.TrialRow(**{k: v for k, v in _trial().model_dump().items() if k != "origin"})  # provenance origin is required
    with pytest.raises(ValidationError):
        _trial(origin="somewhere")


def test_page_is_generic_and_filters_bound_the_page_size():
    page = M.Page[M.TrialRow](items=[_trial()], total=7, offset=0, limit=50)
    assert page.total == 7 and page.items[0].trial_id == "a" * 24
    assert M.PageFilter().limit <= 500 and M.PageFilter(limit=500).limit == 500
    with pytest.raises(ValidationError):
        M.PageFilter(limit=501)
    with pytest.raises(ValidationError):
        M.PageFilter(offset=-1)
    assert M.LeaderboardFilter().purpose == "comparison" and M.LeaderboardFilter().include_saturated is False
    with pytest.raises(ValidationError):
        M.TrialFilter(model="anthropic/x")  # equality filters are lists
    f = M.TrialFilter(model=["anthropic/x"], status=["completed", "running"])
    assert f.model == ["anthropic/x"] and f.offset == 0
    assert M.FunnelStep(stage="judged", kept=1, rejected=2, error=0, pending=3).kept == 1


def test_migration_creates_the_metadata_exactly_and_is_idempotent(tmp_path: Path):
    store = IndexStore(f"sqlite:///{tmp_path / 'index.db'}")
    store.migrate()
    store.migrate()  # a second upgrade is a no-op
    insp = sa.inspect(store.engine)
    assert set(S.TABLES) <= set(insp.get_table_names()) and "alembic_version" in insp.get_table_names()
    with store.engine.connect() as conn:
        assert conn.execute(sa.text("select version_num from alembic_version")).scalar_one() == "0001_index_v1"
        diff = compare_metadata(MigrationContext.configure(conn), S.metadata)
    assert diff == [], diff
    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == ["0001_index_v1"]
