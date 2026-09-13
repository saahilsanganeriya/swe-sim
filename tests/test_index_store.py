"""IndexStore on SQLite: idempotent upserts, the SQL IndexReader, rebuild-from-files with counts and diff, the
process-local advisory lock. Postgres specifics are in tests/test_index_postgres.py."""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from swesim.index import models as M
from swesim.index.reads import IndexReader
from swesim.index.store import IndexStore, UnsupportedDialect
from swesim.settings import Settings

try:
    from test_index_project import GPT, HAIKU, SAT, SID, Record, build_record
except ImportError:  # pragma: no cover
    from tests.test_index_project import GPT, HAIKU, SAT, SID, Record, build_record


@pytest.fixture(scope="module")
def record(tmp_path_factory) -> Record:
    return build_record(tmp_path_factory.mktemp("record"))


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_SPEND_CAP_USD": "100", "SWESIM_ORIGIN": "laptop",
                              "SWESIM_WORKER_IMAGE_DIGEST": "sha256:" + "1" * 64})


@pytest.fixture
def store(settings: Settings) -> IndexStore:
    s = IndexStore("sqlite://", settings=settings)
    s.migrate()
    return s


@pytest.fixture
def rebuilt(store: IndexStore, record: Record) -> tuple[IndexStore, M.IndexRebuildRow]:
    return store, store.rebuild_from_layout(record.layout, record.registry_path)


def _trial(**over) -> M.TrialRow:
    base = dict(trial_id="1" * 24, scenario_id=SID, version=2, spec_hash="h" * 64, condition="interactive", brief_profile="withheld", model=HAIKU,
                provider="anthropic", run_config_hash="r" * 64, attempt_index=0, purpose="comparison", status="completed", origin="laptop")
    return M.TrialRow(**{**base, **over})


def test_store_never_shows_its_url_and_rejects_other_dialects(settings: Settings):
    s = IndexStore("sqlite:///:memory:", settings=settings)
    assert "sqlite" in repr(s) and "memory" not in repr(s) and isinstance(s, IndexReader)
    assert IndexStore.from_settings(settings) is None  # index disabled
    with pytest.raises(UnsupportedDialect):
        IndexStore("mysql+pymysql://u:p@h/db")


def test_ensure_migrated_is_the_workers_startup_step(tmp_path, caplog):
    import logging

    from swesim.index import ensure_migrated

    disabled = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env")})
    assert ensure_migrated(disabled) is False
    url = f"sqlite:///{tmp_path / 'index.db'}"
    enabled = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_DATABASE_URL": url})
    with caplog.at_level(logging.INFO, logger="swesim.index.store"):
        assert ensure_migrated(enabled) is True
        assert ensure_migrated(enabled) is True  # idempotent: nothing to do the second time
    lines = [r.getMessage() for r in caplog.records if r.name == "swesim.index.store"]
    assert len(lines) == 2 and all("0003_measurement_cohorts" in line for line in lines) and "migrated from empty" in lines[0] and "migrated" not in lines[1]
    assert not any(url in line or "index.db" in line for line in lines)
    assert IndexStore(url).trials(M.TrialFilter()).total == 0  # the tables exist


def test_upserts_are_idempotent_on_the_primary_key(store: IndexStore):
    first = _trial(reward=Decimal("0.5"), success=False, outcome_code=2)
    store.upsert_trial(first)
    store.upsert_trial(first)
    page = store.trials(M.TrialFilter())
    assert page.total == 1 and page.items[0].reward == Decimal("0.5")
    created, updated = page.items[0].created_at, page.items[0].updated_at
    store.upsert_trial(_trial(reward=Decimal("1"), success=True, outcome_code=1))
    row = store.trial("1" * 24)
    assert row is not None and row.reward == Decimal("1") and row.success is True and row.created_at == created and row.updated_at >= updated
    assert store.trials(M.TrialFilter()).total == 1
    # one statement with the same key twice keeps the last value
    store.upsert_exchanges([M.ExchangeRow(trial_id="1" * 24, seq=0, role="agent", exchange="ask", ts=None, request_id=None, patience_after=None,
                                          text_public="a", disclosed_fact_ids=[], regenerated=None, redacted_terms=[], audit=None, origin="laptop"),
                            M.ExchangeRow(trial_id="1" * 24, seq=0, role="agent", exchange="ask", ts=None, request_id=None, patience_after=None,
                                          text_public="b", disclosed_fact_ids=[], regenerated=None, redacted_terms=[], audit=None, origin="laptop")])
    assert [e.text_public for e in store.exchanges("1" * 24, evaluator=True)] == ["b"]


def test_rebuild_reprojects_every_file_and_reports_counts_and_skips(rebuilt, record: Record):
    store, row = rebuilt
    assert row.counts["scenarios"] == 5 and row.counts["candidates"] == 5 and row.counts["scores"] == 3 and row.counts["exchanges"] == 6 and row.counts["costs"] == 5
    assert row.counts["trials"] == len(record.tids) - 1  # every attempt but the status.json-only one
    assert [s["id"] for s in row.counts["skipped"]["trials"]] == [record.tids["stale-running"]]
    assert row.counts["skipped"]["scenarios"] == [] and row.counts["projected"]["trials"] == row.counts["trials"]
    assert row.diff["trials"] == {"before": 0, "after": row.counts["trials"], "delta": row.counts["trials"]}
    assert row.origin == "laptop" and row.worker_image_digest == "sha256:" + "1" * 64 and row.finished_at >= row.started_at
    assert [r.rebuild_id for r in store.rebuilds()] == [row.rebuild_id]


def test_rebuild_removes_rows_the_files_no_longer_have_and_keeps_activity_fed_tables(rebuilt, record: Record):
    store, first = rebuilt
    store.upsert_trial(_trial(trial_id="2" * 24))  # a row no file backs
    wf = M.WorkflowRow(workflow_id="run-1", run_id="r1", type="RunTasksWorkflow", parent_workflow_id=None, status="running", params_summary={"k": 3},
                       failure=None, started_at=datetime(2026, 9, 13, tzinfo=UTC), closed_at=None, task_queue="swesim-authoring", cost_usd=None,
                       search_attributes={}, origin="cloudrun")
    store.upsert_workflow(wf)
    store.add_cost(M.CostRow(cost_id="01J8Z0000000000000000JUDGE", trial_id=None, workflow_id="run-1", role="judge", provider="openai", model=GPT,
                             input_tokens=1, output_tokens=1, cached_tokens=None, usd=Decimal("0.5"), price_source="x", at=datetime(2026, 9, 13, tzinfo=UTC), origin="cloudrun"))
    second = store.rebuild_from_layout(record.layout, record.registry_path, origin="vm-systemd")
    assert store.trial("2" * 24) is None and second.diff["trials"]["delta"] == -1
    assert store.workflow("run-1") == wf.model_copy(update={"updated_at": store.workflow("run-1").updated_at, "created_at": store.workflow("run-1").created_at})
    # costs are file-backed: the row no file backs is gone, the workflow cost file's row is back
    ids = {c.cost_id for c in store.costs(M.CostFilter())}
    assert "01J8Z0000000000000000JUDGE" not in ids and "01J8Z0000000000000000JUDG1" in ids and len(ids) == 5 and second.diff["costs"]["delta"] == -1
    assert store.trial(record.tids["done"]).origin == "vm-systemd"  # the rebuild's origin labels rows whose files carry none
    assert [r.rebuild_id for r in store.rebuilds()] == [second.rebuild_id, first.rebuild_id]


def test_sql_reader_filters_pages_and_orders(rebuilt, record: Record):
    store, _ = rebuilt
    completed = store.trials(M.TrialFilter(status=["completed"], order_by="trial_id", desc=False))
    assert completed.total == 5 and [t.trial_id for t in completed.items] == sorted(t.trial_id for t in completed.items)
    paged = store.trials(M.TrialFilter(status=["completed"], limit=2, offset=4, order_by="trial_id", desc=False))
    assert (paged.total, len(paged.items), paged.offset, paged.limit) == (5, 1, 4, 2)
    assert store.trials(M.TrialFilter(scenario_id=[SID], purpose=["calibration"], success=True)).total == 1
    assert store.trials(M.TrialFilter(q=record.tids["done"][:10])).items[0].trial_id == record.tids["done"]
    assert store.trials(M.TrialFilter(started_from=datetime(2026, 9, 13, 11, tzinfo=UTC))).total == 1  # only the running attempt started at 12:00
    assert store.trials(M.TrialFilter(launcher=["cloudrun"])).items[0].status == "running"
    default = store.trials(M.TrialFilter())
    assert default.total == len(record.tids) - 1 and default.items[0].started_at is not None and default.items[-1].started_at is None
    with pytest.raises(ValueError):
        store.trials(M.TrialFilter(order_by="bogus"))
    row = store.trial(record.tids["done"])
    assert row.reward == Decimal("1") and row.cost_usd == Decimal("0.03") and row.started_at == datetime(2026, 9, 13, 9, 59, tzinfo=UTC)
    assert store.trial("0" * 24) is None


def test_sql_reader_scenarios_candidates_scores_and_exchanges(rebuilt, record: Record):
    store, _ = rebuilt
    assert store.scenario(SID).version == 2 and store.scenario(SID, 1).validation_status == "archived" and store.scenario("nope") is None
    assert [s.version for s in store.scenario_versions(SID)] == [1, 2]
    latest = store.scenarios(M.ScenarioFilter(latest_only=True, order_by="scenario_id", desc=False))
    assert [s.scenario_id for s in latest.items] == ["broken-scenario", SID, SAT, "second-scenario"] and latest.total == 4
    assert store.scenarios(M.ScenarioFilter(validation_status=["valid"])).total == 2 and store.scenarios(M.ScenarioFilter(q="broken")).total == 1
    assert store.candidates(M.CandidateFilter(status=["rejected"])).total == 2
    assert store.candidates(M.CandidateFilter(source_key=["swebench-annotated"], stage=["registered"])).items[0].instance_id == SID
    funnel = {s.stage: s for s in store.funnel()}
    assert (funnel["mined"].kept, funnel["judged"].rejected, funnel["probed"].error, funnel["registered"].kept) == (4, 1, 1, 1)
    assert store.funnel("github-repo--fifth__lib")[2].error == 1
    scores = store.scores(M.ScoreFilter(scenario_id=[SID]))
    assert len(scores) == 2 and scores[0].mean_reward is not None and isinstance(scores[0].mean_reward, Decimal)
    assert store.scores(M.ScoreFilter(min_n=2)) == [s for s in scores if s.n >= 2]
    evaluator = store.exchanges(record.tids["done"], evaluator=True)
    public = store.exchanges(record.tids["done"], evaluator=False)
    assert [e.seq for e in evaluator] == [0, 1, 2, 3, 4, 5] and evaluator[1].disclosed_fact_ids == ["f01"] and evaluator[0].audit is not None
    assert all((p.disclosed_fact_ids, p.redacted_terms, p.regenerated, p.audit) == ([], [], None, None) for p in public)
    assert [p.text_public for p in public] == [e.text_public for e in evaluator]
    assert store.exchanges("0" * 24, evaluator=True) == []


def test_sql_reader_leaderboard_spend_costs_and_tables(rebuilt, record: Record):
    store, _ = rebuilt
    board = store.leaderboard(M.LeaderboardFilter(purpose="calibration"))
    assert len(board) == 1 and (board[0].n, board[0].success_count, board[0].pass_rate) == (2, 1, Decimal("0.5"))
    assert store.leaderboard(M.LeaderboardFilter(purpose="calibration", include_saturated=True))[0].n == 3
    spend = store.spend()
    assert [(s.provider, s.model, s.usd, s.records) for s in spend[:2]] == [("openai", GPT, Decimal("0.025"), 2), ("anthropic", HAIKU, Decimal("0.01"), 1)]
    assert [(s.model, s.usd, s.records) for s in spend[2:]] == [("anthropic/unknown", Decimal(0), 1), ("openai/unknown", Decimal(0), 1)]  # unpriced: visible, no money
    assert spend[0].cap_usd == Decimal("100") and spend[0].share_of_cap == Decimal("0.000250") and spend[0].input_tokens == 600
    assert [c.role for c in store.costs(M.CostFilter(trial_id=[record.tids["done"]]))] == ["stakeholder", "judge", "solver"]  # newest first
    assert [c.role for c in store.costs(M.CostFilter(provider=["openai"], at_from=datetime(2026, 9, 13, 10, 1, tzinfo=UTC)))] == ["stakeholder", "judge"]
    assert [c.role for c in store.costs(M.CostFilter(workflow_id=["create-1"]))] == ["drafter", "judge"]
    page = store.table("trials", M.TableFilter(where={"status": "completed"}, order_by="trial_id", desc=False, limit=2))
    assert page.total == 5 and len(page.items) == 2 and isinstance(page.items[0], dict) and page.items[0]["status"] == "completed"
    done = store.table("trials", M.TableFilter(where={"trial_id": record.tids["done"]})).items[0]
    assert done["reward"] == "1.000000" and done["started_at"] == "2026-09-13T09:59:00+00:00"  # JSON-safe values for the browser
    assert store.table("costs", M.TableFilter(where={"trial_id": None})).total == 2 and store.table("costs", M.TableFilter(q="openai")).total == 3
    assert store.table("costs", M.TableFilter(where={"usd": None})).total == 2
    with pytest.raises(ValueError):
        store.table("nope", M.TableFilter())
    with pytest.raises(ValueError):
        store.table("trials", M.TableFilter(where={"bogus": 1}))
    action = M.OperatorActionRow(action_id="01J8Z0000000000000000ACT02", actor="a@example.com", action="rebuild-index", target=None, params={},
                                 confirmed=True, origin_header=None, result="accepted", workflow_id=None, at=datetime(2026, 9, 13, 12, tzinfo=UTC), origin="cloudrun")
    store.add_operator_action(action)
    store.add_operator_action(action)
    assert store.operator_actions(M.PageFilter()).total == 1 and store.table("operator_actions", M.TableFilter()).items[0]["actor"] == "a@example.com"


def test_advisory_lock_on_sqlite_is_exclusive_per_key_within_the_process(store: IndexStore):
    order: list[str] = []
    holding = threading.Event()

    def holder():
        with store.advisory_lock("registry"):
            order.append("a-in")
            holding.set()
            time.sleep(0.2)
            order.append("a-out")

    def waiter():
        holding.wait(2)
        with store.advisory_lock("registry"):
            order.append("b-in")

    def other():
        holding.wait(2)
        with store.advisory_lock("other-key"):
            order.append("c-in")

    threads = [threading.Thread(target=holder), threading.Thread(target=waiter), threading.Thread(target=other)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert order.index("a-out") < order.index("b-in") and order.index("c-in") < order.index("a-out")
