"""Judge stage: code-extracted candidate gaps, model-proposed gaps grounded in verbatim test/patch evidence, an LLM
classification of every gap plus the SWE-bench Verified underspecification rubric, and the code-owned acceptance rule
`accept()`.

The judge model is deliberately different from the draft model so their errors are not correlated. The judge never sees
the annotators' notes, so `calibrate()` measures agreement with an independent label.
"""
from __future__ import annotations

import csv
import json
import random
import re
from pathlib import Path

from swesim.llm import chat_json

JUDGE_PROMPT_VERSION = "judge-v3"
SYSTEM = """You review a GitHub issue, its discussion, the merged fix, and the fix's tests to decide whether it makes a good
interactive software-engineering task in which a simulated maintainer can answer an agent's questions.
You are given CANDIDATE GAPS extracted by code: things the tests or the fix require that the issue text never mentions.
Classify each of them. Then PROPOSE additional gaps by reading the problem statement against test_patch and patch: decisions
the merged tests pin that the issue text does not state (behaviour on edge input, exception type or message, API shape,
defaults, scope). Every gap carries `evidence`, a verbatim excerpt copied from test_patch or patch (code drops any gap whose
evidence is not verbatim), and `question`, what a developer would have to ask to settle it. `resolved_by` is "thread" only
when the issue or PR discussion states the resolution (copy that statement verbatim into resolution_excerpt), "tests" when
only the merged tests or patch pin it (resolution_excerpt = the test excerpt), otherwise "none". derivable_from_repo means an
agent could settle the question by reading the repository at the base commit. Never judge the issue as a whole. Also score
the issue's underspecification 0-3 with the SWE-bench Verified annotator rubric (0: well specified; 1: minor blanks a
reasonable solver fills; 2: multiple valid solutions, the tests accept one; 3: nearly impossible without more information).
Be strict and concrete."""
SCHEMA = """{"gaps": [{"id": "g1", "text": "...", "source": "test-literal|test-exception|patch-public-name|proposed",
  "evidence": "verbatim excerpt of test_patch or patch that pins this decision", "question": "what a developer would have to ask",
  "derivable_from_repo": true|false, "consequential": true|false,
  "resolved_by": "thread|tests|none", "resolution_excerpt": "verbatim thread statement when resolved_by=thread, else the test excerpt",
  "category": "behavior|interface|constraint|reproduction|context"}],
"verified_rubric_score": 0|1|2|3, "complexity": 1-5, "test_behavioralness": 1-5, "self_contained": true|false,
"alternatives_accepted": true|false, "rationale": "2-4 sentences"}"""
CONTEXT_BUDGET_CHARS = 120000
P2P_SAMPLE = 40
EXTRACTED_SOURCES = {"test-exception", "test-literal", "patch-public-name"}
ACCEPTED_RESOLUTIONS = {"thread", "tests"}
MIN_EVIDENCE_CHARS = 12  # after norm_text; a one-word "evidence" pins nothing
_BLOCKQUOTE = re.compile(r"(?m)^[ \t]*(?:>[ \t]?)+")

_EXC = re.compile(r"pytest\.raises\(\s*([A-Za-z_][\w.]*)")
_LIT = re.compile(r"(?:assert[^\n]*?|match=)\s*[\"']([^\"'\n]{6,80})[\"']")
_DEF = re.compile(r"^\+\s*(?:def|class)\s+([A-Za-z_]\w*)", re.M)


def norm_text(text: str | None) -> str:
    """Grounding normalization shared by judge and drafter: drop line-leading markdown blockquote markers, collapse
    whitespace, casefold. Applied identically to corpus and excerpt."""
    return " ".join(_BLOCKQUOTE.sub("", text or "").split()).casefold()


# Trim ladders for fitting a candidate into the model context: each field is cut to the next limit only when the payload is
# still over budget, largest current contributor first. FAIL_TO_PASS ids are never trimmed; the heads of the issue text and
# test patch are the essentials, so exhausting every ladder means the candidate genuinely does not fit.
TRIM_LADDER = {"problem_statement": [20000, 6000], "hints_text": [20000, 4000], "patch": [30000, 8000], "test_patch": [40000, 15000],
               "pass_to_pass": [10, 0], "thread": [20, 5, 0]}


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} characters trimmed for the model; the candidate row keeps the full text]"


def _trim_thread(thread: dict, n: int) -> dict:
    """Keep the first `n` comments of every comment list in the thread, recording the totals."""
    t = dict(thread)
    for k in ("pr_comments", "review_comments", "reviews"):
        if isinstance(t.get(k), list) and len(t[k]) > n:
            t[k + "_total"] = len(t[k])
            t[k] = t[k][:n]
    if isinstance(t.get("linked_issues"), list):
        issues = []
        for i in t["linked_issues"]:
            i = dict(i)
            if isinstance(i.get("comments"), list) and len(i["comments"]) > n:
                i["comments_total"] = len(i["comments"])
                i["comments"] = i["comments"][:n]
            issues.append(i)
        t["linked_issues"] = issues
    return t


def _apply_trim(view: dict, row: dict, field: str, limit: int) -> None:
    if field == "pass_to_pass":
        p2p = row.get("pass_to_pass") or []
        view.update(pass_to_pass=p2p[:limit], pass_to_pass_count=len(p2p), pass_to_pass_truncated=True)
    elif field == "thread":
        view["thread"] = _trim_thread(row.get("thread") or {}, limit)
    else:
        view[field] = _truncate(row.get(field) or "", limit)


def llm_view(row: dict, budget: int | None = None, extra_chars: int = 0) -> dict:
    """The candidate as shown to the judge/draft model. PASS_TO_PASS ids are regression bookkeeping, not evidence, so only a
    count and a head sample are sent (some instances carry 2,000+ ids). With `budget`, the JSON size of the view plus
    `extra_chars` must fit: the largest trimmable field is cut one ladder notch at a time (`TRIM_LADDER`), the cuts are
    recorded under `trimmed`, and ValueError is raised only when every ladder is exhausted and the essentials (issue text
    head, FAIL_TO_PASS ids, test patch head) still do not fit. The spec itself always keeps the full row."""
    view = dict(row)
    p2p = row.get("pass_to_pass") or []
    if len(p2p) > P2P_SAMPLE:
        view.update(pass_to_pass=p2p[:P2P_SAMPLE], pass_to_pass_count=len(p2p), pass_to_pass_truncated=True)
    if budget is None:
        return view

    def size() -> int:
        return len(json.dumps(view, ensure_ascii=False)) + extra_chars

    notch = {f: 0 for f in TRIM_LADDER}
    trimmed: dict[str, int] = {}
    while size() > budget:
        options = [(len(json.dumps(view.get(f), ensure_ascii=False)), f) for f in TRIM_LADDER if notch[f] < len(TRIM_LADDER[f]) and view.get(f)]
        if not options:
            raise ValueError(f"candidate exceeds authoring context budget even after trimming ({size()} > {budget} chars; "
                             "FAIL_TO_PASS ids, issue head and test patch head alone do not fit)")
        _, field = max(options)
        limit = TRIM_LADDER[field][notch[field]]
        notch[field] += 1
        _apply_trim(view, row, field, limit)
        trimmed[field] = limit
        view["trimmed"] = trimmed
    return view


def issue_contract_delta(row: dict) -> list[dict]:
    """Code-extracted candidate gaps: items the tests or the fix require that the issue never mentions."""
    issue = (row.get("problem_statement") or "").lower()
    gaps = []
    for name in sorted(set(_EXC.findall(row.get("test_patch") or ""))):
        if name.lower() not in issue:
            gaps.append({"source": "test-exception", "text": f"tests expect {name}"})
    for lit in sorted(set(_LIT.findall(row.get("test_patch") or ""))):
        if lit.lower() not in issue:
            gaps.append({"source": "test-literal", "text": f"tests assert the literal {lit!r}"})
    for name in sorted(set(_DEF.findall(row.get("patch") or ""))):
        if not name.startswith("_") and name.lower() not in issue:
            gaps.append({"source": "patch-public-name", "text": f"the fix introduces public name {name}"})
    return gaps[:25]


def accept(judged: dict) -> bool:
    """Code-owned acceptance rule: the task is self-contained and at least one gap is explicitly non-derivable
    (`derivable_from_repo is False`; a missing field never counts), consequential, and resolved either by a stakeholder
    statement in the thread or by the merged tests/patch."""
    return judged.get("self_contained") is True and any(
        g.get("derivable_from_repo") is False and g.get("consequential") is True and g.get("resolved_by") in ACCEPTED_RESOLUTIONS
        for g in judged.get("gaps") or [])


def thread_text(row: dict) -> str:
    """Everything a maintainer or reporter actually said: hints, linked issues (title, body, comments), PR title/body/comments,
    reviews and review comments. Never the annotators' notes."""
    t = row.get("thread") or {}

    def bodies(items):
        return [x.get("body") or "" for x in items or []]

    parts = [row.get("hints_text") or "", t.get("pr_title") or "", t.get("pr_body") or "", *bodies(t.get("pr_comments")),
             *bodies(t.get("reviews")), *bodies(t.get("review_comments"))]
    for i in t.get("linked_issues") or []:
        parts += [i.get("title") or "", i.get("body") or "", *bodies(i.get("comments"))]
    return "\n".join(parts)


DIFF_HEADER_PREFIXES = ("diff ", "index ", "--- ", "+++ ", "@@", "\\ No newline", "new file mode", "deleted file mode", "old mode", "new mode",
                        "similarity index", "dissimilarity index", "rename from", "rename to", "copy from", "copy to", "Binary files")


def diff_body_lines(patch: str | None, include_removed: bool = False) -> list[str]:
    """Hunk body lines of a unified diff with the leading marker stripped: added and context lines (removed lines only on
    request); diff/index/---/+++/@@ headers and `\\ No newline` markers dropped. Text that is not a diff line passes through."""
    out = []
    for raw in (patch or "").splitlines():
        if raw.startswith(DIFF_HEADER_PREFIXES):
            continue
        head = raw[:1]
        if head in ("+", " "):
            out.append(raw[1:])
        elif head == "-":
            if include_removed:
                out.append(raw[1:])
        else:
            out.append(raw)
    return out


def diff_body_text(patch: str | None) -> str:
    return "\n".join(diff_body_lines(patch))


def evidence_text(row: dict) -> str:
    """The merged tests and fix as they read after the fix (diff markers and headers removed): the only admissible evidence
    for a proposed gap. Adjacent added lines therefore normalize to adjacent sentences."""
    return diff_body_text(row.get("test_patch")) + "\n" + diff_body_text(row.get("patch"))


def ground_gaps(gaps: list, row: dict) -> tuple[list[dict], dict]:
    """Code-owned grounding of the model's gap list.

    Code-extracted gaps (source in EXTRACTED_SOURCES) are kept as classified. Every other gap is a proposal and survives only
    if its `evidence` is a verbatim span (after norm_text, >= MIN_EVIDENCE_CHARS) of test_patch or patch. Gaps are deduplicated
    by evidence. `resolved_by="thread"` needs a verbatim `resolution_excerpt` from the thread, else it is downgraded to
    "tests" (evidence grounded) or "none"; `resolved_by="tests"` needs grounded evidence, else "none".
    Returns (gaps, {"gaps_proposed", "gaps_dropped", "resolution_downgraded"})."""
    evidence = norm_text(evidence_text(row))
    thread = norm_text(thread_text(row))
    kept, seen = [], set()
    stats = {"gaps_proposed": 0, "gaps_dropped": 0, "resolution_downgraded": 0}
    for g in gaps or []:
        if not isinstance(g, dict):
            stats["gaps_dropped"] += 1
            continue
        proposed = g.get("source") not in EXTRACTED_SOURCES
        ev = norm_text(g.get("evidence"))
        ev_ok = len(ev) >= MIN_EVIDENCE_CHARS and ev in evidence
        if proposed and not ev_ok:
            stats["gaps_dropped"] += 1
            continue
        key = ev if ev_ok else norm_text(g.get("text"))
        if key in seen:
            stats["gaps_dropped"] += 1
            continue
        seen.add(key)
        rb, rx = g.get("resolved_by"), norm_text(g.get("resolution_excerpt"))
        rx_in_tests = len(rx) >= MIN_EVIDENCE_CHARS and rx in evidence
        if rb == "thread" and not (len(rx) >= MIN_EVIDENCE_CHARS and rx in thread):
            rb = "tests" if (ev_ok or rx_in_tests) else "none"
            stats["resolution_downgraded"] += 1
        elif rb == "tests" and not (ev_ok or rx_in_tests):
            rb = "none"
            stats["resolution_downgraded"] += 1
        elif rb not in ACCEPTED_RESOLUTIONS:
            rb = "none"
        if proposed:
            stats["gaps_proposed"] += 1
        kept.append({**g, "source": g.get("source") if not proposed else "proposed", "resolved_by": rb, "evidence_grounded": ev_ok})
    return kept, stats


def judge_candidate(row: dict, model_id: str) -> dict:
    thread = row.get("thread") or {}
    if thread.get("fetch_errors"):
        raise ValueError("incomplete source evidence")
    candidate_gaps = issue_contract_delta(row)
    gaps_json = json.dumps(candidate_gaps, ensure_ascii=False)
    base = {k: v for k, v in row.items() if k != "annotation"}  # calibration independence: no annotator notes
    view = llm_view(base, budget=CONTEXT_BUDGET_CHARS, extra_chars=len(gaps_json) + len('{"candidate": , "candidate_gaps": }'))
    payload = {"candidate": view, "candidate_gaps": candidate_gaps}
    user = json.dumps(payload, ensure_ascii=False)
    if len(user) > CONTEXT_BUDGET_CHARS:
        raise ValueError("candidate exceeds authoring context budget")
    out = chat_json(model_id, SYSTEM, [{"role": "user", "content": user}], SCHEMA)
    out["payload_chars"] = len(user)
    out["payload_trimmed"] = view.get("trimmed", {})
    gaps, stats = ground_gaps(out.get("gaps") if isinstance(out.get("gaps"), list) else [], row)
    out["gaps"] = gaps
    out.update(stats)
    out["gaps_extracted"] = len(candidate_gaps)
    try:
        out["verified_rubric_score"] = int(out.get("verified_rubric_score", 0))
    except (TypeError, ValueError):
        out["verified_rubric_score"] = 0
    out["candidate_gaps"] = candidate_gaps
    out["keep"] = accept(out)
    out["structural_tests"] = not bool(out.get("alternatives_accepted"))
    out["prompt_version"] = JUDGE_PROMPT_VERSION
    out["model_id"] = model_id
    return out


def calibrate(csv_path: Path, rows_by_id: dict[str, dict], model_id: str, n: int, seed: int = 0) -> dict:
    """Agreement of the judge's verified_rubric_score with the human ensembled labels at the >=2 threshold.

    Resilient per row: a failure while preparing or judging one instance is recorded under `errors` and excluded from every
    denominator instead of aborting the run. `n` is the number of judged rows; `n_requested` / `n_judged` / `n_errored` make
    the coverage explicit."""
    with open(csv_path, newline="") as f:
        ann = {r["instance_id"]: r for r in csv.DictReader(f)}
    ids = [i for i in ann if i in rows_by_id]
    random.Random(seed).shuffle(ids)
    ids = ids[:n]
    tp = fp = tn = fn = 0
    per, errors = [], []
    for iid in ids:
        try:
            human_score = float(ann[iid]["underspecified"])
            judged = judge_candidate(rows_by_id[iid], model_id)
            score = int(judged.get("verified_rubric_score", 0))
        except Exception as e:  # noqa: BLE001 - one bad row must not lose the whole calibration
            errors.append({"instance_id": iid, "error": f"{type(e).__name__}: {e}"})
            continue
        human, model = human_score >= 2, score >= 2
        per.append({"instance_id": iid, "human": human_score, "model": score})
        tp += human and model
        fp += (not human) and model
        tn += (not human) and (not model)
        fn += human and (not model)
    total = max(1, tp + fp + tn + fn)
    return {"n": len(per), "n_requested": len(ids), "n_judged": len(per), "n_errored": len(errors),
            "agreement": (tp + tn) / total, "precision": tp / max(1, tp + fp), "recall": tp / max(1, tp + fn),
            "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn}, "model_id": model_id, "prompt_version": JUDGE_PROMPT_VERSION,
            "per_instance": per, "errors": errors}
