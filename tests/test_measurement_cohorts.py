"""Measurement gates preserve exact specs, compatible execution settings and requested k."""
from datetime import UTC, datetime
from decimal import Decimal

from swesim.index import models as M, reports


def scenario():
    return M.ScenarioRow(scenario_id='example-scenario',version=1,spec_hash='a'*64,source='github-repo',source_ref='https://github.com/example/project',validation_status='valid',compiled_hash='b'*64,image_digest=None,difficulty_tier='informative',difficulty_screen=None,task_sense={'coherent':True,'fair_tests':True},registry_json={},archived_at=None,origin='cloudrun')


def trial(number=1, **over):
    return M.TrialRow(**{**dict(trial_id=str(number)*24,scenario_id='example-scenario',version=1,spec_hash='a'*64,condition='interactive',brief_profile='withheld',model='anthropic/claude-haiku-4-5-20251001',provider='anthropic',run_config_hash='c'*64,attempt_index=number,purpose='comparison',status='completed',outcome_code=1,reward=Decimal(1),success=True,stakeholder_model='anthropic/claude-opus-5',stakeholder_prompt_version='v3',origin='cloudrun',finished_at=datetime(2026,9,13,tzinfo=UTC)),**over})


def test_known_wrong_hash_never_uses_version_or_latest():
    assert reports.scenario_for_trial(trial(spec_hash='f'*64), [scenario()]) is None
    assert not reports.compute_leaderboard([trial(spec_hash='f'*64)], [scenario()], M.LeaderboardFilter())


def test_origin_stakeholder_scaffold_and_solver_settings_never_pool():
    cfg={'agent':'mini-swe-agent','agent_version':'2.4.6','solver_timeout_sec':2400,'agent_config':'scaffold-a'}
    baseline=trial(evaluation_config=cfg)
    variants=[trial(2,origin='vm-systemd',evaluation_config=cfg),trial(3,stakeholder_prompt_version='v2',evaluation_config=cfg),
              trial(4,stakeholder_model='openai/gpt-5.5',evaluation_config=cfg),
              trial(5,evaluation_config={**cfg,'agent_config':'scaffold-b'}),trial(6,evaluation_config={**cfg,'solver_timeout_sec':60})]
    board=reports.compute_leaderboard([baseline,*variants],[scenario()],M.LeaderboardFilter())
    assert len(board)==6 and all(row.n==1 for row in board)
    assert len({row.evaluation_cohort for row in board})==6
    selected=reports.compute_leaderboard([baseline,*variants],[scenario()],M.LeaderboardFilter(evaluation_cohort=board[0].evaluation_cohort))
    assert len(selected)==1 and selected[0].n==1


def test_same_semantic_config_can_aggregate_distinct_task_builds():
    from swesim.index.cohorts import semantic_config
    a=semantic_config({'model':'m','condition':'hidden','compiled_hash':'a','agent':'mini-swe-agent','k':2},{})
    b=semantic_config({'model':'m','condition':'hidden','compiled_hash':'b','agent':'mini-swe-agent','k':3},{})
    assert a==b
    board=reports.compute_leaderboard([trial(evaluation_config=a),trial(2,run_config_hash='d'*64,evaluation_config=b)],[scenario()],M.LeaderboardFilter())
    assert len(board)==1 and board[0].n==2


def test_trends_separate_measurement_cohorts_and_link_exact_evidence():
    from swesim.dashboard.trends import summarize
    result=summarize([trial(evaluation_config={'agent_config':'one'}),trial(2,evaluation_config={'agent_config':'two'})],[scenario()],{'purpose':'comparison'})
    assert len(result['history'])==2 and all(row['n']==1 for row in result['history'])
    assert all('evaluation_cohort=' in row['evidence_url'] for row in result['history'])


def test_requested_k_is_frozen_by_planner_and_screen_counts_stay_two_plus_one(tmp_path, monkeypatch):
    from swesim.workflows import activities as A
    from test_score import make_spec, make_workspace
    spec=make_spec()
    ws=make_workspace(tmp_path,spec)
    monkeypatch.setattr(A,'WS',ws)
    base={'scenario_id':spec.id,'models':['openrouter/deepseek/deepseek-v3.2'],'conditions':['hidden'],'stakeholder_url':'https://example.com'}
    planned=A.plan_trials({**base,'k':2})['trials']
    assert len(planned)==2 and {t['run_config']['k'] for t in planned}=={2}
    screen=A.plan_trials({**base,'purpose':'screen','k':2})['trials']
    assert [t['condition'] for t in screen].count('hidden')==2
    assert [t['condition'] for t in screen].count('full')==1
    assert {t['run_config']['k'] for t in screen}=={1}


def test_score_activity_and_index_preserve_requested_k(tmp_path, monkeypatch):
    import asyncio
    from swesim.workflows import activities as A
    from swesim.index import project
    from test_score import make_spec, make_workspace, write_trial
    spec=make_spec()
    ws=make_workspace(tmp_path,spec)
    monkeypatch.setattr(A,'WS',ws)
    write_trial(ws,spec,attempt=0)
    write_trial(ws,spec,attempt=1)
    asyncio.run(A.score_activity({'scenario_id':spec.id,'k':2}))
    rows=project.project_scores(ws.layout,spec.id,origin='cloudrun')
    assert len(rows)==1 and rows[0].requested_k==2 and rows[0].pass_at_k==1 and rows[0].pass_pow_k==1
