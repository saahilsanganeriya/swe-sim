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


def test_hosted_audit_never_falls_back_to_readonly_console_mount(tmp_path):
    from swesim.dashboard import access, actions
    from swesim.dashboard.identity import Identity
    hosted=settings(tmp_path).model_copy(update={'bucket':'test-bucket'})
    row=actions.audit_row(identity=Identity('reviewer@example.com',True),action='label',target='a'*24,params={},confirmed=True,
                          origin_header='https://console.example',result='accepted',workflow_id=None,settings=hosted)
    with pytest.raises(RuntimeError,match='audit backend'):
        access.record_action(object(),hosted,row)
    assert not (tmp_path/'evidence').exists()


def test_operator_workflow_records_signal_as_workflow_command(monkeypatch):
    from types import SimpleNamespace
    from swesim.workflows import operator_actions as O, workflows as W
    events=[]
    request={'action':'review','request_id':'f'*64,'actor':'reviewer@example.com',
             'params':{'scenario_id':'example-scenario','spec_hash':'a'*64,'decision':'approve','note':'Reviewed','workflow_id':'create:example-scenario'}}
    async def act(name,p,*args,**kwargs):
        events.append(name)
        if name=='prepare_operator_action_activity':
            return {'request':p,'signal_target':'create:example-scenario'}
        return {'ok':True}
    async def signal(name,p):
        events.append(('signal',name,p['decision']))
    async def hook(*args,**kwargs):
        return {'indexed':True}
    monkeypatch.setattr(W,'act',act)
    monkeypatch.setattr(W,'hook',hook)
    monkeypatch.setattr(O.workflow,'get_external_workflow_handle',lambda target:SimpleNamespace(signal=signal))
    assert asyncio.run(O.OperatorActionWorkflow().run(request))=={'ok':True}
    assert events==['prepare_operator_action_activity',('signal','approve','approve'),'commit_operator_action_activity']


def test_retry_reuses_frozen_row_without_selecting_first_or_newer_pr(tmp_path, monkeypatch):
    from swesim.dashboard.console import create_sources, validate_retry
    from swesim.workflows import operator_actions as O
    monkeypatch.setattr(O,'get_settings',lambda:settings(tmp_path))
    path=tmp_path/'evidence'/'workflows'/'original'/'candidates.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'instance_id':'example__project-99','body':'newer'})+'\n'+json.dumps({'instance_id':'example__project-42','body':'frozen'})+'\n')
    candidate={'source':'github-repo','repo':'example/project','instance_id':'example__project-42','source_key':'github-repo--example__project--bugfix',
               'pr_number':42,'pr_kind':'bugfix','scenario_id':None,'candidates_path':str(path)}
    params,errors=validate_retry({},candidate)
    assert not errors
    source=create_sources(params)[0]
    assert source['kind']=='frozen-candidate'
    result=asyncio.run(O.read_candidate_source_activity(source))
    assert json.loads(Path(result['candidates_path']).read_text())=={'instance_id':'example__project-42','body':'frozen'}
    with pytest.raises(Exception,match='within evidence/workflows'):
        asyncio.run(O.read_candidate_source_activity({**source,'candidates_path':'/etc/passwd'}))


def test_client_repeated_request_requires_same_actor_and_parameters(monkeypatch):
    from types import SimpleNamespace
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from swesim.workflows import client
    import hashlib
    request={'action':'label','params':{'trial_id':'a'*24,'label':'no-question','note':''},'actor':'reviewer@example.com'}
    binding=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    async def memo():
        return {'request_sha256':binding}
    async def describe():
        return SimpleNamespace(memo=memo)
    async def result():
        return {'ok':True,'saved':True}
    handle=SimpleNamespace(describe=describe,result=result)
    async def start(*args,**kwargs):
        raise WorkflowAlreadyStartedError(kwargs['id'],'OperatorActionWorkflow')
    async def connect():
        return SimpleNamespace(start_workflow=start,get_workflow_handle=lambda wid:handle)
    monkeypatch.setattr(client,'connect',connect)
    assert asyncio.run(client.operator_action(request,'e'*64))['saved'] is True
    with pytest.raises(ValueError,match='different'):
        asyncio.run(client.operator_action({**request,'actor':'other@example.com'},'e'*64))


def test_create_workflow_routes_frozen_retry_without_discovery(monkeypatch):
    from swesim.workflows import workflows as W
    calls=[]
    async def act(name,p,*args,**kwargs):
        calls.append(name)
        if name=='read_candidate_source_activity':
            return {'source_key':p['source_key'],'candidates_path':'/workspace/evidence/workflows/retry/source.jsonl','written':1}
        if name=='prepare_candidate_plan_activity':
            return {'plan_path':'plan','total':0}
        if name=='read_plan_page_activity':
            return {'items':[],'next_offset':0,'total':0}
        if name=='checkpoint_workflow_activity':
            return {'checkpoint_path':'checkpoint'}
        return {}
    monkeypatch.setattr(W,'act',act)
    p={'sources':[{'kind':'frozen-candidate','source_key':'github-repo--example__project--bugfix',
                   'instance_id':'example__project-42','candidates_path':'/workspace/evidence/workflows/original/source.jsonl'}]}
    asyncio.run(W.CreateTasksWorkflow().run(p))
    assert 'read_candidate_source_activity' in calls
    assert 'discover_activity' not in calls and 'mine_activity' not in calls
