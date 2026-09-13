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


def llm_view(row: dict) -> dict:
    """The candidate as shown to the judge/draft model: PASS_TO_PASS ids are regression bookkeeping, not evidence, so only a
    count and a head sample are sent (some instances carry 2,000+ ids). The spec itself always keeps the full list."""
    p2p = row.get("pass_to_pass") or []
    if len(p2p) <= P2P_SAMPLE:
        return row
    return {**row, "pass_to_pass": p2p[:P2P_SAMPLE], "pass_to_pass_count": len(p2p), "pass_to_pass_truncated": True}


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


def evidence_text(row: dict) -> str:
    """The merged tests and fix: the only admissible evidence for a proposed gap."""
    return (row.get("test_patch") or "") + "\n" + (row.get("patch") or "")


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
    view = {k: v for k, v in llm_view(row).items() if k != "annotation"}  # calibration independence: no annotator notes
    payload = {"candidate": view, "candidate_gaps": candidate_gaps}
    user = json.dumps(payload, ensure_ascii=False)
    if len(user) > CONTEXT_BUDGET_CHARS:
        raise ValueError("candidate exceeds authoring context budget")
    out = chat_json(model_id, SYSTEM, [{"role": "user", "content": user}], SCHEMA)
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
    """Agreement of the judge's verified_rubric_score with the human ensembled labels at the >=2 threshold."""
    with open(csv_path, newline="") as f:
        ann = {r["instance_id"]: r for r in csv.DictReader(f)}
    ids = [i for i in ann if i in rows_by_id]
    random.Random(seed).shuffle(ids)
    ids = ids[:n]
    tp = fp = tn = fn = 0
    per = []
    for iid in ids:
        human = float(ann[iid]["underspecified"]) >= 2
        judged = judge_candidate(rows_by_id[iid], model_id)
        model = int(judged.get("verified_rubric_score", 0)) >= 2
        per.append({"instance_id": iid, "human": float(ann[iid]["underspecified"]), "model": judged.get("verified_rubric_score")})
        tp += human and model
        fp += (not human) and model
        tn += (not human) and (not model)
        fn += human and (not model)
    total = max(1, tp + fp + tn + fn)
    return {"n": len(ids), "agreement": (tp + tn) / total, "precision": tp / max(1, tp + fp), "recall": tp / max(1, tp + fn),
            "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn}, "model_id": model_id, "prompt_version": JUDGE_PROMPT_VERSION,
            "per_instance": per}
