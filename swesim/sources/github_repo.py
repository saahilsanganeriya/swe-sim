"""github-repo source: discover merged PRs with linked issues from a repository URL, filter by band, build a
Dockerfile recipe, and probe FAIL_TO_PASS / PASS_TO_PASS on Modal.

Diff size is a filter, never measured difficulty. Discovery never accepts a hand-supplied PR as evidence of
repo-only acceptance: candidates record `discovery.repo_only`.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from collections import Counter

from swesim.sources import threads

log = logging.getLogger(__name__)
LINK = re.compile(r"(?:[Ff]ix(?:es|ed)?|[Cc]los(?:es|ed)?|[Rr]esolves?)\s*:?\s*#(\d+)")
_TEST_PATH = re.compile(r"(^|/)tests?/|test_.*\.py$|_test\.py$|conftest\.py$")
_DIFF_HEADER = re.compile(r"^diff --git a/(\S+)", re.M)
# "Source" for band filtering means code: docs, changelogs, markup, config and lockfiles never count toward
# additions/deletions or the source-file band (they still travel in the oracle patch).
CODE_EXTENSIONS = {".py", ".pyx", ".pxd", ".pyi", ".c", ".h", ".cc", ".cpp", ".hpp", ".rs", ".go", ".js", ".mjs", ".ts", ".tsx", ".jsx",
                   ".java", ".kt", ".rb", ".sh"}
_NON_CODE_DIRS = re.compile(r"(^|/)(docs?|doc|documentation|examples?|resources|fixtures|benchmarks?)/")
_NON_CODE_NAMES = re.compile(r"(^|/)(CHANGELOG|CHANGES|HISTORY|NEWS|README|AUTHORS|CONTRIBUTORS|LICENSE)[^/]*$", re.I)
FEATURE_BAND = {"min_comments": 8, "diff_min": 200, "diff_max": 1500, "source_files": (3, 30)}
FEATURE_LABEL_WORDS = ("enhancement", "feature", "rfc", "proposal")
KNOWN_RECIPES = {"py-pdf/pypdf": {"install_command": "pip install -e '.[full]' -r requirements/ci.txt", "test_marker": '-m "not samples and not enable_socket"'}}
TEST_PYTHON = "/usr/local/bin/python"
BASE_IMAGE = "python:3.12-slim"


def normalize_repo(value: str) -> str:
    value = value.strip()
    for prefix in ("https://github.com/", "http://github.com/", "https://www.github.com/", "github.com/"):
        value = value.removeprefix(prefix)
    value = value.removesuffix(".git").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("GitHub repository URL or owner/repo required")
    return value


def linked_issues(body: str) -> list[int]:
    return sorted({int(n) for n in LINK.findall(body or "")})


def _chunks(patch: str) -> list[tuple[str, str]]:
    out = []
    for chunk in re.split(r"(?=^diff --git )", patch, flags=re.M):
        m = re.match(r"diff --git a/(\S+)", chunk)
        if chunk.strip() and m:
            out.append((m.group(1), chunk))
    return out


def is_code_path(path: str) -> bool:
    """Code file by extension, outside docs/examples/fixtures directories and not a changelog/readme-style file."""
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return ext in CODE_EXTENSIONS and not _NON_CODE_DIRS.search(path) and not _NON_CODE_NAMES.search(path)


def split_patch(patch: str) -> tuple[str, str]:
    """(non-test diff, test diff). The non-test part is the whole oracle patch (docs, config and all); use
    `code_stats` for the band filters."""
    src, tst = [], []
    for path, chunk in _chunks(patch):
        (tst if _TEST_PATH.search(path) else src).append(chunk)
    return "".join(src), "".join(tst)


def code_stats(src: str) -> dict:
    """Code-only view of a non-test diff: {"files": [paths], "additions": n, "deletions": n, "non_code_files": [paths]}."""
    files, non_code, add, dele = [], [], 0, 0
    for path, chunk in _chunks(src):
        if not is_code_path(path):
            non_code.append(path)
            continue
        files.append(path)
        lines = chunk.splitlines()
        add += sum(l.startswith("+") and not l.startswith("+++") for l in lines)
        dele += sum(l.startswith("-") and not l.startswith("---") for l in lines)
    return {"files": files, "additions": add, "deletions": dele, "non_code_files": non_code}


def reject_reason(c: dict, min_comments: int, diff_min: int, diff_max: int) -> str | None:
    if not c.get("issue"):
        if c.get("linked_prs"):
            return "linked-target-is-pr"
        if c.get("linked_missing"):
            return "linked-target-missing"
        return "no-linked-issue"
    if c.get("thread") and not c["thread"].get("linked_issues"):
        return "missing-linked-issue"
    if c.get("thread") and any(i["created_at"] >= c["thread"]["pr_created_at"] for i in c["thread"]["linked_issues"]):
        return "issue-not-before-pr"
    if not c.get("test_files"):
        return "no-test-changes"
    if not c.get("source_files"):
        return "no-source-changes"
    n = c["additions"] + c["deletions"]
    if n < diff_min:
        return "diff-too-small"
    if n > diff_max:
        return "diff-too-large"
    if c.get("comments_total", 0) < min_comments:
        return "thin-thread"
    return None


def _gh(path: str, paginate: bool = False):
    """JSON for one `gh api` path; `paginate=True` walks explicit pages (never `--paginate`, which concatenates documents)."""
    return threads.pages(path) if paginate else threads._gh(path)


def _pr_diff(repo: str, number: int) -> str:
    return threads._gh_text(f"repos/{repo}/pulls/{number}", accept="application/vnd.github.v3.diff")


def _candidate(repo: str, p: dict, patch: str) -> dict:
    src, tst = split_patch(patch)
    code = code_stats(src)
    return {"repo": repo, "pr": p["number"], "title": p["title"], "base_commit": p["base"]["sha"], "merge_commit": p.get("merge_commit_sha"),
            "additions": code["additions"], "deletions": code["deletions"],
            "patch": src, "test_patch": tst, "source_files": code["files"], "non_code_files": code["non_code_files"],
            "test_files": _DIFF_HEADER.findall(tst), "issue": None, "issue_body": None, "thread": None, "linked_prs": [], "linked_missing": []}


def _merged_details(repo: str, since: str, max_prs: int, pr: int | None) -> list[dict]:
    if pr:
        return [threads._gh(f"repos/{repo}/pulls/{pr}")]
    summaries = []
    for page in range(1, (max_prs + 99) // 100 + 1):
        rows = threads._gh(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}")
        if not isinstance(rows, list):
            raise ValueError("expected page array")
        summaries.extend(rows)
        if len(rows) < 100:
            break
    details = []
    for p in summaries[:max_prs]:
        # List rows already tell us closed-unmerged and pre-`since` merges; everything else needs the detail (labels, base sha, stats).
        if "merged_at" in p and (p["merged_at"] is None or p["merged_at"] < since):
            continue
        details.append(threads._gh(f"repos/{repo}/pulls/{p['number']}"))
    return details


def discover(repo: str, since: str, max_prs: int = 400, min_comments: int = 4, diff_min: int = 20, diff_max: int = 600, pr: int | None = None,
             kind: str = "feature") -> tuple[list[dict], dict]:
    """Candidates and a reject histogram for merged PRs of `repo` since `since` (ISO date), bounded by `max_prs` list rows."""
    repo = normalize_repo(repo)
    if kind == "feature":
        min_comments, diff_min, diff_max = FEATURE_BAND["min_comments"], FEATURE_BAND["diff_min"], FEATURE_BAND["diff_max"]
    elif kind != "bugfix":
        raise ValueError("kind must be feature or bugfix")
    prs = [p for p in _merged_details(repo, since, max_prs, pr) if p.get("merged_at") and p["merged_at"] >= since]
    cands, rejects = [], Counter()
    for p in prs:
        try:
            targets = threads.linked_targets(repo, p["number"], threads.closing_text(p))
            issues = targets["issues"]
            c = _candidate(repo, p, _pr_diff(repo, p["number"]))
            c["linked_prs"], c["linked_missing"] = targets["prs"], targets["missing"]
            if issues:
                c["issue"] = issues[0]
                c["thread"] = threads.fetch_pr_thread(repo, p["number"])
                c["issue_body"] = c["thread"]["linked_issues"][0]["body"] if c["thread"]["linked_issues"] else None
                c["comments_total"] = (len(c["thread"]["pr_comments"]) + len(c["thread"]["review_comments"])
                                       + sum(len(i["comments"]) for i in c["thread"]["linked_issues"]))
        except (subprocess.CalledProcessError, ValueError) as e:
            log.warning("github-repo: PR %s/%s fetch failed: %s", repo, p.get("number"), e)
            rejects["fetch-error"] += 1
            continue
        c["labels"] = [x["name"].lower() for x in p.get("labels") or []]
        r = reject_reason(c, min_comments, diff_min, diff_max)
        if not r and kind == "feature":
            lo, hi = FEATURE_BAND["source_files"]
            if not lo <= len(c["source_files"]) <= hi:
                r = "feature-source-file-band"
            elif not any(any(word in label for word in FEATURE_LABEL_WORDS) for label in c["labels"]):
                r = "feature-label-missing"
        c["discovery"] = {"kind": kind, "repo_only": pr is None, "since": since, "max_prs": max_prs}
        if r:
            rejects[r] += 1
        else:
            cands.append(c)
    return cands, dict(rejects)


def build_recipe(repo: str, base_commit: str, profile: str = "python-pytest") -> dict:
    k = KNOWN_RECIPES.get(repo, {})
    install = k.get("install_command", "pip install -e . && pip install pytest")
    marker = k.get("test_marker", "")
    return {"repo_dir": "/app", "install_command": install, "test_python": TEST_PYTHON,
            "test_command": f"python -I /tests/run_pytest.py -rA -p no:cacheprovider {marker} --junitxml={{junit_path}} {{test_files}}".replace("  ", " ")}


def candidate_to_row(c: dict) -> dict:
    iss = ((c.get("thread") or {}).get("linked_issues") or [{}])[0]
    before_pr = [x for x in iss.get("comments", []) if x["created_at"] < c["thread"]["pr_created_at"]]
    return {"source": "github-repo", "instance_id": f"{c['repo'].replace('/', '__')}-{c['pr']}", "repo": c["repo"], "base_commit": c["base_commit"], "version": "",
            "problem_statement": (iss.get("title", c["title"]) + "\n\n" + (iss.get("body") or "")).strip(), "hints_text": "\n\n".join(x["body"] for x in before_pr),
            "test_files": c["test_files"], "fail_to_pass": [], "pass_to_pass": [], "patch": c["patch"], "test_patch": c["test_patch"], "image": None, "repo_dir": "/app",
            "discovery": c.get("discovery", {}),
            "annotation": {"underspecified": "", "underspecified_notes": "", "false_negative": "", "false_negative_notes": "", "difficulty": ""},
            "thread": c["thread"] or {}}


def _compile_imports():
    """Compile-stream helpers, imported lazily (the module belongs to another stream); tests monkeypatch this."""
    from swesim.stages import compile as _compile
    return _compile.resolve_docker_hub_digest, _compile.VERIFIER_SRC


PROBE_SCRIPT = '''import sys,json,subprocess
from pathlib import Path
sys.path.insert(0,'/tests')
from grader import GradeConfig,_run_tests
cfg=GradeConfig(Path('/tmp/a'),Path('/app'),Path('/tmp/w'),{},[],[],json.loads(Path('/tests/probe_config.json').read_text()),None,Path('/logs/verifier'),test_timeout_sec=300)
results={}
for stage,patch in [('base',None),('test','/tests/test.patch'),('gold','/tests/oracle.diff')]:
    if patch: subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null','apply',patch],cwd='/app',check=True)
    st,rc,out,error=_run_tests(cfg,Path('/app'),None)
    results[stage]={'statuses':st,'exit_code':rc,'error':error,'tail':out[-2000:]}
Path('/logs/verifier').mkdir(parents=True,exist_ok=True)
Path('/logs/verifier/probe.json').write_text(json.dumps(results))
Path('/logs/verifier/reward.json').write_text(json.dumps({'reward':int(not results['gold']['error'])}))
'''
PROBE_TASK_TOML = '''schema_version="1.4"
[environment]
network_mode="no-network"
build_timeout_sec=3600
[agent]
timeout_sec=60
[verifier]
environment_mode="separate"
network_mode="no-network"
timeout_sec=1200
[verifier.environment]
build_timeout_sec=3600
'''


def probe_tests(row: dict, ws) -> dict:
    """Separate authoring probe on Modal (nop agent): run the PR's test files with the test patch applied, then with the
    gold patch, and derive FAIL_TO_PASS / PASS_TO_PASS. No ScenarioSpec is constructed until test ids exist."""
    import shlex
    import shutil
    from swesim import harbor_cli
    from swesim.paths import atomic_json

    resolve_docker_hub_digest, verifier_src = _compile_imports()
    name = row["instance_id"].lower().replace("__", "--")
    root = ws.root / "probes" / name
    env = root / "environment"
    tests = root / "tests"
    env.mkdir(parents=True, exist_ok=True)
    tests.mkdir(exist_ok=True)
    digest = resolve_docker_hub_digest(BASE_IMAGE)
    if not digest:
        raise RuntimeError("base image digest unavailable")
    dockerfile = (f"FROM python@{digest}\n"
                  "RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*\n"
                  f"RUN git clone {shlex.quote('https://github.com/' + row['repo'])} /app && cd /app && git checkout {row['base_commit']} && {row['install_command']}\n"
                  "WORKDIR /app\n")
    row["dockerfile"] = dockerfile
    (env / "Dockerfile").write_text(dockerfile)
    (tests / "Dockerfile").write_text(dockerfile + "\nCOPY . /tests/\n")
    for p in verifier_src.glob("*.py"):
        shutil.copyfile(p, tests / p.name)
    (tests / "test.patch").write_text(row["test_patch"])
    (tests / "oracle.diff").write_text(row["patch"])
    files = [p for p in row["test_files"] if p.endswith(".py")]
    command = TEST_PYTHON + " -I /tests/run_pytest.py -rA -p no:cacheprovider --junitxml={junit_path} " + " ".join(shlex.quote(p) for p in files)
    atomic_json(tests / "probe_config.json", {"command": command})
    (tests / "test.sh").write_text("#!/bin/bash\nset -euo pipefail\n" + TEST_PYTHON + " -I /tests/probe.py\n")
    (tests / "probe.py").write_text(PROBE_SCRIPT)
    (root / "instruction.md").write_text("Environment authoring probe; no solver work.")
    (root / "task.toml").write_text(PROBE_TASK_TOML)
    td = harbor_cli.run_task(root, "nop", ws.jobs_dir / "probe", name, timeout_sec=9000)
    data = json.loads((td / "verifier" / "probe.json").read_text())
    before = data["test"]["statuses"]
    after = data["gold"]["statuses"]
    if data["test"]["error"] or data["gold"]["error"] or not after or set(before) != set(after):
        raise ValueError("unreliable base/gold probe; keep evidence and reject")
    f = [t for t in before if before[t] in {"failed", "error"} and after[t] == "passed"]
    p = [t for t in before if before[t] == after[t] == "passed"]
    if not f or not p:
        raise ValueError("probe needs target failure and regression coverage")
    return {"fail_to_pass": f, "pass_to_pass": p, "dockerfile": dockerfile, "probe_dir": str(td), "test_python": TEST_PYTHON}
