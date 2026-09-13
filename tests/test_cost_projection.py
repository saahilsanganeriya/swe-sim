"""Workflow/stakeholder costs participate in live projection and recorded spend."""
import asyncio
import json
from decimal import Decimal

from swesim import costs
from swesim.paths import Workspace, atomic_json
from swesim.settings import Settings
from swesim.workflows import activities as A


def test_file_spend_includes_creation_costs_and_marks_unknown_usage(tmp_path):
    ws = Workspace(tmp_path)
    costs.record_cost(ws, None, 'create-test', 'drafter', 'openai/gpt-5.6-sol', costs.Usage(input_tokens=1000, output_tokens=100), idempotency_key='draft')
    costs.record_cost(ws, 'a' * 24, None, 'solver', 'openai/gpt-5.6-sol', costs.Usage(input_tokens=1000, output_tokens=100), idempotency_key='solver')
    atomic_json(ws.root / 'trials' / ('a' * 24) / 'costs.status.json', {'stakeholder': {'usage_complete': False, 'unknown_usage_count': 1}})
    result = A.recorded_spend(Settings.from_env({'SWESIM_ENV_FILE': str(tmp_path / 'none.env'), 'SWESIM_WORKSPACE': str(tmp_path)}), ws)
    assert result['spend_usd'] == 0.012 and result['records'] == 2
    assert result['spend_complete'] is False and result['unknown_usage_count'] == 1


def test_draft_activity_passes_accounting_context(tmp_path, monkeypatch):
    import importlib
    draft_module = importlib.import_module('swesim.stages.draft_spec')
    ws = Workspace(tmp_path); monkeypatch.setattr(A, 'WS', ws)
    path = tmp_path / 'input.jsonl'; path.write_text(json.dumps({'instance_id': 'x__1', 'judge': {'keep': True}}) + '\n')
    captured = []
    def draft(row, judged, model_id, out_dir, *, layout=None, workflow_id=None):
        captured.append((layout.root, workflow_id))
        out_dir.mkdir(parents=True)
        atomic_json(out_dir / 'scenario.draft.json', {'id': 'x--1'})
    monkeypatch.setattr(draft_module, 'draft', draft)
    asyncio.run(A.draft_activity({'judged_path': str(path), 'instance_id': 'x__1', 'draft_model': 'openai/gpt-5.6-sol'}))
    assert captured == [(tmp_path, 'direct')]


def test_accounting_summary_is_a_known_total_and_rejects_missing_measurement_as_complete(tmp_path):
    ws = Workspace(tmp_path)
    costs.record_cost(ws, None, 'creation', 'audit', 'openai/gpt-5.6-sol', costs.Usage(input_tokens=1000, output_tokens=100), idempotency_key='a')
    costs.record_cost(ws, 'a' * 24, None, 'solver', 'anthropic/unknown-model', costs.Usage(input_tokens=10, output_tokens=1), idempotency_key='b')
    atomic_json(ws.root / 'scenarios' / 'x' / 'audit-usage' / 'unknown.json', {'usage': None})
    summary = costs.accounting_summary(ws)
    assert summary['known_usd'] == Decimal('0.006000') and summary['unpriced_rows'] == 1
    assert summary['unknown_usage_attempts'] == 1 and summary['complete'] is False and summary['records'] == 2


def test_preserved_trial_without_solver_ledger_is_not_claimed_complete(tmp_path):
    ws = Workspace(tmp_path)
    atomic_json(ws.root / 'trials' / ('a' * 24) / 'record.json', {'model': 'openai/gpt-5.6-sol', 'status': 'completed', 'cost_usd': None})
    assert costs.accounting_status(ws)['incomplete_solver_records'] == 1
    assert costs.accounting_summary(ws)['complete'] is False


def test_preserved_draft_without_accounting_is_not_claimed_free(tmp_path):
    ws = Workspace(tmp_path)
    atomic_json(ws.root / 'scenarios' / 'x' / 'draft.meta.json', {'model_id': 'openai/gpt-5.6-sol', 'usage': {'recorded': False}})
    assert costs.accounting_status(ws)['incomplete_draft_records'] == 1
    assert costs.accounting_summary(ws)['complete'] is False


def test_legacy_record_without_ledger_is_incomplete_never_zero_spend(tmp_path):
    """A VM-era record.json that names a model and carries `cost_usd: 0.0`, with no costs.json and no accounting identity:
    the summary's known total covers only what ledgers prove and the workspace is explicitly incomplete, so no reader
    may present the legacy trial as a measured $0. (Interfaces section 6: unknown is never recorded as zero.)"""
    ws = Workspace(tmp_path)
    atomic_json(ws.root / 'trials' / ('b' * 24) / 'record.json',
                {'model': 'anthropic/claude-haiku-4-5-20251001', 'status': 'completed', 'cost_usd': 0.0, 'price_source': None})
    summary = costs.accounting_summary(ws)
    assert summary['known_usd'] == Decimal(0) and summary['records'] == 0 and summary['unpriced_rows'] == 0
    assert summary['complete'] is False
    status = costs.accounting_status(ws)
    assert status['incomplete_solver_records'] == 1 and status['usage_complete'] is False
    for key in ('known_usd', 'unpriced_rows', 'unknown_usage_attempts', 'complete'):
        assert key in summary and key in costs.__doc__          # the documented contract names every field a reader needs
