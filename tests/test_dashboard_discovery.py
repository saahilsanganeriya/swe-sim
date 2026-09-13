"""The Candidates page renders the full per-source funnel and every PR decision from the immutable scan files."""
import json
import subprocess
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from swesim.dashboard import pages
from swesim.dashboard.app import app, layout_dep, reader_dep, settings_dep
from swesim.index import models as M
from swesim.index.files import FileIndexReader
from swesim.paths import atomic_json
from swesim.settings import Settings
from swesim.sources import github_repo as G, observations as obs
from swesim.storage.layout import Layout
from test_github_repo import FakeGH, _routes

AT = datetime(2026, 9, 13, 10, tzinfo=UTC)
KEY = "github-repo--o__r--feature"


@pytest.fixture
def site(tmp_path, monkeypatch):
    settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_WORKSPACE": str(tmp_path / "workspace"),
                                  "SWESIM_REGISTRY": str(tmp_path / "workspace" / "registry.json")})
    layout = Layout(root=settings.workspace)
    monkeypatch.setattr(subprocess, "run", FakeGH(_routes()))
    cands, rejects, scan = G.discover_scan("o/r", "2026-06-01", max_prs=150, kind="feature", scanned_at=AT, workflow_id="create:o-r", run_id="r1")
    rows = [G.candidate_to_row(c) for c in cands]
    layout.candidates_dir().mkdir(parents=True)
    layout.candidates_file(KEY, "mined").write_text("".join(json.dumps(r) + "\n" for r in rows))
    layout.candidates_file(KEY, "judged").write_text("".join(json.dumps({**r, "judge": {"keep": True}}) + "\n" for r in rows))
    atomic_json(layout.candidates_file(KEY, "rejects"), rejects)
    path, digest = obs.write_scan(layout, scan)
    # A second source with only a legacy rejects file: it must still appear, with dashes for the discovery stages.
    atomic_json(layout.candidates_file("github-repo--legacy__repo--bugfix", "rejects"), {"no-linked-issue": 9})
    reader = FileIndexReader(layout, settings.registry_path, settings=settings)
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[reader_dep] = lambda: reader
    app.dependency_overrides[settings_dep] = lambda: settings
    app.dependency_overrides[layout_dep] = lambda: layout
    yield {"client": TestClient(app), "layout": layout, "scan": scan, "digest": digest, "path": path, "reader": reader}
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def test_candidates_page_shows_every_source_funnel_from_the_scan_files(site):
    html = site["client"].get("/candidates").text
    assert "data-table='source-funnels-novel'" in html and "data-table='source-funnels-swebench-a'" in html and "PRs listed" in html
    row = html.split(f"<tr data-key='{KEY}'>", 1)[1].split("</tr>", 1)[0]
    cells = [c.split(">", 1)[1].rsplit("<", 1)[0] for c in row.split("<td")[1:]]
    assert cells[3:10] == ["105", "4", "2", "1", "1", "1", "0"], cells  # listed, merged in range, linked issue, band passed, retained, judged, probed
    assert f"/artifacts?path={site['layout'].relative(site['path'])}" in row and site["digest"][:12] in row
    legacy = html.split("<tr data-key='github-repo--legacy__repo--bugfix'>", 1)[1].split("</tr>", 1)[0]
    assert legacy.count("—") >= 5 and "/artifacts?path=" not in legacy
    assert "Select a source key to list every PR decision" in html and "data-table='decisions'" not in html
    assert "discovery:no-linked-issue" in html  # the indexed histogram is still the chart's source


def test_selected_source_lists_every_pr_decision_with_reason_and_stage(site):
    html = site["client"].get("/candidates", params={"source_key": KEY}).text
    assert "data-table='decisions'" in html and html.count("<tr data-key=") >= 105 + 1 + 1
    rows = html.split("data-table='decisions'", 1)[1]
    assert rows.count("not-merged") == 96 and rows.count("before-since") == 5  # 100 closed rows, 4 of them merged
    for pr, stage, status, reason in ((7, "linked-issue", "rejected", "diff-too-small"), (9, "merged-in-range", "rejected", "no-linked-issue"),
                                      (11, "merged-in-range", "rejected", "linked-target-is-pr"), (42, "retained", "retained", None)):
        line = rows.split(f"<tr data-key='{pr}'>", 1)[1].split("</tr>", 1)[0]
        assert f"href='https://github.com/o/r/pull/{pr}'" in line and stage in line and status in line, line
        assert (reason in line) if reason else ("—" in line), line
    assert site["digest"] in html and f"/artifacts?path={site['layout'].relative(site['path'])}" in html and "create:o-r" in html
    assert "Implements streaming" not in html and "diff --git" not in html  # decisions carry identities and reasons only


def test_api_candidates_carries_the_funnel_rows_and_decisions(site):
    body = site["client"].get("/api/candidates", params={"source_key": KEY}).json()
    assert [f["source_key"] for f in body["source_funnels"]] == [KEY]
    funnel = body["source_funnels"][0]
    assert (funnel["listed"], funnel["retained"], funnel["rejected_prs"], funnel["errors"]) == (105, 1, 104, 0)
    assert len(body["decisions"]) == 105 and body["decisions"][0]["status"] == "rejected" and body["decisions"][-1]["pr_number"] == 42
    everything = site["client"].get("/api/candidates").json()
    assert {f["source_key"] for f in everything["source_funnels"]} == {KEY} and everything["decisions"] == []


def test_source_funnels_join_candidate_stages_and_tolerate_missing_scans(site):
    reader = site["reader"]
    candidates = reader.candidates(M.CandidateFilter()).items
    latest = obs.latest_scans(site["layout"])
    rows = pages.source_funnels(latest, candidates, [KEY, "github-repo--legacy__repo--bugfix"])
    by_key = {r["source_key"]: r for r in rows}
    assert by_key[KEY]["judged"] == 1 and by_key[KEY]["probed"] == 0 and by_key[KEY]["retained"] == 1
    assert by_key[KEY]["band"] == "feature" and by_key[KEY]["repo"] == "o/r"
    legacy = by_key["github-repo--legacy__repo--bugfix"]
    assert legacy["listed"] is None and legacy["band"] == "bugfix" and "scan_path" not in legacy and legacy["rejected_prs"] is None
    decisions = pages.decision_views(latest[KEY])
    assert [d["status"] for d in decisions[:1]] == ["rejected"] and decisions[-1]["status"] == "retained"
    assert pages.decision_views(None) == []
