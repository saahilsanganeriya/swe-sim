"""Discovery survives GitHub's API rate limit: `threads` classifies a rate-limited `gh api` call as `RateLimited` with the
reset time, `github_repo.Discovery` resumes a scan at the page or PR that was refused, `discover_activity` heartbeat-waits
for the reset instead of failing the attempt, and the discover activity runs under its own retry class. No network, no
sleeping: every clock here is a fake."""
from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from subprocess import CalledProcessError, CompletedProcess
from urllib.parse import urlsplit

import pytest

from swesim.sources import github_repo as G, threads
from test_github_repo import FakeGH, _routes

RESET = 1_800_000_000.0
NOW = RESET - 900.0


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """threads' wall clock is `clock["now"]`; a real sleep anywhere in these paths is a test failure."""
    clock = {"now": NOW}
    monkeypatch.setattr(threads, "_now", lambda: clock["now"])
    monkeypatch.setattr(threads, "_status_cache", None)
    monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError(f"real sleep({s}) in a rate-limit test")))
    return clock


def gh_error(stderr: str, output: str = "") -> CalledProcessError:
    return CalledProcessError(1, ["gh", "api", "repos/o/r/pulls/42"], output=output, stderr=stderr)


def rate_limit_payload(remaining: int, limit: int = 5000, reset: float = RESET) -> dict:
    return {"resources": {"core": {"limit": limit, "used": limit - remaining, "remaining": remaining, "reset": int(reset)}}, "rate": {}}


class GHRuns:
    """subprocess.run stand-in: `gh api rate_limit` answers from `status`; every other path raises `error`."""

    def __init__(self, error: Exception | None, status: dict | Exception | None = None):
        self.error, self.status, self.calls = error, status, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[2] == "rate_limit":
            if isinstance(self.status, Exception):
                raise self.status
            if self.status is None:
                raise CalledProcessError(1, cmd, output="", stderr="gh: Not Found (HTTP 404)")
            return CompletedProcess(cmd, 0, stdout=json.dumps(self.status), stderr="")
        if self.error is not None:
            raise self.error
        return CompletedProcess(cmd, 0, stdout="{}", stderr="")


# ---------------------------------------------------------------- threads: classification and reset time

def test_403_with_ratelimit_headers_reads_the_reset_without_another_call(monkeypatch):
    headers = "HTTP/2.0 403 Forbidden\nContent-Type: application/json\nX-Ratelimit-Limit: 5000\nX-Ratelimit-Remaining: 0\nX-Ratelimit-Reset: %d\n" % RESET
    runs = GHRuns(gh_error("gh: API rate limit exceeded for user ID 1 (HTTP 403) secret detail", output=headers))
    monkeypatch.setattr(subprocess, "run", runs)
    with pytest.raises(threads.RateLimited) as exc:
        threads._gh("repos/o/r/pulls/42")
    e = exc.value
    assert isinstance(e, threads.DiscoveryUnavailable) and e.kind == "rate-limit" and e.error_class == "CalledProcessError"
    assert e.reset_at == RESET and e.remaining == 0 and e.secondary is False and e.wait_seconds() == 900.0
    assert [c[2] for c in runs.calls] == ["repos/o/r/pulls/42"]  # the headers already carried the reset: no rate_limit call
    assert "secret detail" not in str(e) and "secret detail" not in repr(e)


def test_403_text_without_headers_reads_the_reset_from_rate_limit(monkeypatch):
    runs = GHRuns(gh_error("gh: API rate limit exceeded for user ID 1. Please include the request ID ... (HTTP 403)"),
                  status=rate_limit_payload(remaining=0))
    monkeypatch.setattr(subprocess, "run", runs)
    with pytest.raises(threads.RateLimited) as exc:
        threads._gh_text("repos/o/r/pulls/42")
    assert exc.value.reset_at == RESET and exc.value.remaining == 0 and exc.value.secondary is False
    assert [c[2] for c in runs.calls] == ["repos/o/r/pulls/42", "rate_limit"]


def test_429_with_quota_left_is_a_secondary_limit_with_a_minute_horizon(monkeypatch):
    runs = GHRuns(gh_error("gh: You have exceeded a secondary rate limit. Please wait a few minutes before you try again. (HTTP 429)"),
                  status=rate_limit_payload(remaining=4990))
    monkeypatch.setattr(subprocess, "run", runs)
    with pytest.raises(threads.RateLimited) as exc:
        threads._gh("repos/o/r/pulls/42")
    e = exc.value
    assert e.secondary is True and e.remaining == 4990 and e.reset_at == NOW + threads.SECONDARY_LIMIT_WAIT


def test_429_retry_after_header_wins_over_the_default_horizon(monkeypatch):
    runs = GHRuns(gh_error("HTTP 429: too many requests", output="HTTP/2.0 429 Too Many Requests\nRetry-After: 120\nX-Ratelimit-Remaining: 4000\nX-Ratelimit-Reset: %d\n" % RESET))
    monkeypatch.setattr(subprocess, "run", runs)
    with pytest.raises(threads.RateLimited) as exc:
        threads._gh("repos/o/r/pulls/42")
    assert exc.value.reset_at == NOW + 120 and exc.value.secondary is True and exc.value.remaining == 4000 and len(runs.calls) == 1


def test_unreadable_quota_still_raises_rate_limited_with_a_bounded_horizon(monkeypatch):
    runs = GHRuns(gh_error("gh: API rate limit exceeded (HTTP 403)"), status=subprocess.TimeoutExpired(["gh"], 60))
    monkeypatch.setattr(subprocess, "run", runs)
    with pytest.raises(threads.RateLimited) as exc:
        threads._gh("repos/o/r/pulls/42")
    assert exc.value.remaining is None and exc.value.reset_at == NOW + threads.SECONDARY_LIMIT_WAIT


@pytest.mark.parametrize("stderr", ["gh: Not Found (HTTP 404)", "gh: Internal Server Error (HTTP 500)", "gh: Bad credentials (HTTP 401)", ""])
def test_other_gh_failures_propagate_unchanged(monkeypatch, stderr):
    runs = GHRuns(gh_error(stderr), status=rate_limit_payload(remaining=0))
    monkeypatch.setattr(subprocess, "run", runs)
    with pytest.raises(CalledProcessError):
        threads._gh("repos/o/r/pulls/42")
    assert len(runs.calls) == 1 and threads.rate_limited(gh_error(stderr)) is None


def test_headers_alone_classify_a_403_only_when_the_quota_is_exhausted():
    exhausted = gh_error("gh: HTTP 403 (forbidden)", output="HTTP/2.0 403 Forbidden\nX-Ratelimit-Remaining: 0\nX-Ratelimit-Reset: %d\n" % RESET)
    assert threads.rate_limited(exhausted).reset_at == RESET
    forbidden = gh_error("gh: HTTP 403 (forbidden)", output="HTTP/2.0 403 Forbidden\nX-Ratelimit-Remaining: 4000\nX-Ratelimit-Reset: %d\n" % RESET)
    assert threads.rate_limited(forbidden) is None
    assert threads.unavailable_kind(threads.RateLimited(RESET, 0)) == "rate-limit"


def test_rate_limit_status_is_one_call_cached_for_thirty_seconds(monkeypatch, frozen_clock):
    runs = GHRuns(None, status=rate_limit_payload(remaining=4321, limit=5000))
    monkeypatch.setattr(subprocess, "run", runs)
    assert threads.rate_limit_status() == (4321, 5000, RESET)
    frozen_clock["now"] += 29
    assert threads.rate_limit_status() == (4321, 5000, RESET) and len(runs.calls) == 1
    runs.status = rate_limit_payload(remaining=7)
    frozen_clock["now"] += 2
    assert threads.rate_limit_status() == (7, 5000, RESET) and len(runs.calls) == 2
    assert threads.rate_limit_status(fresh=True) == (7, 5000, RESET) and len(runs.calls) == 3
    assert all(c[:3] == ["gh", "api", "rate_limit"] for c in runs.calls)


def test_rate_limit_status_failures_propagate_to_the_caller(monkeypatch):
    monkeypatch.setattr(subprocess, "run", GHRuns(None, status=None))
    with pytest.raises(CalledProcessError):
        threads.rate_limit_status()
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: CompletedProcess(cmd, 0, stdout='{"resources": {}}', stderr=""))
    with pytest.raises(KeyError):
        threads.rate_limit_status(fresh=True)
