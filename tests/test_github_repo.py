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
    assert cands == [] and rejects["not-found"] == 1 and "fetch-error" not in rejects   # a 404 is this PR's answer, not infrastructure
    # a 5xx while classifying a closing link is gh being unavailable: the attempt fails (Temporal retries), no PR is counted
    routes = _routes(); routes["repos/o/r/issues/40"] = Exception("gh: HTTP 502 Bad Gateway")
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    with pytest.raises(gr.DiscoveryUnavailable) as exc:
        gr.discover("o/r", since="2026-06-01", max_prs=150, kind="feature")
    assert exc.value.kind == "server-error"
    # an unclassifiable gh failure for one PR stays a fetch-error observation, and a 404 on the issue is data (linked-target-missing)
    routes = _routes(); routes["repos/o/r/issues/40"] = Exception("gh: something unexpected happened")
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
    """Fake Harbor: the probe.json the verifier would write. Unless the fixture says otherwise, the evidence is bound to the
    row the probe task was built for and ran as the grading user, exactly as PROBE_SCRIPT records from probe_config.json."""
    from swesim import harbor_cli
    vsrc = tmp_path / "verifier"; vsrc.mkdir(exist_ok=True)
    for n in ("grader.py", "policy.py", "parsers.py", "run_pytest.py"): (vsrc / n).write_text(f"# {n}\n")
    monkeypatch.setattr(gr, "_compile_imports", lambda: (lambda image: "sha256:" + "e" * 64, vsrc))
    seen = {}

    def fake_run_task(task_dir, agent, jobs_dir, job_name, **kw):
        seen.update(task_dir=task_dir, agent=agent, jobs_dir=jobs_dir, job_name=job_name, kw=kw)
        cfg = json.loads((Path(task_dir) / "tests" / "probe_config.json").read_text())
        data = {k: v for k, v in {"binding": cfg["binding"], "test_user": cfg["test_user"], **probe_json}.items() if v is not None}   # None = absent
        td = jobs_dir / job_name / "trial-1"; (td / "verifier").mkdir(parents=True)
        (td / "verifier" / "probe.json").write_text(json.dumps(data)); (td / "result.json").write_text("{}")
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
    binding = gr.probe_binding(row)
    assert seen["task_dir"] == root and seen["agent"] == "nop" and seen["jobs_dir"] == ws.jobs_dir / "probe"
    assert seen["job_name"] == "o--r-42--" + binding[:16]                              # B-P2-1: a job dir is named for what it measured
    assert (root / "environment" / "Dockerfile").read_text() == out["dockerfile"] and (root / "tests" / "Dockerfile").read_text().endswith("COPY . /tests/\n")
    assert "/usr/local/bin/swesim-python" in (root / "tests" / "Dockerfile").read_text() and (root / "tests" / "test.sh").read_text().endswith("/usr/local/bin/swesim-python -I /tests/probe.py\n")
    assert {p.name for p in (root / "tests").glob("*.py")} >= {"grader.py", "policy.py", "parsers.py", "run_pytest.py", "probe.py"}
    assert (root / "tests" / "test.patch").read_text() == "TP" and (root / "tests" / "oracle.diff").read_text() == "OP"
    cfg = json.loads((root / "tests" / "probe_config.json").read_text())
    assert cfg["command"].startswith("/usr/local/bin/python -I /tests/run_pytest.py -rA -p no:cacheprovider --junitxml={junit_path} ") and cfg["command"].endswith("tests/test_stream.py") and ".pdf" not in cfg["command"]
    assert cfg["repo_dir"] == "/app" and cfg["kind"] == "dockerfile" and out["probe"]["kind"] == "dockerfile" and out["dropped"] == {"network_or_env": [], "gold_regressions": [], "other": [], "order_dependent": []}
    toml = (root / "task.toml").read_text()
    assert 'environment_mode="separate"' in toml and 'network_mode="no-network"' in toml and (root / "instruction.md").exists()
    assert out["probe_dir"] == str(seen["jobs_dir"] / ("o--r-42--" + binding[:16]) / "trial-1")
    # B-P2-1: the binding travels with the evidence and the probe runs under the grading identity
    assert cfg["binding"] == binding and cfg["test_user"] == gr.GRADING_USER == "tester" and out["probe"]["test_user"] == "tester"
    tests_df = (root / "tests" / "Dockerfile").read_text()
    assert "useradd -m tester" in tests_df and tests_df.index("useradd -m tester") < tests_df.index("COPY . /tests/")
    probe_py = (root / "tests" / "probe.py").read_text()
    assert "'binding':probe.get('binding')" in probe_py and "user=probe.get('test_user')" in probe_py and "_chown_tree(repo,user)" in probe_py
    assert "safe.directory=*" in probe_py and "'test_user':user" in probe_py


def test_probe_reuse_requires_the_recorded_binding_to_match(monkeypatch, tmp_path):
    """B-P2-1: a job dir that exists is reconciled, never relaunched, so its evidence must say what it measured. A probe.json
    whose recorded binding differs from the row (or that carries none) is rejected; the row's own hash is never stamped on it."""
    from swesim.paths import Workspace
    ok = {"statuses": {"t::a": "passed", "t::b": "failed"}, "exit_code": 1, "error": None, "tail": ""}
    gold = {"statuses": {"t::a": "passed", "t::b": "passed"}, "exit_code": 0, "error": None, "tail": ""}
    row = _probe_row()
    _setup_probe(monkeypatch, tmp_path, {"base": ok, "test": ok, "gold": gold, "binding": "0" * 64, "test_user": "tester"})
    with pytest.raises(ValueError, match="binding"):
        gr.probe_tests(dict(row), Workspace(tmp_path / "ws1"))
    _setup_probe(monkeypatch, tmp_path, {"base": ok, "test": ok, "gold": gold, "binding": None})                        # unbound evidence
    with pytest.raises(ValueError, match="binding"):
        gr.probe_tests(dict(row), Workspace(tmp_path / "ws2"))
    _setup_probe(monkeypatch, tmp_path, {"base": ok, "test": ok, "gold": gold, "test_user": None})                      # ran as root: not the grading identity
    with pytest.raises(ValueError, match="tester"):
        gr.probe_tests(dict(row), Workspace(tmp_path / "ws3"))
    _setup_probe(monkeypatch, tmp_path, {"base": ok, "test": ok, "gold": gold, "binding": gr.probe_binding(row), "test_user": "tester"})
    out = gr.probe_tests(dict(row), Workspace(tmp_path / "ws4"))
    assert out["probe"]["binding"] == gr.probe_binding(row) and out["fail_to_pass"] == ["t::b"]
    # a changed hidden test patch is a different job name, so an old job dir under the old name is never picked up
    changed = {**row, "test_patch": "TP2"}
    seen = _setup_probe(monkeypatch, tmp_path, {"base": ok, "test": ok, "gold": gold, "binding": gr.probe_binding(changed), "test_user": "tester"})
    gr.probe_tests(dict(changed), Workspace(tmp_path / "ws5"))
    assert seen["job_name"] == "o--r-42--" + gr.probe_binding(changed)[:16] != "o--r-42--" + gr.probe_binding(row)[:16]


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
    _setup_probe(monkeypatch, tmp_path, {"base": ok, "test": gold, "gold": gold})  # nothing fails before gold: measured F2P is empty, the caller rejects
    out = gr.probe_tests(_probe_row(), Workspace(tmp_path / "ws3"))
    assert out["fail_to_pass"] == [] and out["pass_to_pass"] == ["t::a", "t::b"] and out["probe"]["counts"]["fail_to_pass"] == 0
    only_f = {"statuses": {"t::b": "failed"}, "exit_code": 1, "error": None, "tail": ""}
    _setup_probe(monkeypatch, tmp_path, {"base": only_f, "test": only_f, "gold": {"statuses": {"t::b": "passed"}, "exit_code": 0, "error": None, "tail": ""}})
    out = gr.probe_tests(_probe_row(), Workspace(tmp_path / "ws4"))                # no regression coverage is recorded, not fatal
    assert out["fail_to_pass"] == ["t::b"] and out["pass_to_pass"] == [] and out["dropped"] == {"network_or_env": [], "gold_regressions": [], "other": [], "order_dependent": []}


def _swebench_probe_row():
    tp = ("diff --git a/xarray/tests/test_weighted.py b/xarray/tests/test_weighted.py\n--- a/xarray/tests/test_weighted.py\n+++ b/xarray/tests/test_weighted.py\n"
          "@@ -1 +1,2 @@\n x\n+def test_new(): pass\ndiff --git a/xarray/tests/data/w.nc b/xarray/tests/data/w.nc\nBinary files differ\n")
    f = "xarray/tests/test_weighted.py::"
    return {"instance_id": "pydata__xarray-5362", "repo": "pydata/xarray", "base_commit": "a" * 40, "image": "swebench/sweb.eval.x86_64.pydata_1776_xarray-5362:latest",
            "repo_dir": "/testbed", "test_patch": tp, "patch": "OP", "fail_to_pass": [f + "test_new", f + "test_needs_network"], "pass_to_pass": [f + "test_old", f + "test_flaky"]}


def _swebench_statuses():
    f = "xarray/tests/test_weighted.py::"
    test = {f + "test_new": "failed", f + "test_old": "passed", f + "test_needs_network": "failed", f + "test_flaky": "passed", f + "test_extra": "passed"}
    gold = {f + "test_new": "passed", f + "test_old": "passed", f + "test_needs_network": "error", f + "test_flaky": "failed", f + "test_extra": "passed"}
    return {"base": {"statuses": {}, "exit_code": 4, "error": "no tests collected", "tail": ""},
            "test": {"statuses": test, "exit_code": 1, "error": None, "tail": ""}, "gold": {"statuses": gold, "exit_code": 1, "error": None, "tail": ""}}


def test_probe_tests_measures_swebench_image_rows_offline(monkeypatch, tmp_path):
    from swesim.paths import Workspace
    ws = Workspace(tmp_path / "ws")
    seen = _setup_probe(monkeypatch, tmp_path, _swebench_statuses())
    row = _swebench_probe_row()
    out = gr.probe_tests(row, ws)
    f = "xarray/tests/test_weighted.py::"
    assert out["fail_to_pass"] == [f + "test_new"] and out["pass_to_pass"] == [f + "test_old", f + "test_extra"]
    assert out["dropped"] == {"network_or_env": [f + "test_needs_network"], "gold_regressions": [f + "test_flaky"], "other": [], "order_dependent": []}
    assert out["probe"]["swebench"] == {"fail_to_pass_not_reproduced": [f + "test_needs_network"], "pass_to_pass_not_reproduced": [f + "test_flaky"], "fail_to_pass_new": []}
    assert out["probe"]["counts"] == {"fail_to_pass": 1, "pass_to_pass": 2, "network_or_env": 1, "gold_regressions": 1, "other": 0, "order_dependent": 0, "measured": 5}
    assert out["probe"]["runner"]["exact"] == "missing" and out["probe"]["order"] == [f + "test_new", f + "test_old", f + "test_extra"]   # contract ids in collected order; no exact stages in this fixture
    assert out["probe"]["errors"] == {"base": "no tests collected", "test": None, "gold": None}   # the base collect error is expected and kept for audit
    assert sum(v for k, v in out["probe"]["counts"].items() if k != "measured") == out["probe"]["counts"]["measured"]
    assert out["probe"]["kind"] == "image" and out["test_python"] == gr.SWEBENCH_PYTHON and out["probe"]["repo_dir"] == "/testbed"
    assert out["statuses"]["test"][f + "test_new"] == "failed" and out["statuses"]["gold"][f + "test_new"] == "passed" and out["statuses"]["base"] == {}
    root = ws.root / "probes" / "pydata--xarray-5362"
    assert out["dockerfile"] == "FROM swebench/sweb.eval.x86_64.pydata_1776_xarray-5362@sha256:" + "e" * 64 + "\nWORKDIR /testbed\n"   # pinned by digest, no rebuild
    assert (root / "environment" / "Dockerfile").read_text() == out["dockerfile"] and (root / "tests" / "Dockerfile").read_text().endswith("COPY . /tests/\n")
    cfg = json.loads((root / "tests" / "probe_config.json").read_text())
    assert cfg["command"] == gr.SWEBENCH_PYTHON + " -I /tests/run_pytest.py -rA -p no:cacheprovider --junitxml={junit_path} xarray/tests/test_weighted.py"   # all tests of the touched .py files
    assert cfg["repo_dir"] == "/testbed" and cfg["kind"] == "image" and cfg["test_timeout_sec"] == gr.PROBE_TIMEOUT_SEC
    # scikit-learn__scikit-learn-13584: the testbed interpreter predates `from __future__ import annotations`, so the probe
    # controller must run under the verifier image's pinned swesim-python; only the pytest subprocess uses the task interpreter
    assert (root / "tests" / "test.sh").read_text() == "#!/bin/bash\nset -euo pipefail\n/usr/local/bin/swesim-python -I /tests/probe.py\n"
    tests_df = (root / "tests" / "Dockerfile").read_text()
    assert tests_df.startswith(out["dockerfile"]) and tests_df.endswith("COPY . /tests/\n")
    assert "uv python install 3.12" in tests_df and 'ln -s "$(uv python find 3.12)" /usr/local/bin/swesim-python' in tests_df and "astral.sh/uv/install.sh" in tests_df
    assert gr.SWEBENCH_PYTHON not in (root / "tests" / "test.sh").read_text() and cfg["command"].startswith(gr.SWEBENCH_PYTHON)
    assert gr.SWEBENCH_PYTHON not in (root / "environment" / "Dockerfile").read_text() and "uv python install" not in (root / "environment" / "Dockerfile").read_text()
    probe_py = (root / "tests" / "probe.py").read_text()
    assert "probe.get('repo_dir'" in probe_py and "cwd=repo" in probe_py and "'/app'" not in probe_py.replace("probe.get('repo_dir','/app')", "")
    job = "pydata--xarray-5362--" + gr.probe_binding(row)[:16]
    assert seen["agent"] == "nop" and seen["job_name"] == job and out["probe"]["probe_dir"] == str(seen["jobs_dir"] / job / "trial-1")
    assert out["probe"]["job_dir"] == out["probe"]["probe_dir"] and out["probe"]["stages"] == ["base", "test", "gold"]
    assert out["probe"]["binding"] == gr.probe_binding(row) and out["probe"]["probed_at"].endswith("+00:00")            # bound to base_commit + test_patch
    assert gr.probe_binding({**row, "test_patch": row["test_patch"] + "\n"}) != out["probe"]["binding"]
    # without a Docker Hub answer the image tag is used as-is
    monkeypatch.setattr(gr, "_compile_imports", lambda: (lambda image: None, tmp_path / "verifier"))
    out = gr.probe_tests(_swebench_probe_row(), Workspace(tmp_path / "ws2"))
    assert out["dockerfile"].startswith("FROM swebench/sweb.eval.x86_64.pydata_1776_xarray-5362:latest\n") and out["probe"]["image_digest"] is None
    with pytest.raises(ValueError, match="no python test files"):
        gr.probe_tests({**_swebench_probe_row(), "test_patch": "diff --git a/docs/x.rst b/docs/x.rst\n"}, Workspace(tmp_path / "ws3"))


def test_classify_probe_partitions_every_test_including_skip_xfail_xpass():
    """Review v4: skipped/xfail/xpass transitions vanished from `dropped` and `counts`; every measured test must land in one bucket."""
    import tomllib
    before = {"f2p": "failed", "p2p": "passed", "net": "error", "reg": "passed", "xp": "passed",
              "sk_pass": "skipped", "xf_pass": "xfail", "xpass_same": "xpass", "fail_skip": "failed", "sk_same": "skipped"}
    after = {"f2p": "passed", "p2p": "passed", "net": "failed", "reg": "failed", "xp": "xpass",
             "sk_pass": "passed", "xf_pass": "passed", "xpass_same": "xpass", "fail_skip": "skipped", "sk_same": "skipped"}
    got = gr.classify_probe(before, after)
    assert got["fail_to_pass"] == ["f2p"] and got["pass_to_pass"] == ["p2p"]
    assert got["dropped"] == {"network_or_env": ["net"], "gold_regressions": ["reg", "xp"], "other": ["sk_pass", "xf_pass", "xpass_same", "fail_skip", "sk_same"]}
    buckets = [got["fail_to_pass"], got["pass_to_pass"], *got["dropped"].values()]
    assert sorted(t for b in buckets for t in b) == sorted(before) and sum(map(len, buckets)) == len(before)   # a partition: no test vanishes
    # the verifier budget must outlast three full stages
    cfg = tomllib.loads(gr.PROBE_TASK_TOML)
    assert cfg["verifier"]["timeout_sec"] == gr.PROBE_VERIFIER_TIMEOUT_SEC >= 3 * gr.PROBE_TIMEOUT_SEC + 300 and cfg["verifier"]["environment_mode"] == "separate"


def test_probe_is_grading_faithful_exact_ids_in_definition_order(monkeypatch, tmp_path):
    """scikit-learn-13584: the whole-file probe passed test_basic, the exact-id grading run failed it (pollution from an F2P
    test that ran first). The probe now re-runs the exact ids with the grader's own command shape and drops order-dependent tests."""
    from swesim.paths import Workspace
    f = "sklearn/utils/tests/test_pprint.py::"
    order = [f + "test_basic", f + "test_changed_only", f + "test_deeply_nested", f + "test_n_max_elements"]
    full_test = {order[0]: "passed", order[1]: "failed", order[2]: "failed", order[3]: "passed"}
    full_gold = {t: "passed" for t in order}
    exact_test = {**full_test, order[0]: "failed"}                     # test_basic flips in the exact-id run (pollution the grader sees, the whole-file run hid)
    data = {"base": {"statuses": {}, "exit_code": 4, "error": "no tests collected", "tail": ""},
            "test": {"statuses": full_test, "exit_code": 1, "error": None, "tail": ""}, "order": order,
            "exact_command": "CMD", "test_exact": {"statuses": exact_test, "exit_code": 1, "error": None, "tail": ""},
            "gold": {"statuses": full_gold, "exit_code": 0, "error": None, "tail": ""},
            "gold_exact": {"statuses": {**full_gold, order[3]: "failed"}, "exit_code": 1, "error": None, "tail": ""}}
    _setup_probe(monkeypatch, tmp_path, data)
    row = {**_swebench_probe_row(), "test_patch": "diff --git a/sklearn/utils/tests/test_pprint.py b/sklearn/utils/tests/test_pprint.py\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n x\n+def test_changed_only(): pass\n",
           "fail_to_pass": [order[1], order[2]], "pass_to_pass": [order[0], order[3]]}
    out = gr.probe_tests(row, Workspace(tmp_path / "ws"))
    assert out["probe"]["order"] == order and out["probe"]["runner"]["order"] == "definition" and out["probe"]["runner"]["exact"] == "exact"
    assert out["probe"]["runner"]["prefix"] == gr.runner_prefix(gr.SWEBENCH_PYTHON) and out["probe"]["runner"]["exact_command"] == "CMD"
    assert out["dropped"]["order_dependent"] == [order[0], order[3]]                                     # status differs between whole-file and exact runs
    assert out["fail_to_pass"] == [order[1], order[2]] and out["pass_to_pass"] == []                      # order-dependent tests leave the contract entirely
    assert out["probe"]["counts"]["order_dependent"] == 2 and out["probe"]["stages"] == ["base", "test", "test_exact", "gold", "gold_exact"]
    assert out["statuses"]["test_exact"] == exact_test and "test_exact" in out["probe"]["errors"]
    cfg = json.loads((tmp_path / "ws" / "probes" / "pydata--xarray-5362" / "tests" / "probe_config.json").read_text())
    assert cfg["prefix"] == gr.runner_prefix(gr.SWEBENCH_PYTHON) and cfg["max_command_chars"] == gr.MAX_COMMAND_CHARS and cfg["command"].startswith(cfg["prefix"])
    probe_py = (tmp_path / "ws" / "probes" / "pydata--xarray-5362" / "tests" / "probe.py").read_text()
    assert "test_exact" in probe_py and "gold_exact" in probe_py and "run(exact,contract)" in probe_py and "results['order']=contract" in probe_py
    assert "git_apply('/tests/oracle.diff','-R')" in probe_py and "before[t] in ('failed','error') and after.get(t)=='passed'" in probe_py   # contract ids, oracle reverted for test_exact
    assert "oracle revert failed" in probe_py and "results.pop('test_exact',None)" in probe_py   # a failed revert keeps the whole-file evidence and records the skip
    # the exact stages must cover exactly the contract ids: a non-contract id in them, or a missing one, is unreliable
    extra = {**data, "order": order, "test_exact": {**data["test_exact"], "statuses": {**exact_test, f + "test_not_in_contract": "passed"}}}
    _setup_probe(monkeypatch, tmp_path, extra)
    with pytest.raises(ValueError, match="unreliable exact-id probe"):
        gr.probe_tests(dict(row), Workspace(tmp_path / "ws_extra"))
    # whole-file stages decide the contract; `order` only orders it (an id absent from the contract is ignored)
    reorder = {**data, "order": [f + "test_n_max_elements", f + "test_basic", f + "test_deeply_nested", f + "test_changed_only", f + "not_collected"]}
    reorder["test_exact"] = {**data["test_exact"], "statuses": full_test}; reorder["gold_exact"] = {**data["gold_exact"], "statuses": full_gold}
    _setup_probe(monkeypatch, tmp_path, reorder)
    out2 = gr.probe_tests(dict(row), Workspace(tmp_path / "ws_reorder"))
    assert out2["probe"]["order"] == [f + "test_n_max_elements", f + "test_basic", f + "test_deeply_nested", f + "test_changed_only"] and out2["dropped"]["order_dependent"] == []
    # an exact stage that errors (e.g. collection differs from the manifest) makes the probe unreliable
    bad = {**data, "gold_exact": {**data["gold_exact"], "error": "collection differs from manifest"}}
    _setup_probe(monkeypatch, tmp_path, bad)
    with pytest.raises(ValueError, match="unreliable exact-id probe"):
        gr.probe_tests(dict(row), Workspace(tmp_path / "ws2"))
    # without exact stages (older job dirs, argv budget) the whole-file statuses are used and the reason is recorded
    skipped = {k: v for k, v in data.items() if k not in ("test_exact", "gold_exact", "exact_command")}
    skipped["exact_skipped"] = "command exceeds argv budget"
    _setup_probe(monkeypatch, tmp_path, skipped)
    out = gr.probe_tests(dict(row), Workspace(tmp_path / "ws3"))
    assert out["probe"]["runner"]["exact"] == "command exceeds argv budget" and out["dropped"]["order_dependent"] == [] and out["pass_to_pass"] == [order[0], order[3]]
    assert gr.PROBE_VERIFIER_TIMEOUT_SEC >= 5 * gr.PROBE_TIMEOUT_SEC + 300
    # a bound probe with a terminal skip reason is passed through by probe_candidates, never re-probed on later runs
    from swesim.stages.mine import probe_candidates
    over = {**row, "instance_id": "pydata__xarray-9", "judge": {"keep": True, "gaps": []}, "probe": dict(out["probe"])}
    over["probe"]["binding"] = gr.probe_binding(over)
    judged = tmp_path / "over.judged.jsonl"; judged.write_text(json.dumps(over) + "\n")
    monkeypatch.setattr(gr, "probe_tests", lambda r, ws: pytest.fail("a terminal skip reason must not be re-probed"))
    out_path = probe_candidates(Workspace(tmp_path / "ws_over"), judged, max_builds=3)
    assert json.loads(out_path.with_suffix(".summary.json").read_text()) == {"rows": 1, "probed": 0, "already-probed-whole-file-only": 1}
    assert json.loads(out_path.read_text().splitlines()[0])["probe"]["runner"]["exact"] == "command exceeds argv budget"


def test_classify_probe_buckets():
    before = {"a": "failed", "b": "passed", "c": "failed", "d": "passed", "e": "error"}
    after = {"a": "passed", "b": "passed", "c": "failed", "d": "error", "e": "passed"}
    assert gr.classify_probe(before, after) == {"fail_to_pass": ["a", "e"], "pass_to_pass": ["b"], "dropped": {"network_or_env": ["c"], "gold_regressions": ["d"], "other": []}}
    assert gr.probe_kind({"image": "x:latest"}) == "image" and gr.probe_kind({"install_command": "pip install -e ."}) == "dockerfile"


def _old_style_repo_row(tmp_path, binding: str | None = "row", test_user: str | None = "tester"):
    """A repo-URL row as mine_github_repo wrote it before `probe` existed: measured lists, probe_dir, test_python, dockerfile.
    `binding="row"` records the row's own binding in the job's probe.json (the shape PROBE_SCRIPT writes); None leaves the
    evidence unbound, as jobs from before B-P2-1 are."""
    job = tmp_path / "jobs" / "probe" / "py-pdf--pypdf-3826" / "trial-1"
    (job / "verifier").mkdir(parents=True, exist_ok=True)
    tp = "diff --git a/tests/test_writer.py b/tests/test_writer.py\n--- a/tests/test_writer.py\n+++ b/tests/test_writer.py\n@@ -1 +1,3 @@\n x\n+def test_new_a(): pass\n+def test_new_b(): pass\n"
    f = "tests/test_writer.py::"
    row = {"source": "github-repo", "instance_id": "py-pdf__pypdf-3826", "repo": "py-pdf/pypdf", "base_commit": "c" * 40, "repo_dir": "/app", "image": None,
           "test_patch": tp, "patch": "OP", "test_files": ["tests/test_writer.py"], "install_command": "pip install -e '.[full]'",
           "fail_to_pass": [f + "test_new_a", f + "test_new_b"], "pass_to_pass": [f + "test_old"], "probe_dir": str(job), "test_python": "/usr/local/bin/python",
           "dockerfile": "FROM python@sha256:abc\n", "judge": {"keep": True, "gaps": []}}
    data = {"base": {"statuses": {}, "exit_code": 4, "error": "no tests", "tail": ""},
            "test": {"statuses": {f + "test_new_a": "failed", f + "test_new_b": "error", f + "test_old": "passed", f + "test_socket": "failed"}, "exit_code": 1, "error": None, "tail": ""},
            "gold": {"statuses": {f + "test_new_a": "passed", f + "test_new_b": "passed", f + "test_old": "passed", f + "test_socket": "failed"}, "exit_code": 1, "error": None, "tail": ""}}
    if binding is not None:
        data["binding"] = gr.probe_binding(row) if binding == "row" else binding
    if test_user is not None:
        data["test_user"] = test_user
    (job / "verifier" / "probe.json").write_text(json.dumps(data))
    return row


def test_reconstruct_probe_from_job_dir_and_probe_candidates_reuses_measurements(monkeypatch, tmp_path):
    from swesim.paths import Workspace
    from swesim.stages.mine import probe_candidates
    row = _old_style_repo_row(tmp_path)
    f = "tests/test_writer.py::"
    # B-P2-1: evidence without a recorded binding, with a foreign one, or measured as root is unverifiable and never reused
    assert gr.reconstruct_probe(_old_style_repo_row(tmp_path / "unbound", binding=None)) is None
    assert gr.reconstruct_probe(_old_style_repo_row(tmp_path / "foreign", binding="0" * 64)) is None
    assert gr.reconstruct_probe(_old_style_repo_row(tmp_path / "root", test_user=None)) is None
    rec = gr.reconstruct_probe(row)
    assert rec["fail_to_pass"] == [f + "test_new_a", f + "test_new_b"] and rec["pass_to_pass"] == [f + "test_old"]
    assert rec["dropped"] == {"network_or_env": [f + "test_socket"], "gold_regressions": [], "other": [], "order_dependent": []} and rec["probe"]["reconstructed"] is True
    assert rec["probe"]["kind"] == "dockerfile" and rec["probe"]["repo_dir"] == "/app" and rec["probe"]["test_python"] == "/usr/local/bin/python"
    assert rec["probe"]["binding"] == gr.probe_binding(row) and rec["probe"]["job_dir"] == row["probe_dir"] and rec["probe"]["probed_at"]
    assert rec["probe"]["command"] == "/usr/local/bin/python -I /tests/run_pytest.py -rA -p no:cacheprovider --junitxml={junit_path} tests/test_writer.py"
    assert gr.reconstruct_probe({**row, "probe_dir": str(tmp_path / "nowhere")}) is None and gr.reconstruct_probe({**row, "probe_dir": None}) is None
    assert rec["probe"]["runner"]["exact"] == "missing"                                                            # an old job dir has no exact-id stages
    # probe_candidates: a bound probe WITH exact-stage evidence passes through; whole-file-only or stale probes are re-run while
    # budget allows; with no budget left an old-style row is reconstructed from disk and marked whole-file-only
    exact_ok = {**_old_style_repo_row(tmp_path), "instance_id": "py-pdf__pypdf-1", "probe_dir": None, "probe": {**rec["probe"], "runner": {**rec["probe"]["runner"], "exact": "exact"}}}
    exact_ok["probe"]["binding"] = gr.probe_binding(exact_ok)
    whole_only = {**_old_style_repo_row(tmp_path), "instance_id": "py-pdf__pypdf-2", "probe_dir": None, "probe": {**rec["probe"]}}
    whole_only["probe"]["binding"] = gr.probe_binding(whole_only)
    stale = {**_old_style_repo_row(tmp_path), "instance_id": "py-pdf__pypdf-3", "probe_dir": None, "probe": {**rec["probe"], "binding": "0" * 64}}
    judged = tmp_path / "github-repo--py-pdf__pypdf.judged.jsonl"
    judged.write_text("".join(json.dumps(r) + "\n" for r in (row, exact_ok, whole_only, stale)))
    probed = []

    def fake_probe(r, ws):
        probed.append(r["instance_id"])
        return {**rec, "probe": {**rec["probe"], "reconstructed": False, "binding": gr.probe_binding(r), "runner": {**rec["probe"]["runner"], "exact": "exact"}}}
    monkeypatch.setattr(gr, "probe_tests", fake_probe)
    out = probe_candidates(Workspace(tmp_path / "ws"), judged, max_builds=2)
    got = {r["instance_id"]: r for r in (json.loads(l) for l in out.read_text().splitlines())}
    assert probed == ["py-pdf__pypdf-3826", "py-pdf__pypdf-2"]                                                  # old-style and whole-file-only rows cost the two builds
    assert got["py-pdf__pypdf-1"]["probe"]["runner"]["exact"] == "exact" and got["py-pdf__pypdf-2"]["probe"]["runner"]["exact"] == "exact"
    assert got["py-pdf__pypdf-3826"]["probe"]["reconstructed"] is False and got["py-pdf__pypdf-3826"]["swebench_fail_to_pass"] == [f + "test_new_a", f + "test_new_b"]
    assert "py-pdf__pypdf-3" not in got                                                                             # stale, no budget left, nothing on disk
    assert json.loads(out.with_suffix(".summary.json").read_text()) == {"rows": 4, "probed": 2, "already-probed": 1, "reproducible": 2, "build-budget-exhausted": 1}
    probed.clear()
    out = probe_candidates(Workspace(tmp_path / "ws"), judged, max_builds=0)
    got = {r["instance_id"]: r for r in (json.loads(l) for l in out.read_text().splitlines())}
    assert probed == [] and got["py-pdf__pypdf-3826"]["probe"]["reconstructed"] is True and got["py-pdf__pypdf-3826"]["probe"]["runner"]["exact"] == "missing"
    assert json.loads(out.with_suffix(".summary.json").read_text()) == {"rows": 4, "probed": 0, "already-probed": 1, "reconstructed-whole-file-only": 1, "reproducible": 1, "build-budget-exhausted": 2}


@pytest.mark.parametrize("test_user", [None, "root"])
def test_probe_candidates_never_reuses_matching_bindings_measured_as_root(monkeypatch, tmp_path, test_user):
    from swesim.paths import Workspace
    from swesim.stages.mine import probe_candidates
    row = {**_swebench_probe_row(), "judge": {"keep": True}}
    row["probe"] = {"binding": gr.probe_binding(row), "test_user": test_user, "statuses": {"test": {"t": "failed"}}, "runner": {"exact": "exact"}}
    judged = tmp_path / "judged.jsonl"; judged.write_text(json.dumps(row) + "\n")
    calls = []

    def fake_probe(candidate, ws):
        calls.append(candidate["instance_id"])
        return {"fail_to_pass": ["t"], "pass_to_pass": [], "test_python": gr.SWEBENCH_PYTHON, "probe_dir": "/job/trial",
                "dockerfile": None, "probe": {**candidate["probe"], "test_user": "tester"}}

    monkeypatch.setattr(gr, "probe_tests", fake_probe)
    result = probe_candidates(Workspace(tmp_path / "ws"), judged, max_builds=1)
    assert calls == [row["instance_id"]]
    assert json.loads(result.read_text())["probe"]["test_user"] == "tester"


def test_probe_candidates_replaces_swebench_lists_and_rejects_unreproducible_rows(monkeypatch, tmp_path):
    from swesim.paths import Workspace
    from swesim.stages.mine import probe_candidates
    f = "xarray/tests/test_weighted.py::"
    rows = [{**_swebench_probe_row(), "instance_id": "pydata__xarray-1", "judge": {"keep": True, "gaps": []}},
            {**_swebench_probe_row(), "instance_id": "pydata__xarray-2", "judge": {"keep": True, "gaps": []}},
            {**_swebench_probe_row(), "instance_id": "pydata__xarray-3", "judge": {"keep": False, "gaps": []}},
            {**_swebench_probe_row(), "instance_id": "pydata__xarray-4", "judge": {"keep": True, "gaps": []}}]
    judged = tmp_path / "swebench-annotated.judged.jsonl"
    judged.write_text("".join(json.dumps(r) + "\n" for r in rows))
    probed = []

    def fake_probe(row, ws):
        probed.append(row["instance_id"])
        bound = {"kind": "image", "test_user": "tester", "statuses": {"test": {"x": "failed"}, "gold": {"x": "passed"}}, "binding": gr.probe_binding(row), "runner": {"exact": "exact"}}
        if row["instance_id"] == "pydata__xarray-2":
            return {"fail_to_pass": [], "pass_to_pass": [f + "test_old"], "dropped": {"network_or_env": [f + "test_new"], "gold_regressions": [], "other": [], "order_dependent": []}, "statuses": {},
                    "dockerfile": "FROM x", "probe_dir": "/j/2", "test_python": gr.SWEBENCH_PYTHON, "probe": {**bound, "probe_dir": "/j/2"}}
        if row["instance_id"] == "pydata__xarray-4":
            raise ValueError("unreliable base/gold probe; keep evidence and reject")
        return {"fail_to_pass": [f + "test_new"], "pass_to_pass": [f + "test_old", f + "test_extra"], "dropped": {"network_or_env": [f + "test_needs_network"], "gold_regressions": [], "other": [], "order_dependent": []},
                "statuses": {}, "dockerfile": "FROM x", "probe_dir": "/j/1", "test_python": gr.SWEBENCH_PYTHON, "probe": {**bound, "probe_dir": "/j/1"}}
    monkeypatch.setattr(gr, "probe_tests", fake_probe)
    out = probe_candidates(Workspace(tmp_path / "ws"), judged, max_builds=5)
    assert out == tmp_path / "swebench-annotated.judged.probed.jsonl" and probed == ["pydata__xarray-1", "pydata__xarray-2", "pydata__xarray-4"]   # keep=False row skipped
    got = {r["instance_id"]: r for r in (json.loads(l) for l in out.read_text().splitlines())}
    assert set(got) == {"pydata__xarray-1", "pydata__xarray-2", "pydata__xarray-4"}
    r1 = got["pydata__xarray-1"]
    assert r1["fail_to_pass"] == [f + "test_new"] and r1["pass_to_pass"] == [f + "test_old", f + "test_extra"] and r1["judge"]["keep"] is True and "reject_reason" not in r1
    assert r1["swebench_fail_to_pass"] == [f + "test_new", f + "test_needs_network"] and r1["swebench_pass_to_pass"] == [f + "test_old", f + "test_flaky"]   # originals kept
    assert r1["probe"]["probe_dir"] == "/j/1" and r1["test_python"] == gr.SWEBENCH_PYTHON
    r2 = got["pydata__xarray-2"]
    assert r2["fail_to_pass"] == [] and r2["judge"]["keep"] is False and r2["reject_reason"] == "not-reproducible-offline"
    r4 = got["pydata__xarray-4"]
    assert r4["judge"]["keep"] is False and r4["reject_reason"] == "probe-failed" and "unreliable" in r4["probe"]["error"] and r4["fail_to_pass"] == r4["swebench_fail_to_pass"]
    summary = json.loads(out.with_suffix(".summary.json").read_text())
    assert summary == {"rows": 4, "probed": 3, "reproducible": 1, "not-reproducible-offline": 1, "probe-failed": 1, "skipped-not-accepted": 1}
    # build budget and explicit instance selection (which ignores keep)
    # re-running on the probed file writes it back in place and never overwrites the SWE-bench originals with a measurement
    probed.clear()
    again = probe_candidates(Workspace(tmp_path / "ws"), out, max_builds=5)
    assert again == out and probed == []                                                              # the bound probe passes through; rejected rows are not accepted any more
    got2 = {r["instance_id"]: r for r in (json.loads(l) for l in out.read_text().splitlines())}
    assert got2["pydata__xarray-1"]["swebench_fail_to_pass"] == [f + "test_new", f + "test_needs_network"] and got2["pydata__xarray-1"]["fail_to_pass"] == [f + "test_new"]
    assert json.loads(out.with_suffix(".summary.json").read_text()) == {"rows": 3, "probed": 0, "already-probed": 1, "skipped-not-accepted": 2}
    assert not (tmp_path / "swebench-annotated.judged.probed.probed.jsonl").exists()
    # explicit ids re-probe even rejected rows, and the originals survive a second measurement
    out2 = probe_candidates(Workspace(tmp_path / "ws"), judged, max_builds=5)
    probed.clear()
    again = probe_candidates(Workspace(tmp_path / "ws"), out2, instance_ids=["pydata__xarray-1", "pydata__xarray-2"], max_builds=5)
    got3 = {r["instance_id"]: r for r in (json.loads(l) for l in again.read_text().splitlines())}
    assert probed == [] and got3["pydata__xarray-2"]["swebench_fail_to_pass"] == [f + "test_new", f + "test_needs_network"] and got3["pydata__xarray-2"]["fail_to_pass"] == []
    probed.clear()
    probe_candidates(Workspace(tmp_path / "ws"), judged, max_builds=1)
    assert probed == ["pydata__xarray-1"] and json.loads(out.with_suffix(".summary.json").read_text())["build-budget-exhausted"] == 2
    probed.clear()
    out = probe_candidates(Workspace(tmp_path / "ws"), judged, instance_ids=["pydata__xarray-3"], max_builds=1)
    assert probed == ["pydata__xarray-3"] and [json.loads(l)["instance_id"] for l in out.read_text().splitlines()] == ["pydata__xarray-3"]


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
