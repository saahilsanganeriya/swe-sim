"""P1-6: `client.reconcile_workflows` closes `running` workflow rows from Temporal's own view of each execution (or marks them
`unknown` when Temporal no longer has the execution), never touches terminal rows, and is exposed as a CONTROL activity and a
Typer command. A fake Temporal client stands in for the server; nothing here connects."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import typer
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode
from typer.testing import CliRunner

from swesim.index import activities as IA
from swesim.index import models as M
from swesim.settings import Settings, reset_settings
from swesim.storage.layout import Layout
from swesim.workflows import activities as A
from swesim.workflows import client as C
from swesim.workflows import queues as Q
from swesim.workflows import cli_reconcile


class FakeHandle:
    def __init__(self, desc):
        self.desc = desc

    async def describe(self):
        if isinstance(self.desc, Exception):
            raise self.desc
        return self.desc


class FakeClient:
    """`get_workflow_handle(id, run_id=...).describe()` answers from a table; anything else is NOT_FOUND."""

    def __init__(self, executions: dict):
        self.executions = executions
        self.asked: list[tuple[str, str | None]] = []

    def get_workflow_handle(self, workflow_id, *, run_id=None, **kw):
        self.asked.append((workflow_id, run_id))
        return FakeHandle(self.executions.get((workflow_id, run_id), RPCError("workflow execution not found", RPCStatusCode.NOT_FOUND, b"")))


def desc(status, *, close=None, type_="RunTasksWorkflow", queue="swesim-authoring"):
    return SimpleNamespace(status=status, close_time=close, workflow_type=type_, task_queue=queue)


@pytest.fixture
def store(tmp_path):
    s = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_WORKSPACE": str(tmp_path / "ws"),
                           "SWESIM_REGISTRY": str(tmp_path / "registry.json"), "SWESIM_DATABASE_URL": f"sqlite:///{tmp_path / 'i.db'}",
                           "SWESIM_ORIGIN": "cloudrun"})
    IA.reset_store()
    st = IA.store_for(s)
    st.migrate()
    yield s, st
    IA.reset_store()


def seed(s: Settings, wid: str, rid: str, status: str = "running", started: datetime | None = None, type_: str = "RunTasksWorkflow") -> None:
    IA.index_workflow({"workflow_id": wid, "run_id": rid, "type": type_, "status": status, "params_summary": {"k": 1}, "failure": None,
                       "parent_workflow_id": None, "started_at": (started or datetime.now(UTC)).isoformat(), "task_queue": "swesim-authoring"}, s)


def test_reconcile_closes_running_rows_from_temporal_and_marks_missing_ones_unknown(store):
    s, st = store
    now = datetime.now(UTC)
    closed = (now - timedelta(minutes=5)).replace(microsecond=0)
    for i, wid in enumerate(["run-a", "run-b", "run-c", "run-d", "run-e", "run-f", "run-g"]):
        seed(s, wid, f"r{i}")
    seed(s, "create-old", "r9", started=now - timedelta(hours=72))            # outside the window: left alone
    seed(s, "run-done", "r8", status="completed")                              # already terminal: never described
    fake = FakeClient({
        ("run-a", "r0"): desc(WorkflowExecutionStatus.COMPLETED, close=closed),
        ("run-b", "r1"): desc(WorkflowExecutionStatus.FAILED, close=closed),
        ("run-c", "r2"): desc(WorkflowExecutionStatus.CANCELED, close=closed),
        ("run-d", "r3"): desc(WorkflowExecutionStatus.TERMINATED, close=closed),
        ("run-e", "r4"): desc(WorkflowExecutionStatus.TIMED_OUT, close=closed),
        ("run-f", "r5"): desc(WorkflowExecutionStatus.RUNNING),
        # run-g: not found (retention passed, or a row imported from another namespace)
        ("create-old", "r9"): desc(WorkflowExecutionStatus.COMPLETED, close=closed),
        ("run-done", "r8"): desc(WorkflowExecutionStatus.COMPLETED, close=closed),
    })
    out = asyncio.run(C.reconcile_workflows(st, max_age_hours=48, client=fake))
    assert (out["checked"], out["reconciled"], out["unknown"], out["still_running"], out["errors"]) == (7, 5, 1, 1, [])
    assert sorted(w for w, _ in fake.asked) == ["run-a", "run-b", "run-c", "run-d", "run-e", "run-f", "run-g"]
    by = {w.workflow_id: w for w in st.workflows(M.WorkflowFilter(limit=50)).items}
    assert [by[w].status for w in ("run-a", "run-b", "run-c", "run-d", "run-e")] == ["completed", "failed", "cancelled", "terminated", "timed_out"]
    assert all(by[w].lifecycle_source == "temporal" and by[w].closed_at == closed for w in ("run-a", "run-b", "run-c", "run-d", "run-e"))
    assert "Temporal" in by["run-b"].failure and by["run-a"].failure is None and by["run-a"].params_summary == {"k": 1} and by["run-a"].type == "RunTasksWorkflow"
    assert by["run-f"].status == "running" and by["run-f"].lifecycle_source == "workflow"
    assert by["run-g"].status == "unknown" and by["run-g"].lifecycle_source == "temporal" and by["run-g"].closed_at is not None and "retention" in by["run-g"].failure
    assert by["create-old"].status == "running" and by["run-done"].status == "completed" and by["run-done"].lifecycle_source == "workflow"
    rows = {(r["workflow_id"], r["from"], r["to"]) for r in out["rows"]}
    assert ("run-a", "running", "completed") in rows and ("run-g", "running", "unknown") in rows and not any(r["workflow_id"] == "run-f" for r in out["rows"])
    # the run is recorded in the record too (the bucket keeps the last reconciliation)
    checkpoint = json.loads(Layout(root=s.workspace).reconciliation_checkpoint().read_text())
    assert checkpoint["reconciled"] == 5 and checkpoint["unknown"] == 1 and checkpoint["max_age_hours"] == 48 and checkpoint["at"]
    # a late `running` hook cannot reopen a Temporal-closed row; a second reconcile has nothing left to close
    IA.index_workflow({"workflow_id": "run-a", "run_id": "r0", "type": "RunTasksWorkflow", "status": "running", "params_summary": {}, "failure": None,
                       "parent_workflow_id": None}, s)
    assert st.workflow("run-a").status == "completed"
    again = asyncio.run(C.reconcile_workflows(st, max_age_hours=48, client=fake))
    assert (again["checked"], again["reconciled"], again["still_running"]) == (1, 0, 1)
    # no window: the old row is described too; a Settings object is accepted in place of the store
    assert asyncio.run(C.reconcile_workflows(s, max_age_hours=None, client=fake))["reconciled"] == 1 and st.workflow("create-old").status == "completed"


def test_reconcile_reports_rpc_errors_per_row_and_never_raises(store):
    s, st = store
    seed(s, "run-x", "r1")

    class Down(FakeClient):
        def get_workflow_handle(self, workflow_id, *, run_id=None, **kw):
            return FakeHandle(RPCError("service unavailable", RPCStatusCode.UNAVAILABLE, b""))

    out = asyncio.run(C.reconcile_workflows(st, client=Down({})))
    assert (out["checked"], out["reconciled"], out["unknown"]) == (1, 0, 0) and len(out["errors"]) == 1
    assert out["errors"][0]["workflow_id"] == "run-x" and "UNAVAILABLE" in out["errors"][0]["error"] and st.workflow("run-x").status == "running"


def test_reconcile_without_the_index_reports_disabled(tmp_path):
    s = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env")})
    assert asyncio.run(C.reconcile_workflows(s, client=FakeClient({}))) == {"indexed": False, "reason": "index disabled"}


def test_reconcile_connects_through_the_client_module_when_no_client_is_given(store, monkeypatch):
    s, st = store
    fake = FakeClient({})

    async def connect(environ=None):
        return fake

    monkeypatch.setattr(C, "connect", connect)
    seed(s, "run-y", "r1")
    out = asyncio.run(C.reconcile_workflows(st))
    assert out["unknown"] == 1 and fake.asked == [("run-y", "r1")] and st.workflow("run-y").status == "unknown"


def test_unknown_is_a_workflow_status_and_the_activity_is_routed_to_control():
    assert "unknown" in M.WorkflowStatus.__args__ and "unknown" in IA.WORKFLOW_STATUSES
    assert Q.queue_for("reconcile_workflows_activity") == Q.CONTROL and A.reconcile_workflows_activity in A.ALL


def test_reconcile_activity_calls_the_client_function_with_the_worker_settings(monkeypatch):
    seen = {}

    async def fake(settings, *, max_age_hours=48, client=None):
        seen.update(settings=settings, max_age_hours=max_age_hours)
        return {"checked": 2, "reconciled": 1, "unknown": 0, "still_running": 1, "errors": [], "rows": []}

    monkeypatch.setattr(C, "reconcile_workflows", fake)
    out = asyncio.run(A.reconcile_workflows_activity({"max_age_hours": 6}))
    assert out["reconciled"] == 1 and seen["settings"] is A.SETTINGS and seen["max_age_hours"] == 6 and out["origin"] == A.SETTINGS.origin
    assert asyncio.run(A.reconcile_workflows_activity({}))["checked"] == 2 and seen["max_age_hours"] == 48
    assert asyncio.run(A.reconcile_workflows_activity({"max_age_hours": 0}))["checked"] == 2 and seen["max_age_hours"] is None


def _text(result) -> str:
    try:
        return result.output + result.stderr
    except (ValueError, AttributeError):
        return result.output


def test_cli_reconcile_prints_the_summary_and_exits_2_without_the_index(monkeypatch, tmp_path):
    calls = []

    async def fake(settings, *, max_age_hours=48, client=None):
        calls.append(max_age_hours)
        return {"checked": 3, "reconciled": 2, "unknown": 1, "still_running": 0, "errors": [{"workflow_id": "run-z", "run_id": "r", "error": "UNAVAILABLE: down"}],
                "rows": [{"workflow_id": "run-a", "run_id": "r0", "from": "running", "to": "completed"},
                         {"workflow_id": "run-g", "run_id": "r6", "from": "running", "to": "unknown"}]}

    monkeypatch.setattr(C, "reconcile_workflows", fake)
    app = typer.Typer()
    cli_reconcile.mount(app)                          # exactly what swesim/cli.py does for `swesim workflows reconcile`
    monkeypatch.delenv("SWESIM_DATABASE_URL", raising=False)
    monkeypatch.setenv("SWESIM_ENV_FILE", str(tmp_path / "none.env"))
    reset_settings()
    try:
        r = CliRunner().invoke(app, [])
        assert r.exit_code == 2 and "index disabled" in _text(r) and calls == []
        monkeypatch.setenv("SWESIM_DATABASE_URL", "sqlite://")
        reset_settings()
        r = CliRunner().invoke(app, ["--max-age-hours", "12"])
        assert r.exit_code == 0 and calls == [12], _text(r)
        assert "run-a\trunning -> completed" in r.output and "run-g\trunning -> unknown" in r.output and "reconciled=2" in r.output and "run-z" in _text(r)
        r = CliRunner().invoke(app, ["--max-age-hours", "0", "--json"])
        assert r.exit_code == 0 and json.loads(r.output)["checked"] == 3 and calls[-1] is None
    finally:
        reset_settings()
