"""Screen labels require the exact attributable attempts and a full-arm success."""
import pytest

from swesim.stages.difficulty import label


def groups(hidden=1, full=1):
    return [{'condition': 'hidden', 'purpose': 'screen', 'model': 'm', 'n': 2, 'success_count': hidden, 'trial_ids': ['a', 'b']},
            {'condition': 'full', 'purpose': 'screen', 'model': 'm', 'n': 1, 'success_count': full, 'trial_ids': ['c']}]


@pytest.mark.parametrize('hidden,full,tier', [(2, 1, 'saturated'), (1, 1, 'informative'), (0, 1, 'informative'), (2, 0, 'broken'), (0, 0, 'broken')])
def test_difficulty_labels(hidden, full, tier):
    result = label(groups(hidden, full), spec_hash='spec', compiled_hash='build')
    assert result.tier == tier and result.hidden.n == 2 and result.full.n == 1
    assert result.spec_hash == 'spec' and result.compiled_hash == 'build' and result.trial_ids == ['a', 'b', 'c']


@pytest.mark.parametrize('change', [ {'n': 1}, {'n_infra_failures': 1}, {'attempts_unresolved': 1}, {'purpose': 'comparison'},
                                   {'model': 'different'}, {'inconsistent_reward': ['a']}, {'trial_ids': ['a', 'a']}, {'success_count': 3}])
def test_no_label_from_incomplete_mixed_or_invalid_evidence(change):
    rows = groups(); rows[0].update(change)
    with pytest.raises(ValueError):
        label(rows)


def test_screen_plan_and_activity_use_exact_trial_ids_and_current_build(tmp_path, monkeypatch):
    import asyncio
    import json

    from test_workflows import frozen_scenario

    from swesim.models import RunConfig
    from swesim.paths import Workspace, atomic_json
    from swesim.workflows import activities as A
    ws = Workspace(tmp_path)
    monkeypatch.setattr(A, 'WS', ws)
    spec = frozen_scenario(ws)
    planned = A.plan_trials({'scenario_id': spec.id, 'purpose': 'screen', 'stakeholder_url': 'https://example.test', 'k': 99, 'conditions': ['interactive']})
    assert [t['condition'] for t in planned['trials']] == ['hidden', 'hidden', 'full']
    for i, t in enumerate(planned['trials']):
        assert RunConfig.model_validate(t['run_config']).purpose == 'screen'
        record = {**t, 'purpose': 'screen', 'compiled_hash': 'c' * 64, 'status': 'completed',
                  'reward': {'success': 0 if i == 0 else 1, 'reward': 0.0 if i == 0 else 1.0, 'outcome_code': 2 if i == 0 else 1}}
        atomic_json(ws.root / 'trials' / t['trial_id'] / 'record.json', record)
    payload = {'scenario_id': spec.id, 'trial_ids': [t['trial_id'] for t in planned['trials']]}
    result = asyncio.run(A.difficulty_label_activity(payload))
    assert result['tier'] == 'informative'
    stored = json.loads((ws.scenario_dir(spec.id) / 'difficulty.json').read_text())
    assert stored['trial_ids'] == payload['trial_ids'] and stored['compiled_hash'] == 'c' * 64 and stored['spec_hash'] == spec.spec_hash
    asyncio.run(A.difficulty_label_activity(payload))
    assert json.loads((ws.scenario_dir(spec.id) / 'difficulty.json').read_text()) == stored
    bad = ws.root / 'trials' / payload['trial_ids'][0] / 'record.json'
    record = json.loads(bad.read_text()); record['compiled_hash'] = 'old-build'; atomic_json(bad, record)
    from temporalio.exceptions import ApplicationError
    with pytest.raises(ApplicationError, match='current scenario and compiled build'):
        asyncio.run(A.difficulty_label_activity(payload))
    assert json.loads((ws.scenario_dir(spec.id) / 'difficulty.json').read_text()) == stored
