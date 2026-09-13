import hashlib
import json
import shlex
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
         "Expected: raise ValueError.\n"
         "I am using xarray 0.18 on Linux.\n"
         "A small reproducer is attached below.")
WITHHOLD_PROMPT_MARK = "Select spans of this issue to withhold"


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
    # pytest_repos_only=False: psf/requests is excluded by the eligibility filter (network-dependent tests) but this test
    # exercises row construction, not the filter (see test_eligibility_excludes_network_test_repos).
    n = sb.candidates(["pydata__xarray-5362", "psf__requests-1963"], out, Path(CSV), pytest_repos_only=False)
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
    assert out == ws.candidates_dir / "swebench-annotated.jsonl" and len(seen["ids"]) == 40 and seen["fetch_threads"] is False
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


def _issue(n, pr=False):
    d = {"number": n, "html_url": f"https://github.com/o/r/{'pull' if pr else 'issues'}/{n}", "title": f"#{n}", "body": "", "user": {"login": "u"}, "created_at": "2021-05-01T00:00:00Z"}
    if pr:
        d["pull_request"] = {"url": "x"}
    return d


def test_linked_numbers_from_body_and_timeline_fallback(monkeypatch):
    timeline = [{"event": "cross-referenced", "source": {"issue": {"number": 7, "html_url": "https://github.com/o/r/issues/7"}}},
                {"event": "cross-referenced", "source": {"issue": {"number": 8, "html_url": "https://github.com/o/r/pull/8", "pull_request": {}}}},
                {"event": "cross-referenced", "source": {"issue": {"number": 9, "html_url": "https://github.com/other/repo/issues/9"}}},
                {"event": "labeled"}]
    gh = FakeGH({"repos/o/r/issues/12": _issue(12), "repos/o/r/issues/34": _issue(34), "repos/o/r/issues/40": _issue(40, pr=True), "repos/o/r/issues/5/timeline": timeline})
    monkeypatch.setattr(subprocess, "run", gh)
    assert threads.linked_numbers("o/r", 5, "Fixes #12, closes: #34 and resolves #12; see #99") == [12, 34]
    assert not any("timeline" in c[2] for c in gh.calls)                       # real issues found: no timeline fallback
    assert threads.linked_numbers("o/r", 5, "no reference here") == [7]         # timeline fallback skips PRs and other repos
    t = threads.linked_targets("o/r", 5, "Closes #40 and fixes #77")              # 40 is a PR, 77 does not exist -> fallback to timeline
    assert t["prs"] == [40] and t["missing"] == [77] and t["issues"] == [7] and 40 not in t["payloads"]
    t = threads.linked_targets("o/r", 5, "Fixes #12")
    assert t == {"issues": [12], "prs": [], "missing": [], "payloads": {12: _issue(12)}, "link_kind": "closing"}
    assert threads.linked_targets("o/r", 5, "no reference here")["link_kind"] == "timeline"


def test_linked_targets_plain_mention_is_the_weakest_tier(monkeypatch):
    """No closing keyword and no timeline cross-reference: a plain `#N` / `GH-N` / issue-URL mention of a real issue counts,
    tagged link_kind="mention"; PR-typed and 404 mentions are classified, the PR's own number is skipped, lookups are bounded."""
    gh = FakeGH({"repos/o/r/issues/5/timeline": [], "repos/o/r/issues/21": _issue(21), "repos/o/r/issues/22": _issue(22, pr=True),
                 "repos/o/r/issues/23": Exception("gh: HTTP 404 Not Found"), "repos/o/r/issues/24": _issue(24)})
    monkeypatch.setattr(subprocess, "run", gh)
    t = threads.linked_targets("o/r", 5, "Follow-up to #5, related to #22 and #23; see GH-21 and https://github.com/o/r/issues/24")
    assert t["issues"] == [21, 24] and t["prs"] == [22] and t["missing"] == [23] and t["link_kind"] == "mention"
    assert not any("issues/5\n" in c[2] or c[2].endswith("issues/5") for c in gh.calls)   # own number never looked up
    monkeypatch.setattr(threads, "MAX_MENTIONS", 1)
    t = threads.linked_targets("o/r", 5, "see #21 and #24")
    assert t["issues"] == [21] and t["link_kind"] == "mention"                                 # bounded to one lookup
    assert threads.linked_targets("o/r", 5, "nothing here")["link_kind"] is None


def test_linked_targets_only_404_is_missing(monkeypatch):
    gh = FakeGH({"repos/o/r/issues/5/timeline": [], "repos/o/r/issues/77": Exception("gh: HTTP 500 Internal Server Error")})
    monkeypatch.setattr(subprocess, "run", gh)
    assert threads.linked_targets("o/r", 5, "Fixes #66") == {"issues": [], "prs": [], "missing": [66], "payloads": {}, "link_kind": None}   # 404 -> missing
    with pytest.raises(CalledProcessError):
        threads.linked_targets("o/r", 5, "Fixes #77")                                                                     # 500 -> propagates


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
    assert "fetch_errors" not in t and t["linked_prs"] == []


def test_fetch_pr_thread_drops_pull_request_typed_links(monkeypatch):
    routes = _pr_routes()
    routes["repos/o/r/issues/3"] = {**routes["repos/o/r/issues/3"], "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/3"}}
    routes["repos/o/r/issues/5/timeline"] = []
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    t = threads.fetch_pr_thread("o/r", 5)
    assert t["linked_issues"] == [] and t["linked_prs"] == [3] and t["issue_url"] is None and "fetch_errors" not in t
    with pytest.raises(ValueError, match="pull request, not an issue"):
        threads.fetch_issue_thread("o/r", 3)


def test_fetch_pr_thread_links_issues_named_in_the_title(monkeypatch):
    routes = _pr_routes()
    routes["repos/o/r/pulls/5"] = {**routes["repos/o/r/pulls/5"], "title": "ROB: Recover a corrupt pointer (closes #3)", "body": ""}
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    assert [i["number"] for i in threads.fetch_pr_thread("o/r", 5)["linked_issues"]] == [3]


def test_fetch_pr_thread_records_linked_issue_fetch_errors(monkeypatch):
    routes = _pr_routes(); del routes["repos/o/r/issues/3"]
    routes["repos/o/r/issues/5/timeline"] = []   # the unfetchable closing target triggers the timeline fallback, which finds nothing
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    t = threads.fetch_pr_thread("o/r", 5)
    assert t["linked_issues"] == [] and t["fetch_errors"][0]["issue"] == 3 and t["issue_url"] is None and t["linked_prs"] == []


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

    base = {"derivable_from_repo": False, "consequential": True, "category": "behavior"}

    def fake_chat(model_id, system, messages, schema, **kw):
        seen.update(model=model_id, system=system, user=json.loads(messages[0]["content"]))
        return {"gaps": [{"id": "g1", "text": "tests expect ValueError", "source": "test-exception", "resolved_by": "thread",
                          "resolution_excerpt": "raise a ValueError when the dim is missing", **base},
                         {"id": "g2", "text": "message names the dim", "source": "proposed", "evidence": "match=\"dimension 'z' not found\"", "question": "message?",
                          "resolved_by": "tests", "resolution_excerpt": "", **base},
                         {"id": "g3", "text": "invented", "source": "proposed", "evidence": "raise TypeError('nope')", "resolved_by": "tests", **base},   # not verbatim: dropped
                         {"id": "g4", "text": "dup", "source": "proposed", "evidence": "match=\"dimension  'z' not found\"", "resolved_by": "tests", **base}],  # same evidence: dropped
                "verified_rubric_score": "2", "complexity": 2, "test_behavioralness": 4, "self_contained": True, "alternatives_accepted": False, "rationale": "ok"}
    monkeypatch.setattr(judge, "chat_json", fake_chat)
    row = _row(annotation={**_row()["annotation"], "underspecified_notes": "ANNOTATOR SECRET NOTE"})
    out = judge.judge_candidate(row, "openai/gpt-5.5")
    assert out["keep"] is True and out["structural_tests"] is True and out["verified_rubric_score"] == 2
    assert out["prompt_version"] == judge.JUDGE_PROMPT_VERSION == "judge-v3" and out["model_id"] == "openai/gpt-5.5"
    assert out["candidate_gaps"] == judge.issue_contract_delta(row) and seen["user"]["candidate_gaps"] == out["candidate_gaps"]
    assert [g["id"] for g in out["gaps"]] == ["g1", "g2"] and out["gaps_proposed"] == 1 and out["gaps_dropped"] == 2 and out["gaps_extracted"] == len(out["candidate_gaps"])
    assert out["gaps"][0]["resolved_by"] == "thread" and out["gaps"][1]["resolved_by"] == "tests" and out["gaps"][1]["evidence_grounded"] is True
    assert "CANDIDATE GAPS" in seen["system"] and "PROPOSE" in seen["system"]
    # calibration independence: the judge never sees the annotators' notes
    assert "annotation" not in seen["user"]["candidate"] and "ANNOTATOR SECRET NOTE" not in json.dumps(seen["user"])
    with pytest.raises(ValueError, match="incomplete source evidence"):
        judge.judge_candidate({**row, "thread": {"fetch_errors": [{"issue": 3}]}}, "openai/gpt-5.5")
    # an oversized patch is trimmed to fit instead of aborting; the payload records the cut
    out = judge.judge_candidate({**row, "patch": "x" * 130000}, "openai/gpt-5.5")
    assert out["payload_trimmed"] == {"patch": 30000} and out["payload_chars"] <= judge.CONTEXT_BUDGET_CHARS
    assert seen["user"]["candidate"]["patch"].startswith("x" * 30000) and "characters trimmed" in seen["user"]["candidate"]["patch"]
    # only when the essentials themselves do not fit does the judge still refuse
    with pytest.raises(ValueError, match="context budget even after trimming"):
        judge.judge_candidate({**row, "fail_to_pass": [f"tests/test_big.py::test_case_{i:05d}" for i in range(6000)]}, "openai/gpt-5.5")


def test_llm_view_trims_the_largest_field_first_to_fit_the_budget():
    from swesim.stages import judge
    row = _row(patch="p" * 130000, thread={**_thread("pydata/xarray", 5362), "pr_comments": [_c("u", "c" * 20, "2021-05-01T00:00:00Z")] * 300})
    view = judge.llm_view(row, budget=120000)
    assert view["trimmed"] == {"patch": 30000} and len(json.dumps(view)) <= 120000                     # only the culprit is cut
    assert view["problem_statement"] == ISSUE and view["fail_to_pass"] == [F2P] and view["test_patch"] == row["test_patch"]
    assert len(view["thread"]["pr_comments"]) == 300 and "pr_comments_total" not in view["thread"]
    view = judge.llm_view(row, budget=60000)                                    # tighter: patch to 30k, then the thread (now the largest) to 20 comments, then it fits
    assert view["trimmed"] == {"patch": 30000, "thread": 20} and len(json.dumps(view)) <= 60000
    assert view["thread"]["pr_comments_total"] == 300 and len(view["thread"]["pr_comments"]) == 20
    view = judge.llm_view(row, budget=30000)                                    # below the first notch: the patch takes its second notch
    assert view["trimmed"]["patch"] == 8000 and len(json.dumps(view)) <= 30000
    assert judge.llm_view(row, budget=60000, extra_chars=0)["fail_to_pass"] == [F2P]                    # F2P ids are never trimmed
    assert "trimmed" not in judge.llm_view(_row(), budget=120000)                                        # nothing to do when it fits
    assert judge.llm_view(_row()) == judge.llm_view(_row(), budget=None)
    with pytest.raises(ValueError, match="even after trimming"):
        judge.llm_view(_row(fail_to_pass=[f"tests/test_big.py::test_case_{i:05d}" for i in range(6000)]), budget=120000)
    small = judge.llm_view(_row(problem_statement="s" * 50000, hints_text="h" * 50000), budget=40000)
    assert small["trimmed"] == {"problem_statement": 20000, "hints_text": 20000} or small["trimmed"]["problem_statement"] in (20000, 6000)
    assert small["problem_statement"].startswith("s" * 6000) and len(json.dumps(small)) <= 40000


def test_calibrate_records_per_row_errors_and_excludes_them_from_denominators(monkeypatch):
    from swesim.stages import judge
    pool = sb.pool_a(CSV)[:3]
    others = [i for i in sb.annotation_rows(CSV) if float(sb.annotation_rows(CSV)[i]["underspecified"]) < 2][:3]
    ids = pool + others
    rows = {i: {"instance_id": i} for i in ids}

    def flaky(row, model_id):
        if row["instance_id"] == pool[0]:
            raise ValueError("candidate exceeds authoring context budget even after trimming")
        if row["instance_id"] == others[0]:
            raise RuntimeError("provider timeout")
        return {"verified_rubric_score": 3 if row["instance_id"] in pool else 0}
    monkeypatch.setattr(judge, "judge_candidate", flaky)
    out = judge.calibrate(Path(CSV), rows, "fake/model", n=6)
    assert out["n_requested"] == 6 and out["n_judged"] == 4 and out["n_errored"] == 2 and out["n"] == 4
    assert {e["instance_id"] for e in out["errors"]} == {pool[0], others[0]} and all("ValueError" in e["error"] or "RuntimeError" in e["error"] for e in out["errors"])
    assert out["confusion"] == {"tp": 2, "fp": 0, "tn": 2, "fn": 0} and out["agreement"] == 1.0 and len(out["per_instance"]) == 4
    assert {p["instance_id"] for p in out["per_instance"]}.isdisjoint({e["instance_id"] for e in out["errors"]})


def test_ground_gaps_requires_verbatim_evidence_and_grounded_resolutions():
    from swesim.stages.judge import accept, ground_gaps
    row = _row()
    base = {"derivable_from_repo": False, "consequential": True, "category": "behavior"}
    gaps = [
        {"id": "g1", "source": "test-exception", "text": "tests expect ValueError", "resolved_by": "thread", "resolution_excerpt": "raise a ValueError when the dim is missing", **base},
        {"id": "g2", "source": "proposed", "text": "message names the dim", "evidence": "match=\"dimension 'z' not found\"", "resolved_by": "tests", "resolution_excerpt": "", **base},
        {"id": "g3", "source": "proposed", "text": "invented", "evidence": "raise TypeError('nope')", "resolved_by": "tests", **base},                 # not in tests/patch: dropped
        {"id": "g4", "source": "proposed", "text": "dup", "evidence": "match=\"dimension  'z' not found\"", "resolved_by": "tests", **base},            # same evidence after _norm: dropped
        {"id": "g5", "source": "proposed", "text": "short", "evidence": "x\n", "resolved_by": "tests", **base},                                          # below the 12-char floor: dropped
        {"id": "g6", "source": "proposed", "text": "claims thread", "evidence": "ds.weighted(w).mean('z')", "resolved_by": "thread",
         "resolution_excerpt": "the maintainer agreed to raise", **base},                                                                             # thread claim not in thread -> tests
        {"id": "g7", "source": "test-literal", "text": "literal", "resolved_by": "tests", **base},                                                     # extracted, no test evidence -> none
        {"id": "g8", "source": "diff-branch", "text": "invented source", "resolved_by": "none", **base},                                              # non-extracted source, no evidence: dropped
        "garbage",
    ]
    kept, stats = ground_gaps(gaps, row)
    assert [g["id"] for g in kept] == ["g1", "g2", "g6", "g7"]
    assert stats == {"gaps_proposed": 2, "gaps_dropped": 5, "resolution_downgraded": 2}
    by = {g["id"]: g for g in kept}
    assert by["g1"]["resolved_by"] == "thread" and by["g2"]["resolved_by"] == "tests" and by["g6"]["resolved_by"] == "tests" and by["g7"]["resolved_by"] == "none"
    assert by["g6"]["source"] == "proposed" and by["g2"]["evidence_grounded"] is True and by["g1"]["evidence_grounded"] is False
    assert accept({"self_contained": True, "gaps": [by["g2"]]}) and accept({"self_contained": True, "gaps": [by["g1"]]})
    assert not accept({"self_contained": True, "gaps": [by["g7"]]}) and not accept({"self_contained": False, "gaps": [by["g2"]]})
    assert ground_gaps([], row) == ([], {"gaps_proposed": 0, "gaps_dropped": 0, "resolution_downgraded": 0})


def test_tests_for_excerpt_and_synthesized_authored_facts():
    from swesim.stages.draft_spec import tests_for_excerpt, tests_resolved_facts
    row = _row()
    f2p = [F2P, "xarray/tests/test_weighted.py::test_new", "xarray/tests/test_other.py::test_x"]
    assert tests_for_excerpt(row["test_patch"], "match=\"dimension 'z' not found\"", f2p) == [F2P]                       # enclosing test function
    assert tests_for_excerpt(row["test_patch"], "def test_new(): pass", f2p) == ["xarray/tests/test_weighted.py::test_new"]
    assert tests_for_excerpt(row["test_patch"], "not in the test patch at all", f2p) == sorted(f2p)                   # evidence from the source patch: every F2P test
    facts = tests_resolved_facts(row, _judged())
    assert len(facts) == 1 and facts[0]["id"] == "tests-001" and facts[0]["provenance"] == "authored" and facts[0]["discovery"] == "on-request"
    assert facts[0]["source_excerpt"] == "match=\"dimension 'z' not found\"" and facts[0]["source_url"] == "https://github.com/pydata/xarray/pull/5362"
    assert facts[0]["tests"] == [F2P] and facts[0]["kind"] == "behavior" and "g2" in facts[0]["authored_rationale"] and "What should the message say?" in facts[0]["authored_rationale"]
    assert facts[0]["text"] == "The error message names the missing dimension"
    j = _judged(); j["gaps"][1]["evidence"] = "raise TypeError('nope')"                                             # evidence re-checked by code: fabricated -> no fact
    assert tests_resolved_facts(row, j) == []
    j = _judged(); j["gaps"][1]["derivable_from_repo"] = True
    assert tests_resolved_facts(row, j) == []
    assert tests_resolved_facts(_row(thread={}), _judged())[0]["source_url"] == "https://github.com/pydata/xarray/pull/5362"   # PR url fallback from the instance id


def test_authored_facts_must_ground_in_tests_or_patch():
    from swesim.stages.draft_spec import ledger_violations
    assert ledger_violations(_draft_llm(), _row()) == []                                                            # f04 quotes the test patch
    d = _draft_llm(); d["ledger"][3]["source_excerpt"] = "Reviewer tone is terse and precise"                          # not in tests/patch
    assert any("f04" in v and "merged tests/patch" in v for v in ledger_violations(d, _row()))
    d = _draft_llm(); d["ledger"][3]["source_excerpt"] = None
    assert any("f04" in v and "too short" in v for v in ledger_violations(d, _row()))
    d = _draft_llm(); d["ledger"][3]["source_url"] = "https://github.com/other/repo/pull/1"
    assert any("f04" in v and "source_url" in v for v in ledger_violations(d, _row()))
    d = _draft_llm(); d["ledger"].append({**d["ledger"][3], "id": "tests-001"})
    assert any("tests-001" in v and "reserved" in v for v in ledger_violations(d, _row()))
    d = _draft_llm(); d["ledger"][0]["discovery"] = d["ledger"][3]["discovery"] = "repo-derivable"                     # no model fact links a F2P test any more ...
    assert any("on-request" in v for v in ledger_violations(d, _row()))
    assert not any("on-request" in v for v in ledger_violations(d, _row(), [{"discovery": "on-request", "tests": [F2P]}]))   # ... but a synthesized one does


# ---------------------------------------------------------------- urgent drafter fixes: diff-body corpus, on-request facts carry tests, auto-link fallback

SPHINX_TEST_PATCH = ("diff --git a/tests/test_ext_autodoc.py b/tests/test_ext_autodoc.py\nindex 1111111..2222222 100644\n--- a/tests/test_ext_autodoc.py\n+++ b/tests/test_ext_autodoc.py\n"
                     "@@ -10,3 +10,6 @@ def test_other():\n     pass\n+def test_table_rows(app):\n+    content = app.render()\n+    rows = content.splitlines()\n"
                     "+    assert len(content.splitlines()) == 11\n+    assert len(rows) == 11\n-    assert old_value == 7\n\\ No newline at end of file\n")


def test_diff_body_text_strips_markers_and_headers():
    from swesim.stages.judge import diff_body_lines, diff_body_text
    body = diff_body_text(SPHINX_TEST_PATCH)
    assert body == ("    pass\ndef test_table_rows(app):\n    content = app.render()\n    rows = content.splitlines()\n"
                    "    assert len(content.splitlines()) == 11\n    assert len(rows) == 11")
    assert "diff --git" not in body and "index " not in body and "+++" not in body and "@@" not in body and "No newline" not in body and "old_value" not in body
    assert "    assert old_value == 7" in diff_body_lines(SPHINX_TEST_PATCH, include_removed=True)
    assert diff_body_text("plain text, not a diff\nsecond line") == "plain text, not a diff\nsecond line"


def test_grounding_excerpt_may_span_adjacent_added_lines():
    """sphinx-doc__sphinx-9467: `assert len(content.splitlines()) == 11 assert len(rows) == 11` sits on two adjacent `+` lines."""
    from swesim.stages.draft_spec import ledger_violations
    from swesim.stages.judge import ground_gaps
    f2p = ["tests/test_ext_autodoc.py::test_table_rows"]
    row = _row(repo="sphinx-doc/sphinx", test_patch=SPHINX_TEST_PATCH, fail_to_pass=f2p, thread=_thread("sphinx-doc/sphinx", 9467))
    excerpt = "assert len(content.splitlines()) == 11 assert len(rows) == 11"
    d = _draft_llm(repo="sphinx-doc/sphinx", pr=9467)
    d["ledger"] = [{"id": "f01", "text": "The rendered table has eleven rows", "kind": "behavior", "provenance": "authored", "source_excerpt": excerpt,
                    "source_url": "https://github.com/sphinx-doc/sphinx/pull/9467", "authored_rationale": "only the tests pin the count", "discovery": "on-request", "tests": f2p}]
    assert ledger_violations(d, row) == []
    d["ledger"][0]["source_excerpt"] = "assert len(rows) == 11 assert old_value == 7"     # removed (`-`) lines are not evidence of the merged state
    assert any("f01" in v and "verbatim" in v for v in ledger_violations(d, row))
    gap = {"id": "g9", "source": "proposed", "text": "row count", "evidence": excerpt, "question": "how many rows?", "derivable_from_repo": False, "consequential": True,
           "resolved_by": "tests", "category": "behavior"}
    kept, stats = ground_gaps([gap], row)
    assert kept and kept[0]["evidence_grounded"] is True and kept[0]["resolved_by"] == "tests" and stats["gaps_dropped"] == 0


MPL_TEST_PATCH = ("diff --git a/lib/matplotlib/tests/test_figure.py b/lib/matplotlib/tests/test_figure.py\n--- a/lib/matplotlib/tests/test_figure.py\n+++ b/lib/matplotlib/tests/test_figure.py\n"
                  "@@ -1 +1,9 @@\n x\n+def test_subplot_mosaic_width_ratios():\n+    fig = plt.figure()\n+    fig.subplot_mosaic([[\"A\", [[\"B\"], [\"C\"]]]], width_ratios=[2, 1])\n"
                  "+    assert fig.axes\n+def test_subplot_mosaic_height_ratios_nested():\n+    fig = plt.figure()\n+    fig.subplot_mosaic([[\"A\"], [[[\"B\", \"C\"]]]], height_ratios=[1, 3])\n"
                  "+def test_unrelated_colorbar_label():\n+    assert plt.colorbar().ax.get_ylabel() == \"\"\n")
MPL_F2P = ["lib/matplotlib/tests/test_figure.py::test_subplot_mosaic_width_ratios", "lib/matplotlib/tests/test_figure.py::test_subplot_mosaic_height_ratios_nested[a]",
           "lib/matplotlib/tests/test_figure.py::test_unrelated_colorbar_label"]


def test_auto_link_tests_by_shared_content_words():
    from swesim.stages.draft_spec import auto_link_tests, content_words
    assert content_words("Nested subplot_mosaic layouts must apply width_ratios while nothing is None") == {"nested", "subplot", "mosaic", "layouts", "apply", "width", "ratios", "nothing"}
    assert auto_link_tests("Nested subplot_mosaic layouts must apply width_ratios to every level", MPL_TEST_PATCH, MPL_F2P) == sorted([MPL_F2P[0], MPL_F2P[1]])   # mosaic+subplot shared with both; width+ratios with the first
    assert auto_link_tests("Only the height_ratios of nested mosaics matter here", MPL_TEST_PATCH, MPL_F2P) == [MPL_F2P[1]]
    assert auto_link_tests("The maintainer prefers a friendly wording", MPL_TEST_PATCH, MPL_F2P) == []                  # no shared content words
    assert auto_link_tests("figure figure figure", MPL_TEST_PATCH, MPL_F2P) == []                                    # one distinct shared word is not enough
    assert auto_link_tests("", MPL_TEST_PATCH, MPL_F2P) == [] and auto_link_tests("assert while return", MPL_TEST_PATCH, MPL_F2P) == []   # keywords never count


def test_on_request_facts_must_carry_f2p_tests_named_in_the_violation():
    from swesim.stages.draft_spec import ledger_violations
    d = _draft_llm(); d["ledger"][0]["tests"] = []
    bad = ledger_violations(d, _row())
    assert any(v.startswith("f01:") and "on-request" in v and F2P in v for v in bad)
    d["ledger"][0]["tests"] = [P2P]                                                       # a PASS_TO_PASS link does not satisfy the rule
    assert any(v.startswith("f01:") for v in ledger_violations(d, _row()))
    # withheld facts are on-request without tests by design: exempt via require_f2p_link=False
    w = {"id": "withheld-001", "text": "t", "kind": "context", "provenance": "original-issue", "source_excerpt": "Traceback shows no error at all.",
         "source_url": "https://github.com/pydata/xarray/issues/5360", "discovery": "on-request", "tests": []}
    assert ledger_violations({"ledger": [w]}, _row(), corpus_by_fact={"withheld-001": w["source_excerpt"]}, allow_reserved=True, require_f2p_link=False) == []


def test_draft_lists_f2p_ids_and_auto_links_on_request_facts_before_failing(monkeypatch, tmp_path):
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    d = _draft_llm(); d["ledger"][0].update(tests=[], text="Raise ValueError with the dimension not found message")   # shares valueerror/dimension/found with the test body
    calls = _patch_llm(monkeypatch, d, _withhold_llm())
    p = draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    assert next(f for f in spec.ledger if f.id == "f01").tests == [F2P]
    meta = json.loads((tmp_path / "s" / "draft.meta.json").read_text())
    assert meta["auto_linked"] == ["f01"]
    draft_calls = [c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]]
    assert len(draft_calls) == 2                                                                        # correction round happened before the fallback
    assert draft_calls[0]["messages"][0]["content"].startswith("FAIL_TO_PASS ids") and F2P in draft_calls[0]["messages"][0]["content"].splitlines()[0]
    correction = draft_calls[1]["messages"][-1]["content"]
    assert "Correct these violations" in correction and "f01:" in correction and F2P in correction
    # no shared content words: the fallback cannot link, so the draft fails as before
    d = _draft_llm(); d["ledger"][0].update(tests=[], text="The maintainer prefers a friendly wording")
    _patch_llm(monkeypatch, d, _withhold_llm())
    with pytest.raises(ValueError, match="grounding validation after two attempts.*f01: every on-request fact must carry"):
        draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s2")
    assert not (tmp_path / "s2" / "scenario.draft.json").exists()


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
    assert out["n_requested"] == out["n_judged"] == 6 and out["n_errored"] == 0 and out["errors"] == []


# ---------------------------------------------------------------- draft_spec

def test_withheld_brief_has_no_excerpts(monkeypatch):
    from swesim.stages import draft_spec
    issue = 'Import fails\nThe importer stops on the first row.\nTraceback: ERROR42 raised in importer\nrun_import("bad.csv") crashes at once\nWe expected the whole file to load.'
    monkeypatch.setattr(draft_spec, 'chat_json', lambda *a, **k: {'withheld_brief': 'Import fails',
        'removed': [{'category': 'error_message', 'excerpt': 'ERROR42 raised in importer', 'kind': 'reproduction', 'discovery': 'on-request'},
                    {'category': 'reproduction', 'excerpt': 'run_import("bad.csv") crashes at once', 'kind': 'reproduction', 'discovery': 'on-request'}]})
    result = draft_spec.withhold(issue, 'fake/model')
    assert all(x['excerpt'] not in result['withheld_brief'] for x in result['removed'])
    assert result['withheld_brief'] == 'Import fails\nThe importer stops on the first row.\nWe expected the whole file to load.'


def test_withhold_retries_once_with_violations_then_fails(monkeypatch):
    """A selected span that does not match the issue verbatim gets one correction round, then ValueError."""
    from swesim.stages import draft_spec
    issue = 'Import fails\nThe importer stops on the first row.\nTraceback: ERROR42 raised in importer'
    attempts = []

    def not_verbatim(model_id, system, messages, schema, **kw):
        attempts.append(messages)
        return {'removed': [{'category': 'error_message', 'excerpt': 'Traceback: ERROR99 raised in importer', 'kind': 'reproduction', 'discovery': 'on-request'}]}
    monkeypatch.setattr(draft_spec, 'chat_json', not_verbatim)
    with pytest.raises(ValueError, match="withholding failed.*not a verbatim span"):
        draft_spec.withhold(issue, 'fake/model')
    assert len(attempts) == 2 and "Correct these violations" in attempts[1][-1]["content"] and "ERROR99" in attempts[1][-1]["content"]


WITHHOLD_ISSUE = ("Importer crashes on empty header\n"
                  "Running run_import(\"bad.csv\") stops at row 1. The importer must stay compatible with v1 files.\n"
                  "Traceback: ERROR42 raised in parse_header\n"
                  "> quoted from the mailing list: the expected result is an empty table\n"
                  "Expected: an empty table and no error.\n"
                  "Seen on version 2.3.1 with Python 3.12.")
CODE_ISSUE = ("Parser drops the last row\n"
              "When the file has no trailing newline the final row disappears. I expected all rows to be kept, e.g. three rows in the sample below.\n"
              "\n"
              "```python\n"
              "from importer import run_import\n"
              "\n"
              "rows = run_import(\"bad.csv\")\n"
              "    # indented comment\n"
              "```\n"
              "\n"
              "Traceback (most recent call last):\n"
              "  File \"importer.py\", line 12, in run_import\n"
              "    raise ParseError(\"row 3 truncated\")\n"
              "ParseError: row 3 truncated\n"
              "\n"
              "Seen with importer 2.3.1 on Python 3.12.\n")


def _select(*items):
    """Model selections without fact_text: the ledger text then defaults to the removed span itself."""
    return {"removed": [{"category": c, "excerpt": e, "kind": "context", "discovery": "on-request"} for c, e in items]}


def test_withhold_brief_is_computed_not_trusted(monkeypatch):
    """The model's own brief is ignored: it cannot add a requirement or silently delete an unlisted sentence."""
    from swesim.stages import draft_spec
    hostile = _select(("error_message", "Traceback: ERROR42 raised in parse_header"))
    hostile["withheld_brief"] = ("Importer crashes on empty header\nThe importer must break v1 files.\n"
                                 "Running run_import(\"bad.csv\") stops at row 1.")      # adds a contradictory requirement, drops the compatibility and expectation sentences
    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: hostile)
    res = draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")
    assert res["withheld_brief"] == WITHHOLD_ISSUE.replace("Traceback: ERROR42 raised in parse_header\n", "")   # byte-exact: the line and its newline
    assert "break v1" not in res["withheld_brief"] and "ERROR42" not in res["withheld_brief"]
    assert [r["excerpt"] for r in res["removed"]] == ["Traceback: ERROR42 raised in parse_header"] and res["removed"][0]["fact_text"] == res["removed"][0]["excerpt"]
    assert res["removed"][0]["selected"] == "Traceback: ERROR42 raised in parse_header" and res["stats"]["sentences_total"] == 7


def test_withhold_extends_to_sentence_boundaries_and_reconstructs(monkeypatch):
    from swesim.stages import draft_spec
    sel = _select(("expected_output", "empty table and no error"), ("version", "version 2.3.1 with Python"), ("reproduction", "stops at row 1"))
    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: sel)
    res = draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")
    assert [r["excerpt"] for r in res["removed"]] == ["Expected: an empty table and no error.", "Seen on version 2.3.1 with Python 3.12.",
                                                       "Running run_import(\"bad.csv\") stops at row 1."]
    assert all(WITHHOLD_ISSUE[r["start"]:r["end"]] == r["excerpt"] for r in res["removed"])
    ranges = sorted(res["deleted"] + res["kept"])
    assert "".join(WITHHOLD_ISSUE[a:b] for a, b in ranges) == WITHHOLD_ISSUE                       # kept pieces + deleted ranges partition the natural issue
    assert all(any(a <= o["start"] and o["end"] <= b for a, b in res["deleted"]) for r in res["removed"] for o in r["occurrences"])
    assert res["withheld_brief"] == "".join(WITHHOLD_ISSUE[a:b] for a, b in res["kept"])          # the brief IS the kept text, byte for byte
    assert res["withheld_brief"] == (WITHHOLD_ISSUE.replace("Running run_import(\"bad.csv\") stops at row 1. ", "")   # first sentence: splice, one space left
                                     .replace("Expected: an empty table and no error.\n", "")                          # whole line with its newline
                                     .replace("\nSeen on version 2.3.1 with Python 3.12.", ""))                        # last line: takes the newline before it
    assert res["stats"] == {"sentences_total": 7, "sentences_removed": 3, "fraction_removed": 0.429, "duplicate_selections": 0, "prompt_version": "withhold-v2"}


def test_withhold_preserves_code_blocks_tracebacks_and_paragraphs(monkeypatch):
    from swesim.stages import draft_spec
    sel = _select(("expected_output", "I expected all rows to be kept"), ("version", "importer 2.3.1 on Python 3.12"))
    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: sel)
    res = draft_spec.withhold(CODE_ISSUE, "fake/model")
    expected = CODE_ISSUE.replace(" I expected all rows to be kept, e.g. three rows in the sample below.", "").replace("Seen with importer 2.3.1 on Python 3.12.\n", "")
    assert res["withheld_brief"] == expected
    assert "```python\nfrom importer import run_import\n\nrows = run_import(\"bad.csv\")\n    # indented comment\n```\n" in res["withheld_brief"]   # fence, blank line, indentation intact
    assert "Traceback (most recent call last):\n  File \"importer.py\", line 12, in run_import\n    raise ParseError(\"row 3 truncated\")\nParseError: row 3 truncated\n" in res["withheld_brief"]
    assert "disappears.\n\n```python" in res["withheld_brief"]                                     # paragraph break kept, no trailing space after the splice
    assert res["removed"][0]["excerpt"] == "I expected all rows to be kept, e.g. three rows in the sample below."   # "e.g." does not end a sentence
    # a deleted line between two blank lines takes one blank line with it, so paragraphs keep a single separator
    issue = "T\nFirst paragraph here.\n\nMiddle paragraph to remove.\n\nLast paragraph here.\n"
    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: _select(("reproduction", "Middle paragraph to remove")))
    assert draft_spec.withhold(issue, "fake/model")["withheld_brief"] == "T\nFirst paragraph here.\n\nLast paragraph here.\n"
    # mid-line splices: middle sentence leaves one space; last sentence leaves no trailing space
    issue = "T\nA sentence here. Another middle one. Final sentence here.\nOne more line to keep.\nAnd another kept line."
    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: _select(("reproduction", "Another middle one")))
    assert draft_spec.withhold(issue, "fake/model")["withheld_brief"] == "T\nA sentence here. Final sentence here.\nOne more line to keep.\nAnd another kept line."
    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: _select(("reproduction", "Final sentence here")))
    assert draft_spec.withhold(issue, "fake/model")["withheld_brief"] == "T\nA sentence here. Another middle one.\nOne more line to keep.\nAnd another kept line."
    # deleting the last two lines of an issue without a trailing newline leaves none behind either
    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: _select(("reproduction", "One more line to keep"), ("reproduction", "And another kept line")))
    assert draft_spec.withhold(issue, "fake/model")["withheld_brief"] == "T\nA sentence here. Another middle one. Final sentence here."


def test_withhold_selection_minimums_malformed_items_duplicates_and_fact_text(monkeypatch):
    from swesim.stages import draft_spec
    attempts = []

    def fake(items):
        def chat(model_id, system, messages, schema, **kw):
            attempts.append(messages)
            return {"removed": items}
        return chat
    # a one-word selection cannot delete sentences
    monkeypatch.setattr(draft_spec, "chat_json", fake(_select(("reproduction", "the"))["removed"]))
    with pytest.raises(ValueError, match="selected span too short"):
        draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")
    # malformed selections enter the correction round instead of raising TypeError/AttributeError
    monkeypatch.setattr(draft_spec, "chat_json", fake(["just a string", {"category": "error_message", "excerpt": 42, "kind": "context", "discovery": "on-request"}, None]))
    attempts.clear()
    with pytest.raises(ValueError, match="withholding failed.*must be an object.*excerpt must be a string"):
        draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")
    assert len(attempts) == 2 and "Correct these violations" in attempts[1][-1]["content"]
    # duplicate selections of the same span collapse into one fact
    dup = _select(("error_message", "Traceback: ERROR42 raised in parse_header"), ("error_message", "ERROR42 raised in parse_header"))
    monkeypatch.setattr(draft_spec, "chat_json", fake(dup["removed"]))
    res = draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")
    assert len(res["removed"]) == 1 and res["stats"]["duplicate_selections"] == 1 and res["stats"]["sentences_removed"] == 1
    # fact_text: bounded, and content words must come from the span or the issue
    ok = _select(("error_message", "Traceback: ERROR42 raised in parse_header"))
    ok["removed"][0]["fact_text"] = "Traceback with ERROR42 raised in parse_header when the importer crashes."   # every 5+ letter word occurs in the span or issue
    monkeypatch.setattr(draft_spec, "chat_json", fake(ok["removed"]))
    assert draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")["removed"][0]["fact_text"] == ok["removed"][0]["fact_text"]
    foreign = _select(("error_message", "Traceback: ERROR42 raised in parse_header"))
    foreign["removed"][0]["fact_text"] = "The kubernetes scheduler raises ERROR42"
    monkeypatch.setattr(draft_spec, "chat_json", fake(foreign["removed"]))
    with pytest.raises(ValueError, match="introduces words absent.*kubernetes"):
        draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")
    long = _select(("error_message", "Traceback: ERROR42 raised in parse_header"))
    long["removed"][0]["fact_text"] = "ERROR42 " * 60
    monkeypatch.setattr(draft_spec, "chat_json", fake(long["removed"]))
    with pytest.raises(ValueError, match="longer than 400"):
        draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")


def test_withheld_facts_pass_through_ledger_violations_with_their_span_as_corpus():
    from swesim.stages.draft_spec import ledger_violations
    fact = {"id": "withheld-001", "text": "Traceback shows no error at all.", "kind": "reproduction", "provenance": "original-issue",
            "source_excerpt": "Traceback shows no error at all.", "source_url": "https://github.com/pydata/xarray/issues/5360", "discovery": "on-request", "tests": []}
    kw = {"corpus_by_fact": {"withheld-001": fact["source_excerpt"]}, "allow_reserved": True, "require_f2p_link": False}
    assert ledger_violations({"ledger": [fact]}, _row(), **kw) == []
    assert any("reserved" in v for v in ledger_violations({"ledger": [fact]}, _row(), corpus_by_fact=kw["corpus_by_fact"], require_f2p_link=False))
    assert any("source_url" in v for v in ledger_violations({"ledger": [{**fact, "source_url": "https://github.com/other/repo/issues/1"}]}, _row(), **kw))
    assert any("removed span" in v for v in ledger_violations({"ledger": [fact]}, _row(), corpus_by_fact={"withheld-001": "a different span entirely"}, allow_reserved=True, require_f2p_link=False))


def test_withhold_caps_removed_fraction_and_protects_title(monkeypatch):
    from swesim.stages import draft_spec
    too_much = _select(("reproduction", "stops at row 1"), ("error_message", "Traceback: ERROR42 raised in parse_header"), ("expected_output", "an empty table and no error"),
                       ("version", "with Python 3.12"), ("location", "quoted from the mailing list"))
    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: too_much)
    with pytest.raises(ValueError, match="removed 5 of 7 sentences; the cap is 60%"):
        draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")
    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: _select(("location", "crashes on empty header")))
    with pytest.raises(ValueError, match="title line cannot be withheld"):
        draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")
    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: {"removed": []})
    with pytest.raises(ValueError, match="no removed spans"):
        draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")


def test_withhold_matches_after_normalization_and_removes_every_occurrence(monkeypatch):
    from swesim.stages import draft_spec
    issue = "Dup title\nThe error is  Boom.\nUnrelated line here.\n> the error is boom.\nAnother fine line."
    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: _select(("error_message", "the ERROR is boom")))
    res = draft_spec.withhold(issue, "fake/model")
    assert res["withheld_brief"] == "Dup title\nUnrelated line here.\nAnother fine line."
    assert len(res["removed"]) == 1 and len(res["removed"][0]["occurrences"]) == 2 and res["removed"][0]["excerpt"] == "The error is  Boom."
    norm_text, idx = draft_spec._norm_map(issue)
    assert norm_text == draft_spec._norm(issue) and len(idx) == len(norm_text)
    norm_text, idx = draft_spec._norm_map(WITHHOLD_ISSUE)
    assert norm_text == draft_spec._norm(WITHHOLD_ISSUE)


def _row(**over):
    row = {"instance_id": "pydata__xarray-5362", "repo": "pydata/xarray", "base_commit": "a" * 40, "version": "0.18",
           "problem_statement": ISSUE, "hints_text": "", "fail_to_pass": [F2P], "pass_to_pass": [P2P],
           "patch": "diff --git a/xarray/core/weighted.py b/xarray/core/weighted.py\n--- a/xarray/core/weighted.py\n+++ b/xarray/core/weighted.py\n@@ -1 +1,2 @@\n x\n+y\n",
           "test_patch": ("diff --git a/xarray/tests/test_weighted.py b/xarray/tests/test_weighted.py\n--- a/xarray/tests/test_weighted.py\n+++ b/xarray/tests/test_weighted.py\n"
                          "@@ -1 +1,5 @@\n x\n+def test_weighted_operations_nonexistent_dim(a):\n+    with pytest.raises(ValueError, match=\"dimension 'z' not found\"):\n"
                          "+        ds.weighted(w).mean('z')\n+def test_new(): pass\n"),
           "image": sb.image_for("pydata__xarray-5362"), "repo_dir": "/testbed",
           "annotation": {"underspecified": "2.0", "underspecified_notes": "n", "false_negative": "0.0", "false_negative_notes": "", "difficulty": "15 min - 1 hour"},
           "thread": _thread("pydata/xarray", 5362), "probe": _probe_meta()}
    row.update(over); return row


def _probe_meta():
    """Offline probe metadata as `probe_candidates` attaches it: the measured contract, not SWE-bench's network-era lists."""
    return {"kind": "image", "probe_dir": "/ws/jobs/probe/pydata--xarray-5362/trial-1", "image_digest": "sha256:" + "e" * 64, "repo_dir": "/testbed",
            "statuses": {"base": {}, "test": {F2P: "failed", P2P: "passed"}, "gold": {F2P: "passed", P2P: "passed"}},
            "dropped": {"network_or_env": [], "gold_regressions": []}, "counts": {"fail_to_pass": 1, "pass_to_pass": 1, "network_or_env": 0, "gold_regressions": 0}}


def _thread(repo, pr):
    """A PR thread whose review text and body contain the spans the fake drafter cites (grounding)."""
    url = f"https://github.com/{repo}/pull/{pr}"
    return {"pr_url": url, "number": pr, "pr_title": "Raise on missing weights dim", "pr_body": "Adds a _check_dim helper before reducing.",
            "author": "dev", "merged_at": "2021-06-01T00:00:00Z", "gold_commit": "b" * 40, "pr_created_at": "2021-05-20T00:00:00Z",
            "reviews": [{"user": "rev", "created_at": "2021-05-21T00:00:00Z", "body": "Please raise a  ValueError\nwhen the dim is missing.", "url": url + "#pullrequestreview-1", "path": None}],
            "pr_comments": [], "review_comments": [], "linked_prs": [],
            "linked_issues": [{"issue_url": f"https://github.com/{repo}/issues/5360", "number": 5360, "title": "t", "body": "b", "created_at": "2021-05-01T00:00:00Z", "comments": []}],
            "issue_url": f"https://github.com/{repo}/issues/5360", "issue_comments": []}


def _judged(**over):
    j = {"gaps": [{"id": "g1", "text": "tests expect ValueError", "source": "test-exception", "evidence": "pytest.raises(ValueError", "question": "Which exception?",
                   "derivable_from_repo": False, "consequential": True, "resolved_by": "thread", "resolution_excerpt": "raise a ValueError when the dim is missing", "category": "behavior"},
                  {"id": "g2", "text": "The error message names the missing dimension", "source": "proposed", "evidence": "match=\"dimension 'z' not found\"",
                   "question": "What should the message say?", "derivable_from_repo": False, "consequential": True, "resolved_by": "tests",
                   "resolution_excerpt": "match=\"dimension 'z' not found\"", "category": "behavior"}],
         "gaps_proposed": 1, "gaps_extracted": 1, "gaps_dropped": 0, "resolution_downgraded": 0,
         "verified_rubric_score": 2, "complexity": 2, "test_behavioralness": 4, "self_contained": True, "alternatives_accepted": True, "keep": True, "structural_tests": False,
         "prompt_version": "judge-v2", "model_id": "fake/judge"}
    j.update(over); return j


def _draft_llm(alt=None, repo="pydata/xarray", pr=5362):
    url = f"https://github.com/{repo}/pull/{pr}"
    return {"briefs": [{"profile": "natural", "text": "ignored", "provenance": "original-issue"}],
            "ledger": [{"id": "f01", "text": "Raise ValueError when the weights dim is missing", "kind": "behavior", "provenance": "pr-review",
                        "source_excerpt": "raise a ValueError when the dim is missing", "source_url": url + "#pullrequestreview-1",  # whitespace differs from the review text
                        "authored_rationale": None, "discovery": "on-request", "tests": [F2P]},
                       {"id": "f02", "text": "_check_dim helper name", "kind": "interface", "provenance": "pr-thread", "source_excerpt": "Adds a _check_dim helper",
                        "source_url": url, "authored_rationale": None, "discovery": "author-only", "tests": []},
                       {"id": "f03", "text": "Reporter expects a ValueError", "kind": "behavior", "provenance": "original-issue", "source_excerpt": "Expected: raise ValueError.",
                        "source_url": f"https://github.com/{repo}/issues/5360", "authored_rationale": None, "discovery": "brief", "tests": []},
                       {"id": "f04", "text": "The tests pin the exact error message", "kind": "behavior", "provenance": "authored",
                        "source_excerpt": "with pytest.raises(ValueError, match=\"dimension 'z' not found\"):", "source_url": url,   # authored = pinned only by the merged tests
                        "authored_rationale": "only the merged tests state the message", "discovery": "on-request", "tests": [F2P]}],
            "operations": [{"type": "clarification", "target": "f01", "rationale": "issue never names the exception"}],
            "persona": {"summary": "xarray maintainer", "tone": "terse", "disclosure_style": "answers what is asked",
                        "derived_from_urls": [url], "role": "maintainer"},
            "requirements": [{"id": "r01", "text": "raise ValueError", "fact_id": "f01", "tests": [F2P], "source_url": url + "#pullrequestreview-1"}],
            "checks": [{"id": "c01", "description": "user-level reproducer", "command": "python -c 'import xarray'", "expected_observation": "ValueError"}],
            "groups": [{"id": "g01", "weight": 0.5, "tests": [F2P]}],  # weights are renormalized to sum to 1
            "plausible_wrong": {"name": "keyerror", "rationale": "wrong exception type", "diff": "diff --git a/xarray/core/weighted.py b/xarray/core/weighted.py\n--- a/xarray/core/weighted.py\n+++ b/xarray/core/weighted.py\n@@ -1 +1,2 @@\n x\n+raise KeyError\n"},
            "alternative_correct": alt,
            "protected_paths_extra": ["xarray/tests/data/w.nc"]}


def _withhold_llm():
    return {"withheld_brief": "Weighted mean silently ignores missing dims\nCalling a weighted reduction with a missing dim returns a result.",
            "removed": [{"category": "expected_output", "excerpt": "Expected: raise ValueError.", "kind": "behavior", "discovery": "on-request"},
                        {"category": "error_message", "excerpt": "Traceback shows no error at all.", "kind": "reproduction", "discovery": "on-request"},
                        {"category": "location", "excerpt": "Calling ds.weighted(w).mean('z') with a dim", "kind": "context", "discovery": "on-request"}]}


def _patch_llm(monkeypatch, draft_out, withhold_out):
    from swesim.stages import draft_spec
    calls = []

    def fake_chat(model_id, system, messages, schema, **kw):
        calls.append({"model": model_id, "system": system, "messages": messages, "kw": kw})
        return withhold_out if WITHHOLD_PROMPT_MARK in system else draft_out
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
    # runner: exact node ids; the verifier substitutes {junit_path} with str.replace, so braces in ids pass through literally
    r = spec.contract.runner
    assert r.kind == "pytest-ra" and r.command.startswith("/opt/miniconda3/envs/testbed/bin/python -I /tests/run_pytest.py -rA -p no:cacheprovider --junitxml={junit_path} ")
    substituted = r.command.replace("{junit_path}", "/tmp/j.xml")
    assert "--junitxml=/tmp/j.xml" in substituted and shlex.quote(F2P) in substituted and P2P in substituted
    assert "{{" not in r.command and "}}" not in r.command and r.command.count("{") == 2  # {junit_path} + the literal brace in F2P
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
    # the brief is computed by code: title plus every sentence not selected; operations carry the exact spans and offsets
    assert spec.briefs[1].text == "Weighted mean silently ignores missing dims\nI am using xarray 0.18 on Linux.\nA small reproducer is attached below."
    assert wf["withheld-003"].source_excerpt == "Calling ds.weighted(w).mean('z') with a dim 'z' that does not exist returns a result."  # selection extended to its sentence
    assert wf["withheld-001"].text == wf["withheld-001"].source_excerpt   # no fact_text given: the ledger text is the span itself
    for o, f in zip(ops, wf.values()):
        start, end = ISSUE.index(f.source_excerpt), ISSUE.index(f.source_excerpt) + len(f.source_excerpt)
        assert f"natural[{start}:{end}]" in o.rationale and repr(f.source_excerpt) in o.rationale
    # code-synthesized fact for the judge gap that only the merged tests resolve: authored, on-request, linked to the test citing it
    tf = next(f for f in spec.ledger if f.id == "tests-001")
    assert tf.provenance == "authored" and tf.discovery == "on-request" and tf.tests == [F2P] and tf.source_url.endswith("/pull/5362")
    assert tf.source_excerpt == "match=\"dimension 'z' not found\"" and "g2" in tf.authored_rationale and "resolved_by=tests" in tf.authored_rationale
    assert [f.id for f in spec.ledger] == ["f01", "f02", "f03", "f04", "tests-001", "withheld-001", "withheld-002", "withheld-003"]
    meta = json.loads((out_dir / "draft.meta.json").read_text())
    assert meta["auto_linked"] == [] and [r["fact_id"] for r in meta["withholding"]["removed"]] == ["withheld-001", "withheld-002", "withheld-003"]
    assert meta["withholding"]["stats"]["sentences_removed"] == 3 and meta["withholding"]["kept"]
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
    first = calls[0]["messages"][0]["content"]
    assert first.startswith("FAIL_TO_PASS ids") and json.loads(first.split("\n\n", 1)[1])["candidate"]["instance_id"] == "pydata__xarray-5362"


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
    leaky = _withhold_llm(); leaky["removed"][0]["excerpt"] = "Expected: raise TypeError."   # not a verbatim span of the issue
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
    _patch_llm(monkeypatch, _draft_llm(repo="py-pdf/pypdf", pr=3400), _withhold_llm())
    thread = {**_thread("py-pdf/pypdf", 3400), "gold_commit": None, "linked_issues": [], "issue_url": None}
    row = _row(source="github-repo", instance_id="py-pdf__pypdf-3400", repo="py-pdf/pypdf", image=None, repo_dir="/app",
               dockerfile="FROM python@sha256:abc\nRUN echo hi\n", test_python="/usr/local/bin/python",
               annotation={"underspecified": "", "underspecified_notes": "", "false_negative": "", "false_negative_notes": "", "difficulty": ""},
               thread=thread)
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
    assert json.loads(calls[0]["messages"][0]["content"].split("\n\n", 1)[1])["candidate"]["pass_to_pass_count"] == 300


def test_draft_requires_offline_probe_metadata(monkeypatch, tmp_path):
    """SWE-bench's FAIL_TO_PASS/PASS_TO_PASS were produced with network access; drafting needs the offline-measured contract."""
    from swesim.stages import draft_spec
    calls = _patch_llm(monkeypatch, _draft_llm(), _withhold_llm())
    with pytest.raises(ValueError, match="no offline probe metadata"):
        draft_spec.draft(_row(probe=None), _judged(), "fake/model", tmp_path / "s1")
    with pytest.raises(ValueError, match="no offline probe metadata"):
        draft_spec.draft(_row(probe={"error": "unreliable base/gold probe"}), _judged(), "fake/model", tmp_path / "s2")   # a failed probe is not a measurement
    assert calls == [] and not (tmp_path / "s1").exists()                                                                    # refused before any LLM call or file
    p = draft_spec.draft(_row(probe=None), _judged(), "fake/model", tmp_path / "s3", require_probe=False)
    meta = json.loads((tmp_path / "s3" / "draft.meta.json").read_text())
    assert p.exists() and meta["probe"] == {"unmeasured": True}
    draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s4")
    meta = json.loads((tmp_path / "s4" / "draft.meta.json").read_text())
    assert meta["probe"]["kind"] == "image" and meta["probe"]["probe_dir"].endswith("trial-1") and meta["probe"]["counts"]["fail_to_pass"] == 1


def test_draft_trims_an_oversized_candidate_for_the_model_but_grounds_against_the_full_row(monkeypatch, tmp_path):
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    calls = _patch_llm(monkeypatch, _draft_llm(), _withhold_llm())
    big = _row(patch=_row()["patch"] + "\n" + "+# " + "x" * 130000 + "\n")            # oversized fix: trimmed in the prompt only
    p = draft_spec.draft(big, _judged(), "fake/model", tmp_path / "s")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    assert (tmp_path / "s" / "assets" / "oracle.diff").read_text() == big["patch"] and spec.assets["oracle.diff"].bytes == len(big["patch"].encode())
    sent = json.loads(calls[0]["messages"][0]["content"].split("\n\n", 1)[1])["candidate"]
    assert sent["trimmed"] == {"patch": 30000} and len(calls[0]["messages"][0]["content"]) <= draft_spec.CONTEXT_BUDGET_CHARS


def test_draft_fails_without_a_source_url_for_withheld_facts(monkeypatch, tmp_path):
    from swesim.stages import draft_spec
    _patch_llm(monkeypatch, _draft_llm(), _withhold_llm())
    thread = {**_thread("pydata/xarray", 5362), "pr_url": None, "linked_issues": []}
    with pytest.raises(ValueError, match="source URL"):
        draft_spec.draft(_row(thread=thread), _judged(), "fake/model", tmp_path / "s")


# ---------------------------------------------------------------- fix round 1: grounding, model separation, discovery restriction

def test_ledger_violations_ground_every_non_authored_fact():
    from swesim.stages.draft_spec import ledger_violations
    assert ledger_violations(_draft_llm(), _row()) == []
    d = _draft_llm(); d["ledger"][0]["source_excerpt"] = "raise a TypeError when the dim is missing"   # paraphrase, not in any review
    assert any("f01" in v and "verbatim" in v for v in ledger_violations(d, _row()))
    d = _draft_llm(); d["ledger"][0]["source_url"] = "https://github.com/other/repo/pull/1#r1"          # URL outside the repo
    assert any("f01" in v and "source_url" in v for v in ledger_violations(d, _row()))
    d = _draft_llm(); d["ledger"][1]["provenance"] = "issue-thread"                                     # "_check_dim helper" is PR-body text, not issue text
    assert any("f02" in v for v in ledger_violations(d, _row()))
    d = _draft_llm(); d["ledger"][2]["source_excerpt"] = "Expected: raise KeyError."                    # not in the original issue
    assert any("f03" in v for v in ledger_violations(d, _row()))
    d = _draft_llm(); d["ledger"][0]["discovery"] = d["ledger"][3]["discovery"] = "repo-derivable"      # nothing on-request links a F2P test any more
    assert any("on-request" in v for v in ledger_violations(d, _row()))
    d = _draft_llm(); d["ledger"].append({"id": "f05", "text": "n", "kind": "context", "provenance": "annotator-note", "source_excerpt": "The note says n", "source_url": "https://github.com/pydata/xarray/issues/5360", "discovery": "on-request", "tests": [F2P]})
    assert ledger_violations(d, _row(annotation={**_row()["annotation"], "underspecified_notes": "The note says n"})) == []  # annotator notes are a valid corpus
    assert any("f05" in v for v in ledger_violations(d, _row()))                                        # blank annotation notes cannot ground a fact
    # hints_text counts as issue-thread text (SWE-bench issue comments)
    d = _draft_llm(); d["ledger"][1].update(provenance="issue-thread", source_excerpt="fixed in the next release")
    assert ledger_violations(d, _row(hints_text="This will be fixed  in the\nnext release")) == []


def test_ledger_violations_require_a_minimum_excerpt(monkeypatch):
    """One common word is a substring of any thread; grounding needs >= 12 chars and >= 3 words after normalization."""
    from swesim.stages.draft_spec import MIN_EXCERPT_CHARS, MIN_EXCERPT_TOKENS, ledger_violations
    assert (MIN_EXCERPT_CHARS, MIN_EXCERPT_TOKENS) == (12, 3)
    for short in ("a", "the", "Adds a", "_check_dim helper", "  the   dim  "):
        d = _draft_llm(); d["ledger"][1]["source_excerpt"] = short
        assert any("f02" in v and "too short" in v for v in ledger_violations(d, _row())), short
    d = _draft_llm(); d["ledger"][1]["source_excerpt"] = "Adds a _check_dim helper"      # 24 chars, 4 words, in the PR body
    assert ledger_violations(d, _row()) == []
    d = _draft_llm(); d["ledger"][1]["source_excerpt"] = "a _check_dim helper"           # 19 chars, 3 words: the minimum passes
    assert ledger_violations(d, _row()) == []


def test_ledger_norm_strips_blockquotes_and_casefolds():
    from swesim.stages.draft_spec import _norm, ledger_violations
    assert _norm("> raise a ValueError\n> when the dim is missing") == "raise a valueerror when the dim is missing"
    assert _norm(">> nested\n >quote  here") == "nested quote here" and _norm("x > y") == "x > y"   # only line-leading markers go
    quoted = {**_thread("pydata/xarray", 5362)}
    quoted["reviews"] = [{**quoted["reviews"][0], "body": "> raise a ValueError\n> when the dim is missing\n\nAgreed, and use `match=` for the message."}]
    assert ledger_violations(_draft_llm(), _row(thread=quoted)) == []                    # excerpt without the `>` markers still grounds
    d = _draft_llm(); d["ledger"][0]["source_excerpt"] = "agreed, and use `match=` for the message"   # case differs from the review text
    assert ledger_violations(d, _row(thread=quoted)) == []
    d = _draft_llm(); d["ledger"][0]["source_excerpt"] = "agreed, and use `match=` for the subject"
    assert any("f01" in v and "verbatim" in v for v in ledger_violations(d, _row(thread=quoted)))


def test_draft_rejects_ungrounded_facts_after_one_correction_round(monkeypatch, tmp_path):
    from swesim.stages import draft_spec
    d = _draft_llm(); d["ledger"][0]["source_excerpt"] = "raise a TypeError when the dim is missing"
    calls = _patch_llm(monkeypatch, d, _withhold_llm())
    with pytest.raises(ValueError, match="grounding validation after two attempts.*f01"):
        draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s")
    draft_calls = [c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]]
    assert len(draft_calls) == 2 and "Correct these violations" in draft_calls[1]["messages"][-1]["content"] and "f01" in draft_calls[1]["messages"][-1]["content"]
    assert not (tmp_path / "s" / "scenario.draft.json").exists()


def test_draft_requires_distinct_judge_and_draft_models(monkeypatch, tmp_path):
    from swesim.stages import draft_spec
    calls = _patch_llm(monkeypatch, _draft_llm(), _withhold_llm())
    with pytest.raises(ValueError, match="judge and draft models must differ"):
        draft_spec.draft(_row(), _judged(model_id="fake/model"), "fake/model", tmp_path / "s")
    assert calls == []  # rejected before any LLM call


def test_withhold_restricts_discovery_to_on_request_or_repo_derivable(monkeypatch):
    from swesim.stages import draft_spec
    out = {"withheld_brief": "Import fails", "removed": [{"category": "error_message", "excerpt": "ERROR42", "kind": "reproduction", "discovery": "author-only"}]}
    attempts = []
    monkeypatch.setattr(draft_spec, "chat_json", lambda model_id, system, messages, schema, **kw: attempts.append(messages) or out)
    with pytest.raises(ValueError, match="withholding failed"):
        draft_spec.withhold("Import fails\nTraceback: ERROR42", "fake/model")
    assert len(attempts) == 2 and "invalid discovery" in attempts[1][-1]["content"]


def test_accept_requires_explicit_non_derivable_gap():
    from swesim.stages.judge import accept
    gap = {"consequential": True, "resolved_by": "thread"}
    assert not accept({"gaps": [gap], "self_contained": True})                                   # derivable_from_repo missing
    assert not accept({"gaps": [{**gap, "derivable_from_repo": None}], "self_contained": True})
    assert accept({"gaps": [{**gap, "derivable_from_repo": False}], "self_contained": True})
    assert not accept({"gaps": [{**gap, "derivable_from_repo": False}], "self_contained": "yes"})
    assert accept({"gaps": [{**gap, "derivable_from_repo": False, "resolved_by": "tests"}], "self_contained": True})      # pinned only by the merged tests
    assert not accept({"gaps": [{**gap, "derivable_from_repo": False, "resolved_by": "none"}], "self_contained": True})
    assert not accept({"gaps": [{**gap, "derivable_from_repo": False, "resolved_by": "patch"}], "self_contained": True})  # old enum value never accepts


def test_eligibility_excludes_network_test_repos():
    """The verifier is offline, so repos whose suites need the network (psf/requests -> httpbin) are not trial-eligible."""
    assert sb.filter_pytest_repos(["psf__requests-1963", "pydata__xarray-5362", "django__django-1"]) == ["pydata__xarray-5362"]
    assert "psf/requests" in sb.PYTEST_REPOS and "psf/requests" in sb.NETWORK_TEST_REPOS
