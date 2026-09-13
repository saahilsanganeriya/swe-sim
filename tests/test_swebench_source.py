import hashlib
import json
import subprocess
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
from urllib.parse import parse_qs, urlsplit

import pytest

from swesim.sources import swebench_annotated as sb
from swesim.sources import threads

CSV = "data/annotations/ensembled_annotations_public.csv"
F2P = "xarray/tests/test_weighted.py::test_weighted_operations_nonexistent_dim[{a}-b]"
P2P = "xarray/tests/test_weighted.py::test_weighted_sum"
ISSUE = ("Weighted mean silently ignores missing dims\n"
         "Calling ds.weighted(w).mean('z') with a dim 'z' that does not exist returns a result.\n"
         "Traceback shows no error at all.\n"
         "Expected: raise ValueError.")


# ---------------------------------------------------------------- pool A / pure helpers

def test_pool_a_count():
    ids = sb.pool_a(CSV)
    assert len(ids) == 109 and "sympy__sympy-13581" in ids


def test_image_name_and_repo_dir():
    assert sb.image_for("pydata__xarray-5362") == "swebench/sweb.eval.x86_64.pydata_1776_xarray-5362:latest"
    assert sb.REPO_DIR == "/testbed"


def test_pytest_repo_filter():
    ids = sb.pool_a(CSV)
    keep = sb.filter_pytest_repos(ids)
    assert all(not i.startswith(("django__", "sympy__")) for i in keep) and len(keep) >= 40


def test_repo_of_handles_hyphenated_repos():
    assert sb.repo_of("scikit-learn__scikit-learn-10297") == "scikit-learn/scikit-learn"
    assert sb.repo_of("pydata__xarray-5362") == "pydata/xarray"


def test_test_files_from_patch():
    tp = "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1 +1 @@\n-x\n+y\ndiff --git a/tests/sub/test_b.py b/tests/sub/test_b.py\n"
    assert sb.test_files_from_patch(tp) == ["tests/sub/test_b.py", "tests/test_a.py"]


# ---------------------------------------------------------------- candidates (dataset + threads mocked)

def _ds_row(iid, repo):
    return {"instance_id": iid, "repo": repo, "base_commit": "a" * 40, "version": "0.18", "problem_statement": ISSUE,
            "hints_text": "hint", "FAIL_TO_PASS": json.dumps([F2P]), "PASS_TO_PASS": json.dumps([P2P]),
            "patch": "diff --git a/x.py b/x.py\n", "test_patch": "diff --git a/xarray/tests/test_weighted.py b/xarray/tests/test_weighted.py\n"}


def test_candidates_writes_rows_with_annotation_and_thread(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "_load_dataset", lambda *a, **k: [_ds_row("pydata__xarray-5362", "pydata/xarray"), _ds_row("psf__requests-1963", "psf/requests")])
    calls = []
    monkeypatch.setattr(threads, "fetch_pr_thread", lambda repo, pr: calls.append((repo, pr)) or {"pr_url": f"https://github.com/{repo}/pull/{pr}"})
    out = tmp_path / "c" / "swebench-annotated.jsonl"
    n = sb.candidates(["pydata__xarray-5362", "psf__requests-1963"], out, Path(CSV))
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert n == 2 and len(rows) == 2 and calls == [("pydata/xarray", 5362), ("psf/requests", 1963)]
    r = rows[0]
    assert r["image"] == sb.image_for("pydata__xarray-5362") and r["repo_dir"] == "/testbed"
    assert r["fail_to_pass"] == [F2P] and r["pass_to_pass"] == [P2P]
    assert set(r["annotation"]) == {"underspecified", "underspecified_notes", "false_negative", "false_negative_notes", "difficulty"}
    assert r["annotation"]["underspecified"] == "2.0" and r["thread"]["pr_url"].endswith("/pull/5362")
    # psf__requests-1963 is not in the ensembled CSV: the row still exists with blank annotation fields
    assert rows[1]["annotation"]["underspecified"] == "" and rows[1]["annotation"]["difficulty"] == ""


def test_candidates_respects_pytest_filter_and_no_threads(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "_load_dataset", lambda *a, **k: [_ds_row("django__django-1", "django/django"), _ds_row("pydata__xarray-5362", "pydata/xarray")])
    monkeypatch.setattr(threads, "fetch_pr_thread", lambda *a: pytest.fail("threads must not be fetched"))
    out = tmp_path / "c.jsonl"
    assert sb.candidates(["django__django-1", "pydata__xarray-5362"], out, Path(CSV), fetch_threads=False) == 1
    assert sb.candidates(["django__django-1", "pydata__xarray-5362"], out, Path(CSV), pytest_repos_only=False, fetch_threads=False) == 2
    assert json.loads(out.read_text().splitlines()[0])["thread"] == {}


def test_candidates_unknown_instance_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "_load_dataset", lambda *a, **k: [])
    with pytest.raises(ValueError, match="not in SWE-bench"):
        sb.candidates(["pydata__xarray-5362"], tmp_path / "c.jsonl", Path(CSV), fetch_threads=False)


def test_mine_swebench_uses_workspace_and_pool(monkeypatch, tmp_path):
    from swesim.paths import Workspace
    from swesim.stages.mine import mine_swebench
    seen = {}

    def fake_candidates(ids, out, csv_path, pytest_repos_only=True, fetch_threads=True):
        seen.update(ids=ids, out=out, csv=csv_path, pytest_repos_only=pytest_repos_only, fetch_threads=fetch_threads)
        out.parent.mkdir(parents=True, exist_ok=True); out.write_text(""); return 0
    monkeypatch.setattr(sb, "candidates", fake_candidates)
    ws = Workspace(tmp_path / "ws")
    out = mine_swebench(ws, fetch_threads=False)
    assert out == ws.candidates_dir / "swebench-annotated.jsonl" and len(seen["ids"]) == 45 and seen["fetch_threads"] is False
    assert all(not i.startswith(("django__", "sympy__")) for i in seen["ids"])
    mine_swebench(ws, instance_ids=["pydata__xarray-5362"], pytest_only=False)
    assert seen["ids"] == ["pydata__xarray-5362"] and seen["pytest_repos_only"] is False
    with pytest.raises(ValueError):
        mine_swebench(ws, pool="B")


# ---------------------------------------------------------------- threads (gh api mocked)

class FakeGH:
    """Routes `gh api <path>` calls to handlers keyed by URL path; records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, cmd, **kw):
        assert cmd[:2] == ["gh", "api"], cmd
        self.calls.append(cmd)
        u = urlsplit(cmd[2])
        handler = self.routes.get(u.path)
        if handler is None:
            raise CalledProcessError(1, cmd, output="", stderr="gh: Not Found (HTTP 404)")
        out = handler(parse_qs(u.query)) if callable(handler) else handler
        if isinstance(out, Exception):
            raise CalledProcessError(1, cmd, output="", stderr=str(out))
        return CompletedProcess(cmd, 0, stdout=out if isinstance(out, str) else json.dumps(out), stderr="")


def _c(login, body, created="2021-05-01T00:00:00Z", **extra):
    return {"user": {"login": login}, "body": body, "created_at": created, "html_url": "https://github.com/o/r/x#1", **extra}


def test_pages_stops_on_short_page_and_bounds_pagination(monkeypatch):
    gh = FakeGH({"repos/o/r/issues/1/comments": lambda q: [_c("a", "x")] * (100 if q["page"] == ["1"] else 3)})
    monkeypatch.setattr(subprocess, "run", gh)
    assert len(threads.pages("repos/o/r/issues/1/comments?per_page=100")) == 103
    assert [parse_qs(urlsplit(c[2]).query)["page"] for c in gh.calls] == [["1"], ["2"]]
    monkeypatch.setattr(subprocess, "run", FakeGH({"repos/o/r/issues/1/comments": lambda q: [_c("a", "x")] * 100}))
    with pytest.raises(ValueError, match="pagination exceeded"):
        threads.pages("repos/o/r/issues/1/comments", max_pages=2)


def test_gh_backs_off_on_rate_limit_then_succeeds(monkeypatch):
    state = {"n": 0}

    def flaky(cmd, **kw):
        if cmd[2] == "rate_limit":  # primary limit exhausted, resets in ~5 s
            return CompletedProcess(cmd, 0, stdout=json.dumps({"resources": {"core": {"remaining": 0, "reset": int(threads.time.time()) + 5}}}), stderr="")
        state["n"] += 1
        if state["n"] < 3:
            raise CalledProcessError(1, cmd, output="", stderr="gh: API rate limit exceeded for user (HTTP 403)")
        return CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr="")
    sleeps = []
    monkeypatch.setattr(subprocess, "run", flaky)
    monkeypatch.setattr(threads.time, "sleep", sleeps.append)
    assert threads._gh("repos/o/r") == {"ok": True} and state["n"] == 3 and len(sleeps) == 2 and all(1 <= s <= 10 for s in sleeps)
    # primary quota still available (remaining > 0): bounded exponential backoff
    state["n"] = 0; sleeps.clear()
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: CompletedProcess(cmd, 0, stdout=json.dumps({"resources": {"core": {"remaining": 10, "reset": 0}}}), stderr="") if cmd[2] == "rate_limit" else flaky(cmd))
    assert threads._gh("repos/o/r") == {"ok": True} and sleeps == [2.0, 4.0]
    # secondary (burst) limit: never shorter than GitHub's one-minute guidance
    state["n"] = 0; sleeps.clear()

    def secondary(cmd, **kw):
        if cmd[2] == "rate_limit":
            return CompletedProcess(cmd, 0, stdout=json.dumps({"resources": {"core": {"remaining": 10, "reset": 0}}}), stderr="")
        state["n"] += 1
        if state["n"] < 3:
            raise CalledProcessError(1, cmd, output="", stderr="gh: You have exceeded a secondary rate limit. Please wait a few minutes before you try again. (HTTP 403)")
        return CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr="")
    monkeypatch.setattr(subprocess, "run", secondary)
    assert threads._gh("repos/o/r") == {"ok": True} and sleeps == [60.0, 60.0]


def test_gh_does_not_retry_other_errors(monkeypatch):
    calls = []

    def bad(cmd, **kw):
        calls.append(cmd); raise CalledProcessError(1, cmd, output="", stderr="gh: Not Found (HTTP 404)")
    monkeypatch.setattr(subprocess, "run", bad)
    monkeypatch.setattr(threads.time, "sleep", lambda s: pytest.fail("must not sleep"))
    with pytest.raises(CalledProcessError):
        threads._gh("repos/o/r/pulls/1")
    assert len(calls) == 1


def test_base_text_decodes_content_and_returns_none_on_404(monkeypatch):
    import base64
    gh = FakeGH({"repos/o/r/contents/tests/test_a.py": {"content": base64.b64encode(b"def test_a():\n    pass\n").decode(), "encoding": "base64"}})
    monkeypatch.setattr(subprocess, "run", gh)
    assert threads.base_text("o/r", "a" * 40, "tests/test_a.py") == "def test_a():\n    pass\n"
    assert "ref=" + "a" * 40 in gh.calls[0][2]
    assert threads.base_text("o/r", "a" * 40, "conftest.py") is None


def test_linked_numbers_from_body_and_timeline_fallback(monkeypatch):
    assert threads.linked_numbers("o/r", 5, "Fixes #12, closes: #34 and resolves #12; see #99") == [12, 34]
    timeline = [{"event": "cross-referenced", "source": {"issue": {"number": 7, "html_url": "https://github.com/o/r/issues/7"}}},
                {"event": "cross-referenced", "source": {"issue": {"number": 8, "html_url": "https://github.com/o/r/pull/8", "pull_request": {}}}},
                {"event": "cross-referenced", "source": {"issue": {"number": 9, "html_url": "https://github.com/other/repo/issues/9"}}},
                {"event": "labeled"}]
    monkeypatch.setattr(subprocess, "run", FakeGH({"repos/o/r/issues/5/timeline": timeline}))
    assert threads.linked_numbers("o/r", 5, "no reference here") == [7]


def _pr_routes():
    return {
        "repos/o/r/pulls/5": {"html_url": "https://github.com/o/r/pull/5", "title": "Fix it", "body": "Fixes #3", "user": {"login": "dev"},
                              "merged_at": "2021-06-01T00:00:00Z", "merge_commit_sha": "b" * 40, "created_at": "2021-05-20T00:00:00Z"},
        "repos/o/r/pulls/5/reviews": [{"user": {"login": "rev"}, "body": "LGTM", "submitted_at": "2021-05-21T00:00:00Z", "html_url": "https://github.com/o/r/pull/5#r1"}],
        "repos/o/r/issues/5/comments": [_c("dev", "human comment"), _c("codecov[bot]", "coverage")],
        "repos/o/r/pulls/5/comments": [_c("rev", "use ValueError", path="x.py")],
        "repos/o/r/issues/3": {"html_url": "https://github.com/o/r/issues/3", "title": "It breaks", "body": None, "user": {"login": "reporter"}, "created_at": "2021-05-01T00:00:00Z"},
        "repos/o/r/issues/3/comments": [_c("maint", "please raise"), _c("dependabot[bot]", "bump")],
    }


def test_fetch_pr_thread_composes_pr_reviews_comments_and_linked_issue(monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeGH(_pr_routes()))
    t = threads.fetch_pr_thread("o/r", 5)
    assert t["pr_url"].endswith("/pull/5") and t["gold_commit"] == "b" * 40 and t["pr_created_at"] == "2021-05-20T00:00:00Z"
    assert [c["user"] for c in t["pr_comments"]] == ["dev"] and t["reviews"][0]["created_at"] == "2021-05-21T00:00:00Z"
    assert t["review_comments"][0]["path"] == "x.py" and t["review_comments"][0]["body"] == "use ValueError"
    li = t["linked_issues"]
    assert len(li) == 1 and li[0]["issue_url"].endswith("/issues/3") and li[0]["body"] == "" and [c["user"] for c in li[0]["comments"]] == ["maint"]
    # Interfaces-block keys: issue_url / issue_comments at top level mirror the first linked issue
    assert t["issue_url"] == li[0]["issue_url"] and t["issue_comments"] == li[0]["comments"]
    assert "fetch_errors" not in t


def test_fetch_pr_thread_links_issues_named_in_the_title(monkeypatch):
    routes = _pr_routes()
    routes["repos/o/r/pulls/5"] = {**routes["repos/o/r/pulls/5"], "title": "ROB: Recover a corrupt pointer (closes #3)", "body": ""}
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    assert [i["number"] for i in threads.fetch_pr_thread("o/r", 5)["linked_issues"]] == [3]


def test_fetch_pr_thread_records_linked_issue_fetch_errors(monkeypatch):
    routes = _pr_routes(); del routes["repos/o/r/issues/3"]
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    t = threads.fetch_pr_thread("o/r", 5)
    assert t["linked_issues"] == [] and t["fetch_errors"][0]["issue"] == 3 and t["issue_url"] is None


# ---------------------------------------------------------------- judge

def test_issue_contract_delta_finds_unmentioned_requirements():
    from swesim.stages.judge import issue_contract_delta, accept
    row = {"problem_statement": "weighted mean should raise when the dim does not exist",
           "test_patch": "diff --git a/t.py b/t.py\n+    with pytest.raises(ValueError, match=\"dimension 'z' not found\"):\n+        w.mean('z')\n",
           "patch": "diff --git a/w.py b/w.py\n+def _check_dim(d):\n+    pass\n+class WeightedError(Exception):\n+    pass\n"}
    gaps = issue_contract_delta(row); srcs = {g["source"] for g in gaps}
    assert {"test-exception", "test-literal", "patch-public-name"} <= srcs and all("_check_dim" not in g["text"] for g in gaps)
    judged = {"gaps": [{"derivable_from_repo": False, "consequential": True, "resolved_by": "thread"}], "self_contained": True}
    assert accept(judged) and not accept({"gaps": [{"derivable_from_repo": True, "consequential": True, "resolved_by": "thread"}], "self_contained": True})
    assert not accept({"gaps": [{"derivable_from_repo": False, "consequential": True, "resolved_by": "thread"}], "self_contained": False})
    assert not accept({"gaps": [{"derivable_from_repo": False, "consequential": True, "resolved_by": "patch"}], "self_contained": True})


def test_judge_candidate_adds_code_owned_fields(monkeypatch):
    from swesim.stages import judge
    seen = {}

    def fake_chat(model_id, system, messages, schema, **kw):
        seen.update(model=model_id, system=system, user=json.loads(messages[0]["content"]))
        return {"gaps": [{"id": "g1", "text": "tests expect ValueError", "source": "test-exception", "derivable_from_repo": False, "consequential": True, "resolved_by": "thread", "category": "behavior"}],
                "verified_rubric_score": "2", "complexity": 2, "test_behavioralness": 4, "self_contained": True, "alternatives_accepted": False, "rationale": "ok"}
    monkeypatch.setattr(judge, "chat_json", fake_chat)
    row = {"instance_id": "pydata__xarray-5362", "problem_statement": ISSUE, "test_patch": "+    with pytest.raises(ValueError):\n", "patch": "", "thread": {}}
    out = judge.judge_candidate(row, "openai/gpt-5.5")
    assert out["keep"] is True and out["structural_tests"] is True and out["verified_rubric_score"] == 2
    assert out["prompt_version"] == judge.JUDGE_PROMPT_VERSION and out["model_id"] == "openai/gpt-5.5"
    assert out["candidate_gaps"] == judge.issue_contract_delta(row) and seen["user"]["candidate_gaps"] == out["candidate_gaps"]
    assert "CANDIDATE GAPS" in seen["system"]
    with pytest.raises(ValueError, match="incomplete source evidence"):
        judge.judge_candidate({**row, "thread": {"fetch_errors": [{"issue": 3}]}}, "openai/gpt-5.5")
    with pytest.raises(ValueError, match="context budget"):
        judge.judge_candidate({**row, "patch": "x" * 130000}, "openai/gpt-5.5")


def test_calibrate_reports_agreement_at_threshold(monkeypatch):
    from swesim.stages import judge
    ids = sb.pool_a(CSV)[:3] + [i for i in sb.annotation_rows(CSV) if float(sb.annotation_rows(CSV)[i]["underspecified"]) < 2][:3]
    rows = {i: {"instance_id": i} for i in ids}
    # model: says >=2 for pool-A ids (human >=2), 0 for the others except one false positive
    fp = ids[3]
    monkeypatch.setattr(judge, "judge_candidate", lambda row, model_id: {"verified_rubric_score": 3 if row["instance_id"] in ids[:3] or row["instance_id"] == fp else 0})
    out = judge.calibrate(Path(CSV), rows, "fake/model", n=6)
    assert out["n"] == 6 and out["confusion"] == {"tp": 3, "fp": 1, "tn": 2, "fn": 0}
    assert out["agreement"] == pytest.approx(5 / 6) and out["precision"] == pytest.approx(0.75) and out["recall"] == 1.0
    assert out["prompt_version"] == judge.JUDGE_PROMPT_VERSION and len(out["per_instance"]) == 6 and out["model_id"] == "fake/model"


# ---------------------------------------------------------------- draft_spec

def test_withheld_brief_has_no_excerpts(monkeypatch):
    from swesim.stages import draft_spec
    issue = 'Import fails\nTraceback: ERROR42\nrun_import("bad.csv")'
    monkeypatch.setattr(draft_spec, 'chat_json', lambda *a, **k: {'withheld_brief': 'Import fails',
        'removed': [{'category': 'error_message', 'excerpt': 'ERROR42', 'kind': 'reproduction', 'discovery': 'on-request'},
                    {'category': 'reproduction', 'excerpt': 'run_import("bad.csv")', 'kind': 'reproduction', 'discovery': 'on-request'}]})
    result = draft_spec.withhold(issue, 'fake/model')
    assert all(x['excerpt'] not in result['withheld_brief'] for x in result['removed'])


def test_withhold_retries_once_with_violations_then_fails(monkeypatch):
    from swesim.stages import draft_spec
    issue = 'Import fails\nTraceback: ERROR42'
    attempts = []

    def leaky(model_id, system, messages, schema, **kw):
        attempts.append(messages)
        return {'withheld_brief': 'Import fails ERROR42', 'removed': [{'category': 'error_message', 'excerpt': 'ERROR42', 'kind': 'reproduction', 'discovery': 'on-request'}]}
    monkeypatch.setattr(draft_spec, 'chat_json', leaky)
    with pytest.raises(ValueError, match="withholding failed"):
        draft_spec.withhold(issue, 'fake/model')
    assert len(attempts) == 2 and "Correct these violations" in attempts[1][-1]["content"] and "ERROR42" in attempts[1][-1]["content"]


def _row(**over):
    row = {"instance_id": "pydata__xarray-5362", "repo": "pydata/xarray", "base_commit": "a" * 40, "version": "0.18",
           "problem_statement": ISSUE, "hints_text": "", "fail_to_pass": [F2P], "pass_to_pass": [P2P],
           "patch": "diff --git a/xarray/core/weighted.py b/xarray/core/weighted.py\n--- a/xarray/core/weighted.py\n+++ b/xarray/core/weighted.py\n@@ -1 +1,2 @@\n x\n+y\n",
           "test_patch": "diff --git a/xarray/tests/test_weighted.py b/xarray/tests/test_weighted.py\n--- a/xarray/tests/test_weighted.py\n+++ b/xarray/tests/test_weighted.py\n@@ -1 +1,2 @@\n x\n+def test_new(): pass\n",
           "image": sb.image_for("pydata__xarray-5362"), "repo_dir": "/testbed",
           "annotation": {"underspecified": "2.0", "underspecified_notes": "n", "false_negative": "0.0", "false_negative_notes": "", "difficulty": "15 min - 1 hour"},
           "thread": {"pr_url": "https://github.com/pydata/xarray/pull/5362", "gold_commit": "b" * 40, "pr_created_at": "2021-05-20T00:00:00Z",
                      "linked_issues": [{"issue_url": "https://github.com/pydata/xarray/issues/5360", "title": "t", "body": "b", "comments": []}]}}
    row.update(over); return row


def _judged(**over):
    j = {"gaps": [{"id": "g1", "text": "tests expect ValueError", "source": "test-exception", "derivable_from_repo": False, "consequential": True, "resolved_by": "thread", "category": "behavior"}],
         "verified_rubric_score": 2, "complexity": 2, "test_behavioralness": 4, "self_contained": True, "alternatives_accepted": True, "keep": True, "structural_tests": False,
         "prompt_version": "judge-v2", "model_id": "fake/judge"}
    j.update(over); return j


def _draft_llm(alt=None):
    return {"briefs": [{"profile": "natural", "text": "ignored", "provenance": "original-issue"}],
            "ledger": [{"id": "f01", "text": "Raise ValueError when the weights dim is missing", "kind": "behavior", "provenance": "pr-review",
                        "source_excerpt": "raise a ValueError", "source_url": "https://github.com/pydata/xarray/pull/5362#discussion_r1",
                        "authored_rationale": None, "discovery": "on-request", "tests": [F2P]},
                       {"id": "f02", "text": "_check_dim helper name", "kind": "interface", "provenance": "pr-thread", "source_excerpt": "_check_dim",
                        "source_url": "https://github.com/pydata/xarray/pull/5362", "authored_rationale": None, "discovery": "author-only", "tests": []}],
            "operations": [{"type": "clarification", "target": "f01", "rationale": "issue never names the exception"}],
            "persona": {"summary": "xarray maintainer", "tone": "terse", "disclosure_style": "answers what is asked",
                        "derived_from_urls": ["https://github.com/pydata/xarray/pull/5362"], "role": "maintainer"},
            "requirements": [{"id": "r01", "text": "raise ValueError", "fact_id": "f01", "tests": [F2P], "source_url": "https://github.com/pydata/xarray/pull/5362#discussion_r1"}],
            "checks": [{"id": "c01", "description": "user-level reproducer", "command": "python -c 'import xarray'", "expected_observation": "ValueError"}],
            "groups": [{"id": "g01", "weight": 0.5, "tests": [F2P]}],  # weights are renormalized to sum to 1
            "plausible_wrong": {"name": "keyerror", "rationale": "wrong exception type", "diff": "diff --git a/xarray/core/weighted.py b/xarray/core/weighted.py\n--- a/xarray/core/weighted.py\n+++ b/xarray/core/weighted.py\n@@ -1 +1,2 @@\n x\n+raise KeyError\n"},
            "alternative_correct": alt,
            "protected_paths_extra": ["xarray/tests/data/w.nc"]}


def _withhold_llm():
    return {"withheld_brief": "Weighted mean silently ignores missing dims\nCalling a weighted reduction with a missing dim returns a result.",
            "removed": [{"category": "expected_output", "excerpt": "Expected: raise ValueError.", "kind": "behavior", "discovery": "on-request"},
                        {"category": "error_message", "excerpt": "Traceback shows no error at all.", "kind": "reproduction", "discovery": "on-request"},
                        {"category": "location", "excerpt": "ds.weighted(w).mean('z')", "kind": "context", "discovery": "on-request"}]}


def _patch_llm(monkeypatch, draft_out, withhold_out):
    from swesim.stages import draft_spec
    calls = []

    def fake_chat(model_id, system, messages, schema, **kw):
        calls.append({"model": model_id, "system": system, "messages": messages, "kw": kw})
        return withhold_out if "Remove selected spans" in system else draft_out
    monkeypatch.setattr(draft_spec, "chat_json", fake_chat)
    monkeypatch.setattr(threads, "base_text", lambda repo, commit, path: "def test_weighted_sum():\n    assert True\n" if path.endswith(".py") and "tests" in path else None)
    return calls


def test_draft_writes_valid_spec_assets_and_judge_json(monkeypatch, tmp_path):
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    calls = _patch_llm(monkeypatch, _draft_llm(), _withhold_llm())
    out_dir = tmp_path / "scenarios" / "pydata--xarray-5362"
    p = draft_spec.draft(_row(), _judged(), "fake/model", out_dir)
    assert p == out_dir / "scenario.draft.json"
    spec = ScenarioSpec.model_validate_json(p.read_text())
    assert spec.id == "pydata--xarray-5362" and spec.source == "swebench-annotated" and spec.source_ref == "pydata__xarray-5362"
    assert spec.environment.kind == "prebuilt-image" and spec.environment.image == sb.image_for("pydata__xarray-5362")
    assert spec.environment.repo_dir == "/testbed" and spec.environment.gold_commit == "b" * 40
    # assets: every manifest entry matches the file on disk
    for name, a in spec.assets.items():
        data = (out_dir / a.path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == a.sha256 and len(data) == a.bytes and a.path == f"assets/{name}"
    assert {"oracle.diff", "test.patch", "plausible-wrong.diff", "tamper-root-conftest.diff", "tamper-sitecustomize.diff", "tamper-skip-marker.diff"} <= set(spec.assets)
    assert (out_dir / "assets" / "oracle.diff").read_text() == _row()["patch"]
    # controls
    assert spec.controls.oracle == "oracle.diff"
    assert [c.cls for c in spec.controls.plausible_wrong] == ["plausible-wrong"] and spec.controls.plausible_wrong[0].expected_success == 0
    assert len(spec.controls.tamper) == 3 and all(c.cls == "incorrect-exploit" and c.expected_reward == 0.0 for c in spec.controls.tamper)
    assert {c.asset for c in spec.controls.tamper} == {"tamper-root-conftest.diff", "tamper-sitecustomize.diff", "tamper-skip-marker.diff"}
    # runner: exact node ids, {junit_path} placeholder survives str.format, braces in ids are escaped
    r = spec.contract.runner
    assert r.kind == "pytest-ra" and r.command.startswith("/opt/miniconda3/envs/testbed/bin/python -I /tests/run_pytest.py -rA -p no:cacheprovider --junitxml={junit_path} ")
    formatted = r.command.format(junit_path="/tmp/j.xml")
    assert "--junitxml=/tmp/j.xml" in formatted and "[{a}-b]" in formatted and P2P in formatted
    assert r.single_command.endswith("{nodeid}") and r.test_files == ["xarray/tests/test_weighted.py"]
    assert spec.environment.test_command == r.command
    assert spec.contract.test_assets == ["test.patch"] and spec.contract.protected_paths == ["xarray/tests/data/w.nc", "xarray/tests/test_weighted.py"]
    # briefs / withholding provenance
    assert [b.profile for b in spec.briefs] == ["natural", "withheld"] and spec.briefs[0].text == ISSUE
    ops = [o for o in spec.operations if o.type == "withholding"]
    assert [o.target for o in ops] == ["withheld-001", "withheld-002", "withheld-003"] and all("Removed category" in o.rationale for o in ops)
    wf = {f.id: f for f in spec.ledger if f.id.startswith("withheld-")}
    assert all(f.provenance == "original-issue" and f.source_excerpt in ISSUE and f.source_url.endswith("/issues/5360") for f in wf.values())
    assert wf["withheld-001"].discovery == "on-request" and wf["withheld-003"].discovery == "repo-derivable"  # location category is repo-derivable
    # the on-request fact linked to a FAIL_TO_PASS test survives for the approval gate
    assert any(f.discovery == "on-request" and F2P in f.tests for f in spec.ledger)
    assert [g.weight for g in spec.groups] == [1.0]
    assert spec.difficulty.annotated == "15 min - 1 hour" and spec.difficulty.judged["keep"] is True
    assert spec.thread["pr_url"].endswith("/pull/5362")
    # judge.json next to the draft with the keys approve reads
    j = json.loads((out_dir / "judge.json").read_text())
    assert {"gaps", "verified_rubric_score", "keep"} <= set(j) and j["keep"] is True
    # LLM calls: one draft, one withhold, both with the requested model
    assert [c["model"] for c in calls] == ["fake/model", "fake/model"] and calls[0]["kw"].get("max_tokens") == 8000
    assert json.loads(calls[0]["messages"][0]["content"])["candidate"]["instance_id"] == "pydata__xarray-5362"


def test_draft_structural_tests_require_alternative_correct(monkeypatch, tmp_path):
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    alt = {"name": "loop-impl", "rationale": "same behaviour, different structure", "diff": "diff --git a/xarray/core/weighted.py b/xarray/core/weighted.py\n--- a/xarray/core/weighted.py\n+++ b/xarray/core/weighted.py\n@@ -1 +1,2 @@\n x\n+z\n"}
    _patch_llm(monkeypatch, _draft_llm(alt=alt), _withhold_llm())
    p = draft_spec.draft(_row(), _judged(alternatives_accepted=False, structural_tests=True), "fake/model", tmp_path / "s1")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    ac = spec.controls.alternative_correct
    assert len(ac) == 1 and ac[0].cls == "alternative-correct" and ac[0].expected_success == 1 and ac[0].asset == "alternative-correct.diff"
    assert (tmp_path / "s1" / "assets" / "alternative-correct.diff").read_text() == alt["diff"]
    _patch_llm(monkeypatch, _draft_llm(alt=None), _withhold_llm())
    with pytest.raises(ValueError, match="alternative-correct"):
        draft_spec.draft(_row(), _judged(alternatives_accepted=False, structural_tests=True), "fake/model", tmp_path / "s2")


def test_draft_rejects_leaky_withholding_and_empty_controls(monkeypatch, tmp_path):
    from swesim.stages import draft_spec
    leaky = _withhold_llm(); leaky["withheld_brief"] = ISSUE
    _patch_llm(monkeypatch, _draft_llm(), leaky)
    with pytest.raises(ValueError, match="withholding failed"):
        draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s1")
    d = _draft_llm(); d["plausible_wrong"]["diff"] = ""
    _patch_llm(monkeypatch, d, _withhold_llm())
    with pytest.raises(ValueError, match="plausible-wrong"):
        draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s2")
    d = _draft_llm(); d["ledger"][0]["tests"] = ["not/a/test.py::nope"]
    _patch_llm(monkeypatch, d, _withhold_llm())
    with pytest.raises(ValueError):
        draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s3")


def test_draft_github_repo_uses_dockerfile_environment(monkeypatch, tmp_path):
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    _patch_llm(monkeypatch, _draft_llm(), _withhold_llm())
    row = _row(source="github-repo", instance_id="py-pdf__pypdf-3400", repo="py-pdf/pypdf", image=None, repo_dir="/app",
               dockerfile="FROM python@sha256:abc\nRUN echo hi\n", test_python="/usr/local/bin/python",
               annotation={"underspecified": "", "underspecified_notes": "", "false_negative": "", "false_negative_notes": "", "difficulty": ""},
               thread={"pr_url": "https://github.com/py-pdf/pypdf/pull/3400", "gold_commit": None, "pr_created_at": "2026-07-01T00:00:00Z", "linked_issues": []})
    p = draft_spec.draft(row, _judged(), "fake/model", tmp_path / "pypdf")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    assert spec.id == "py-pdf--pypdf-3400" and spec.source == "github-repo"
    assert spec.environment.kind == "dockerfile" and spec.environment.image is None and spec.environment.dockerfile_asset == "Dockerfile.base"
    assert spec.environment.repo_dir == "/app" and spec.contract.runner.command.startswith("/usr/local/bin/python -I /tests/run_pytest.py")
    assert (tmp_path / "pypdf" / "assets" / "Dockerfile.base").read_text() == row["dockerfile"] and "Dockerfile.base" in spec.assets
    # no linked issue: the withheld facts cite the PR url
    assert all(f.source_url.endswith("/pull/3400") for f in spec.ledger if f.id.startswith("withheld-"))


def test_draft_rejects_runner_command_over_argv_limit(monkeypatch, tmp_path):
    from swesim.stages import draft_spec
    _patch_llm(monkeypatch, _draft_llm(), _withhold_llm())
    p2p = [f"xarray/tests/test_big.py::test_case_{i:05d}[{'x' * 40}]" for i in range(2000)]  # ~170k chars, like pydata__xarray-4759
    with pytest.raises(ValueError, match="single-argument limit"):
        draft_spec.draft(_row(pass_to_pass=p2p), _judged(), "fake/model", tmp_path / "s")


def test_llm_view_trims_pass_to_pass_but_spec_keeps_it(monkeypatch, tmp_path):
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec, judge
    p2p = [f"xarray/tests/test_many.py::test_case_{i:04d}" for i in range(300)]
    view = judge.llm_view(_row(pass_to_pass=p2p))
    assert len(view["pass_to_pass"]) == judge.P2P_SAMPLE and view["pass_to_pass_count"] == 300 and view["pass_to_pass_truncated"] is True
    assert judge.llm_view(_row())["pass_to_pass"] == [P2P] and "pass_to_pass_truncated" not in judge.llm_view(_row())
    calls = _patch_llm(monkeypatch, _draft_llm(), _withhold_llm())
    p = draft_spec.draft(_row(pass_to_pass=p2p), _judged(), "fake/model", tmp_path / "s")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    assert spec.contract.pass_to_pass == p2p and all(t in spec.contract.runner.command for t in p2p)
    assert json.loads(calls[0]["messages"][0]["content"])["candidate"]["pass_to_pass_count"] == 300


def test_draft_fails_without_a_source_url_for_withheld_facts(monkeypatch, tmp_path):
    from swesim.stages import draft_spec
    _patch_llm(monkeypatch, _draft_llm(), _withhold_llm())
    with pytest.raises(ValueError, match="source URL"):
        draft_spec.draft(_row(thread={}), _judged(), "fake/model", tmp_path / "s")
