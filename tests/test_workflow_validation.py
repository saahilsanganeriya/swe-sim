"""Split controls retain the actual reports and use the legacy verdict rules."""
import asyncio
import json
from pathlib import Path

import pytest
from test_validate import CLEAN_PROBE, GOOD, _fake_compile, _fake_harbor, _spec_ws

from swesim import harbor_cli
from swesim.models import RunConfig
from swesim.workflows import activities as A
from swesim.workflows import validation as V
from swesim.workflows import workflows as W


@pytest.mark.parametrize('bad', [False, True])
def test_split_controls_match_legacy_evidence(tmp_path, monkeypatch, bad):
    ws, spec = _spec_ws(tmp_path)
    compiler = _fake_compile(tmp_path, spec)
    compiled = compiler(ws, spec, RunConfig.default('full'), 'https://example.test')
    (ws.scenario_dir(spec.id) / 'compile.json').write_text(json.dumps({'compiled_hash': 'c' * 64, 'spec_hash': spec.spec_hash}))
    monkeypatch.setattr(V, 'verify_compiled_artifacts', lambda *args: None)
    monkeypatch.setattr(ws.__class__, 'task_dir', lambda self, sid, cond: compiled.task_dirs[cond])
    rewards = json.loads(json.dumps(GOOD))
    if bad:
        rewards['tamper-root-conftest']['outcome_code'] = 7
        rewards['nop']['per_test'] = []
    run, launches = _fake_harbor(ws.jobs_dir, rewards, CLEAN_PROBE)
    monkeypatch.setattr(harbor_cli, 'run_task', run)
    paths = {name: V.run_control(ws, spec, name, 'c' * 64)['trial_dir'] for name in V.control_names(spec)}
    result = V.assemble_validation(ws, spec, paths, 'c' * 64, compiled.image_digest, RunConfig.default('full').run_config_hash)
    monkeypatch.setattr(V.V, 'compile_scenario', compiler)
    # This parity fixture isolates verdict assembly; manifest binding has independent compile/control tests.
    monkeypatch.setattr(V.V, 'verify_compiled_artifacts', lambda *args: None, raising=False)
    monkeypatch.setattr(harbor_cli, 'run_task', lambda task, agent, jobs, job_name, **kw: jobs / job_name / 'trial')
    legacy = V.V.validate_scenario(ws, spec, RunConfig.default('full'), 'https://example.test')
    assert {k: v for k, v in result.items() if k != 'validated_by'} == legacy
    assert result['status'] == ('invalid' if bad else 'valid')
    assert result['expected_gold'] == {'t1': 'passed', 't0': 'passed'}
    assert len(launches) == len(V.control_names(spec))
    assert all('report' not in c for c in result['controls'].values())
    assert all((Path(path) / 'verifier' / 'report.json').exists() for name, path in paths.items() if name != 'probe')


def test_one_control_failure_propagates_without_fabricated_verdict(tmp_path, monkeypatch):
    ws, spec = _spec_ws(tmp_path)
    comp = _fake_compile(tmp_path, spec)(ws, spec, RunConfig.default('full'), 'https://example.test')
    (ws.scenario_dir(spec.id) / 'compile.json').write_text(json.dumps({'compiled_hash': 'c' * 64, 'spec_hash': spec.spec_hash}))
    monkeypatch.setattr(V, 'verify_compiled_artifacts', lambda *args: None)
    monkeypatch.setattr(ws.__class__, 'task_dir', lambda self, sid, cond: comp.task_dirs[cond])
    def failed(*args, **kwargs):
        raise harbor_cli.HarborRunError('lost Modal acknowledgement')
    monkeypatch.setattr(harbor_cli, 'run_task', failed)
    with pytest.raises(harbor_cli.HarborRunError):
        V.run_control(ws, spec, 'nop', 'c' * 64)
    assert not (ws.scenario_dir(spec.id) / 'validation.json').exists()


def test_per_control_workflow_carries_only_paths_to_verdict(monkeypatch):
    calls = []; progress = []
    async def act(name, p, *args, **kwargs):
        calls.append((name, p))
        if name == 'validate_control_activity':
            return {'trial_dir': '/trials/' + p['control'], 'reward': 0, 'outcome_code': 2}
        return {'status': 'valid'}
    async def status(name, state):
        progress.append((name, state))
    monkeypatch.setattr(W, 'act', act)
    result = asyncio.run(W.validate_per_control({'scenario_id': 'example'}, ['nop', 'oracle'], status))
    assert result['status'] == 'valid'
    assert calls[-1] == ('evaluate_controls_activity', {'scenario_id': 'example', 'trial_dirs': {'nop': '/trials/nop', 'oracle': '/trials/oracle'}})
    assert {n for n, s in progress if s == 'running'} == {'nop', 'oracle'}


def test_blocking_observes_cancellation_before_launch_and_between_stages(monkeypatch):
    called = []
    monkeypatch.setattr(A, '_cancelled', lambda: True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(A.blocking(lambda: called.append('launched')))
    assert called == []
    checks = iter([False, True])
    monkeypatch.setattr(A, '_cancelled', lambda: next(checks))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(A.blocking(lambda: called.append('first stage')))
    assert called == ['first stage']


def test_cancelling_trial_stops_runtime_once_without_relaunch(monkeypatch):
    names = []
    async def act(name, *args, **kwargs):
        names.append(name)
        if name == 'await_trial_activity':
            raise asyncio.CancelledError()
        return {'launched': True, 'cancelled': True}
    monkeypatch.setattr(W, 'act', act)
    w = W.TrialWorkflow()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(w.run({'trial_id': 'a' * 24}))
    assert names == ['launch_trial_activity', 'await_trial_activity', 'cancel_trial_activity', 'index_trial_activity']
    assert w.state['stage'] == 'cancelled'


def test_discovery_writes_unprobed_rows_and_reject_funnel(tmp_path, monkeypatch):
    from swesim.paths import Workspace
    from swesim.sources import github_repo as github
    ws = Workspace(tmp_path / 'workspace')
    monkeypatch.setattr(A, 'WS', ws)
    from swesim.sources import observations as obs
    seen = []
    def discover(repo, since, kind='feature', *, observations=None, **_):
        # discover_scan passes max_prs/pr/observations and derives the reject histogram from the observations it records
        seen.append((repo, since, kind)); observations = observations if observations is not None else []
        cands = [{'base_commit': 'abc', 'instance_id': 'x__y-1'}, {'base_commit': 'def', 'instance_id': 'x__y-2'}]
        for i, c in enumerate(cands):
            c['_observation_seq'] = i
            observations.append(obs.DiscoveryPRObservation(seq=i, pr_number=i + 1, pr_url=f'https://github.com/x/y/pull/{i + 1}',
                                                           stages=['listed', 'merged-in-range', 'linked-issue', 'band-passed', 'retained'], status='retained'))
        for _ in range(3):
            seq = len(observations)
            observations.append(obs.DiscoveryPRObservation(seq=seq, pr_number=seq + 1, pr_url=f'https://github.com/x/y/pull/{seq + 1}',
                                                           stages=['listed', 'merged-in-range', 'linked-issue'], status='rejected', reason='too-small'))
        return cands, {'too-small': 3}
    monkeypatch.setattr(github, 'discover', discover)
    monkeypatch.setattr(github, 'candidate_to_row', lambda c: {**c, 'patch': 'large source patch stays on disk', 'fail_to_pass': []})
    monkeypatch.setattr(github, 'build_recipe', lambda *args: {'repo_dir': '/app'})
    monkeypatch.setattr(harbor_cli, 'run_task', lambda *args, **kw: pytest.fail('discovery cannot probe'))
    result = asyncio.run(A.discover_activity({'repo': 'https://github.com/x/y', 'kind': 'bugfix', 'max_candidates': 1}))
    assert seen == [('x/y', '2026-06-01', 'bugfix')]
    assert result['written'] == 1 and result['discovered'] == 2
    assert result['rejects'] == {'too-small': 3, 'max-candidates-cut': 1}
    rows = [json.loads(line) for line in Path(result['candidates_path']).read_text().splitlines()]
    assert rows[0]['fail_to_pass'] == [] and rows[0]['repo_dir'] == '/app'
    assert 'patch' not in result


def test_control_refuses_unbound_compile_before_any_harbor_launch(tmp_path, monkeypatch):
    ws, spec = _spec_ws(tmp_path)
    comp = _fake_compile(tmp_path, spec)(ws, spec, RunConfig.default('full'), 'https://example.test')
    (ws.scenario_dir(spec.id) / 'compile.json').write_text(json.dumps({'compiled_hash': 'c' * 64, 'spec_hash': spec.spec_hash}))
    monkeypatch.setattr(ws.__class__, 'task_dir', lambda self, sid, cond: comp.task_dirs[cond])
    monkeypatch.setattr(harbor_cli, 'run_task', lambda *args, **kwargs: pytest.fail('unbound artifact reached Harbor'))
    with pytest.raises(ValueError, match='manifest|compiler|build identity'):
        V.run_control(ws, spec, 'nop', 'c' * 64)


def test_discovery_bands_and_immutable_snapshots_do_not_overwrite_history(tmp_path, monkeypatch):
    import hashlib

    from swesim.paths import Workspace
    from swesim.sources import github_repo as github
    ws = Workspace(tmp_path); monkeypatch.setattr(A, 'WS', ws)
    from swesim.sources import observations as obs
    records = {'rejects': {'too-small': 3}}
    def fake_discover(repo, since, *args, observations=None, **kwargs):
        observations = observations if observations is not None else []
        for reason, n in records['rejects'].items():   # the scan derives the histogram from recorded decisions
            for _ in range(n):
                seq = len(observations)
                observations.append(obs.DiscoveryPRObservation(seq=seq, pr_number=seq + 1, pr_url=f'https://github.com/x/y/pull/{seq + 1}',
                                                               stages=['listed', 'merged-in-range', 'linked-issue'], status='rejected', reason=reason))
        return [], dict(records['rejects'])
    monkeypatch.setattr(github, 'discover', fake_discover)
    first = asyncio.run(A.discover_activity({'repo': 'x/y', 'kind': 'bugfix'}))
    records['rejects'] = {'no-tests': 7}
    second = asyncio.run(A.discover_activity({'repo': 'x/y', 'kind': 'bugfix'}))
    feature = asyncio.run(A.discover_activity({'repo': 'x/y', 'kind': 'feature'}))
    assert first['source_key'] == second['source_key'] == 'github-repo--x__y--bugfix'
    assert feature['source_key'] == 'github-repo--x__y--feature'
    assert first['rejects_path'] != second['rejects_path']
    assert first['canonical_candidates_path'] != feature['canonical_candidates_path']
    assert json.loads(Path(first['rejects_path']).read_text()) == {'too-small': 3}
    assert hashlib.sha256(Path(first['rejects_path']).read_bytes()).hexdigest() == first['rejects_sha256']
    assert Path(first['canonical_candidates_path']).with_suffix('.status').read_text() == 'no-valid-candidate'
