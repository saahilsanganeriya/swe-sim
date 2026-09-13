"""Every listed PR and filter/cap decision is preserved, without network or Modal."""
import asyncio
import json
import subprocess
from pathlib import Path

from swesim.sources import github_repo as G
from swesim.sources.observations import funnel, retain_selected
from test_github_repo import FakeGH, _routes


def test_full_discovery_observations_include_prefilters_links_bands_and_reject_ids(monkeypatch):
    monkeypatch.setattr(subprocess,'run',FakeGH(_routes()))
    observations=[]
    candidates,_=G.discover('o/r',since='2026-06-01',max_prs=150,kind='feature',observations=observations)
    assert len(observations)==105 and len(candidates)==1
    by_pr={o.pr_number:o for o in observations}
    assert by_pr[1].reason=='not-merged' and by_pr[101].reason=='before-since'
    assert by_pr[9].reason=='no-linked-issue' and by_pr[11].reason=='linked-target-is-pr'
    assert by_pr[7].reason=='diff-too-small' and by_pr[7].linked_issue_numbers==[6]
    assert by_pr[42].status=='retained' and by_pr[42].source_changed_lines==250
    assert funnel(observations)=={'listed':105,'merged-in-range':4,'linked-issue':2,'band-passed':1,'retained':1}
    capped=retain_selected(observations,set())
    assert next(o for o in capped if o.pr_number==42).reason=='max-candidates-cut'
    assert funnel(capped)['band-passed']==1 and funnel(capped)['retained']==0


def test_fetch_failure_is_an_error_observation_not_a_rejected_task(monkeypatch):
    routes=_routes()
    routes['repos/o/r/pulls/42']=subprocess.CalledProcessError(1,['gh','api'],stderr='HTTP 500 secret detail')
    monkeypatch.setattr(subprocess,'run',FakeGH(routes))
    observations=[]
    G.discover('o/r',since='2026-06-01',max_prs=150,kind='feature',observations=observations)
    failed=next(o for o in observations if o.pr_number==42)
    assert failed.status=='error' and failed.error_class=='CalledProcessError'
    assert failed.source_changed_lines is None and failed.linked_issue_numbers is None
    assert 'secret detail' not in failed.model_dump_json()
