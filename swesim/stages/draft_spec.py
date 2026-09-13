"""Draft stage: turn a judged candidate row into `scenario.draft.json` plus `assets/`.

The LLM authors the fact ledger, persona, requirements, checks, requirement groups and a plausible-wrong patch (and an
alternative-correct patch when the judge flagged structural tests). Code owns everything else: the natural brief is the
original issue verbatim, the withheld brief records every removal as an original-issue fact with a withholding operation,
tamper controls are generated from the base tree, assets carry a sha256 manifest, and `judge.json` is written next to the
draft for the approval gates. The result must validate against `ScenarioSpec` or the draft fails.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from swesim import costs, tamper
from swesim.costs import Usage
from swesim.llm import chat_json, take_last_usage
from swesim.paths import atomic_json
from swesim.sources import threads
from swesim.sources.github_repo import _TEST_PATH, is_code_path, probe_binding, runner_prefix
from swesim.sources.swebench_annotated import TEST_PYTHON, test_files_from_patch
import keyword

from swesim.stages.judge import DIFF_HEADER_PREFIXES, _BLOCKQUOTE, diff_body_text, evidence_ok, evidence_text, llm_view, norm_text, thread_text

DRAFT_PROMPT_VERSION = "draft-v1"
CONTEXT_BUDGET_CHARS = 120000
# Linux MAX_ARG_STRLEN is 131072 bytes per argv element; a `sh -c "<command>"` launch would fail (E2BIG) above it.
RUNNER_COMMAND_MAX_CHARS = 120000
SYSTEM = """You turn a GitHub issue, its thread, and the merged fix into a scenario record for an interactive coding task.
Rules: every fact must quote a short excerpt from the thread with its URL, or be marked authored with a rationale.
Facts the agent can learn by reading the repository are discovery=repo-derivable. Facts absent from the repository that the
maintainer decided are discovery=on-request. Private helper names chosen by the fix author are discovery=author-only and must
not link tests. Requirements come from review comments. Checks are user-level reproducers (not the hidden tests).
Every requirement must be grounded: its fact_id names a ledger fact that is not author-only, or its text is a verbatim excerpt of
the issue or thread (code checks both and rejects ungrounded requirements). Requirement text follows the same rule as fact text:
user-visible behaviour, never test construction or identifiers that exist only in the tests.
Groups partition FAIL_TO_PASS test ids into requirement groups with weights summing to 1. Also write one plausible-wrong patch:
a tempting incomplete or wrong fix (e.g. wrong exception type). Give control patches as `files`: the COMPLETE modified text of
each file you change (only files the merged fix touches; code builds the diff against the base commit), and use `diff` only when
that is impossible. Control names are short labels; code turns them into file-safe slugs.
Constraints the record must satisfy: every on-request fact carries `tests` drawn from the FAIL_TO_PASS ids listed at the top
of the user message (the decision the agent has to ask about is what those tests pin); fact, requirement, check and group ids match ^[a-z0-9][a-z0-9._-]{2,63}$ and never start with "withheld-" or
"tests-" (code-generated facts use those); test links use only the FAIL_TO_PASS and PASS_TO_PASS ids given; check ids differ
from test ids. Every fact's source_excerpt is copied verbatim (no paraphrase) from the text its provenance names: issue-thread
= linked issue title/body/comments or hints_text, pr-thread = PR title/body/comments, pr-review = review bodies and review
comments, annotator-note = the annotation notes, original-issue = the problem statement; its source_url is a URL under the
repository. provenance=authored is only for decisions that nothing in the discussion states and only the merged tests or
patch pin: source_excerpt verbatim from test_patch or patch, source_url the PR URL, authored_rationale saying why no thread
statement exists, discovery on-request, tests = the FAIL_TO_PASS ids whose code the excerpt comes from. Code also adds such
facts from the judge's tests-resolved gaps. Every fact's text states behaviour the reporter or maintainer could observe and
say; it never describes how a hidden test is built: no assert/pytest wording, fixture names, private or dunder calls, test file
paths, or identifiers that exist only in the tests (code rejects such text). protected_paths_extra may name only test-tree
fixture or data files the hidden tests read; the hidden test files are protected by code and source files the fix modifies are
never protected. If the judge marked
structural_tests, also write one alternative-correct patch (a different implementation the tests should still accept);
otherwise set alternative_correct to null."""
SCHEMA = """{"briefs": [{"profile": "natural", "text": "...", "provenance": "original-issue"}],
"ledger": [{"id": "f01", "text": "...", "kind": "behavior|interface|constraint|reproduction|context",
  "provenance": "original-issue|issue-thread|pr-thread|pr-review|annotator-note|authored", "source_excerpt": "...", "source_url": "...",
  "authored_rationale": null, "discovery": "brief|on-request|volunteered-at-review|repo-derivable|author-only", "tests": ["nodeid"]}],
"operations": [{"type": "clarification|evaluator-repair|synthetic-extension", "target": "...", "rationale": "..."}],
"persona": {"summary": "...", "tone": "...", "disclosure_style": "...", "derived_from_urls": ["..."], "role": "maintainer|reporter"},
"requirements": [{"id": "r01", "text": "...", "fact_id": "f01 (a non-author-only ledger fact; null only when text is a verbatim issue/thread excerpt)", "tests": ["nodeid"], "source_url": "..."}],
"checks": [{"id": "c01", "description": "...", "command": "python -c '...'", "expected_observation": "..."}],
"groups": [{"id": "g01", "weight": 0.5, "tests": ["nodeid"]}],
"plausible_wrong": {"name": "short label", "rationale": "...", "files": {"path/touched/by/the/fix.py": "COMPLETE modified file text"}, "diff": "unified diff, only if files cannot be given"},
"alternative_correct": {"name": "short label", "rationale": "...", "files": {"path.py": "COMPLETE modified file text"}, "diff": "unified diff, only if files cannot be given"} | null,
"protected_paths_extra": ["test-tree fixture or data files the hidden tests read (never source files; code drops anything else)"]}"""

VISIBLE_DISCOVERY = {"brief", "on-request", "volunteered-at-review", "repo-derivable"}  # everything the stakeholder prompt can render
HUMAN_PROVENANCE = ("original-issue", "issue-thread", "pr-thread", "pr-review")  # what a reporter or maintainer actually wrote
WITHHOLD_CATEGORIES = {"reproduction", "error_message", "expected_output", "location", "version", "proposed_fix"}
WITHHOLD_DISCOVERY = {"on-request", "repo-derivable"}  # a removed reporter statement is never author-only
REPO_DERIVABLE_CATEGORIES = {"location", "proposed_fix"}
MIN_EXCERPT_CHARS = 12   # a one-word "excerpt" grounds nothing; both bounds apply after _norm
MIN_EXCERPT_TOKENS = 3
RESERVED_FACT_PREFIXES = ("withheld-", "tests-", "thread-")  # code-generated fact ids the drafter may not use
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
_CLASS_DEF = re.compile(r"^\s*class\s+(\w+)\b")
WITHHOLD_PROMPT_VERSION = "withhold-v2"  # v2: the model selects spans, code deletes them; the brief is computed, never trusted
WITHHOLD_KINDS = {"reproduction", "context", "behavior", "interface", "constraint"}
MAX_REMOVED_FRACTION = 0.6  # of sentence/line segments
_WITHHOLDABLE = re.compile(r"```|^Traceback \(most recent call last\)", re.M)  # material the model usually can withhold
WITHHOLD_NUDGE = ("You selected no spans, but the issue contains a fenced code block or a traceback. If it is a reproduction, an "
                  "error message, or a literal expected output that a developer could ask the reporter for, select that span "
                  "verbatim under its category. If nothing qualifies, return {\"removed\": []} again.")
WITHHOLD_SCHEMA = ('{"removed":[{"category":"reproduction|error_message|expected_output|location|version|proposed_fix",'
                   '"excerpt":"verbatim contiguous span copied from the issue","paraphrase":"optional one-sentence paraphrase (metadata only)",'
                   '"kind":"reproduction|context|behavior|interface|constraint","discovery":"on-request|repo-derivable"}]}')
WITHHOLD_PROMPT = ("Select spans of this issue to withhold from the agent, by category. Do not rewrite the issue and do not return a brief: "
                   "code deletes the sentence or line containing each selected span and keeps everything else byte for byte. Each excerpt must be a "
                   "verbatim contiguous span copied from the issue, at least three words long; the maintainer's ledger quotes the removed span "
                   "itself. Never select the first line (the title); keep a recognizable symptom and a desired outcome; select at most about half "
                   "of the sentences. Selecting nothing is allowed when the issue has no safely removable statement. File locations and explicit "
                   "fix proposals are repo-derivable; expected behaviour, error text, versions and reproductions the maintainer can supply are "
                   "on-request.")
MAX_FACT_TEXT_CHARS = 400
WITHHELD_TEXT_PREFIX = "Reporter's original statement: "
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


def _kept_text(natural: str, ranges: list[tuple[int, int]]) -> tuple[str, list[tuple[int, int]], list[tuple[int, int]]]:
    """(brief, deleted, kept) for stripped removal ranges of `natural`: ranges are merged, widened to their line-aware
    deletion ranges (`_deletion_range`), and the brief is the concatenation of what remains, byte for byte. The natural's
    missing trailing newline is never gained by the brief."""
    merged = _merge(ranges)
    deleted = _merge([_deletion_range(natural, a, b) for a, b in merged])
    if deleted and deleted[-1][1] == len(natural) and not natural.endswith("\n") and natural[deleted[-1][0] - 1:deleted[-1][0]] == "\n":
        deleted[-1] = (deleted[-1][0] - 1, deleted[-1][1])  # the natural has no trailing newline: the brief must not gain one
    kept, pos = [], 0
    for a, b in deleted:
        if a > pos:
            kept.append((pos, a))
        pos = b
    if pos < len(natural):
        kept.append((pos, len(natural)))
    return "".join(natural[a:b] for a, b in kept), deleted, kept


def reconstruct_withheld(natural: str, excerpts: list[str]) -> str:
    """The withheld brief that exactly `excerpts` (the recorded removals, each a whole sentence/line of `natural`) produce:
    every occurrence of each excerpt, matched after `_norm` and widened to sentence/line segments like `withhold` does, is
    deleted line-aware and everything else is kept byte for byte. `withhold` checks its own brief against this, and the
    approval gate (and, by patch, `ScenarioSpec._integrity`) recompute it from the spec alone, so a sentence can never
    disappear from the brief without a ledger entry (B-P1-2)."""
    segs = _segments(natural)
    ranges = [_extend(span, segs) for e in excerpts for span in _find_spans(natural, e)]
    return _kept_text(natural, ranges)[0]


def verbatim_fact_text(span: str) -> str:
    """The ledger text of a withheld fact is code-owned: the removed span itself, whitespace-normalized and trimmed, cut to
    MAX_FACT_TEXT_CHARS with an ellipsis, prefixed so the stakeholder quotes the reporter rather than paraphrasing."""
    body = " ".join(span.split())
    if len(body) > MAX_FACT_TEXT_CHARS:
        body = body[:MAX_FACT_TEXT_CHARS - 1].rstrip() + "…"
    return WITHHELD_TEXT_PREFIX + body


def _accumulate(usage: Usage | None) -> Usage | None:
    """Add the usage of the `chat_json` call just made (`take_last_usage`) to a running total. None once any call left no
    usage behind (a monkeypatched or failed call): unknown is never recorded as zero."""
    last = take_last_usage()
    return None if usage is None or last is None else usage + last


def _usage_json(usage: Usage | None) -> dict | None:
    return usage.model_dump(mode="json") if usage is not None else None


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
    verbatim (after `_norm`); every occurrence is extended to whole sentence/line boundaries and each distinct extended
    occurrence becomes its own removal record (its own ledger fact and withholding operation), so a phrase that occurs in
    two sentences records both (B-P1-2). Deletion is line-aware (`_deletion_range`): whole lines go with their newline,
    mid-line deletions splice with one space, and every other byte of the natural issue is preserved, so code blocks,
    tracebacks and paragraph breaks survive and no text can be added or disappear undeclared: the brief must equal
    `reconstruct_withheld(natural, recorded excerpts)`. The title line is never deletable, at most `MAX_REMOVED_FRACTION`
    of the sentence segments may go, at least one removal is required, duplicate selections collapse, `fact_text` is
    bounded and may not introduce content words absent from the span/issue, and every removed span must be absent from the
    brief. Malformed selections and violations get one correction round, then ValueError.

    Zero removals is a valid outcome, not a failure: when the model selects nothing, or every selection is rejected after the
    correction round, the result has `removed == []`, `withheld_brief is None` and `stats.reason`; the draft then keeps only
    the natural brief. The ledger text of each removal is code-owned (`verbatim_fact_text`); the model's `paraphrase` is kept
    as metadata that nothing reads.

    Returns {"withheld_brief", "removed": [{category, kind, discovery, fact_text, paraphrase, selected, excerpt, start, end,
    occurrences}], "deleted": [(start, end)], "kept": [(start, end)], "stats": {...}}; `natural[start:end] == excerpt`
    for every removal (`occurrences` is that single range, kept for readers of draft.meta.json), kept + deleted partition
    the natural text, and the brief is the kept text byte for byte."""
    messages = [{"role": "user", "content": natural_issue}]
    title_a, title_b = _title_range(natural_issue)
    title = natural_issue[title_a:title_b]
    segs = _segments(natural_issue)
    bad: list[str] = []
    removed: list[dict] = []
    attempts = 0
    usage: Usage | None = Usage()  # summed over every withholder call; None when a call left no usage behind

    rejected: list[str] = []

    def none_removed(reason: str) -> dict:
        return {"withheld_brief": None, "removed": [], "deleted": [], "kept": [(0, len(natural_issue))], "usage": _usage_json(usage),
                "stats": {"sentences_total": len(segs), "sentences_removed": 0, "fraction_removed": 0.0, "duplicate_selections": 0,
                          "prompt_version": WITHHOLD_PROMPT_VERSION, "attempts": attempts, "reason": reason, "rejected_selections": rejected}}

    for _attempt in range(2):
        attempts += 1
        take_last_usage()
        out = chat_json(model_id, WITHHOLD_PROMPT, messages, WITHHOLD_SCHEMA, max_tokens=5000)
        usage = _accumulate(usage)
        # `rejected` = per-selection problems (that selection is dropped); `bad` = result-level problems (fatal after the correction round)
        bad, rejected, removed, ranges, seen_ranges, duplicates = [], [], [], [], set(), 0
        items = (out.get("removed") if isinstance(out, dict) else None) or []
        if not isinstance(items, list):
            rejected.append("removed must be a list of selections")
            items = []
        if not items and not rejected:
            if _attempt == 0 and _WITHHOLDABLE.search(natural_issue):
                # Span selection is a model choice and varies run to run (seaborn-3190 lost its withheld brief on a rebuild);
                # one nudge when the issue visibly carries withholdable material, then zero removals stands.
                messages.append({"role": "assistant", "content": json.dumps(out)})
                messages.append({"role": "user", "content": WITHHOLD_NUDGE})
                continue
            return none_removed("model selected no spans" if attempts == 1 else "model selected no spans after one nudge")
        for item in items:
            if not isinstance(item, dict):
                rejected.append(f"selection must be an object with category/excerpt/kind/discovery: {str(item)[:60]!r}")
                continue
            attrs = []
            if item.get("category") not in WITHHOLD_CATEGORIES:
                attrs.append(f"invalid category {item.get('category')!r}")
            if item.get("kind") not in WITHHOLD_KINDS:
                attrs.append(f"invalid kind {item.get('kind')!r}")
            if item.get("discovery") not in WITHHOLD_DISCOVERY:
                attrs.append(f"invalid discovery {item.get('discovery')!r}; use on-request or repo-derivable")
            selected, paraphrase = item.get("excerpt"), item.get("paraphrase", item.get("fact_text"))
            if not isinstance(selected, str):
                rejected.append(f"excerpt must be a string, got {type(selected).__name__}")
                continue
            if attrs:  # a Fact needs valid enum values; the selection is dropped rather than recorded with them
                rejected.extend(f"{a} (span {selected!r})" for a in attrs)
                continue
            if not isinstance(paraphrase, str) or not paraphrase.strip():
                paraphrase = None  # metadata only; never validated, never shown
            needle = _norm(selected)
            if len(needle) < MIN_EXCERPT_CHARS or len(needle.split()) < MIN_EXCERPT_TOKENS:
                rejected.append(f"selected span too short (need >= {MIN_EXCERPT_CHARS} chars and >= {MIN_EXCERPT_TOKENS} words): {selected!r}")
                continue
            spans = _find_spans(natural_issue, selected)
            if not spans:
                rejected.append(f"excerpt is not a verbatim span of the issue: {selected!r}; closest lines: "
                                f"{json.dumps(closest_lines(needle, [natural_issue]), ensure_ascii=False)}")
                continue
            extended = sorted({_extend(s, segs) for s in spans})
            if any(a < title_b and b > title_a for a, b in extended):
                rejected.append(f"the title line cannot be withheld: {selected!r}")
                continue
            fresh = [r for r in extended if r not in seen_ranges]
            if not fresh:
                duplicates += 1
                continue
            for a, b in fresh:  # B-P1-2: every expanded occurrence is its own recorded removal (one fact, one operation each)
                seen_ranges.add((a, b))
                ranges.append((a, b))
                span = natural_issue[a:b]
                removed.append({"category": item.get("category"), "kind": item.get("kind"), "discovery": item.get("discovery"),
                                "fact_text": verbatim_fact_text(span), "paraphrase": paraphrase, "selected": selected, "excerpt": span,
                                "start": a, "end": b, "occurrences": [{"start": a, "end": b}]})
        merged = _merge(ranges)
        removed_segs = sum(1 for s in segs if any(a <= s[0] and s[1] <= b for a, b in merged))
        if segs and removed_segs / len(segs) > MAX_REMOVED_FRACTION:
            bad.append(f"removed {removed_segs} of {len(segs)} sentences; the cap is {int(MAX_REMOVED_FRACTION * 100)}%")
        brief, deleted, kept = _kept_text(natural_issue, ranges)
        if title and title not in brief:
            bad.append("missing title")
        for r in removed:
            if r["excerpt"].casefold() in brief.casefold():
                bad.append(f"removed span still visible in the brief: {r['excerpt']!r}")
        if removed and reconstruct_withheld(natural_issue, [r["excerpt"] for r in removed]) != brief:
            bad.append("the recorded excerpts do not reconstruct the withheld brief (an excerpt occurs where no removal was recorded)")
        def success() -> dict:
            return {"withheld_brief": brief, "removed": removed, "deleted": deleted, "kept": kept, "usage": _usage_json(usage),
                    "stats": {"sentences_total": len(segs), "sentences_removed": removed_segs,
                              "fraction_removed": round(removed_segs / max(1, len(segs)), 3), "duplicate_selections": duplicates,
                              "prompt_version": WITHHOLD_PROMPT_VERSION, "attempts": attempts, "reason": None, "rejected_selections": rejected}}

        if not bad and not rejected:
            return success()
        if _attempt == 0:
            messages.append({"role": "assistant", "content": json.dumps(out)})
            messages.append({"role": "user", "content": "Correct these violations: " + json.dumps(rejected + bad, ensure_ascii=False)})
    if bad:  # a result-level problem with otherwise valid selections (cap, leak, lost title): the draft cannot use this withholding
        raise ValueError("withholding failed validation after two attempts: " + "; ".join(rejected + bad))
    if not removed:  # every selection was rejected: nothing safely removable, which is a valid outcome
        return none_removed("all selections rejected: " + "; ".join(rejected))
    return success()  # the valid selections proceed; the rejected ones are recorded


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _norm(text: str | None) -> str:
    """Grounding normalization shared with the judge (`judge.norm_text`): drop leading markdown blockquote markers,
    collapse whitespace, casefold. Applied identically to corpus and excerpt."""
    return norm_text(text)


def _corpus_sources(row: dict) -> dict[str, list[str]]:
    """Raw source texts per provenance. `authored` facts are decisions pinned only by the merged tests/patch, so their source
    is the diff body (markers/headers stripped, so excerpts may span adjacent added lines)."""
    t = row.get("thread") or {}
    ann = row.get("annotation") or {}

    def bodies(items):
        return [x.get("body") or "" for x in items or []]

    issue_parts = [row.get("hints_text") or ""]
    for i in t.get("linked_issues") or []:
        issue_parts += [i.get("title") or "", i.get("body") or "", *bodies(i.get("comments"))]
    return {"original-issue": [row.get("problem_statement") or ""],
            "issue-thread": issue_parts,
            "pr-thread": [t.get("pr_title") or "", t.get("pr_body") or "", *bodies(t.get("pr_comments"))],
            "pr-review": bodies(t.get("reviews")) + bodies(t.get("review_comments")),
            "annotator-note": [ann.get("underspecified_notes") or "", ann.get("false_negative_notes") or ""],
            "authored": [evidence_text(row)]}


def _corpora(row: dict) -> dict[str, str]:
    """Whitespace-normalized source text per provenance; a fact's excerpt must be a verbatim span of its corpus."""
    return {prov: _norm("\n".join(texts)) for prov, texts in _corpus_sources(row).items()}


def closest_lines(needle: str, texts: list[str], n: int = 3) -> list[str]:
    """The `n` source lines most similar (difflib) to a normalized excerpt, returned raw so a correction round can quote exactly."""
    lines = [line.strip() for text in texts for line in (text or "").splitlines() if line.strip()]
    by_norm: dict[str, str] = {}
    for line in lines:
        by_norm.setdefault(_norm(line), line)
    return [by_norm[m] for m in difflib.get_close_matches(needle, list(by_norm), n=n, cutoff=0.0)]


def excerpt_grounds(text: str | None, corpora: dict[str, str]) -> str | None:
    """The provenance whose (normalized) corpus contains `text` verbatim, subject to the fact-excerpt minimums; None when
    the text grounds in nothing. Used for requirements without a fact_id (their text must be a quote, not model prose)."""
    needle = _norm(text)
    if len(needle) < MIN_EXCERPT_CHARS or len(needle.split()) < MIN_EXCERPT_TOKENS:
        return None
    return next((prov for prov in HUMAN_PROVENANCE if prov in corpora and needle in corpora[prov]), None)


def _requirement_violations(d: dict, row: dict, facts_by_id: dict[str, dict], corpora: dict[str, str], *, demote_unsafe: bool,
                            demoted: set[str], filter_log: list[dict] | None) -> list[str]:
    """Requirements are stakeholder-visible text and get the fact rules (A-P1-2 / B-P1-2): `unsafe_fact_text` on every
    requirement, and every requirement grounded either by a `fact_id` naming a stakeholder-visible ledger fact or by text
    that is a verbatim excerpt of what humans wrote (`excerpt_grounds`). Strict mode returns violations for the correction
    round. Lenient mode (`demote_unsafe`) repairs `d["requirements"]` in place and records every decision in `filter_log`:
    unsafe text -> dropped; a requirement whose fact the sanitizer just demoted (`demoted`) -> rewritten to that fact's own
    excerpt when it is human text that passes the sanitizer, else dropped; model prose without any fact -> dropped. A link
    the model itself made to an author-only or unknown fact is left for the schema to reject (the correction round already
    named it; code never guesses what the model meant)."""
    bad: list[str] = []
    kept: list = []
    human = {p: corpora[p] for p in HUMAN_PROVENANCE if p in corpora}
    for r in d.get("requirements") or []:
        if not isinstance(r, dict):
            kept.append(r)  # the schema validation names the problem
            continue
        rid, fid = r.get("id", "?"), r.get("fact_id")
        reasons = unsafe_fact_text(r.get("text"), row)
        fact = facts_by_id.get(fid) if fid else None
        by_fact = fact is not None and fact.get("discovery") in VISIBLE_DISCOVERY
        by_excerpt = excerpt_grounds(r.get("text"), human)
        if not demote_unsafe:
            if reasons:
                bad.append(f"{rid}: requirement text would tell the agent how the hidden tests are built ({'; '.join(reasons)}); state the user-visible behaviour instead")
            if not by_fact and not by_excerpt:
                if fid and fact is None:
                    bad.append(f"{rid}: requirement is ungrounded: fact_id {fid!r} is not in the ledger; link a stakeholder-visible fact or make the text a verbatim excerpt of the issue or thread")
                elif fid:
                    bad.append(f"{rid}: requirement is ungrounded: fact {fid} is author-only; link a stakeholder-visible fact_id or make the text a verbatim excerpt of the issue or thread")
                else:
                    bad.append(f"{rid}: requirement is ungrounded: set fact_id to a stakeholder-visible ledger fact or make the text a verbatim excerpt of the issue or thread")
            kept.append(r)
            continue
        if reasons:
            if filter_log is not None:
                filter_log.append({"requirement_id": rid, "source": "model", "action": "dropped", "reasons": reasons, "fact_id": fid})
            continue
        if by_fact:
            kept.append(r)
            continue
        if by_excerpt:
            if fid:  # the fact is author-only; the text stands on its own quote
                r["fact_id"] = None
                if filter_log is not None:
                    filter_log.append({"requirement_id": rid, "source": "model", "action": "unlinked-grounded-by-excerpt", "fact_id": fid, "provenance": by_excerpt})
            kept.append(r)
            continue
        if fid and fid not in demoted:
            kept.append(r)  # the model's own author-only/unknown link: the schema rejects it, as the correction round warned
            continue
        excerpt = " ".join(str(fact.get("source_excerpt") or "").split()) if fact else ""
        if fact and fact.get("provenance") in HUMAN_PROVENANCE and excerpt_grounds(excerpt, human) and not unsafe_fact_text(excerpt, row) \
                and len(excerpt) <= MAX_FACT_TEXT_CHARS:
            r["text"], r["fact_id"] = excerpt, None
            if fact.get("source_url"):
                r["source_url"] = fact["source_url"]
            if filter_log is not None:
                filter_log.append({"requirement_id": rid, "source": "model", "action": "rewritten-from-thread", "fact_id": fid, "provenance": fact.get("provenance")})
            kept.append(r)
            continue
        if filter_log is not None:
            filter_log.append({"requirement_id": rid, "source": "model", "action": "dropped", "fact_id": fid,
                               "reasons": ["ungrounded: fact demoted and no verbatim human excerpt can replace the text" if fid
                                           else "ungrounded: no fact_id and the text is not a verbatim excerpt of the issue or thread"]})
    if demote_unsafe and isinstance(d.get("requirements"), list):
        d["requirements"][:] = kept
    return bad


def ledger_violations(d: dict, row: dict, extra_facts: list[dict] | None = None, *, corpus_by_fact: dict[str, str] | None = None,
                      allow_reserved: bool = False, require_f2p_link: bool = True, demote_unsafe: bool = False,
                      filter_log: list[dict] | None = None) -> list[str]:
    """Code-owned grounding of ledger facts and requirements. Every fact must quote a verbatim span (after normalization) of
    the text its provenance names (`authored` = the merged tests/patch; `corpus_by_fact[id]` overrides the corpus, used for
    the withheld facts whose corpus is their own removed span) and cite a URL under the repository; every stakeholder-visible
    fact (`VISIBLE_DISCOVERY`) and every requirement passes `unsafe_fact_text`; every requirement is grounded by a visible
    fact_id or a verbatim human excerpt (`_requirement_violations`); and, unless `require_f2p_link` is off, at least one
    on-request fact (in the model ledger or in `extra_facts`, the code-synthesized ones) must link a FAIL_TO_PASS test,
    otherwise interaction is not load-bearing and approval would fail anyway. Reserved id prefixes are violations for model
    output (`allow_reserved=False`) and permitted for code-generated facts. `demote_unsafe` is the lenient repair mode: an
    unsafe fact becomes author-only (tests cleared) and the requirements that depended on it are rewritten or dropped."""
    sources = _corpus_sources(row)
    corpora = {prov: _norm("\n".join(texts)) for prov, texts in sources.items()}
    url_prefix = f"https://github.com/{row['repo']}/".casefold()
    bad = []
    demoted: set[str] = set()
    ledger = d.get("ledger") or []
    for f in ledger:
        fid, prov = f.get("id", "?"), f.get("provenance")
        if not allow_reserved and str(fid).startswith(RESERVED_FACT_PREFIXES):
            bad.append(f"{fid}: fact ids starting with {RESERVED_FACT_PREFIXES} are reserved for code-generated facts")
        if prov not in corpora:
            bad.append(f"{fid}: unknown provenance {prov!r}")
            continue
        own = bool(corpus_by_fact and fid in corpus_by_fact)
        corpus = _norm(corpus_by_fact[fid]) if own else corpora[prov]
        excerpt = _norm(f.get("source_excerpt"))
        where = "removed span" if own else ("merged tests/patch" if prov == "authored" else f"{prov} text")
        if len(excerpt) < MIN_EXCERPT_CHARS or len(excerpt.split()) < MIN_EXCERPT_TOKENS:
            bad.append(f"{fid}: source_excerpt too short to ground a fact (need >= {MIN_EXCERPT_CHARS} chars and >= {MIN_EXCERPT_TOKENS} words): {excerpt!r}")
        elif excerpt not in corpus:
            near = closest_lines(excerpt, [corpus_by_fact[fid]] if own else sources[prov])
            bad.append(f"{fid}: source_excerpt is not a verbatim span of the {where}: {excerpt[:80]!r}; closest lines: {json.dumps(near, ensure_ascii=False)}")
        if not (f.get("source_url") or "").casefold().startswith(url_prefix):
            bad.append(f"{fid}: source_url must be under https://github.com/{row['repo']}/")
        if f.get("discovery") in VISIBLE_DISCOVERY:
            reasons = unsafe_fact_text(f.get("text"), row)  # stakeholder-visible text must never describe test construction
            if reasons and demote_unsafe:
                f["discovery"], f["tests"] = "author-only", []
                demoted.add(fid)
                if filter_log is not None:
                    filter_log.append({"fact_id": fid, "source": "model", "action": "demoted-author-only", "reasons": reasons})
            elif reasons:
                bad.append(f"{fid}: fact text would tell the agent how the hidden tests are built ({'; '.join(reasons)}); state the user-visible behaviour instead")
        if require_f2p_link and f.get("discovery") == "on-request" and not (set(f.get("tests") or []) & set(row.get("fail_to_pass") or [])):
            bad.append(f"{fid}: every on-request fact must carry `tests` drawn from the FAIL_TO_PASS ids {sorted(row.get('fail_to_pass') or [])}")
    facts_by_id = {f.get("id"): f for f in [*ledger, *(extra_facts or [])] if isinstance(f, dict)}
    bad += _requirement_violations(d, row, facts_by_id, corpora, demote_unsafe=demote_unsafe, demoted=demoted, filter_log=filter_log)
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
    current_file, current_class = None, None

    def qualified(name: str, indented: bool) -> str:
        return f"{current_class}::{name}" if indented and current_class else name

    for raw in (test_patch or "").splitlines():
        m = re.match(r"diff --git a/(\S+)", raw)
        if m:
            current_file, current_class = m.group(1), None
            blocks.append((current_file, None, []))
            continue
        if raw.startswith("@@"):
            trailer = raw.split("@@", 2)[2] if raw.count("@@") >= 2 else ""
            trailer = trailer[1:] if trailer.startswith(" ") else trailer  # the single separator space after "@@" is not indentation
            c = _CLASS_DEF.match(trailer.strip())
            if c:
                current_class = c.group(1)
            d = _TEST_DEF.match(trailer.strip())
            indented = d is not None and trailer != trailer.lstrip()
            if d and not indented:
                current_class = None  # a top-level test function: we have left any class
            blocks.append((current_file, qualified(d.group(1), indented) if d else None, []))
            continue
        if raw.startswith(DIFF_HEADER_PREFIXES) or raw[:1] == "-":
            continue
        line = raw[1:] if raw[:1] in ("+", " ") else raw
        c = _CLASS_DEF.match(line)
        if c and not line.startswith((" ", "\t")):
            current_class = c.group(1)
        d = _TEST_DEF.match(line)
        if d:
            indented = line != line.lstrip()
            if not indented:
                current_class = None
            blocks.append((current_file, qualified(d.group(1), indented), []))
        if blocks:
            blocks[-1][2].append(line)
    return blocks


def _qualname(nodeid: str) -> tuple[str, str]:
    """(file, 'Class::test_x' or 'test_x') of a pytest node id with its parametrization stripped."""
    path, *rest = nodeid.split("::")
    return path, re.split(r"[\[(]", "::".join(rest))[0] if rest else ""


def _f2p_for_block(file: str | None, fn: str | None, fail_to_pass: list[str]) -> set[str]:
    """Ids of `fail_to_pass` in `file` whose qualified name matches the block's function: exact when the block knows its class
    (`TestA::test_x`); by bare function name when it does not, or when the qualified match finds nothing (a class context the
    diff mis-tracked must not unlink a real test)."""
    exact, bare = set(), set()
    for t in fail_to_pass:
        path, qual = _qualname(t)
        if path != file:
            continue
        if fn is None or qual == fn:
            exact.add(t)
        if fn is not None and qual.split("::")[-1] == fn.split("::")[-1]:
            bare.add(t)
    return exact or bare


_KEYWORDS = set(keyword.kwlist) | set(getattr(keyword, "softkwlist", []))


def content_words(text: str | None) -> set[str]:
    """Casefolded alphanumeric tokens of length >= 5 that are not Python keywords."""
    return {t for t in re.findall(r"[^\W_]+", (text or "").casefold()) if len(t) >= 5 and t not in _KEYWORDS}


def auto_link_tests(text: str | None, test_patch: str, fail_to_pass: list[str], min_shared: int = 2) -> list[str]:
    """Code-owned fallback link for an on-request fact without tests: the FAIL_TO_PASS tests whose function *body* in the
    hidden-test diff shares at least `min_shared` distinct content words with the fact text. The `def` line is excluded and
    the function's own name tokens never count (`test_weighted_operations_nonexistent_dim` must not link every fact that
    says "weighted operations"), so the link rests on what the test actually does."""
    words = content_words(text)
    out: set[str] = set()
    if not words:
        return []
    for file, fn, lines in _test_blocks(test_patch):
        if fn is None:
            continue
        body = [line for line in lines if not _TEST_DEF.match(line)]
        body_words = content_words("\n".join(body)) - content_words(fn)
        if len(words & body_words) >= min_shared:
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


FACT_TEST_VOCABULARY = {"assert", "pytest", "monkeypatch", "tmp_path", "capsys", "capfd", "fixture", "parametrize", "pytester", "testdir"}
_IDENT_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_PRIVATE_ATTR = re.compile(r"\._[A-Za-z0-9_]+(?:\s*\()?|__[A-Za-z0-9_]+__")
_PATHLIKE = re.compile(r"[\w./-]+\.py\b")
_KEYWORDS = {k.casefold() for k in keyword.kwlist}


def _ident_tokens(text: str | None) -> set[str]:
    return {t.casefold() for t in _IDENT_TOKEN.findall(text or "")}


def unsafe_fact_text(text: str | None, row: dict) -> list[str]:
    """Reasons a stakeholder-visible fact text would tell the agent how the hidden tests are built (seaborn-3190: "constructs a
    pandas Series of booleans and passes it directly to Continuous()._setup with IntervalProperty()"). Forbidden, each only
    when absent from the reporter/maintainer corpus (problem_statement, thread, gold patch body): assert/pytest/fixture
    vocabulary and `test_`-prefixed names, identifiers (`[A-Za-z_][A-Za-z0-9_]{2,}`, Python keywords excepted) that occur in
    test_patch, private or dunder attribute references, and test-tree file paths. The last two are judged against what the
    humans wrote only (issue + thread): the gold patch naming `_setup` does not make `Continuous()._setup(...)` observable
    behaviour. A reporter's own statement therefore always passes (a withheld fact's prefix is ignored); the check guards
    paraphrases. Empty list = safe."""
    text = (text or "").removeprefix(WITHHELD_TEXT_PREFIX)
    human_text = "\n".join([row.get("problem_statement") or "", thread_text(row)])
    allowed, allowed_cf = _ident_tokens(human_text + "\n" + diff_body_text(row.get("patch"))), human_text.casefold()
    test_tokens = _ident_tokens(diff_body_text(row.get("test_patch")))
    reasons = []
    for tok in sorted(_ident_tokens(text)):
        if tok in allowed:
            continue
        if tok in FACT_TEST_VOCABULARY or tok.startswith("test_"):
            reasons.append(f"test vocabulary {tok!r}")
        elif tok in test_tokens and tok not in _KEYWORDS:
            reasons.append(f"identifier {tok!r} exists only in the tests")
    for m in _PRIVATE_ATTR.finditer(text):
        if m.group(0).casefold() not in allowed_cf:
            reasons.append(f"private attribute {m.group(0).strip()!r}")
    for m in _PATHLIKE.finditer(text):
        if _TEST_PATH.search(m.group(0)) and m.group(0).casefold() not in allowed_cf:
            reasons.append(f"test-tree path {m.group(0)!r}")
    return reasons


def tests_resolved_facts(row: dict, judged: dict, filter_log: list[dict] | None = None) -> list[dict]:
    """Code-synthesized ledger facts for judge gaps that only the merged tests resolve: provenance `authored`, source_url =
    the PR, source_excerpt = the verbatim evidence (re-checked here against the added/context lines of the test files and
    `evidence_ok`), linked to the FAIL_TO_PASS tests whose function contains the excerpt — a gap whose excerpt sits in no test
    function is not tests-resolved and yields no fact. The stakeholder-visible `text` is the reporter's observable requirement:
    the judge gap's `text`, else its `question`, the first that stays within `_fact_text_violation` and `unsafe_fact_text`;
    when neither can be phrased without test construction the fact is demoted to `author-only` (evaluator-only, no test link,
    text = the excerpt). Every decision is recorded in `filter_log`. Ids `tests-NNN`."""
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
        fid = f"tests-{len(out) + 1:03d}"
        candidates = [c.strip() for c in (g.get("text") or "", question) if isinstance(c, str) and c.strip()]
        rejected = {}
        safe = None
        for c in candidates:
            problems = ([v] if (v := _fact_text_violation(c, evidence, natural)) else []) + unsafe_fact_text(c, row)
            if problems:
                rejected[c] = problems
            else:
                safe = c
                break
        base = {"id": fid, "kind": g.get("category") if g.get("category") in kinds else "behavior", "provenance": "authored",
                "source_excerpt": evidence, "source_url": pr_url}
        if safe is None:  # cannot be phrased as observable behaviour: evaluator-only, never shown, never load-bearing
            out.append({**base, "text": evidence[:MAX_FACT_TEXT_CHARS], "discovery": "author-only", "tests": [],
                        "authored_rationale": f"Pinned only by the merged tests (judge gap {g.get('id', '?')}); no phrasing free of test construction was "
                                              "available, so this fact is evaluator-only." + (f" Question a developer would ask: {question}" if question else "")})
            if filter_log is not None:
                filter_log.append({"fact_id": fid, "source": "synthesized", "action": "demoted-author-only", "candidates_rejected": rejected})
        else:
            out.append({**base, "text": safe, "discovery": "on-request", "tests": tests,
                        "authored_rationale": "Pinned only by the merged tests; no issue or thread statement resolves it (judge gap "
                                              f"{g.get('id', '?')}, resolved_by=tests)." + (f" Question a developer would ask: {question}" if question else "")})
            if filter_log is not None:
                filter_log.append({"fact_id": fid, "source": "synthesized", "action": "kept", "text": safe, "candidates_rejected": rejected})
    return out


def thread_requirement_fact(row: dict, judged: dict) -> dict | None:
    """A short requirement-level on-request fact grounded in the discussion, used to keep the load-bearing gate honest when
    every synthesized fact had to be demoted: the first judge gap resolved by the thread whose `resolution_excerpt` is a
    verbatim span of a thread corpus and passes `unsafe_fact_text`, linked to the FAIL_TO_PASS tests its evidence sits in (or
    to the single FAIL_TO_PASS function). None when no such statement exists."""
    corpora = _corpora(row)
    thread = row.get("thread") or {}
    f2p = list(row.get("fail_to_pass") or [])
    kinds = {"behavior", "interface", "constraint", "reproduction", "context"}
    for g in judged.get("gaps") or []:
        if not isinstance(g, dict) or g.get("resolved_by") != "thread" or g.get("derivable_from_repo") is not False or g.get("consequential") is not True:
            continue
        rx = (g.get("resolution_excerpt") or "").strip()
        norm = _norm(rx)
        if len(norm) < MIN_EXCERPT_CHARS or len(norm.split()) < MIN_EXCERPT_TOKENS or len(rx) > MAX_FACT_TEXT_CHARS or unsafe_fact_text(rx, row):
            continue
        prov = next((p for p in ("pr-review", "pr-thread", "issue-thread") if norm in corpora[p]), None)
        if prov is None:
            continue
        tests = tests_for_excerpt(row.get("test_patch") or "", g.get("evidence") or "", f2p) or (f2p if len(f2p_functions(f2p)) == 1 else [])
        if not tests:
            continue
        url = thread.get("pr_url") if prov != "issue-thread" else next((i.get("issue_url") for i in thread.get("linked_issues") or []), None)
        url = url or thread.get("pr_url") or f"https://github.com/{row['repo']}/pull/{str(row.get('instance_id', '')).rsplit('-', 1)[-1]}"
        return {"id": "thread-001", "text": rx, "kind": g.get("category") if g.get("category") in kinds else "behavior",
                "provenance": prov, "source_excerpt": rx, "source_url": url, "discovery": "on-request", "tests": tests}
    return None


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


def definition_order(ids: list[str], test_order: list[str] | None, test_patch: str) -> tuple[list[str], str]:
    """Order the exact node ids the grader runs in the DEFINITION order of the post-test-patch test files, never
    FAIL_TO_PASS-then-PASS_TO_PASS (scikit-learn-13584: a polluting F2P test ran before a P2P test it corrupted). Source of
    truth is the probe's collected order (`test_order`); without it, the order of `def test_*` appearance in the hidden-test
    diff; ids unknown to either keep their given relative order at the end. Returns (ordered ids, source)."""
    ids = list(dict.fromkeys(ids))
    if test_order:
        rank = {t: i for i, t in enumerate(test_order)}
        known = sorted((t for t in ids if t in rank), key=rank.__getitem__)
        return known + [t for t in ids if t not in rank], "probe"
    funcs = [(f, fn) for f, fn, _ in _test_blocks(test_patch) if fn is not None]
    rank = {}
    for i, key in enumerate(funcs):
        rank.setdefault(key, i)

    def rank_of(t: str):
        path, qual = _qualname(t)
        if (path, qual) in rank:
            return rank[(path, qual)]
        return rank.get((path, qual.split("::")[-1]))  # a block that does not know its class matches by bare name

    known = [t for t in ids if rank_of(t) is not None]
    known.sort(key=rank_of)  # stable: parametrizations keep their given order
    return known + [t for t in ids if rank_of(t) is None], "diff" if known else "given"


def f2p_functions(fail_to_pass: list[str]) -> set[tuple[str, str]]:
    """(file, qualified function) pairs behind the FAIL_TO_PASS ids; parametrizations of one test collapse to one pair, while
    `TestA::test_x` and `TestB::test_x` stay distinct."""
    return {_qualname(t) for t in fail_to_pass}


def link_single_target(d: dict, fail_to_pass: list[str]) -> list[str]:
    """Code-owned rule: when every FAIL_TO_PASS id belongs to one test function (any parametrizations), every on-request fact
    without an F2P link is about that function and is linked to all of its ids. Returns the ids of the facts linked."""
    if len(f2p_functions(fail_to_pass)) != 1:
        return []
    linked = []
    for f in d.get("ledger") or []:
        if isinstance(f, dict) and f.get("discovery") == "on-request" and not (set(f.get("tests") or []) & set(fail_to_pass)):
            f["tests"] = list(f.get("tests") or []) + [t for t in fail_to_pass if t not in (f.get("tests") or [])]  # contract order
            linked.append(f.get("id"))
    return linked


CONTROL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
ASSET_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_BASE_FILES = 8
MAX_BASE_BYTES = 400_000


def slugify_control_name(label, fallback: str, taken: set[str]) -> str:
    """Code-owned control name: lowercase `[a-z0-9._-]`, 3-64 chars, derived from the model's label by a stable slugify,
    `-2`, `-3`, ... on collision. The compile stage turns control names into `controls/<name>.diff` paths, so a label such as
    "strip only width/height ratios ..." must never reach it verbatim."""
    s = re.sub(r"[^a-z0-9._-]+", "-", str(label or "").casefold())
    s = re.sub(r"-{2,}", "-", s).strip("-._")[:64].rstrip("-._")
    if not CONTROL_NAME_RE.match(s):
        s = fallback
    base, n, name = s, 2, s
    while name in taken:
        suffix = f"-{n}"
        name = base[:64 - len(suffix)].rstrip("-._") + suffix
        n += 1
    taken.add(name)
    return name


def check_asset_paths(names) -> None:
    for name in names:
        parts = str(name).split("/")
        if not parts or any(not ASSET_COMPONENT_RE.match(p) or p == ".." for p in parts):
            raise ValueError(f"drafter error: asset path component outside [A-Za-z0-9._-]: {name!r}")


def fetch_base_files(row: dict, paths, cache: dict[str, str | None] | None = None) -> tuple[dict[str, str | None], list[str]]:
    """Base-commit content of `paths` via `threads.base_text` (None = absent on base), bounded to MAX_BASE_FILES files and
    MAX_BASE_BYTES in total; returns (files, skipped paths). `cache` carries fetched content across authoring attempts."""
    files = dict(cache or {})
    skipped = []
    total = sum(len(t.encode()) for t in files.values() if t)
    for path in sorted(set(paths)):
        if path in files:
            continue
        if len(files) >= MAX_BASE_FILES:
            skipped.append(path)
            continue
        text = threads.base_text(row["repo"], row["base_commit"], path)
        if text is not None and total + len(text.encode()) > MAX_BASE_BYTES:
            skipped.append(path)
            continue
        files[path] = text
        total += len(text.encode()) if text else 0
    if cache is not None:
        cache.update(files)
    return files, skipped


def safe_repo_path(path) -> bool:
    """Repository-relative, no absolute or `..` components (model-supplied `files` keys and diff paths are written into a temp repo)."""
    return isinstance(path, str) and bool(path.strip()) and not path.startswith(("/", "\\")) and ".." not in path.split("/") and "\\" not in path


def verify_diff_applies(base_files: dict[str, str | None], diff: str) -> tuple[bool | None, str]:
    """`git apply --check` of `diff` against a temporary git repo holding `base_files` (a key with None = confirmed absent on
    base; a key that is missing = never fetched). (True, ""), (False, git error), or (None, reason) when it cannot be verified
    locally: reason starts with "unfetched:" when a touched file was not fetched (callers treat that as unusable) and with
    "no-git:" when git is unavailable (callers accept unverified)."""
    if shutil.which("git") is None:
        return None, "no-git: git unavailable locally; the Modal sweep verifies application"
    touched = patch_paths(diff)
    unsafe = sorted(p for p in touched if not safe_repo_path(p))
    if unsafe:
        return False, f"patch touches a path outside the repository: {unsafe}"
    missing = sorted(p for p in touched if p not in base_files)
    if missing:
        return None, f"unfetched: base content not fetched for {missing} (fetch bound)"
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()
        for path, text in base_files.items():
            if text is None or not safe_repo_path(path):
                continue
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "init.templateDir=", "-c", "init.defaultBranch=main"]
        subprocess.run([*git, "init", "-q"], cwd=repo, capture_output=True, text=True)
        subprocess.run([*git, "add", "-A"], cwd=repo, capture_output=True, text=True)
        patch = Path(td) / "control.diff"
        patch.write_text(diff)
        proc = subprocess.run([*git, "apply", "--check", str(patch)], cwd=repo, capture_output=True, text=True)
    return (True, "") if proc.returncode == 0 else (False, (proc.stderr or proc.stdout).strip() or "git apply --check failed")


def build_control_diff(ctrl: dict, base_files: dict[str, str | None]) -> tuple[str | None, str]:
    """(diff, method). Primary path: `files` = complete modified text per touched file -> well-formed diff via difflib against
    the fetched base ('difflib'); fallback: the model's own unified diff ('model-diff'); (None, 'missing') otherwise. A
    `files` path that was never fetched (missing key) is a violation — only a key holding None means the base tree confirmed
    the file is absent, and only then is a new-file diff emitted."""
    files = ctrl.get("files") if isinstance(ctrl.get("files"), dict) else None
    if files:
        parts = []
        for path, text in files.items():
            if not isinstance(path, str) or not isinstance(text, str) or not path.strip():
                continue
            if not safe_repo_path(path):
                raise DraftViolation(f"control path must be repository-relative without `..`: {path!r}")
            if path not in base_files:
                raise DraftViolation(f"control touches {path!r}, whose base-commit content could not be fetched (fetch bound: {MAX_BASE_FILES} files, "
                                     f"{MAX_BASE_BYTES} bytes); change only these fetched files: {sorted(base_files)}")
            base = base_files[path]
            if base is not None and text == base:
                continue  # unchanged file: nothing to diff
            parts.append(tamper.file_diff(path, base, text if text.endswith("\n") else text + "\n"))
        if parts:
            return "".join(parts), "difflib"
        raise DraftViolation("control patch changes nothing: every file in `files` equals its base-commit content")
    diff = ctrl.get("diff")
    if isinstance(diff, str) and diff.strip():
        return diff, "model-diff"
    return None, "missing"


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_INVERSIONS = ((" == ", " != "), (" != ", " == "), (" is not ", " is "), (" not in ", " in "), (" >= ", " < "), (" <= ", " > "),
               ("True", "False"), ("False", "True"))
_WORD_INVERSIONS = {"True", "False"}
_HEADER_STARTS = ("def ", "async def ", "class ", "@", "if ", "elif ", "else", "try", "except", "finally", "with ", "for ", "while ", "match ", "case ")
_PREFERRED_INVERSION_STARTS = ("if ", "elif ", "while ", "return ", "assert ", "raise ")


def _code_mask(text: str, state: dict) -> list[bool]:
    """True at the positions of `text` that are code: outside string literals (single-, double- and triple-quoted; a triple
    quote left open carries across lines in `state["quote"]`, so docstring continuation lines are not code) and before an
    inline `#` comment."""
    mask = [False] * len(text)
    quote = state.get("quote")
    i, n = 0, len(text)
    while i < n:
        if quote:
            if text[i] == "\\":
                i += 2
                continue
            if text.startswith(quote, i):
                i += len(quote)
                quote = None
                continue
            i += 1
            continue
        if text[i] == "#":
            break
        for q in ('"""', "'''", '"', "'"):
            if text.startswith(q, i):
                quote = q
                i += len(q)
                break
        else:
            mask[i] = True
            i += 1
    state["quote"] = quote if quote and len(quote) == 3 else None  # a single-quoted string cannot span lines
    return mask


def _code_of(text: str, mask: list[bool]) -> str:
    return "".join(ch for ch, m in zip(text, mask) if m).strip()


def _invert_once(text: str, mask: list[bool]) -> str | None:
    """`text` with the first comparison/boolean that sits entirely at code positions inverted; None when there is none."""
    for old, new in _INVERSIONS:
        pattern = rf"\b{old}\b" if old in _WORD_INVERSIONS else re.escape(old)
        for m in re.finditer(pattern, text):
            if all(mask[m.start():m.end()]):
                return text[:m.start()] + new + text[m.end():]
    return None


def _split_hunks(chunk: str) -> tuple[str, list[list[str]]]:
    head, hunks = [], []
    for line in chunk.splitlines(keepends=True):
        if line.startswith("@@"):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
        else:
            head.append(line)
    return "".join(head), hunks


def _new_side_lines(hunk: list[str], masks: list[list[bool]]):
    """(index, line, code) for the new-side lines of a hunk (context and added), with the string/comment state carried across
    lines so `code` is only what is code on that line."""
    match = _HUNK.match(hunk[0].rstrip("\n"))
    if match is None:
        return
    lineno = int(match.group(3)) - 1
    for li, line in enumerate(hunk[1:], start=1):
        if line.startswith(("-", "\\")):
            continue
        text = line[1:]
        mask = masks[lineno]
        lineno += 1
        yield li, line, _code_of(text, mask), mask


def _shift_new_starts(hunks: list[list[str]], delta: int) -> list[list[str]]:
    out = []
    for h in hunks:
        h = list(h)
        hm = _HUNK.match(h[0].rstrip("\n"))
        if hm and delta:
            ha, hb, hc, hd, ht = hm.groups()
            h[0] = f"@@ -{ha},{hb if hb is not None else 1} +{int(hc) + delta},{hd if hd is not None else 1} @@{ht}\n"
        out.append(h)
    return out


def _incorrect_variants(head: str, hunks: list[list[str]], gold_text: str):
    """Real incorrect changes derived from one code chunk of the gold patch. First, one added line with a comparison or boolean
    inverted at a code position (never inside a string, docstring or comment), preferring `if`/`elif`/`while`/`return`/
    `assert`/`raise` statements; then, one added non-header statement line dropped (hunk recounted, an emptied hunk removed,
    later new-side starts shifted either way). Yields (method, diff)."""
    state = {"quote": None}
    masks = [_code_mask(line, state) for line in gold_text.splitlines(keepends=True)]
    inversions = []
    for hi, hunk in enumerate(hunks):
        for li, line, code, mask in _new_side_lines(hunk, masks):
            if not line.startswith("+") or not code:
                continue
            inverted = _invert_once(line[1:], mask)
            if inverted is not None:
                inversions.append((0 if code.startswith(_PREFERRED_INVERSION_STARTS) else 1, hi, li, inverted))
    for _prio, hi, li, inverted in sorted(inversions):
        new_hunk = list(hunks[hi])
        new_hunk[li] = "+" + inverted
        yield "fallback-inverted-comparison", head + "".join("".join(h) for h in [*hunks[:hi], new_hunk, *hunks[hi + 1:]])
    for hi, hunk in enumerate(hunks):
        m = _HUNK.match(hunk[0].rstrip("\n"))
        if not m:
            continue
        a, b, c, d, trailer = m.groups()
        b, d = int(b) if b is not None else 1, int(d) if d is not None else 1
        for li, line, code, _mask in _new_side_lines(hunk, masks):
            if not line.startswith("+") or not code or code.startswith(_HEADER_STARTS) or code.endswith(":"):
                continue
            if li + 1 < len(hunk) and hunk[li + 1].startswith("\\"):
                continue
            body = hunk[1:li] + hunk[li + 1:]
            still_changes = any(x[:1] in "+-" for x in body if not x.startswith("\\"))
            new_hunk = [f"@@ -{a},{b} +{c},{d - 1} @@{trailer}\n", *body]
            delta = -1 if still_changes else -(d - b)  # later hunks lose one line, or the whole removed hunk's net lines
            new_hunks = [*hunks[:hi], *([new_hunk] if still_changes else []), *_shift_new_starts(hunks[hi + 1:], delta)]
            if new_hunks:
                yield "fallback-dropped-line", head + "".join("".join(h) for h in new_hunks)
            break


def _apply_hunks(base: str, hunks: list[list[str]]) -> str | None:
    """Reconstruct a file using exact base offsets and context; reject incomplete or inconsistent hunks."""
    lines = base.splitlines(keepends=True)
    out: list[str] = []
    cursor = 0
    for hunk in hunks:
        match = _HUNK.match(hunk[0].rstrip("\n"))
        if match is None:
            return None
        start, count, _, new_count, _ = match.groups()
        old: list[str] = []
        new: list[str] = []
        previous = ""
        for line in hunk[1:]:
            if line.startswith("\\"):
                if previous in (" ", "-") and old: old[-1] = old[-1].rstrip("\n")
                if previous in (" ", "+") and new: new[-1] = new[-1].rstrip("\n")
                continue
            if line[:1] in (" ", "-"): old.append(line[1:])
            if line[:1] in (" ", "+"): new.append(line[1:])
            previous = line[:1]
        if len(old) != int(count or 1) or len(new) != int(new_count or 1):
            return None
        offset = int(start) - (1 if old else 0)
        if offset < cursor or lines[offset:offset + len(old)] != old:
            return None
        out.extend(lines[cursor:offset]); out.extend(new)
        cursor = offset + len(old)
    out.extend(lines[cursor:])
    return "".join(out)


def _executable_ast(text: str) -> str | None:
    """AST without location data or docstrings; syntax errors cannot establish an executable control."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                node.body = node.body[1:]
    return ast.dump(tree, include_attributes=False)


def fallback_plausible_wrong(gold_patch: str, base_files: dict[str, str | None]) -> tuple[str, str, bool | None] | None:
    """Code-generated stand-in when the model's plausible-wrong patch is unusable after the correction round (ScenarioSpec
    requires one plausible-wrong control). It must be a real incorrect change: from the first CODE chunk of the gold patch
    (`is_code_path`; changelogs, docs and non-code paths skipped) invert one added comparison/boolean at a code position, else
    drop one added statement line. Returns (diff, method, verified) — `verified` is the actual `git apply --check` result
    (None when it could not run) — or None when no such change exists, in which case the draft fails rather than carrying a
    meaningless control."""
    for chunk in (c for c in re.split(r"(?=^diff --git )", gold_patch or "", flags=re.M) if c.strip()):
        m = re.match(r"diff --git a/(\S+)", chunk)
        if not m or not is_code_path(m.group(1)) or not m.group(1).endswith(".py") or m.group(1) not in base_files:
            continue
        head, hunks = _split_hunks(chunk)
        base = base_files[m.group(1)] or ""
        gold_text = _apply_hunks(base, hunks)
        gold_ast = _executable_ast(gold_text) if gold_text is not None else None
        if gold_ast is None:
            continue
        for method, diff in _incorrect_variants(head, hunks, gold_text):
            modified = _apply_hunks(base, _split_hunks(diff)[1])
            modified_ast = _executable_ast(modified) if modified is not None else None
            if modified_ast is None or modified_ast == gold_ast:
                continue
            ok, reason = verify_diff_applies(base_files, diff)
            if ok is True or (ok is None and reason.startswith("no-git:")):
                return diff, method, ok
    return None


class DraftViolation(ValueError):
    """A model-fixable problem found while assembling the spec (empty control diff, missing alternative-correct patch, or a
    ScenarioSpec validation error such as a requirement depending on an author-only fact). Fed back to the model in the
    correction round; fatal only after that round, like every other violation."""


def _schema_violations(exc) -> list[str]:
    out = []
    for err in exc.errors():
        loc = " -> ".join(str(x) for x in err.get("loc") or ())
        out.append("schema: " + (loc + ": " if loc else "") + str(err.get("msg", "")))
    return out


def _is_gate_violation(v: str) -> bool:
    return v.startswith("no on-request fact links")


def _author(row: dict, judged: dict, model_id: str, extra_facts: list[dict] | None = None, validate=None,
            rescue_facts: list[dict] | None = None) -> tuple[dict, dict]:
    """One LLM authoring call, validated by `ledger_violations`, one correction round (its message names the FAIL_TO_PASS ids
    and the closest corpus lines for any non-verbatim excerpt), then a code-owned fallback that links on-request facts without
    tests to the FAIL_TO_PASS tests sharing content words with them (`auto_link_tests`), then fail. When all FAIL_TO_PASS ids
    belong to a single test function, `link_single_target` links every on-request fact to it before validation and the
    content-word rule never applies. `validate(d)` (the drafter's spec assembly + ScenarioSpec validation) runs once the
    ledger is clean and its messages are violations too, so a pydantic `_integrity` error reaches the model instead of
    killing the draft. Returns (draft dict, {"auto_linked": [...], "auto_linked_single_target": bool})."""
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
    single: list[str] = []
    usage: Usage | None = Usage()  # summed over every drafter call; None when a call left no usage behind
    for attempt in range(2):
        take_last_usage()
        d = chat_json(model_id, SYSTEM, messages, SCHEMA, max_tokens=8000)
        usage = _accumulate(usage)
        if not isinstance(d, dict):
            d = {}
        single = link_single_target(d, f2p)
        bad = ledger_violations(d, row, extra_facts)
        if validate:
            bad = bad + validate(d)  # both violation kinds reach the single correction round
        if not bad:
            return d, {"auto_linked": single, "auto_linked_single_target": bool(single), "usage": _usage_json(usage)}
        if attempt == 0:
            messages.append({"role": "assistant", "content": json.dumps(d)})
            messages.append({"role": "user", "content": "Correct these violations: " + json.dumps(bad, ensure_ascii=False)})
    auto_linked = list(single)
    if len(f2p_functions(f2p)) >= 2:  # the content-word rule applies only when the target is ambiguous
        for f in d.get("ledger") or []:
            if isinstance(f, dict) and f.get("discovery") == "on-request" and not (set(f.get("tests") or []) & set(f2p)):
                links = auto_link_tests(f.get("text"), row.get("test_patch") or "", f2p)
                if links:
                    f["tests"] = sorted(set(f.get("tests") or []) | set(links))
                    auto_linked.append(f.get("id"))
    info = {"auto_linked": auto_linked, "auto_linked_single_target": bool(single), "usage": _usage_json(usage)}
    if auto_linked:
        bad = ledger_violations(d, row, extra_facts)
        if not bad and validate:
            bad = validate(d)
        if not bad:
            return d, info
    # Lenient stage: facts whose text still describes test construction become author-only (recorded), the load-bearing gate
    # may be rescued by a thread-grounded requirement fact, and control patches that still do not apply are dropped/replaced.
    log: list[dict] = []
    extra = list(extra_facts or [])
    used_rescue: list[dict] = []
    bad = ledger_violations(d, row, extra, demote_unsafe=True, filter_log=log)
    if bad and rescue_facts and all(_is_gate_violation(v) for v in bad):
        used_rescue = list(rescue_facts)
        bad = ledger_violations(d, row, extra + used_rescue, demote_unsafe=True, filter_log=log)
    if not bad and validate:
        bad = validate(d, lenient=True, extra_ledger=used_rescue)
    if not bad:
        return d, {**info, "lenient": True, "fact_filter": log, "rescue_facts": [f.get("id") for f in used_rescue]}
    if all(_is_gate_violation(v) for v in bad):
        raise ValueError("no-stakeholder-safe-fact: no stakeholder-visible on-request fact links a FAIL_TO_PASS test and no thread-grounded "
                         "statement can supply one; " + "; ".join(bad))
    raise ValueError("draft failed grounding validation after two attempts: " + "; ".join(bad))


def _normalize_groups(groups: list[dict]) -> list[dict]:
    total = sum(float(g.get("weight") or 0) for g in groups)
    if total > 0:
        for g in groups:
            g["weight"] = float(g["weight"]) / total
    return groups


def draft(row: dict, judged: dict, model_id: str, out_dir: Path, pipeline_version: str = "0.1.0", require_probe: bool = True, version: int = 1,
          layout=None, workflow_id: str | None = None) -> Path:
    """Write `scenario.draft.json`, `assets/`, `judge.json` and `draft.meta.json` under `out_dir`. Model usage of the
    withholder and drafter calls is written to `draft.meta.json["usage"]`; when `layout` (a storage Layout or Workspace)
    is given it is also recorded through `costs.record_cost` (roles "withholder"/"drafter", trial None, `workflow_id`)."""
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
    fact_filter: list[dict] = []
    synthesized = tests_resolved_facts(row, judged, fact_filter)
    bad = ledger_violations({"ledger": synthesized}, row, corpus_by_fact={f["id"]: f["source_excerpt"] for f in synthesized},
                            allow_reserved=True, require_f2p_link=False)
    if bad:  # code-built from judge evidence; a failure here is a bug or an unusable evidence span, never a model correction
        raise ValueError("synthesized tests-resolved facts failed grounding: " + "; ".join(bad))
    rescue = thread_requirement_fact(row, judged)
    rescued: list[dict] = []
    from pydantic import ValidationError
    from swesim.models import ScenarioSpec

    # Everything that does not depend on the model output is computed once, before authoring.
    test_files = test_files_from_patch(row["test_patch"])
    gold_paths = patch_paths(row["patch"])
    base_files = {p: threads.base_text(row["repo"], row["base_commit"], p) for p in [*test_files, "conftest.py", "sitecustomize.py"]}
    control_diffs = tamper.all_controls(base_files)
    fixed_files = {"oracle.diff": row["patch"].encode(), "test.patch": row["test_patch"].encode()}
    for name, diff in control_diffs.items():
        if not diff:
            raise ValueError("empty tamper control")
        fixed_files[f"{name}.diff"] = diff.encode()
    is_github = row.get("source") == "github-repo"
    if is_github:
        fixed_files["Dockerfile.base"] = row["dockerfile"].encode()

    nodeid_map = row.get("nodeid_map") or {}
    ordered, order_source = definition_order(row["fail_to_pass"] + row["pass_to_pass"], row.get("test_order"), row["test_patch"])
    selected = [nodeid_map.get(t, t) for t in ordered]
    interpreter = row.get("test_python") or TEST_PYTHON
    prefix = runner_prefix(interpreter)  # identical to the probe's exact-id stages
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
    tamper_controls = [{"name": n, "asset": f"{n}.diff", "rationale": "code-generated exploit", "expected_reward": 0.0, "cls": "incorrect-exploit"}
                       for n in control_diffs]
    created_at = datetime.now(timezone.utc).isoformat()

    # Base content of the files the fix touches (bounded), for building/verifying control patches locally before the Modal sweep.
    base_cache: dict[str, str | None] = {}
    base_sources, base_skipped = fetch_base_files(row, gold_paths, base_cache)
    oracle_ok, oracle_msg = verify_diff_applies(base_sources, row["patch"])
    if oracle_ok is False:  # the merged PR must apply to its own base commit; anything else is a data error, never a correction
        raise ValueError(f"oracle patch does not apply to the base commit (git apply --check): {oracle_msg}")
    control_notes: dict = {}

    def control_asset(d: dict, key: str, lenient: bool) -> tuple[dict | None, bytes | None]:
        """Build and verify one model control (`plausible_wrong` / `alternative_correct`). Returns (record, diff bytes); the
        record has the code-owned slug, the model's label, the build method and the verification result. Strict mode raises
        DraftViolation (correction round); lenient mode drops the control (None) and records why."""
        ctrl = d.get(key) if isinstance(d.get(key), dict) else None
        if not ctrl:
            return None, None
        try:
            paths = set(ctrl.get("files") or {}) if isinstance(ctrl.get("files"), dict) else patch_paths(ctrl.get("diff") or "")
            unsafe = sorted(p for p in paths if not safe_repo_path(p))
            if unsafe:
                raise DraftViolation(f"{key} touches paths that are not repository-relative: {unsafe}")
            files, _ = fetch_base_files(row, paths, base_cache)  # unfetched paths stay out of `files` (never mistaken for absent)
            diff, method = build_control_diff(ctrl, files)
        except DraftViolation as e:
            control_notes.setdefault("control_attempts", []).append({"control": key, "phase": "final" if lenient else "correction",
                                                                    "label": ctrl.get("name"), "reason": str(e)})
            if lenient:
                control_notes.setdefault("controls_dropped", []).append({"control": key, "label": ctrl.get("name"), "reason": str(e)})
                return None, None
            raise
        if diff is None:
            return None, None
        ok, msg = verify_diff_applies(files, diff)
        if ok is False or (ok is None and msg.startswith("unfetched:")):
            control_notes.setdefault("control_attempts", []).append({"control": key, "phase": "final" if lenient else "correction",
                                                                    "label": ctrl.get("name"), "reason": msg, "method": method})
            if not lenient:
                raise DraftViolation(f"{key} patch cannot be applied to the base commit (git apply --check): {msg}. Prefer `files` with the complete "
                                     "modified text of each touched, fetched file so the diff is built for you.")
            control_notes.setdefault("controls_dropped", []).append({"control": key, "label": ctrl.get("name"), "reason": f"does not apply: {msg}", "method": method})
            return None, None
        record = {"label": ctrl.get("name"), "rationale": ctrl.get("rationale") or "", "method": method, "verified": ok}
        return record, diff.encode()

    def assemble(d: dict, withheld_parts: dict | None = None, lenient: bool = False, extra_ledger: list[dict] | None = None) -> tuple[dict, dict, list[dict]]:
        """Spec dict, asset bytes and dropped protected-path extras from a model output. DraftViolation for model-fixable
        problems (fed back in the correction round); plain ValueError for drafter errors (never corrected). Lenient mode
        (after the correction round) drops unusable model controls instead: alternative-correct is optional, plausible-wrong
        is replaced by `fallback_plausible_wrong` because ScenarioSpec requires one."""
        # Keep prior correction-round evidence; final selected controls below replace only their summary fields.
        taken = set(control_diffs)
        pw_rec, pw_bytes = control_asset(d, "plausible_wrong", lenient)
        if pw_rec is None:
            if not lenient:
                raise DraftViolation("plausible-wrong control needs a patch: give `files` with the complete modified text of each touched file")
            fb = fallback_plausible_wrong(row["patch"], base_sources)
            if fb is None:  # never write a meaningless control; re-draft or take the next candidate
                raise ValueError("no usable plausible-wrong control: the model's patch was unusable after the correction round and the gold "
                                 "patch has no code change a fallback can make incorrect")
            diff, method, verified = fb
            pw_rec, pw_bytes = {"label": method, "rationale": "code fallback: the model's plausible-wrong patch was unusable after the correction round",
                                "method": method, "verified": verified}, diff.encode()
            control_notes["controls_fallback"] = {"plausible_wrong": method}
        alt_rec, alt_bytes = control_asset(d, "alternative_correct", lenient)
        if alt_rec is None and judged.get("structural_tests"):
            raise DraftViolation("structural tests require an alternative-correct control; write one under alternative_correct (`files` with complete text)")
        pw_name = slugify_control_name(pw_rec["label"], "plausible-wrong", taken)
        alt_name = slugify_control_name(alt_rec["label"], "alternative-correct", taken) if alt_rec else None
        protected, protected_dropped = protected_paths_for(test_files, d.get("protected_paths_extra") or [], gold_paths)
        files = {**fixed_files, "plausible-wrong.diff": pw_bytes}
        if alt_rec:
            files["alternative-correct.diff"] = alt_bytes
        check_asset_paths(files)
        manifest = {name: {"path": f"assets/{name}", "sha256": _sha(data), "bytes": len(data)} for name, data in files.items()}
        control_notes["controls"] = {"plausible_wrong": {"name": pw_name, **pw_rec}, **({"alternative_correct": {"name": alt_name, **alt_rec}} if alt_rec else {})}
        controls = {"oracle": "oracle.diff",
                    "plausible_wrong": [{"name": pw_name, "asset": "plausible-wrong.diff", "rationale": pw_rec["rationale"],
                                         "expected_success": 0, "expected_reward_max": 0.5, "cls": "plausible-wrong"}],
                    "alternative_correct": [{"name": alt_name, "asset": "alternative-correct.diff", "rationale": alt_rec["rationale"],
                                             "expected_success": 1, "cls": "alternative-correct"}] if alt_rec else [],
                    "tamper": tamper_controls}
        briefs = [{"profile": "natural", "text": row["problem_statement"], "provenance": "original-issue"}]
        ledger = [*(d.get("ledger") or []), *synthesized, *(extra_ledger or [])]
        operations = list(d.get("operations") or [])
        if withheld_parts and withheld_parts["result"]["removed"]:  # zero removals is valid: natural brief only
            briefs.append({"profile": "withheld", "text": withheld_parts["result"]["withheld_brief"], "provenance": "withheld"})
            ledger += withheld_parts["facts"]
            operations += withheld_parts["operations"]
        spec = {"id": row["instance_id"].lower().replace("__", "--"), "version": int(version), "source": row.get("source", "swebench-annotated"),
                "source_ref": row["instance_id"], "created_at": created_at, "pipeline_version": pipeline_version,
                "environment": environment, "assets": manifest, "briefs": briefs, "ledger": ledger, "operations": operations,
                "persona": d.get("persona") or {}, "requirements": d.get("requirements") or [],
                "contract": {"fail_to_pass": row["fail_to_pass"], "pass_to_pass": row["pass_to_pass"], "test_assets": ["test.patch"],
                             "protected_paths": protected,
                             "runner": {"kind": "pytest-ra", "command": runner_cmd, "single_command": prefix + "{nodeid}", "test_files": test_files,
                                        "nodeid_map": nodeid_map}},
                "checks": d.get("checks") or [], "controls": controls, "groups": _normalize_groups(d.get("groups") or []),
                "difficulty": {"annotated": row["annotation"]["difficulty"], "judged": judged}, "thread": dict(thread)}
        return spec, files, protected_dropped

    def validate(d: dict, lenient: bool = False, extra_ledger: list[dict] | None = None) -> list[str]:
        """The withheld parts are code-guaranteed to satisfy `_integrity`, so validating without them is enough to catch
        everything the model can get wrong (requirement on an author-only fact, unknown test id, duplicate ids, a control
        patch that does not apply, ...). `extra_ledger` carries the thread-grounded rescue fact of the lenient stage."""
        try:
            ScenarioSpec.model_validate(assemble(d, lenient=lenient, extra_ledger=extra_ledger)[0])
        except DraftViolation as e:
            return [str(e)]
        except ValidationError as e:
            return _schema_violations(e)
        return []

    d, links = _author(row, judged, model_id, extra_facts=synthesized, validate=validate, rescue_facts=[rescue] if rescue else None)
    fact_filter.extend(links.get("fact_filter") or [])
    if links.get("rescue_facts"):
        rescued.append(rescue)
        fact_filter.append({"fact_id": rescue["id"], "source": "thread", "action": "added-rescue", "provenance": rescue["provenance"], "tests": rescue["tests"]})

    withheld = withhold(row["problem_statement"], model_id)
    issue_url = next((i["issue_url"] for i in thread.get("linked_issues") or []), thread.get("pr_url"))
    if withheld["removed"] and not issue_url:
        raise ValueError("withheld facts need source URL")
    withheld_facts, withheld_ops = [], []
    for i, item in enumerate(withheld["removed"]):
        fid = f"withheld-{i + 1:03d}"
        discovery = "repo-derivable" if item["category"] in REPO_DERIVABLE_CATEGORIES else item["discovery"]
        withheld_facts.append({"id": fid, "text": item["fact_text"], "kind": item["kind"], "provenance": "original-issue",
                               "source_excerpt": item["excerpt"], "source_url": issue_url, "discovery": discovery, "tests": []})
        # Operation is frozen at {type, target, rationale}: the exact span and its offsets in the natural text ride in the rationale
        # (machine-readable copy in draft.meta.json under "withholding").
        offsets = ", ".join(f"natural[{o['start']}:{o['end']}]" for o in item["occurrences"])
        withheld_ops.append({"type": "withholding", "target": fid, "rationale": f"Removed category: {item['category']}; {offsets} = {item['excerpt']!r}"})
    # The withheld facts go through the same grounding gate as every other fact, with their own removed span as corpus. Their
    # text is the reporter's own statement, so the identifier check passes by construction; it is run and recorded anyway.
    bad = ledger_violations({"ledger": withheld_facts}, row, corpus_by_fact={f["id"]: f["source_excerpt"] for f in withheld_facts},
                            allow_reserved=True, require_f2p_link=False)
    if bad:
        raise ValueError("withheld facts failed grounding: " + "; ".join(bad))
    for f in withheld_facts:
        fact_filter.append({"fact_id": f["id"], "source": "withheld", "action": "kept", "reasons": unsafe_fact_text(f["text"], row)})

    spec, files, protected_dropped = assemble(d, {"result": withheld, "facts": withheld_facts, "operations": withheld_ops}, lenient=True, extra_ledger=rescued)
    parsed = ScenarioSpec.model_validate(spec)  # the model's part already validated; a failure here is a drafter error
    usage = {"withholder": withheld.get("usage"), "drafter": links.get("usage"), "recorded": False, "workflow_id": workflow_id}
    if layout is not None and workflow_id:  # a cost without a trial is filed under the workflow; without one there is nowhere to file it
        for role in ("withholder", "drafter"):
            if usage[role] is not None:
                costs.record_cost(layout, None, workflow_id, role, model_id, Usage.model_validate(usage[role]))
                usage["recorded"] = True
    for name, data in files.items():
        (assets / name).write_bytes(data)
    p = out_dir / "scenario.draft.json"
    p.write_text(parsed.model_dump_json(indent=2))
    atomic_json(out_dir / "judge.json", {"gaps": judged.get("gaps", []), "verified_rubric_score": judged.get("verified_rubric_score"),
                                         "keep": judged.get("keep"), **judged})
    atomic_json(out_dir / "draft.meta.json", {"model_id": model_id, "prompt_version": DRAFT_PROMPT_VERSION, "pipeline_version": pipeline_version,
                                              "version": int(version), "created_at": spec["created_at"], "spec_hash": parsed.spec_hash,
                                              "auto_linked": links["auto_linked"], "auto_linked_single_target": links["auto_linked_single_target"],
                                              "fact_filter": fact_filter, "rescue_facts": links.get("rescue_facts", []),
                                              "protected_paths_dropped": protected_dropped, "usage": usage,
                                              "controls": control_notes.get("controls", {}), "controls_dropped": control_notes.get("controls_dropped", []),
                                              "control_attempts": control_notes.get("control_attempts", []),
                                              "controls_fallback": control_notes.get("controls_fallback", {}),
                                              "oracle_apply": {"verified": oracle_ok, "note": oracle_msg, "base_files_fetched": sorted(base_sources), "base_files_skipped": base_skipped},
                                              "runner": {"order": "definition", "order_source": order_source, "ids": selected},
                                              "probe": ({k: probe.get(k) for k in ("kind", "probe_dir", "image_digest", "counts", "dropped", "swebench", "runner")} if probe.get("statuses")
                                                        else {"unmeasured": True}),
                                              "withholding": {"removed": len(withheld["removed"]), "reason": withheld["stats"].get("reason"),
                                                              "spans": [{**r, "fact_id": f"withheld-{i + 1:03d}"} for i, r in enumerate(withheld["removed"])],
                                                              "kept": withheld["kept"], "stats": withheld["stats"]}})
    return p
