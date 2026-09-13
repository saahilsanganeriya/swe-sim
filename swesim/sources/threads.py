"""GitHub issue / pull-request threads via the authenticated `gh api` CLI.

Every call returns one JSON document (never `--paginate`, whose output is several concatenated documents).
Paging is explicit and bounded; rate-limit errors back off (primary limit: sleep until the reported reset,
secondary limit: exponential) a bounded number of times, then propagate.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import time
from urllib.parse import quote, urlencode

log = logging.getLogger(__name__)
RATE_LIMIT_RETRIES = 6
MAX_RATE_LIMIT_SLEEP = 3600.0
SECONDARY_LIMIT_MIN_SLEEP = 60.0  # GitHub asks for at least a minute after a secondary (burst) limit
_RATE_LIMIT = re.compile(r"rate limit|HTTP 429|abuse detection", re.I)
_SECONDARY = re.compile(r"secondary|abuse detection", re.I)
_LINK = re.compile(r"(?:[Ff]ix(?:es|ed)?|[Cc]los(?:es|ed)?|[Rr]esolves?)\s*:?\s*#(\d+)")


def _rate_limit_wait(attempt: int, stderr: str = "") -> float:
    """Seconds to sleep after a rate-limit error: until the primary reset when exhausted, >= 60 s for a secondary
    (burst) limit, else bounded exponential."""
    try:
        proc = subprocess.run(["gh", "api", "rate_limit"], capture_output=True, text=True, check=True, timeout=60)
        core = json.loads(proc.stdout)["resources"]["core"]
        if int(core.get("remaining", 1)) == 0:
            return max(1.0, min(MAX_RATE_LIMIT_SLEEP, float(core["reset"]) - time.time() + 2.0))
    except (subprocess.SubprocessError, ValueError, KeyError, TypeError):
        pass
    backoff = min(2.0 ** (attempt + 1), 120.0)
    return max(backoff, SECONDARY_LIMIT_MIN_SLEEP) if _SECONDARY.search(stderr or "") else backoff


def _gh_text(path: str, accept: str | None = None) -> str:
    cmd = ["gh", "api", path] + (["-H", f"Accept: {accept}"] if accept else [])
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120).stdout
        except subprocess.CalledProcessError as e:
            if attempt < RATE_LIMIT_RETRIES and _RATE_LIMIT.search(e.stderr or ""):
                wait = _rate_limit_wait(attempt, e.stderr or "")
                log.warning("gh api %s: rate limited (%s); retry %d/%d in %.0fs", path, (e.stderr or "").strip()[-120:], attempt + 1, RATE_LIMIT_RETRIES, wait)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")


def _gh(path: str):
    """One `gh api` call -> one parsed JSON document."""
    return json.loads(_gh_text(path))


def pages(path: str, max_pages: int = 10) -> list:
    out = []
    for page in range(1, max_pages + 1):
        rows = _gh(path + ("&" if "?" in path else "?") + urlencode({"per_page": 100, "page": page}))
        if not isinstance(rows, list):
            raise ValueError("expected page array")
        out.extend(rows)
        if len(rows) < 100:
            return out
    raise ValueError("thread pagination exceeded bound; do not silently truncate")


def base_text(repo: str, commit: str, path: str) -> str | None:
    """File text at `commit`, or None when the path does not exist there."""
    api_path = f"repos/{repo}/contents/{quote(path)}?ref={commit}"
    try:
        r = _gh(api_path)
    except subprocess.CalledProcessError as e:
        if "404" in (e.stderr or ""):
            return None
        raise
    if isinstance(r, list):
        raise ValueError(f"{path} is a directory at {commit}")
    if r.get("encoding") == "base64" and r.get("content"):
        return base64.b64decode(r["content"]).decode()
    if r.get("type") == "file":  # >1 MiB files come back without inline content; fetch the raw media type
        return _gh_text(api_path, accept="application/vnd.github.raw+json")
    raise ValueError(f"unsupported contents response for {path} at {commit}")


def _login(obj: dict) -> str:
    return ((obj.get("user") or {}).get("login")) or "ghost"


def _is_bot(obj: dict) -> bool:
    return _login(obj).endswith("[bot]")


def _strip(c: dict) -> dict:
    return {"user": _login(c), "created_at": c.get("created_at", c.get("submitted_at")), "body": c.get("body") or "",
            "url": c.get("html_url"), "path": c.get("path")}


def fetch_issue_thread(repo: str, number: int) -> dict:
    issue = _gh(f"repos/{repo}/issues/{number}")
    comments = pages(f"repos/{repo}/issues/{number}/comments?per_page=100")
    return {"issue_url": issue["html_url"], "number": issue.get("number", number), "title": issue["title"], "body": issue.get("body") or "",
            "user": _login(issue), "created_at": issue["created_at"], "comments": [_strip(c) for c in comments if not _is_bot(c)]}


def closing_text(pr: dict) -> str:
    """Title plus body: maintainers sometimes put the closing keyword in the title ("... (closes #123)")."""
    return f"{pr.get('title') or ''}\n{pr.get('body') or ''}"


def linked_numbers(repo: str, pr: int, body: str) -> list[int]:
    """Issue numbers a PR closes: closing keywords in the body, else cross-referenced issues of this repo in the timeline."""
    found = {int(n) for n in _LINK.findall(body or "")}
    if not found:
        for event in pages(f"repos/{repo}/issues/{pr}/timeline", max_pages=3):
            issue = (event.get("source") or {}).get("issue") or {}
            if event.get("event") == "cross-referenced" and issue and not issue.get("pull_request"):
                if (issue.get("html_url") or "").startswith(f"https://github.com/{repo}/issues/"):
                    found.add(int(issue["number"]))
    return sorted(found)


def fetch_pr_thread(repo: str, pr: int) -> dict:
    p = _gh(f"repos/{repo}/pulls/{pr}")
    body = p.get("body") or ""
    linked = linked_numbers(repo, pr, closing_text(p))
    out = {"pr_url": p["html_url"], "number": p.get("number", pr), "pr_title": p["title"], "pr_body": body, "author": _login(p),
           "merged_at": p.get("merged_at"), "gold_commit": p.get("merge_commit_sha"), "pr_created_at": p["created_at"],
           "reviews": [_strip(c) for c in pages(f"repos/{repo}/pulls/{pr}/reviews")],
           "pr_comments": [_strip(c) for c in pages(f"repos/{repo}/issues/{pr}/comments?per_page=100") if not _is_bot(c)],
           "review_comments": [_strip(c) for c in pages(f"repos/{repo}/pulls/{pr}/comments?per_page=100")],
           "linked_issues": []}
    for n in linked[:3]:
        try:
            out["linked_issues"].append(fetch_issue_thread(repo, n))
        except subprocess.CalledProcessError as e:
            out.setdefault("fetch_errors", []).append({"issue": n, "error": str(e)})
    # Interfaces-block keys: the primary linked issue mirrored at the top level.
    first = out["linked_issues"][0] if out["linked_issues"] else None
    out["issue_url"] = first["issue_url"] if first else None
    out["issue_comments"] = first["comments"] if first else []
    return out
