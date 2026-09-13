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
from swesim.sources.swebench_annotated import TEST_PYTHON, test_files_from_patch
from swesim.stages.judge import _BLOCKQUOTE, llm_view, norm_text

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
Constraints the record must satisfy: at least one on-request fact links a FAIL_TO_PASS test id (the decision the agent has to
ask about); fact, requirement, check and group ids match ^[a-z0-9][a-z0-9._-]{2,63}$ and never start with "withheld-" or
"tests-" (code-generated facts use those); test links use only the FAIL_TO_PASS and PASS_TO_PASS ids given; check ids differ
from test ids. Every fact's source_excerpt is copied verbatim (no paraphrase) from the text its provenance names: issue-thread
= linked issue title/body/comments or hints_text, pr-thread = PR title/body/comments, pr-review = review bodies and review
comments, annotator-note = the annotation notes, original-issue = the problem statement; its source_url is a URL under the
repository. provenance=authored is only for decisions that nothing in the discussion states and only the merged tests or
patch pin: source_excerpt verbatim from test_patch or patch, source_url the PR URL, authored_rationale saying why no thread
statement exists, discovery on-request, tests = the FAIL_TO_PASS ids whose code the excerpt comes from. Code also adds such
facts from the judge's tests-resolved gaps. If the judge marked structural_tests, also write one alternative-correct patch (a
different implementation the tests should still accept); otherwise set alternative_correct to null."""
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
"protected_paths_extra": ["paths the fix's tests depend on"]}"""

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
                   "code deletes the sentence or line containing each selected span and keeps everything else verbatim. Each excerpt must be a "
                   "verbatim contiguous span copied from the issue. Never select the first line (the title); keep a recognizable symptom and a "
                   "desired outcome; select at most about half of the sentences. File locations and explicit fix proposals are repo-derivable; "
                   "expected behaviour, error text, versions and reproductions the maintainer can supply are on-request.")
_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*(?=\s|$)")


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
        starts = [0] + [m.end() for m in _SENTENCE_END.finditer(content)]
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


def _tidy(text: str, title: str) -> str:
    """Join of the kept pieces with normalized whitespace: runs collapsed, blank lines dropped (a deleted line leaves no
    hole), the title line verbatim."""
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.strip():
            lines.append(title if title and raw.strip() == title else " ".join(raw.split()))
    return "\n".join(lines)


def withhold(natural_issue: str, model_id: str) -> dict:
    """Category-targeted withholding where the model only SELECTS spans and code performs the deletion.

    Each selected excerpt must match the natural issue verbatim (after `_norm`); it is extended to whole sentence/line
    boundaries and every occurrence is deleted. The withheld brief is exactly the natural issue minus the deleted spans,
    joined with normalized whitespace, so no text can be added and nothing undeclared can disappear. The title line is
    never deletable, at most `MAX_REMOVED_FRACTION` of the sentence segments may go, at least one removal is required,
    and every removed span must be absent from the brief. Violations get one correction round, then ValueError.

    Returns {"withheld_brief", "removed": [{category, kind, discovery, fact_text, selected, excerpt, start, end,
    occurrences}], "kept": [(start, end)], "stats": {...}}; `natural[start:end] == excerpt` for every removal."""
    messages = [{"role": "user", "content": natural_issue}]
    title_a, title_b = _title_range(natural_issue)
    title = natural_issue[title_a:title_b]
    segs = _segments(natural_issue)
    bad: list[str] = []
    for _attempt in range(2):
        out = chat_json(model_id, WITHHOLD_PROMPT, messages, WITHHOLD_SCHEMA, max_tokens=5000)
        bad, removed, ranges = [], [], []
        items = out.get("removed") or []
        if not items:
            bad.append("no removed spans")
        for item in items:
            selected = item.get("excerpt") or ""
            if item.get("category") not in WITHHOLD_CATEGORIES:
                bad.append(f"invalid category {item.get('category')!r}")
            if item.get("kind") not in WITHHOLD_KINDS:
                bad.append(f"invalid kind {item.get('kind')!r}")
            if item.get("discovery") not in WITHHOLD_DISCOVERY:
                bad.append(f"invalid discovery {item.get('discovery')!r}; use on-request or repo-derivable")
            spans = _find_spans(natural_issue, selected)
            if not spans:
                bad.append(f"excerpt is not a verbatim span of the issue: {selected!r}")
                continue
            extended = [_extend(s, segs) for s in spans]
            if any(a < title_b and b > title_a for a, b in extended):
                bad.append(f"the title line cannot be withheld: {selected!r}")
                continue
            ranges.extend(extended)
            a, b = extended[0]
            removed.append({"category": item.get("category"), "kind": item.get("kind"), "discovery": item.get("discovery"),
                            "fact_text": (item.get("fact_text") or "").strip() or natural_issue[a:b], "selected": selected,
                            "excerpt": natural_issue[a:b], "start": a, "end": b, "occurrences": [{"start": x, "end": y} for x, y in extended]})
        merged = _merge(ranges)
        removed_segs = sum(1 for s in segs if any(a <= s[0] and s[1] <= b for a, b in merged))
        if segs and removed_segs / len(segs) > MAX_REMOVED_FRACTION:
            bad.append(f"removed {removed_segs} of {len(segs)} sentences; the cap is {int(MAX_REMOVED_FRACTION * 100)}%")
        kept, pos = [], 0
        for a, b in merged:
            if a > pos:
                kept.append((pos, a))
            pos = b
        if pos < len(natural_issue):
            kept.append((pos, len(natural_issue)))
        brief = _tidy("".join(natural_issue[a:b] for a, b in kept), title)
        if title and title not in brief:
            bad.append("missing title")
        for r in removed:
            if r["excerpt"].casefold() in brief.casefold():
                bad.append(f"removed span still visible in the brief: {r['excerpt']!r}")
        if not bad:
            return {"withheld_brief": brief, "removed": removed, "kept": kept,
                    "stats": {"sentences_total": len(segs), "sentences_removed": removed_segs,
                              "fraction_removed": round(removed_segs / max(1, len(segs)), 3), "prompt_version": WITHHOLD_PROMPT_VERSION}}
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
            "authored": _norm("\n".join([row.get("test_patch") or "", row.get("patch") or ""]))}


def ledger_violations(d: dict, row: dict, extra_facts: list[dict] | None = None) -> list[str]:
    """Code-owned grounding of LLM-authored facts. Every fact must quote a verbatim span (after normalization) of the text
    its provenance names (`authored` = the merged tests/patch) and cite a URL under the repository; and at least one
    on-request fact (in the model ledger or in `extra_facts`, the code-synthesized ones) must link a FAIL_TO_PASS test,
    otherwise interaction is not load-bearing and approval would fail anyway."""
    corpora = _corpora(row)
    url_prefix = f"https://github.com/{row['repo']}/".casefold()
    bad = []
    ledger = d.get("ledger") or []
    for f in ledger:
        fid, prov = f.get("id", "?"), f.get("provenance")
        if str(fid).startswith(RESERVED_FACT_PREFIXES):
            bad.append(f"{fid}: fact ids starting with {RESERVED_FACT_PREFIXES} are reserved for code-generated facts")
        if prov not in corpora:
            bad.append(f"{fid}: unknown provenance {prov!r}")
            continue
        excerpt = _norm(f.get("source_excerpt"))
        where = "merged tests/patch" if prov == "authored" else f"{prov} text"
        if len(excerpt) < MIN_EXCERPT_CHARS or len(excerpt.split()) < MIN_EXCERPT_TOKENS:
            bad.append(f"{fid}: source_excerpt too short to ground a fact (need >= {MIN_EXCERPT_CHARS} chars and >= {MIN_EXCERPT_TOKENS} words): {excerpt!r}")
        elif excerpt not in corpora[prov]:
            bad.append(f"{fid}: source_excerpt is not a verbatim span of the {where}: {excerpt[:80]!r}")
        if not (f.get("source_url") or "").casefold().startswith(url_prefix):
            bad.append(f"{fid}: source_url must be under https://github.com/{row['repo']}/")
    f2p = set(row.get("fail_to_pass") or [])
    if not any(f.get("discovery") == "on-request" and set(f.get("tests") or []) & f2p for f in [*ledger, *(extra_facts or [])]):
        bad.append("no on-request fact links a FAIL_TO_PASS test id (interaction would not be load-bearing)")
    return bad


def tests_for_excerpt(test_patch: str, excerpt: str, fail_to_pass: list[str]) -> list[str]:
    """FAIL_TO_PASS ids whose test function (in the unified diff of the hidden tests) contains `excerpt`. Falls back to the
    F2P tests of the containing file when the excerpt sits at module level, and to every F2P test when the excerpt is not in
    the test patch at all (evidence from the source patch)."""
    needle = _norm(excerpt)
    blocks: list[tuple[str | None, str | None, list[str]]] = []
    current_file = None
    for raw in (test_patch or "").splitlines():
        m = re.match(r"diff --git a/(\S+)", raw)
        if m:
            current_file = m.group(1)
            blocks.append((current_file, None, []))
            continue
        line = raw[1:] if raw[:1] in "+- " else raw
        d = _TEST_DEF.match(line)
        if d:
            blocks.append((current_file, d.group(1), []))
        if blocks:
            blocks[-1][2].append(line)
    hits = [(f, fn) for f, fn, lines in blocks if needle and needle in _norm("\n".join(lines))]
    if not hits:
        return sorted(fail_to_pass)
    out = set()
    for f, fn in hits:
        for t in fail_to_pass:
            path, *rest = t.split("::")
            func = re.split(r"[\[(]", rest[-1])[0] if rest else ""
            if path == f and (fn is None or func == fn):
                out.add(t)
    return sorted(out) or sorted(fail_to_pass)


def tests_resolved_facts(row: dict, judged: dict) -> list[dict]:
    """Code-synthesized ledger facts for judge gaps that only the merged tests/patch resolve: provenance `authored`,
    source_url = the PR, source_excerpt = the verbatim evidence (re-checked here), discovery on-request, linked to the
    FAIL_TO_PASS tests whose code the excerpt comes from. Ids `tests-NNN`."""
    corpus = _corpora(row)["authored"]
    thread = row.get("thread") or {}
    pr_url = thread.get("pr_url") or f"https://github.com/{row['repo']}/pull/{str(row.get('instance_id', '')).rsplit('-', 1)[-1]}"
    kinds = {"behavior", "interface", "constraint", "reproduction", "context"}
    out, seen = [], set()
    for g in judged.get("gaps") or []:
        if not (g.get("resolved_by") == "tests" and g.get("derivable_from_repo") is False and g.get("consequential") is True):
            continue
        evidence = (g.get("evidence") or "").strip()
        key = _norm(evidence)
        if len(key) < MIN_EXCERPT_CHARS or key not in corpus or key in seen:
            continue
        seen.add(key)
        question = (g.get("question") or "").strip()
        out.append({"id": f"tests-{len(out) + 1:03d}", "text": (g.get("text") or question or evidence).strip(), "kind": g.get("category") if g.get("category") in kinds else "behavior",
                    "provenance": "authored", "source_excerpt": evidence, "source_url": pr_url,
                    "authored_rationale": "Pinned only by the merged tests/patch; no issue or thread statement resolves it (judge gap "
                                          f"{g.get('id', '?')}, resolved_by=tests)." + (f" Question a developer would ask: {question}" if question else ""),
                    "discovery": "on-request", "tests": tests_for_excerpt(row.get("test_patch") or "", evidence, list(row.get("fail_to_pass") or []))})
    return out


def _author(row: dict, judged: dict, model_id: str, extra_facts: list[dict] | None = None) -> dict:
    """One LLM authoring call, validated by `ledger_violations`, one correction round, then fail."""
    user = json.dumps({"candidate": llm_view(row), "judge": judged}, ensure_ascii=False)
    if len(user) > CONTEXT_BUDGET_CHARS:
        raise ValueError("candidate exceeds authoring context budget")
    messages = [{"role": "user", "content": user}]
    bad: list[str] = []
    for _attempt in range(2):
        d = chat_json(model_id, SYSTEM, messages, SCHEMA, max_tokens=8000)
        bad = ledger_violations(d, row, extra_facts)
        if not bad:
            return d
        messages.append({"role": "assistant", "content": json.dumps(d)})
        messages.append({"role": "user", "content": "Correct these violations: " + json.dumps(bad)})
    raise ValueError("draft failed grounding validation after two attempts: " + "; ".join(bad))


def _normalize_groups(groups: list[dict]) -> list[dict]:
    total = sum(float(g.get("weight") or 0) for g in groups)
    if total > 0:
        for g in groups:
            g["weight"] = float(g["weight"]) / total
    return groups


def draft(row: dict, judged: dict, model_id: str, out_dir: Path, pipeline_version: str = "0.1.0") -> Path:
    if judged.get("model_id") and judged["model_id"] == model_id:
        raise ValueError(f"judge and draft models must differ so their errors are not correlated (both {model_id})")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    assets = out_dir / "assets"
    assets.mkdir(exist_ok=True)
    synthesized = tests_resolved_facts(row, judged)
    d = _author(row, judged, model_id, extra_facts=synthesized)

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
                         "protected_paths": sorted(set(test_files) | set(d.get("protected_paths_extra") or [])),
                         "runner": {"kind": "pytest-ra", "command": runner_cmd, "single_command": prefix + "{nodeid}", "test_files": test_files,
                                    "nodeid_map": nodeid_map}},
            "checks": d.get("checks") or [], "controls": controls, "groups": _normalize_groups(d["groups"]),
            "difficulty": {"annotated": row["annotation"]["difficulty"], "judged": judged}, "thread": dict(thread)}

    withheld = withhold(row["problem_statement"], model_id)
    spec["briefs"].append({"profile": "withheld", "text": withheld["withheld_brief"], "provenance": "withheld"})
    issue_url = next((i["issue_url"] for i in thread.get("linked_issues") or []), thread.get("pr_url"))
    if not issue_url:
        raise ValueError("withheld facts need source URL")
    for i, item in enumerate(withheld["removed"]):
        fid = f"withheld-{i + 1:03d}"
        if any(f.get("id") == fid for f in spec["ledger"]):
            raise ValueError("reserved withholding fact id")
        discovery = "repo-derivable" if item["category"] in REPO_DERIVABLE_CATEGORIES else item["discovery"]
        spec["ledger"].append({"id": fid, "text": item["fact_text"], "kind": item["kind"], "provenance": "original-issue",
                               "source_excerpt": item["excerpt"], "source_url": issue_url, "discovery": discovery, "tests": []})
        # Operation is frozen at {type, target, rationale}: the exact span and its offsets in the natural text ride in the rationale
        # (machine-readable copy in draft.meta.json under "withholding").
        offsets = ", ".join(f"natural[{o['start']}:{o['end']}]" for o in item["occurrences"])
        spec["operations"].append({"type": "withholding", "target": fid,
                                   "rationale": f"Removed category: {item['category']}; {offsets} = {item['excerpt']!r}"})

    from swesim.models import ScenarioSpec
    parsed = ScenarioSpec.model_validate(spec)
    p = out_dir / "scenario.draft.json"
    p.write_text(parsed.model_dump_json(indent=2))
    atomic_json(out_dir / "judge.json", {"gaps": judged.get("gaps", []), "verified_rubric_score": judged.get("verified_rubric_score"),
                                         "keep": judged.get("keep"), **judged})
    atomic_json(out_dir / "draft.meta.json", {"model_id": model_id, "prompt_version": DRAFT_PROMPT_VERSION, "pipeline_version": pipeline_version,
                                              "created_at": spec["created_at"], "spec_hash": parsed.spec_hash,
                                              "withholding": {"removed": [{**r, "fact_id": f"withheld-{i + 1:03d}"} for i, r in enumerate(withheld["removed"])],
                                                              "kept": withheld["kept"], "stats": withheld["stats"]}})
    return p
