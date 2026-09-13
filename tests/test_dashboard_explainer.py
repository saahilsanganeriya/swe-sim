"""Explainer page (/explainer): the whole system explained from structured data. Static, identical for every identity,
no evaluator material, every cited path exists, every card says where to see it in the console, diagrams stay aligned."""
from __future__ import annotations

import re
from pathlib import Path

from swesim.dashboard import render, render_explainer as X
from test_dashboard import EVALUATOR_JSON_KEYS, SENTINELS, site as site

REPO = Path(__file__).resolve().parents[1]
TITLES = ("What swesim measures", "Hosted architecture", "Creating a task from a repository URL", "The lossy spec", "The stakeholder",
          "The verifier and reward", "The judge", "Difficulty screen and task-sense audit", "Frontier admission gate", "Decisions log")
KNOWN_PAGES = {"/", "/scenarios", "/scenario/<id>", "/trials", "/trial/<id>", "/candidates", "/workflows", "/workflows/<id>", "/review", "/review/<id>",
               "/leaderboard", "/trends", "/create", "/run", "/spend", "/health", "/db", "/db/<table>", "/artifacts", "/search", "/explainer"}


def _main(html: str) -> str:
    return html.split("<main", 1)[1].split("</main>", 1)[0]


def test_explainer_renders_every_section_in_order_with_nav_and_toc(site):
    r = site["client"].get("/explainer")
    assert r.status_code == 200
    html = r.text
    body = _main(html)
    positions = [body.index(f"{i}. {render.h(t)}</h2>") for i, t in enumerate(TITLES, 1)]
    assert positions == sorted(positions)
    assert [s.title for s in X.SECTIONS] == list(TITLES) and len(X.SECTIONS) == 10
    for s in X.SECTIONS:
        assert f"id='{s.key}'" in body and f"href='#{s.key}'" in body  # anchor + table of contents entry
    assert "href='/explainer'" in html and "aria-current='page'" in html.split("<aside", 1)[1].split("</aside>", 1)[0]
    assert any(it.href == "/explainer" for _g, items in render.NAV for it in items)


def test_explainer_is_static_and_identical_for_every_identity(site):
    c = site["client"]
    anon, ev = c.get("/explainer").text, c.get("/explainer", headers=site["eval"]).text
    assert _main(anon) == _main(ev)
    for name, sentinel in SENTINELS.items():
        assert sentinel not in anon, name
    for key in EVALUATOR_JSON_KEYS:
        assert key not in anon, key
    assert "evaluator=1" not in anon and site["spec"].spec_hash not in anon and site["done"] not in anon


def test_every_card_cites_existing_paths_and_a_console_page():
    seen_titles = set()
    for section in X.SECTIONS:
        assert section.cards and section.lead
        for card in section.cards:
            assert card.title not in seen_titles, card.title
            seen_titles.add(card.title)
            assert card.text and card.sources and card.console, card.title
            assert card.console.page in KNOWN_PAGES, (card.title, card.console.page)
            assert card.console.look_for, card.title
            for src in card.sources:
                assert src.where, (card.title, src.path)
                if src.tracked:
                    assert (REPO / src.path).exists(), (card.title, src.path)
                else:
                    assert src.path.startswith(".superpowers/"), src.path  # the only untracked source is the operator ledger
            for d in card.decisions:
                assert d.what and d.why and d.cost, (card.title, d.what)
    for path in X.cited_paths():
        assert (REPO / path).exists(), path


def test_decisions_log_rows_are_complete_and_dated():
    assert len(X.DECISIONS) >= 20
    for d in X.DECISIONS:
        assert d.what and d.why and d.cost and d.date and d.source, d.what
        assert re.fullmatch(r"2026-09-1[23]( \d\d:\d\d (PDT|UTC))?", d.date), d.date
        if d.source.tracked:
            assert (REPO / d.source.path).exists(), d.source.path


def test_diagrams_are_aligned_and_escaped(site):
    for section in X.SECTIONS:
        for card in section.cards:
            if not card.diagram:
                continue
            lines = card.diagram.strip("\n").splitlines()
            assert lines and all(len(ln) <= X.DIAGRAM_MAX_COLUMNS for ln in lines), (card.title, max(map(len, lines)))
            assert len(lines) <= X.DIAGRAM_MAX_LINES, (card.title, len(lines))  # app.css caps pre at 28rem; no inner scrolling
            assert all(ln == ln.rstrip() for ln in lines), (card.title, "trailing whitespace")
            assert "\t" not in card.diagram, card.title
    body = _main(site["client"].get("/explainer").text)
    assert body.count("<pre class='code'") >= 12
    assert "style=" not in body and re.search(r"#[0-9a-fA-F]{6}\b", body) is None  # no inline styles, no colour literals


def test_text_numbers_carry_a_source_note():
    # Every card that states a number cites at least one document (numbers come only from cited docs, brief rule).
    for section in X.SECTIONS:
        for card in section.cards:
            if re.search(r"\d", card.text):
                assert card.sources, card.title
