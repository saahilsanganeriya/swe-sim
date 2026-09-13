"""The stakeholder-visible view of a ScenarioSpec, built in the worker before ``POST /sessions``.

Structural exclusion: the view carries only brief, non-author-only facts (id, text, kind, discovery), requirement
texts, persona, checks and the repo dir. It never references ``spec.assets``, ``spec.controls`` (oracle) or
``spec.contract`` (hidden tests), so nothing downstream can leak them by accident.

``forbidden_terms`` is the code-owned deny-list the run stage adds to the session body as
``visible_view["forbidden_terms"]``: identifiers that occur in the hidden test asset(s) but not in the natural brief,
the visible facts or the requirements, plus fixed test vocabulary. It is a list of tokens, never content; the
service's reply filter redacts sentences that use them and the prompt never renders them.
"""
from __future__ import annotations

import keyword, re
from pathlib import Path

from swesim.models import Discovery, ScenarioSpec

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
FIXED_FORBIDDEN = ("assert", "pytest", "fixture", "parametrize", "test_", "_setup(", "tests/",
                   # test vocabulary a reporter without the suite would not use (multi-word phrases match case-insensitively)
                   "regression test", "test case", "unit test", "test coverage", "add a test", "a test that", "tests for")
_RESERVED = set(keyword.kwlist) | set(keyword.softkwlist)          # reserved words are not identifiers a test author chose
_PATCH_FRAMING = ("diff ", "index ", "--- ", "+++ ", "@@", "new file mode", "deleted file mode", "old mode", "new mode",
                  "similarity index", "rename from", "rename to", "Binary files")


def visible_view(spec: ScenarioSpec) -> dict:
    return {
        "brief": next(b.text for b in spec.briefs if b.profile == "natural"),
        "facts": [{"id": f.id, "text": f.text, "kind": f.kind.value, "discovery": f.discovery.value}
                  for f in spec.ledger if f.discovery != Discovery.AUTHOR_ONLY],
        "requirements": [{"id": r.id, "text": r.text} for r in spec.requirements],
        "persona": spec.persona.model_dump(),
        "checks": [c.model_dump() for c in spec.checks],
        "repo_dir": spec.environment.repo_dir,
    }


def allowed_identifiers(spec: ScenarioSpec) -> set[str]:
    """Casefolded identifiers the stakeholder legitimately knows: natural brief, visible facts, requirements."""
    texts = [b.text for b in spec.briefs if b.profile == "natural"]
    texts += [f.text for f in spec.ledger if f.discovery != Discovery.AUTHOR_ONLY]
    texts += [r.text for r in spec.requirements]
    return {i.casefold() for t in texts for i in IDENT.findall(t)}


def _asset_identifiers(text: str) -> set[str]:
    """Identifiers in a test asset's content; patch framing lines are skipped and hunk markers stripped."""
    found: set[str] = set()
    for line in text.splitlines():
        if line.startswith(_PATCH_FRAMING): continue
        if line[:1] in "+- ": line = line[1:]
        found.update(IDENT.findall(line))
    return found


def forbidden_terms(spec: ScenarioSpec, scenario_dir: Path | str) -> list[str]:
    allowed = allowed_identifiers(spec); found: set[str] = set()
    for name in spec.contract.test_assets:
        found |= _asset_identifiers((Path(scenario_dir) / spec.assets[name].path).read_text(errors="replace"))
    terms = {t for t in found if t.casefold() not in allowed and t not in _RESERVED}
    return sorted(terms | set(FIXED_FORBIDDEN))
