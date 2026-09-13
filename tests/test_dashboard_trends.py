"""The Trends page as the capability-gradient view: tier ordering (roles.TIERS A→E), the per-scenario gradient chips
(suspicious / saturated / holds), the three condition deltas, cost and integrity blocks, nothing below n = 1, the Home
tile "frontier vs last-gen gap", and the routes' evidence links. Offline, over the file reader."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from swesim.dashboard import access, charts, gradient, render, render_trends
from swesim.index import models as M
from swesim.roles import TIERS
from test_dashboard import reader_of, site as site  # noqa: F401 - fixture
from test_dashboard_origin import _add_scenario, _spec
from test_score import HAIKU, write_trial

DS_OLD = "openrouter/deepseek/deepseek-chat-v3-0324"   # tier A
GLM_OLD = "openrouter/z-ai/glm-4.5"                     # tier A
DS_NEW = "openrouter/deepseek/deepseek-v3.2"            # tier B
SONNET = "anthropic/claude-sonnet-5"                    # tier D
FABLE = "anthropic/claude-fable-5-1"                    # tier E
MYSTERY = "openai/gpt-99-unknown"                       # outside the tier table


def trial(number: int, model: str, scenario: str = "s1", condition: str = "hidden", success: bool = True, profile: str = "withheld", **over) -> M.TrialRow:
    base = dict(trial_id=f"{number:024d}", scenario_id=scenario, version=1, spec_hash="a" * 64, condition=condition, brief_profile="natural" if condition == "full" else profile,
                model=model, provider=model.split("/")[0], run_config_hash="c" * 64, attempt_index=number, purpose="comparison", status="completed", outcome_code=1 if success else 2,
                reward=Decimal(1 if success else 0), success=success, stakeholder_model="anthropic/claude-opus-5", stakeholder_prompt_version="v3", origin="cloudrun",
                finished_at=datetime(2026, 9, 13, tzinfo=UTC), integrity_signal_count=0, questions_asked=1 if condition == "interactive" else None)
    return M.TrialRow(**{**base, **over})


def scenario_row(sid: str, label: str | None = "informative") -> M.ScenarioRow:
    return M.ScenarioRow(scenario_id=sid, version=1, spec_hash="a" * 64, source="github-repo", source_ref="https://github.com/example/p", validation_status="valid",
                         compiled_hash="b" * 64, image_digest=None, difficulty_tier=label, difficulty_screen=None, task_sense={"coherent": True, "fair_tests": True},
                         registry_json={}, archived_at=None, origin="cloudrun")


# ---------------------------------------------------------------- tier ordering (roles.TIERS is the axis)

def test_models_order_by_tier_a_to_e_then_family_then_id_and_unknown_last():
    assert render.TIERS == {t: tuple(ms) for t, ms in TIERS.items()}  # the console's axis is the brief's table, verbatim
    ordered = gradient.sort_models([MYSTERY, FABLE, HAIKU, DS_NEW, GLM_OLD, DS_OLD, SONNET])
    assert ordered == [DS_OLD, GLM_OLD, DS_NEW, HAIKU, SONNET, FABLE, MYSTERY]
    assert [gradient.tier_label(m) for m in ordered] == ["A", "A", "B", "C", "D", "E", "other"]
    cells = gradient.accuracy_cells([trial(1, FABLE), trial(2, DS_OLD), trial(3, HAIKU), trial(4, DS_OLD, condition="full")], {"purpose": "comparison"})
    assert [(c["condition"], c["model"]) for c in cells] == [("full", DS_OLD), ("hidden", DS_OLD), ("hidden", HAIKU), ("hidden", FABLE)]
    assert all(c["n"] >= 1 for c in cells) and cells[1]["evidence_url"].startswith("/leaderboard/evidence?") and "condition=hidden" in cells[1]["evidence_url"]


# ---------------------------------------------------------------- the gradient chips

def test_gradient_state_suspicious_when_last_gen_beats_frontier_by_more_than_one_trial():
    # tier A 3/3 vs best tier E 0/2: +100 pp, one trial's worth is max(1/3, 1/2) = 50 pp
    state, reason = gradient.gradient_state({DS_OLD: (3, 3), FABLE: (2, 0), SONNET: (2, 0)})
    assert state == "suspicious" and DS_OLD in reason and FABLE in reason and "+100 pp" in reason
    # exactly one trial's worth is not suspicious: A 2/2 (1.0) vs E 1/2 (0.5), margin 0.5
    assert gradient.gradient_state({DS_OLD: (2, 2), FABLE: (2, 1)})[0] == "not-monotone"
    # at n = 1 nothing can be suspicious: the margin is a whole trial
    assert gradient.gradient_state({DS_OLD: (1, 1), FABLE: (1, 0)})[0] == "not-monotone"
    # a tier C dip is not the A/B-over-D/E kind
    assert gradient.gradient_state({DS_OLD: (2, 1), HAIKU: (2, 0), FABLE: (2, 2)})[0] == "not-monotone"


def test_gradient_state_saturated_holds_and_single_tier():
    state, reason = gradient.gradient_state({DS_OLD: (2, 2), FABLE: (2, 2), HAIKU: (1, 0)})
    assert state == "saturated" and "n ≥ 2" in reason  # the n = 1 miss does not break saturation
    assert gradient.gradient_state({DS_OLD: (2, 0), DS_NEW: (2, 1), FABLE: (2, 2)})[0] == "holds"
    assert gradient.gradient_state({DS_OLD: (2, 1), FABLE: (2, 1)})[0] == "holds"  # equal is non-decreasing
    assert gradient.gradient_state({DS_OLD: (1, 1), GLM_OLD: (1, 0)}) == (None, "one tier with data: no gradient to read")
    assert gradient.gradient_state({MYSTERY: (2, 2), FABLE: (2, 1)})[0] is None  # a model outside the table has no tier to compare
    assert gradient.gradient_state({})[0] is None


def test_matrix_rows_carry_chips_sources_labels_and_only_cells_with_trials():
    trials = [trial(1, DS_OLD, "s1"), trial(2, DS_OLD, "s1"), trial(3, FABLE, "s1", success=False), trial(4, FABLE, "s1", success=False),
              trial(5, DS_OLD, "s2"), trial(6, DS_OLD, "s2"), trial(7, FABLE, "s2"), trial(8, FABLE, "s2"),
              trial(9, HAIKU, "s3", condition="interactive")]
    scenarios = {"s1": scenario_row("s1", "broken"), "s2": scenario_row("s2", None)}
    out = gradient.matrices(trials, scenarios)
    assert [(m["condition"], m["brief_profile"]) for m in out] == [("hidden", "withheld"), ("interactive", "withheld")]
    hidden = out[0]
    assert hidden["models"] == [DS_OLD, FABLE] and hidden["tiers"] == ["A", "E"]
    by_sid = {r["scenario_id"]: r for r in hidden["scenarios"]}
    assert by_sid["s1"]["gradient"]["state"] == "suspicious" and by_sid["s1"]["difficulty_tier"] == "broken" and by_sid["s1"]["source"] == "github-repo"
    assert by_sid["s2"]["gradient"]["state"] == "saturated" and by_sid["s2"]["difficulty_tier"] is None
    assert hidden["flags"] == {"holds": 0, "suspicious": 1, "saturated": 1, "not-monotone": 0}
    assert set(by_sid["s1"]["cells"]) == {DS_OLD, FABLE} and by_sid["s1"]["cells"][FABLE] == {"n": 2, "success_count": 0, "accuracy": 0.0, "scenarios": 1}
    interactive = out[1]
    assert interactive["models"] == [HAIKU] and interactive["scenarios"][0]["cells"][HAIKU]["n"] == 1 and interactive["scenarios"][0]["gradient"]["state"] is None
    assert "s3" not in by_sid  # nothing below n = 1: a scenario without hidden trials has no hidden row


def test_gradient_chip_markup_is_informational_and_in_the_live_vocabulary():
    assert "suspicious" in render_trends.gradient_chip("suspicious", "why") and "class='chip serious'" in render_trends.gradient_chip("suspicious", "why")
    assert "class='chip good'" in render_trends.gradient_chip("holds") and "class='chip neutral'" in render_trends.gradient_chip("saturated")
    assert "one tier" in render_trends.gradient_chip(None, "x")


# ---------------------------------------------------------------- the three deltas

def test_deltas_are_percentage_points_on_shared_scenarios_with_both_n():
    trials = [trial(1, HAIKU, "s1", "full"), trial(2, HAIKU, "s1", "full"), trial(3, HAIKU, "s2", "full", success=False),   # full 2/3 overall
              trial(4, HAIKU, "s1", "hidden", success=False), trial(5, HAIKU, "s1", "hidden"),                            # hidden 1/2 on s1 only
              trial(6, HAIKU, "s1", "interactive"), trial(7, HAIKU, "s3", "interactive"),                                  # interactive on s1 and s3
              trial(8, FABLE, "s1", "hidden")]                                                                               # a model with one arm only
    rows = gradient.deltas(trials, {"purpose": "comparison"})
    assert [(r["model"], r["brief_profile"]) for r in rows] == [(HAIKU, "withheld"), (FABLE, "withheld")]
    haiku = rows[0]
    assert haiku["arms"]["full"]["n"] == 3 and haiku["arms"]["full"]["accuracy"] == pytest.approx(2 / 3)
    fh = haiku["deltas"]["full_minus_hidden"]          # shared scenario s1: full 2/2 (1.0) − hidden 1/2 (0.5)
    assert fh["pp"] == 50.0 and fh["shared_scenarios"] == 1 and fh["a"] == {"n": 2, "success_count": 2, "accuracy": 1.0} and fh["b"]["n"] == 2
    ih = haiku["deltas"]["interactive_minus_hidden"]   # s1: interactive 1/1 − hidden 1/2
    assert ih["pp"] == 50.0 and ih["a"]["n"] == 1 and ih["b"]["n"] == 2
    fi = haiku["deltas"]["full_minus_interactive"]     # s1: full 2/2 − interactive 1/1 = 0
    assert fi["pp"] == 0.0 and fi["shared_scenarios"] == 1
    fable = rows[1]
    assert fable["arms"]["full"] is None and fable["arms"]["hidden"]["n"] == 1 and all(v is None for v in fable["deltas"].values())
    html = charts.delta_bars(rows, chart_id="d", title="deltas")
    assert "<svg" in html and "<table" in html and "+50" in html and "1 of 2 model rows have a computable delta" in html


# ---------------------------------------------------------------- cost, integrity, asking

def test_cost_integrity_and_asking_rows_keep_unknowns_unknown():
    trials = [trial(1, HAIKU, cost_usd=Decimal("0.10")), trial(2, HAIKU, cost_usd=Decimal("0.30"), success=False),
              trial(3, DS_OLD, cost_usd=Decimal("0.05")), trial(4, DS_OLD, cost_usd=None),
              trial(5, FABLE, condition="interactive", questions_asked=2, asked_before_implementing=True, integrity_signal_count=2),
              trial(6, FABLE, condition="interactive", questions_asked=0, asked_before_implementing=False, integrity_signal_count=None),
              trial(7, FABLE, condition="interactive", questions_asked=None)]
    cost = gradient.cost_rows(trials, {"purpose": "comparison"})
    by_model = {r["model"]: r for r in cost["by_model"]}
    assert [r["model"] for r in cost["by_model"]] == [DS_OLD, HAIKU, FABLE]  # tier order
    assert by_model[HAIKU]["cost_per_trial"] == pytest.approx(0.20) and by_model[HAIKU]["cost_per_success"] == pytest.approx(0.40)
    assert by_model[DS_OLD]["cost_per_trial"] == pytest.approx(0.05) and by_model[DS_OLD]["cost_per_success"] is None and "1/2" in by_model[DS_OLD]["cost_per_success_reason"]
    assert by_model[FABLE]["cost_per_trial"] is None and by_model[FABLE]["cost_per_success_reason"] == "no cost recorded"
    assert [(r["model"], r["condition"]) for r in cost["by_model_condition"]] == [(DS_OLD, "hidden"), (HAIKU, "hidden"), (FABLE, "interactive")]
    integrity = {r["model"]: r for r in gradient.integrity_rows(trials, {"purpose": "comparison"})}
    assert integrity[FABLE]["n"] == 3 and integrity[FABLE]["known_n"] == 2 and integrity[FABLE]["flagged_n"] == 1 and integrity[FABLE]["signals"] == 2
    assert integrity[FABLE]["rate"] == 0.5 and integrity[FABLE]["flagged_trial_ids"] == [f"{5:024d}"] and integrity[FABLE]["evidence_url"].startswith("/trends/integrity?")
    assert integrity[HAIKU]["flagged_n"] == 0 and integrity[HAIKU]["rate"] == 0.0
    asking = gradient.asking_rows(trials, {"purpose": "comparison"})
    assert len(asking) == 1 and asking[0]["model"] == FABLE and asking[0]["n"] == 3 and asking[0]["questions_known_n"] == 2
    assert asking[0]["questions_per_trial"] == 1.0 and asking[0]["asked_any_rate"] == 0.5 and asking[0]["asked_first_labelled_n"] == 2 and asking[0]["asked_first_rate"] == 0.5
    assert gradient.asking_rows([trial(8, HAIKU)], {})== []  # no interactive trials: no asking row


# ---------------------------------------------------------------- the Home tile

def test_frontier_gap_uses_shared_hidden_scenarios_within_one_cohort_and_profile():
    trials = [trial(1, DS_OLD, "s1"), trial(2, DS_OLD, "s1", success=False), trial(3, DS_OLD, "s9"),                  # A: 1/2 on s1, s9 not shared
              trial(4, FABLE, "s1"), trial(5, FABLE, "s1"), trial(6, SONNET, "s1", success=False),                     # E 2/2, D 0/1 on s1
              trial(7, GLM_OLD, "s2"), trial(8, FABLE, "s2", success=False),                                            # s2 shared too: A 1/1, E 0/1
              trial(9, DS_OLD, "s3", condition="full"), trial(10, FABLE, "s3", condition="full")]                       # full arm never counts
    gap = gradient.frontier_gap(trials)
    # shared hidden scenarios s1, s2: best D/E is Fable 2/3 (0.667); best A is GLM 1/1 (1.0) over DS 1/2 → gap −33.3 pp
    assert gap["scenarios"] == 2 and gap["best_high"]["model"] == FABLE and gap["best_high"]["n"] == 3
    assert gap["best_low"]["model"] == GLM_OLD and gap["gap_pp"] == pytest.approx(-33.3) and gap["n"] == 4 and gap["brief_profile"] == "withheld"
    assert gradient.frontier_gap([trial(1, DS_OLD, "s1"), trial(2, FABLE, "s2")]) is None  # no shared scenario
    assert gradient.frontier_gap([trial(1, HAIKU, "s1"), trial(2, FABLE, "s1")]) is None    # tier C is not tier A
    assert gradient.frontier_gap([trial(1, DS_OLD, "s1"), trial(2, FABLE, "s1", origin="laptop")]) is None  # another cohort: never pooled


# ---------------------------------------------------------------- charts: table twins, n everywhere, nothing below n = 1

def test_tier_bars_group_by_tier_label_every_n_and_fold_nothing():
    cells = gradient.accuracy_cells([trial(1, DS_OLD), trial(2, DS_OLD, success=False), trial(3, DS_NEW), trial(4, HAIKU), trial(5, SONNET), trial(6, FABLE), trial(7, MYSTERY)], {})
    html = charts.tier_bars(cells, chart_id="tb", title="hidden")
    assert "<svg" in html and "<table" in html and "<ul class='legend'>" in html
    for tier in ("A", "B", "C", "D", "E"):
        assert f"tier {tier} · " in html
    assert "outside the tier table" in html and "n=2" in html and "n=1" in html and html.count("<a class='bar fam-") == 6  # 7 trials, 6 model cells
    assert "0.50" in html  # tier A's maximum is direct-labelled
    order = [html.index(m.split("/")[-1][:18]) for m in (DS_OLD, DS_NEW, HAIKU, SONNET, FABLE)]
    assert order == sorted(order)  # A→E left to right
    assert charts.tier_bars([], chart_id="x", title="t").startswith("<p class='empty'>")
    assert charts.tier_bars([{"model": HAIKU, "tier": "C", "n": 0, "accuracy": None}], chart_id="x", title="t").startswith("<p class='empty'>")  # n = 0 is never drawn


# ---------------------------------------------------------------- the routes over the file reader

@pytest.fixture
def gradient_site(site):
    """The fixture site plus two audited scenarios with hidden trials from a tier A model and a tier E model (one scenario suspicious,
    one saturated), an interactive Haiku trial, and one trial whose grader report carries an integrity signal."""
    ws = site["ws"]
    s1 = _spec("gradient-one", "github-repo", "https://github.com/acme/one/pull/1")
    s2 = _spec("gradient-two", "github-repo", "https://github.com/acme/two/pull/2")
    _add_scenario(ws, s1, eligible=True)
    _add_scenario(ws, s2, eligible=True)
    ids = {"a1": write_trial(ws, s1, condition="hidden", model=DS_OLD, purpose="comparison", attempt=0),
           "a2": write_trial(ws, s1, condition="hidden", model=DS_OLD, purpose="comparison", attempt=1),
           "e1": write_trial(ws, s1, condition="hidden", model=FABLE, purpose="comparison", attempt=0, outcome_code=2, success=0, reward=0.0),
           "e2": write_trial(ws, s1, condition="hidden", model=FABLE, purpose="comparison", attempt=1, outcome_code=2, success=0, reward=0.0),
           "a3": write_trial(ws, s2, condition="hidden", model=DS_OLD, purpose="comparison", attempt=0),
           "a4": write_trial(ws, s2, condition="hidden", model=DS_OLD, purpose="comparison", attempt=1),
           "e3": write_trial(ws, s2, condition="hidden", model=FABLE, purpose="comparison", attempt=0),
           "e4": write_trial(ws, s2, condition="hidden", model=FABLE, purpose="comparison", attempt=1),
           "f1": write_trial(ws, s1, condition="full", model=FABLE, purpose="comparison", attempt=0),
           "h1": write_trial(ws, s1, condition="interactive", model=HAIKU, purpose="comparison", attempt=0)}
    # one trial's grader report carries an integrity signal (record.json and report.json both, as the runner writes them)
    tdir = ws.root / "trials" / ids["a1"]
    for name in ("record.json", "report.json"):
        doc = json.loads((tdir / name).read_text())
        target = doc["report"] if name == "record.json" else doc
        target["integrity_signals"] = ["forged-junit"]
        (tdir / name).write_text(json.dumps(doc))
    access.reset()
    return {**site, "ids": ids, "s1": s1.id, "s2": s2.id}


def test_trends_page_and_json_carry_the_gradient_with_tier_order_chips_and_evidence(gradient_site):
    c = gradient_site["client"]
    api = c.get("/api/trends").json()
    g = api["gradient"]
    assert g["models"] == [DS_OLD, HAIKU, FABLE] and g["scenario_count"] == 2 and len(g["cohorts"]) == 1  # tier A, C, E
    cohort = g["cohorts"][0]
    hidden = next(m for m in cohort["matrices"] if m["condition"] == "hidden")
    assert hidden["models"] == [DS_OLD, FABLE] and hidden["tiers"] == ["A", "E"]
    states = {r["scenario_id"]: r["gradient"]["state"] for r in hidden["scenarios"]}
    assert states == {gradient_site["s1"]: "suspicious", gradient_site["s2"]: "saturated"} and hidden["flags"]["suspicious"] == 1 and hidden["flags"]["saturated"] == 1
    cells = {(x["condition"], x["model"]): x for x in cohort["accuracy"]}
    assert cells[("hidden", DS_OLD)]["n"] == 4 and cells[("hidden", DS_OLD)]["accuracy"] == 1.0 and cells[("hidden", FABLE)]["accuracy"] == 0.5
    assert all(x["n"] >= 1 for x in cohort["accuracy"])
    fable = next(r for r in cohort["deltas"] if r["model"] == FABLE)
    assert fable["deltas"]["full_minus_hidden"]["pp"] == 100.0 and fable["deltas"]["full_minus_hidden"]["shared_scenarios"] == 1   # s1: full 1/1 − hidden 0/2
    integrity = {r["model"]: r for r in cohort["integrity"]}
    assert integrity[DS_OLD]["flagged_n"] == 1 and integrity[DS_OLD]["signals"] == 1 and integrity[FABLE]["flagged_n"] == 0
    asking = cohort["asking"]
    assert len(asking) == 1 and asking[0]["model"] == HAIKU and asking[0]["n"] == 1
    # every chart fragment has a table twin; the gradient fragments come first and stay in the live vocabulary
    figures = {cid: html for cid, html in api["charts"].items() if "<svg" in html}
    assert figures and all("<table" in html for html in figures.values())
    assert any(cid.endswith("-hidden-withheld-accuracy") for cid in api["charts"]) and any(cid.endswith("-hidden-withheld-matrix") for cid in api["charts"])
    assert any(cid.endswith("-deltas") for cid in api["charts"]) and any(cid.endswith("-cost-solved") for cid in api["charts"]) and any(cid.endswith("-integrity") for cid in api["charts"])
    joined = "".join(api["charts"].values())
    assert "<input" not in joined and "<form" not in joined and "<code" not in joined
    html = c.get("/trends").text
    assert "Capability gradient" in html and "tier A · older open" in html and ">suspicious</span>" in html and ">saturated</span>" in html
    assert "1/2" in html or "2/2" in html  # solved/n printed in the matrix cells
    assert "80–90%" in html and "include saturated" not in html
    # evidence links resolve: a cell's n, the integrity list, the leaderboard evidence
    evidence = c.get(cells[("hidden", DS_OLD)]["evidence_url"])
    assert evidence.status_code == 200 and "4 admitted attempts" in evidence.text
    flagged = c.get(integrity[DS_OLD]["evidence_url"])
    assert flagged.status_code == 200 and "1 admitted comparison attempts" in flagged.text and gradient_site["ids"]["a1"][:12] in flagged.text
    assert c.get("/trends/integrity").status_code == 200
    # the tier filter narrows the axis; the difficulty filter narrows scenarios; nothing pooled across purposes
    only_a = c.get("/api/trends?tier=A").json()["gradient"]
    assert only_a["models"] == [DS_OLD]
    assert c.get("/api/trends?difficulty_tier=broken").json()["gradient"]["cohorts"] == []
    assert c.get("/api/trends?purpose=calibration").json()["gradient"]["models"] == []  # never pooled: the calibration scenario is unaudited, so nothing is admitted there
