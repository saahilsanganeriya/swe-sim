"""Billed audit attempts survive an interruption between audit completion and ledger projection."""
import asyncio
import json
from dataclasses import replace
from decimal import Decimal

import pytest
from temporalio.testing import ActivityEnvironment

from swesim import costs, llm
from swesim.audit import activities as A
from swesim.audit import llm_audit as L
from test_audit_llm import FakeChat
from test_audit_run import make_record


def environment(attempt=1):
    env = ActivityEnvironment()
    env.info = replace(env.info, workflow_id='audit-test', workflow_run_id='run-1', activity_id='audit', attempt=attempt)
    return env


def test_audit_ledger_replays_when_result_was_already_written(tmp_path, monkeypatch):
    ws, spec = make_record(tmp_path)
    monkeypatch.setattr(A, 'layout_from_settings', lambda: ws)
    fake = FakeChat(); calls = []
    def chat(*args, **kwargs):
        calls.append(1)
        return llm.ChatResult(fake(*args, **kwargs), costs.Usage(input_tokens=1000, output_tokens=100, calls=1))
    monkeypatch.setattr(A, '_judge', lambda p: L.Judge(chat=chat, models=('openai/gpt-5.6-sol',)))
    original = costs.record_cost
    def fail_ledger(*args, **kwargs): raise OSError('simulated crash after audit.json')
    monkeypatch.setattr(costs, 'record_cost', fail_ledger)
    with pytest.raises(OSError):
        asyncio.run(environment().run(A.audit_scenario_activity, {'scenario_id': spec.id}))
    audit_path = ws.scenario_dir(spec.id) / 'audit.json'
    assert audit_path.exists() and len(list((audit_path.parent / 'audit-usage').glob('*.json'))) == 1
    assert json.loads(audit_path.read_text())['cost_recorded'] is False
    monkeypatch.setattr(costs, 'record_cost', original)
    result = asyncio.run(environment(2).run(A.audit_scenario_activity, {'scenario_id': spec.id}))
    assert result['written'] is False and result['cost_recorded'] is True and calls == [1]
    rows = costs.read_costs(ws.root / 'costs' / 'audit-test.json')
    assert len(rows) == 1 and rows[0].usd == Decimal('0.006000')
    asyncio.run(environment(3).run(A.audit_scenario_activity, {'scenario_id': spec.id}))
    assert costs.read_costs(ws.root / 'costs' / 'audit-test.json') == rows and calls == [1]


def test_failed_paid_attempts_flush_and_equal_token_attempts_stay_distinct(tmp_path, monkeypatch):
    ws, spec = make_record(tmp_path)
    monkeypatch.setattr(A, 'layout_from_settings', lambda: ws)
    def malformed(*args, **kwargs):
        llm.note_usage(costs.Usage(input_tokens=1000, output_tokens=100, calls=1))
        raise llm.LLMError('billed response was not JSON')
    monkeypatch.setattr(A, '_judge', lambda p: L.Judge(chat=malformed, models=('openai/gpt-5.6-sol',)))
    for attempt in (1, 2):
        with pytest.raises(L.AuditLLMError):
            asyncio.run(environment(attempt).run(A.audit_scenario_activity, {'scenario_id': spec.id}))
    rows = costs.read_costs(ws.root / 'costs' / 'audit-test.json')
    assert len(rows) == 2 and sum(row.usd for row in rows) == Decimal('0.012000')
    pending = json.loads((ws.scenario_dir(spec.id) / 'audit.json').read_text())
    assert pending['status'] == 'pending' and pending['coherent'] is None and pending['cost_recorded'] is True


def test_unknown_usage_is_visible_without_zero_cost_row(tmp_path, monkeypatch):
    ws, spec = make_record(tmp_path)
    monkeypatch.setattr(A, 'layout_from_settings', lambda: ws)
    def unavailable(*args, **kwargs): raise llm.LLMError('no usage was returned')
    monkeypatch.setattr(A, '_judge', lambda p: L.Judge(chat=unavailable, models=('openai/gpt-5.6-sol',)))
    with pytest.raises(L.AuditLLMError):
        asyncio.run(environment().run(A.audit_scenario_activity, {'scenario_id': spec.id}))
    pending = json.loads((ws.scenario_dir(spec.id) / 'audit.json').read_text())
    assert pending['usage_complete'] is False and pending['unknown_usage_count'] == 1
    assert len(pending['usage_receipts']) == 1 and not (ws.root / 'costs' / 'audit-test.json').exists()


def test_partial_paid_usage_survives_a_later_provider_error(tmp_path, monkeypatch):
    ws, spec = make_record(tmp_path)
    monkeypatch.setattr(A, 'layout_from_settings', lambda: ws)
    def interrupted(*args, **kwargs):
        llm.note_usage(costs.Usage(input_tokens=1000, output_tokens=100, calls=1))
        raise RuntimeError('later provider request lost acknowledgement')
    monkeypatch.setattr(A, '_judge', lambda p: L.Judge(chat=interrupted, models=('openai/gpt-5.6-sol',)))
    with pytest.raises(RuntimeError):
        asyncio.run(environment().run(A.audit_scenario_activity, {'scenario_id': spec.id}))
    summary = costs.accounting_summary(ws)
    assert summary['known_usd'] == Decimal('0.006000')
    assert summary['unknown_usage_attempts'] == 1 and summary['complete'] is False


def test_llm_publishes_first_response_usage_before_fallback_failure(monkeypatch):
    import sys
    from types import SimpleNamespace
    first = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=None),
                            choices=[SimpleNamespace(message=SimpleNamespace(content=''), finish_reason='length')])
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1: return first
        raise RuntimeError('second provider request failed')
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))))
    monkeypatch.setattr(llm, '_secret', lambda *args: 'fake-test-key')
    llm.take_last_usage()
    with pytest.raises(RuntimeError):
        llm.chat_json_with_usage('openai/gpt-5.6-sol', 'system', [{'role': 'user', 'content': 'request'}], '{}')
    measured = llm.take_last_usage()
    assert measured.input_tokens == 100 and measured.output_tokens == 20 and measured.calls == 1
