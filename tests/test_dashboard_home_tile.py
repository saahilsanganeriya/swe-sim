"""Home: the "frontier vs last-gen gap" stat tile (best tier D/E accuracy minus best tier A accuracy on the same hidden
scenarios, with n) links to the capability gradient on Trends and is a dash until both tiers share a hidden scenario."""
from __future__ import annotations

from test_dashboard import site as site  # noqa: F401 - fixture
from test_dashboard_trends import DS_OLD, FABLE, gradient_site as gradient_site  # noqa: F401 - fixture


def test_home_tile_shows_the_frontier_gap_with_n_and_links_to_trends(gradient_site):
    c = gradient_site["client"]
    home = c.get("/api/home").json()
    tile = home["tiles"]
    # hidden, shared scenarios s1 + s2: best D/E is Fable 2/4 (0.50), best A is DeepSeek v3-0324 4/4 (1.00) → −50 pp on n = 8
    assert tile["frontier_gap_text"] == "−50 pp" and tile["frontier_gap"]["n"] == 8 and tile["frontier_gap"]["scenarios"] == 2
    assert tile["frontier_gap"]["best_high"]["model"] == FABLE and tile["frontier_gap"]["best_low"]["model"] == DS_OLD
    html = c.get("/").text
    assert "frontier vs last-gen gap" in html and "−50 pp" in html and "n=8" in html and "href='/trends" in html


def test_home_tile_is_a_dash_until_both_tiers_share_a_hidden_scenario(site):
    c = site["client"]
    home = c.get("/api/home").json()
    assert home["tiles"]["frontier_gap"] is None and home["tiles"]["frontier_gap_text"] == "—"
    assert "frontier vs last-gen gap" in c.get("/").text
