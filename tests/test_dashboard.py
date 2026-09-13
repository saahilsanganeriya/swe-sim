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
    return {'ws': ws, 'spec': spec, 'done': done, 'wrong': wrong, 'full': full, 'running': running, 'client': TestClient(app)}


def test_runs_page_lists_scenarios_scores_and_trials(site):
    r = site['client'].get('/')
    assert r.status_code == 200 and 'text/html' in r.headers['content-type']
    html = r.text
    assert 'swesim runs' in html and site['spec'].id in html and 'valid' in html
    for tid in (site['done'], site['wrong'], site['full'], site['running']):
        assert f'href="/trial/{tid}"' in html
    assert 'running' in html and 'valid-correct' in html and 'valid-incorrect' in html and HAIKU in html
    assert 'calibration' in html and 'comparison' in html
    assert 'n/a' in html  # n_valid (2) < k (3): pass@k is null, shown as unavailable, never a reduced k
    assert 'unresolved' in html  # reviewer label surfaces on the runs table
    assert 'SECRET-AUTHOR-ONLY-NOTE' not in html


def test_episode_page_renders_conversation_and_outcome(site):
    tid = site['done']
    r = site['client'].get(f'/trial/{tid}')
    assert r.status_code == 200
    html = r.text
    assert f'Episode {tid}' in html and 'It breaks.' in html and 'ask-user' in html  # brief
    assert 'Which exception should be raised?' in html and 'ValueError.' in html and 'Looks right.' in html
    assert 'read_diff' in html and 'f01' in html and 'evaluator-only' in html
    assert '3 / 6' in html  # patience end / start
    assert 'valid-correct' in html and 'g01' in html and 't1' in html and 'interactive' in html and 'withheld' in html
    assert 'unadjudicated' in html and 'diff-only' in html  # reviewed_equals_submitted null is explained, not hidden
    assert f'href="/trial/{tid}/file/trajectory.json"' in html and f'href="/trial/{tid}/file/verifier-report.json"' in html
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
    page = site['client'].get(f'/trial/{tid}')
    assert page.status_code == 200 and 'setInterval' in page.text and 'running' in page.text and 'Live answer.' in page.text
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
    assert c.get(f'/trial/{tid}/file/record.json').status_code == 200
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
    assert r.text.count('unreadable scores.json') == 2 and 'mixed-scenario' in r.text and 'valid-correct 1' in r.text
    for tid in (site['done'], site['wrong'], site['full'], site['running']):
        assert f'href="/trial/{tid}"' in r.text  # the trials table still renders
