import json
import subprocess
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
from urllib.parse import parse_qs, urlsplit

import pytest

from swesim.sources import github_repo as gr


# ---------------------------------------------------------------- pure functions (brief)

def test_linked_issue_extraction():
    assert gr.linked_issues("Fixes #12 and closes #34; see #99 unrelated") == [12, 34]


def test_split_patch_by_tests():
    patch = "diff --git a/pypdf/a.py b/pypdf/a.py\n--- a/pypdf/a.py\n+++ b/pypdf/a.py\n@@ -1 +1 @@\n-x\n+y\ndiff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1 +1 @@\n-a\n+b\n"
    src, tst = gr.split_patch(patch)
    assert "pypdf/a.py" in src and "tests/test_a.py" not in src and "tests/test_a.py" in tst


def test_filters_reject_reasons():
    c = {"comments_total": 1, "additions": 5, "deletions": 0, "test_files": [], "source_files": ["a.py"], "issue": None}
    assert gr.reject_reason(c, min_comments=4, diff_min=20, diff_max=600) == "no-linked-issue"
    assert gr.reject_reason({**c, "linked_prs": [8]}, 4, 20, 600) == "linked-target-is-pr"
    assert gr.reject_reason({**c, "linked_missing": [77]}, 4, 20, 600) == "linked-target-missing"


def test_source_stats_count_code_only_but_oracle_patch_keeps_everything():
    patch = (_file("pkg/a.py", 30) + _file("docs/index.md", 400) + _file("CHANGELOG.md", 50) + _file("README.rst", 20) + _file("pyproject.toml", 5)
             + _file("docs/conf.py", 10) + _file("pkg/data.json", 300) + _file("tests/test_a.py", 4))
    src, tst = gr.split_patch(patch)
    assert "docs/index.md" in src and "pyproject.toml" in src and "tests/test_a.py" not in src and "tests/test_a.py" in tst   # oracle patch stays complete
    stats = gr.code_stats(src)
    assert stats["files"] == ["pkg/a.py"] and stats["additions"] == 30 and stats["deletions"] == 0
    assert stats["non_code_files"] == ["docs/index.md", "CHANGELOG.md", "README.rst", "pyproject.toml", "docs/conf.py", "pkg/data.json"]
    assert gr.is_code_path("pypdf/_page.py") and gr.is_code_path("src/lib.rs") and not gr.is_code_path("docs/conf.py") and not gr.is_code_path("CHANGES.txt")


def test_build_recipe_pypdf():
    r = gr.build_recipe("py-pdf/pypdf", "abc")
    assert "pip install -e" in r["install_command"] and r["repo_dir"] == "/app" and "-rA" in r["test_command"]
    assert "not samples and not enable_socket" in r["test_command"] and "{junit_path}" in r["test_command"] and r["test_python"] == "/usr/local/bin/python"
    generic = gr.build_recipe("o/r", "abc")
    assert "pip install -e ." in generic["install_command"] and "  " not in generic["test_command"]


def test_normalize_repo_accepts_urls_and_owner_repo():
    for v in ("https://github.com/py-pdf/pypdf", "https://github.com/py-pdf/pypdf/", "https://github.com/py-pdf/pypdf.git", "github.com/py-pdf/pypdf", "py-pdf/pypdf"):
        assert gr.normalize_repo(v) == "py-pdf/pypdf", v
    for bad in ("pypdf", "git@github.com:py-pdf/pypdf.git", "https://gitlab.com/a/b", "a/b/c"):
        with pytest.raises(ValueError):
            gr.normalize_repo(bad)


def test_reject_reason_order_and_thread_rules():
    thread = {"pr_created_at": "2026-07-01T00:00:00Z", "linked_issues": [{"created_at": "2026-06-01T00:00:00Z"}]}
    base = {"issue": 3, "thread": thread, "test_files": ["tests/test_a.py"], "source_files": ["a.py"], "additions": 100, "deletions": 10, "comments_total": 9}
    assert gr.reject_reason(base, 4, 20, 600) is None
    assert gr.reject_reason({**base, "thread": {**thread, "linked_issues": []}}, 4, 20, 600) == "missing-linked-issue"
    assert gr.reject_reason({**base, "thread": {**thread, "linked_issues": [{"created_at": "2026-07-02T00:00:00Z"}]}}, 4, 20, 600) == "issue-not-before-pr"
    assert gr.reject_reason({**base, "test_files": []}, 4, 20, 600) == "no-test-changes"
    assert gr.reject_reason({**base, "source_files": []}, 4, 20, 600) == "no-source-changes"
    assert gr.reject_reason({**base, "additions": 5, "deletions": 5}, 4, 20, 600) == "diff-too-small"
    assert gr.reject_reason({**base, "additions": 700}, 4, 20, 600) == "diff-too-large"
    assert gr.reject_reason({**base, "comments_total": 3}, 4, 20, 600) == "thin-thread"


def test_candidate_to_row_shapes_like_swebench():
    thread = {"pr_url": "https://github.com/o/r/pull/42", "pr_created_at": "2026-07-01T00:00:00Z",
              "linked_issues": [{"issue_url": "https://github.com/o/r/issues/40", "title": "Add streaming", "body": "We need it.", "created_at": "2026-06-01T00:00:00Z",
                                 "comments": [{"body": "before", "created_at": "2026-06-02T00:00:00Z"}, {"body": "after", "created_at": "2026-07-05T00:00:00Z"}]}]}
    c = {"repo": "o/r", "pr": 42, "title": "pr title", "base_commit": "c" * 40, "patch": "P", "test_patch": "T", "test_files": ["tests/test_s.py"], "thread": thread,
         "discovery": {"kind": "feature", "repo_only": True}}
    row = gr.candidate_to_row(c)
    assert row["source"] == "github-repo" and row["instance_id"] == "o__r-42" and row["base_commit"] == "c" * 40
    assert row["problem_statement"] == "Add streaming\n\nWe need it." and row["hints_text"] == "before"
    assert row["fail_to_pass"] == [] and row["pass_to_pass"] == [] and row["image"] is None and row["repo_dir"] == "/app"
    assert set(row["annotation"]) == {"underspecified", "underspecified_notes", "false_negative", "false_negative_notes", "difficulty"}
    assert row["thread"] is thread and row["discovery"]["repo_only"] is True and row["test_files"] == ["tests/test_s.py"]
    bare = gr.candidate_to_row({**c, "thread": {**thread, "linked_issues": []}})   # no linked issue: PR title, no hints, no crash
    assert bare["problem_statement"] == "pr title" and bare["hints_text"] == ""


# ---------------------------------------------------------------- discovery against a fake gh api

class FakeGH:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, cmd, **kw):
        assert cmd[:2] == ["gh", "api"], cmd
        self.calls.append(cmd)
        u = urlsplit(cmd[2]); q = parse_qs(u.query)
        key = (u.path, "diff") if any("diff" in a for a in cmd[3:]) else u.path
        handler = self.routes.get(key)
        if handler is None:
            raise CalledProcessError(1, cmd, output="", stderr="gh: Not Found (HTTP 404)")
        out = handler(q) if callable(handler) else handler
        if isinstance(out, Exception):
            raise CalledProcessError(1, cmd, output="", stderr=str(out))
        return CompletedProcess(cmd, 0, stdout=out if isinstance(out, str) else json.dumps(out), stderr="")

    def list_pages(self):
        return [parse_qs(urlsplit(c[2]).query)["page"][0] for c in self.calls if urlsplit(c[2]).path == "repos/o/r/pulls"]


def _c(login, body, created, **extra):
    return {"user": {"login": login}, "body": body, "created_at": created, "html_url": "https://github.com/o/r/x#1", **extra}


def _file(path, n_added):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1,{n_added + 1} @@\n x\n" + "".join(f"+line{i}\n" for i in range(n_added))


def _detail(n, merged_at, body, labels, created_at="2026-07-01T00:00:00Z"):
    return {"number": n, "html_url": f"https://github.com/o/r/pull/{n}", "title": f"PR {n}", "body": body, "user": {"login": "dev"}, "merged_at": merged_at,
            "merge_commit_sha": "d" * 40, "created_at": created_at, "base": {"sha": "b" * 40}, "labels": [{"name": l} for l in labels]}


def _routes():
    def pulls(q):
        page = int(q["page"][0]); assert q["state"] == ["closed"] and q["per_page"] == ["100"]
        if page == 1:
            rows = [{"number": n, "merged_at": None} for n in range(1, 101)]           # closed-unmerged: skipped without a detail fetch
            rows[6] = {"number": 7, "merged_at": "2026-07-03T00:00:00Z"}              # merged, tiny diff -> diff-too-small
            rows[8] = {"number": 9, "merged_at": "2026-07-04T00:00:00Z"}              # merged, no linked issue
            rows[10] = {"number": 11, "merged_at": "2026-07-05T00:00:00Z"}            # merged, "Fixes #8" but #8 is a pull request
            rows[41] = {"number": 42}                                                 # list row lacks merged_at/labels/stats: admitted only after the detail fetch
            return rows
        return [{"number": n, "merged_at": "2026-01-01T00:00:00Z"} for n in range(101, 106)]  # short page; merged before `since`
    issue_comments = [_c("u%d" % i, "c%d" % i, "2026-06-0%dT00:00:00Z" % (i + 1)) for i in range(8)]
    return {
        "repos/o/r/pulls": pulls,
        "repos/o/r/pulls/42": _detail(42, "2026-07-10T00:00:00Z", "Implements streaming.\n\nCloses #40", ["enhancement", "needs-docs"]),
        ("repos/o/r/pulls/42", "diff"): _file("pkg/a.py", 120) + _file("pkg/b.py", 80) + _file("pkg/c.py", 50) + _file("tests/test_stream.py", 30),
        "repos/o/r/issues/40": {"html_url": "https://github.com/o/r/issues/40", "title": "Streaming support", "body": "Please add streaming.", "user": {"login": "rep"}, "created_at": "2026-06-01T00:00:00Z"},
        "repos/o/r/issues/40/comments": issue_comments,
        "repos/o/r/issues/42/comments": [], "repos/o/r/pulls/42/reviews": [], "repos/o/r/pulls/42/comments": [],
        "repos/o/r/pulls/7": _detail(7, "2026-07-03T00:00:00Z", "Fixes #6", ["bug"]),
        ("repos/o/r/pulls/7", "diff"): _file("pkg/a.py", 3) + _file("tests/test_a.py", 2),
        "repos/o/r/issues/6": {"html_url": "https://github.com/o/r/issues/6", "title": "bug", "body": "", "user": {"login": "rep"}, "created_at": "2026-06-01T00:00:00Z"},
        "repos/o/r/issues/6/comments": [], "repos/o/r/issues/7/comments": [], "repos/o/r/pulls/7/reviews": [], "repos/o/r/pulls/7/comments": [],
        "repos/o/r/pulls/9": _detail(9, "2026-07-04T00:00:00Z", "no link here", ["bug"]),
        "repos/o/r/issues/9/timeline": [],
        ("repos/o/r/pulls/9", "diff"): _file("pkg/a.py", 30) + _file("tests/test_a.py", 2),
        "repos/o/r/pulls/11": _detail(11, "2026-07-05T00:00:00Z", "Fixes #8", ["bug"]),
        "repos/o/r/issues/8": {"html_url": "https://github.com/o/r/pull/8", "number": 8, "title": "a PR", "body": "", "user": {"login": "dev"}, "created_at": "2026-06-01T00:00:00Z", "pull_request": {"url": "x"}},
        "repos/o/r/issues/11/timeline": [],
        ("repos/o/r/pulls/11", "diff"): _file("pkg/a.py", 300) + _file("tests/test_a.py", 2),
    }


def test_discover_pages_filters_and_admits_feature_after_detail_fetch(monkeypatch):
    gh = FakeGH(_routes())
    monkeypatch.setattr(subprocess, "run", gh)
    cands, rejects = gr.discover("https://github.com/o/r", since="2026-06-01", max_prs=150, kind="feature")
    assert gh.list_pages() == ["1", "2"]                                   # more than one page, stops on the short page
    assert rejects == {"diff-too-small": 1, "no-linked-issue": 1, "linked-target-is-pr": 1}
    assert [c["pr"] for c in cands] == [42]
    c = cands[0]
    assert c["repo"] == "o/r" and c["issue"] == 40 and c["base_commit"] == "b" * 40 and c["merge_commit"] == "d" * 40
    assert c["source_files"] == ["pkg/a.py", "pkg/b.py", "pkg/c.py"] and c["test_files"] == ["tests/test_stream.py"]
    assert c["additions"] == 250 and c["deletions"] == 0                    # sizes come from the diff, not list-response statistics
    assert "tests/test_stream.py" not in c["patch"] and "tests/test_stream.py" in c["test_patch"]
    assert c["comments_total"] == 8 and c["labels"] == ["enhancement", "needs-docs"] and c["linked_prs"] == [] and c["non_code_files"] == []
    assert c["issue_body"] == "Please add streaming." and c["thread"]["linked_issues"][0]["number"] == 40
    assert c["discovery"] == {"kind": "feature", "repo_only": True, "since": "2026-06-01", "max_prs": 150}
    detail_fetches = [x for x in gh.calls if urlsplit(x[2]).path.startswith("repos/o/r/pulls/") and "-H" not in x]
    assert {urlsplit(x[2]).path for x in detail_fetches} >= {"repos/o/r/pulls/42", "repos/o/r/pulls/7", "repos/o/r/pulls/9"}
    assert not any(urlsplit(x[2]).path == "repos/o/r/pulls/1" for x in gh.calls)  # closed-unmerged rows never cost a detail fetch


def test_discover_bugfix_kind_and_no_survivors(monkeypatch):
    gh = FakeGH(_routes())
    monkeypatch.setattr(subprocess, "run", gh)
    cands, rejects = gr.discover("o/r", since="2026-06-01", max_prs=150, kind="bugfix")
    # bugfix band (min_comments 4, 20..600 lines): 42 has 8 comments, 250 lines -> accepted; 7 too small; 9 no issue; 11 links a PR
    assert [c["pr"] for c in cands] == [42] and rejects == {"diff-too-small": 1, "no-linked-issue": 1, "linked-target-is-pr": 1}
    cands, rejects = gr.discover("o/r", since="2026-08-01", max_prs=150, kind="feature")
    assert cands == [] and rejects == {}
    with pytest.raises(ValueError, match="kind"):
        gr.discover("o/r", since="2026-06-01", kind="docs")


def test_discover_feature_band_rejects(monkeypatch):
    routes = _routes()
    routes["repos/o/r/pulls/42"] = _detail(42, "2026-07-10T00:00:00Z", "Closes #40", ["bug"])
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    cands, rejects = gr.discover("o/r", since="2026-06-01", max_prs=150, kind="feature")
    assert cands == [] and rejects["feature-label-missing"] == 1
    routes = _routes()
    routes[("repos/o/r/pulls/42", "diff")] = _file("pkg/a.py", 250) + _file("tests/test_stream.py", 30)
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    cands, rejects = gr.discover("o/r", since="2026-06-01", max_prs=150, kind="feature")
    assert cands == [] and rejects["feature-source-file-band"] == 1


def test_discover_pr_override_is_recorded_as_not_repo_only(monkeypatch):
    gh = FakeGH(_routes())
    monkeypatch.setattr(subprocess, "run", gh)
    cands, rejects = gr.discover("o/r", since="2026-06-01", pr=42, kind="feature")
    assert [c["pr"] for c in cands] == [42] and cands[0]["discovery"]["repo_only"] is False and gh.list_pages() == []


def test_discover_records_fetch_failures_instead_of_aborting(monkeypatch):
    routes = _routes(); del routes[("repos/o/r/pulls/42", "diff")]
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    cands, rejects = gr.discover("o/r", since="2026-06-01", max_prs=150, kind="feature")
    assert cands == [] and rejects["fetch-error"] == 1
    # a non-404 failure while classifying a closing link is infrastructure (fetch-error), a 404 is data (linked-target-missing)
    routes = _routes(); routes["repos/o/r/issues/40"] = Exception("gh: HTTP 502 Bad Gateway")
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    cands, rejects = gr.discover("o/r", since="2026-06-01", max_prs=150, kind="feature")
    assert cands == [] and rejects["fetch-error"] == 1 and "linked-target-missing" not in rejects
    routes = _routes(); del routes["repos/o/r/issues/40"]; routes["repos/o/r/issues/42/timeline"] = []
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    cands, rejects = gr.discover("o/r", since="2026-06-01", max_prs=150, kind="feature")
    assert cands == [] and rejects["linked-target-missing"] == 1 and "fetch-error" not in rejects


# ---------------------------------------------------------------- probe_tests (Harbor + compile helpers mocked)

def _probe_row():
    return {"instance_id": "o__r-42", "repo": "o/r", "base_commit": "b" * 40, "install_command": "pip install -e .", "test_files": ["tests/test_stream.py", "tests/data/x.pdf"],
            "test_patch": "TP", "patch": "OP", "repo_dir": "/app"}


def _setup_probe(monkeypatch, tmp_path, probe_json):
    from swesim import harbor_cli
    vsrc = tmp_path / "verifier"; vsrc.mkdir(exist_ok=True)
    for n in ("grader.py", "policy.py", "parsers.py", "run_pytest.py"): (vsrc / n).write_text(f"# {n}\n")
    monkeypatch.setattr(gr, "_compile_imports", lambda: (lambda image: "sha256:" + "e" * 64, vsrc))
    seen = {}

    def fake_run_task(task_dir, agent, jobs_dir, job_name, **kw):
        seen.update(task_dir=task_dir, agent=agent, jobs_dir=jobs_dir, job_name=job_name, kw=kw)
        td = jobs_dir / job_name / "trial-1"; (td / "verifier").mkdir(parents=True)
        (td / "verifier" / "probe.json").write_text(json.dumps(probe_json)); (td / "result.json").write_text("{}")
        return td
    monkeypatch.setattr(harbor_cli, "run_task", fake_run_task)
    return seen


def test_probe_tests_builds_probe_task_and_derives_f2p_p2p(monkeypatch, tmp_path):
    from swesim.paths import Workspace
    ws = Workspace(tmp_path / "ws")
    probe = {"base": {"statuses": {}, "exit_code": 4, "error": "no tests", "tail": ""},
             "test": {"statuses": {"tests/test_stream.py::test_new": "failed", "tests/test_stream.py::test_old": "passed", "tests/test_stream.py::test_err": "error"}, "exit_code": 1, "error": None, "tail": ""},
             "gold": {"statuses": {"tests/test_stream.py::test_new": "passed", "tests/test_stream.py::test_old": "passed", "tests/test_stream.py::test_err": "passed"}, "exit_code": 0, "error": None, "tail": ""}}
    seen = _setup_probe(monkeypatch, tmp_path, probe)
    row = _probe_row()
    out = gr.probe_tests(row, ws)
    assert out["fail_to_pass"] == ["tests/test_stream.py::test_new", "tests/test_stream.py::test_err"] and out["pass_to_pass"] == ["tests/test_stream.py::test_old"]
    assert out["test_python"] == "/usr/local/bin/python" and out["dockerfile"].startswith("FROM python@sha256:") and row["dockerfile"] == out["dockerfile"]
    assert "git checkout " + "b" * 40 in out["dockerfile"] and "pip install -e ." in out["dockerfile"] and "https://github.com/o/r" in out["dockerfile"]
    root = ws.root / "probes" / "o--r-42"
    assert seen["task_dir"] == root and seen["agent"] == "nop" and seen["jobs_dir"] == ws.jobs_dir / "probe" and seen["job_name"] == "o--r-42"
    assert (root / "environment" / "Dockerfile").read_text() == out["dockerfile"] and (root / "tests" / "Dockerfile").read_text().endswith("COPY . /tests/\n")
    assert {p.name for p in (root / "tests").glob("*.py")} >= {"grader.py", "policy.py", "parsers.py", "run_pytest.py", "probe.py"}
    assert (root / "tests" / "test.patch").read_text() == "TP" and (root / "tests" / "oracle.diff").read_text() == "OP"
    cfg = json.loads((root / "tests" / "probe_config.json").read_text())
    assert cfg["command"].startswith("/usr/local/bin/python -I /tests/run_pytest.py -rA -p no:cacheprovider --junitxml={junit_path} ") and cfg["command"].endswith("tests/test_stream.py") and ".pdf" not in cfg["command"]
    toml = (root / "task.toml").read_text()
    assert 'environment_mode="separate"' in toml and 'network_mode="no-network"' in toml and (root / "instruction.md").exists()
    assert out["probe_dir"] == str(seen["jobs_dir"] / "o--r-42" / "trial-1")


def test_probe_tests_rejects_unreliable_or_uncovered_probes(monkeypatch, tmp_path):
    from swesim.paths import Workspace
    ok = {"statuses": {"t::a": "passed", "t::b": "failed"}, "exit_code": 1, "error": None, "tail": ""}
    gold = {"statuses": {"t::a": "passed", "t::b": "passed"}, "exit_code": 0, "error": None, "tail": ""}
    _setup_probe(monkeypatch, tmp_path, {"base": ok, "test": ok, "gold": {**gold, "error": "timeout"}})
    with pytest.raises(ValueError, match="unreliable"):
        gr.probe_tests(_probe_row(), Workspace(tmp_path / "ws1"))
    _setup_probe(monkeypatch, tmp_path, {"base": ok, "test": ok, "gold": {**gold, "statuses": {"t::a": "passed"}}})
    with pytest.raises(ValueError, match="unreliable"):
        gr.probe_tests(_probe_row(), Workspace(tmp_path / "ws2"))
    _setup_probe(monkeypatch, tmp_path, {"base": ok, "test": gold, "gold": gold})  # nothing fails before gold: no target failure
    with pytest.raises(ValueError, match="target failure"):
        gr.probe_tests(_probe_row(), Workspace(tmp_path / "ws3"))
    only_f = {"statuses": {"t::b": "failed"}, "exit_code": 1, "error": None, "tail": ""}
    _setup_probe(monkeypatch, tmp_path, {"base": only_f, "test": only_f, "gold": {"statuses": {"t::b": "passed"}, "exit_code": 0, "error": None, "tail": ""}})
    with pytest.raises(ValueError, match="regression coverage"):
        gr.probe_tests(_probe_row(), Workspace(tmp_path / "ws4"))


def test_probe_tests_requires_a_base_image_digest(monkeypatch, tmp_path):
    from swesim.paths import Workspace
    monkeypatch.setattr(gr, "_compile_imports", lambda: (lambda image: None, tmp_path))
    with pytest.raises(RuntimeError, match="digest"):
        gr.probe_tests(_probe_row(), Workspace(tmp_path / "ws"))


# ---------------------------------------------------------------- mine_github_repo (discovery + probe mocked)

def _cand(pr):
    return {"repo": "o/r", "pr": pr, "title": f"PR {pr}", "base_commit": "b" * 40, "merge_commit": "d" * 40, "patch": "P", "test_patch": "T",
            "source_files": ["pkg/a.py"], "test_files": ["tests/test_a.py"], "issue": 1, "additions": 300, "deletions": 0, "labels": ["enhancement"],
            "thread": {"pr_url": f"https://github.com/o/r/pull/{pr}", "pr_created_at": "2026-07-01T00:00:00Z", "pr_comments": [], "review_comments": [],
                       "linked_issues": [{"issue_url": "https://github.com/o/r/issues/1", "title": "t", "body": "b", "created_at": "2026-06-01T00:00:00Z", "comments": []}]},
            "discovery": {"kind": "feature", "repo_only": True, "since": "2026-06-01", "max_prs": 400}}


def test_mine_github_repo_writes_rows_rejects_and_respects_build_budget(monkeypatch, tmp_path):
    from swesim.paths import Workspace
    from swesim.stages.mine import mine_github_repo
    monkeypatch.setattr(gr, "discover", lambda repo, since, **kw: ([_cand(1), _cand(2), _cand(3)], {"thin-thread": 4}))
    probes = []

    def fake_probe(row, ws):
        probes.append(row["instance_id"])
        if row["instance_id"] == "o__r-2":
            raise ValueError("unreliable base/gold probe; keep evidence and reject")
        return {"fail_to_pass": ["tests/test_a.py::t_new"], "pass_to_pass": ["tests/test_a.py::t_old"], "dockerfile": "FROM x\n", "probe_dir": "/j", "test_python": "/usr/local/bin/python"}
    monkeypatch.setattr(gr, "probe_tests", fake_probe)
    ws = Workspace(tmp_path / "ws")
    out = mine_github_repo(ws, "https://github.com/o/r", max_builds=2)
    assert out == ws.candidates_dir / "github-repo--o__r.jsonl" and probes == ["o__r-1", "o__r-2"]
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert [r["instance_id"] for r in rows] == ["o__r-1"] and rows[0]["fail_to_pass"] == ["tests/test_a.py::t_new"] and rows[0]["dockerfile"] == "FROM x\n"
    assert rows[0]["install_command"] and rows[0]["repo_dir"] == "/app" and rows[0]["source"] == "github-repo"
    rejects = json.loads(out.with_suffix(".rejects.json").read_text())
    assert rejects == {"thin-thread": 4, "build-or-probe-failed": 1, "build-budget-exhausted": 1}
    assert json.loads((out.parent / "o__r-2.error.json").read_text())["error"].startswith("unreliable")
    assert not out.with_suffix(".status").exists()


def test_mine_github_repo_no_valid_candidate_status(monkeypatch, tmp_path):
    from swesim.paths import Workspace
    from swesim.stages.mine import mine_github_repo
    monkeypatch.setattr(gr, "discover", lambda repo, since, **kw: ([], {"no-linked-issue": 12, "diff-too-small": 3}))
    monkeypatch.setattr(gr, "probe_tests", lambda row, ws: pytest.fail("no candidate should be probed"))
    ws = Workspace(tmp_path / "ws")
    out = mine_github_repo(ws, "o/r", since="2026-06-01")
    assert out.read_text() == "" and out.with_suffix(".status").read_text() == "no-valid-candidate"
    assert json.loads(out.with_suffix(".rejects.json").read_text()) == {"no-linked-issue": 12, "diff-too-small": 3}


def test_mine_github_repo_drops_probe_without_f2p(monkeypatch, tmp_path):
    from swesim.paths import Workspace
    from swesim.stages.mine import mine_github_repo
    monkeypatch.setattr(gr, "discover", lambda repo, since, **kw: ([_cand(1)], {}))
    monkeypatch.setattr(gr, "probe_tests", lambda row, ws: {"fail_to_pass": [], "pass_to_pass": ["t::a"], "dockerfile": "FROM x\n", "probe_dir": "/j", "test_python": "/usr/local/bin/python"})
    out = mine_github_repo(Workspace(tmp_path / "ws"), "o/r")
    assert out.read_text() == "" and json.loads(out.with_suffix(".rejects.json").read_text()) == {"no-f2p-after-probe": 1}
