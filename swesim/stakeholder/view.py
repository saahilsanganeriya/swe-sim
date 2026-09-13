"""The stakeholder-visible view of a ScenarioSpec, built in the worker before ``POST /sessions``.

Structural exclusion: the view carries only brief, non-author-only facts (id, text, kind, discovery), requirement
texts, persona, checks and the repo dir. It never references ``spec.assets``, ``spec.controls`` (oracle) or
``spec.contract`` (hidden tests), so nothing downstream can leak them by accident.

``forbidden_terms`` is the code-owned deny-list the run stage adds to the session body as
``visible_view["forbidden_terms"]``: code identifiers that occur in the hidden test asset(s) but not in the natural
issue text, plus fixed test vocabulary. The natural issue is the only permitted vocabulary (A-P1-2 ruling): a
requirement or visible fact that repeats a hidden-test identifier puts it in the stakeholder's context but never
exempts it from redaction, because the reporter did not write it. "Code identifiers" means names at code positions of
the test (defined, imported, called, attribute names); words inside comments and string literals are natural language
the reporter may use, and Python keywords and builtins (``ValueError``, ``None``, ``len`` ...) are language, not a test
author's choice, so none of those become terms. It is a list of tokens, never content; the service's reply filter
redacts sentences that use them and the prompt never renders them.
"""
from __future__ import annotations

import builtins, keyword, re
from pathlib import Path

from swesim.models import Discovery, ScenarioSpec

IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
FIXED_FORBIDDEN = ("assert", "pytest", "fixture", "parametrize", "test_", "_setup(", "tests/",
                   # test vocabulary a reporter without the suite would not use (multi-word phrases match case-insensitively)
                   "regression test", "test case", "unit test", "test coverage", "add a test", "a test that", "tests for")
# Language, not identifiers a test author chose: keywords, soft keywords and every builtin name (exception classes included).
_RESERVED = set(keyword.kwlist) | set(keyword.softkwlist) | set(dir(builtins))
_PATCH_FRAMING = ("diff ", "index ", "--- ", "+++ ", "@@", "new file mode", "deleted file mode", "old mode", "new mode",
                  "similarity index", "rename from", "rename to", "Binary files")
_QUOTES = ('"""', "'''", '"', "'")


def _code_only(line: str, state: dict) -> str:
    """`line` without its string literals and trailing comment. `state["quote"]` carries an open triple-quoted string across
    lines (a single-quoted string never does). A hunk that starts inside a docstring is read as code, the conservative
    direction for a deny-list (extra terms, never missing ones)."""
    out: list[str] = []
    quote = state.get("quote")
    i, n = 0, len(line)
    while i < n:
        if quote:
            if line[i] == "\\":
                i += 2
                continue
            if line.startswith(quote, i):
                i += len(quote)
                quote = None
                continue
            i += 1
            continue
        if line[i] == "#":
            break
        for q in _QUOTES:
            if line.startswith(q, i):
                quote = q
                i += len(q)
                break
        else:
            out.append(line[i])
            i += 1
    state["quote"] = quote if quote and len(quote) == 3 else None
    return "".join(out)


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
    """Casefolded identifiers the reporter wrote: the natural issue text only. Visible facts and requirements are
    stakeholder context, not reporter vocabulary, so they never widen the allow-list."""
    texts = [b.text for b in spec.briefs if b.profile == "natural"]
    return {i.casefold() for t in texts for i in IDENT.findall(t)}


def _asset_identifiers(text: str) -> set[str]:
    """Identifiers at code positions of a test asset's content (comments and string literals blanked); patch framing lines
    are skipped and hunk markers stripped; the open-string state resets at every file or hunk header."""
    found: set[str] = set()
    state: dict = {"quote": None}
    for line in text.splitlines():
        if line.startswith(_PATCH_FRAMING):
            state["quote"] = None
            continue
        if line[:1] in "+- ": line = line[1:]
        found.update(IDENT.findall(_code_only(line, state)))
    return found


def forbidden_terms(spec: ScenarioSpec, scenario_dir: Path | str) -> list[str]:
    allowed = allowed_identifiers(spec); found: set[str] = set()
    for name in spec.contract.test_assets:
        found |= _asset_identifiers((Path(scenario_dir) / spec.assets[name].path).read_text(errors="replace"))
    terms = {t for t in found if t.casefold() not in allowed and t not in _RESERVED}
    return sorted(terms | set(FIXED_FORBIDDEN))
