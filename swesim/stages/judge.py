"""Judge stage: code-extracted candidate gaps, an LLM classification of each gap plus the SWE-bench Verified
underspecification rubric, and the code-owned acceptance rule `accept()`.

The judge model is deliberately different from the draft model so their errors are not correlated.
"""
from __future__ import annotations

import csv
import json
import random
import re
from pathlib import Path

from swesim.llm import chat_json

JUDGE_PROMPT_VERSION = "judge-v2"
SYSTEM = """You review a GitHub issue, its discussion, the merged fix, and the fix's tests to decide whether it makes a good
interactive software-engineering task in which a simulated maintainer can answer an agent's questions.
You are given CANDIDATE GAPS extracted by code: things the tests or the fix require that the issue text never mentions.
Classify each gap; never judge the issue as a whole. Also score the issue's underspecification 0-3 with the SWE-bench
Verified annotator rubric (0: well specified; 1: minor blanks a reasonable solver fills; 2: multiple valid solutions,
the tests accept one; 3: nearly impossible without more information). Be strict and concrete."""
SCHEMA = """{"gaps": [{"id": "g1", "text": "...", "source": "test-literal|test-exception|patch-public-name|diff-branch|thread",
  "derivable_from_repo": true|false, "consequential": true|false, "resolved_by": "thread|patch|none",
  "category": "behavior|interface|constraint|reproduction|context"}],
"verified_rubric_score": 0|1|2|3, "complexity": 1-5, "test_behavioralness": 1-5, "self_contained": true|false,
"alternatives_accepted": true|false, "rationale": "2-4 sentences"}"""
CONTEXT_BUDGET_CHARS = 120000
P2P_SAMPLE = 40

_EXC = re.compile(r"pytest\.raises\(\s*([A-Za-z_][\w.]*)")


def llm_view(row: dict) -> dict:
    """The candidate as shown to the judge/draft model: PASS_TO_PASS ids are regression bookkeeping, not evidence, so only a
    count and a head sample are sent (some instances carry 2,000+ ids). The spec itself always keeps the full list."""
    p2p = row.get("pass_to_pass") or []
    if len(p2p) <= P2P_SAMPLE:
        return row
    return {**row, "pass_to_pass": p2p[:P2P_SAMPLE], "pass_to_pass_count": len(p2p), "pass_to_pass_truncated": True}
_LIT = re.compile(r"(?:assert[^\n]*?|match=)\s*[\"']([^\"'\n]{6,80})[\"']")
_DEF = re.compile(r"^\+\s*(?:def|class)\s+([A-Za-z_]\w*)", re.M)


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
    """Code-owned acceptance rule: at least one gap that is explicitly non-derivable (`derivable_from_repo is False`, a
    missing field never counts), consequential and resolved by a stakeholder statement; and the task is self-contained."""
    return any(g.get("derivable_from_repo") is False and g.get("consequential") is True and g.get("resolved_by") == "thread"
               for g in judged.get("gaps") or []) and judged.get("self_contained") is True


def judge_candidate(row: dict, model_id: str) -> dict:
    thread = row.get("thread") or {}
    if thread.get("fetch_errors"):
        raise ValueError("incomplete source evidence")
    payload = {"candidate": llm_view(row), "candidate_gaps": issue_contract_delta(row)}
    user = json.dumps(payload, ensure_ascii=False)
    if len(user) > CONTEXT_BUDGET_CHARS:
        raise ValueError("candidate exceeds authoring context budget")
    out = chat_json(model_id, SYSTEM, [{"role": "user", "content": user}], SCHEMA)
    if not isinstance(out.get("gaps"), list):
        out["gaps"] = []
    try:
        out["verified_rubric_score"] = int(out.get("verified_rubric_score", 0))
    except (TypeError, ValueError):
        out["verified_rubric_score"] = 0
    out["candidate_gaps"] = payload["candidate_gaps"]
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
