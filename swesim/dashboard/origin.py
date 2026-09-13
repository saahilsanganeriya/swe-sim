"""Task origin: where a scenario came from, as the operator reads it.

Two origins matter on the console: **novel** tasks created from a repository URL or PR (``ScenarioRow.source ==
"github-repo"``) and tasks derived from **SWE-bench pool A**, the underspecified-but-valid SWE-bench instances outside
SWE-bench Verified (``source == "swebench-annotated"``). Hand-authored fixtures (``source == "authored"``) belong to
neither; they show under "All" with their own neutral badge.

Contract
- One query parameter, ``?pool=novel|swebench-a``, is the facet on the Scenarios, Trials and Leaderboard pages. It maps
  onto the readers' ``source`` filter (``ScenarioFilter`` / ``TrialFilter`` / ``LeaderboardFilter``); an unknown value
  is ignored (``All``). The facet is carried by the paging links, the filter forms and the JSON twins.
- ``badge(source)`` is the small chip shown next to every scenario id; it uses the existing chip tokens (``info`` for
  novel, ``neutral`` for pool A and authored) and never colour alone.
- ``of_source_key(key)`` classifies a candidates source key (``github-repo--<owner>__<repo>--<band>`` vs
  ``swebench-annotated:a`` / ``swebench-annotated--a``) for the Candidates page's two headings.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Iterable
from urllib.parse import urlencode

PARAM = "pool"


@dataclass(frozen=True)
class Facet:
    key: str  # query value
    label: str  # facet control / headings
    badge: str  # short chip text next to a scenario id
    sources: tuple[str, ...]  # ScenarioRow.source values
    kind: str  # chip token (existing status-chip kinds only)
    title: str  # hover explanation


NOVEL = Facet("novel", "Novel (repository)", "novel", ("github-repo",), "info",
              "novel task: created from a repository URL / pull request")
SWEBENCH_A = Facet("swebench-a", "SWE-bench pool A", "SWE-bench A", ("swebench-annotated",), "neutral",
                   "derived from SWE-bench pool A (underspecified-but-valid instances outside SWE-bench Verified)")
FACETS: tuple[Facet, ...] = (NOVEL, SWEBENCH_A)
BY_KEY: dict[str, Facet] = {f.key: f for f in FACETS}
BY_SOURCE: dict[str, Facet] = {s: f for f in FACETS for s in f.sources}
ALL_LABEL = "All"
CANDIDATE_HEADINGS: tuple[tuple[str, str], ...] = (("novel", "Repositories"), ("swebench-a", SWEBENCH_A.label))


def _h(x) -> str:
    return escape("" if x is None else str(x), quote=True)


def facet_key(value: str | None) -> str | None:
    """The validated facet key from a query value; anything unknown means All."""
    return value if value in BY_KEY else None


def sources_for(value: str | None) -> list[str] | None:
    f = BY_KEY.get(value or "")
    return list(f.sources) if f else None


def of_source(source: str | None) -> Facet | None:
    return BY_SOURCE.get(source or "")


def of_source_key(source_key: str | None) -> Facet:
    """Candidates source keys: anything scanned from SWE-bench is pool A; every other key is a repository scan."""
    return SWEBENCH_A if str(source_key or "").startswith(SWEBENCH_A.sources) else NOVEL


def badge_label(source: str | None) -> str | None:
    """The chip text for a scenario source: the facet badge, or the raw source (``authored``) when it is neither."""
    if not source:
        return None
    f = of_source(source)
    return f.badge if f else str(source)


def badge(source: str | None) -> str:
    """Small chip next to a scenario id: ``novel`` / ``SWE-bench A`` / the raw source; a dash when unknown."""
    if not source:
        return "<span class='muted' title='scenario source unknown'>—</span>"
    f = of_source(source)
    label, kind, title = (f.badge, f.kind, f.title) if f else (str(source), "neutral", f"scenario source {source}")
    return f"<span class='chip {kind} origin' title='{_h(title)}'>{_h(label)}</span>"


def subset_text(value: str | None) -> str:
    """How the page's count line and the leaderboard header say which subset is shown."""
    f = BY_KEY.get(value or "")
    return f"{f.label} only" if f else "all task origins"


def hidden_input(value: str | None) -> str:
    """Keeps the facet through a GET filter form."""
    key = facet_key(value)
    return f"<input type='hidden' name='{PARAM}' value='{_h(key)}'>" if key else ""


def keep_query(items: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """The query pairs paging links must carry: everything but the paging itself."""
    return [(k, v) for k, v in items if k not in ("offset", "limit") and v not in (None, "")]


def facet_bar(current: str | None, items: Iterable[tuple[str, str]], path: str) -> str:
    """Segmented control All | Novel (repository) | SWE-bench pool A; every other query parameter is preserved and
    paging resets. The active option carries ``aria-current``."""
    current = facet_key(current)
    base = [(k, v) for k, v in keep_query(items) if k != PARAM]
    links = []
    for key, label in ((None, ALL_LABEL), *((f.key, f.label) for f in FACETS)):
        q = urlencode(base + ([(PARAM, key)] if key else []))
        active = key == current
        links.append(f"<a class='button small{' primary' if active else ''}' href='{_h(path)}{'?' + _h(q) if q else ''}'"
                     f"{' aria-current=true' if active else ''} data-facet='{_h(key or 'all')}'>{_h(label)}</a>")
    return f"<nav class='facet-bar' aria-label='task origin'><span class='muted small'>task origin</span>{''.join(links)}</nav>"
