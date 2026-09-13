"""Immutable per-scan discovery records under candidates/<source_key>/scans/: written once, hash-named, every PR kept."""
import hashlib
import json
import subprocess
from datetime import UTC, datetime

import pytest

from swesim.paths import atomic_json
from swesim.sources import github_repo as G, observations as obs
from swesim.storage.layout import Layout, LayoutError
from test_github_repo import FakeGH, _routes

AT = datetime(2026, 9, 13, 10, tzinfo=UTC)


def scan_of(monkeypatch, **kw):
    monkeypatch.setattr(subprocess, "run", FakeGH(_routes()))
    return G.discover_scan("https://github.com/o/r", "2026-06-01", max_prs=150, kind="feature", scanned_at=AT, **kw)


def test_discover_scan_keeps_every_pr_and_its_histogram_matches_the_legacy_rejects(monkeypatch):
    cands, rejects, scan = scan_of(monkeypatch, workflow_id="create:x", run_id="r1")
    assert [c["pr"] for c in cands] == [42]
    assert rejects == {"diff-too-small": 1, "no-linked-issue": 1, "linked-target-is-pr": 1} == scan.rejects
    assert scan.source_key == "github-repo--o__r--feature" and scan.repo == "o/r" and scan.band == "feature"
    assert scan.funnel == {"listed": 105, "merged-in-range": 4, "linked-issue": 2, "band-passed": 1, "retained": 1}
    assert scan.rejects_sha256 == hashlib.sha256(json.dumps(rejects, sort_keys=True, indent=2).encode()).hexdigest()
    by_pr = {o.pr_number: o for o in scan.observations}
    assert by_pr[1].reason == "not-merged" and by_pr[101].reason == "before-since" and by_pr[42].status == "retained"
    assert "not-merged" not in scan.rejects  # list-page prefilters stay out of the legacy histogram but are recorded
    decisions = obs.decisions(scan.observations)
    assert {d["pr_number"]: d["stage"] for d in decisions if d["pr_number"] in (1, 7, 9, 42)} == {1: "listed", 7: "linked-issue", 9: "merged-in-range", 42: "retained"}
    assert "_observation_seq" not in G.candidate_to_row(cands[0])


def test_cap_is_a_recorded_decision_and_the_rejects_artifact_agrees(monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeGH(_routes()))
    observations = []
    cands, _ = G.discover("o/r", "2026-06-01", max_prs=150, kind="feature", observations=observations)
    capped = obs.retain_selected(observations, set())
    cut = next(o for o in capped if o.pr_number == 42)
    assert cut.status == "rejected" and cut.reason == "max-candidates-cut" and "retained" not in cut.stages and "band-passed" in cut.stages
    rejects = obs.legacy_rejects(capped)
    scan = obs.DiscoveryScan(source_key="github-repo--o__r--feature", repo="o/r", band="feature", since="2026-06-01", max_prs=150, scanned_at=AT,
                             observations=capped, funnel=obs.funnel(capped), rejects=rejects,
                             rejects_sha256=hashlib.sha256(obs.rejects_bytes(rejects)).hexdigest())
    assert scan.rejects["max-candidates-cut"] == 1 and scan.funnel["retained"] == 0 and scan.funnel["band-passed"] == 1
    with pytest.raises(ValueError, match="histogram"):
        obs.DiscoveryScan(**{**scan.model_dump(), "rejects": {"diff-too-small": 1}, "rejects_sha256": scan.rejects_sha256})
    with pytest.raises(ValueError, match="funnel"):
        obs.DiscoveryScan(**{**scan.model_dump(), "funnel": {**scan.funnel, "listed": 1}})
    with pytest.raises(ValueError, match="run ids"):
        obs.DiscoveryScan(**{**scan.model_dump(), "workflow_id": "create:x"})
    with pytest.raises(ValueError):
        G.discover_scan("o/r", "2026-06-01", max_candidates=0)


def test_scan_files_are_hash_named_written_once_and_refused_when_tampered(tmp_path, monkeypatch):
    layout = Layout(root=tmp_path)
    _, _, scan = scan_of(monkeypatch)
    path, digest = obs.write_scan(layout, scan)
    assert path == layout.scan_file(scan.source_key, digest) == tmp_path / "candidates" / scan.source_key / "scans" / f"{digest}.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest == obs.scan_id(scan)
    assert obs.read_scan(path) == scan
    before = path.stat().st_mtime_ns
    assert obs.write_scan(layout, scan) == (path, digest) and path.stat().st_mtime_ns == before  # immutable: no rewrite
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".write-")]
    tampered = path.read_bytes().replace(b'"diff-too-small": 1', b'"diff-too-small": 0')
    path.write_bytes(tampered)
    with pytest.raises(ValueError, match="hash"):
        obs.read_scan(path)
    with pytest.raises(LayoutError):
        layout.scan_file(scan.source_key, "not-a-sha")
    with pytest.raises(LayoutError):
        layout.scan_dir("../escape")
    # The legacy rejects artifact is byte-identical to the histogram the scan describes.
    atomic_json(layout.candidates_file(scan.source_key, "rejects"), scan.rejects)
    assert hashlib.sha256(layout.candidates_file(scan.source_key, "rejects").read_bytes()).hexdigest() == scan.rejects_sha256


def test_latest_scans_reads_the_newest_valid_record_per_source(tmp_path, monkeypatch):
    layout = Layout(root=tmp_path)
    assert obs.latest_scans(layout) == {}
    _, _, first = scan_of(monkeypatch)
    later = first.model_copy(update={"scanned_at": datetime(2026, 9, 13, 11, tzinfo=UTC), "max_prs": 200})
    obs.write_scan(layout, first)
    newest_path, newest_id = obs.write_scan(layout, later)
    other = first.model_copy(update={"source_key": "github-repo--o__r--bugfix", "band": "bugfix"})
    obs.write_scan(layout, other)
    (layout.scan_dir(first.source_key) / ("0" * 64 + ".json")).write_text("{}")  # tampered/invalid: skipped, never rendered
    (layout.candidates_dir() / "github-repo--o__r--feature.jsonl").write_text("")  # a flat file next to the scans dir is not a source dir
    latest = obs.latest_scans(layout)
    assert set(latest) == {first.source_key, other.source_key}
    view = latest[first.source_key]
    assert view["scan_sha256"] == newest_id and view["scan_path"] == layout.relative(newest_path) and view["max_prs"] == 200
    assert view["funnel"]["listed"] == 105 and len(view["decisions"]) == 105 and view["discovered_at"] == "2026-09-13T11:00:00+00:00"
    assert not any(key in view for key in ("observations", "patch", "thread"))
    assert obs.latest_scans(layout, other.source_key) == {other.source_key: latest[other.source_key]}
    assert obs.latest_scans(layout, "github-repo--none") == {}
