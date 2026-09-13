"""Operator commands use the shared workflow clients and retain explicit run identities."""
from typer.testing import CliRunner

from swesim.cli import app
from swesim.workflows import client


def test_open_types_prints_nothing_when_no_execution_is_open(monkeypatch):
    async def empty():
        return []
    monkeypatch.setattr(client, "open_types", empty)
    result = CliRunner().invoke(app, ["workflows", "open-types"])
    assert result.exit_code == 0 and result.stdout == ""


def test_open_types_reports_every_open_type(monkeypatch):
    async def running():
        return [{"type": "TrialWorkflow", "open_count": 3}, {"type": "CreateTasksWorkflow", "open_count": 1}]
    monkeypatch.setattr(client, "open_types", running)
    result = CliRunner().invoke(app, ["workflows", "open-types"])
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["TrialWorkflow\t3", "CreateTasksWorkflow\t1"]


def test_screen_selects_the_screen_model_and_preserves_explicit_override(monkeypatch):
    submitted = []
    async def start(params):
        submitted.append(params)
        return "run-test"
    monkeypatch.setattr(client, "start_run", start)
    args = ["run-tasks", "--scenario-ids", "test-scenario", "--purpose", "screen"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert submitted[-1]["models"] == ["openrouter/deepseek/deepseek-v3.2"]
    result = CliRunner().invoke(app, args + ["--models", "openai/gpt-5-mini"])
    assert result.exit_code == 0
    assert submitted[-1]["models"] == ["openai/gpt-5-mini"]
