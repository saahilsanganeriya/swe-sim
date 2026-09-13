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
import shlex
from datetime import datetime, timezone
from pathlib import Path

from swesim import tamper
from swesim.llm import chat_json
from swesim.paths import atomic_json
from swesim.sources import threads
from swesim.sources.swebench_annotated import TEST_PYTHON, test_files_from_patch
from swesim.stages.judge import llm_view

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
ask about); fact, requirement, check and group ids match ^[a-z0-9][a-z0-9._-]{2,63}$ and never start with "withheld-"; test
links use only the FAIL_TO_PASS and PASS_TO_PASS ids given; check ids differ from test ids. Every non-authored fact's
source_excerpt is copied verbatim (no paraphrase) from the text its provenance names: issue-thread = linked issue
title/body/comments or hints_text, pr-thread = PR title/body/comments, pr-review = review bodies and review comments,
annotator-note = the annotation notes; its source_url is a URL under the repository. If the judge marked structural_tests,
also write one alternative-correct patch (a different implementation the tests should still accept); otherwise set
alternative_correct to null."""
SCHEMA = """{"briefs": [{"profile": "natural", "text": "...", "provenance": "original-issue"}],
"ledger": [{"id": "f01", "text": "...", "kind": "behavior|interface|constraint|reproduction|context",
  "provenance": "issue-thread|pr-thread|pr-review|annotator-note|authored", "source_excerpt": "...", "source_url": "...",
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
GROUNDED_PROVENANCE = ("original-issue", "issue-thread", "pr-thread", "pr-review", "annotator-note")
WITHHOLD_SCHEMA = ('{"withheld_brief":"text","removed":[{"category":"reproduction|error_message|expected_output|location|version|proposed_fix",'
                   '"excerpt":"verbatim contiguous span","kind":"reproduction|context|behavior|interface|constraint","discovery":"on-request|repo-derivable"}]}')
WITHHOLD_PROMPT = ("Remove selected spans by category; preserve the first line exactly and a recognizable symptom and desired outcome. "
                   "Do not add requirements. Record EVERY removal verbatim. No free summary. File locations and explicit fix proposals are repo-derivable.")


def _title(natural_issue: str) -> str:
    lines = [l.strip() for l in natural_issue.strip().splitlines() if l.strip()]
    return lines[0] if lines else ""


def withhold(natural_issue: str, model_id: str) -> dict:
    """Category-targeted withholding with verbatim removal records; validated, one correction round, then fail."""
    messages = [{"role": "user", "content": natural_issue}]
    title = _title(natural_issue)
    for _attempt in range(2):
        out = chat_json(model_id, WITHHOLD_PROMPT, messages, WITHHOLD_SCHEMA, max_tokens=5000)
        bad = []
        text = out.get("withheld_brief") or ""
        if not text or title not in text:
            bad.append("missing title")
        if not out.get("removed"):
            bad.append("no removed spans")
        for item in out.get("removed") or []:
            span = item.get("excerpt") or ""
            if item.get("category") not in WITHHOLD_CATEGORIES or not span or span not in natural_issue:
                bad.append("invalid removed span")
            elif span.casefold() in text.casefold():
                bad.append(span)
            if item.get("discovery") not in WITHHOLD_DISCOVERY:
                bad.append(f"invalid discovery {item.get('discovery')!r}; use on-request or repo-derivable")
        if not bad:
            return out
        messages.append({"role": "assistant", "content": json.dumps(out)})
        messages.append({"role": "user", "content": "Correct these violations: " + json.dumps(bad)})
    raise ValueError("withholding failed validation after two attempts")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _corpora(row: dict) -> dict[str, str]:
    """Whitespace-normalized source text per provenance; a fact's excerpt must be a verbatim span of its corpus."""
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
            "annotator-note": _norm("\n".join([ann.get("underspecified_notes") or "", ann.get("false_negative_notes") or ""]))}


def ledger_violations(d: dict, row: dict) -> list[str]:
    """Code-owned grounding of LLM-authored facts. Every non-authored fact must quote a verbatim span (after whitespace
    normalization) of the text its provenance names and cite a URL under the repository; and at least one on-request
    fact must link a FAIL_TO_PASS test, otherwise interaction is not load-bearing and approval would fail anyway."""
    corpora = _corpora(row)
    url_prefix = f"https://github.com/{row['repo']}/".casefold()
    bad = []
    ledger = d.get("ledger") or []
    for f in ledger:
        fid, prov = f.get("id", "?"), f.get("provenance")
        if prov == "authored":
            continue
        if prov not in corpora:
            bad.append(f"{fid}: unknown provenance {prov!r}")
            continue
        excerpt = _norm(f.get("source_excerpt"))
        if not excerpt or excerpt not in corpora[prov]:
            bad.append(f"{fid}: source_excerpt is not a verbatim span of the {prov} text: {excerpt[:80]!r}")
        if not (f.get("source_url") or "").casefold().startswith(url_prefix):
            bad.append(f"{fid}: source_url must be under https://github.com/{row['repo']}/")
    f2p = set(row.get("fail_to_pass") or [])
    if not any(f.get("discovery") == "on-request" and set(f.get("tests") or []) & f2p for f in ledger):
        bad.append("no on-request fact links a FAIL_TO_PASS test id (interaction would not be load-bearing)")
    return bad


def _author(row: dict, judged: dict, model_id: str) -> dict:
    """One LLM authoring call, validated by `ledger_violations`, one correction round, then fail."""
    user = json.dumps({"candidate": llm_view(row), "judge": judged}, ensure_ascii=False)
    if len(user) > CONTEXT_BUDGET_CHARS:
        raise ValueError("candidate exceeds authoring context budget")
    messages = [{"role": "user", "content": user}]
    bad: list[str] = []
    for _attempt in range(2):
        d = chat_json(model_id, SYSTEM, messages, SCHEMA, max_tokens=8000)
        bad = ledger_violations(d, row)
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
    d = _author(row, judged, model_id)

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
            "ledger": d["ledger"], "operations": d.get("operations") or [], "persona": d["persona"], "requirements": d.get("requirements") or [],
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
        spec["ledger"].append({"id": fid, "text": item["excerpt"], "kind": item["kind"], "provenance": "original-issue",
                               "source_excerpt": item["excerpt"], "source_url": issue_url, "discovery": discovery, "tests": []})
        spec["operations"].append({"type": "withholding", "target": fid, "rationale": "Removed category: " + item["category"]})

    from swesim.models import ScenarioSpec
    parsed = ScenarioSpec.model_validate(spec)
    p = out_dir / "scenario.draft.json"
    p.write_text(parsed.model_dump_json(indent=2))
    atomic_json(out_dir / "judge.json", {"gaps": judged.get("gaps", []), "verified_rubric_score": judged.get("verified_rubric_score"),
                                         "keep": judged.get("keep"), **judged})
    atomic_json(out_dir / "draft.meta.json", {"model_id": model_id, "prompt_version": DRAFT_PROMPT_VERSION, "pipeline_version": pipeline_version,
                                              "created_at": spec["created_at"], "spec_hash": parsed.spec_hash})
    return p
