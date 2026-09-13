"""GitHub issue / pull-request threads via the authenticated `gh api` CLI.

Every call returns one JSON document (never `--paginate`, whose output is several concatenated documents).
Paging is explicit and bounded. GitHub has two limits: the hourly core quota (primary; `x-ratelimit-remaining: 0`,
"API rate limit exceeded") and the secondary (burst/abuse) limit, triggered by concurrent or rapid requests while the
quota is untouched ("You have exceeded a secondary rate limit", sometimes with `retry-after`). Against the secondary
limit every gh call in this process goes through one lock and is paced `GH_PACING_SEC` apart.

Several quota buckets: every call takes its credential from the process-wide `TokenPool` (`swesim.sources.github_auth`;
`GH_TOKEN`, extra `GH_TOKEN_n`, a GitHub App installation), handed to the gh subprocess as `GH_TOKEN` in its environment
and never on its command line or in a log. The pool gives the token with the most quota left. When GitHub refuses a call
under one token, that token is set aside until its reset and the same call is retried at once with another token that
has quota (no Temporal attempt is spent); only when every token is out does a refused call raise `RateLimited` (a
`DiscoveryUnavailable`) carrying `secondary`, the `retry_after` GitHub named, if any, the soonest reset (read from the
response headers of `gh api -i` output or from one uncounted `gh api rate_limit` call under that token) and which
`source` that is; the owner of the clock (the discovery activity, which heartbeats while it waits) decides how long to
wait and resumes. Nothing here sleeps through either limit. `rate_limit_status()` reads each token's core quota (cached
30 s per token) and reports the best one, or the soonest reset when all are empty.
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

from swesim.settings import child_environment, get_settings
from swesim.sources import github_auth

log = logging.getLogger(__name__)
RATE_LIMIT_STATUS_TTL = 30.0
SECONDARY_LIMIT_WAIT = 120.0  # horizon for a secondary (burst) limit without retry-after, and when the quota cannot be read; the activity doubles it
TOKEN_ERROR_SET_ASIDE_SEC = 300.0  # a source whose token could not be minted (network, refused) is skipped this long
GH_PACING_SEC = 0.25  # minimum spacing between two gh calls of this process (tests set 0)
_GH_LOCK = threading.Lock()
_last_call_end = 0.0  # time.monotonic() when the previous gh process ended
_POOL: github_auth.TokenPool | None = None  # built from Settings on the first gh call; tests install a fake
_POOL_LOCK = threading.Lock()
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
                 repo: str = "", where: str = "", error_class: str = "CalledProcessError", source: str = "", exhausted: tuple[str, ...] = ()):
        self.reset_at, self.remaining, self.secondary, self.retry_after = float(reset_at), remaining, secondary, retry_after
        self.source, self.exhausted = source, tuple(exhausted)  # the token bucket this horizon belongs to; every bucket, when all are out
        super().__init__("rate-limit", repo, where, error_class)
        named = f"retry-after {int(retry_after)}s" if retry_after is not None else f"resets at epoch {int(self.reset_at)}"
        who = f" {source}" if source else ""
        text = f"{self.args[0]}; {'secondary limit' if secondary else 'quota exhausted'}{who}, {named}"
        if self.exhausted:
            text += f"; every GitHub token exhausted ({', '.join(self.exhausted)}), {source or 'the soonest'} frees up first"
        self.args = (text,)

    @property
    def authoritative(self) -> bool:
        """GitHub named this horizon (retry-after, or the exhausted quota's reset); False for the default pause."""
        return self.retry_after is not None or (not self.secondary and self.remaining is not None)

    def located(self, repo: str, where: str) -> "RateLimited":
        return RateLimited(self.reset_at, self.remaining, secondary=self.secondary, retry_after=self.retry_after, repo=repo, where=where,
                           error_class=self.error_class, source=self.source, exhausted=self.exhausted)

    def wait_seconds(self, now: float | None = None) -> float:
        return max(0.0, self.reset_at - (_now() if now is None else now))


def pool() -> github_auth.TokenPool:
    """The process-wide token pool, built from `Settings` on first use (`github_auth.pool_from_settings`)."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = github_auth.pool_from_settings(get_settings())
        return _POOL


def _run_gh(cmd: list[str], timeout: float, token: str | None = None) -> subprocess.CompletedProcess:
    """One gh process at a time per worker process, at least `GH_PACING_SEC` apart: GitHub's secondary limit is about
    concurrency and bursts, not the hourly quota, and a worker running several scans must still look like one client.
    `token` reaches gh as `GH_TOKEN` in the child's environment only (None: gh's own credential store)."""
    global _last_call_end
    env = child_environment(GH_TOKEN=token) if token is not None else None
    with _GH_LOCK:
        pause = GH_PACING_SEC - (time.monotonic() - _last_call_end)
        if pause > 0:
            time.sleep(pause)
        try:
            return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout, env=env)
        finally:
            _last_call_end = time.monotonic()


def _read_quota(p: github_auth.TokenPool, name: str, now: float) -> None:
    """One `gh api rate_limit` call under `name`'s token (GitHub does not count it), recorded in the pool. gh failures and
    malformed payloads propagate; a token that cannot be produced sets the source aside and raises `TokenError`."""
    try:
        token = p.token(name)
    except github_auth.TokenError as error:
        log.warning("gh api rate_limit: no token from %s (%s: %s); set aside for %.0fs", name, error.kind, error, TOKEN_ERROR_SET_ASIDE_SEC)
        p.set_aside(name, now + TOKEN_ERROR_SET_ASIDE_SEC)
        raise
    core = json.loads(_run_gh(["gh", "api", "rate_limit"], timeout=60, token=token).stdout)["resources"]["core"]
    p.record_status(name, int(core["remaining"]), int(core["limit"]), float(core["reset"]), now)


def rate_limit_status(*, fresh: bool = False, source: str | None = None) -> tuple[int, int, float]:
    """(remaining, limit, reset_at) of the core REST quota. Without `source`: every token's quota is read (each reading
    cached for `RATE_LIMIT_STATUS_TTL` seconds unless `fresh`) and the token with the most calls left is reported, or
    `(0, limit, soonest reset)` when all are empty. With `source`: that one token's reading. gh failures and malformed
    payloads propagate; a token that cannot be minted is skipped when another can answer, else its `TokenError` is
    raised as `DiscoveryUnavailable`."""
    p = pool()
    now = _now()
    names = (source,) if source is not None else p.names
    failed: github_auth.TokenError | None = None
    for name in names:
        if fresh or p.stale(name, now, ttl=RATE_LIMIT_STATUS_TTL):
            try:
                _read_quota(p, name, now)
            except github_auth.TokenError as error:
                failed = error
    if source is not None:
        if failed is not None:
            raise DiscoveryUnavailable(failed.kind, "", "", "TokenError") from failed
        return p.reading(source, now)
    if failed is not None and not any(state["available"] for state in p.summary(now)):
        raise DiscoveryUnavailable(failed.kind, "", "", "TokenError") from failed
    return p.status(now)


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


def rate_limited(error: BaseException, source: str | None = None) -> RateLimited | None:
    """`error` as GitHub's rate limit with its reset time, or None when it is not a rate-limit failure. The reset comes
    from the response headers when the call printed them (`retry-after`, or `x-ratelimit-reset` with the quota at 0),
    else from one `rate_limit_status(fresh=True, source=source)` call under the token that was refused (`source`, the
    pool name; None reads the pool's best, which is right only for a one-token pool); an unreadable quota gets the
    secondary-limit horizon."""
    if not is_rate_limit_error(error):
        return None
    text = _error_text(error)
    headers = rate_limit_headers(text)
    retry_after = _HEADER_RETRY_AFTER.search(text)
    who = source or ""
    if retry_after:
        pause = float(retry_after.group(1))
        return RateLimited(_now() + pause, headers[0] if headers else None, secondary=True, retry_after=pause, source=who)
    if headers is not None and headers[0] == 0:
        return RateLimited(headers[1], 0, source=who)
    try:
        remaining, _limit, reset_at = rate_limit_status(fresh=True, source=source)
    except (subprocess.SubprocessError, ValueError, KeyError, TypeError, DiscoveryUnavailable):
        return RateLimited(_now() + SECONDARY_LIMIT_WAIT, None, secondary=bool(_SECONDARY.search(text)), source=who)
    if remaining == 0:
        return RateLimited(reset_at, 0, source=who)
    return RateLimited(_now() + SECONDARY_LIMIT_WAIT, remaining, secondary=True, source=who)


def _all_exhausted(p: github_auth.TokenPool, pick: github_auth.Selection, last: RateLimited | None = None) -> RateLimited:
    """Every token is out: the `RateLimited` for the one that frees up soonest (`pick`, from `pool.select`). When that is
    the token GitHub just refused (`last`), its own horizon travels on (retry-after, the quota it read)."""
    if last is not None and last.source == pick.name:
        return RateLimited(last.reset_at, last.remaining, secondary=last.secondary, retry_after=last.retry_after, source=pick.name, exhausted=p.names)
    return RateLimited(pick.reset_at, pick.remaining, secondary=pick.secondary, source=pick.name, exhausted=p.names)


def _gh_text(path: str, accept: str | None = None) -> str:
    """One `gh api` call under the pool's best token. A token GitHub refuses is set aside until its reset and the call
    is retried at once with another token that has quota; a token that cannot be produced is set aside for
    `TOKEN_ERROR_SET_ASIDE_SEC`. With every token out the call raises `RateLimited` naming the soonest reset (or
    `DiscoveryUnavailable` when no token could be produced at all) for the activity to wait on."""
    cmd = ["gh", "api", path] + (["-H", f"Accept: {accept}"] if accept else [])
    p = pool()
    tried: set[str] = set()
    last: RateLimited | None = None
    while True:
        now = _now()
        pick = p.select(now)
        if not pick.available:
            raise _all_exhausted(p, pick, last)
        if pick.name in tried:  # one try per token per call; only reachable when a refusal named a horizon already in the past
            raise last if last is not None else _all_exhausted(p, pick)
        tried.add(pick.name)
        try:
            token = p.token(pick.name)
        except github_auth.TokenError as error:
            log.warning("gh api %s: no token from %s (%s: %s); set aside for %.0fs", path, pick.name, error.kind, error, TOKEN_ERROR_SET_ASIDE_SEC)
            p.set_aside(pick.name, now + TOKEN_ERROR_SET_ASIDE_SEC)
            if p.select(now).available:
                continue
            raise DiscoveryUnavailable(error.kind, "", "", "TokenError") from error
        try:
            out = _run_gh(cmd, timeout=120, token=token).stdout
        except subprocess.CalledProcessError as e:
            limited = rate_limited(e, source=pick.name)
            if limited is None:
                raise
            last = limited
            p.record_limited(pick.name, max(limited.reset_at, now + 1.0), remaining=limited.remaining, secondary=limited.secondary)
            following = p.select(_now())
            log.warning("gh api %s: %s rate limited (%s, remaining=%s, resets in %.0fs); %s", path, pick.name,
                        "secondary" if limited.secondary else "primary", limited.remaining, limited.wait_seconds(),
                        f"retrying with {following.name}" if following.available else "every token is out")
            if following.available:
                continue
            raise _all_exhausted(p, following, limited) from e
        p.record_call(pick.name)
        return out


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
