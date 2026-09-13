"""Draft stage: turn a judged candidate row into `scenario.draft.json` plus `assets/`.

The LLM authors the fact ledger, persona, requirements, checks, requirement groups and a plausible-wrong patch (and an
alternative-correct patch when the judge flagged structural tests). Code owns everything else: the natural brief is the
original issue verbatim, the withheld brief records every removal as an original-issue fact with a withholding operation,
tamper controls are generated from the base tree, assets carry a sha256 manifest, and `judge.json` is written next to the
draft for the approval gates. The result must validate against `ScenarioSpec` or the draft fails.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path

from swesim import tamper
from swesim.llm import chat_json
from swesim.paths import atomic_json
from swesim.sources import threads
from swesim.sources.github_repo import probe_binding
from swesim.sources.swebench_annotated import TEST_PYTHON, test_files_from_patch
import keyword

from swesim.stages.judge import DIFF_HEADER_PREFIXES, _BLOCKQUOTE, diff_body_text, evidence_ok, evidence_text, llm_view, norm_text

DRAFT_PROMPT_VERSION = "draft-v1"
CONTEXT_BUDGET_CHARS = 120000
# Linux MAX_ARG_STRLEN is 131072 bytes per argv element; a `sh -c "<command>"` launch would fail (E2BIG) above it.
RUNNER_COMMAND_MAX_CHARS = 120000
SYSTEM = """You turn a GitHub issue, its thread, and the merged fix into a scenario record for an interactive coding task.
Rules: every fact must quote a short excerpt from the thread with its URL, or be marked authored with a rationale.
Facts the agent can learn by reading the repository are discovery=repo-derivable. Facts absent from the repository that the
maintainer decided are discovery=on-request. Private helper names chosen by the fix author are discovery=author-only and must
not link tests. Requirements come from review comments. Checks are user-level reproducers (not the hidden tests).
Groups partition FAIL_TO_PASS test ids into requirement groups with weights summing to 1. Also write one plausible-wrong patch
as a unified diff against the repository at the base commit: a tempting incomplete or wrong fix (e.g. wrong exception type).
Constraints the record must satisfy: every on-request fact carries `tests` drawn from the FAIL_TO_PASS ids listed at the top
of the user message (the decision the agent has to ask about is what those tests pin); fact, requirement, check and group ids match ^[a-z0-9][a-z0-9._-]{2,63}$ and never start with "withheld-" or
"tests-" (code-generated facts use those); test links use only the FAIL_TO_PASS and PASS_TO_PASS ids given; check ids differ
from test ids. Every fact's source_excerpt is copied verbatim (no paraphrase) from the text its provenance names: issue-thread
= linked issue title/body/comments or hints_text, pr-thread = PR title/body/comments, pr-review = review bodies and review
comments, annotator-note = the annotation notes, original-issue = the problem statement; its source_url is a URL under the
repository. provenance=authored is only for decisions that nothing in the discussion states and only the merged tests or
patch pin: source_excerpt verbatim from test_patch or patch, source_url the PR URL, authored_rationale saying why no thread
statement exists, discovery on-request, tests = the FAIL_TO_PASS ids whose code the excerpt comes from. Code also adds such
facts from the judge's tests-resolved gaps. protected_paths_extra may name only test-tree fixture or data files the hidden tests
read; the hidden test files are protected by code and source files the fix modifies are never protected. If the judge marked
structural_tests, also write one alternative-correct patch (a different implementation the tests should still accept);
otherwise set alternative_correct to null."""
SCHEMA = """{"briefs": [{"profile": "natural", "text": "...", "provenance": "original-issue"}],
"ledger": [{"id": "f01", "text": "...", "kind": "behavior|interface|constraint|reproduction|context",
  "provenance": "original-issue|issue-thread|pr-thread|pr-review|annotator-note|authored", "source_excerpt": "...", "source_url": "...",
  "authored_rationale": null, "discovery": "brief|on-request|volunteered-at-review|repo-derivable|author-only", "tests": ["nodeid"]}],
"operations": [{"type": "clarification|evaluator-repair|synthetic-extension", "target": "...", "rationale": "..."}],
"persona": {"summary": "...", "tone": "...", "disclosure_style": "...", "derived_from_urls": ["..."], "role": "maintainer|reporter"},
"requirements": [{"id": "r01", "text": "...", "fact_id": "f01", "tests": ["nodeid"], "source_url": "..."}],
"checks": [{"id": "c01", "description": "...", "command": "python -c '...'", "expected_observation": "..."}],
"groups": [{"id": "g01", "weight": 0.5, "tests": ["nodeid"]}],
"plausible_wrong": {"name": "...", "rationale": "...", "diff": "unified diff text"},
"alternative_correct": {"name": "...", "rationale": "...", "diff": "unified diff text"} | null,
"protected_paths_extra": ["test-tree fixture or data files the hidden tests read (never source files; code drops anything else)"]}"""

WITHHOLD_CATEGORIES = {"reproduction", "error_message", "expected_output", "location", "version", "proposed_fix"}
WITHHOLD_DISCOVERY = {"on-request", "repo-derivable"}  # a removed reporter statement is never author-only
REPO_DERIVABLE_CATEGORIES = {"location", "proposed_fix"}
MIN_EXCERPT_CHARS = 12   # a one-word "excerpt" grounds nothing; both bounds apply after _norm
MIN_EXCERPT_TOKENS = 3
RESERVED_FACT_PREFIXES = ("withheld-", "tests-")  # code-generated fact ids the drafter may not use
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
WITHHOLD_PROMPT_VERSION = "withhold-v2"  # v2: the model selects spans, code deletes them; the brief is computed, never trusted
WITHHOLD_KINDS = {"reproduction", "context", "behavior", "interface", "constraint"}
MAX_REMOVED_FRACTION = 0.6  # of sentence/line segments
WITHHOLD_SCHEMA = ('{"removed":[{"category":"reproduction|error_message|expected_output|location|version|proposed_fix",'
                   '"excerpt":"verbatim contiguous span copied from the issue","fact_text":"one sentence stating the withheld fact",'
                   '"kind":"reproduction|context|behavior|interface|constraint","discovery":"on-request|repo-derivable"}]}')
WITHHOLD_PROMPT = ("Select spans of this issue to withhold from the agent, by category. Do not rewrite the issue and do not return a brief: "
                   "code deletes the sentence or line containing each selected span and keeps everything else byte for byte. Each excerpt must be a "
                   "verbatim contiguous span copied from the issue, at least three words long. fact_text restates the removed span for the "
                   "maintainer's ledger: every word of five or more letters in it must occur in the span or in the issue, or simply copy the span. "
                   "Never select the first line (the title); keep a recognizable symptom and a desired outcome; select at most about half of the "
                   "sentences. File locations and explicit fix proposals are repo-derivable; expected behaviour, error text, versions and "
                   "reproductions the maintainer can supply are on-request.")
MAX_FACT_TEXT_CHARS = 400
_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*(?=\s|$)")
_ABBREV = re.compile(r"(?i)(?:^|[\s(\[])(?:e\.g|i\.e|etc|vs|cf|viz)$")  # a terminator ending one of these is not a sentence end


def _title_range(natural_issue: str) -> tuple[int, int]:
    """Offsets of the first non-empty line, stripped: the title, which withholding never touches."""
    pos = 0
    for line in natural_issue.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if content.strip():
            lead = len(content) - len(content.lstrip())
            return pos + lead, pos + len(content.rstrip())
        pos += len(line)
    return 0, 0


def _title(natural_issue: str) -> str:
    a, b = _title_range(natural_issue)
    return natural_issue[a:b]


def _segments(text: str) -> list[tuple[int, int]]:
    """Sentence-or-line segments of the natural issue as [start, end) offsets trimmed of surrounding whitespace. A line
    without sentence terminators (code, tracebacks, headings) is one segment."""
    segs, pos = [], 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        starts = [0] + [m.end() for m in _SENTENCE_END.finditer(content) if not _ABBREV.search(content[:m.start()])]
        for a, b in zip(starts, starts[1:] + [len(content)]):
            piece = content[a:b]
            if piece.strip():
                lead = len(piece) - len(piece.lstrip())
                segs.append((pos + a + lead, pos + a + len(piece.rstrip())))
        pos += len(line)
    return segs


def _norm_map(text: str) -> tuple[str, list[int]]:
    """`_norm(text)` together with, for every normalized character, the offset of the original character it came from."""
    skip: set[int] = set()
    for m in _BLOCKQUOTE.finditer(text):
        skip.update(range(m.start(), m.end()))
    chars, idx, ws_start, started = [], [], None, False
    for i, ch in enumerate(text):
        if i in skip:
            continue
        if ch.isspace():
            if started and ws_start is None:
                ws_start = i
            continue
        if ws_start is not None:
            chars.append(" ")
            idx.append(ws_start)
            ws_start = None
        for c in ch.casefold():
            chars.append(c)
            idx.append(i)
        started = True
    return "".join(chars), idx


def _find_spans(natural: str, excerpt: str) -> list[tuple[int, int]]:
    """Every non-overlapping occurrence of `excerpt` in `natural`, matched after `_norm` on both sides, as original offsets."""
    norm_text, idx = _norm_map(natural)
    needle = _norm(excerpt)
    spans, pos = [], 0
    while needle:
        hit = norm_text.find(needle, pos)
        if hit < 0:
            break
        spans.append((idx[hit], idx[hit + len(needle) - 1] + 1))
        pos = hit + len(needle)
    return spans


def _extend(span: tuple[int, int], segs: list[tuple[int, int]]) -> tuple[int, int]:
    """Grow a matched span to the whole sentence/line segments it touches so the remaining text stays grammatical."""
    a, b = span
    hits = [s for s in segs if s[0] < b and s[1] > a]
    return (min(s[0] for s in hits), max(s[1] for s in hits)) if hits else span


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _deletion_range(text: str, a: int, b: int) -> tuple[int, int]:
    """The range actually deleted for a stripped span [a, b): whole lines with their newline when the span covers all the
    text of the lines it touches (plus one adjoining blank line when the deleted line sat between two blank lines, so
    paragraphs keep a single separator); otherwise a mid-line splice that leaves exactly one separating space. Everything
    outside the returned range is preserved byte for byte."""
    ls = text.rfind("\n", 0, a) + 1
    le = text.find("\n", b)
    if le < 0:
        le = len(text)
    if not text[ls:a].strip() and not text[b:le].strip():
        start, end = ls, (le + 1 if le < len(text) else le)
        if le >= len(text) and ls > 0:  # last line without a trailing newline: take the newline before it instead
            start = ls - 1
        if text[max(0, ls - 2):ls] == "\n\n" and text[end:end + 1] == "\n":
            end += 1
        return start, end
    e = b
    while e < le and text[e] in " \t":
        e += 1
    if e < le:  # text follows on the same line: drop the trailing spaces, keep the one before the span
        return a, e
    s = a
    while s > ls and text[s - 1] in " \t":  # span ends the line: drop the spaces before it
        s -= 1
    return s, b


def _fact_text_violation(fact_text: str, span: str, natural: str) -> str | None:
    """The ledger text of a withheld fact is model prose: bound it and forbid content words absent from the span/issue."""
    if not fact_text.strip():
        return "fact_text is empty"
    if len(fact_text) > MAX_FACT_TEXT_CHARS:
        return f"fact_text longer than {MAX_FACT_TEXT_CHARS} characters"
    allowed = _norm(span) + " " + _norm(natural)
    foreign = sorted({t for t in re.findall(r"[^\W_]+", _norm(fact_text)) if len(t) >= 5 and t not in allowed})
    if foreign:
        return f"fact_text introduces words absent from the removed span and the issue: {foreign[:5]}"
    return None


def withhold(natural_issue: str, model_id: str) -> dict:
    """Category-targeted withholding where the model only SELECTS spans and code performs the deletion.

    Each selected excerpt (at least MIN_EXCERPT_CHARS / MIN_EXCERPT_TOKENS after `_norm`) must match the natural issue
    verbatim (after `_norm`); it is extended to whole sentence/line boundaries and every occurrence is deleted. Deletion
    is line-aware (`_deletion_range`): whole lines go with their newline, mid-line deletions splice with one space, and
    every other byte of the natural issue is preserved, so code blocks, tracebacks and paragraph breaks survive and no text
    can be added or disappear undeclared. The title line is never deletable, at most `MAX_REMOVED_FRACTION` of the
    sentence segments may go, at least one removal is required, duplicate selections collapse, `fact_text` is bounded and
    may not introduce content words absent from the span/issue, and every removed span must be absent from the brief.
    Malformed selections and violations get one correction round, then ValueError.

    Returns {"withheld_brief", "removed": [{category, kind, discovery, fact_text, selected, excerpt, start, end,
    occurrences}], "deleted": [(start, end)], "kept": [(start, end)], "stats": {...}}; `natural[start:end] == excerpt`
    for every removal, kept + deleted partition the natural text, and the brief is the kept text byte for byte."""
    messages = [{"role": "user", "content": natural_issue}]
    title_a, title_b = _title_range(natural_issue)
    title = natural_issue[title_a:title_b]
    segs = _segments(natural_issue)
    bad: list[str] = []
    for _attempt in range(2):
        out = chat_json(model_id, WITHHOLD_PROMPT, messages, WITHHOLD_SCHEMA, max_tokens=5000)
        bad, removed, ranges, seen_ranges, duplicates = [], [], [], set(), 0
        items = (out.get("removed") if isinstance(out, dict) else None) or []
        if not isinstance(items, list) or not items:
            bad.append("no removed spans")
            items = []
        for item in items:
            if not isinstance(item, dict):
                bad.append(f"selection must be an object with category/excerpt/kind/discovery: {str(item)[:60]!r}")
                continue
            if item.get("category") not in WITHHOLD_CATEGORIES:
                bad.append(f"invalid category {item.get('category')!r}")
            if item.get("kind") not in WITHHOLD_KINDS:
                bad.append(f"invalid kind {item.get('kind')!r}")
            if item.get("discovery") not in WITHHOLD_DISCOVERY:
                bad.append(f"invalid discovery {item.get('discovery')!r}; use on-request or repo-derivable")
            selected, fact_text = item.get("excerpt"), item.get("fact_text")
            if not isinstance(selected, str):
                bad.append(f"excerpt must be a string, got {type(selected).__name__}")
                continue
            if fact_text is not None and not isinstance(fact_text, str):
                bad.append(f"fact_text must be a string, got {type(fact_text).__name__}")
                fact_text = None
            needle = _norm(selected)
            if len(needle) < MIN_EXCERPT_CHARS or len(needle.split()) < MIN_EXCERPT_TOKENS:
                bad.append(f"selected span too short (need >= {MIN_EXCERPT_CHARS} chars and >= {MIN_EXCERPT_TOKENS} words): {selected!r}")
                continue
            spans = _find_spans(natural_issue, selected)
            if not spans:
                bad.append(f"excerpt is not a verbatim span of the issue: {selected!r}")
                continue
            extended = [_extend(s, segs) for s in spans]
            if any(a < title_b and b > title_a for a, b in extended):
                bad.append(f"the title line cannot be withheld: {selected!r}")
                continue
            if extended[0] in seen_ranges:
                duplicates += 1
                continue
            seen_ranges.add(extended[0])
            ranges.extend(extended)
            a, b = extended[0]
            span = natural_issue[a:b]
            text = (fact_text or "").strip() or span
            problem = _fact_text_violation(text, span, natural_issue)
            if problem:
                bad.append(f"{problem} (span {selected!r})")
                continue
            removed.append({"category": item.get("category"), "kind": item.get("kind"), "discovery": item.get("discovery"),
                            "fact_text": text, "selected": selected, "excerpt": span, "start": a, "end": b,
                            "occurrences": [{"start": x, "end": y} for x, y in extended]})
        merged = _merge(ranges)
        removed_segs = sum(1 for s in segs if any(a <= s[0] and s[1] <= b for a, b in merged))
        if segs and removed_segs / len(segs) > MAX_REMOVED_FRACTION:
            bad.append(f"removed {removed_segs} of {len(segs)} sentences; the cap is {int(MAX_REMOVED_FRACTION * 100)}%")
        deleted = _merge([_deletion_range(natural_issue, a, b) for a, b in merged])
        if deleted and deleted[-1][1] == len(natural_issue) and not natural_issue.endswith("\n") and natural_issue[deleted[-1][0] - 1:deleted[-1][0]] == "\n":
            deleted[-1] = (deleted[-1][0] - 1, deleted[-1][1])  # the natural has no trailing newline: the brief must not gain one
        kept, pos = [], 0
        for a, b in deleted:
            if a > pos:
                kept.append((pos, a))
            pos = b
        if pos < len(natural_issue):
            kept.append((pos, len(natural_issue)))
        brief = "".join(natural_issue[a:b] for a, b in kept)
        if title and title not in brief:
            bad.append("missing title")
        for r in removed:
            if r["excerpt"].casefold() in brief.casefold():
                bad.append(f"removed span still visible in the brief: {r['excerpt']!r}")
        if not bad:
            return {"withheld_brief": brief, "removed": removed, "deleted": deleted, "kept": kept,
                    "stats": {"sentences_total": len(segs), "sentences_removed": removed_segs,
                              "fraction_removed": round(removed_segs / max(1, len(segs)), 3), "duplicate_selections": duplicates,
                              "prompt_version": WITHHOLD_PROMPT_VERSION}}
        messages.append({"role": "assistant", "content": json.dumps(out)})
        messages.append({"role": "user", "content": "Correct these violations: " + json.dumps(bad)})
    raise ValueError("withholding failed validation after two attempts: " + "; ".join(bad))


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _norm(text: str | None) -> str:
    """Grounding normalization shared with the judge (`judge.norm_text`): drop leading markdown blockquote markers,
    collapse whitespace, casefold. Applied identically to corpus and excerpt."""
    return norm_text(text)


def _corpora(row: dict) -> dict[str, str]:
    """Whitespace-normalized source text per provenance; a fact's excerpt must be a verbatim span of its corpus. `authored`
    facts are decisions pinned only by the merged tests/patch, so their corpus is test_patch + patch."""
    t = row.get("thread") or {}
    ann = row.get("annotation") or {}

    def bodies(items):
        return [x.get("body") or "" for x in items or []]

    issue_parts = [row.get("hints_text") or ""]
    for i in t.get("linked_issues") or []:
        issue_parts += [i.get("title") or "", i.get("body") or "", *bodies(i.get("comments"))]
    return {"original-issue": _norm(row.get("problem_statement")),
            "issue-thread": _norm("\n".join(issue_parts)),
            "pr-thread": _norm("\n".join([t.get("pr_title") or "", t.get("pr_body") or "", *bodies(t.get("pr_comments"))])),
            "pr-review": _norm("\n".join(bodies(t.get("reviews")) + bodies(t.get("review_comments")))),
            "annotator-note": _norm("\n".join([ann.get("underspecified_notes") or "", ann.get("false_negative_notes") or ""])),
            "authored": _norm(evidence_text(row))}  # diff markers/headers stripped, so excerpts may span adjacent added lines


def ledger_violations(d: dict, row: dict, extra_facts: list[dict] | None = None, *, corpus_by_fact: dict[str, str] | None = None,
                      allow_reserved: bool = False, require_f2p_link: bool = True) -> list[str]:
    """Code-owned grounding of ledger facts. Every fact must quote a verbatim span (after normalization) of the text its
    provenance names (`authored` = the merged tests/patch; `corpus_by_fact[id]` overrides the corpus, used for the withheld
    facts whose corpus is their own removed span) and cite a URL under the repository; and, unless `require_f2p_link` is
    off, at least one on-request fact (in the model ledger or in `extra_facts`, the code-synthesized ones) must link a
    FAIL_TO_PASS test, otherwise interaction is not load-bearing and approval would fail anyway. Reserved id prefixes are
    violations for model output (`allow_reserved=False`) and permitted for code-generated facts."""
    corpora = _corpora(row)
    url_prefix = f"https://github.com/{row['repo']}/".casefold()
    bad = []
    ledger = d.get("ledger") or []
    for f in ledger:
        fid, prov = f.get("id", "?"), f.get("provenance")
        if not allow_reserved and str(fid).startswith(RESERVED_FACT_PREFIXES):
            bad.append(f"{fid}: fact ids starting with {RESERVED_FACT_PREFIXES} are reserved for code-generated facts")
        if prov not in corpora:
            bad.append(f"{fid}: unknown provenance {prov!r}")
            continue
        corpus = _norm(corpus_by_fact[fid]) if corpus_by_fact and fid in corpus_by_fact else corpora[prov]
        excerpt = _norm(f.get("source_excerpt"))
        where = "removed span" if corpus_by_fact and fid in corpus_by_fact else ("merged tests/patch" if prov == "authored" else f"{prov} text")
        if len(excerpt) < MIN_EXCERPT_CHARS or len(excerpt.split()) < MIN_EXCERPT_TOKENS:
            bad.append(f"{fid}: source_excerpt too short to ground a fact (need >= {MIN_EXCERPT_CHARS} chars and >= {MIN_EXCERPT_TOKENS} words): {excerpt!r}")
        elif excerpt not in corpus:
            bad.append(f"{fid}: source_excerpt is not a verbatim span of the {where}: {excerpt[:80]!r}")
        if not (f.get("source_url") or "").casefold().startswith(url_prefix):
            bad.append(f"{fid}: source_url must be under https://github.com/{row['repo']}/")
        if require_f2p_link and f.get("discovery") == "on-request" and not (set(f.get("tests") or []) & set(row.get("fail_to_pass") or [])):
            bad.append(f"{fid}: every on-request fact must carry `tests` drawn from the FAIL_TO_PASS ids {sorted(row.get('fail_to_pass') or [])}")
    f2p = set(row.get("fail_to_pass") or [])
    if require_f2p_link and not any(f.get("discovery") == "on-request" and set(f.get("tests") or []) & f2p for f in [*ledger, *(extra_facts or [])]):
        bad.append(f"no on-request fact links a FAIL_TO_PASS test id (interaction would not be load-bearing); link one of {sorted(f2p)}")
    return bad


def _test_blocks(test_patch: str) -> list[tuple[str | None, str | None, list[str]]]:
    """(file, test function or None, body lines) blocks of the hidden-test diff, built from added and context lines with the
    diff markers stripped. A block starts at every file header, at every hunk header (`@@ -a,b +c,d @@ def test_existing(...)`
    names the function an edit lands in, so lines added inside an existing test belong to it) and at every `def test_*`
    line, and runs to the next boundary."""
    blocks: list[tuple[str | None, str | None, list[str]]] = []
    current_file = None
    for raw in (test_patch or "").splitlines():
        m = re.match(r"diff --git a/(\S+)", raw)
        if m:
            current_file = m.group(1)
            blocks.append((current_file, None, []))
            continue
        if raw.startswith("@@"):
            trailer = raw.split("@@", 2)[2] if raw.count("@@") >= 2 else ""
            d = _TEST_DEF.match(trailer.strip())
            blocks.append((current_file, d.group(1) if d else None, []))
            continue
        if raw.startswith(DIFF_HEADER_PREFIXES) or raw[:1] == "-":
            continue
        line = raw[1:] if raw[:1] in ("+", " ") else raw
        d = _TEST_DEF.match(line)
        if d:
            blocks.append((current_file, d.group(1), []))
        if blocks:
            blocks[-1][2].append(line)
    return blocks


def _f2p_for_block(file: str | None, fn: str | None, fail_to_pass: list[str]) -> set[str]:
    out = set()
    for t in fail_to_pass:
        path, *rest = t.split("::")
        func = re.split(r"[\[(]", rest[-1])[0] if rest else ""
        if path == file and (fn is None or func == fn):
            out.add(t)
    return out


_KEYWORDS = set(keyword.kwlist) | set(getattr(keyword, "softkwlist", []))


def content_words(text: str | None) -> set[str]:
    """Casefolded alphanumeric tokens of length >= 5 that are not Python keywords."""
    return {t for t in re.findall(r"[^\W_]+", (text or "").casefold()) if len(t) >= 5 and t not in _KEYWORDS}


def auto_link_tests(text: str | None, test_patch: str, fail_to_pass: list[str], min_shared: int = 2) -> list[str]:
    """Code-owned fallback link for an on-request fact without tests: the FAIL_TO_PASS tests whose function body in the
    hidden-test diff shares at least `min_shared` distinct content words with the fact text."""
    words = content_words(text)
    out: set[str] = set()
    if not words:
        return []
    for file, fn, lines in _test_blocks(test_patch):
        if fn is not None and len(words & content_words("\n".join(lines))) >= min_shared:
            out |= _f2p_for_block(file, fn, fail_to_pass)
    return sorted(out)


def tests_for_excerpt(test_patch: str, excerpt: str, fail_to_pass: list[str]) -> list[str]:
    """FAIL_TO_PASS ids whose test function (a `def test_*` block of the hidden-test diff, or the function named by the hunk
    header the excerpt was added under) contains `excerpt`. No fallback: an excerpt outside every test function (module
    level, or evidence from the source patch) links nothing."""
    needle = _norm(excerpt)
    out: set[str] = set()
    if not needle:
        return []
    for f, fn, lines in _test_blocks(test_patch):
        if fn is not None and needle in _norm("\n".join(lines)):
            out |= _f2p_for_block(f, fn, fail_to_pass)
    return sorted(out)


def tests_resolved_facts(row: dict, judged: dict) -> list[dict]:
    """Code-synthesized ledger facts for judge gaps that only the merged tests resolve: provenance `authored`, source_url =
    the PR, source_excerpt = the verbatim evidence (re-checked here against the added/context lines of the test files and
    `evidence_ok`), discovery on-request, linked to the FAIL_TO_PASS tests whose function contains the excerpt — a gap whose
    excerpt sits in no test function is not tests-resolved and yields no fact. The ledger `text` is the judge's prose only
    when it stays within `_fact_text_violation` (<= 400 chars, content words from the excerpt or the issue), else the
    question, else the excerpt itself. Distinct decisions sharing evidence are kept apart by (evidence, question). Ids
    `tests-NNN`."""
    tests_corpus = _norm(diff_body_text(row.get("test_patch")))
    thread = row.get("thread") or {}
    pr_url = thread.get("pr_url") or f"https://github.com/{row['repo']}/pull/{str(row.get('instance_id', '')).rsplit('-', 1)[-1]}"
    kinds = {"behavior", "interface", "constraint", "reproduction", "context"}
    natural = row.get("problem_statement") or ""
    out, seen = [], set()
    for g in judged.get("gaps") or []:
        if not isinstance(g, dict) or not (g.get("resolved_by") == "tests" and g.get("derivable_from_repo") is False and g.get("consequential") is True):
            continue
        evidence = (g.get("evidence") or "").strip()
        key = _norm(evidence)
        question = (g.get("question") or "").strip()
        if not evidence_ok(key) or key not in tests_corpus or (key, _norm(question)) in seen:
            continue
        tests = tests_for_excerpt(row.get("test_patch") or "", evidence, list(row.get("fail_to_pass") or []))
        if not tests:
            continue
        seen.add((key, _norm(question)))
        text = next((c.strip() for c in (g.get("text") or "", question) if c and c.strip() and _fact_text_violation(c.strip(), evidence, natural) is None),
                    evidence[:MAX_FACT_TEXT_CHARS])
        out.append({"id": f"tests-{len(out) + 1:03d}", "text": text, "kind": g.get("category") if g.get("category") in kinds else "behavior",
                    "provenance": "authored", "source_excerpt": evidence, "source_url": pr_url,
                    "authored_rationale": "Pinned only by the merged tests; no issue or thread statement resolves it (judge gap "
                                          f"{g.get('id', '?')}, resolved_by=tests)." + (f" Question a developer would ask: {question}" if question else ""),
                    "discovery": "on-request", "tests": tests})
    return out


_DIFF_PATHS = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.M)
_PLUS_PATH = re.compile(r"^\+\+\+ b/(\S+)", re.M)


def patch_paths(patch: str | None) -> set[str]:
    """Every path a unified diff touches (both sides of each `diff --git` header and the `+++ b/` lines)."""
    out: set[str] = set()
    for a, b in _DIFF_PATHS.findall(patch or ""):
        out.update({a, b})
    out.update(_PLUS_PATH.findall(patch or ""))
    return out


def protected_paths_for(test_files: list[str], extras: list, gold_paths: set[str]) -> tuple[list[str], list[dict]]:
    """Code-owned protected paths: the hidden test files, plus model-proposed extras only when they are test-tree paths
    (`github_repo._TEST_PATH`) that the gold patch does not touch; everything else is dropped with a reason. Then the hard
    invariant: no path the gold patch modifies may be protected — the oracle control would be graded an invalid submission —
    and a violation is a drafter error, never a correction round."""
    from swesim.sources.github_repo import _TEST_PATH

    kept, dropped = set(test_files), []
    for p in extras or []:
        if not isinstance(p, str) or not p.strip():
            dropped.append({"path": p, "reason": "not a path"})
            continue
        p = p.strip()
        if p in gold_paths:
            dropped.append({"path": p, "reason": "touched by the gold patch"})
        elif not _TEST_PATH.search(p):
            dropped.append({"path": p, "reason": "not a test-tree path"})
        else:
            kept.add(p)
    shielded = sorted(kept & gold_paths)
    if shielded:
        raise ValueError(f"drafter error: protected_paths would shield files the gold patch modifies: {shielded}")
    return sorted(kept), dropped


def _author(row: dict, judged: dict, model_id: str, extra_facts: list[dict] | None = None) -> tuple[dict, list[str]]:
    """One LLM authoring call, validated by `ledger_violations`, one correction round, then a code-owned fallback that links
    on-request facts without tests to the FAIL_TO_PASS tests sharing content words with them (`auto_link_tests`), then fail.
    Returns (draft dict, ids of auto-linked facts)."""
    f2p = list(row.get("fail_to_pass") or [])
    prefix = "FAIL_TO_PASS ids (every on-request fact must carry `tests` drawn from this list): " + json.dumps(f2p) + "\n\n"
    judged_json = json.dumps(judged, ensure_ascii=False)
    view = llm_view(row, budget=CONTEXT_BUDGET_CHARS, extra_chars=len(prefix) + len(judged_json) + len('{"candidate": , "judge": }'))
    user = prefix + json.dumps({"candidate": view, "judge": judged}, ensure_ascii=False)
    if len(user) > CONTEXT_BUDGET_CHARS:
        raise ValueError("candidate exceeds authoring context budget")
    messages = [{"role": "user", "content": user}]
    bad: list[str] = []
    d: dict = {}
    for attempt in range(2):
        d = chat_json(model_id, SYSTEM, messages, SCHEMA, max_tokens=8000)
        bad = ledger_violations(d, row, extra_facts)
        if not bad:
            return d, []
        if attempt == 0:
            messages.append({"role": "assistant", "content": json.dumps(d)})
            messages.append({"role": "user", "content": "Correct these violations: " + json.dumps(bad)})
    auto_linked = []
    for f in d.get("ledger") or []:
        if isinstance(f, dict) and f.get("discovery") == "on-request" and not (set(f.get("tests") or []) & set(f2p)):
            links = auto_link_tests(f.get("text"), row.get("test_patch") or "", f2p)
            if links:
                f["tests"] = sorted(set(f.get("tests") or []) | set(links))
                auto_linked.append(f.get("id"))
    if auto_linked:
        bad = ledger_violations(d, row, extra_facts)
        if not bad:
            return d, auto_linked
    raise ValueError("draft failed grounding validation after two attempts: " + "; ".join(bad))


def _normalize_groups(groups: list[dict]) -> list[dict]:
    total = sum(float(g.get("weight") or 0) for g in groups)
    if total > 0:
        for g in groups:
            g["weight"] = float(g["weight"]) / total
    return groups


def draft(row: dict, judged: dict, model_id: str, out_dir: Path, pipeline_version: str = "0.1.0", require_probe: bool = True) -> Path:
    if judged.get("model_id") and judged["model_id"] == model_id:
        raise ValueError(f"judge and draft models must differ so their errors are not correlated (both {model_id})")
    probe = row.get("probe") or {}
    if require_probe:
        if not probe.get("statuses"):
            raise ValueError("row has no offline probe metadata (measured FAIL_TO_PASS / PASS_TO_PASS from probe_tests); SWE-bench's lists were "
                             "produced with network access and do not reproduce in the offline verifier. Run `swesim probe` first, or pass "
                             "require_probe=False to draft unmeasured.")
        if probe.get("binding") != probe_binding(row):
            raise ValueError("probe metadata binding mismatch: the probe measured a different base_commit/test_patch than this row carries "
                             "(stale or foreign measurement). Re-run `swesim probe`, or pass require_probe=False to draft unmeasured.")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    assets = out_dir / "assets"
    assets.mkdir(exist_ok=True)
    synthesized = tests_resolved_facts(row, judged)
    bad = ledger_violations({"ledger": synthesized}, row, corpus_by_fact={f["id"]: f["source_excerpt"] for f in synthesized},
                            allow_reserved=True, require_f2p_link=False)
    if bad:  # code-built from judge evidence; a failure here is a bug or an unusable evidence span, never a model correction
        raise ValueError("synthesized tests-resolved facts failed grounding: " + "; ".join(bad))
    d, auto_linked = _author(row, judged, model_id, extra_facts=synthesized)

    pw = d.get("plausible_wrong") or {}
    if not (pw.get("diff") or "").strip():
        raise ValueError("plausible-wrong control needs a non-empty diff")
    files = {"oracle.diff": row["patch"].encode(), "test.patch": row["test_patch"].encode(), "plausible-wrong.diff": pw["diff"].encode()}
    alt = d.get("alternative_correct") or None
    if alt and (alt.get("diff") or "").strip():
        files["alternative-correct.diff"] = alt["diff"].encode()
    elif judged.get("structural_tests"):
        raise ValueError("structural tests require an alternative-correct control and the drafter produced none")
    else:
        alt = None

    test_files = test_files_from_patch(row["test_patch"])
    protected, protected_dropped = protected_paths_for(test_files, d.get("protected_paths_extra") or [], patch_paths(row["patch"]))
    base_files = {p: threads.base_text(row["repo"], row["base_commit"], p) for p in [*test_files, "conftest.py", "sitecustomize.py"]}
    control_diffs = tamper.all_controls(base_files)
    for name, diff in control_diffs.items():
        if not diff:
            raise ValueError("empty tamper control")
        files[f"{name}.diff"] = diff.encode()
    is_github = row.get("source") == "github-repo"
    if is_github:
        files["Dockerfile.base"] = row["dockerfile"].encode()
    manifest = {}
    for name, data in files.items():
        (assets / name).write_bytes(data)
        manifest[name] = {"path": f"assets/{name}", "sha256": _sha(data), "bytes": len(data)}

    nodeid_map = row.get("nodeid_map") or {}
    selected = [nodeid_map.get(t, t) for t in row["fail_to_pass"] + row["pass_to_pass"]]
    interpreter = row.get("test_python") or TEST_PYTHON
    prefix = shlex.quote(interpreter) + " -I /tests/run_pytest.py -rA -p no:cacheprovider --junitxml={junit_path} "
    # The verifier substitutes {junit_path}/{nodeid} with str.replace (integrator ruling), so node ids keep literal braces.
    runner_cmd = prefix + " ".join(shlex.quote(t) for t in selected)
    if len(runner_cmd) > RUNNER_COMMAND_MAX_CHARS:
        raise ValueError(f"runner command with {len(selected)} node ids is {len(runner_cmd)} chars, over the single-argument limit; "
                         "candidate needs a nodeid-file runner")
    thread = row.get("thread") or {}

    environment = {"kind": "prebuilt-image", "image": row.get("image"), "repo_url": f"https://github.com/{row['repo']}",
                   "base_commit": row["base_commit"], "language": "python", "repo_dir": row.get("repo_dir", "/testbed"),
                   "test_command": runner_cmd, "gold_commit": thread.get("gold_commit")}
    if is_github:
        environment.update(kind="dockerfile", image=None, dockerfile_asset="Dockerfile.base")
    controls = {"oracle": "oracle.diff",
                "plausible_wrong": [{"name": pw["name"], "asset": "plausible-wrong.diff", "rationale": pw["rationale"],
                                     "expected_success": 0, "expected_reward_max": 0.5, "cls": "plausible-wrong"}],
                "alternative_correct": [{"name": alt["name"], "asset": "alternative-correct.diff", "rationale": alt["rationale"],
                                         "expected_success": 1, "cls": "alternative-correct"}] if alt else [],
                "tamper": [{"name": n, "asset": f"{n}.diff", "rationale": "code-generated exploit", "expected_reward": 0.0, "cls": "incorrect-exploit"}
                           for n in control_diffs]}
    spec = {"id": row["instance_id"].lower().replace("__", "--"), "version": 1, "source": row.get("source", "swebench-annotated"),
            "source_ref": row["instance_id"], "created_at": datetime.now(timezone.utc).isoformat(), "pipeline_version": pipeline_version,
            "environment": environment, "assets": manifest,
            "briefs": [{"profile": "natural", "text": row["problem_statement"], "provenance": "original-issue"}],
            "ledger": [*d["ledger"], *synthesized], "operations": d.get("operations") or [], "persona": d["persona"], "requirements": d.get("requirements") or [],
            "contract": {"fail_to_pass": row["fail_to_pass"], "pass_to_pass": row["pass_to_pass"], "test_assets": ["test.patch"],
                         "protected_paths": protected,
                         "runner": {"kind": "pytest-ra", "command": runner_cmd, "single_command": prefix + "{nodeid}", "test_files": test_files,
                                    "nodeid_map": nodeid_map}},
            "checks": d.get("checks") or [], "controls": controls, "groups": _normalize_groups(d["groups"]),
            "difficulty": {"annotated": row["annotation"]["difficulty"], "judged": judged}, "thread": dict(thread)}

    withheld = withhold(row["problem_statement"], model_id)
    spec["briefs"].append({"profile": "withheld", "text": withheld["withheld_brief"], "provenance": "withheld"})
    issue_url = next((i["issue_url"] for i in thread.get("linked_issues") or []), thread.get("pr_url"))
    if not issue_url:
        raise ValueError("withheld facts need source URL")
    withheld_facts = []
    for i, item in enumerate(withheld["removed"]):
        fid = f"withheld-{i + 1:03d}"
        if any(f.get("id") == fid for f in spec["ledger"]):
            raise ValueError("reserved withholding fact id")
        discovery = "repo-derivable" if item["category"] in REPO_DERIVABLE_CATEGORIES else item["discovery"]
        withheld_facts.append({"id": fid, "text": item["fact_text"], "kind": item["kind"], "provenance": "original-issue",
                               "source_excerpt": item["excerpt"], "source_url": issue_url, "discovery": discovery, "tests": []})
        # Operation is frozen at {type, target, rationale}: the exact span and its offsets in the natural text ride in the rationale
        # (machine-readable copy in draft.meta.json under "withholding").
        offsets = ", ".join(f"natural[{o['start']}:{o['end']}]" for o in item["occurrences"])
        spec["operations"].append({"type": "withholding", "target": fid,
                                   "rationale": f"Removed category: {item['category']}; {offsets} = {item['excerpt']!r}"})
    # The withheld facts go through the same grounding gate as every other fact, with their own removed span as corpus.
    bad = ledger_violations({"ledger": withheld_facts}, row, corpus_by_fact={f["id"]: f["source_excerpt"] for f in withheld_facts},
                            allow_reserved=True, require_f2p_link=False)
    if bad:
        raise ValueError("withheld facts failed grounding: " + "; ".join(bad))
    spec["ledger"].extend(withheld_facts)

    from swesim.models import ScenarioSpec
    parsed = ScenarioSpec.model_validate(spec)
    p = out_dir / "scenario.draft.json"
    p.write_text(parsed.model_dump_json(indent=2))
    atomic_json(out_dir / "judge.json", {"gaps": judged.get("gaps", []), "verified_rubric_score": judged.get("verified_rubric_score"),
                                         "keep": judged.get("keep"), **judged})
    atomic_json(out_dir / "draft.meta.json", {"model_id": model_id, "prompt_version": DRAFT_PROMPT_VERSION, "pipeline_version": pipeline_version,
                                              "created_at": spec["created_at"], "spec_hash": parsed.spec_hash, "auto_linked": auto_linked,
                                              "protected_paths_dropped": protected_dropped,
                                              "probe": ({k: probe.get(k) for k in ("kind", "probe_dir", "image_digest", "counts", "dropped", "swebench")} if probe.get("statuses")
                                                        else {"unmeasured": True}),
                                              "withholding": {"removed": [{**r, "fact_id": f"withheld-{i + 1:03d}"} for i, r in enumerate(withheld["removed"])],
                                                              "kept": withheld["kept"], "stats": withheld["stats"]}})
    return p
