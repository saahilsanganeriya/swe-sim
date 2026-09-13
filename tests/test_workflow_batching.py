"""Pagination and workflow checkpoints keep histories and payloads bounded."""

import pytest

from swesim.paths import Workspace
from swesim.workflows import batching as B
from swesim.workflows import payloads as P


def test_plan_pages_and_checkpoint_retries_are_immutable(tmp_path):
    ws = Workspace(tmp_path)
    items = [{'trial_id': f'{i:024x}', 'scenario_id': 'example', 'model': 'm', 'condition': 'hidden', 'attempt_index': i,
              'run_config': {'purpose': 'comparison', 'large_config': 'x' * 10000}} for i in range(251)]
    path = B.plan_path(ws, 'run-example', 'scenario')
    prepared = B.trial_plan(ws, path, items)
    assert prepared['total'] == 251
    seen = []
    for offset, expected in ((0, 100), (100, 100), (200, 51)):
        page = B.read_page(ws, str(path), offset)
        assert len(page['items']) == expected
        assert all('run_config' not in item for item in page['items'])
        P.check_payload(page)
        seen.extend(item['trial_id'] for item in page['items'])
    assert seen == [item['trial_id'] for item in items]
    state = {'cursor': 100, 'plan_path': str(path), 'summary': {'completed': 100}}
    checkpoint = B.checkpoint(ws, 'run-example', 'first-run', state)
    assert B.checkpoint(ws, 'run-example', 'first-run', state) == checkpoint
    with pytest.raises(ValueError, match='differs'):
        B.checkpoint(ws, 'run-example', 'first-run', {**state, 'cursor': 101})
    with pytest.raises(ValueError, match='1 <= limit <= 100'):
        B.read_page(ws, str(path), 0, 101)
    with pytest.raises(ValueError, match='within evidence'):
        B.read_page(ws, '/tmp/outside-plan.json')


def test_payload_limit_uses_encoded_bytes_and_rejects_nonfinite():
    P.check_payload({'small': 'ok'})
    with pytest.raises(ValueError, match='below 256 KB'):
        P.check_payload({'large': 'x' * (256 * 1024)})
    with pytest.raises(ValueError, match='below 256 KB'):
        P.check_payload({'unicode': '\U0001f600' * 25000})
    with pytest.raises(ValueError):
        P.check_payload({'reward': float('nan')})


@pytest.mark.parametrize('kind', ['create', 'run'])
def test_real_temporal_continues_after_100_children_without_repeating_plans(kind):
    from temporalio import activity
    from temporalio.api.enums.v1 import EventType
    from test_workflows import _create_fakes, _run_in_env, workers

    from swesim.workflows import queues as Q
    from swesim.workflows import workflows as W
    launches = []; plans = []; checkpoints = []
    @activity.defn(name='checkpoint_workflow_activity')
    async def checkpoint(p):
        P.check_payload(p)
        checkpoints.append(p)
        return {'checkpoint_path': '/checkpoints/' + p['run_id']}
    if kind == 'create':
        items = {'/c/A.jsonl': [f'a__{i}' for i in range(101)]}
        acts = _create_fakes({'cur': 0, 'max': 0}, launches, validate_invalid=(), judge_error=(), judge_drop=(), lists=items, not_reproducible=())
        acts.append(checkpoint)
        params = {'sources': [{'kind': 'swebench-annotated', 'pool': 'A'}], 'judge_model': 'j', 'draft_model': 'd', 'max_parallel': 20}
        wf = W.CreateTasksWorkflow; children = W.CandidateWorkflow
    else:
        @activity.defn(name='plan_trials_activity')
        async def plan(p):
            plans.append(p)
            return {'trials': [{'trial_id': f't{i}', 'scenario_id': 'scenario', 'model': 'm', 'condition': 'hidden', 'attempt_index': i} for i in range(101)]}
        @activity.defn(name='launch_trial_activity')
        async def launch(p):
            launches.append(p['trial_id']); return {'launched': True}
        @activity.defn(name='await_trial_activity')
        async def finish(p): return {'trial_id': p['trial_id'], 'status': 'completed'}
        @activity.defn(name='score_activity')
        async def score(p): return {'scores_path': '/scores'}
        @activity.defn(name='export_activity')
        async def export(p): return {'manifest_paths': []}
        acts = [plan, launch, finish, score, export, checkpoint]
        params = {'scenario_ids': ['scenario'], 'models': ['m'], 'max_parallel_trials': 20}
        wf = W.RunTasksWorkflow; children = W.TrialWorkflow
    async def scenario(env):
        async with workers(env, workflows=[wf, children], activities=acts):
            handle = await env.client.start_workflow(wf.run, params, id='batch-' + kind, task_queue=Q.AUTHORING)
            result = await handle.result()
            assert result['status'] == 'complete'
            assert len(launches) == len(set(launches)) == 101
            assert len(checkpoints) == 2
            key = 'candidates' if kind == 'create' else 'trials'
            assert [len(c['state'][key]) for c in checkpoints] == [100, 1]
            assert checkpoints[1]['state']['previous_checkpoint'] == '/checkpoints/' + checkpoints[0]['run_id']
            history = await env.client.get_workflow_handle('batch-' + kind, run_id=checkpoints[0]['run_id']).fetch_history()
            for event in history.events:
                payloads = []
                if event.HasField('activity_task_scheduled_event_attributes'):
                    payloads = event.activity_task_scheduled_event_attributes.input.payloads
                elif event.HasField('activity_task_completed_event_attributes'):
                    payloads = event.activity_task_completed_event_attributes.result.payloads
                assert all(payload.ByteSize() < P.MAX_PAYLOAD_BYTES for payload in payloads)
            started = [e for e in history.events if e.event_type == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED]
            assert len(started) == 100
            assert history.events[-1].event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW
            if kind == 'run':
                assert len(plans) == 1 and result['summary']['children'] == 101
            else:
                assert result['counts']['valid'] == 101
    _run_in_env(scenario)


def test_search_attributes_are_versioned_on_child_starts(monkeypatch):
    from swesim.workflows import workflows as W
    params = {'scenario_id': 'scenario', 'model': 'm', 'condition': 'hidden', 'purpose': 'screen', 'version': 2}
    attrs = W._child_options(params)['search_attributes']
    assert {pair.key.name: pair.value for pair in attrs} == {'swesim_scenario_id': 'scenario', 'swesim_model': 'm', 'swesim_condition': 'hidden',
                                                           'swesim_purpose': 'screen', 'swesim_version': '2', 'swesim_status': 'running'}
    monkeypatch.setattr(W, '_patched', lambda name: False)
    assert W._child_options(params) == {}


def test_threaded_launch_finishes_before_cancellation_stops_runtime():
    import asyncio
    import threading

    from temporalio import activity
    from temporalio.client import WorkflowFailureError
    from test_workflows import _run_in_env, workers

    from swesim.workflows import activities as A
    from swesim.workflows import queues as Q
    from swesim.workflows import workflows as W
    entered = threading.Event(); release = threading.Event(); order = []
    def launch_thread():
        entered.set()
        assert release.wait(timeout=10)
        order.append('created')
        return {'launched': True}
    @activity.defn(name='launch_trial_activity')
    async def launch(p): return await A.blocking(launch_thread, every=0.01)
    @activity.defn(name='cancel_trial_activity')
    async def cancel(p):
        order.append('cancelled'); return {'cancelled': True}
    @activity.defn(name='await_trial_activity')
    async def await_trial(p): return {'trial_id': p['trial_id'], 'status': 'completed'}
    async def scenario(env):
        async with workers(env, workflows=[W.TrialWorkflow], activities=[launch, cancel, await_trial]):
            h = await env.client.start_workflow(W.TrialWorkflow.run, {'trial_id': 'a' * 24}, id='cancel-thread', task_queue=Q.AUTHORING)
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            await h.cancel()
            await asyncio.sleep(0.05)
            assert order == []
            release.set()
            with pytest.raises(WorkflowFailureError):
                await h.result()
            assert order == ['created', 'cancelled']
    try:
        _run_in_env(scenario)
    finally:
        release.set()


def test_rebuild_preserves_original_assets_and_all_compiled_variants(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from test_compile import _materialize

    from swesim.models import RunConfig, minimal_spec_dict
    from swesim.paths import atomic_json
    from swesim.stages.compile import compile_scenario, verify_assets, verify_compiled_artifacts
    from swesim.workflows import activities as A
    ws, spec = _materialize(tmp_path, minimal_spec_dict())
    monkeypatch.setattr(A, 'WS', ws)
    result = compile_scenario(ws, spec, RunConfig.default('full'), 'https://example.test', resolve_digest=False)
    atomic_json(ws.scenario_dir(spec.id) / 'scenario.json', spec.model_dump(mode='json'))
    compiled = json.loads(result.compile_json.read_text())
    archived = A.archive_scenario(spec.id, 1)
    archive = Path(archived['archive_dir'])
    # A subsequent draft/compiler can replace every live file; the old spec still verifies against its archive.
    for asset in spec.assets.values():
        (ws.scenario_dir(spec.id) / asset.path).write_text('new version')
    for condition in ('full', 'hidden', 'interactive'):
        (ws.task_dir(spec.id, condition) / 'instruction.md').write_text('new version')
        verify_compiled_artifacts(archive / 'tasks' / condition, compiled, condition)
    verify_assets(archive, spec)
    again = A.archive_scenario(spec.id, 1)
    assert again['already_archived'] is True
    verify_assets(archive, spec)
    assert Path(archived['snapshot_path']).is_file()


def test_archive_recovers_interrupted_file_copy_without_directory_rename(tmp_path, monkeypatch):
    from swesim.paths import Workspace, atomic_json
    from swesim.workflows import activities as A
    from swesim.workflows import archive
    ws = Workspace(tmp_path); monkeypatch.setattr(A, 'WS', ws)
    sid = 'example'; sdir = ws.scenario_dir(sid)
    (sdir / 'assets').mkdir(parents=True)
    (sdir / 'assets' / 'a.diff').write_text('a')
    (sdir / 'assets' / 'b.diff').write_text('b')
    atomic_json(sdir / 'scenario.json', {'version': 1})
    original = archive.copy_file; count = 0
    def interrupted(source, destination):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError('worker interrupted during copy')
        return original(source, destination)
    monkeypatch.setattr(archive, 'copy_file', interrupted)
    with pytest.raises(OSError):
        A.archive_scenario(sid, 1)
    target = sdir / 'archive' / 'v1'
    assert (sdir / 'scenario.json').exists() and (target / 'assets' / 'a.diff').read_text() == 'a'
    assert not (target / 'snapshot.json').exists()
    monkeypatch.setattr(archive, 'copy_file', original)
    real_replace = archive.os.replace
    def files_only(source, destination):
        assert not __import__('pathlib').Path(source).is_dir()
        return real_replace(source, destination)
    monkeypatch.setattr(archive.os, 'replace', files_only)
    A.archive_scenario(sid, 1)
    assert (target / 'assets' / 'b.diff').read_text() == 'b' and (target / 'snapshot.json').exists()
    assert not (sdir / 'scenario.json').exists()


def test_automatic_audits_run_before_index_and_keep_grader_evidence(monkeypatch):
    import asyncio

    from swesim.workflows import workflows as W
    calls = []
    async def act(name, p, *args, **kwargs):
        calls.append((name, p))
        if name == 'verify_approval_activity': return {'scenario_id': 's', 'spec_hash': 'h'}
        if name == 'validate_activity': return {'status': 'valid'}
        if name.startswith('audit_'):
            assert p['audit_model'] == 'openai/gpt-5.6-sol'
            raise RuntimeError('audit provider unavailable')
        return {}
    async def stage(*args, **kwargs): pass
    monkeypatch.setattr(W, 'act', act)
    async def scenario():
        built = await W.build_scenario(stage, {'audit_model': 'openai/gpt-5.6-sol'}, 's', 'h')
        assert built['status'] == 'valid' and built['audit']['status'] == 'pending'
        assert [name for name, _ in calls][-2:] == ['register_activity', 'audit_scenario_activity']
        calls.clear()
        result = {'status': 'completed', 'reward': {'reward': 1.0, 'success': 1, 'outcome_code': 1}}
        original = {**result, 'reward': dict(result['reward'])}
        book = await W.TrialWorkflow()._bookkeeping({'trial_id': 'a' * 24, 'condition': 'interactive', 'audit_model': 'openai/gpt-5.6-sol'}, result)
        names = [name for name, _ in calls]
        assert names.index('audit_transcript_activity') < names.index('index_trial_activity')
        assert book['audit']['status'] == 'pending' and result == original
    asyncio.run(scenario())
