import hashlib
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
from urllib.parse import parse_qs, urlsplit

import pytest

from swesim.sources import github_repo as gr
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
    cand_text = judge.issue_contract_delta(_row())[0]["text"]          # "tests assert the literal 'dimension '": the one code-extracted gap of the fixture

    def fake_chat(model_id, system, messages, schema, **kw):
        seen.update(model=model_id, system=system, user=json.loads(messages[0]["content"]))
        return {"gaps": [{"id": "g1", "text": cand_text, "source": "test-literal", "resolved_by": "thread",
                          "resolution_excerpt": "raise a ValueError when the dim is missing", **base},
                         {"id": "g2", "text": "message names the dim", "source": "proposed", "evidence": "match=\"dimension 'z' not found\"", "question": "message?",
                          "resolved_by": "tests", "resolution_excerpt": "", **base},
                         {"id": "g3", "text": "invented", "source": "proposed", "evidence": "raise TypeError('nope')", "resolved_by": "tests", **base},   # not verbatim: dropped
                         {"id": "g4", "text": "dup", "source": "proposed", "evidence": "match=\"dimension  'z' not found\"", "question": "message?", "resolved_by": "tests", **base}],  # same evidence + question: dropped
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
    from swesim.stages.judge import issue_contract_delta
    assert [c["text"] for c in issue_contract_delta(row)] == ["tests assert the literal 'dimension '"]   # the issue already names ValueError
    gaps = [
        {"id": "g1", "source": "test-exception", "text": "tests assert the literal 'dimension '", "resolved_by": "thread", "resolution_excerpt": "raise a ValueError when the dim is missing", **base},
        {"id": "g2", "source": "proposed", "text": "message names the dim", "evidence": "match=\"dimension 'z' not found\"", "resolved_by": "tests", "resolution_excerpt": "", **base},
        {"id": "g3", "source": "proposed", "text": "invented", "evidence": "raise TypeError('nope')", "resolved_by": "tests", **base},                 # not in tests/patch: dropped
        {"id": "g4", "source": "proposed", "text": "dup", "evidence": "match=\"dimension  'z' not found\"", "resolved_by": "tests", **base},            # same evidence after _norm: dropped
        {"id": "g5", "source": "proposed", "text": "short", "evidence": "x\n", "resolved_by": "tests", **base},                                          # below the 12-char floor: dropped
        {"id": "g6", "source": "proposed", "text": "claims thread", "evidence": "with pytest.raises(ValueError, match=\"dimension 'z' not found\"):",
         "resolved_by": "thread", "resolution_excerpt": "the maintainer agreed to raise", **base},                                                    # thread claim not in thread -> tests
        {"id": "g7", "source": "test-literal", "text": "literal", "resolved_by": "tests", **base},                              # extracted-looking label, text matches no candidate gap: a proposal without evidence -> dropped
        {"id": "g8", "source": "diff-branch", "text": "invented source", "resolved_by": "none", **base},                                              # non-extracted source, no evidence: dropped
        "garbage",
    ]
    kept, stats = ground_gaps(gaps, row)
    assert [g["id"] for g in kept] == ["g1", "g2", "g6"]
    assert stats == {"gaps_proposed": 2, "gaps_dropped": 6, "resolution_downgraded": 1}
    by = {g["id"]: g for g in kept}
    assert by["g1"]["resolved_by"] == "thread" and by["g2"]["resolved_by"] == "tests" and by["g6"]["resolved_by"] == "tests"
    assert by["g1"]["source"] == "test-literal" and by["g6"]["source"] == "proposed" and by["g2"]["evidence_grounded"] is True and by["g1"]["evidence_grounded"] is False   # source is code-owned
    assert by["g2"]["evidence_in_tests"] is True and by["g6"]["evidence_in_tests"] is True
    assert accept({"self_contained": True, "gaps": [by["g2"]]}) and accept({"self_contained": True, "gaps": [by["g1"]]})
    assert not accept({"self_contained": True, "gaps": [{**by["g2"], "resolved_by": "none"}]}) and not accept({"self_contained": False, "gaps": [by["g2"]]})
    assert ground_gaps([], row) == ([], {"gaps_proposed": 0, "gaps_dropped": 0, "resolution_downgraded": 0})


def test_ground_gaps_closes_the_label_smuggle_and_boilerplate_evidence_holes():
    """Reviewer probes on d5ad128: an invented gap labelled with an extracted source, `diff --git a/` as evidence, and patch-only
    evidence all passed both auto gates. None may."""
    from swesim.stages.draft_spec import tests_resolved_facts
    from swesim.stages.judge import accept, evidence_ok, ground_gaps
    row = _row(patch="diff --git a/xarray/core/weighted.py b/xarray/core/weighted.py\n--- a/xarray/core/weighted.py\n+++ b/xarray/core/weighted.py\n@@ -1 +1,3 @@\n x\n"
                     "+    if dim not in self.obj.dims:\n+        raise ValueError(f\"dimension {dim!r} not found in weights\")\n")
    base = {"derivable_from_repo": False, "consequential": True, "category": "behavior"}
    smuggled = {"id": "s1", "source": "test-exception", "text": "tests require a retry loop", "resolved_by": "thread", "resolution_excerpt": "Please raise a ValueError", **base}
    boiler = {"id": "s2", "source": "proposed", "text": "invented decision", "evidence": "diff --git a/", "resolved_by": "tests", **base}
    header = {"id": "s3", "source": "proposed", "text": "invented decision", "evidence": "+++ b/xarray/core/weighted.py", "resolved_by": "tests", **base}
    keywords = {"id": "s4", "source": "proposed", "text": "invented decision", "evidence": "if not in", "resolved_by": "tests", **base}
    patch_only = {"id": "s5", "source": "proposed", "text": "the fix rejects unknown dims", "evidence": "if dim not in self.obj.dims:", "question": "which dims?", "resolved_by": "tests", **base}
    kept, stats = ground_gaps([smuggled, boiler, header, keywords, patch_only], row)
    assert [g["id"] for g in kept] == ["s5"] and stats["gaps_dropped"] == 4
    assert kept[0]["evidence_grounded"] is True and kept[0]["evidence_in_tests"] is False and kept[0]["resolved_by"] == "none"   # patch-only evidence cannot be tests-resolved
    assert not accept({"self_contained": True, "gaps": kept})
    assert tests_resolved_facts(row, {"gaps": [{**patch_only, "resolved_by": "tests"}]}) == []                                     # and synthesizes no on-request fact
    assert not evidence_ok("diff --git a/") and not evidence_ok("assert not in") and not evidence_ok("valueerror") and evidence_ok("raise valueerror(msg) here")
    # review v4: ordinary identifiers ground; header-shaped spans are rejected by shape, not by a keyword denylist
    for ok in ("assert x == 11", "assert new == old", "assert index == 3", "assert out.ret == 0", "assert file.mode == copy"):
        assert evidence_ok(ok), ok
    for bad in ("diff --git a/x.py b/x.py", "index 1111111..2222222 100644", "+++ b/xarray/core/weighted.py", "--- a/xarray/core/weighted.py",
                "@@ -1 +1,3 @@ def test_x():", "new file mode 100644 here", "binary files a/x and b/x differ", "assert 11 == 11", "if not in"):
        assert not evidence_ok(bad), bad
    # two distinct decisions pinned by the same test line both survive (dedupe is on evidence + question)
    ev = "match=\"dimension 'z' not found\""
    two = [{"id": "d1", "source": "proposed", "text": "message names the dim", "evidence": ev, "question": "what message?", "resolved_by": "tests", **base},
           {"id": "d2", "source": "proposed", "text": "message quotes the dim name", "evidence": ev, "question": "is the dim name quoted?", "resolved_by": "tests", **base},
           {"id": "d3", "source": "proposed", "text": "dup of d1", "evidence": ev, "question": "what message?", "resolved_by": "tests", **base}]
    kept, stats = ground_gaps(two, _row())
    assert [g["id"] for g in kept] == ["d1", "d2"] and stats["gaps_dropped"] == 1


def test_tests_for_excerpt_and_synthesized_authored_facts():
    from swesim.stages.draft_spec import tests_for_excerpt, tests_resolved_facts
    row = _row()
    f2p = [F2P, "xarray/tests/test_weighted.py::test_new", "xarray/tests/test_other.py::test_x"]
    assert tests_for_excerpt(row["test_patch"], "match=\"dimension 'z' not found\"", f2p) == [F2P]                       # enclosing test function
    assert tests_for_excerpt(row["test_patch"], "def test_new(): pass", f2p) == ["xarray/tests/test_weighted.py::test_new"]
    assert tests_for_excerpt(row["test_patch"], "not in the test patch at all", f2p) == []                            # no all-F2P fallback any more
    assert tests_for_excerpt(row["test_patch"], "", f2p) == []
    # the hunk trailer names the existing test an edit lands in; a hunk without a test trailer is module level and links nothing
    edited = ("diff --git a/xarray/tests/test_weighted.py b/xarray/tests/test_weighted.py\n--- a/xarray/tests/test_weighted.py\n+++ b/xarray/tests/test_weighted.py\n"
              "@@ -1,2 +1,3 @@\n import pytest\n+import numpy as np\n"
              "@@ -5,2 +5,3 @@ def test_new():\n     first = 1\n+    assert first == 1\n"
              "@@ -40,3 +41,5 @@ def test_weighted_operations_nonexistent_dim(a):\n     w = weights()\n+    with pytest.raises(ValueError, match=\"dimension 'z' not found\"):\n+        ds.weighted(w).mean('z')\n"
              "@@ -80,2 +83,3 @@ class TestOther:\n     pass\n+    helper_line = 2\n")
    assert tests_for_excerpt(edited, "match=\"dimension 'z' not found\"", f2p) == [F2P]                                # not test_new, the previous block
    assert tests_for_excerpt(edited, "assert first == 1", f2p) == ["xarray/tests/test_weighted.py::test_new"]
    assert tests_for_excerpt(edited, "helper_line = 2", f2p) == []                                                    # a class trailer is not a test function
    assert tests_for_excerpt(edited, "import numpy as np", f2p) == []                                                 # module-level hunk
    facts = tests_resolved_facts(row, _judged())
    assert len(facts) == 1 and facts[0]["id"] == "tests-001" and facts[0]["provenance"] == "authored" and facts[0]["discovery"] == "on-request"
    assert facts[0]["source_excerpt"] == "match=\"dimension 'z' not found\"" and facts[0]["source_url"] == "https://github.com/pydata/xarray/pull/5362"
    assert facts[0]["tests"] == [F2P] and facts[0]["kind"] == "behavior" and "g2" in facts[0]["authored_rationale"] and "What should the message say?" in facts[0]["authored_rationale"]
    assert facts[0]["text"] == "Expected error: dimension not found"                                                  # judge prose within the fact_text bounds
    j = _judged(); j["gaps"][1]["text"] = "The kubernetes scheduler must reject the dimension"                          # foreign content words: falls back to the question, then the excerpt
    assert tests_resolved_facts(row, j)[0]["text"] == "What should the message say?" or tests_resolved_facts(row, j)[0]["text"] == j["gaps"][1]["evidence"]
    j = _judged(); j["gaps"][1]["text"] = "x" * 500; j["gaps"][1]["question"] = "y" * 500
    assert tests_resolved_facts(row, j)[0]["text"] == j["gaps"][1]["evidence"]
    j = _judged(); j["gaps"][1]["evidence"] = "raise TypeError('nope')"                                             # evidence re-checked by code: fabricated -> no fact
    assert tests_resolved_facts(row, j) == []
    j = _judged(); j["gaps"][1]["derivable_from_repo"] = True
    assert tests_resolved_facts(row, j) == []
    j = _judged(); j["gaps"][1]["evidence"] = "def test_new(): pass"; j["gaps"][1]["text"] = "Expected error for a missing dim"   # in a test function -> linked to it, not to every F2P test
    assert tests_resolved_facts(row, j) == [] if F2P.startswith("xarray/tests/test_weighted.py::test_new") else tests_resolved_facts(_row(fail_to_pass=f2p), j)[0]["tests"] == ["xarray/tests/test_weighted.py::test_new"]
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
    d["ledger"] = [{"id": "f01", "text": "The rendered table has eleven entries", "kind": "behavior", "provenance": "authored", "source_excerpt": excerpt,
                    "source_url": "https://github.com/sphinx-doc/sphinx/pull/9467", "authored_rationale": "only the tests pin the count", "discovery": "on-request", "tests": f2p}]
    assert ledger_violations(d, row) == []
    d["ledger"][0]["source_excerpt"] = "assert len(rows) == 11 assert old_value == 7"     # removed (`-`) lines are not evidence of the merged state
    assert any("f01" in v and "verbatim" in v for v in ledger_violations(d, row))
    gap = {"id": "g9", "source": "proposed", "text": "row count", "evidence": excerpt, "question": "how many rows?", "derivable_from_repo": False, "consequential": True,
           "resolved_by": "tests", "category": "behavior"}
    kept, stats = ground_gaps([gap], row)
    assert kept and kept[0]["evidence_grounded"] is True and kept[0]["resolved_by"] == "tests" and stats["gaps_dropped"] == 0


MPL_TEST_PATCH = ("diff --git a/lib/matplotlib/tests/test_figure.py b/lib/matplotlib/tests/test_figure.py\n--- a/lib/matplotlib/tests/test_figure.py\n+++ b/lib/matplotlib/tests/test_figure.py\n"
                  "@@ -1 +1,10 @@\n x\n+def test_subplot_mosaic_width_ratios():\n+    fig = plt.figure()\n+    fig.subplot_mosaic([[\"A\", [[\"B\"], [\"C\"]]]], width_ratios=[2, 1])\n"
                  "+    assert fig.axes[0].get_gridspec().get_width_ratios() == [2, 1]\n+def test_subplot_mosaic_height_ratios_nested():\n+    fig = plt.figure()\n+    fig.subplot_mosaic([[\"A\"], [[[\"B\", \"C\"]]]], height_ratios=[1, 3])\n"
                  "+def test_unrelated_colorbar_label():\n+    assert plt.colorbar().ax.get_ylabel() == \"\"\n")
MPL_F2P = ["lib/matplotlib/tests/test_figure.py::test_subplot_mosaic_width_ratios", "lib/matplotlib/tests/test_figure.py::test_subplot_mosaic_height_ratios_nested[a]",
           "lib/matplotlib/tests/test_figure.py::test_unrelated_colorbar_label"]


def test_auto_link_tests_by_shared_content_words():
    from swesim.stages.draft_spec import auto_link_tests, content_words
    assert content_words("Nested subplot_mosaic layouts must apply width_ratios while nothing is None") == {"nested", "subplot", "mosaic", "layouts", "apply", "width", "ratios", "nothing"}
    # the function's own name tokens (subplot, mosaic, width, ratios, height, nested) never count and the def line is not body:
    # only body words outside the name link, so the height test (body: figure, subplot_mosaic call, height_ratios) links nothing here
    assert auto_link_tests("Nested subplot_mosaic layouts must apply width_ratios to every level", MPL_TEST_PATCH, MPL_F2P) == []
    assert auto_link_tests("The gridspec width ratios of the figure follow the mosaic", MPL_TEST_PATCH, MPL_F2P) == [MPL_F2P[0]]   # gridspec + figure are body words
    assert auto_link_tests("Only the height_ratios of nested mosaics matter here", MPL_TEST_PATCH, MPL_F2P) == []
    assert auto_link_tests("The weighted mean operations", _row()["test_patch"], [F2P]) == [] and auto_link_tests("Some weighted operations nonexistent", _row()["test_patch"], [F2P]) == []   # name tokens only
    assert auto_link_tests("Raise ValueError with the dimension not found message", _row()["test_patch"], [F2P]) == [F2P]                 # body words
    sig_only = ("diff --git a/tests/test_s.py b/tests/test_s.py\n--- a/tests/test_s.py\n+++ b/tests/test_s.py\n@@ -1 +1,3 @@\n x\n"
                "+def test_something(gridspec_holder, figure_factory):\n+    assert holder.value == 1\n")
    assert auto_link_tests("The gridspec figure factory", sig_only, ["tests/test_s.py::test_something"]) == []                                # gridspec/figure/factory appear only in the signature
    assert auto_link_tests("The holder value is one", sig_only, ["tests/test_s.py::test_something"]) == ["tests/test_s.py::test_something"]    # holder/value are body words
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
    two = [F2P, "xarray/tests/test_weighted.py::test_new"]                                                            # two distinct F2P functions: the content-word rule applies
    d = _draft_llm(); d["ledger"][0].update(tests=[], text="Raise ValueError with the dimension not found message")   # shares valueerror/dimension/found with the test body
    d["groups"] = [{"id": "g01", "weight": 1.0, "tests": two}]
    calls = _patch_llm(monkeypatch, d, _withhold_llm())
    p = draft_spec.draft(_row(fail_to_pass=two), _judged(), "fake/model", tmp_path / "s")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    assert next(f for f in spec.ledger if f.id == "f01").tests == [F2P]
    meta = json.loads((tmp_path / "s" / "draft.meta.json").read_text())
    assert meta["auto_linked"] == ["f01"] and meta["auto_linked_single_target"] is False
    draft_calls = [c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]]
    assert len(draft_calls) == 2                                                                        # correction round happened before the fallback
    assert draft_calls[0]["messages"][0]["content"].startswith("FAIL_TO_PASS ids") and F2P in draft_calls[0]["messages"][0]["content"].splitlines()[0]
    correction = draft_calls[1]["messages"][-1]["content"]
    assert "Correct these violations" in correction and "f01:" in correction and F2P in correction
    # no shared content words and two candidate functions: the fallback cannot link, so the draft fails as before
    d = _draft_llm(); d["ledger"][0].update(tests=[], text="The maintainer prefers a friendly wording")
    d["groups"] = [{"id": "g01", "weight": 1.0, "tests": two}]
    _patch_llm(monkeypatch, d, _withhold_llm())
    with pytest.raises(ValueError, match="grounding validation after two attempts.*f01: every on-request fact must carry"):
        draft_spec.draft(_row(fail_to_pass=two), _judged(), "fake/model", tmp_path / "s2")
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


def test_withhold_retries_once_with_violations_then_returns_zero_removals(monkeypatch):
    """A selected span that does not match the issue verbatim gets one correction round; if every selection is still rejected,
    zero removals is the (valid) outcome, not a failure."""
    from swesim.stages import draft_spec
    issue = 'Import fails\nThe importer stops on the first row.\nTraceback: ERROR42 raised in importer'
    attempts = []

    def not_verbatim(model_id, system, messages, schema, **kw):
        attempts.append(messages)
        return {'removed': [{'category': 'error_message', 'excerpt': 'Traceback: ERROR99 raised in importer', 'kind': 'reproduction', 'discovery': 'on-request'}]}
    monkeypatch.setattr(draft_spec, 'chat_json', not_verbatim)
    res = draft_spec.withhold(issue, 'fake/model')
    assert res["removed"] == [] and res["withheld_brief"] is None and res["deleted"] == [] and res["kept"] == [(0, len(issue))]
    assert res["stats"]["attempts"] == 2 and res["stats"]["reason"].startswith("all selections rejected") and "not a verbatim span" in res["stats"]["reason"]
    assert len(attempts) == 2 and "Correct these violations" in attempts[1][-1]["content"] and "ERROR99" in attempts[1][-1]["content"]
    assert "closest lines" in attempts[1][-1]["content"] and "Traceback: ERROR42 raised in importer" in attempts[1][-1]["content"]   # the issue line to quote exactly
    monkeypatch.setattr(draft_spec, 'chat_json', lambda *a, **k: {"removed": []})
    res = draft_spec.withhold(issue, 'fake/model')
    assert res["removed"] == [] and res["stats"] == {"sentences_total": 3, "sentences_removed": 0, "fraction_removed": 0.0, "duplicate_selections": 0,
                                                     "prompt_version": "withhold-v2", "attempts": 1, "reason": "model selected no spans", "rejected_selections": []}
    # a mix: one valid and one rejected selection -> one correction round, then the valid one proceeds and the rejection is recorded
    mixed = {'removed': [{'category': 'error_message', 'excerpt': 'Traceback: ERROR42 raised in importer', 'kind': 'reproduction', 'discovery': 'on-request'},
                         {'category': 'error_message', 'excerpt': 'Traceback: ERROR99 raised in importer', 'kind': 'reproduction', 'discovery': 'on-request'}]}
    attempts.clear()
    monkeypatch.setattr(draft_spec, 'chat_json', lambda model_id, system, messages, schema, **kw: attempts.append(messages) or mixed)
    res = draft_spec.withhold(issue, 'fake/model')
    assert [r["excerpt"] for r in res["removed"]] == ["Traceback: ERROR42 raised in importer"] and len(attempts) == 2
    assert res["stats"]["attempts"] == 2 and len(res["stats"]["rejected_selections"]) == 1 and "ERROR99" in res["stats"]["rejected_selections"][0]


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
    assert [r["excerpt"] for r in res["removed"]] == ["Traceback: ERROR42 raised in parse_header"]
    assert res["removed"][0]["fact_text"] == "Reporter's original statement: Traceback: ERROR42 raised in parse_header" and res["removed"][0]["paraphrase"] is None
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
    assert res["stats"] == {"sentences_total": 7, "sentences_removed": 3, "fraction_removed": 0.429, "duplicate_selections": 0, "prompt_version": "withhold-v2",
                            "attempts": 1, "reason": None, "rejected_selections": []}


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
    # a one-word selection cannot delete sentences; rejected twice, the outcome is zero removals
    monkeypatch.setattr(draft_spec, "chat_json", fake(_select(("reproduction", "the"))["removed"]))
    res = draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")
    assert res["removed"] == [] and "selected span too short" in res["stats"]["reason"]
    # malformed selections enter the correction round instead of raising TypeError/AttributeError
    monkeypatch.setattr(draft_spec, "chat_json", fake(["just a string", {"category": "error_message", "excerpt": 42, "kind": "context", "discovery": "on-request"}, None]))
    attempts.clear()
    res = draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")
    assert res["removed"] == [] and "must be an object" in res["stats"]["reason"] and "excerpt must be a string" in res["stats"]["reason"]
    assert len(attempts) == 2 and "Correct these violations" in attempts[1][-1]["content"]
    # duplicate selections of the same span collapse into one fact
    dup = _select(("error_message", "Traceback: ERROR42 raised in parse_header"), ("error_message", "ERROR42 raised in parse_header"))
    monkeypatch.setattr(draft_spec, "chat_json", fake(dup["removed"]))
    res = draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")
    assert len(res["removed"]) == 1 and res["stats"]["duplicate_selections"] == 1 and res["stats"]["sentences_removed"] == 1
    # the ledger text is code-owned: the verbatim span with the reporter prefix; the model's paraphrase is metadata only
    # (live drafts pypdf-3826 / sphinx-10097 died on ordinary paraphrase verbs such as "executes", "building", "given")
    para = _select(("error_message", "Traceback: ERROR42 raised in parse_header"))
    para["removed"][0]["paraphrase"] = "The kubernetes scheduler executes and reproduces ERROR42 when building"
    monkeypatch.setattr(draft_spec, "chat_json", fake(para["removed"]))
    rec = draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")["removed"][0]
    assert rec["fact_text"] == "Reporter's original statement: Traceback: ERROR42 raised in parse_header" and rec["paraphrase"] == para["removed"][0]["paraphrase"]
    legacy = _select(("error_message", "Traceback: ERROR42 raised in parse_header"))
    legacy["removed"][0]["fact_text"] = "ERROR42 " * 60                                            # an old-style fact_text is just a paraphrase too, never validated
    monkeypatch.setattr(draft_spec, "chat_json", fake(legacy["removed"]))
    rec = draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")["removed"][0]
    assert rec["fact_text"].startswith("Reporter's original statement: Traceback") and rec["paraphrase"] == "ERROR42 " * 60
    # a very long removed span is whitespace-normalized and cut to 400 characters with an ellipsis
    long_issue = "Title line\n" + " ".join(["word%d" % i for i in range(120)]) + "  spaced   out\nAnother line kept."
    monkeypatch.setattr(draft_spec, "chat_json", fake(_select(("reproduction", "word0 word1 word2 word3"))["removed"]))
    rec = draft_spec.withhold(long_issue, "fake/model")["removed"][0]
    assert len(rec["fact_text"]) == len("Reporter's original statement: ") + 400 and rec["fact_text"].endswith("…") and "  " not in rec["fact_text"]
    assert draft_spec.verbatim_fact_text("  a\n\n b  ") == "Reporter's original statement: a b"


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
    res = draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")                          # the only selection is the title: rejected twice -> zero removals
    assert res["removed"] == [] and "title line cannot be withheld" in res["stats"]["reason"] and res["stats"]["attempts"] == 2
    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: {"removed": []})
    assert draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")["stats"]["reason"] == "model selected no spans"
    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: {"removed": "not a list"})
    assert draft_spec.withhold(WITHHOLD_ISSUE, "fake/model")["stats"]["reason"].startswith("all selections rejected: removed must be a list")


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
    row.update(over)
    if "probe" not in over and isinstance(row.get("probe"), dict):
        row["probe"]["binding"] = gr.probe_binding(row)   # the default probe is bound to whatever base_commit/test_patch the row ends up with
    return row


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
            "reviews": [{"user": "rev", "created_at": "2021-05-21T00:00:00Z", "body": "Please raise a  ValueError\nwhen the dim is missing.\nThe message should say the dimension was not found.",
                         "url": url + "#pullrequestreview-1", "path": None}],
            "pr_comments": [], "review_comments": [], "linked_prs": [],
            "linked_issues": [{"issue_url": f"https://github.com/{repo}/issues/5360", "number": 5360, "title": "t", "body": "b", "created_at": "2021-05-01T00:00:00Z", "comments": []}],
            "issue_url": f"https://github.com/{repo}/issues/5360", "issue_comments": []}


def _judged(**over):
    j = {"gaps": [{"id": "g1", "text": "tests assert the literal 'dimension '", "source": "test-literal", "evidence": "pytest.raises(ValueError", "question": "Which exception?",
                   "derivable_from_repo": False, "consequential": True, "resolved_by": "thread", "resolution_excerpt": "raise a ValueError when the dim is missing", "category": "behavior"},
                  {"id": "g2", "text": "Expected error: dimension not found", "source": "proposed", "evidence": "match=\"dimension 'z' not found\"",
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
    monkeypatch.setattr(threads, "base_text", _fake_base_text)
    return calls


def _fake_base_text(repo, commit, path):
    """Base-commit content the drafter fetches: hidden test files, and the source file the fixture's gold/control diffs touch."""
    if path == "xarray/core/weighted.py":
        return "x\n"
    if path.endswith(".py") and "tests" in path:
        return "def test_weighted_sum():\n    assert True\n"
    return None


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
    assert spec.contract.test_assets == ["test.patch"] and spec.contract.protected_paths == ["xarray/tests/data/w.nc", "xarray/tests/test_weighted.py"]   # test files + a test-tree extra
    assert not (set(spec.contract.protected_paths) & draft_spec.patch_paths(_row()["patch"]))                                   # never a gold-patched file
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
    assert all(f.text == "Reporter's original statement: " + " ".join(f.source_excerpt.split()) for f in wf.values())   # ledger text is the verbatim span, code-owned
    for o, f in zip(ops, wf.values()):
        start, end = ISSUE.index(f.source_excerpt), ISSUE.index(f.source_excerpt) + len(f.source_excerpt)
        assert f"natural[{start}:{end}]" in o.rationale and repr(f.source_excerpt) in o.rationale
    # code-synthesized fact for the judge gap that only the merged tests resolve: authored, on-request, linked to the test citing it
    tf = next(f for f in spec.ledger if f.id == "tests-001")
    assert tf.provenance == "authored" and tf.discovery == "on-request" and tf.tests == [F2P] and tf.source_url.endswith("/pull/5362")
    assert tf.source_excerpt == "match=\"dimension 'z' not found\"" and "g2" in tf.authored_rationale and "resolved_by=tests" in tf.authored_rationale
    assert [f.id for f in spec.ledger] == ["f01", "f02", "f03", "f04", "tests-001", "withheld-001", "withheld-002", "withheld-003"]
    meta = json.loads((out_dir / "draft.meta.json").read_text())
    assert meta["auto_linked"] == [] and [r["fact_id"] for r in meta["withholding"]["spans"]] == ["withheld-001", "withheld-002", "withheld-003"]
    assert meta["withholding"]["removed"] == 3 and meta["withholding"]["reason"] is None and meta["withholding"]["stats"]["sentences_removed"] == 3 and meta["withholding"]["kept"]
    assert meta["auto_linked_single_target"] is False                                              # f01 carried its own F2P link; nothing was auto-linked
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


def test_schema_errors_are_fed_back_in_the_correction_round(monkeypatch, tmp_path):
    """sphinx-10097: a requirement depended on an author-only fact and the pydantic _integrity error killed the draft with no
    feedback. Schema errors are now violations for the correction round; a still-invalid second output fails as before."""
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    first = _draft_llm(); first["requirements"][0]["fact_id"] = "f02"                                     # f02 is author-only
    outputs = iter([first, _draft_llm()])
    calls = []

    def fake_chat(model_id, system, messages, schema, **kw):
        calls.append({"system": system, "messages": messages})
        return _withhold_llm() if WITHHOLD_PROMPT_MARK in system else next(outputs)
    monkeypatch.setattr(draft_spec, "chat_json", fake_chat)
    monkeypatch.setattr(threads, "base_text", _fake_base_text)
    p = draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    assert spec.requirements[0].fact_id == "f01" and (tmp_path / "s" / "assets" / "oracle.diff").exists()
    author = [c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]]
    assert len(author) == 2
    correction = author[1]["messages"][-1]["content"]
    assert "Correct these violations" in correction and "schema:" in correction and "author-only" in correction
    # a ledger violation and a schema error in the same output both reach the single correction round
    both = _draft_llm(); both["requirements"][0]["fact_id"] = "f02"; both["ledger"][0]["source_excerpt"] = "raise a TypeError when the dim is missing"
    outputs = iter([both, _draft_llm()]); calls.clear()
    draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s_both")
    correction = [c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]][1]["messages"][-1]["content"]
    assert "f01: source_excerpt is not a verbatim span" in correction and "schema:" in correction and "author-only" in correction
    # still invalid after the correction round: fails with the schema message and writes nothing
    outputs = iter([first, first]); calls.clear()
    with pytest.raises(ValueError, match="grounding validation after two attempts.*schema:.*author-only"):
        draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s2")
    assert not (tmp_path / "s2" / "scenario.draft.json").exists() and not (tmp_path / "s2" / "assets" / "oracle.diff").exists()
    # a missing persona or an empty groups list is a schema violation the model can fix, not a KeyError
    broken = _draft_llm(); del broken["persona"]
    outputs = iter([broken, _draft_llm()]); calls.clear()
    draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s3")
    assert "schema: persona" in [c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]][1]["messages"][-1]["content"]
    broken = _draft_llm(); broken["groups"] = []                                                         # field errors come first; _integrity runs once fields validate
    outputs = iter([broken, _draft_llm()]); calls.clear()
    draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s3b")
    assert "groups must cover fail_to_pass" in [c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]][1]["messages"][-1]["content"]
    # an unknown test id on a fact is caught the same way
    unknown = _draft_llm(); unknown["ledger"][0]["tests"] = [F2P, "xarray/tests/test_weighted.py::test_missing"]
    outputs = iter([unknown, _draft_llm()]); calls.clear()
    draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s4")
    assert "links unknown test" in [c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]][1]["messages"][-1]["content"]


def test_control_names_are_code_owned_slugs(monkeypatch, tmp_path):
    """matplotlib-24189: a control label with spaces and slashes became a file path at compile time."""
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    label = "strip only width/height ratios from gridspec_kw before passing to inner"
    taken = set()
    slug = draft_spec.slugify_control_name(label, "plausible-wrong", taken)
    assert slug == "strip-only-width-height-ratios-from-gridspec_kw-before-passing-to-inner"[:64].rstrip("-._") and draft_spec.CONTROL_NAME_RE.match(slug) and "/" not in slug
    second = draft_spec.slugify_control_name(label, "alternative-correct", taken)                                 # collision suffix, still <= 64 chars
    assert second == slug[:62].rstrip("-._") + "-2" and len(second) <= 64
    assert draft_spec.slugify_control_name("  ", "plausible-wrong", set()) == "plausible-wrong" and draft_spec.slugify_control_name("Ab", "x-y-z", set()) == "x-y-z"
    assert draft_spec.slugify_control_name("Wrong Exception Type!!", "p", set()) == "wrong-exception-type"
    d = _draft_llm(); d["plausible_wrong"]["name"] = label
    alt = {"name": label, "rationale": "same behaviour", "diff": "diff --git a/xarray/core/weighted.py b/xarray/core/weighted.py\n--- a/xarray/core/weighted.py\n+++ b/xarray/core/weighted.py\n@@ -1 +1,2 @@\n x\n+z\n"}
    d["alternative_correct"] = alt
    _patch_llm(monkeypatch, d, _withhold_llm())
    p = draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    assert spec.controls.plausible_wrong[0].name == slug and spec.controls.alternative_correct[0].name == second
    assert all(draft_spec.CONTROL_NAME_RE.match(c.name) for c in [*spec.controls.plausible_wrong, *spec.controls.alternative_correct, *spec.controls.tamper])
    meta = json.loads((tmp_path / "s" / "draft.meta.json").read_text())
    assert meta["controls"]["plausible_wrong"]["label"] == label and meta["controls"]["plausible_wrong"]["name"] == slug and meta["controls"]["alternative_correct"]["name"] == second
    assert all(all(draft_spec.ASSET_COMPONENT_RE.match(part) for part in a.path.split("/")) for a in spec.assets.values())
    with pytest.raises(ValueError, match="asset path component"):
        draft_spec.check_asset_paths(["ok.diff", "bad name.diff"])
    with pytest.raises(ValueError, match="asset path component"):
        draft_spec.check_asset_paths(["../escape.diff"])


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_control_patches_are_verified_at_draft_time(monkeypatch, tmp_path):
    """py-pdf__pypdf-3826: a corrupt model plausible-wrong diff reached the Modal sweep. Now: `files` (complete text) -> difflib
    diff that applies by construction; a model diff is git-apply-checked; a failing one is corrected once, then dropped/replaced."""
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    base = {"xarray/core/weighted.py": "x\n"}
    good = _draft_llm()["plausible_wrong"]["diff"]
    corrupt = good.replace("@@ -1 +1,2 @@", "@@ -1 +1,9 @@")                                   # wrong hunk counts: "corrupt patch"
    assert draft_spec.verify_diff_applies(base, good) == (True, "")
    ok, msg = draft_spec.verify_diff_applies(base, corrupt)
    assert ok is False and "corrupt" in msg.lower() or ok is False and msg
    ok, msg = draft_spec.verify_diff_applies({}, good)
    assert ok is None and msg.startswith("unfetched:")                                            # touched file not fetched: marked, and controls treat it as unusable
    assert draft_spec.verify_diff_applies(base, good.replace("a/xarray/core/weighted.py", "a/../etc/passwd").replace("b/xarray/core/weighted.py", "b/../etc/passwd"))[0] is False
    # a `files` path that was never fetched is a violation, never a new-file diff; None (confirmed absent) is the only new-file case
    with pytest.raises(draft_spec.DraftViolation, match="could not be fetched"):
        draft_spec.build_control_diff({"files": {"pkg/unfetched.py": "x\n"}}, base)
    with pytest.raises(draft_spec.DraftViolation, match="repository-relative"):
        draft_spec.build_control_diff({"files": {"../escape.py": "x\n"}}, {**base, "../escape.py": None})
    newfile, method = draft_spec.build_control_diff({"files": {"pkg/new.py": "print(1)\n"}}, {**base, "pkg/new.py": None})
    assert method == "difflib" and "new file mode" in newfile and "--- /dev/null" in newfile
    # primary path: complete file text -> well-formed diff
    ctrl = {"name": "keyerror", "rationale": "r", "files": {"xarray/core/weighted.py": "x\nraise KeyError\n"}}
    diff, method = draft_spec.build_control_diff(ctrl, base)
    assert method == "difflib" and diff.startswith("diff --git a/xarray/core/weighted.py b/xarray/core/weighted.py\n") and "+raise KeyError" in diff
    assert draft_spec.verify_diff_applies(base, diff) == (True, "")
    assert draft_spec.build_control_diff({"diff": good}, base) == (good, "model-diff") and draft_spec.build_control_diff({}, base) == (None, "missing")
    with pytest.raises(draft_spec.DraftViolation, match="changes nothing"):
        draft_spec.build_control_diff({"files": {"xarray/core/weighted.py": "x\n"}}, base)
    # end to end: first output has a corrupt diff -> correction names the git error; second output uses `files` -> success
    first = _draft_llm(); first["plausible_wrong"]["diff"] = corrupt
    second = _draft_llm(); second["plausible_wrong"] = {"name": "keyerror", "rationale": "wrong exception type", "files": {"xarray/core/weighted.py": "x\nraise KeyError\n"}}
    outputs = iter([first, second]); calls = []

    def fake_chat(model_id, system, messages, schema, **kw):
        calls.append({"system": system, "messages": messages})
        return _withhold_llm() if WITHHOLD_PROMPT_MARK in system else next(outputs)
    monkeypatch.setattr(draft_spec, "chat_json", fake_chat)
    monkeypatch.setattr(threads, "base_text", _fake_base_text)
    p = draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    author = [c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]]
    assert len(author) == 2 and "cannot be applied to the base commit (git apply --check)" in author[1]["messages"][-1]["content"]
    assert spec.controls.plausible_wrong[0].name == "keyerror" and "+raise KeyError" in (tmp_path / "s" / "assets" / "plausible-wrong.diff").read_text()
    meta = json.loads((tmp_path / "s" / "draft.meta.json").read_text())
    assert meta["controls"]["plausible_wrong"] == {"name": "keyerror", "label": "keyerror", "rationale": "wrong exception type", "method": "difflib", "verified": True}
    assert meta["controls_dropped"] == [] and meta["controls_fallback"] == {} and meta["oracle_apply"]["verified"] is True and meta["oracle_apply"]["base_files_fetched"] == ["xarray/core/weighted.py"]
    # still corrupt after the correction round: plausible-wrong is replaced by a REAL incorrect change (an inverted comparison
    # in the gold's first code chunk), a corrupt alternative-correct is dropped
    gold_two = ("diff --git a/xarray/core/weighted.py b/xarray/core/weighted.py\n--- a/xarray/core/weighted.py\n+++ b/xarray/core/weighted.py\n"
                "@@ -1 +1,3 @@\n x\n+if dim == 'z':\n+    raise ValueError(dim)\n")
    bad_alt = {"name": "alt", "rationale": "r", "diff": corrupt.replace("+raise KeyError", "+z")}
    outputs = iter([{**first, "alternative_correct": bad_alt}, {**first, "alternative_correct": bad_alt}]); calls.clear()
    p = draft_spec.draft(_row(patch=gold_two), _judged(), "fake/model", tmp_path / "s2")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    meta = json.loads((tmp_path / "s2" / "draft.meta.json").read_text())
    assert spec.controls.plausible_wrong[0].name == "fallback-inverted-comparison" and spec.controls.alternative_correct == []
    assert meta["controls_fallback"] == {"plausible_wrong": "fallback-inverted-comparison"}
    assert [(x["control"], x["method"]) for x in meta["controls_dropped"]] == [("plausible_wrong", "model-diff"), ("alternative_correct", "model-diff")]
    fallback_diff = (tmp_path / "s2" / "assets" / "plausible-wrong.diff").read_text()
    assert "+if dim != 'z':" in fallback_diff and "+    raise ValueError(dim)" in fallback_diff and draft_spec.verify_diff_applies(base, fallback_diff) == (True, "")
    # no comparison to invert: one added code line is dropped, the emptied hunk removed and the following hunk re-based
    gold2 = ("diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1,2 +1,3 @@\n a\n+b\n c\n@@ -7,2 +8,3 @@\n h\n+i\n j\n")
    base2 = {"pkg/a.py": "a\nc\nd\ne\nf\ng\nh\nj\n"}
    fb = draft_spec.fallback_plausible_wrong(gold2, base2)
    assert fb is not None and fb[1] == "fallback-dropped-line" and "+i" in fb[0] and "+b" not in fb[0] and "@@ -7,2 +7,3 @@" in fb[0]
    assert draft_spec.verify_diff_applies(base2, fb[0]) == (True, "")
    # changelog/docs chunks are skipped; a gold with no code chunk or no droppable change yields no fallback
    gold3 = "diff --git a/CHANGES b/CHANGES\n--- a/CHANGES\n+++ b/CHANGES\n@@ -1 +1,2 @@\n old\n+new entry\n" + gold2
    fb = draft_spec.fallback_plausible_wrong(gold3, {**base2, "CHANGES": "old\n"})
    assert fb is not None and "CHANGES" not in fb[0] and fb[1] == "fallback-dropped-line"
    assert draft_spec.fallback_plausible_wrong("diff --git a/CHANGES b/CHANGES\n--- a/CHANGES\n+++ b/CHANGES\n@@ -1 +1,2 @@\n old\n+new\n", {"CHANGES": "old\n"}) is None
    assert draft_spec.fallback_plausible_wrong(_row()["patch"], base) is None                     # a single added line: dropping it leaves no change
    # a fetch-bound (unfetched) file: the model control is dropped with the reason, never turned into a bogus new-file diff
    monkeypatch.setattr(draft_spec, "MAX_BASE_FILES", 0)
    outputs = iter([second, second]); calls.clear()
    p = draft_spec.draft(_row(patch=gold_two), _judged(), "fake/model", tmp_path / "s5")
    meta = json.loads((tmp_path / "s5" / "draft.meta.json").read_text())
    assert meta["controls_dropped"][0]["control"] == "plausible_wrong" and "could not be fetched" in meta["controls_dropped"][0]["reason"]
    assert meta["controls_fallback"] == {"plausible_wrong": "fallback-inverted-comparison"} and meta["oracle_apply"]["verified"] is None
    assert meta["controls"]["plausible_wrong"]["verified"] is None                                   # honest: the fallback could not be checked either
    assert "new file mode" not in (tmp_path / "s5" / "assets" / "plausible-wrong.diff").read_text()
    monkeypatch.setattr(draft_spec, "MAX_BASE_FILES", 8)
    # an oracle that does not apply to its base is a drafter error, not a correction
    outputs = iter([_draft_llm(), _draft_llm()])
    with pytest.raises(ValueError, match="oracle patch does not apply"):
        draft_spec.draft(_row(patch=corrupt.replace("+raise KeyError", "+y")), _judged(), "fake/model", tmp_path / "s3")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_fallback_inversion_respects_strings_comments_and_prefers_statements():
    """Review v7: the inversion matched `not in` inside a docstring, yielding a control functionally equal to the gold."""
    from swesim.stages import draft_spec
    base = {"pkg/a.py": "a\nc\n"}

    def gold(*added):
        return ("diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n" + f"@@ -1,2 +1,{2 + len(added)} @@\n a\n"
                + "".join("+" + line + "\n" for line in added) + " c\n")
    # occurrences inside a docstring, a string literal or an inline comment are never inverted -> a statement line is dropped instead
    fb = draft_spec.fallback_plausible_wrong(gold("def f():", '    """Return items not in the set."""', '    msg = "a == b"', "    x = 1  # a == b", "    return x"), base)
    assert fb is not None and fb[1] == "fallback-dropped-line" and fb[2] is True
    assert '+    msg = "a == b"' not in fb[0] and "+def f():" in fb[0] and '+    """Return items not in the set."""' in fb[0]   # the def line is never the one dropped
    # a multi-line docstring continuation is not code; the `if` comparison is inverted and the docstring left intact
    fb = draft_spec.fallback_plausible_wrong(gold("def f():", '    """First line.', "    values not in the set are dropped.", '    """', "    if a == b:", "        return 1"), base)
    assert fb[1] == "fallback-inverted-comparison" and "+    if a != b:" in fb[0] and "+    values not in the set are dropped." in fb[0]
    # a comparison inside if/return/assert is preferred over a plain assignment; the inline comment stays untouched
    fb = draft_spec.fallback_plausible_wrong(gold("flag = a == b", "if c != d:  # c == d is fine", "    pass"), base)
    assert fb[1] == "fallback-inverted-comparison" and "+if c == d:  # c == d is fine" in fb[0] and "+flag = a == b" in fb[0]
    fb = draft_spec.fallback_plausible_wrong(gold("flag = a == b", "other = 2"), base)
    assert fb[1] == "fallback-inverted-comparison" and "+flag = a != b" in fb[0]                    # no statement comparison: the assignment is fine
    fb = draft_spec.fallback_plausible_wrong(gold("name = 'TrueType'", "enabled = True"), base)
    assert fb[1] == "fallback-inverted-comparison" and "+enabled = False" in fb[0] and "+name = 'TrueType'" in fb[0]   # word boundary, string skipped
    # only header lines and nothing to invert: no real incorrect change exists
    assert draft_spec.fallback_plausible_wrong(gold("@decorator", "class K:"), base) is None
    assert draft_spec.fallback_plausible_wrong(gold("def g():", "    if x:"), base) is None
    # lightening a hunk (not just removing one) shifts the later hunk's new-side start
    two = ("diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1,2 +1,4 @@\n a\n+b = 1\n+c2 = 2\n c\n@@ -7,2 +9,3 @@\n h\n+i = 3\n j\n")
    base2 = {"pkg/a.py": "a\nc\nd\ne\nf\ng\nh\nj\n"}
    fb = draft_spec.fallback_plausible_wrong(two, base2)
    assert fb[1] == "fallback-dropped-line" and "+b = 1" not in fb[0] and "+c2 = 2" in fb[0] and "@@ -1,2 +1,3 @@" in fb[0] and "@@ -7,2 +8,3 @@" in fb[0] and fb[2] is True
    # the code mask itself
    state = {"quote": None}
    mask = draft_spec._code_mask('x = "a == b"  # c == d', state)
    assert draft_spec._code_of('x = "a == b"  # c == d', mask) == "x =" and state["quote"] is None     # quotes, string body and comment are not code positions
    state = {"quote": None}
    draft_spec._code_mask('doc = """open', state); assert state["quote"] == '"""'
    assert draft_spec._code_of("still inside", draft_spec._code_mask("still inside", state)) == "" and state["quote"] == '"""'
    assert draft_spec._code_of('end""" + tail', draft_spec._code_mask('end""" + tail', state)) == "+ tail" and state["quote"] is None


SEABORN_ISSUE = ("Boolean color data fails on a continuous scale\n"
                 "Passing boolean values as color data with a continuous color scale raises a TypeError.\n"
                 "Expected: True maps to 1 and False to 0 on the scale, and plotting boolean color data must not raise.\n"
                 "Seen with seaborn 0.13 on Linux.\n"
                 "A small reproducer is attached below.")
SEABORN_TP = ("diff --git a/tests/_core/test_scales.py b/tests/_core/test_scales.py\n--- a/tests/_core/test_scales.py\n+++ b/tests/_core/test_scales.py\n"
              "@@ -1 +1,7 @@\n x\n+class TestContinuous:\n+    def test_boolean_series(self):\n+        s = pd.Series([True, False, False])\n"
              "+        p = IntervalProperty()\n+        mapped = Continuous()._setup(s, p)(s)\n+        assert mapped == [1, 0, 0]\n")
SEABORN_F2P = "tests/_core/test_scales.py::TestContinuous::test_boolean_series"


def _seaborn_row():
    return _row(problem_statement=SEABORN_ISSUE, test_patch=SEABORN_TP, fail_to_pass=[SEABORN_F2P], pass_to_pass=["tests/_core/test_scales.py::TestContinuous::test_other"])


def test_fact_text_never_describes_test_construction():
    """seaborn-3190 f05: the stakeholder told the agent exactly how the hidden test is built."""
    from swesim.stages import draft_spec
    row = _seaborn_row()
    leak = ("The regression test constructs a pandas Series of booleans and passes it directly to Continuous()._setup with IntervalProperty(), "
            "expecting mapped output [1, 0, 0].")
    reasons = draft_spec.unsafe_fact_text(leak, row)
    assert any("private attribute '._setup'" in r for r in reasons) and any("'intervalproperty' exists only in the tests" in r for r in reasons)
    assert draft_spec.unsafe_fact_text("Plotting with boolean color data must not raise", row) == []             # Python keywords are not identifiers
    gold_names_setup = _seaborn_row(); gold_names_setup["patch"] = gold_names_setup["patch"].replace("+y\n", "+    def _setup(self, data, prop):\n")
    assert draft_spec.unsafe_fact_text("Continuous()._setup(data, prop) upcasts booleans", gold_names_setup) == ["private attribute '._setup('"]   # gold patch naming it does not make it observable
    assert draft_spec.unsafe_fact_text("Boolean values on a continuous color scale map True to 1 and False to 0, and plotting boolean color data must not raise", row) == []
    assert draft_spec.unsafe_fact_text("assert the scale maps booleans", row) == ["test vocabulary 'assert'"]
    assert draft_spec.unsafe_fact_text("see test_boolean_series and pytest", row) == ["test vocabulary 'pytest'", "test vocabulary 'test_boolean_series'"]
    assert draft_spec.unsafe_fact_text("covered by tests/_core/test_scales.py", row) and draft_spec.unsafe_fact_text("uses obj.__dict__ internally", row)
    assert draft_spec.unsafe_fact_text("ValueError when the dim is missing", _row()) == []          # words from the issue/thread are always allowed
    # a model fact naming _setup( or assert is a violation in the correction round and is demoted to author-only leniently
    fact = {"id": "f09", "text": "The test asserts Continuous()._setup(series, IntervalProperty()) returns [1, 0, 0]", "kind": "behavior", "provenance": "pr-review",
            "source_excerpt": "raise a ValueError when the dim is missing", "source_url": "https://github.com/pydata/xarray/pull/5362#r1", "discovery": "on-request", "tests": [SEABORN_F2P]}
    d = {"ledger": [fact], "requirements": [{"id": "r01", "text": "r", "fact_id": "f09", "tests": [SEABORN_F2P]}]}
    bad = draft_spec.ledger_violations(d, row)
    assert any("f09: fact text would tell the agent how the hidden tests are built" in v and "_setup(" in v for v in bad)
    log = []
    bad = draft_spec.ledger_violations(d, row, demote_unsafe=True, filter_log=log)
    assert fact["discovery"] == "author-only" and fact["tests"] == [] and d["requirements"][0]["fact_id"] is None
    assert log[0]["fact_id"] == "f09" and log[0]["action"] == "demoted-author-only" and any(_is_gate(v) for v in bad)
    # synthesized facts: rewritten to observable behaviour when the judge phrased it so, demoted to author-only otherwise
    evidence = "mapped = Continuous()._setup(s, p)(s)"
    good = {"id": "g2", "source": "proposed", "text": "Boolean values on a continuous color scale map True to 1 and False to 0", "evidence": evidence,
            "question": "What do boolean values map to on a continuous scale?", "derivable_from_repo": False, "consequential": True, "resolved_by": "tests", "category": "behavior"}
    log = []
    facts = draft_spec.tests_resolved_facts(row, {"gaps": [good]}, log)
    assert facts[0]["discovery"] == "on-request" and facts[0]["tests"] == [SEABORN_F2P] and facts[0]["text"] == good["text"] and log[0]["action"] == "kept"
    leaky_gap = {**good, "text": leak, "question": "Does Continuous()._setup( map a pandas Series of booleans?"}
    log = []
    facts = draft_spec.tests_resolved_facts(row, {"gaps": [leaky_gap]}, log)
    assert facts[0]["discovery"] == "author-only" and facts[0]["tests"] == [] and facts[0]["text"] == evidence and "evaluator-only" in facts[0]["authored_rationale"]
    assert log[0]["action"] == "demoted-author-only" and set(log[0]["candidates_rejected"]) == {leak, leaky_gap["question"]}
    question_only = {**good, "text": leak}
    facts = draft_spec.tests_resolved_facts(row, {"gaps": [question_only]}, [])
    assert facts[0]["discovery"] == "on-request" and facts[0]["text"] == good["question"]


def _is_gate(v):
    return v.startswith("no on-request fact links")


def test_demoted_synthesized_fact_is_rescued_by_a_thread_statement_or_the_candidate_is_rejected(monkeypatch, tmp_path):
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    leaky = {"id": "g2", "source": "proposed", "text": "assert the match string", "evidence": "match=\"dimension 'z' not found\"", "question": "pytest match?",
             "derivable_from_repo": False, "consequential": True, "resolved_by": "tests", "category": "behavior"}
    judged = {**_judged(), "gaps": [_judged()["gaps"][0], leaky]}                                    # g1: thread-resolved, resolution excerpt in the review
    d = _draft_llm(); d["ledger"][0]["discovery"] = "repo-derivable"; d["ledger"] = [f for f in d["ledger"] if f["id"] != "f04"]   # no visible on-request F2P fact left
    d["requirements"] = []
    calls = _patch_llm(monkeypatch, d, _withhold_llm())
    p = draft_spec.draft(_row(), judged, "fake/model", tmp_path / "s")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    by = {f.id: f for f in spec.ledger}
    assert by["tests-001"].discovery == "author-only" and by["tests-001"].tests == []
    assert by["thread-001"].discovery == "on-request" and by["thread-001"].tests == [F2P] and by["thread-001"].provenance == "pr-review"
    assert by["thread-001"].text == "raise a ValueError when the dim is missing" and by["thread-001"].source_url.endswith("/pull/5362")
    meta = json.loads((tmp_path / "s" / "draft.meta.json").read_text())
    assert meta["rescue_facts"] == ["thread-001"]
    actions = {(x["fact_id"], x["action"]) for x in meta["fact_filter"]}
    assert ("tests-001", "demoted-author-only") in actions and ("thread-001", "added-rescue") in actions and ("withheld-001", "kept") in actions
    assert len([c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]]) == 2                   # the model got one round to add a visible fact first
    # without any thread-resolved statement the candidate is rejected with the recorded reason
    _patch_llm(monkeypatch, d, _withhold_llm())
    with pytest.raises(ValueError, match="no-stakeholder-safe-fact"):
        draft_spec.draft(_row(), {**_judged(), "gaps": [leaky]}, "fake/model", tmp_path / "s2")
    assert not (tmp_path / "s2" / "scenario.draft.json").exists()
    # a model on-request fact that describes the test is demoted leniently and the requirement that depended on it is unlinked
    d2 = _draft_llm(); d2["ledger"][0]["text"] = "The test asserts pytest.raises(ValueError) with the match string 'z' not found"
    _patch_llm(monkeypatch, d2, _withhold_llm())
    p = draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s3")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    by = {f.id: f for f in spec.ledger}
    assert by["f01"].discovery == "author-only" and by["f01"].tests == [] and spec.requirements[0].fact_id is None
    meta = json.loads((tmp_path / "s3" / "draft.meta.json").read_text())
    assert ("f01", "demoted-author-only") in {(x["fact_id"], x["action"]) for x in meta["fact_filter"]} and meta["rescue_facts"] == []   # tests-001 still carries the gate


def test_draft_with_zero_removals_keeps_only_the_natural_brief(monkeypatch, tmp_path):
    """seaborn-3190: a short issue with nothing safely removable is a valid scenario with the natural brief only."""
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    calls = _patch_llm(monkeypatch, _draft_llm(), {"removed": []})
    p = draft_spec.draft(_row(thread={**_thread("pydata/xarray", 5362), "pr_url": None, "linked_issues": []}), _judged(), "fake/model", tmp_path / "s")   # no issue URL needed either
    spec = ScenarioSpec.model_validate_json(p.read_text())
    assert [b.profile for b in spec.briefs] == ["natural"] and spec.briefs[0].text == ISSUE
    assert not any(f.id.startswith("withheld-") for f in spec.ledger) and not any(o.type == "withholding" for o in spec.operations)
    meta = json.loads((tmp_path / "s" / "draft.meta.json").read_text())
    assert meta["withholding"] == {"removed": 0, "reason": "model selected no spans", "spans": [], "kept": [[0, len(ISSUE)]],
                                   "stats": {"sentences_total": 6, "sentences_removed": 0, "fraction_removed": 0.0, "duplicate_selections": 0,
                                             "prompt_version": "withhold-v2", "attempts": 1, "reason": "model selected no spans", "rejected_selections": []}}
    assert len([c for c in calls if WITHHOLD_PROMPT_MARK in c["system"]]) == 1                       # an explicit empty selection is not corrected


def test_single_f2p_function_links_every_on_request_fact(monkeypatch, tmp_path):
    """pytest-dev__pytest-7283: one FAIL_TO_PASS function (two parametrizations) and on-request facts without tests -> linked by code."""
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    f2p = ["testing/test_unittest.py::test_pdb_teardown_skipped[@unittest.skip]", "testing/test_unittest.py::test_pdb_teardown_skipped[@pytest.mark.skip]"]
    assert draft_spec.f2p_functions(f2p) == {("testing/test_unittest.py", "test_pdb_teardown_skipped")}
    # the same method name in two classes is two functions; parametrizations of a class method are one
    assert draft_spec.f2p_functions(["t.py::TestA::test_x", "t.py::TestB::test_x[1]"]) == {("t.py", "TestA::test_x"), ("t.py", "TestB::test_x")}
    assert draft_spec.f2p_functions(["t.py::TestA::test_x[1]", "t.py::TestA::test_x[2]"]) == {("t.py", "TestA::test_x")}
    assert draft_spec.link_single_target({"ledger": [{"discovery": "on-request", "tests": []}]}, ["t.py::TestA::test_x", "t.py::TestB::test_x"]) == []   # ambiguous: no single target
    class_patch = ("diff --git a/t.py b/t.py\n--- a/t.py\n+++ b/t.py\n@@ -1 +1,7 @@\n x\n+class TestA:\n+    def test_x(self):\n+        assert alpha_value == 1\n"
                   "+class TestB:\n+    def test_x(self):\n+        assert beta_value == 2\n")
    assert [fn for _, fn, _ in draft_spec._test_blocks(class_patch) if fn] == ["TestA::test_x", "TestB::test_x"]
    assert draft_spec.tests_for_excerpt(class_patch, "assert beta_value == 2", ["t.py::TestA::test_x", "t.py::TestB::test_x"]) == ["t.py::TestB::test_x"]
    assert draft_spec.definition_order(["t.py::TestB::test_x", "t.py::TestA::test_x"], None, class_patch) == (["t.py::TestA::test_x", "t.py::TestB::test_x"], "diff")
    # review v6: a mixed file (top-level tests after a class); git's hunk-header separator space is not indentation
    mixed = ("diff --git a/t.py b/t.py\n--- a/t.py\n+++ b/t.py\n"
             "@@ -1,3 +1,4 @@ class TestA:\n     def test_x(self):\n+        assert alpha_value == 1\n         pass\n"
             "@@ -10,2 +11,4 @@ def test_top():\n     first = 1\n+    assert gridspec_value == 2\n+    assert figure_value == 3\n"
             "+def test_tail():\n+    assert tail_value == 4\n")
    ids = ["t.py::TestA::test_x", "t.py::test_top", "t.py::test_tail"]
    assert [fn for _, fn, _ in draft_spec._test_blocks(mixed) if fn] == ["TestA::test_x", "test_top", "test_tail"]
    assert draft_spec.tests_for_excerpt(mixed, "assert gridspec_value == 2", ids) == ["t.py::test_top"]
    assert draft_spec.tests_for_excerpt(mixed, "assert tail_value == 4", ids) == ["t.py::test_tail"]
    assert draft_spec.auto_link_tests("The gridspec value and the figure value", mixed, ids) == ["t.py::test_top"]
    assert draft_spec._f2p_for_block("t.py", "TestZ::test_top", ids) == {"t.py::test_top"}                       # bare-name fallback when the class was mis-tracked
    assert draft_spec.definition_order(["t.py::test_tail", "t.py::test_top", "t.py::TestA::test_x"], None, mixed) == (ids, "diff")
    tp = ("diff --git a/testing/test_unittest.py b/testing/test_unittest.py\n--- a/testing/test_unittest.py\n+++ b/testing/test_unittest.py\n"
          "@@ -1 +1,5 @@\n x\n+@pytest.mark.parametrize(\"mark\", [\"@unittest.skip\", \"@pytest.mark.skip\"])\n+def test_pdb_teardown_skipped(pytester, mark):\n"
          "+    with pytest.raises(ValueError, match=\"dimension 'z' not found\"):\n+        pytester.runpytest(\"--pdb\")\n")
    row = _row(fail_to_pass=f2p, pass_to_pass=["testing/test_unittest.py::test_other"], test_patch=tp)
    d = _draft_llm(); d["ledger"][0].update(tests=[], text="The maintainer prefers a friendly wording")      # no shared content words at all
    d["ledger"][3]["tests"] = []
    d["requirements"][0]["tests"] = f2p; d["groups"] = [{"id": "g01", "weight": 1.0, "tests": f2p}]
    calls = _patch_llm(monkeypatch, d, _withhold_llm())
    p = draft_spec.draft(row, {**_judged(), "gaps": []}, "fake/model", tmp_path / "s")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    assert next(f for f in spec.ledger if f.id == "f01").tests == f2p and next(f for f in spec.ledger if f.id == "f04").tests == f2p   # contract order kept
    meta = json.loads((tmp_path / "s" / "draft.meta.json").read_text())
    assert meta["auto_linked_single_target"] is True and meta["auto_linked"] == ["f01", "f04"]
    assert len([c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]]) == 1                    # linked before validation: no correction round
    assert meta["runner"] == {"order": "definition", "order_source": "diff", "ids": [*f2p, "testing/test_unittest.py::test_other"]}   # P2P id unknown to the diff goes last


def test_runner_ids_follow_definition_order_not_f2p_first(monkeypatch, tmp_path):
    """scikit-learn-13584: F2P-first ordering let a polluting F2P test run before a P2P test it corrupted."""
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    a, b, c = "xarray/tests/test_weighted.py::test_weighted_operations_nonexistent_dim[{a}-b]", "xarray/tests/test_weighted.py::test_new", "xarray/tests/test_other.py::test_x"
    # probe order is the source of truth
    assert draft_spec.definition_order([b, a, c], [c, a, b], "") == ([c, a, b], "probe")
    assert draft_spec.definition_order([b, a, "unknown::test_zz"], [a, b], "") == ([a, b, "unknown::test_zz"], "probe")
    # without a probe order: the diff's def order; unknown ids keep their relative order at the end; parametrizations keep order
    tp = _row()["test_patch"]
    assert draft_spec.definition_order([b, a, c], None, tp) == ([a, b, c], "diff")
    a2 = "xarray/tests/test_weighted.py::test_weighted_operations_nonexistent_dim[{c}-d]"
    assert draft_spec.definition_order([b, a2, a], None, tp) == ([a2, a, b], "diff")
    assert draft_spec.definition_order([c, "other::test_q"], None, tp) == ([c, "other::test_q"], "given")
    # end to end: F2P is test_new (defined second), P2P is the first function -> the command lists the P2P id first
    d = _draft_llm(); d["ledger"][0]["tests"] = [b]; d["ledger"][3]["tests"] = [b]; d["requirements"][0]["tests"] = [b]; d["groups"] = [{"id": "g01", "weight": 1.0, "tests": [b]}]
    _patch_llm(monkeypatch, d, _withhold_llm())
    row = _row(fail_to_pass=[b], pass_to_pass=[a])
    p = draft_spec.draft(row, {**_judged(), "gaps": []}, "fake/model", tmp_path / "s")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    assert spec.contract.runner.command.endswith(shlex.quote(a) + " " + shlex.quote(b)) and spec.contract.runner.command.startswith(draft_spec.runner_prefix(row["test_python"] if row.get("test_python") else "/opt/miniconda3/envs/testbed/bin/python"))
    meta = json.loads((tmp_path / "s" / "draft.meta.json").read_text())
    assert meta["runner"] == {"order": "definition", "order_source": "diff", "ids": [a, b]}
    row = _row(fail_to_pass=[b], pass_to_pass=[a], test_order=[b, a])                                # a probe order overrides the diff order
    p = draft_spec.draft(row, {**_judged(), "gaps": []}, "fake/model", tmp_path / "s2")
    assert json.loads((tmp_path / "s2" / "draft.meta.json").read_text())["runner"] == {"order": "definition", "order_source": "probe", "ids": [b, a]}


def test_draft_rejects_leaky_withholding_and_empty_controls(monkeypatch, tmp_path):
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    leaky = _withhold_llm(); leaky["removed"] = [{**leaky["removed"][0], "excerpt": "Expected: raise TypeError."}]   # the only selection is not verbatim
    _patch_llm(monkeypatch, _draft_llm(), leaky)
    p = draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s1")                                       # zero removals: natural-only scenario
    spec = ScenarioSpec.model_validate_json(p.read_text())
    assert [b.profile for b in spec.briefs] == ["natural"] and not any(f.id.startswith("withheld-") for f in spec.ledger) and not any(o.type == "withholding" for o in spec.operations)
    meta = json.loads((tmp_path / "s1" / "draft.meta.json").read_text())
    assert meta["withholding"]["removed"] == 0 and meta["withholding"]["reason"].startswith("all selections rejected") and meta["withholding"]["spans"] == []
    d = _draft_llm(); d["plausible_wrong"]["diff"] = ""                    # no usable control twice and a gold with a single added line: no real incorrect change exists -> the draft fails
    calls = _patch_llm(monkeypatch, d, _withhold_llm())
    with pytest.raises(ValueError, match="no usable plausible-wrong control"):
        draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s2")
    assert not (tmp_path / "s2" / "scenario.draft.json").exists()
    assert len([c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]]) == 2 and "needs a patch" in [c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]][1]["messages"][-1]["content"]
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


def test_protected_paths_are_code_owned_and_never_shield_gold_files(monkeypatch, tmp_path):
    """pytest-dev--pytest-7283 / seaborn-3190: the model's protected_paths_extra named the source files the gold patch modifies,
    so the oracle control was graded agent-invalid-submission. Only test-tree paths untouched by the gold patch may be added."""
    from swesim.models import ScenarioSpec
    from swesim.stages import draft_spec
    d = _draft_llm(); d["protected_paths_extra"] = ["xarray/core/weighted.py", "docs/whats-new.rst", "xarray/tests/data/w.nc", "src/_pytest/unittest.py", "", None]
    _patch_llm(monkeypatch, d, _withhold_llm())
    p = draft_spec.draft(_row(), _judged(), "fake/model", tmp_path / "s")
    spec = ScenarioSpec.model_validate_json(p.read_text())
    assert spec.contract.protected_paths == ["xarray/tests/data/w.nc", "xarray/tests/test_weighted.py"]
    meta = json.loads((tmp_path / "s" / "draft.meta.json").read_text())
    assert [(x["path"], x["reason"]) for x in meta["protected_paths_dropped"]] == [
        ("xarray/core/weighted.py", "touched by the gold patch"), ("docs/whats-new.rst", "not a test-tree path"),
        ("src/_pytest/unittest.py", "not a test-tree path"), ("", "not a path"), (None, "not a path")]
    assert draft_spec.patch_paths(_row()["patch"]) == {"xarray/core/weighted.py"}
    assert draft_spec.patch_paths("diff --git a/old/name.py b/new/name.py\n--- a/old/name.py\n+++ b/new/name.py\n") == {"old/name.py", "new/name.py"}
    # hard invariant: a hidden test file the gold patch also modifies is a drafter error, not a correction round
    calls = _patch_llm(monkeypatch, _draft_llm(), _withhold_llm())
    shared = "diff --git a/xarray/core/weighted.py b/xarray/core/weighted.py\n--- a/xarray/core/weighted.py\n+++ b/xarray/core/weighted.py\n@@ -1 +1,2 @@\n x\n+z\n"
    with pytest.raises(ValueError, match="drafter error: protected_paths would shield.*xarray/core/weighted.py"):
        draft_spec.draft(_row(test_patch=_row()["test_patch"] + shared), _judged(), "fake/model", tmp_path / "s2")
    assert not (tmp_path / "s2" / "scenario.draft.json").exists()
    assert len([c for c in calls if WITHHOLD_PROMPT_MARK not in c["system"]]) == 1                       # one authoring call, no correction round
    assert draft_spec.protected_paths_for(["tests/test_a.py"], ["tests/fixtures/a.json", "pkg/a.py"], {"pkg/a.py"}) == (["tests/fixtures/a.json", "tests/test_a.py"], [{"path": "pkg/a.py", "reason": "touched by the gold patch"}])
    with pytest.raises(ValueError, match="drafter error"):
        draft_spec.protected_paths_for(["src/_pytest/unittest.py"], [], {"src/_pytest/unittest.py"})


def test_draft_requires_offline_probe_metadata(monkeypatch, tmp_path):
    """SWE-bench's FAIL_TO_PASS/PASS_TO_PASS were produced with network access; drafting needs the offline-measured contract."""
    from swesim.stages import draft_spec
    calls = _patch_llm(monkeypatch, _draft_llm(), _withhold_llm())
    with pytest.raises(ValueError, match="no offline probe metadata"):
        draft_spec.draft(_row(probe=None), _judged(), "fake/model", tmp_path / "s1")
    with pytest.raises(ValueError, match="no offline probe metadata"):
        draft_spec.draft(_row(probe={"error": "unreliable base/gold probe"}), _judged(), "fake/model", tmp_path / "s2")   # a failed probe is not a measurement
    stale = _row(); stale["probe"]["binding"] = "0" * 64                                                                  # measured a different base_commit/test_patch
    with pytest.raises(ValueError, match="binding mismatch"):
        draft_spec.draft(stale, _judged(), "fake/model", tmp_path / "s2b")
    edited = _row(); edited["test_patch"] += "\n"                                                                          # row edited after the probe: the binding no longer matches
    with pytest.raises(ValueError, match="binding mismatch"):
        draft_spec.draft(edited, _judged(), "fake/model", tmp_path / "s2c")
    assert calls == [] and not (tmp_path / "s1").exists()                                                                    # refused before any LLM call or file
    # a repo-URL row probed by mine_github_repo carries the same metadata and is accepted without a second Modal run
    repo_row = _row(source="github-repo", instance_id="py-pdf__pypdf-3826", repo="py-pdf/pypdf", image=None, repo_dir="/app", dockerfile="FROM python@sha256:abc\n",
                    test_python="/usr/local/bin/python", thread={**_thread("py-pdf/pypdf", 3826), "gold_commit": None, "linked_issues": [], "issue_url": None},
                    annotation={"underspecified": "", "underspecified_notes": "", "false_negative": "", "false_negative_notes": "", "difficulty": ""})
    repo_row["probe"].update(kind="dockerfile", repo_dir="/app", job_dir="/ws/jobs/probe/py-pdf--pypdf-3826/trial-1", probed_at="2026-09-12T20:00:00+00:00")
    assert repo_row["probe"]["binding"] == gr.probe_binding(repo_row)
    _patch_llm(monkeypatch, _draft_llm(repo="py-pdf/pypdf", pr=3826), _withhold_llm())
    assert draft_spec.draft(repo_row, _judged(), "fake/model", tmp_path / "s2d").exists()
    _patch_llm(monkeypatch, _draft_llm(), _withhold_llm())
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
    big = _row(patch=_row()["patch"].replace("+y\n", "+# " + "x" * 130000 + "\n"))    # oversized (but well-formed) fix: trimmed in the prompt only
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
    correction = draft_calls[1]["messages"][-1]["content"]
    assert len(draft_calls) == 2 and "Correct these violations" in correction and "f01" in correction
    assert "closest lines" in correction and "Please raise a  ValueError" in correction and "when the dim is missing." in correction   # difflib nearest review lines, quoted raw
    assert not (tmp_path / "s" / "scenario.draft.json").exists()


def test_draft_requires_distinct_judge_and_draft_models(monkeypatch, tmp_path):
    from swesim.stages import draft_spec
    calls = _patch_llm(monkeypatch, _draft_llm(), _withhold_llm())
    with pytest.raises(ValueError, match="judge and draft models must differ"):
        draft_spec.draft(_row(), _judged(model_id="fake/model"), "fake/model", tmp_path / "s")
    assert calls == []  # rejected before any LLM call


def test_withhold_restricts_discovery_to_on_request_or_repo_derivable(monkeypatch):
    from swesim.stages import draft_spec
    out = {"withheld_brief": "Import fails", "removed": [{"category": "error_message", "excerpt": "Traceback: ERROR42 raised here", "kind": "reproduction", "discovery": "author-only"}]}
    attempts = []
    monkeypatch.setattr(draft_spec, "chat_json", lambda model_id, system, messages, schema, **kw: attempts.append(messages) or out)
    res = draft_spec.withhold("Import fails\nTraceback: ERROR42 raised here\nMore context here.", "fake/model")   # rejected twice -> zero removals
    assert res["removed"] == [] and "invalid discovery" in res["stats"]["reason"]
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


def test_withhold_nudges_once_when_nothing_selected_but_issue_has_a_code_block(monkeypatch):
    """Zero selections on an issue with a fenced code block or a traceback gets one nudge; a second empty answer stands as zero
    removals. An issue without such material is accepted as zero removals on the first answer (no extra model call)."""
    from swesim.stages import draft_spec
    issue = "Import fails\nThe importer stops on the first row.\n```\nimport lib; lib.load('x.csv')\n```\nExpected the rows to load."
    calls = []

    def empty_then_select(model_id, system, messages, schema, **kw):
        calls.append(list(messages))
        if len(calls) == 1:
            return {"removed": []}
        return {"removed": [{"category": "reproduction", "excerpt": "import lib; lib.load('x.csv')", "kind": "reproduction", "discovery": "on-request"}]}
    monkeypatch.setattr(draft_spec, "chat_json", empty_then_select)
    res = draft_spec.withhold(issue, "fake/model")
    assert len(calls) == 2 and calls[1][-1]["role"] == "user" and "selected no spans" in calls[1][-1]["content"]
    assert res["stats"]["attempts"] == 2 and len(res["removed"]) == 1 and "lib.load" not in res["withheld_brief"]

    monkeypatch.setattr(draft_spec, "chat_json", lambda *a, **k: {"removed": []})
    res = draft_spec.withhold(issue, "fake/model")
    assert res["removed"] == [] and res["stats"]["attempts"] == 2 and res["stats"]["reason"] == "model selected no spans after one nudge"

    plain = "Import fails\nThe importer stops on the first row.\nExpected the rows to load."
    calls.clear(); monkeypatch.setattr(draft_spec, "chat_json", lambda m, s, msgs, sch, **k: (calls.append(1), {"removed": []})[1])
    res = draft_spec.withhold(plain, "fake/model")
    assert len(calls) == 1 and res["stats"]["attempts"] == 1 and res["stats"]["reason"] == "model selected no spans"
