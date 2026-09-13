"""Task origin on the console: the ``?pool=`` facet (novel repository tasks vs SWE-bench pool A) filters the Scenarios, Trials
and Leaderboard pages through both readers, is sticky across paging and forms, is stated in every count line and the
leaderboard header; every scenario id carries its origin badge; the Candidates page groups sources under two headings."""
from __future__ import annotations

import json

import pytest

from swesim.dashboard import access, origin, pages
from swesim.index import models as M
from swesim.index.files import FileIndexReader
from swesim.index.store import IndexStore
from swesim.models import ScenarioSpec, minimal_spec_dict
from swesim.settings import get_settings
from swesim.storage.layout import Layout
from test_dashboard import site as site  # noqa: F401 - fixture
from test_score import COMPILED, IMAGE, write_trial

NOVEL_ID = "acme-widget-13"
POOL_ID = "psf-requests-1142"
POOL_KEY = "swebench-annotated--a"


def _spec(sid: str, source: str, ref: str) -> ScenarioSpec:
    d = minimal_spec_dict()
    d["id"], d["source"], d["source_ref"] = sid, source, ref
    return ScenarioSpec.model_validate(d)


def _add_scenario(ws, spec: ScenarioSpec, *, eligible: bool) -> None:
    """A valid scenario directory next to the fixture's (the same files make_workspace writes), optionally board-eligible."""
    sdir = ws.scenario_dir(spec.id)
    sdir.mkdir(parents=True)
    (sdir / "scenario.json").write_text(spec.model_dump_json(indent=2))
    (sdir / "validation.json").write_text(json.dumps({
        "status": "valid", "failures": [], "expected_base": {"t1": "failed"}, "expected_gold": {"t1": "passed"},
        "controls": {"nop": {"reward": 0, "success": 0, "outcome": "valid-incorrect"}}, "probe": {}, "trial_dirs": {}, "spec_hash": spec.spec_hash,
        "source": spec.source.value, "source_ref": spec.source_ref, "environment_provider": "modal", "image_digest": IMAGE, "compiled_hash": COMPILED}))
    (sdir / "compile.json").write_text(json.dumps({"compiled_hash": COMPILED, "spec_hash": spec.spec_hash, "run_config_hash": "x" * 64, "image_digest": IMAGE,
                                                   "brief_profile": "withheld", "task_dirs": {}}))
    if eligible:
        (sdir / "difficulty.json").write_text(json.dumps({"tier": "informative", "model": "openrouter/deepseek/deepseek-v3.2", "hidden": {"n": 2, "successes": 1},
                                                          "full": {"n": 1, "successes": 1}, "labelled_at": "2026-09-13T08:00:00+00:00", "trial_ids": []}))
        (sdir / "audit.json").write_text(json.dumps({"coherent": True, "fair_tests": True, "plausible_withholding": True, "reasons": {}, "model": "anthropic/claude-opus-5",
                                                     "audited_at": "2026-09-13T08:00:00+00:00", "rubric_version": "v1"}))


@pytest.fixture
def origins(site):
    """The fixture site (one authored scenario) plus a novel repository scenario with two trials, a SWE-bench pool A scenario
    with one trial, and a pool A candidates source."""
    ws = site["ws"]
    novel = _spec(NOVEL_ID, "github-repo", "https://github.com/acme/widget/pull/13")
    pool = _spec(POOL_ID, "swebench-annotated", "psf__requests-1142")
    _add_scenario(ws, novel, eligible=True)
    _add_scenario(ws, pool, eligible=True)
    t_novel = write_trial(ws, novel)
    t_novel_wrong = write_trial(ws, novel, attempt=1, outcome_code=2, success=0, reward=0.0)
    t_pool = write_trial(ws, pool)
    cdir = ws.root / "candidates"
    (cdir / f"{POOL_KEY}.jsonl").write_text(json.dumps({"source": "swebench-annotated", "instance_id": "psf__requests-1142", "repo": "psf/requests", "base_commit": "abc"}) + "\n")
    (cdir / f"{POOL_KEY}.rejects.json").write_text(json.dumps({"no-linked-issue": 1}))
    access.reset()
    return {**site, "t_novel": t_novel, "t_novel_wrong": t_novel_wrong, "t_pool": t_pool}


# ---------------------------------------------------------------- the readers

def test_source_filter_selects_trials_and_scenarios_in_both_readers(origins):
    ws = origins["ws"]
    settings = get_settings()
    layout = Layout(root=ws.root)
    files = FileIndexReader(layout, ws.registry_path, settings=settings)
    store = IndexStore("sqlite://", settings=settings)
    store.migrate()
    store.rebuild_from_layout(layout, ws.registry_path)
    for reader in (files, store):
        pool = reader.trials(M.TrialFilter(source=["swebench-annotated"]))
        assert pool.total == 1 and {t.trial_id for t in pool.items} == {origins["t_pool"]}, type(reader).__name__
        novel = reader.trials(M.TrialFilter(source=["github-repo"]))
        assert novel.total == 2 and {t.scenario_id for t in novel.items} == {NOVEL_ID}
        assert reader.trials(M.TrialFilter(source=["github-repo", "swebench-annotated"])).total == 3
        assert reader.trials(M.TrialFilter(source=["github-repo"], status=["completed"], limit=1)).total == 2  # composes with the other filters and paging
        assert reader.trials(M.TrialFilter(source=["authored"])).total == reader.trials(M.TrialFilter(scenario_id=[origins["spec"].id])).total
        assert {s.scenario_id for s in reader.scenarios(M.ScenarioFilter(source=["swebench-annotated"])).items} == {POOL_ID}
        assert {s.scenario_id for s in reader.scenarios(M.ScenarioFilter(source=["github-repo"], latest_only=True)).items} == {NOVEL_ID}
    assert files.trials(M.TrialFilter()).total == store.trials(M.TrialFilter()).total  # the unfiltered listing is untouched


# ---------------------------------------------------------------- scenarios

def test_scenarios_page_facet_filters_rows_states_the_subset_and_badges_every_id(origins):
    c = origins["client"]
    html = c.get("/scenarios").text
    assert "3 scenario version(s) · showing all task origins" in html
    assert ">novel</span>" in html and ">SWE-bench A</span>" in html and ">authored</span>" in html
    assert "aria-current=true data-facet='all'" in html and "data-facet='swebench-a'" in html and "data-facet='novel'" in html
    a = c.get(f"/scenarios?{origin.PARAM}=swebench-a").text
    assert "1 scenario version(s) · showing SWE-bench pool A only" in a and POOL_ID in a and NOVEL_ID not in a
    assert "aria-current=true data-facet='swebench-a'" in a
    api = c.get(f"/api/scenarios?{origin.PARAM}=novel").json()
    assert api["total"] == 1 and api["items"][0]["scenario_id"] == NOVEL_ID and api["items"][0]["origin_kind"] == "novel" and api["items"][0]["source"] == "github-repo"
    assert c.get(f"/api/scenarios?{origin.PARAM}=bogus").json()["total"] == 3  # an unknown facet value means All
    assert c.get(f"/api/scenarios?{origin.PARAM}=swebench-a&source=github-repo").json()["total"] == 1  # the facet wins over an explicit source
    # paging links keep the facet (server-rendered, no JS needed)
    paged = c.get(f"/scenarios?{origin.PARAM}=novel&limit=1&latest=0").text
    assert "1 scenario version(s)" in paged
    all_paged = c.get("/scenarios?limit=1&validation_status=valid").text
    assert "href='?validation_status=valid&offset=1&limit=1'" in all_paged


# ---------------------------------------------------------------- trials

def test_trials_page_facet_is_sticky_across_paging_and_the_filter_form(origins):
    c = origins["client"]
    api = c.get(f"/api/trials?{origin.PARAM}=novel").json()
    assert api["total"] == 2 and {t["scenario_id"] for t in api["items"]} == {NOVEL_ID} and {t["origin_kind"] for t in api["items"]} == {"novel"}
    assert {t["scenario_source"] for t in api["items"]} == {"github-repo"}
    html = c.get(f"/trials?{origin.PARAM}=novel&limit=1").text
    assert "2 attempt(s) match · showing Novel (repository) only" in html
    assert f"href='?{origin.PARAM}=novel&offset=1&limit=1'" in html  # paging carries the facet
    assert f"<input type='hidden' name='{origin.PARAM}' value='novel'>" in html  # the filter form carries it too
    assert "aria-current=true data-facet='novel'" in html
    a = c.get(f"/trials?{origin.PARAM}=swebench-a").text
    assert origins["t_pool"][:12] in a and origins["t_novel"][:12] not in a and origins["done"][:12] not in a
    assert "1 attempt(s) match · showing SWE-bench pool A only" in a
    everything = c.get("/trials").text
    assert ">novel</span>" in everything and ">SWE-bench A</span>" in everything and ">authored</span>" in everything
    assert "showing all task origins" in everything and f"name='{origin.PARAM}'" not in everything
    # the scenario page's trial table and the home page carry the badge as well
    assert ">SWE-bench A</span>" in c.get(f"/scenario/{POOL_ID}").text.split("Trials on this scenario", 1)[1]
    home = c.get("/api/home").json()
    assert {t["origin_kind"] for t in home["recent"]} >= {"novel", "SWE-bench A"}


# ---------------------------------------------------------------- leaderboard

def test_leaderboard_header_cells_matrix_and_gate_reflect_the_subset(origins):
    c = origins["client"]
    everything = c.get("/leaderboard?purpose=calibration").text
    assert "data-subset>· all task origins</span>" in everything and NOVEL_ID in everything and POOL_ID in everything and "2 / 3" in everything
    a = c.get(f"/leaderboard?purpose=calibration&{origin.PARAM}=swebench-a").text
    assert "data-subset>· SWE-bench pool A only</span>" in a and "1 / 1" in a and f"<input type='hidden' name='{origin.PARAM}' value='swebench-a'>" in a
    matrix = a.split("Per-scenario matrix", 1)[1].split("Pass rate by condition", 1)[0]
    assert POOL_ID in matrix and NOVEL_ID not in matrix and "SWE-bench A" in matrix
    gate = a.split("<h2>Gate</h2>", 1)[1]
    assert POOL_ID in gate and NOVEL_ID not in gate and ">SWE-bench A</span>" in gate
    api_a = c.get(f"/api/leaderboard?purpose=calibration&{origin.PARAM}=swebench-a").json()
    assert len(api_a["items"]) == 1 and api_a["items"][0]["n"] == 1 and api_a["items"][0]["scenarios"] == 1
    assert {g["scenario_id"] for g in api_a["gate"]} == {POOL_ID}
    api_n = c.get(f"/api/leaderboard?purpose=calibration&{origin.PARAM}=novel").json()
    assert api_n["items"][0]["n"] == 2 and api_n["items"][0]["scenarios"] == 1 and {g["scenario_id"] for g in api_n["gate"]} == {NOVEL_ID}
    api_all = c.get("/api/leaderboard?purpose=calibration").json()
    assert api_all["items"][0]["n"] == 3 and api_all["items"][0]["scenarios"] == 2
    evidence = c.get(f"/leaderboard/evidence?purpose=calibration&{origin.PARAM}=novel&limit=1").text
    assert "2 admitted attempts" in evidence and "showing Novel (repository) only" in evidence and f"{origin.PARAM}=novel" in evidence.split("class='paging'", 1)[1]


# ---------------------------------------------------------------- candidates

def test_candidates_page_groups_sources_under_repositories_and_swebench_pool_a(origins):
    c = origins["client"]
    html = c.get("/candidates").text
    assert "<h3>Repositories" in html and "<h3>SWE-bench pool A" in html
    repos = html.split("<h3>Repositories", 1)[1].split("<h3>SWE-bench pool A", 1)[0]
    pool = html.split("<h3>SWE-bench pool A", 1)[1].split("<div class='side-by-side'", 1)[0]
    assert "github-repo--acme__widget" in repos and POOL_KEY not in repos
    assert POOL_KEY in pool and "github-repo--acme__widget" not in pool
    assert "<optgroup label='Repositories'>" in html and "<optgroup label='SWE-bench pool A'>" in html
    api = c.get("/api/candidates").json()
    assert {r["origin_kind"] for r in api["items"]} == {"novel", "SWE-bench A"}
    # both headings render even when one group is empty
    empty_page = M.Page[dict](items=[], total=0, offset=0, limit=200)
    body = pages.candidates_body(empty_page, [], [], {}, ["github-repo--x__y--bugfix"], None, [{"source_key": "github-repo--x__y--bugfix"}], None)
    assert "<h3>SWE-bench pool A" in body and "No SWE-bench pool A scan recorded" in body and "<h3>Repositories <span class='muted small'>1 source</span>" in body


# ---------------------------------------------------------------- badges elsewhere, and the mapping itself

def test_badge_sits_next_to_the_scenario_id_on_detail_episode_and_search_pages(origins):
    c = origins["client"]
    h1 = c.get(f"/scenario/{POOL_ID}").text.split("<h1>", 1)[1].split("</h1>", 1)[0]
    assert POOL_ID in h1 and ">SWE-bench A</span>" in h1
    assert ">novel</span>" in c.get(f"/scenario/{NOVEL_ID}").text.split("<h1>", 1)[1].split("</h1>", 1)[0]
    lead = c.get(f"/trial/{origins['t_pool']}").text.split("<p class='lead'>", 1)[1].split("</p>", 1)[0]
    assert POOL_ID in lead and ">SWE-bench A</span>" in lead
    assert ">novel</span>" in c.get(f"/trial/{origins['t_novel']}").text.split("<p class='lead'>", 1)[1].split("</p>", 1)[0]
    assert ">SWE-bench A</span>" in c.get(f"/search?q={POOL_ID}").text
    assert origin.sources_for("novel") == ["github-repo"] and origin.sources_for("swebench-a") == ["swebench-annotated"] and origin.sources_for("x") is None
    assert origin.of_source_key("swebench-annotated:a").key == "swebench-a" and origin.of_source_key(POOL_KEY).key == "swebench-a"
    assert origin.of_source_key("github-repo--o__r--feature").key == "novel"
    assert origin.badge_label("github-repo") == "novel" and origin.badge_label("swebench-annotated") == "SWE-bench A" and origin.badge_label("authored") == "authored"
    assert "chip info origin" in origin.badge("github-repo") and "chip neutral origin" in origin.badge("swebench-annotated") and "—" in origin.badge(None)
