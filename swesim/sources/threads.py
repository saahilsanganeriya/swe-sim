"""GitHub issue / pull-request threads via the authenticated `gh api` CLI.

Every call returns one JSON document (never `--paginate`, whose output is several concatenated documents).
Paging is explicit and bounded. GitHub has two limits: the hourly core quota (primary; `x-ratelimit-remaining: 0`,
"API rate limit exceeded") and the secondary (burst/abuse) limit, triggered by concurrent or rapid requests while the
quota is untouched ("You have exceeded a secondary rate limit", sometimes with `retry-after`). Against the secondary
limit every gh call in this process goes through one lock and is paced `GH_PACING_SEC` apart. Nothing here sleeps
through either limit: a refused call raises `RateLimited` (a `DiscoveryUnavailable`) carrying `secondary`, the
`retry_after` GitHub named, if any, and a reset time read from the response headers (`gh api -i` output) or from one
uncounted `gh api rate_limit` call; the owner of the clock (the discovery activity, which heartbeats while it waits)
decides how long to wait and resumes. `rate_limit_status()` exposes the core quota, cached for 30 s.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import threading
import time
from urllib.parse import quote, urlencode

log = logging.getLogger(__name__)
RATE_LIMIT_STATUS_TTL = 30.0
SECONDARY_LIMIT_WAIT = 120.0  # horizon for a secondary (burst) limit without retry-after, and when the quota cannot be read; the activity doubles it
GH_PACING_SEC = 0.25  # minimum spacing between two gh calls of this process (tests set 0)
_GH_LOCK = threading.Lock()
_last_call_end = 0.0  # time.monotonic() when the previous gh process ended
_RATE_LIMIT = re.compile(r"rate limit|HTTP 429|abuse detection", re.I)
_SECONDARY = re.compile(r"secondary|abuse detection", re.I)
_HEADER_REMAINING = re.compile(r"^x-ratelimit-remaining:\s*(\d+)\s*$", re.I | re.M)
_HEADER_RESET = re.compile(r"^x-ratelimit-reset:\s*(\d+)\s*$", re.I | re.M)
_HEADER_RETRY_AFTER = re.compile(r"^retry-after:\s*(\d+)\s*$", re.I | re.M)
_HTTP_STATUS = re.compile(r"HTTP(?:/[\d.]+)? (\d{3})\b")
_SERVER_ERROR = re.compile(r"HTTP 5\d\d", re.I)
_AUTH_ERROR = re.compile(r"HTTP 40[13]\b|bad credentials|authentication|not logged in|gh auth login", re.I)
_NOT_FOUND = re.compile(r"HTTP 404\b|Not Found", re.I)
_NETWORK = re.compile(r"error connecting|connection (?:refused|reset)|dial tcp|tls handshake|could not resolve|no such host|network is unreachable"
                      r"|i/o timeout|timed? ?out|unexpected EOF|temporary failure|server misbehaving|broken pipe", re.I)
_LINK = re.compile(r"(?:[Ff]ix(?:es|ed)?|[Cc]los(?:es|ed)?|[Rr]esolves?)\s*:?\s*#(\d+)")
# Weaker evidence, used only when no closing keyword and no timeline cross-reference names a real issue: a plain
# `#N`, `GH-N` or full issue URL in the PR title/body. Recorded as link_kind="mention" so ranking can prefer stronger links.
_MENTION = re.compile(r"(?:(?<![\w/])#|GH-|/issues/)(\d+)")


def _now() -> float:
    """Wall clock (epoch seconds); a module seam so tests run these paths against a fake clock."""
    return time.time()


class DiscoveryUnavailable(RuntimeError):
    """`gh api` could not answer (rate limit, 5xx, network, timeout, auth): an infrastructure failure of this attempt,
    never a PR decision. It propagates out of a scan so the Temporal activity fails and is retried (or, for a rate
    limit, waits); the message names the kind and, once `github_repo._fetch` has located it, the PR, never `gh` stderr."""

    def __init__(self, kind: str, repo: str, where: str, error_class: str):
        self.kind, self.repo, self.where, self.error_class = kind, repo, where, error_class
        located = f" while scanning {repo} {where}" if repo or where else ""
        super().__init__(f"gh api unavailable ({kind}, {error_class}){located}")


class RateLimited(DiscoveryUnavailable):
    """GitHub refused the call under a rate limit. `secondary` marks the burst limit (quota left); `retry_after` is the
    pause GitHub named in seconds, when it named one; `reset_at` is the epoch second to retry at: `retry-after` when
    present, the core quota's reset for an exhausted primary limit, else `SECONDARY_LIMIT_WAIT` from now (a default the
    activity may lengthen: `authoritative` is False then); `remaining` the core calls left when the quota was read
    (None when it could not be read)."""

    def __init__(self, reset_at: float, remaining: int | None, *, secondary: bool = False, retry_after: float | None = None,
                 repo: str = "", where: str = "", error_class: str = "CalledProcessError"):
        self.reset_at, self.remaining, self.secondary, self.retry_after = float(reset_at), remaining, secondary, retry_after
        super().__init__("rate-limit", repo, where, error_class)
        named = f"retry-after {int(retry_after)}s" if retry_after is not None else f"resets at epoch {int(self.reset_at)}"
        self.args = (f"{self.args[0]}; {'secondary limit' if secondary else 'quota exhausted'}, {named}",)

    @property
    def authoritative(self) -> bool:
        """GitHub named this horizon (retry-after, or the exhausted quota's reset); False for the default pause."""
        return self.retry_after is not None or (not self.secondary and self.remaining is not None)

    def located(self, repo: str, where: str) -> "RateLimited":
        return RateLimited(self.reset_at, self.remaining, secondary=self.secondary, retry_after=self.retry_after, repo=repo, where=where,
                           error_class=self.error_class)

    def wait_seconds(self, now: float | None = None) -> float:
        return max(0.0, self.reset_at - (_now() if now is None else now))


_status_cache: tuple[float, tuple[int, int, float]] | None = None


def _run_gh(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """One gh process at a time per worker process, at least `GH_PACING_SEC` apart: GitHub's secondary limit is about
    concurrency and bursts, not the hourly quota, and a worker running several scans must still look like one client."""
    global _last_call_end
    with _GH_LOCK:
        pause = GH_PACING_SEC - (time.monotonic() - _last_call_end)
        if pause > 0:
            time.sleep(pause)
        try:
            return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout)
        finally:
            _last_call_end = time.monotonic()


def rate_limit_status(*, fresh: bool = False) -> tuple[int, int, float]:
    """(remaining, limit, reset_at) of the core REST quota from one `gh api rate_limit` call (GitHub does not count it),
    cached for `RATE_LIMIT_STATUS_TTL` seconds unless `fresh`. gh failures and malformed payloads propagate."""
    global _status_cache
    now = _now()
    if not fresh and _status_cache is not None and 0 <= now - _status_cache[0] < RATE_LIMIT_STATUS_TTL:
        return _status_cache[1]
    proc = _run_gh(["gh", "api", "rate_limit"], timeout=60)
    core = json.loads(proc.stdout)["resources"]["core"]
    status = (int(core["remaining"]), int(core["limit"]), float(core["reset"]))
    _status_cache = (now, status)
    return status


def _error_text(error: subprocess.CalledProcessError) -> str:
    return f"{error.stderr or ''}\n{error.output or ''}"


def rate_limit_headers(text: str) -> tuple[int, float] | None:
    """(remaining, reset_at) from `x-ratelimit-*` response headers in `text` (`gh api -i` output); None when absent."""
    remaining, reset = _HEADER_REMAINING.search(text), _HEADER_RESET.search(text)
    return (int(remaining.group(1)), float(reset.group(1))) if remaining and reset else None


def is_rate_limit_error(error: BaseException) -> bool:
    """A `gh api` failure GitHub attributes to its rate limit: the stderr text ("API rate limit exceeded", HTTP 429,
    secondary/abuse limit) or a 403/429 whose headers show `x-ratelimit-remaining: 0`."""
    if not isinstance(error, subprocess.CalledProcessError):
        return False
    text = _error_text(error)
    if _RATE_LIMIT.search(text):
        return True
    headers = rate_limit_headers(text)
    status = _HTTP_STATUS.search(text)
    return headers is not None and headers[0] == 0 and status is not None and int(status.group(1)) in (403, 429)


def rate_limited(error: BaseException) -> RateLimited | None:
    """`error` as GitHub's rate limit with its reset time, or None when it is not a rate-limit failure. The reset comes
    from the response headers when the call printed them (`retry-after`, or `x-ratelimit-reset` with the quota at 0),
    else from one `rate_limit_status(fresh=True)` call; an unreadable quota gets the secondary-limit horizon."""
    if not is_rate_limit_error(error):
        return None
    text = _error_text(error)
    headers = rate_limit_headers(text)
    retry_after = _HEADER_RETRY_AFTER.search(text)
    if retry_after:
        pause = float(retry_after.group(1))
        return RateLimited(_now() + pause, headers[0] if headers else None, secondary=True, retry_after=pause)
    if headers is not None and headers[0] == 0:
        return RateLimited(headers[1], 0)
    try:
        remaining, _limit, reset_at = rate_limit_status(fresh=True)
    except (subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return RateLimited(_now() + SECONDARY_LIMIT_WAIT, None, secondary=bool(_SECONDARY.search(text)))
    if remaining == 0:
        return RateLimited(reset_at, 0)
    return RateLimited(_now() + SECONDARY_LIMIT_WAIT, remaining, secondary=True)


def _gh_text(path: str, accept: str | None = None) -> str:
    cmd = ["gh", "api", path] + (["-H", f"Accept: {accept}"] if accept else [])
    try:
        return _run_gh(cmd, timeout=120).stdout
    except subprocess.CalledProcessError as e:
        limited = rate_limited(e)
        if limited is None:
            raise
        log.warning("gh api %s: rate limited (%s, remaining=%s); quota resets in %.0fs", path, "secondary" if limited.secondary else "primary",
                    limited.remaining, limited.wait_seconds())
        raise limited from e


def unavailable_kind(error: BaseException) -> str | None:
    """Why `gh api` could not answer (rate-limit, server-error, network, timeout, auth), or None when the failure is an
    answer about the resource (404, 422, malformed payload) or an unknown gh failure."""
    if isinstance(error, DiscoveryUnavailable):
        return error.kind
    if isinstance(error, subprocess.TimeoutExpired):
        return "timeout"
    if not isinstance(error, subprocess.CalledProcessError):
        return None
    stderr = str(error.stderr or "")
    if _RATE_LIMIT.search(stderr):
        return "rate-limit"
    if _SERVER_ERROR.search(stderr):
        return "server-error"
    if _NOT_FOUND.search(stderr):
        return None
    if _AUTH_ERROR.search(stderr):
        return "auth"
    if _NETWORK.search(stderr) or not stderr.strip():  # gh prints nothing useful when the process itself failed to reach the API
        return "network"
    return None


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


def fetch_issue_thread(repo: str, number: int, issue: dict | None = None) -> dict:
    """Issue title/body/comments. `issue` may be a prefetched `repos/{repo}/issues/{n}` payload. PRs are refused:
    the issues endpoint returns them too, but a PR body is not a stakeholder's problem statement."""
    issue = issue if issue is not None else _gh(f"repos/{repo}/issues/{number}")
    if issue.get("pull_request"):
        raise ValueError(f"#{number} is a pull request, not an issue")
    comments = pages(f"repos/{repo}/issues/{number}/comments?per_page=100")
    return {"issue_url": issue["html_url"], "number": issue.get("number", number), "title": issue["title"], "body": issue.get("body") or "",
            "user": _login(issue), "created_at": issue["created_at"], "comments": [_strip(c) for c in comments if not _is_bot(c)]}


def closing_text(pr: dict) -> str:
    """Title plus body: maintainers sometimes put the closing keyword in the title ("... (closes #123)")."""
    return f"{pr.get('title') or ''}\n{pr.get('body') or ''}"


MAX_MENTIONS = 4  # bound on plain-mention issue lookups per PR (one API call each)


def linked_targets(repo: str, pr: int, body: str) -> dict:
    """Classify the numbers a PR claims to close. Closing-keyword numbers are fetched and kept only when the payload is
    a real issue (no `pull_request`); PR-typed targets go to `prs`, unfetchable ones to `missing`. When no real issue
    is named, fall back to issues of this repo that cross-reference the PR in its timeline.
    When the timeline names none either, plain mentions (`#N`, `GH-N`, issue URLs) are tried as the weakest tier.
    Returns {"issues": [n], "prs": [n], "missing": [n], "payloads": {n: issue_json}, "link_kind": closing|timeline|mention|None}."""
    out = {"issues": [], "prs": [], "missing": [], "payloads": {}}
    for n in sorted({int(x) for x in _LINK.findall(body or "")}):
        try:
            payload = _gh(f"repos/{repo}/issues/{n}")
        except subprocess.CalledProcessError as e:
            if "404" in (e.stderr or ""):  # a number this repo cannot serve is a data property; anything else is infrastructure
                out["missing"].append(n)
                continue
            raise
        if payload.get("pull_request"):
            out["prs"].append(n)
        else:
            out["issues"].append(n)
            out["payloads"][n] = payload
    out["link_kind"] = "closing" if out["issues"] else None
    if not out["issues"]:
        for event in pages(f"repos/{repo}/issues/{pr}/timeline", max_pages=3):
            issue = (event.get("source") or {}).get("issue") or {}
            if event.get("event") == "cross-referenced" and issue and not issue.get("pull_request"):
                if (issue.get("html_url") or "").startswith(f"https://github.com/{repo}/issues/") and int(issue["number"]) not in out["issues"]:
                    out["issues"].append(int(issue["number"]))
        out["issues"].sort()
        if out["issues"]:
            out["link_kind"] = "timeline"
    if not out["issues"]:
        mentioned = sorted({int(x) for x in _MENTION.findall(body or "")} - {pr} - set(out["prs"]) - set(out["missing"]))
        for n in mentioned[:MAX_MENTIONS]:
            try:
                payload = _gh(f"repos/{repo}/issues/{n}")
            except subprocess.CalledProcessError as e:
                if "404" in (e.stderr or ""):
                    out["missing"].append(n)
                    continue
                raise
            if payload.get("pull_request"):
                out["prs"].append(n)
            else:
                out["issues"].append(n)
                out["payloads"][n] = payload
        if out["issues"]:
            out["link_kind"] = "mention"
    return out


def linked_numbers(repo: str, pr: int, body: str) -> list[int]:
    """Issue numbers a PR closes (real issues only; see `linked_targets`)."""
    return linked_targets(repo, pr, body)["issues"]


def fetch_pr_thread(repo: str, pr: int, detail: dict | None = None, targets: dict | None = None) -> dict:
    """PR title/body/reviews/comments plus its linked issue threads. `detail` (a `repos/{repo}/pulls/{pr}` payload) and
    `targets` (its `linked_targets`) may be prefetched by a scan that already paid for them."""
    p = detail if detail is not None else _gh(f"repos/{repo}/pulls/{pr}")
    body = p.get("body") or ""
    targets = targets if targets is not None else linked_targets(repo, pr, closing_text(p))
    out = {"pr_url": p["html_url"], "number": p.get("number", pr), "pr_title": p["title"], "pr_body": body, "author": _login(p),
           "merged_at": p.get("merged_at"), "gold_commit": p.get("merge_commit_sha"), "pr_created_at": p["created_at"],
           "reviews": [_strip(c) for c in pages(f"repos/{repo}/pulls/{pr}/reviews")],
           "pr_comments": [_strip(c) for c in pages(f"repos/{repo}/issues/{pr}/comments?per_page=100") if not _is_bot(c)],
           "review_comments": [_strip(c) for c in pages(f"repos/{repo}/pulls/{pr}/comments?per_page=100")],
           "linked_issues": [], "linked_prs": targets["prs"]}
    for n in targets["issues"][:3]:
        try:
            out["linked_issues"].append(fetch_issue_thread(repo, n, issue=targets["payloads"].get(n)))
        except (subprocess.CalledProcessError, ValueError) as e:
            if unavailable_kind(e) is not None:
                raise  # gh unavailable: the caller's attempt fails and is retried; a swallowed error would become a false "missing linked issue"
            out.setdefault("fetch_errors", []).append({"issue": n, "error": str(e)})
    for n in targets["missing"]:
        out.setdefault("fetch_errors", []).append({"issue": n, "error": "closing keyword names a number this repo's issues endpoint cannot serve"})
    # Interfaces-block keys: the primary linked issue mirrored at the top level.
    first = out["linked_issues"][0] if out["linked_issues"] else None
    out["issue_url"] = first["issue_url"] if first else None
    out["issue_comments"] = first["comments"] if first else []
    return out
