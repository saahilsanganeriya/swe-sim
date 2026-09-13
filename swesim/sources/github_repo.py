"""github-repo source: discover merged PRs with linked issues from a repository URL, filter by band, build a
Dockerfile recipe, and probe FAIL_TO_PASS / PASS_TO_PASS on Modal.

Diff size is a filter, never measured difficulty. Discovery never accepts a hand-supplied PR as evidence of
repo-only acceptance: candidates record `discovery.repo_only`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

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
SWEBENCH_PYTHON = "/opt/miniconda3/envs/testbed/bin/python"
PROBE_TIMEOUT_SEC = 900  # per stage (base, tests applied, gold); SWE-bench test files can run for minutes


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
            c["linked_prs"], c["linked_missing"], c["link_kind"] = targets["prs"], targets["missing"], targets.get("link_kind")
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


def _verifier_python() -> str:
    """The pinned interpreter version the compile stream installs as /usr/local/bin/swesim-python in every verifier image."""
    try:
        from swesim.stages import compile as _compile
        return str(_compile.AGENT_PYTHON)
    except (ImportError, AttributeError):
        return "3.12"


SWESIM_PYTHON = "/usr/local/bin/swesim-python"
GRADING_USER = "tester"  # the identity compiled tasks grade under (compile._grader_config test_user); the probe measures as the same user


def verifier_setup(python_version: str) -> str:
    """Dockerfile lines that give the probe's verifier image the same pinned controller interpreter as compiled tasks: the
    grader/probe controller runs under it (SWE-bench testbed interpreters can predate `from __future__ import annotations`);
    only the test subprocess uses the task's own interpreter from the runner command, and it runs as the grading user
    (`GRADING_USER`, created here exactly as the compiled verifier image creates it)."""
    return ("USER root\n"
            "ENV DEBIAN_FRONTEND=noninteractive\n"
            "RUN apt-get update && apt-get install -y --no-install-recommends curl git ca-certificates && rm -rf /var/lib/apt/lists/*\n"
            f"RUN if id {GRADING_USER} >/dev/null 2>&1; then echo '{GRADING_USER} user pre-exists in base image; rejected for this pilot' >&2; exit 1; fi && useradd -m {GRADING_USER}\n"
            "RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh && uv --version\n"
            "ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python\n"
            f"RUN uv python install {python_version} && ln -s \"$(uv python find {python_version})\" {SWESIM_PYTHON} && {SWESIM_PYTHON} -I -c 'import sys; print(sys.version)'\n")


def runner_prefix(interpreter: str) -> str:
    """The trusted launcher invocation shared by the probe's exact-id stages and the scenario's `runner.command`: same
    interpreter, same launcher, same flags, ids appended in definition order."""
    import shlex
    return shlex.quote(interpreter) + " -I /tests/run_pytest.py -rA -p no:cacheprovider --junitxml={junit_path} "


# Stages: base (pristine), test (test_patch applied, whole test files), gold (oracle applied, whole files); then the CONTRACT
# ids (fail/error->passed and passed->passed, in collected definition order) are re-run exactly as the grader will run them:
# oracle reverted -> test_exact, oracle re-applied -> gold_exact. A contract test whose status differs between the whole-file
# run and the exact-id run is order-dependent and is dropped from the contract. The tests run as `test_user` (the grading
# identity; the tree is chowned to it after every patch application, as grade() does) and the output records the `binding`
# of the inputs it measured, so a job dir can only be reused for the row it was built for (B-P2-1).
PROBE_SCRIPT = '''import sys,json,subprocess,shlex
from pathlib import Path
sys.path.insert(0,'/tests')
from grader import GradeConfig,_run_tests,_chown_tree
probe=json.loads(Path('/tests/probe_config.json').read_text())
repo=probe.get('repo_dir','/app'); prefix=probe.get('prefix'); max_chars=int(probe.get('max_command_chars',120000)); user=probe.get('test_user')
def cfg_for(command): return GradeConfig(Path('/tmp/a'),Path(repo),Path('/tmp/w'),{},[],[],{'kind':'pytest-ra','command':command},user,Path('/logs/verifier'),test_timeout_sec=int(probe.get('test_timeout_sec',300)))
def run(command,expected=None):
    st,rc,out,error=_run_tests(cfg_for(command),Path(repo),expected)
    return {'statuses':st,'exit_code':rc,'error':error,'tail':out[-2000:]}
def own():
    if user: _chown_tree(repo,user)
def git_apply(patch,*flags):
    subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','safe.directory=*','apply',*flags,patch],cwd=repo,check=True); own()
own()
results={'binding':probe.get('binding'),'test_user':user,'base':run(probe['command'])}
git_apply('/tests/test.patch')
results['test']=run(probe['command'])
git_apply('/tests/oracle.diff')
results['gold']=run(probe['command'])
before,after=results['test']['statuses'],results['gold']['statuses']
contract=[t for t in before if (before[t] in ('failed','error') and after.get(t)=='passed') or (before[t]=='passed' and after.get(t)=='passed')]
results['order']=contract
exact=(prefix or '')+' '.join(shlex.quote(t) for t in contract)
if prefix and contract and len(exact)<=max_chars and not results['test']['error'] and not results['gold']['error']:
    try:
        git_apply('/tests/oracle.diff','-R')
        results['exact_command']=exact
        results['test_exact']=run(exact,contract)
        git_apply('/tests/oracle.diff')
        results['gold_exact']=run(exact,contract)
    except Exception as e:  # keep the whole-file evidence; the exact stages are recorded as skipped
        results.pop('test_exact',None); results.pop('gold_exact',None); results.pop('exact_command',None)
        results['exact_skipped']='oracle revert failed: '+str(e)[:300]
else:
    results['exact_skipped']='no contract tests' if not contract else ('no prefix' if not prefix else ('whole-file stage errored' if (results['test']['error'] or results['gold']['error']) else 'command exceeds argv budget'))
Path('/logs/verifier').mkdir(parents=True,exist_ok=True)
Path('/logs/verifier/probe.json').write_text(json.dumps(results))
Path('/logs/verifier/reward.json').write_text(json.dumps({'reward':int(not results['gold']['error'])}))
'''
PROBE_VERIFIER_TIMEOUT_SEC = 5 * PROBE_TIMEOUT_SEC + 600  # five stages (base, test, test_exact, gold, gold_exact) may each use the budget, plus slack
MAX_COMMAND_CHARS = 120000  # Linux MAX_ARG_STRLEN is 131072 bytes per argv element (bash -c "<command>")
PROBE_TASK_TOML = f'''schema_version="1.4"
[environment]
network_mode="no-network"
build_timeout_sec=3600
[agent]
timeout_sec=60
[verifier]
environment_mode="separate"
network_mode="no-network"
timeout_sec={PROBE_VERIFIER_TIMEOUT_SEC}
[verifier.environment]
build_timeout_sec=3600
'''


def probe_kind(row: dict) -> str:
    """'image' for prebuilt-image rows (SWE-bench: `image`, `/testbed`, the conda interpreter); 'dockerfile' for repo-URL rows
    built from a recipe."""
    return "image" if row.get("image") else "dockerfile"


def classify_probe(before: dict, after: dict) -> dict:
    """Measured contract from per-test statuses with the hidden tests applied (`before`) and after the gold patch (`after`).
    Every test lands in exactly one bucket: fail_to_pass = failed/error before and passed after; pass_to_pass = passed on
    both; dropped.network_or_env = failed/error on both sides (network-dependent or environment-broken tests SWE-bench's
    lists still name); dropped.gold_regressions = passed before, anything else after (a passed->xpass flip lands here too);
    dropped.other = every remaining transition involving skipped/xfail/xpass (skipped->passed, xfail->passed, xpass->xpass,
    failed->skipped, ...). Statuses follow parsers.reconcile: passed|failed|error|skipped|xfail|xpass."""
    failing = {"failed", "error"}
    f2p = [t for t in before if before[t] in failing and after.get(t) == "passed"]
    p2p = [t for t in before if before[t] == "passed" and after.get(t) == "passed"]
    dropped = {"network_or_env": [t for t in before if before[t] in failing and after.get(t) in failing],
               "gold_regressions": [t for t in before if before[t] == "passed" and after.get(t) != "passed"]}
    placed = set(f2p) | set(p2p) | set(dropped["network_or_env"]) | set(dropped["gold_regressions"])
    dropped["other"] = [t for t in before if t not in placed]
    return {"fail_to_pass": f2p, "pass_to_pass": p2p, "dropped": dropped}


def probe_binding(row: dict) -> str:
    """Hash binding a probe to the exact base_commit and hidden test patch it measured. `draft(require_probe=True)` accepts a
    probe only when this matches the row, so an edited row can never reuse a stale measurement."""
    return hashlib.sha256(f"{row.get('base_commit') or ''}\n{row.get('test_patch') or ''}".encode()).hexdigest()


def _probe_defaults(row: dict) -> tuple[str, str, str, list[str]]:
    from swesim.sources.swebench_annotated import test_files_from_patch

    kind = probe_kind(row)
    repo_dir = row.get("repo_dir") or ("/testbed" if kind == "image" else "/app")
    interpreter = row.get("test_python") or (SWEBENCH_PYTHON if kind == "image" else TEST_PYTHON)
    files = [p for p in (row.get("test_files") or test_files_from_patch(row.get("test_patch") or "")) if p.endswith(".py")]
    return kind, repo_dir, interpreter, files


def _probe_record(row: dict, data: dict, *, kind: str, digest: str | None, repo_dir: str, interpreter: str, files: list[str], command: str,
                  job_dir: str, probed_at: str) -> dict:
    """Measured lists plus the `probe` metadata block from a probe.json payload; raises on an unreliable probe.

    Grading-faithful: the contract (fail_to_pass + pass_to_pass) comes from the whole-file stages; when the exact-id stages
    (`test_exact`, `gold_exact`: exactly the contract ids in collected definition order, the grader's own command shape) are
    present, every contract test whose status differs between the whole-file run and the exact-id run in either stage is
    dropped as `order_dependent` (test pollution the grader would see and the whole-file run hides). Without exact stages
    (older job dirs, argv budget) the whole-file contract stands and `probe.runner.exact` says why — approval can see it.

    Bound evidence (B-P2-1): the payload must record the `binding` of the base_commit/test_patch it measured and it must
    equal the row's, and the tests must have run as `GRADING_USER`; otherwise the evidence is stale, foreign or measured
    under a different identity and is rejected rather than relabelled with the row's hash."""
    binding = probe_binding(row)
    if data.get("binding") != binding:
        raise ValueError("probe evidence is not bound to this row's base_commit/test_patch (recorded binding %s); keep evidence and re-probe"
                         % ("missing" if not data.get("binding") else str(data["binding"])[:16]))
    if data.get("test_user") != GRADING_USER:
        raise ValueError(f"probe tests ran as {data.get('test_user') or 'root'!r}, not the grading identity {GRADING_USER!r}; keep evidence and re-probe")
    before, after = data["test"]["statuses"], data["gold"]["statuses"]
    if data["test"].get("error") or data["gold"].get("error") or not after or set(before) != set(after):
        raise ValueError("unreliable base/gold probe; keep evidence and reject")
    measured = classify_probe(before, after)
    contract_set = set(measured["fail_to_pass"]) | set(measured["pass_to_pass"])
    order = [t for t in (data.get("order") or before) if t in contract_set]  # contract ids in collected definition order
    exact_note = "exact"
    order_dependent: list[str] = []
    if "test_exact" in data and "gold_exact" in data:
        eb, ea = data["test_exact"]["statuses"], data["gold_exact"]["statuses"]
        if data["test_exact"].get("error") or data["gold_exact"].get("error") or set(eb) != contract_set or set(ea) != contract_set:
            raise ValueError("unreliable exact-id probe (the grader's own command shape on the contract ids); keep evidence and reject")
        order_dependent = [t for t in order if before.get(t) != eb.get(t) or after.get(t) != ea.get(t)]
    else:
        exact_note = data.get("exact_skipped") or "missing"
    dropped = {k: [t for t in v if t not in order_dependent] for k, v in measured["dropped"].items()}
    dropped["order_dependent"] = order_dependent
    measured = {"fail_to_pass": [t for t in measured["fail_to_pass"] if t not in order_dependent],
                "pass_to_pass": [t for t in measured["pass_to_pass"] if t not in order_dependent], "dropped": dropped}
    statuses = {"base": data["base"]["statuses"], "test": before, "gold": after}
    if "test_exact" in data:
        statuses["test_exact"], statuses["gold_exact"] = data["test_exact"]["statuses"], data["gold_exact"]["statuses"]
    stages = [s for s in ("base", "test", "test_exact", "gold", "gold_exact") if s in data]
    probe = {"kind": kind, "image": row.get("image"), "image_digest": digest, "repo_dir": repo_dir, "test_python": interpreter, "test_files": files,
             "command": command, "probe_dir": job_dir, "job_dir": job_dir, "stages": stages, "probed_at": probed_at, "test_user": data["test_user"],
             "binding": binding, "statuses": statuses, "dropped": dropped, "order": order,
             "runner": {"order": "definition", "prefix": runner_prefix(interpreter), "exact_command": data.get("exact_command"), "exact": exact_note},
             "errors": {stage: data[stage].get("error") for stage in stages},  # base collect errors are expected; kept for audit
             "counts": {"fail_to_pass": len(measured["fail_to_pass"]), "pass_to_pass": len(measured["pass_to_pass"]),
                        "network_or_env": len(dropped["network_or_env"]), "gold_regressions": len(dropped["gold_regressions"]),
                        "other": len(dropped["other"]), "order_dependent": len(order_dependent), "measured": len(before)}}
    if kind == "image":  # how far SWE-bench's network-era lists are from what the offline verifier can reproduce
        orig_f, orig_p = set(row.get("fail_to_pass") or []), set(row.get("pass_to_pass") or [])
        probe["swebench"] = {"fail_to_pass_not_reproduced": sorted(orig_f - set(measured["fail_to_pass"])),
                             "pass_to_pass_not_reproduced": sorted(orig_p - set(measured["pass_to_pass"])),
                             "fail_to_pass_new": sorted(set(measured["fail_to_pass"]) - orig_f)}
    return {**measured, "statuses": statuses, "probe_dir": job_dir, "test_python": interpreter, "probe": probe}


def reconstruct_probe(row: dict) -> dict | None:
    """Rebuild the probe metadata of a row that was probed before `probe` existed (it carries only a top-level probe_dir plus
    test_python / dockerfile) from the job directory still on disk, without another Modal run. Never for a row that already
    carries a bound `probe` block: if that binding did not match, the row changed since the measurement and re-binding the
    old job dir would launder a stale measurement — such rows must be probed again. None when the evidence is missing or
    unreliable, and None when the job's probe.json records no binding, a different binding, or a run as another user than
    `GRADING_USER` (B-P2-1: unverifiable legacy evidence is never reused)."""
    if (row.get("probe") or {}).get("binding"):
        return None
    job_dir = row.get("probe_dir")
    if not job_dir:
        return None
    path = Path(job_dir) / "verifier" / "probe.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    kind, repo_dir, interpreter, files = _probe_defaults(row)
    import shlex
    command = (row.get("probe") or {}).get("command") or runner_prefix(interpreter) + " ".join(shlex.quote(p) for p in files)
    try:
        res = _probe_record(row, data, kind=kind, digest=(row.get("probe") or {}).get("image_digest"), repo_dir=repo_dir, interpreter=interpreter, files=files,
                            command=command, job_dir=str(job_dir), probed_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
    except (ValueError, KeyError):
        return None
    res["probe"]["reconstructed"] = True
    res["dockerfile"] = row.get("dockerfile")
    return res


def probe_tests(row: dict, ws) -> dict:
    """Separate authoring probe on Modal (nop agent) that measures the contract offline, for repo-URL rows (Dockerfile recipe,
    `/app`) and SWE-bench rows (prebuilt `image`, `/testbed`, the testbed interpreter) alike: the verifier runs every test in
    the test files touched by test_patch with test_patch applied, then again after the gold patch, and the statuses decide
    fail_to_pass / pass_to_pass / dropped. SWE-bench's own lists were produced with network access and do not reproduce in
    the offline verifier, so nothing is drafted from them unmeasured. Returns {fail_to_pass, pass_to_pass, dropped, statuses,
    dockerfile, probe_dir, test_python, probe} where `probe` is the metadata attached to the candidate row."""
    import shlex
    import shutil
    from swesim import harbor_cli
    from swesim.paths import atomic_json

    resolve_docker_hub_digest, verifier_src = _compile_imports()
    kind, repo_dir, interpreter, files = _probe_defaults(row)
    name = row["instance_id"].lower().replace("__", "--")
    binding = probe_binding(row)
    job_name = f"{name}--{binding[:16]}"  # a job dir names what it measured: a changed base_commit/test_patch never reuses an old one
    root = ws.root / "probes" / name
    env = root / "environment"
    tests = root / "tests"
    env.mkdir(parents=True, exist_ok=True)
    tests.mkdir(exist_ok=True)
    if kind == "dockerfile":
        digest = resolve_docker_hub_digest(BASE_IMAGE)
        if not digest:
            raise RuntimeError("base image digest unavailable")
        dockerfile = (f"FROM python@{digest}\n"
                      "RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*\n"
                      f"RUN git clone {shlex.quote('https://github.com/' + row['repo'])} /app && cd /app && git checkout {row['base_commit']} && {row['install_command']}\n"
                      "WORKDIR /app\n")
    else:
        digest = resolve_docker_hub_digest(row["image"])
        ref = f"{row['image'].rsplit(':', 1)[0]}@{digest}" if digest else row["image"]  # pin when Docker Hub answers, else the tag
        dockerfile = f"FROM {ref}\nWORKDIR {repo_dir}\n"
    row["dockerfile"] = dockerfile
    (env / "Dockerfile").write_text(dockerfile)
    (tests / "Dockerfile").write_text(dockerfile + "\n" + verifier_setup(_verifier_python()) + "COPY . /tests/\n")
    for p in verifier_src.glob("*.py"):
        shutil.copyfile(p, tests / p.name)
    (tests / "test.patch").write_text(row["test_patch"])
    (tests / "oracle.diff").write_text(row["patch"])
    if not files:
        raise ValueError("no python test files in test_patch to probe")
    command = runner_prefix(interpreter) + " ".join(shlex.quote(p) for p in files)
    atomic_json(tests / "probe_config.json", {"command": command, "prefix": runner_prefix(interpreter), "max_command_chars": MAX_COMMAND_CHARS,
                                              "repo_dir": repo_dir, "test_timeout_sec": PROBE_TIMEOUT_SEC, "kind": kind,
                                              "binding": binding, "test_user": GRADING_USER})
    # The probe controller (grader import, patch application, status collection) runs under the pinned verifier interpreter,
    # exactly like test.sh runs grader.py for compiled tasks; the test subprocess itself uses `command`'s task interpreter.
    (tests / "test.sh").write_text("#!/bin/bash\nset -euo pipefail\n" + SWESIM_PYTHON + " -I /tests/probe.py\n")
    (tests / "probe.py").write_text(PROBE_SCRIPT)
    (root / "instruction.md").write_text("Environment authoring probe; no solver work.")
    (root / "task.toml").write_text(PROBE_TASK_TOML)
    td = harbor_cli.run_task(root, "nop", ws.jobs_dir / "probe", job_name, timeout_sec=9000)
    data = json.loads((td / "verifier" / "probe.json").read_text())
    res = _probe_record(row, data, kind=kind, digest=digest, repo_dir=repo_dir, interpreter=interpreter, files=files, command=command,
                        job_dir=str(td), probed_at=datetime.now(timezone.utc).isoformat())
    res["dockerfile"] = dockerfile
    return res
