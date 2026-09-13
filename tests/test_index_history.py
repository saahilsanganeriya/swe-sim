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


# ----------------------------------------------------------------------------- candidate decision receipts (P1-7)

def _event(**over) -> dict:
    return {"source_key": "swebench-annotated", "instance_id": "example-scenario", "candidates_path": "/plans/create-1/0.candidates.jsonl",
            "scenario_id": "example-scenario", "version": 2, "phase": "auto-approval", "stage": "approved", "status": "rejected",
            "gate": "auto-approval", "reasons": ["withheld brief lost the natural title line"], "terminal": True,
            "occurred_at": "2026-09-13T12:00:00+00:00", "workflow_id": "create:example-scenario", "run_id": "run-9", "type": "CandidateWorkflow",
            "parent_workflow_id": "create-1", **over}


def test_candidate_receipt_is_durable_first_and_makes_a_rejected_candidate_never_show_as_kept(tmp_path, monkeypatch):
    from swesim.index import activities as IA
    from swesim.index import project as P
    from swesim.index.files import FileIndexReader

    try:
        from test_index_project import SID, build_record
    except ImportError:  # pragma: no cover
        from tests.test_index_project import SID, build_record
    record = build_record(tmp_path / "record")
    settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_WORKSPACE": str(record.layout.root),
                                  "SWESIM_REGISTRY": str(record.registry_path), "SWESIM_ORIGIN": "cloudrun",
                                  "SWESIM_DATABASE_URL": f"sqlite:///{tmp_path / 'index.db'}", "SWESIM_WORKER_IMAGE_DIGEST": "sha256:" + "3" * 64})
    IA.reset_store()
    store = IA.store_for(settings); store.migrate()
    # Before any receipt the files say the scenario is registered, so the candidate reads as kept.
    assert IA.index_candidate({"source_key": "swebench-annotated", "instance_id": SID}, settings)["rows"] == 1
    assert store.candidates(M.CandidateFilter(instance_id=[SID])).items[0].status == "kept"
    # The workflow's auto-approval rejection: the receipt lands in the record even when SQL is down ...
    original = store.upsert_candidate_event
    monkeypatch.setattr(store, "upsert_candidate_event", lambda row: (_ for _ in ()).throw(RuntimeError("database down")))
    with pytest.raises(RuntimeError, match="database down"):
        IA.index_candidate_event(_event(), settings)
    assert len(history.read_rows(record.layout, "candidate_events", settings)) == 1
    monkeypatch.setattr(store, "upsert_candidate_event", original)
    # ... and a retry is idempotent (same id, same snapshot), then the candidate row says rejected at the approval gate.
    out = IA.index_candidate_event(_event(), settings)
    assert out["indexed"] is True and out["rows"] == 2 and out["status"] == "rejected"
    assert IA.index_candidate_event(_event(), settings)["event_id"] == out["event_id"]
    assert len(history.read_rows(record.layout, "candidate_events", settings)) == 1 and store.table("candidate_events", M.TableFilter()).total == 1
    row = store.candidates(M.CandidateFilter(instance_id=[SID])).items[0]
    assert (row.stage, row.status, row.gate) == ("approved", "rejected", "auto-approval") and row.reasons == ["withheld brief lost the natural title line"]
    assert (row.workflow_id, row.run_id, row.candidates_path) == ("create:example-scenario", "run-9", "/plans/create-1/0.candidates.jsonl")
    event = store.table("candidate_events", M.TableFilter()).items[0]
    assert event["origin"] == "cloudrun" and event["worker_image_digest"] == "sha256:" + "3" * 64 and event["terminal"] is True
    # A later decision (another run registers it) is a new receipt and the files' registered/kept view returns.
    later = _event(phase="register", stage="registered", status="kept", gate=None, reasons=[], run_id="run-10", occurred_at="2026-09-13T13:00:00+00:00")
    assert IA.index_candidate_event(later, settings)["event_id"] != out["event_id"]
    row = store.candidates(M.CandidateFilter(instance_id=[SID])).items[0]
    assert (row.stage, row.status, row.run_id) == ("registered", "kept", "run-10")
    # An approval timeout on another candidate whose draft exists reads as rejected at the approval gate, never as drafted/kept.
    timeout = _event(instance_id="second__scenario-1", scenario_id="second-scenario", version=1, phase="hold", gate="approval-timeout",
                     reasons=["no reviewer decision within the hold window"], run_id="run-11")
    IA.index_candidate_event(timeout, settings)
    # Rebuild into a fresh database and read the same record with the file reader: both agree with the live rows.
    fresh = IndexStore(f"sqlite:///{tmp_path / 'fresh.db'}", settings=settings); fresh.migrate()
    fresh.rebuild_from_layout(record.layout, settings.registry_path)
    assert fresh.table("candidate_events", M.TableFilter()).total == 3 == store.table("candidate_events", M.TableFilter()).total
    assert fresh.candidates(M.CandidateFilter(instance_id=[SID])).items[0].status == "kept"
    files = FileIndexReader(record.layout, settings.registry_path, settings=settings)
    assert files.table("candidate_events", M.TableFilter()).total == 3
    assert files.candidates(M.CandidateFilter(instance_id=[SID])).items[0].run_id == "run-10"
    events = P.candidate_events_by_identity(record.layout, settings)
    assert {k[1] for k in events} == {SID, "second__scenario-1"}
    with pytest.raises(Exception, match="payload lacks"):
        IA.index_candidate_event({"source_key": "swebench-annotated", "instance_id": SID}, settings)
    IA.reset_store()


def test_candidate_receipt_without_the_index_still_lands_in_the_record(tmp_path):
    from swesim.index import activities as IA
    layout = Layout(root=tmp_path / "record")
    settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_WORKSPACE": str(layout.root)})
    out = IA.index_candidate_event(_event(), settings)
    assert out["indexed"] is False and out["reason"] == "index disabled" and len(out["event_id"]) == 64
    [row] = history.read_rows(layout, "candidate_events")
    assert row.event_id == out["event_id"] and row.gate == "auto-approval" and row.origin == "laptop"
