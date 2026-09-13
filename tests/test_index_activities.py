"""Index activities (interfaces §2) on SQLite: exact names routed to the control queue, `{"indexed": True, "rows": n}`
on success, `{"indexed": False, "reason": "index disabled"}` without the index, non-retryable errors for payloads the
files cannot satisfy, idempotent upserts."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError

from swesim.index import activities as IA
from swesim.index import models as M
from swesim.settings import QUEUES, Settings, get_settings, reset_settings
from swesim.workflows import queues as Q

try:
    from test_index_project import SID, Record, build_record
except ImportError:  # pragma: no cover
    from tests.test_index_project import SID, Record, build_record

NAMES = ["index_scenario_activity", "index_candidate_activity", "index_trial_activity", "index_score_activity", "index_workflow_activity",
         "index_rebuild_activity", "index_discovery_activity"]


@pytest.fixture(scope="module")
def record(tmp_path_factory) -> Record:
    return build_record(tmp_path_factory.mktemp("record"))


@pytest.fixture
def enabled(tmp_path, record: Record, monkeypatch) -> Settings:
    """Process settings with the index on (a SQLite file so every connection sees the same database)."""
    for key in ("SWESIM_DATABASE_URL", "SWESIM_WORKSPACE", "SWESIM_REGISTRY", "SWESIM_ENV_FILE", "SWESIM_ORIGIN", "SWESIM_WORKER_IMAGE_DIGEST"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SWESIM_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("SWESIM_DATABASE_URL", f"sqlite:///{tmp_path / 'index.db'}")
    monkeypatch.setenv("SWESIM_WORKSPACE", str(record.layout.root))
    monkeypatch.setenv("SWESIM_REGISTRY", str(record.registry_path))
    monkeypatch.setenv("SWESIM_ORIGIN", "cloudrun")
    monkeypatch.setenv("SWESIM_WORKER_IMAGE_DIGEST", "sha256:" + "2" * 64)
    reset_settings()
    IA.reset_store()
    s = get_settings()
    IA.store_for(s).migrate()
    yield s
    reset_settings()
    IA.reset_store()


@pytest.fixture
def disabled(tmp_path, monkeypatch) -> Settings:
    for key in ("SWESIM_DATABASE_URL", "SWESIM_WORKSPACE", "SWESIM_REGISTRY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SWESIM_ENV_FILE", str(tmp_path / "none.env"))
    reset_settings()
    IA.reset_store()
    yield get_settings()
    reset_settings()
    IA.reset_store()


def run(fn, p):
    return asyncio.run(fn(p))


def test_names_are_exact_and_routed_to_the_control_queue():
    assert [activity._Definition.from_callable(fn).name for fn in IA.ALL] == NAMES
    assert all(Q.queue_for(n) == Q.CONTROL for n in NAMES) and Q.CONTROL in QUEUES


def test_disabled_index_never_raises(disabled: Settings):
    for fn in IA.ALL:
        if fn is IA.index_discovery_activity:
            continue  # discovery also preserves the file-backed history and requires its source payload
        assert run(fn, {}) == {"indexed": False, "reason": "index disabled"}
        assert run(fn, {"trial_id": "nope", "scenario_id": 7}) == {"indexed": False, "reason": "index disabled"}
    assert IA.store_for(disabled) is None


def test_trial_activity_indexes_row_exchanges_and_costs_idempotently(enabled: Settings, record: Record):
    tid = record.tids["done"]
    first = run(IA.index_trial_activity, {"trial_id": tid})
    assert first == {"indexed": True, "rows": 1 + 6 + 3, "status": "completed"}
    assert run(IA.index_trial_activity, {"trial_id": tid}) == first
    store = IA.store_for(enabled)
    row = store.trial(tid)
    assert row is not None and row.origin == "cloudrun" and row.cost_usd == Decimal("0.03") and store.trials(M.TrialFilter()).total == 1
    assert len(store.exchanges(tid, evaluator=True)) == 6 and len(store.costs(M.CostFilter(trial_id=[tid]))) == 3
    assert run(IA.index_trial_activity, {"trial_id": record.tids["launch-only"]})["status"] == "unresolved"
    with pytest.raises(ApplicationError) as e:
        run(IA.index_trial_activity, {"trial_id": "0" * 24})
    assert e.value.non_retryable and e.value.type == "projection"
    with pytest.raises(ApplicationError) as e:
        run(IA.index_trial_activity, {})
    assert e.value.non_retryable and "trial_id" in str(e.value)


def test_scenario_candidate_and_score_activities(enabled: Settings, record: Record):
    store = IA.store_for(enabled)
    assert run(IA.index_scenario_activity, {"scenario_id": SID}) == {"indexed": True, "rows": 2}  # current + archived
    assert [s.version for s in store.scenario_versions(SID)] == [1, 2]
    assert run(IA.index_scenario_activity, {"scenario_id": SID, "version": 2}) == {"indexed": True, "rows": 1}
    assert store.scenarios(M.ScenarioFilter()).total == 2
    with pytest.raises(ApplicationError):
        run(IA.index_scenario_activity, {"scenario_id": SID, "version": 9})
    with pytest.raises(ApplicationError):
        run(IA.index_scenario_activity, {"scenario_id": "no-such-scenario"})
    assert run(IA.index_candidate_activity, {"source_key": "swebench-annotated", "instance_id": "other__lib-12"}) == {"indexed": True, "rows": 1}
    assert store.candidates(M.CandidateFilter()).items[0].status == "rejected"
    with pytest.raises(ApplicationError):
        run(IA.index_candidate_activity, {"source_key": "swebench-annotated", "instance_id": "missing__x-1"})
    assert run(IA.index_score_activity, {"scenario_id": SID}) == {"indexed": True, "rows": 2}
    assert run(IA.index_score_activity, {"scenario_id": "second-scenario"}) == {"indexed": True, "rows": 0}
    assert len(store.scores(M.ScoreFilter())) == 2


def test_workflow_activity_keeps_started_at_and_closes_terminal_rows(enabled: Settings):
    store = IA.store_for(enabled)
    p = {"workflow_id": "create:x", "run_id": "r1", "type": "CandidateWorkflow", "status": "running", "params_summary": {"instance_id": "x"},
         "failure": None, "parent_workflow_id": "create-1", "started_at": "2026-09-13T10:00:00+00:00", "task_queue": "swesim-authoring"}
    assert run(IA.index_workflow_activity, p) == {"indexed": True, "rows": 1}
    running = store.workflow("create:x")
    assert running.status == "running" and running.closed_at is None and running.started_at == datetime(2026, 9, 13, 10, tzinfo=UTC)
    assert running.origin == "cloudrun" and running.worker_image_digest == "sha256:" + "2" * 64 and running.parent_workflow_id == "create-1"
    done = {**p, "status": "failed", "failure": "judge did not keep", "started_at": None, "cost_usd": "0.25"}
    run(IA.index_workflow_activity, done)
    failed = store.workflow("create:x")
    assert failed.status == "failed" and failed.started_at == running.started_at and failed.closed_at is not None and failed.cost_usd == Decimal("0.25")
    assert failed.created_at == running.created_at and store.workflows(M.WorkflowFilter()).total == 1
    with pytest.raises(ApplicationError):
        run(IA.index_workflow_activity, {**p, "status": "exploded"})
    with pytest.raises(ApplicationError):
        run(IA.index_workflow_activity, {"workflow_id": "w"})


def test_rebuild_activity_returns_counts_and_a_bounded_skip_list(enabled: Settings, record: Record):
    out = run(IA.index_rebuild_activity, {})
    assert out["indexed"] is True and out["counts"]["trials"] == len(record.tids) - 1 and out["rows"] > out["counts"]["trials"]
    assert out["skipped_total"]["trials"] == 1 and len(out["skipped"]["trials"]) == 1 and "rebuild_id" in out
    assert out["diff"]["scenarios"]["after"] == 5 and "projected" not in out["counts"]
    assert IA.store_for(enabled).rebuilds()[0].origin == "cloudrun"
    assert run(IA.index_rebuild_activity, {"origin": "vm-systemd"})["indexed"] is True
    assert IA.store_for(enabled).trial(record.tids["done"]).origin == "vm-systemd"
    with pytest.raises(ApplicationError):
        run(IA.index_rebuild_activity, {"origin": "moon"})
