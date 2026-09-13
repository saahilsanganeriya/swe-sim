"""Postgres integration: migrate, upsert twice, advisory lock across connections, rebuild. Runs only when
SWESIM_TEST_DATABASE_URL points at a disposable database (e.g. `docker run -d --name swesim-pg -p 5433:5432
-e POSTGRES_PASSWORD=test postgres:16`, then `postgresql+psycopg://postgres:test@localhost:5433/postgres`)."""
from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa

from swesim.index import models as M
from swesim.index import schema as S
from swesim.index.store import IndexStore
from swesim.settings import Settings

try:
    from test_index_project import HAIKU, SID, Record, build_record
except ImportError:  # pragma: no cover
    from tests.test_index_project import HAIKU, SID, Record, build_record

URL = os.environ.get("SWESIM_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="SWESIM_TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def record(tmp_path_factory) -> Record:
    return build_record(tmp_path_factory.mktemp("record"))


@pytest.fixture
def store(tmp_path) -> IndexStore:
    settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_SPEND_CAP_USD": "100", "SWESIM_ORIGIN": "cloudrun"})
    s = IndexStore(URL, settings=settings)
    with s.engine.begin() as conn:  # a disposable database: start from nothing every time
        for table in reversed(S.metadata.sorted_tables):
            conn.execute(sa.text(f'DROP TABLE IF EXISTS "{table.name}" CASCADE'))
        conn.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
    s.migrate()
    return s


def test_migrate_upsert_and_read_on_postgres(store: IndexStore):
    assert store.engine.dialect.name == "postgresql"
    insp = sa.inspect(store.engine)
    assert set(S.TABLES) <= set(insp.get_table_names())
    usd = {c["name"]: c["type"] for c in insp.get_columns("costs")}["usd"]
    assert (usd.precision, usd.scale) == (12, 6)
    assert {c["name"]: c["type"] for c in insp.get_columns("trials")}["started_at"].timezone is True
    row = M.TrialRow(trial_id="1" * 24, scenario_id=SID, version=2, spec_hash="h" * 64, condition="interactive", brief_profile="withheld", model=HAIKU,
                     provider="anthropic", run_config_hash="r" * 64, attempt_index=0, purpose="comparison", status="completed", reward=Decimal("0.123456"),
                     started_at=datetime(2026, 9, 13, 9, 59, tzinfo=UTC), origin="cloudrun")
    store.upsert_trial(row)
    store.upsert_trial(row.model_copy(update={"reward": Decimal("1")}))
    got = store.trials(M.TrialFilter())
    assert got.total == 1 and got.items[0].reward == Decimal("1") and got.items[0].started_at == row.started_at
    store.migrate()  # idempotent


def test_advisory_lock_is_exclusive_across_connections(store: IndexStore):
    order: list[str] = []
    held = threading.Event()

    def holder():
        with store.advisory_lock("registry.json"):
            order.append("a-in")
            held.set()
            time.sleep(0.5)
            order.append("a-out")

    def waiter():
        held.wait(5)
        with store.advisory_lock("registry.json"):
            order.append("b-in")

    a, b = threading.Thread(target=holder), threading.Thread(target=waiter)
    a.start()
    b.start()
    a.join(10)
    b.join(10)
    assert order == ["a-in", "a-out", "b-in"]


def test_rebuild_from_files_on_postgres(store: IndexStore, record: Record):
    first = store.rebuild_from_layout(record.layout, record.registry_path)
    assert first.counts["trials"] == len(record.tids) - 1 and first.counts["scenarios"] == 5 and first.diff["trials"]["before"] == 0
    second = store.rebuild_from_layout(record.layout, record.registry_path)
    assert second.diff["trials"]["delta"] == 0 and [r.rebuild_id for r in store.rebuilds()] == [second.rebuild_id, first.rebuild_id]
    assert store.trial(record.tids["done"]).cost_usd == Decimal("0.03") and store.leaderboard(M.LeaderboardFilter(purpose="calibration"))[0].n == 2
    assert store.spend()[0].cap_usd == Decimal("100")
