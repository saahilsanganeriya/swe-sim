"""Console record writes are durable Temporal actions, safe with a read-only console mount."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from swesim.models import ScenarioSpec, minimal_spec_dict
from swesim.settings import Settings


def settings(tmp_path):
    return Settings.from_env({'SWESIM_ENV_FILE': str(tmp_path / 'none'), 'SWESIM_WORKSPACE': str(tmp_path), 'SWESIM_REGISTRY': str(tmp_path / 'registry.json')})


def test_label_worker_write_is_idempotent_and_receipt_binds_request(tmp_path, monkeypatch):
    from swesim.workflows import operator_actions as O
    monkeypatch.setattr(O, 'get_settings', lambda: settings(tmp_path))
    tid = 'a' * 24
    (tmp_path / 'trials' / tid).mkdir(parents=True)
    p = {'action': 'label', 'request_id': 'b' * 64, 'actor': 'reviewer@example.com',
         'params': {'trial_id': tid, 'label': 'no-question', 'note': 'Observed no question.'}}
    prepared = asyncio.run(O.prepare_operator_action_activity(p))
    first = asyncio.run(O.commit_operator_action_activity(prepared))
    assert json.loads((tmp_path / 'trials' / tid / 'label.json').read_text())['label'] == 'no-question'
    assert asyncio.run(O.commit_operator_action_activity(prepared)) == first
    changed = {**p, 'params': {**p['params'], 'label': 'unresolved'}}
    with pytest.raises(Exception, match='different'):
        asyncio.run(O.prepare_operator_action_activity(changed))


def test_review_worker_refuses_changed_spec_and_writes_without_console_mount(tmp_path, monkeypatch):
    from swesim.workflows import operator_actions as O
    monkeypatch.setattr(O, 'get_settings', lambda: settings(tmp_path))
    spec = ScenarioSpec.model_validate(minimal_spec_dict())
    directory = tmp_path / 'scenarios' / spec.id
    directory.mkdir(parents=True)
    (directory / 'scenario.draft.json').write_text(spec.model_dump_json())
    p = {'action': 'review', 'request_id': 'c' * 64, 'actor': 'reviewer@example.com',
         'params': {'scenario_id': spec.id, 'spec_hash': 'wrong', 'decision': 'reject', 'note': 'Unsafe task', 'workflow_id': ''}}
    with pytest.raises(Exception, match='changed'):
        asyncio.run(O.prepare_operator_action_activity(p))
    p['params']['spec_hash'] = spec.spec_hash
    prepared = asyncio.run(O.prepare_operator_action_activity(p))
    assert prepared.get('signal_target') is None
    asyncio.run(O.commit_operator_action_activity(prepared))
    receipt = json.loads((directory / 'review.json').read_text())
    assert receipt['decision'] == 'reject' and receipt['decided_by'] == 'reviewer@example.com'


def test_console_label_dispatch_never_writes_record(tmp_path, monkeypatch):
    from swesim.dashboard import app as A
    from swesim.dashboard import console
    from swesim.storage.layout import Layout
    called = []
    async def submit(action, params, actor, request_id):
        called.append((action, params, actor, request_id))
        return {'ok': True, 'workflow_id': 'operator-one'}
    monkeypatch.setattr(console, 'submit_record_action', submit)
    monkeypatch.setattr(Path, 'write_text', lambda *args, **kwargs: pytest.fail('console wrote its read-only mount'))
    result = asyncio.run(A._execute('label', {'trial_id': 'a'*24, 'label': 'no-question', 'note': '',
                                             '_actor': 'reviewer@example.com', '_request_id': 'd'*64}, None, Layout(root=tmp_path), settings(tmp_path)))
    assert result[0] == 200 and called[0][0] == 'label'


def test_retry_targets_exact_pr():
    from swesim.dashboard.console import create_sources, validate_retry
    candidate = {'source': 'github-repo', 'repo': 'example/project', 'instance_id': 'example__project-42',
                 'source_key': 'github-repo--example__project--bugfix', 'pr_number': 42, 'pr_kind': 'bugfix', 'scenario_id': None}
    params, errors = validate_retry({}, candidate)
    assert not errors
    assert create_sources(params)[0]['pr'] == 42
