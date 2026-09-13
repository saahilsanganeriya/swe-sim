"""SQL can be discarded without losing workflow, action or rebuild history."""
from datetime import UTC, datetime, timedelta

import pytest

from swesim.index import models as M
from swesim.index.files import FileIndexReader
from swesim.index.store import IndexStore
from swesim.settings import Settings
from swesim.storage import history
from swesim.storage.layout import Layout

NOW = datetime(2026, 9, 13, 10, tzinfo=UTC)


@pytest.fixture
def context(tmp_path):
    settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_WORKSPACE": str(tmp_path / "record"),
                                  "SWESIM_REGISTRY": str(tmp_path / "record" / "registry.json"), "SWESIM_ORIGIN": "cloudrun"})
    store = IndexStore("sqlite://", settings=settings); store.migrate()
    return Layout(root=settings.workspace), settings, store


def workflow(**over):
    data = dict(workflow_id="create:one", run_id="run-1", type="CandidateWorkflow", parent_workflow_id="parent",
                status="running", params_summary={"instance_id": "org__repo-1"}, failure=None, started_at=NOW,
                closed_at=None, task_queue="swesim-authoring", cost_usd=None, search_attributes={}, origin="cloudrun",
                created_at=NOW, updated_at=NOW, lifecycle_source="workflow")
    return M.WorkflowRow(**{**data, **over})


def action():
    return M.OperatorActionRow(action_id="action-1", actor="operator@example.com", action="reject", target="scenario-one",
                               params={"note": "not grounded"}, confirmed=True, origin_header="https://console.example",
                               result="accepted", workflow_id="operator-action-1", at=NOW, origin="cloudrun")


def test_history_survives_sql_write_failure_and_rebuild(context, monkeypatch):
    layout, settings, store = context
    row = workflow()
    original = store._upsert

    def crash(*args, **kwargs):
        raise RuntimeError("SQL unavailable after durable publication")

    monkeypatch.setattr(store, "_upsert", crash)
    with pytest.raises(RuntimeError, match="SQL unavailable"):
        store.upsert_workflow(row)
    assert history.read_rows(layout, "workflows") == [row]
    monkeypatch.setattr(store, "_upsert", original)
    store.add_operator_action(action())
    rebuilt = IndexStore("sqlite://", settings=settings); rebuilt.migrate()
    first = rebuilt.rebuild_from_layout(layout, settings.registry_path)
    assert rebuilt.workflow(row.workflow_id).run_id == row.run_id
    assert rebuilt.operator_actions(M.PageFilter()).items[0].action_id == "action-1"
    fresh = IndexStore("sqlite://", settings=settings); fresh.migrate()
    fresh.rebuild_from_layout(layout, settings.registry_path)
    assert first.rebuild_id in {row.rebuild_id for row in fresh.rebuilds()}
    files = FileIndexReader(layout, settings.registry_path, settings=settings)
    assert files.workflow(row.workflow_id).run_id == row.run_id
    assert files.operator_actions(M.PageFilter()).total == 1 and len(files.rebuilds()) >= 2


def test_temporal_termination_wins_over_a_late_running_hook(context):
    layout, settings, store = context
    terminal = workflow(status="terminated", closed_at=NOW + timedelta(minutes=2), updated_at=NOW + timedelta(minutes=2), lifecycle_source="temporal")
    store.upsert_workflow(terminal)
    store.upsert_workflow(workflow(updated_at=NOW + timedelta(hours=1)))
    assert store.workflow(terminal.workflow_id).status == "terminated"
    assert FileIndexReader(layout, settings=settings).workflow(terminal.workflow_id).status == "terminated"
    other = IndexStore("sqlite://", settings=settings); other.migrate()
    other.rebuild_from_layout(layout, settings.registry_path)
    assert other.workflow(terminal.workflow_id).status == "terminated"


def test_operator_action_is_durable_before_sql(context, monkeypatch):
    layout, _, store = context
    monkeypatch.setattr(store, "_upsert", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database down")))
    row = action()
    with pytest.raises(RuntimeError): store.add_operator_action(row)
    assert history.read_rows(layout, "operator_actions") == [row]
