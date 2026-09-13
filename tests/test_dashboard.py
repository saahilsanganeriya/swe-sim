"""Task 14: dashboard runs table, episode page, merged timeline, live transcript, labels. Offline (httpx monkeypatched)."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import swesim.dashboard.app as dash
from swesim.dashboard.app import app, merge_timeline
from swesim.stages.score import score_scenario

try:  # pytest's default (prepend) import mode puts tests/ on sys.path
    from test_score import (
        HAIKU,
        T0,
        TRAJ,
        ask_events,
        make_spec,
        make_workspace,
        review_events,
        write_trial,
        write_unresolved,
    )
except ImportError:  # pragma: no cover - importlib mode
    from tests.test_score import (
        HAIKU,
        T0,
        TRAJ,
        ask_events,
        make_spec,
        make_workspace,
        review_events,
        write_trial,
        write_unresolved,
    )


def test_merge_orders_by_time():
    traj = {'steps': [{'timestamp': '2026-09-13T10:00:05Z', 'source': 'agent', 'message': 'reading'}]}
    t = datetime.fromisoformat('2026-09-13T10:00:00+00:00').timestamp()
    transcript = {'events': [{'kind': 'message', 'ts': t, 'role': 'user', 'content': 'Which exception?', 'exchange': 'ask'},
                             {'kind': 'message', 'ts': t + 3, 'role': 'assistant', 'content': 'ValueError.', 'tool_calls': [{'tool': 'read_diff'}]}]}
    rows = merge_timeline(traj, transcript)
    assert [r['actor'] for r in rows] == ['agent', 'stakeholder', 'stakeholder', 'agent']
    assert [r['ts'] for r in rows] == [t, t + 3, t + 3, t + 5]
    assert [r['kind'] for r in rows] == ['ask', 'ask', 'tool_call', 'agent-step']
    assert rows[2]['text'] == json.dumps({'tool': 'read_diff'}) and rows[3]['text'] == 'reading'
    assert all(set(r) >= {'ts', 'actor', 'kind', 'text', 'meta'} for r in rows)


def test_merge_flags_missing_timestamps_and_adds_verifier_row():
    traj = {'steps': [{'source': 'user', 'message': 'Fix it.'},  # harness prompt without a timestamp
                      {'timestamp': '2026-09-13T10:00:05', 'source': 'agent', 'message': 'ok'},  # naive -> UTC
                      {'timestamp': '2026-09-13T10:00:01+00:00', 'source': 'system', 'message': 'sys'}]}
    rows = merge_timeline(traj, {'events': [{'kind': 'session', 'ts': 1.0}]},
                          report={'outcome': 'valid-correct', 'per_group': []}, reward={'reward': 1.0, 'success': 1, 'outcome_code': 1},
                          status={'status': 'finished', 'finished_at': '2026-09-13T10:05:00Z'})
    assert [r['actor'] for r in rows] == ['harness', 'agent', 'verifier', 'harness']
    assert rows[0]['kind'] == 'system' and rows[3]['kind'] == 'prompt' and rows[3]['ts'] is None and rows[3]['no_timestamp'] is True
    assert rows[1]['ts'] == datetime.fromisoformat('2026-09-13T10:00:05+00:00').timestamp() and rows[1]['no_timestamp'] is False
    assert rows[2]['kind'] == 'outcome' and 'valid-correct' in rows[2]['text'] and rows[2]['meta']['reward'] == {'reward': 1.0, 'success': 1, 'outcome_code': 1}
    # non-message transcript events never become rows; a verifier row without a timestamp is flagged too
    v = merge_timeline({}, {}, report={'outcome': 'valid-incorrect'}, status={'status': 'completed'})
    assert len(v) == 1 and v[0]['ts'] is None and v[0]['no_timestamp'] is True
    assert merge_timeline({}, {}) == [] and merge_timeline({}, {}, report={}) == []


@pytest.fixture
def site(tmp_path, monkeypatch):
    spec = make_spec(with_author_only=True)
    ws = make_workspace(tmp_path, spec)
    q = ask_events(T0 + 6, 'Which exception should be raised?', 'ValueError.', ['f01'], 5)
    done = write_trial(ws, spec, events=q + review_events(T0 + 30, 3), label={'label': 'unresolved', 'note': 'first look'})
    wrong = write_trial(ws, spec, attempt=1, outcome_code=2, success=0, reward=0.0)
    full = write_trial(ws, spec, condition='full', purpose='comparison')
    running = 'ab' * 12
    rdir = ws.root / 'trials' / running
    (rdir / 'task').mkdir(parents=True)
    (rdir / 'task' / 'instruction.md').write_text('# Task\n\nIt breaks.\n')
    (rdir / 'status.json').write_text(json.dumps({'status': 'running', 'trial_id': running}))
    (rdir / 'request.json').write_text(json.dumps({'trial_id': running, 'scenario_id': spec.id, 'condition': 'interactive',
                                                  'model': HAIKU, 'run_config': {'purpose': 'calibration', 'brief_profile': 'withheld',
                                                                                 'stakeholder': {'patience_profile': {'start': 6}}}}))
    score_scenario(ws, spec.id, k=3)
    ws.registry_path.write_text(json.dumps({'scenarios': {spec.id: {
        'id': spec.id, 'version': 1, 'spec_hash': spec.spec_hash, 'source': 'authored', 'source_ref': 'example',
        'validation_status': 'valid', 'validation_path': str(ws.scenario_dir(spec.id) / 'validation.json'),
        'conditions': ['full', 'hidden', 'interactive'], 'compiled_hash': 'c' * 64, 'image_digest': 'sha256:' + 'd' * 64,
        'calibration_runs': [done, wrong], 'comparison_runs': [full], 'demonstration_runs': []}}}))
    monkeypatch.setattr(dash, 'ROOT', ws.root)
    monkeypatch.setattr(dash, 'REGISTRY', ws.registry_path)
    monkeypatch.delenv('SWESIM_STAKEHOLDER_URL', raising=False)
    monkeypatch.delenv('SWESIM_WORKER_SECRET', raising=False)
    monkeypatch.setenv('SWESIM_ENV_FILE', str(tmp_path / 'absent.env'))  # never load the real env file in tests
    monkeypatch.setattr(dash.httpx, 'get', lambda *a, **k: pytest.fail('network call for a finished trial'))
    monkeypatch.setattr(dash.data, '_live_cache', {})
    return {'ws': ws, 'spec': spec, 'done': done, 'wrong': wrong, 'full': full, 'running': running, 'client': TestClient(app)}


# ---------------------------------------------------------------- redesign: overview API, runs, scenario, empty states, XSS, static

def test_api_overview_shape_and_live_patience_with_cache(site, monkeypatch):
    c, tid = site['client'], site['running']
    ov = c.get('/api/overview').json()
    assert set(ov) == {'generated_at', 'now', 'summary', 'scenarios', 'trials'}
    assert ov['summary']['scenarios'] == 1 and ov['summary']['trials'] == 4 and ov['summary']['running'] == 1 and ov['summary']['finished'] == 3
    s = ov['scenarios'][0]
    assert s['id'] == site['spec'].id and [x['stage'] for x in s['stages']] == ['candidate', 'judged', 'drafted', 'approved', 'compiled', 'validated', 'registered']
    assert {x['stage']: x['state'] for x in s['stages']}['validated'] == 'done' and {x['stage']: x['state'] for x in s['stages']}['registered'] == 'done'
    assert {x['stage']: x['state'] for x in s['stages']}['judged'] == 'pending' and s['source_label'].startswith('authored')
    assert s['facts'] == 2 and s['controls'] == {'oracle': 'oracle.diff', 'plausible_wrong': 1, 'alternative_correct': 0, 'tamper': 1}
    assert s['scores']['groups'] and s['validation_status'] == 'valid' and s['registered'] is True
    running = next(t for t in ov['trials'] if t['trial_id'] == tid)
    assert running['running'] is True and running['status'] == 'running' and running['elapsed_s'] is None  # no started_at in that fixture
    assert running['patience_start'] == 6 and running['live_error'] == 'SWESIM_STAKEHOLDER_URL not configured'
    done = next(t for t in ov['trials'] if t['trial_id'] == site['done'])
    assert done['outcome'] == 'valid-correct' and done['reward'] == 1.0 and done['elapsed_s'] == 360.0 and done['patience_end'] == 3
    assert done['questions'] == 1 and done['reviews'] == 1 and done['label'] == 'unresolved'
    # live patience for running trials comes from the stakeholder service, at most once per trial per cache TTL
    # (errors are cached too, so a dead stakeholder cannot slow every poll; clear the "not configured" entry first)
    dash.data._live_cache.clear()
    calls = []

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {'events': ask_events(T0, 'q?', 'a.', ['f01'], 4), 'patience_start': 6, 'patience_end': 4}

    monkeypatch.setattr(dash.httpx, 'get', lambda url, headers=None, timeout=None, **k: (calls.append(url), Resp())[1])
    monkeypatch.setenv('SWESIM_STAKEHOLDER_URL', 'https://stakeholder.example.test')
    monkeypatch.setenv('SWESIM_WORKER_SECRET', 'secret-value')
    running = next(t for t in c.get('/api/overview').json()['trials'] if t['trial_id'] == tid)
    assert running['patience_end'] == 4 and running['questions'] == 1 and running['live_error'] is None
    assert calls == [f'https://stakeholder.example.test/transcript/{tid}']
    c.get('/api/overview')
    assert len(calls) == 1  # cached
    assert 'secret-value' not in json.dumps(c.get('/api/overview').json())
    assert c.get('/').status_code == 200 and len(calls) == 1  # the page itself never blocks on the stakeholder


def test_runs_page_has_filters_chart_and_score_table(site):
    html = site['client'].get('/runs').text
    for f in ('scenario', 'condition', 'model', 'status'):
        assert f"data-filter='{f}'" in html
    assert "<option value='interactive'>" in html and f"<option value='{HAIKU}'>" in html and "<option value='running'>" in html
    assert '<svg class=' in html and 'aria-label=' in html and 'pass@k' in html and 'pass^k' in html
    assert "<g class='bar s1'" in html or "<g class='bar na'" in html  # one series: bars or n/a markers, no legend box
    assert "class='legend'" not in html
    assert 'calibration · withheld brief · k=3' in html and 'comparison · natural brief · k=3' in html  # one facet per purpose/profile
    assert 'groups are never pooled' in html
    for tid in (site['done'], site['wrong'], site['full'], site['running']):
        assert f'data-id=\'{tid}\'' in html and f"href='/trial/{tid}'" in html
    assert 'unknown-config' in html  # the running fixture has a partial run_config: attributable, unresolved, no run config hash


def test_chart_draws_every_group_and_labels_k_per_facet():
    from swesim.dashboard.charts import pass_at_k_svg
    groups = []
    for purpose, profile, k in (('calibration', 'withheld', 1), ('comparison', 'withheld', 3), ('comparison', 'natural', 3)):
        for cond in ('full', 'hidden', 'interactive'):
            for i, model in enumerate(('a/one', 'b/two')):
                groups.append({'condition': cond, 'model': model, 'purpose': purpose, 'brief_profile': profile, 'requested_k': k,
                               'pass_at_k': 0.5 if i else None, 'n_valid': 3, 'n_total': 3, 'run_config_hash': f'{purpose}{profile}{cond}{i}' * 4})
    svg = pass_at_k_svg(groups)
    assert svg.count("<g class='bar") == 18 and svg.count('<svg') == 3  # every group drawn, one facet per (purpose, profile)
    assert 'calibration · withheld brief · k=1' in svg and 'comparison · withheld brief · k=3' in svg and 'comparison · natural brief · k=3' in svg
    assert 'k in {1, 3}' in svg and '18 of 18 group(s) drawn' in svg and 'never pooled' in svg
    assert svg.count("class='legend'") == 1 and 'a/one' in svg and 'b/two' in svg  # two series: legend present
    assert svg.count("class='bar s2'") == 9 and svg.count("class='bar na'") == 9  # b/two has values, a/one is n/a everywhere
    # two groups sharing a slot inside a facet (a rebuild) stay separate bars and are called out
    dup = groups[:1] + [{**groups[0], 'run_config_hash': 'z' * 64, 'pass_at_k': 0.9}]
    svg2 = pass_at_k_svg(dup)
    assert svg2.count("<g class='bar") == 2 and 'hold more than one group' in svg2 and 'build z' in svg2
    assert 'No scored groups yet' in pass_at_k_svg([]) and 'role=\'img\'' not in svg  # focusable bars inside an svg: no role=img


def test_api_overview_live_fetch_stays_within_budget(site, monkeypatch):
    import time as _time
    c, tid = site['client'], site['running']
    dash.data._live_cache.clear()

    class Slow:
        def raise_for_status(self):
            return None

        def json(self):
            return {'events': [], 'patience_start': 6, 'patience_end': 2}

    def slow_get(url, headers=None, timeout=None, **k):
        _time.sleep(0.6)
        return Slow()

    monkeypatch.setattr(dash.httpx, 'get', slow_get)
    monkeypatch.setenv('SWESIM_STAKEHOLDER_URL', 'https://stakeholder.example.test')
    monkeypatch.setenv('SWESIM_WORKER_SECRET', 's')
    monkeypatch.setattr(dash.data, 'LIVE_BUDGET', 0.15)
    t0 = _time.monotonic()
    ov = c.get('/api/overview').json()
    assert _time.monotonic() - t0 < 0.5  # the poll returns within the budget even though the stakeholder is slow
    row = next(t for t in ov['trials'] if t['trial_id'] == tid)
    assert row['live_error'] == 'live fetch budget exceeded' and row['patience_end'] is None
    _time.sleep(0.7)  # the background fetch finished and filled the cache: the next poll is served from it
    row = next(t for t in c.get('/api/overview').json()['trials'] if t['trial_id'] == tid)
    assert row['patience_end'] == 2 and row['live_error'] is None


def test_post_routes_reject_cross_origin_requests(site):
    c, tid = site['client'], site['wrong']
    evil = {'Origin': 'http://evil.example'}
    assert c.post(f'/trial/{tid}/label', json={'label': 'unresolved'}, headers=evil).status_code == 403
    assert c.post('/api/create', json={'source': 'github-repo', 'repos': 'a/b'}, headers=evil).status_code == 403
    assert c.post('/api/run', json={}, headers=evil).status_code == 403
    assert c.post('/api/jobs/x/cancel', json={'confirm': True}, headers=evil).status_code == 403
    assert c.post(f"/api/review/{site['spec'].id}", json={}, headers=evil).status_code == 403
    assert c.post(f'/trial/{tid}/label', json={'label': 'unresolved'}, headers={'Origin': 'null'}).status_code == 403
    assert c.post(f'/trial/{tid}/label', json={'label': 'unresolved'}, headers={'Origin': 'http://testserver:8080'}).status_code == 200
    assert c.post(f'/trial/{tid}/label', json={'label': 'unresolved'}).status_code == 200  # no Origin (curl, scripts): allowed
    assert 'CSRF posture' in dash.__doc__


def test_console_inputs_are_fullmatched_and_cli_strings_quoted(site, monkeypatch):
    c = site['client']
    client = _Client()
    _install_client(monkeypatch, client)
    bad = c.post('/api/create', json={'source': 'github-repo', 'repos': 'a/b', 'since': '2026-01-01\n', 'approval_mode': '<script>x;rm -rf /</script>'})
    assert bad.status_code == 400
    errs = bad.json()['errors']
    assert any('since must be' in e for e in errs) and any('approval_mode' in e for e in errs)
    assert "'<script>x;rm -rf /</script>'" in bad.json()['cli']  # shell-quoted, never bare, in the CLI equivalent
    assert c.post('/api/create', json={'source': 'github-repo', 'repos': '../evil'}).status_code == 400
    assert c.post('/api/create', json={'source': 'github-repo', 'repos': 'https://github.com/../..'}).status_code == 400
    assert c.get('/jobs/x%0A').status_code == 404 and c.get('/jobs/ok-id').status_code == 200
    bad = c.post('/api/run', json={'scenario_ids': [site['spec'].id], 'models': ['a/b'], 'conditions': ['interactive'], 'purpose': '$(id)'})
    assert bad.status_code == 400 and "'$(id)'" in bad.json()['cli']
    # fallback paths carry every collected field
    class Legacy:
        def __init__(self):
            self.pipeline = []
            self.matrix = []

        async def start_pipeline(self, params):
            self.pipeline.append(params)
            return 'pipeline-1'

        async def start_matrix(self, plan):
            self.matrix.append(plan)
            return 'matrix-1'

    legacy = Legacy()
    _install_client(monkeypatch, legacy)
    ok = c.post('/api/create', json={'source': 'github-repo', 'repos': 'a/b', 'since': '2026-03-01', 'limit': 7})
    assert ok.status_code == 200 and legacy.pipeline[0]['since'] == '2026-03-01' and legacy.pipeline[0]['limit'] == 7
    ok = c.post('/api/run', json={'scenario_ids': [site['spec'].id], 'models': [HAIKU], 'conditions': ['interactive'], 'k': 1,
                                  'stakeholder_model': 'anthropic/claude-sonnet-5'})
    assert ok.status_code == 200 and 'stakeholder model from RunConfig defaults' not in ok.json()['via']
    plan = legacy.matrix[0]
    assert plan['trials'] and all(t['run_config']['stakeholder']['model_id'] == 'anthropic/claude-sonnet-5' for t in plan['trials'])


def test_hostile_spec_and_progress_text_is_escaped(tmp_path, monkeypatch):
    spec = make_spec(withheld=True)
    d = json.loads(spec.model_dump_json())
    d['briefs'][0]['text'] = d['briefs'][0]['text'] + '\n<script>alert(1)</script>'
    d['ledger'][0]['text'] = '<img src=x onerror=alert(2)> Raise ValueError'
    d['environment']['repo_url'] = 'javascript:alert(3)'
    d['persona']['summary'] = '<b>maintainer</b>'
    ws = make_workspace(tmp_path, spec)
    (ws.scenario_dir(spec.id) / 'scenario.json').write_text(json.dumps(d))
    (ws.scenario_dir(spec.id) / 'judge.json').write_text(json.dumps({'keep': True, 'verified_rubric_score': 2, 'rationale': '<script>alert(4)</script>',
                                                                     'gaps': [{'category': 'behavior', 'text': '<svg onload=alert(5)>'}]}))
    monkeypatch.setattr(dash, 'ROOT', ws.root)
    monkeypatch.setattr(dash, 'REGISTRY', ws.registry_path)
    monkeypatch.setenv('SWESIM_ENV_FILE', str(tmp_path / 'absent.env'))
    c = TestClient(app)
    for path in (f'/scenario/{spec.id}', f'/review/{spec.id}'):
        html = c.get(path).text
        for raw in ('<script>alert(1)</script>', '<img src=x onerror=alert(2)>', '<script>alert(4)</script>', '<svg onload=alert(5)>', '<b>maintainer</b>'):
            assert raw not in html, (path, raw)
        assert "href='javascript:" not in html and 'repository ↗' not in html  # non-http repo_url gets no link
    client = _Client()
    client.jobs = [{'workflow_id': 'j1', 'type': 'pipeline', 'status': 'running', 'summary': '<script>alert(6)</script>'}]
    client.progress_by_id = {'j1': {'stage': '<img src=x onerror=alert(7)>', 'scenario_id': '<b>x</b>', 'needs_human': ['<script>alert(8)</script>'],
                                    'candidates': [{'instance_id': '<i>evil</i>', 'stage': 'judging', 'detail': 'token=abc123 https://secret.example/x'}]}}
    _install_client(monkeypatch, client)
    jobs = c.get('/jobs').text
    assert '<script>alert(6)</script>' not in jobs
    job = c.get('/jobs/j1').text
    for raw in ('<img src=x onerror=alert(7)>', '<b>x</b>', '<script>alert(8)</script>', '<i>evil</i>'):
        assert raw not in job
    assert 'https://secret.example/x' not in job and '&lt;url&gt;' in job  # raw progress pane is redacted
    # review toggle stays on the review page
    review = c.get(f'/review/{spec.id}').text
    assert f"href='/review/{spec.id}?evaluator=1'" in review and f"href='/scenario/{spec.id}?evaluator=1'" not in review


def test_scenario_page_briefs_ledger_controls_and_evaluator_toggle(tmp_path, monkeypatch):
    spec = make_spec(with_author_only=True, withheld=True)
    ws = make_workspace(tmp_path, spec)
    (ws.scenario_dir(spec.id) / 'judge.json').write_text(json.dumps({'keep': True, 'verified_rubric_score': 2, 'complexity': 3,
                                                                     'test_behavioralness': 4, 'self_contained': True,
                                                                     'alternatives_accepted': False, 'model_id': 'openai/gpt-5.5',
                                                                     'rationale': 'Underspecified error type.',
                                                                     'gaps': [{'category': 'behavior', 'text': 'Which exception?', 'consequential': True,
                                                                               'derivable_from_repo': False, 'resolved_by': 'thread'}]}))
    monkeypatch.setattr(dash, 'ROOT', ws.root)
    monkeypatch.setattr(dash, 'REGISTRY', ws.registry_path)
    monkeypatch.setenv('SWESIM_ENV_FILE', str(tmp_path / 'absent.env'))
    c = TestClient(app)
    r = c.get(f'/scenario/{spec.id}')
    assert r.status_code == 200
    html = r.text
    assert '<h3>natural' in html and '<h3>withheld' in html
    assert "<mark class='removed'" in html and 'Traceback: SECRET_ERROR raised in loader</mark>' in html
    assert html.count("<mark class='removed' title=") == 1 and 'Import fails\nSteps: run `pkg load`.' in html  # one removed span; withheld brief verbatim
    assert 'withholding' in html and 'Removed category: reproduction' in html
    assert '<h3>on-request' in html and 'withheld-001' in html and 'f01' in html
    assert 'SECRET-AUTHOR-ONLY-NOTE' not in html and 'author-only facts (1) hidden' in html  # off by default: not even in the DOM
    on = c.get(f'/scenario/{spec.id}?evaluator=1').text
    assert 'SECRET-AUTHOR-ONLY-NOTE' in on and '<h3>author-only' in on and 'evaluator-only' in on
    # controls joined with validation results; the fixture validation only ran nop
    assert 'oracle.diff' in html and 'keyerror' in html and 'root-conftest' in html and 'incorrect-exploit' in html
    assert 'as expected' in html and 'not run' in html and 'valid</span>' in html
    # judge verdict and gaps
    assert 'rubric 2/3' in html and 'Which exception?' in html and 'Underspecified error type.' in html
    # stage chips and links
    for stage in ('candidate', 'judged', 'drafted', 'approved', 'compiled', 'validated', 'registered'):
        assert f'>{stage}</span>' in html
    assert f"href='/scenario/{spec.id}/file/validation.json'" in html
    assert c.get(f'/scenario/{spec.id}/file/validation.json').json()['status'] == 'valid'
    assert c.get(f'/scenario/{spec.id}/file/passwd').status_code == 404
    assert c.get('/scenario/no-such-scenario').status_code == 404 and c.get('/scenario/BAD').status_code == 404
    assert c.get('/scenarios').status_code == 200 and spec.id in c.get('/scenarios').text


def test_empty_workspace_renders_empty_states(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, 'ROOT', tmp_path / 'empty-workspace')
    monkeypatch.setattr(dash, 'REGISTRY', tmp_path / 'registry.json')
    c = TestClient(app)
    home = c.get('/')
    assert home.status_code == 200 and 'No scenarios yet' in home.text and 'No trials yet' in home.text and 'swesim draft' in home.text
    runs = c.get('/runs')
    assert runs.status_code == 200 and 'No trials yet' in runs.text and 'No scores yet' in runs.text
    assert c.get('/scenarios').status_code == 200 and 'No scenarios yet' in c.get('/scenarios').text
    ov = c.get('/api/overview').json()
    assert ov['scenarios'] == [] and ov['trials'] == [] and ov['summary'] == {'scenarios': 0, 'valid': 0, 'trials': 0, 'running': 0,
                                                                              'finished': 0, 'unresolved': 0, 'succeeded': 0}
    assert c.get('/trial/' + 'a' * 24).status_code == 404


def test_agent_and_stakeholder_text_is_escaped(site):
    c, ws, spec = site['client'], site['ws'], site['spec']
    evil_q = '<script>alert(1)</script> which exception?'
    evil_a = '<img src=x onerror=alert(2)> ValueError.'
    tid = write_trial(ws, spec, attempt=3, events=ask_events(T0 + 6, evil_q, evil_a, ['f01'], 5))
    (ws.root / 'trials' / tid / 'task' / 'instruction.md').write_text('# Task <b>bold</b>\n\n<script>alert(3)</script>')
    html = c.get(f'/trial/{tid}').text
    for raw in ('<script>alert(1)</script>', '<img src=x onerror=alert(2)>', '<b>bold</b>', '<script>alert(3)</script>'):
        assert raw not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html and '&lt;img src=x onerror=alert(2)&gt;' in html and '&lt;b&gt;bold&lt;/b&gt;' in html
    body = c.get(f'/api/trial/{tid}/timeline').json()
    assert any(r['text'] == evil_q for r in body['timeline'])  # JSON carries the data verbatim; escaping happens at render time
    # a hostile scenario id in request.json reaches the boards escaped
    rdir = ws.root / 'trials' / ('cd' * 12)
    rdir.mkdir()
    (rdir / 'request.json').write_text(json.dumps({'trial_id': 'cd' * 12, 'scenario_id': '<x onmouseover=alert(4)>', 'condition': 'full', 'model': HAIKU}))
    for path in ('/', '/runs'):
        page = c.get(path).text
        assert '<x onmouseover' not in page and '&lt;x onmouseover=alert(4)&gt;' in page


# ---------------------------------------------------------------- operator console: create, run, jobs, review

class _Client:
    """Stand-in for swesim.workflows.client with the functions the orchestration stream is adding."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.progress_by_id = {}
        self.jobs = []

    async def start_create(self, params):
        self.calls.append(('start_create', params))
        if self.fail:
            raise ConnectionRefusedError('temporal 7233 refused')
        return 'create-abc'

    async def start_run(self, params):
        self.calls.append(('start_run', params))
        return ['run-1', 'run-2'][: len(params['scenario_ids'])]

    async def list_runs(self):
        self.calls.append(('list_runs',))
        return self.jobs

    async def progress(self, wid):
        self.calls.append(('progress', wid))
        return self.progress_by_id.get(wid, {'stage': 'starting'})

    async def cancel(self, wid):
        self.calls.append(('cancel', wid))
        return {'cancelled': True}

    async def approve_candidate(self, wid, sid, spec_hash, decision, note):
        self.calls.append(('approve_candidate', wid, sid, spec_hash, decision, note))


def _install_client(monkeypatch, client):
    import swesim.workflows.client as real
    for name in ('start_create', 'start_run', 'list_runs', 'progress', 'cancel', 'approve_candidate', 'start_pipeline', 'start_matrix', 'approve'):
        if hasattr(client, name):
            monkeypatch.setattr(real, name, getattr(client, name), raising=False)
        else:
            monkeypatch.delattr(real, name, raising=False)
    return real


def test_create_form_and_api_validation_and_success(site, monkeypatch):
    c = site['client']
    html = c.get('/create').text
    assert "name='source'" in html and "name='repos'" in html and "name='since'" in html and "name='max_candidates'" in html
    assert "name='instance_ids'" in html and "name='limit'" in html and "name='max_parallel'" in html
    assert "value='openai/gpt-5.5'" in html and "value='anthropic/claude-sonnet-5'" in html  # judge / draft prefilled from CLI defaults
    assert "name='approval_mode'" in html and 'Auto-approve with morning review (default)' in html and 'Hold each task for my approval' in html
    assert "<option value='auto' selected>" in html
    for item in ('Jobs', 'Review', 'Create', 'Run'):
        assert f'>{item}</a>' in html
    assert 'SWESIM_WORKER_SECRET' not in html and 'stakeholder_url' not in html
    client = _Client()
    _install_client(monkeypatch, client)
    monkeypatch.setenv('SWESIM_STAKEHOLDER_URL', 'https://stakeholder.example.test')
    monkeypatch.setenv('SWESIM_WORKER_SECRET', 'never-shown')
    bad = c.post('/api/create', json={'source': 'github-repo', 'repos': 'not a url\nhttps://github.com/py-pdf/pypdf', 'judge_model': 'x'})
    assert bad.status_code == 400
    errs = bad.json()['errors']
    assert any('not a GitHub repository' in e for e in errs) and any('judge model' in e for e in errs)
    assert c.post('/api/create', json={'source': 'github-repo', 'repos': ''}).status_code == 400
    same = c.post('/api/create', json={'source': 'github-repo', 'repos': 'py-pdf/pypdf', 'judge_model': 'a/b', 'draft_model': 'a/b'})
    assert same.status_code == 400 and any('must differ' in e for e in same.json()['errors'])
    assert client.calls == []
    ok = c.post('/api/create', json={'source': 'github-repo', 'repos': 'https://github.com/py-pdf/pypdf\npsf/requests', 'since': '2026-06-01',
                                     'max_candidates': '2', 'approval_mode': 'signal', 'max_parallel': 2})
    assert ok.status_code == 200
    body = ok.json()
    assert body['ok'] is True and body['workflow_ids'] == ['create-abc'] and body['via'] == 'start_create'
    sent = client.calls[-1][1]
    assert sent['repos'] == ['https://github.com/py-pdf/pypdf', 'https://github.com/psf/requests'] and sent['approval_mode'] == 'signal'
    assert sent['max_candidates_per_repo'] == 2 and sent['judge_model'] == 'openai/gpt-5.5' and sent['stakeholder_url'] == 'https://stakeholder.example.test'
    assert 'stakeholder_url' not in body['params'] and 'never-shown' not in ok.text and 'stakeholder.example.test' not in ok.text
    assert '--approval-mode signal' in body['cli'] and 'py-pdf/pypdf' in body['cli']
    sw = c.post('/api/create', json={'source': 'swebench-annotated', 'instance_ids': 'psf__requests-1944\nbad id', 'limit': 3})
    assert sw.status_code == 400 and any('SWE-bench instance id' in e for e in sw.json()['errors'])
    sw = c.post('/api/create', json={'source': 'swebench-annotated', 'instance_ids': 'psf__requests-1944', 'limit': 3})
    assert sw.status_code == 200 and client.calls[-1][1]['instance_ids'] == ['psf__requests-1944']
    # Temporal unreachable: a clear error with the equivalent CLI command, never a silent failure
    _install_client(monkeypatch, _Client(fail=True))
    down = c.post('/api/create', json={'source': 'github-repo', 'repos': 'py-pdf/pypdf'})
    assert down.status_code == 502 and 'ConnectionRefusedError' in down.json()['error'] and 'uv run swesim pipeline' in down.json()['cli']


def test_create_falls_back_to_start_pipeline_per_repo_or_reports_missing(site, monkeypatch):
    c = site['client']

    class Legacy:
        def __init__(self):
            self.calls = []

        async def start_pipeline(self, params):
            self.calls.append(params)
            return f'pipeline-{len(self.calls)}'

    legacy = Legacy()
    _install_client(monkeypatch, legacy)
    ok = c.post('/api/create', json={'source': 'github-repo', 'repos': 'a/b\nc/d', 'approval_mode': 'auto'})
    assert ok.status_code == 200 and ok.json()['workflow_ids'] == ['pipeline-1', 'pipeline-2'] and 'start_pipeline' in ok.json()['via']
    assert legacy.calls[0]['repo'] == 'https://github.com/a/b' and legacy.calls[0]['approval_mode'] == 'auto' and legacy.calls[0]['models'] == ['anthropic/claude-haiku-4-5-20251001']
    _install_client(monkeypatch, object())
    none = c.post('/api/create', json={'source': 'github-repo', 'repos': 'a/b'})
    assert none.status_code == 502 and 'neither start_create nor start_pipeline' in none.json()['error'] and 'uv run swesim pipeline' in none.json()['cli']


def test_run_form_and_api(site, monkeypatch):
    c, spec = site['client'], site['spec']
    html = c.get('/run').text
    assert f"<option value='{spec.id}'>" in html and "name='scenario_ids'" in html and 'multiple' in html
    assert f"name='models' value='{HAIKU}' checked" in html and "value='anthropic/claude-sonnet-5'" in html and "name='models_extra'" in html
    assert "name='conditions' value='interactive' checked" in html and "name='k'" in html and "name='purpose'" in html
    assert "name='brief_profile'" in html and "name='stakeholder_model'" in html and "name='max_parallel'" in html
    client = _Client()
    _install_client(monkeypatch, client)
    empty = c.post('/api/run', json={'scenario_ids': [], 'models': [HAIKU], 'conditions': ['interactive']})
    assert empty.status_code == 400 and any('at least one registered' in e for e in empty.json()['errors'])
    unknown = c.post('/api/run', json={'scenario_ids': ['nope-scenario'], 'models': [HAIKU], 'conditions': ['interactive']})
    assert unknown.status_code == 400 and any('not a registered valid scenario' in e for e in unknown.json()['errors'])
    nomodel = c.post('/api/run', json={'scenario_ids': [spec.id], 'models': [], 'conditions': ['interactive']})
    assert nomodel.status_code == 400 and client.calls == []
    ok = c.post('/api/run', json={'scenario_ids': [spec.id], 'models': [HAIKU], 'models_extra': 'anthropic/claude-sonnet-5, openai/gpt-5.5',
                                  'conditions': ['hidden', 'interactive'], 'k': '3', 'purpose': 'comparison', 'brief_profile': 'withheld',
                                  'stakeholder_model': 'openai/gpt-5.5', 'max_parallel': 2})
    assert ok.status_code == 200 and ok.json()['workflow_ids'] == ['run-1']
    sent = client.calls[-1][1]
    assert sent['models'] == [HAIKU, 'anthropic/claude-sonnet-5', 'openai/gpt-5.5'] and sent['conditions'] == ['hidden', 'interactive'] and sent['k'] == 3
    assert f'uv run swesim matrix {spec.id}' in ok.json()['cli'] and '--k 3' in ok.json()['cli']
    # an unregistered-valid registry renders the empty state with a disabled button
    site['ws'].registry_path.write_text(json.dumps({'scenarios': {}}))
    page = c.get('/run').text
    assert 'No registered valid scenarios' in page and 'disabled' in page


def test_jobs_list_progress_and_cancel(site, monkeypatch):
    c = site['client']
    client = _Client()
    client.jobs = [{'workflow_id': 'create-abc', 'type': 'pipeline', 'status': 'running', 'started_at': '2026-09-13T08:00:00Z', 'summary': {'stage': 'validating'}},
                   {'workflow_id': 'run-1', 'type': 'matrix', 'status': 'completed', 'started_at': '2026-09-13T07:00:00Z', 'summary': '3 trials'}]
    client.progress_by_id = {
        'create-abc': {'stage': 'awaiting-approval', 'scenario_id': site['spec'].id, 'spec_hash': 'h' * 64,
                       'candidates': [{'instance_id': 'psf__requests-1944', 'scenario_id': site['spec'].id, 'stage': 'awaiting-approval'},
                                      {'instance_id': 'psf__requests-2000', 'stage': 'judging', 'detail': 'rubric 1/3'}]},
        'run-1': {'stage': 'complete', 'trials': [{'trial_id': site['done'], 'condition': 'interactive', 'model': HAIKU, 'status': 'completed',
                                                   'outcome': 'valid-correct', 'success': 1, 'reward': 1.0},
                                                  {'trial_id': 'f' * 24, 'condition': 'hidden', 'model': HAIKU, 'status': 'unresolved'}]}}
    _install_client(monkeypatch, client)
    html = c.get('/jobs').text
    assert "href='/jobs/create-abc'" in html and "href='/jobs/run-1'" in html and 'stage=validating' in html and '3 trials' in html
    assert '>running</span>' in html and '>completed</span>' in html and "data-poll-ms='5000'" in html
    api = c.get('/api/jobs').json()
    assert api['ok'] is True and [j['workflow_id'] for j in api['jobs']] == ['create-abc', 'run-1'] and api['jobs'][0]['summary'] == 'stage=validating'
    create = c.get('/jobs/create-abc').text
    assert 'psf__requests-1944' in create and 'awaiting-approval' in create and f"href='/review/{site['spec'].id}'" in create and 'psf__requests-2000' in create
    assert "id='cancel-job'" in create and "data-endpoint='/api/jobs/create-abc/cancel'" in create
    run = c.get('/jobs/run-1').text
    assert f"href='/trial/{site['done']}'" in run and 'valid-correct' in run and 'unresolved' in run
    assert c.get('/api/jobs/run-1').json()['progress']['stage'] == 'complete'
    # cancel requires an explicit confirm step
    assert c.post('/api/jobs/create-abc/cancel', json={}).status_code == 400
    assert c.post('/api/jobs/create-abc/cancel', json={'confirm': 'yes'}).status_code == 400
    assert ('cancel', 'create-abc') not in client.calls
    ok = c.post('/api/jobs/create-abc/cancel', json={'confirm': True})
    assert ok.status_code == 200 and ok.json()['cancelled'] == 'create-abc' and ('cancel', 'create-abc') in client.calls
    assert c.get('/jobs/bad id').status_code == 404
    # without list_runs / progress in the client: banner with the CLI equivalent, page still renders
    _install_client(monkeypatch, object())
    page = c.get('/jobs')
    assert page.status_code == 200 and "client does not support that yet" in page.text and 'pipeline-progress' in page.text
    assert 'Temporal is not reachable' not in page.text  # a missing client function is not an unreachable server
    assert c.get('/api/jobs').status_code == 502 and c.get('/jobs/create-abc').status_code == 200


def test_review_queue_and_decisions(site, monkeypatch, tmp_path):
    c, ws, _spec = site['client'], site['ws'], site['spec']
    # a local draft without approval.json (fresh scenario dir) and a candidate held by a creation job
    held = make_spec('held-scenario', withheld=True)
    hdir = ws.scenario_dir(held.id)
    hdir.mkdir(parents=True)
    (hdir / 'scenario.draft.json').write_text(held.model_dump_json())
    (hdir / 'judge.json').write_text(json.dumps({'keep': True, 'verified_rubric_score': 2, 'complexity': 3, 'gaps': []}))
    local = make_spec('local-draft', with_author_only=True)
    ldir = ws.scenario_dir(local.id)
    ldir.mkdir(parents=True)
    (ldir / 'scenario.draft.json').write_text(local.model_dump_json())
    (ldir / 'draft.meta.json').write_text(json.dumps({'created_at': '2026-09-13T01:00:00+00:00'}))
    client = _Client()
    client.jobs = [{'workflow_id': 'create-abc', 'type': 'pipeline', 'status': 'running'}]
    client.progress_by_id = {'create-abc': {'stage': 'awaiting-approval', 'scenario_id': held.id, 'spec_hash': held.spec_hash}}
    _install_client(monkeypatch, client)
    q = c.get('/api/review').json()
    rows = {i['scenario_id']: i for i in q['items']}
    assert set(rows) == {held.id, local.id}
    assert rows[held.id]['workflow_id'] == 'create-abc' and rows[held.id]['disk_hash'] == held.spec_hash and rows[held.id]['on_disk'] is True
    assert rows[local.id]['source'] == 'local-draft' and rows[local.id]['workflow_id'] is None
    html = c.get('/review').text
    assert f"href='/review/{held.id}'" in html and f"href='/review/{local.id}'" in html and 'create-abc' in html
    # review page: briefs with removed span, ledger, controls, judge, assets, decision form carrying the disk hash and workflow id
    page = c.get(f'/review/{held.id}').text
    assert "<mark class='removed' title=" in page and '<h3>withheld' in page and 'withheld-001' in page and 'oracle.diff' in page
    assert 'rubric 2/3' in page and '<h2>Assets</h2>' in page and 'test.patch' in page
    assert f"name='spec_hash' value='{held.spec_hash}'" in page and "name='workflow_id' value='create-abc'" in page
    assert "value='approve'" in page and "value='reject'" in page and 'awaiting approval' in page
    lpage = c.get(f'/review/{local.id}').text
    assert 'SECRET-AUTHOR-ONLY-NOTE' not in lpage and 'SECRET-AUTHOR-ONLY-NOTE' in c.get(f'/review/{local.id}?evaluator=1').text
    # stale hash is refused
    stale = c.post(f'/api/review/{held.id}', json={'decision': 'approve', 'spec_hash': 'x' * 64, 'workflow_id': 'create-abc'})
    assert stale.status_code == 409 and stale.json()['current_spec_hash'] == held.spec_hash and client.calls[-1][0] != 'approve_candidate'
    # approve a held candidate: the job is signalled, the decision is recorded
    ok = c.post(f'/api/review/{held.id}', json={'decision': 'approve', 'spec_hash': held.spec_hash, 'workflow_id': 'create-abc', 'note': 'looks right'})
    assert ok.status_code == 200 and ok.json()['via'] == 'signal'
    assert client.calls[-1] == ('approve_candidate', 'create-abc', held.id, held.spec_hash, 'approve', 'looks right')
    assert json.loads((hdir / 'review.json').read_text())['decision'] == 'approve'
    # reject a local draft: recorded locally, dropped from the queue
    rj = c.post(f'/api/review/{local.id}', json={'decision': 'reject', 'spec_hash': local.spec_hash, 'note': 'thin thread'})
    assert rj.status_code == 200 and rj.json()['via'] == 'local review.json'
    assert local.id not in {i['scenario_id'] for i in c.get('/api/review').json()['items']}
    assert 'rejected' in c.get(f'/review/{local.id}').text and "value='approve' disabled" in c.get(f'/review/{local.id}').text
    # approve a local draft through the code-owned gates (approve_scenario monkeypatched: it needs real asset files)
    third = make_spec('third-draft')
    tdir = ws.scenario_dir(third.id)
    tdir.mkdir(parents=True)
    (tdir / 'scenario.draft.json').write_text(third.model_dump_json())
    seen = []

    def fake_approve(ws_, sid):
        seen.append((str(ws_.root), sid))
        (tdir / 'approval.json').write_text(json.dumps({'scenario_id': sid, 'spec_hash': third.spec_hash, 'approved_by': 'human-command', 'review_needed': False}))
        return {'approved': True, 'reasons': [], 'spec_hash': third.spec_hash}

    monkeypatch.setattr(dash.console, 'local_approve', fake_approve)
    ok = c.post(f'/api/review/{third.id}', json={'decision': 'approve', 'spec_hash': third.spec_hash})
    assert ok.status_code == 200 and seen == [(str(ws.root), third.id)] and ok.json()['approval']['approved_by'] == 'human-command'
    again = c.post(f'/api/review/{third.id}', json={'decision': 'approve', 'spec_hash': third.spec_hash})
    assert again.status_code == 409 and 'already approved' in again.json()['error']
    assert 'Read-only' in c.get(f'/review/{third.id}').text
    assert c.post(f'/api/review/{third.id}', json={'decision': 'maybe', 'spec_hash': third.spec_hash}).status_code == 400
    assert c.post('/api/review/no-such-draft', json={'decision': 'approve', 'spec_hash': 'x'}).status_code == 404
    # gate rejection surfaces the reasons
    monkeypatch.setattr(dash.console, 'local_approve', lambda ws_, sid: {'approved': False, 'reasons': ['asset mismatch assets/oracle.diff']})
    fourth = make_spec('fourth-draft')
    fdir = ws.scenario_dir(fourth.id)
    fdir.mkdir(parents=True)
    (fdir / 'scenario.draft.json').write_text(fourth.model_dump_json())
    gated = c.post(f'/api/review/{fourth.id}', json={'decision': 'approve', 'spec_hash': fourth.spec_hash})
    assert gated.status_code == 400 and gated.json()['reasons'] == ['asset mismatch assets/oracle.diff']


def test_overview_review_needed_chip_and_start_cta(site, tmp_path, monkeypatch):
    c, ws, spec = site['client'], site['ws'], site['spec']
    (ws.scenario_dir(spec.id) / 'approval.json').write_text(json.dumps({'scenario_id': spec.id, 'spec_hash': spec.spec_hash, 'version': 1,
                                                                        'approved_by': 'auto-validators', 'review_needed': True}))
    html = c.get('/').text
    assert f"href='/review/{spec.id}'" in html and '>review needed</a>' in html
    ov = c.get('/api/overview').json()
    assert ov['scenarios'][0]['approval'] == {'approved_by': 'auto-validators', 'review_needed': True, 'approved_at': None, 'spec_hash': spec.spec_hash}
    assert 'Read-only' in c.get(f'/review/{spec.id}').text  # already approved: read-only review page
    held = make_spec('held-two')
    hdir = ws.scenario_dir(held.id)
    hdir.mkdir(parents=True)
    (hdir / 'scenario.draft.json').write_text(held.model_dump_json())
    assert '>awaiting approval</a>' in c.get('/').text
    assert 'Create tasks' not in c.get('/').text.split('<h2>Scenarios</h2>')[0]  # CTA only when the board is empty
    monkeypatch.setattr(dash, 'ROOT', tmp_path / 'fresh')
    monkeypatch.setattr(dash, 'REGISTRY', tmp_path / 'fresh-registry.json')
    empty = c.get('/').text
    assert "href='/create'" in empty and "href='/run'" in empty and 'Nothing here yet' in empty


def test_static_assets_are_served_without_a_build_step(site):
    c = site['client']
    css = c.get('/static/app.css')
    assert css.status_code == 200 and css.headers['content-type'].startswith('text/css') and '--surface' in css.text and 'prefers-color-scheme: dark' in css.text
    js = c.get('/static/app.js')
    assert js.status_code == 200 and js.headers['content-type'].startswith('text/javascript') and '/api/overview' in js.text and 'textContent' in js.text
    assert 'innerHTML' not in js.text  # values are inserted as text nodes only
    assert c.get('/static/app.py').status_code == 404 and c.get('/static/nope').status_code == 404
    home = c.get('/').text
    assert "href='/static/app.css'" in home and "src='/static/app.js'" in home and 'http://localhost:8233' in home
    assert "aria-current=\"page\"" in home and 'Runs' in home and 'Scenarios' in home


def test_runs_page_lists_scenarios_scores_and_trials(site):
    r = site['client'].get('/')
    assert r.status_code == 200 and 'text/html' in r.headers['content-type']
    html = r.text
    assert 'swesim' in html and 'Overview' in html and site['spec'].id in html and 'valid' in html
    for tid in (site['done'], site['wrong'], site['full'], site['running']):
        assert f"href='/trial/{tid}'" in html
    assert 'running' in html and 'valid-correct' in html and 'valid-incorrect' in html and HAIKU in html
    assert 'calibration' in html and 'comparison' in html
    assert 'n/a' in html  # n_valid (2) < k (3): pass@k is null, shown as unavailable, never a reduced k
    assert 'unresolved' in html  # reviewer label surfaces on the runs table
    assert 'SECRET-AUTHOR-ONLY-NOTE' not in html


def test_episode_page_renders_conversation_and_outcome(site):
    tid = site['done']
    c = site['client']
    r = site['client'].get(f'/trial/{tid}')
    assert r.status_code == 200
    html = r.text
    assert f'Episode {tid}' in html and 'It breaks.' in html and 'ask-user' in html  # brief
    assert 'Which exception should be raised?' in html and 'ValueError.' in html and 'Looks right.' in html
    # evaluator-only material is gated (default off): no fact annotations, tier/patience internals, tool audit or delivery flags
    assert 'read_diff' not in html and 'disclosed fact ids' not in html and 'delivery generated' not in html and 'tier full' not in html
    assert "role='switch' aria-checked='false'" in html and f"href='/trial/{tid}?evaluator=1'" in html
    on = c.get(f'/trial/{tid}?evaluator=1').text
    assert 'read_diff' in on and 'f01' in on and 'disclosed fact ids' in on and 'delivery generated' in on and "aria-checked='true'" in on
    assert 'SECRET-AUTHOR-ONLY-NOTE' not in on  # the trial page never renders the spec's author-only facts
    assert '3 / 6' in html  # patience end / start (agent-observable availability stays visible)
    assert 'valid-correct' in html and 'g01' in html and 't1' in html and 'interactive' in html and 'withheld' in html
    assert 'unadjudicated' in html and 'diff-only' in html  # reviewed_equals_submitted null is explained, not hidden
    assert f"href='/trial/{tid}/file/trajectory.json'" in html and f"href='/trial/{tid}/file/verifier-report.json'" in html
    assert f"href='/trial/{tid}/file/transcript.json" not in html and 'evaluator-only file(s)' in html  # raw transcript/record/spec gated
    assert f"href='/trial/{tid}/file/transcript.json?evaluator=1'" in on
    assert 'name="label"' in html and 'selected' in html and 'first look' in html
    assert 'setInterval' not in html  # finished: no polling
    assert 'SECRET-AUTHOR-ONLY-NOTE' not in html
    # agent steps from the finished trajectory are interleaved
    assert 'I will ask which exception.' in html and 'Editing.' in html
    assert site['client'].get('/trial/' + 'f' * 24).status_code == 404
    assert site['client'].get('/trial/not-a-trial').status_code == 404


def test_timeline_api_merges_agent_stakeholder_verifier(site):
    r = site['client'].get(f"/api/trial/{site['done']}/timeline")
    assert r.status_code == 200
    body = r.json()
    rows = body['timeline']
    actors = [x['actor'] for x in rows]
    assert {'agent', 'stakeholder', 'verifier', 'harness'} <= set(actors)
    stamped = [x['ts'] for x in rows if x['ts'] is not None]
    assert stamped == sorted(stamped) and rows[-1]['actor'] == 'verifier'
    assert body['report']['outcome'] == 'valid-correct' and body['status']['status'] == 'finished' and body['live'] is False
    question = next(x for x in rows if x['kind'] == 'ask' and x['actor'] == 'agent')
    assert question['text'] == 'Which exception should be raised?'
    assert site['client'].get('/api/trial/' + 'f' * 24 + '/timeline').status_code == 404


def test_timeline_live_transcript_only_when_running(site, monkeypatch):
    tid = site['running']
    calls = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {'trial_id': tid, 'events': ask_events(T0 + 6, 'Live question?', 'Live answer.', ['f01'], 5),
                    'patience_start': 6, 'patience_end': 5, 'frozen': False, 'transcript_hash': None}

    def fake_get(url, headers=None, timeout=None, **kw):
        calls.append((url, headers, timeout))
        return Resp()

    monkeypatch.setattr(dash.httpx, 'get', fake_get)
    monkeypatch.setenv('SWESIM_STAKEHOLDER_URL', 'https://stakeholder.example.test/')
    monkeypatch.setenv('SWESIM_WORKER_SECRET', 'worker-secret-value')
    body = site['client'].get(f'/api/trial/{tid}/timeline').json()
    assert calls == [(f'https://stakeholder.example.test/transcript/{tid}', {'X-Worker-Secret': 'worker-secret-value'}, 10.0)]
    assert body['live'] is True and body['status']['status'] == 'running'
    assert [x['text'] for x in body['timeline'] if x['actor'] in ('agent', 'stakeholder')] == ['Live question?', 'Live answer.']
    assert 'worker-secret-value' not in json.dumps(body)
    assert 'disclosed_fact_ids' not in json.dumps(body) and body['evaluator'] is False  # API strips audit meta unless ?evaluator=1
    ev = site['client'].get(f'/api/trial/{tid}/timeline?evaluator=1').json()
    assert ev['evaluator'] is True and any(x.get('meta', {}).get('disclosed_fact_ids') == ['f01'] for x in ev['timeline'])
    assert 'tier' not in body['patience'] and ev['patience'].get('tier') == 'full'  # stakeholder tier is evaluator-only too
    page = site['client'].get(f'/trial/{tid}')
    assert page.status_code == 200 and "data-live='true'" in page.text and 'running' in page.text and 'Live answer.' in page.text
    assert 'agent is working' in page.text and 'setInterval' not in page.text  # behaviour lives in /static/app.js, never inline
    assert '5 / 6' in page.text and 'worker-secret-value' not in page.text
    # finished trials never trigger a live fetch, even with the env set
    calls.clear()
    assert site['client'].get(f"/api/trial/{site['done']}/timeline").status_code == 200 and calls == []
    assert site['client'].get('/').status_code == 200 and calls == []


def test_live_transcript_failure_degrades(site, monkeypatch):
    tid = site['running']

    def boom(*a, **k):
        raise dash.httpx.ConnectError('down')

    monkeypatch.setattr(dash.httpx, 'get', boom)
    monkeypatch.setenv('SWESIM_STAKEHOLDER_URL', 'https://stakeholder.example.test')
    monkeypatch.setenv('SWESIM_WORKER_SECRET', 's')
    body = site['client'].get(f'/api/trial/{tid}/timeline').json()
    assert body['timeline'] == [] and body['live'] is True and body['live_transcript_error'] == 'ConnectError'
    monkeypatch.delenv('SWESIM_STAKEHOLDER_URL')
    body = site['client'].get(f'/api/trial/{tid}/timeline').json()
    assert body['live_transcript_error'] == 'SWESIM_STAKEHOLDER_URL not configured'
    assert site['client'].get(f'/trial/{tid}').status_code == 200


def test_label_post_validation_and_storage(site):
    c, tid = site['client'], site['wrong']
    r = c.post(f'/trial/{tid}/label', json={'label': 'no-question', 'note': 'never asked' + 'x' * 3000})
    assert r.status_code == 200 and r.json() == {'saved': True}
    saved = json.loads((site['ws'].root / 'trials' / tid / 'label.json').read_text())
    assert set(saved) == {'label', 'note'} and saved['label'] == 'no-question' and len(saved['note']) == 2000
    assert 'no-question' in c.get(f'/trial/{tid}').text
    assert c.post(f'/trial/{tid}/label', json={'label': 'looks-fine'}).status_code == 400
    assert c.post(f'/trial/{tid}/label', json={}).status_code == 400
    assert c.post('/trial/' + 'f' * 24 + '/label', json={'label': 'unresolved'}).status_code == 404
    assert c.post('/trial/zzz/label', json={'label': 'unresolved'}).status_code == 404
    assert json.loads((site['ws'].root / 'trials' / tid / 'label.json').read_text())['label'] == 'no-question'


def test_file_route_serves_whitelisted_trial_files_only(site):
    c, tid = site['client'], site['done']
    r = c.get(f'/trial/{tid}/file/trajectory.json')
    assert r.status_code == 200 and r.json()['steps'] == TRAJ['steps']
    assert c.get(f'/trial/{tid}/file/verifier-report.json').json()['outcome'] == 'valid-correct'
    assert c.get(f'/trial/{tid}/file/instruction.md').text.startswith('# Task')
    for gated in ('record.json', 'transcript.json', 'spec.json'):  # evaluator-only raw files
        assert c.get(f'/trial/{tid}/file/{gated}').status_code == 403
        assert c.get(f'/trial/{tid}/file/{gated}?evaluator=1').status_code == 200
    assert c.get(f"/scenario/{site['spec'].id}/file/scenario.json").status_code == 403
    assert c.get(f"/scenario/{site['spec'].id}/file/scenario.json?evaluator=1").status_code == 200
    assert c.get(f"/scenario/{site['spec'].id}/file/compile.json").status_code == 200
    assert c.get(f'/trial/{tid}/file/passwd').status_code == 404
    assert c.get(f'/trial/{tid}/file/..%2Fspec.json').status_code == 404
    assert c.get(f"/trial/{site['running']}/file/trajectory.json").status_code == 404  # not there yet
    assert c.get('/trial/' + 'f' * 24 + '/file/record.json').status_code == 404


def test_timestamps_read_from_status_then_record_with_flagged_fallback(site):
    c, tid = site['client'], site['wrong']
    d = site['ws'].root / 'trials' / tid
    # status.json without timestamps, record.json with them (the run stage may write either or both)
    (d / 'status.json').write_text(json.dumps({'status': 'finished', 'trial_id': tid}))
    rec = json.loads((d / 'record.json').read_text())
    rec['started_at'] = '2026-09-13T11:00:00Z'
    rec['finished_at'] = '2026-09-13T11:07:30Z'
    (d / 'record.json').write_text(json.dumps(rec))
    body = c.get(f'/api/trial/{tid}/timeline').json()
    verifier = next(r for r in body['timeline'] if r['actor'] == 'verifier')
    assert verifier['ts_iso'] == '2026-09-13T11:07:30+00:00' and verifier['no_timestamp'] is False
    assert body['meta']['started_at'] == '2026-09-13T11:00:00Z' and body['status']['status'] == 'finished'
    page = c.get(f'/trial/{tid}').text
    assert '2026-09-13T11:00:00Z' in page and '2026-09-13T11:07:30Z' in page and 'no timestamp' not in page
    assert '2026-09-13T11:00:00Z' in c.get('/').text  # runs table falls back to record.json too
    # status.json wins when both are present
    (d / 'status.json').write_text(json.dumps({'status': 'finished', 'trial_id': tid, 'finished_at': '2026-09-13T11:09:00Z'}))
    body = c.get(f'/api/trial/{tid}/timeline').json()
    assert next(r for r in body['timeline'] if r['actor'] == 'verifier')['ts_iso'] == '2026-09-13T11:09:00+00:00'
    # neither: the verifier row stays, last and flagged
    (d / 'status.json').write_text(json.dumps({'status': 'finished', 'trial_id': tid}))
    rec.pop('started_at')
    rec.pop('finished_at')
    (d / 'record.json').write_text(json.dumps(rec))
    body = c.get(f'/api/trial/{tid}/timeline').json()
    assert body['timeline'][-1]['actor'] == 'verifier' and body['timeline'][-1]['no_timestamp'] is True
    assert 'no timestamp' in c.get(f'/trial/{tid}').text


def test_runs_page_tolerates_malformed_scores_files(site):
    c, ws = site['client'], site['ws']
    (ws.scenario_dir(site['spec'].id) / 'scores.json').write_text('[1, 2, 3]')  # not an object of groups
    other = ws.root / 'scenarios' / 'broken-scenario'
    other.mkdir(parents=True)
    (other / 'scores.json').write_text('{not json')
    mixed = ws.root / 'scenarios' / 'mixed-scenario'
    mixed.mkdir(parents=True)
    (mixed / 'scores.json').write_text(json.dumps({'interactive|withheld|r': {'condition': 'interactive', 'brief_profile': 'withheld',
                                                                             'model': HAIKU, 'purpose': 'calibration', 'requested_k': 3,
                                                                             'n_valid': 1, 'n_total': 1, 'success_count': 1,
                                                                             'pass_at_k': None, 'pass_pow_k': None, 'mean_reward': 1.0,
                                                                             'outcomes': {'valid-correct': 1}, 'trial_ids': ['a' * 24]},
                                                   'garbage': 'not a group'}))
    r = c.get('/')
    assert r.status_code == 200
    assert r.text.count('unreadable scores.json') == 2 and 'mixed-scenario' in r.text
    for tid in (site['done'], site['wrong'], site['full'], site['running']):
        assert f"href='/trial/{tid}'" in r.text  # the trials table still renders
    runs = c.get('/runs')
    assert runs.status_code == 200 and runs.text.count('unreadable scores.json') == 2 and 'valid-correct 1' in runs.text
    assert c.get('/api/overview').status_code == 200


def test_unresolved_attempts_are_flagged_on_runs_table_and_render_an_episode_page(site):
    c, ws, spec = site['client'], site['ws'], site['spec']
    err = write_unresolved(ws, spec, kind='status-error', attempt=5)
    rerr = write_unresolved(ws, spec, kind='runner-error', attempt=6)
    req = write_unresolved(ws, spec, kind='request-only', attempt=7)
    score_scenario(ws, spec.id, k=3)
    html = c.get('/').text
    for tid in (err, rerr, req):
        assert f"href='/trial/{tid}'" in html
    assert html.count('>unresolved</span>') >= 3 and 'RuntimeError' in html and 'HarborRunError' in html
    assert 'runner-error' in html and 'requested' in html
    runs = c.get('/runs').text
    assert 'attempts (unresolved)' in runs and runs.count('>unresolved</span>') >= 3
    for tid in (site['done'], site['wrong'], site['full']):  # finished trials are not flagged
        row = html[html.index(f"href='/trial/{tid}'"):]
        assert '>unresolved</span>' not in row[:row.index('</tr>')]
    page = c.get(f'/trial/{rerr}').text
    assert 'Unresolved attempt' in page and 'HarborRunError' in page and 'runner-error.json' in page and spec.id in page
    assert f"href='/trial/{rerr}/file/runner-error.json'" in page and f"href='/trial/{rerr}/file/launch.json'" in page
    assert c.get(f'/trial/{rerr}/file/runner-error.json').json()['type'] == 'HarborRunError'
    err_page = c.get(f'/trial/{err}').text
    assert 'Unresolved attempt' in err_page and 'RuntimeError' in err_page and 'status.json' in err_page and 'setInterval' not in err_page
    assert c.get(f'/trial/{req}').status_code == 200 and 'requested' in c.get(f'/trial/{req}').text
    body = c.get(f'/api/trial/{err}/timeline').json()
    assert body['unresolved']['status'] == 'error' and body['unresolved']['error_type'] == 'RuntimeError' and body['timeline'] == []
    assert c.get(f"/api/trial/{site['done']}/timeline").json()['unresolved'] is None
    live = c.get(f"/api/trial/{site['running']}/timeline").json()  # running without a record: unresolved so far, still live
    assert live['live'] is True and live['unresolved']['status'] == 'running'
