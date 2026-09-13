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
from test_github_repo import FakeGH, _file, _routes

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


# ---------------------------------------------------------------- threads: the secondary (burst) limit, measured with a full quota

SECONDARY_TEXT = ("gh: You have exceeded a secondary rate limit. Please wait a few minutes before you try again. "
                  "If you reach out to GitHub Support for help, please include the request ID ... review our Terms of Service (HTTP 403)")


def test_secondary_limit_text_with_a_full_quota_is_its_own_case(monkeypatch):
    runs = GHRuns(gh_error(SECONDARY_TEXT), status=rate_limit_payload(remaining=5000))
    monkeypatch.setattr(subprocess, "run", runs)
    with pytest.raises(threads.RateLimited) as exc:
        threads._gh("repos/o/r/pulls?state=closed&page=1")
    e = exc.value
    assert e.secondary is True and e.retry_after is None and e.remaining == 5000 and e.authoritative is False
    assert e.reset_at == NOW + threads.SECONDARY_LIMIT_WAIT and threads.SECONDARY_LIMIT_WAIT == 120.0
    assert "secondary limit" in str(e) and "Terms of Service" not in str(e)


def test_secondary_limit_retry_after_header_is_honoured(monkeypatch):
    runs = GHRuns(gh_error(SECONDARY_TEXT, output="HTTP/2.0 403 Forbidden\nRetry-After: 60\nX-Ratelimit-Remaining: 5000\nX-Ratelimit-Reset: %d\n" % RESET),
                  status=rate_limit_payload(remaining=5000))
    monkeypatch.setattr(subprocess, "run", runs)
    with pytest.raises(threads.RateLimited) as exc:
        threads._gh("repos/o/r/pulls?state=closed&page=1")
    e = exc.value
    assert e.secondary is True and e.retry_after == 60 and e.reset_at == NOW + 60 and e.authoritative is True and e.remaining == 5000
    assert len(runs.calls) == 1  # the header named the pause: no rate_limit call


def test_primary_exhaustion_is_authoritative_and_a_default_horizon_is_not():
    assert threads.RateLimited(RESET, 0).authoritative is True
    assert threads.RateLimited(NOW + 120, None).authoritative is False
    assert threads.RateLimited(NOW + 120, 4999, secondary=True).authoritative is False


_REAL_SLEEP = time.sleep


def test_gh_calls_are_serialised_and_paced_per_process(monkeypatch):
    monkeypatch.setattr(time, "sleep", _REAL_SLEEP)
    monkeypatch.setattr(threads, "GH_PACING_SEC", 0.02)
    monkeypatch.setattr(threads, "_last_call_end", 0.0)
    state = {"inflight": 0, "max": 0, "starts": []}; guard = threading.Lock()

    def run(cmd, **kw):
        with guard:
            state["inflight"] += 1; state["max"] = max(state["max"], state["inflight"]); state["starts"].append(time.monotonic())
        _REAL_SLEEP(0.005)
        with guard:
            state["inflight"] -= 1
        return CompletedProcess(cmd, 0, stdout="{}", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    workers = [threading.Thread(target=threads._gh, args=(f"repos/o/r/pulls/{i}",)) for i in range(6)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert len(state["starts"]) == 6 and state["max"] == 1                       # never two gh processes at once
    gaps = [b - a for a, b in zip(state["starts"], state["starts"][1:])]
    assert min(gaps) >= 0.005 + 0.02 - 0.003                                     # each call waits for the previous to end, then the pacing


# ---------------------------------------------------------------- github_repo: resume at the refused page or PR, cheaper call order

class LimitedOnce:
    """FakeGH route: the first call is refused with `stderr`; later calls answer `payload`."""

    def __init__(self, payload, stderr=SECONDARY_TEXT):
        self.payload, self.stderr, self.tripped = payload, stderr, False

    def __call__(self, q):
        if not self.tripped:
            self.tripped = True
            return RuntimeError(self.stderr)
        return self.payload(q) if callable(self.payload) else self.payload


def test_scan_resumes_at_the_refused_pr_and_finishes_like_an_uninterrupted_one(monkeypatch):
    routes = _routes(); routes["rate_limit"] = rate_limit_payload(remaining=5000)
    routes["repos/o/r/pulls/42"] = LimitedOnce(routes["repos/o/r/pulls/42"])
    gh = FakeGH(routes); monkeypatch.setattr(subprocess, "run", gh)
    scan = G.begin_scan("o/r", "2026-06-01", max_prs=150, kind="feature")
    with pytest.raises(G.RateLimited) as exc:
        scan.run()
    e = exc.value
    assert e.secondary and e.where == "PR 42" and e.repo == "o/r" and e.remaining == 5000 and e.retry_after is None
    assert not scan.done and scan.listed and scan.cursor == 41 and 42 not in {o.pr_number for o in scan.observations}
    assert len(scan.observations) == 41 and all(o.status != "pending" for o in scan.observations)
    assert scan.run() is True and scan.done
    kept, rejects, record = G.finish_scan(scan)
    assert [c["pr"] for c in kept] == [42] and rejects == {"diff-too-small": 1, "no-linked-issue": 1, "linked-target-is-pr": 1}
    assert record.funnel == {"listed": 105, "merged-in-range": 4, "linked-issue": 2, "band-passed": 1, "retained": 1}
    details = [urlsplit(c[2]).path for c in gh.calls if "-H" not in c]
    assert details.count("repos/o/r/pulls/42") == 2 and details.count("repos/o/r/pulls/7") == 1  # only the refused PR's detail is fetched again
    with pytest.raises(ValueError, match="not complete"):
        G.finish_scan(G.begin_scan("o/r", "2026-06-01"))


def test_scan_resumes_at_the_refused_list_page(monkeypatch):
    routes = _routes(); routes["rate_limit"] = rate_limit_payload(remaining=5000)
    routes["repos/o/r/pulls"] = LimitedOnce(routes["repos/o/r/pulls"])
    gh = FakeGH(routes); monkeypatch.setattr(subprocess, "run", gh)
    scan = G.begin_scan("o/r", "2026-06-01", max_prs=150, kind="feature")
    with pytest.raises(G.RateLimited) as exc:
        scan.run()
    assert exc.value.where == "list page 1" and scan.page == 1 and not scan.listed and scan.observations == [] and scan.summaries == []
    assert scan.run() and gh.list_pages() == ["1", "1", "2"]
    assert [c["pr"] for c in scan.cands] == [42] and len(scan.observations) == 105


def test_run_in_batches_decides_prs_in_order_and_reports_completion(monkeypatch):
    gh = FakeGH(_routes()); monkeypatch.setattr(subprocess, "run", gh)
    scan = G.begin_scan("o/r", "2026-06-01", max_prs=150, kind="bugfix")
    steps = []
    while not scan.run(batch=40):
        steps.append((scan.cursor, len(scan.observations)))
    assert steps == [(40, 40), (80, 80)] and scan.cursor == 105 and scan.done
    assert [c["pr"] for c in scan.cands] == [42] and scan.rejects == {"diff-too-small": 1, "no-linked-issue": 1, "linked-target-is-pr": 1}


def test_cheaper_call_order_defers_the_diff_and_the_thread_until_the_band_needs_them(monkeypatch):
    gh = FakeGH(_routes()); monkeypatch.setattr(subprocess, "run", gh)
    observations = []
    cands, rejects = G.discover("o/r", since="2026-06-01", max_prs=150, kind="feature", observations=observations)
    assert [c["pr"] for c in cands] == [42] and rejects == {"diff-too-small": 1, "no-linked-issue": 1, "linked-target-is-pr": 1}
    paths = [urlsplit(c[2]).path for c in gh.calls]
    diffs = [urlsplit(c[2]).path for c in gh.calls if "-H" in c]
    assert diffs == ["repos/o/r/pulls/42"]                      # 7 is under the floor by its totals; 9 and 11 fail on links: no diff for them
    assert paths.count("repos/o/r/pulls/42") == 2               # detail + diff; the thread reuses the prefetched detail and links
    assert paths.count("repos/o/r/pulls/7") == 1 and paths.count("repos/o/r/issues/6") == 1 and "repos/o/r/issues/6/comments" not in paths
    assert [p for p in paths if p.startswith("repos/o/r/pulls/7/") or p.startswith("repos/o/r/issues/7/")] == []
    assert len(gh.calls) == 16                                  # was 27 with the detail -> links -> diff -> thread-for-every-linked-PR order
    by_pr = {o.pr_number: o for o in observations}
    assert by_pr[7].reason == "diff-too-small" and by_pr[7].linked_issue_numbers == [6] and "linked-issue" in by_pr[7].stages
    assert by_pr[7].source_changed_lines is None and by_pr[42].source_changed_lines == 250 and by_pr[42].comment_count == 8
    assert cands[0]["comments_total"] == 8 and cands[0]["thread"]["linked_issues"][0]["number"] == 40


def test_a_detail_without_totals_falls_back_to_the_diff_for_the_same_decision(monkeypatch):
    routes = _routes()
    routes["repos/o/r/pulls/7"] = {k: v for k, v in routes["repos/o/r/pulls/7"].items() if k not in ("additions", "deletions", "changed_files")}
    gh = FakeGH(routes); monkeypatch.setattr(subprocess, "run", gh)
    observations = []
    cands, rejects = G.discover("o/r", since="2026-06-01", max_prs=150, kind="feature", observations=observations)
    assert rejects == {"diff-too-small": 1, "no-linked-issue": 1, "linked-target-is-pr": 1}
    assert [urlsplit(c[2]).path for c in gh.calls if "-H" in c] == ["repos/o/r/pulls/7", "repos/o/r/pulls/42"]
    assert next(o for o in observations if o.pr_number == 7).source_changed_lines == 3
    assert G.detail_changed_lines({"additions": "x"}) is None and G.detail_changed_lines({"additions": 3, "deletions": 2}) == 5


def test_totals_prefilter_is_only_the_exact_floor_never_the_ceiling(monkeypatch):
    """A PR whose whole diff is over the ceiling may still be inside the code-only band (large tests): the ceiling is measured on the diff."""
    routes = _routes()
    big_tests = _file("pkg/a.py", 250) + _file("pkg/b.py", 30) + _file("pkg/c.py", 20) + _file("tests/test_stream.py", 1400)
    routes["repos/o/r/pulls/42"] = {**routes["repos/o/r/pulls/42"], "additions": 1700, "deletions": 0}
    routes[("repos/o/r/pulls/42", "diff")] = big_tests
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    cands, rejects = G.discover("o/r", since="2026-06-01", max_prs=150, kind="feature")
    assert [c["pr"] for c in cands] == [42] and cands[0]["additions"] == 300 and "diff-too-large" not in rejects


# ---------------------------------------------------------------- discover_activity: heartbeat-wait, resume, quota margin, one scan per worker

from datetime import timedelta  # noqa: E402

from temporalio.exceptions import ApplicationError  # noqa: E402

from swesim.workflows import activities as A, queues as Q, retries, workflows as W  # noqa: E402


@pytest.fixture
def activity_clock(monkeypatch, frozen_clock):
    """activities' clock follows threads' fake; `_sleep` advances it and records; heartbeats are captured; no Temporal context."""
    seen = {"sleeps": [], "heartbeats": []}
    monkeypatch.setattr(A, "_clock", lambda: frozen_clock["now"])

    async def sleep(seconds):
        seen["sleeps"].append(seconds); frozen_clock["now"] += seconds
    monkeypatch.setattr(A, "_sleep", sleep)
    monkeypatch.setattr(A, "_heartbeat", lambda *details: seen["heartbeats"].append(details[0] if details else None))
    monkeypatch.setattr(A, "_attempt_time_left", lambda: None)
    return seen


def test_wait_heartbeats_every_thirty_seconds_until_the_reset_plus_grace(activity_clock, frozen_clock):
    waited = asyncio.run(A.wait_for_github_rate_limit(NOW + 100, remaining=0))
    assert waited == 105 and activity_clock["sleeps"] == [30, 30, 30, 15] and frozen_clock["now"] == NOW + 105
    assert [h["waiting_for"] for h in activity_clock["heartbeats"]] == ["github-rate-limit"] * 4
    assert all(h["reset_at"] == NOW + 100 and h["remaining"] == 0 and h["secondary"] is False for h in activity_clock["heartbeats"])
    assert asyncio.run(A.wait_for_github_rate_limit(NOW - 10)) == 0.0 and len(activity_clock["sleeps"]) == 4  # a past reset: no wait


def test_a_wait_beyond_the_attempts_remaining_time_is_a_transient_failure(activity_clock, monkeypatch):
    monkeypatch.setattr(A, "_attempt_time_left", lambda: 60.0)
    with pytest.raises(ApplicationError) as exc:
        asyncio.run(A.wait_for_github_rate_limit(NOW + 100))
    assert exc.value.type == "GitHubRateLimited" and exc.value.non_retryable is False and activity_clock["sleeps"] == []
    monkeypatch.setattr(A, "_attempt_time_left", lambda: 200.0)
    assert asyncio.run(A.wait_for_github_rate_limit(NOW + 100)) == 105


def test_secondary_limits_without_a_named_pause_wait_120s_doubling_to_15_minutes(activity_clock, frozen_clock):
    unnamed = threads.RateLimited(NOW + threads.SECONDARY_LIMIT_WAIT, 5000, secondary=True)
    assert [A.rate_limit_horizon(unnamed, k) - frozen_clock["now"] for k in range(6)] == [120, 240, 480, 900, 900, 900]
    assert A.rate_limit_horizon(threads.RateLimited(NOW + 60, 5000, secondary=True, retry_after=60), 3) == NOW + 60  # retry-after is honoured
    assert A.rate_limit_horizon(threads.RateLimited(RESET, 0), 3) == RESET                                             # so is the quota's reset
    assert A.rate_limit_horizon(threads.RateLimited(NOW + 120, None), 1) == NOW + 240                                   # unreadable quota: doubling too


class FakeScan:
    """A `Discovery` stand-in: `run` raises the queued refusals one per call, then completes."""

    def __init__(self, refusals=(), repo="o/r"):
        self.refusals, self.runs, self.done, self.repo = list(refusals), 0, False, repo

    def run(self, batch=None):
        self.runs += 1
        if self.refusals:
            raise self.refusals.pop(0)
        self.done = True
        return True


def test_run_discovery_waits_out_refusals_and_resumes_within_the_attempt(activity_clock, frozen_clock, monkeypatch):
    monkeypatch.setattr(A, "_github_quota", lambda: (5000, 5000, RESET))
    scan = FakeScan([threads.RateLimited(NOW + 120, 5000, secondary=True), threads.RateLimited(NOW + 120, 5000, secondary=True)])
    asyncio.run(A.run_discovery(scan))
    assert scan.done and scan.runs == 3
    assert sum(activity_clock["sleeps"]) == (120 + 5) + (240 + 5)  # first refusal 120 s, the consecutive one 240 s, each plus the grace
    assert {h["waiting_for"] for h in activity_clock["heartbeats"]} == {"github-rate-limit"} and all(h["secondary"] for h in activity_clock["heartbeats"])


def test_run_discovery_resets_the_backoff_after_a_successful_batch(activity_clock, monkeypatch):
    monkeypatch.setattr(A, "_github_quota", lambda: (5000, 5000, RESET))

    class TwoBatches(FakeScan):
        def run(self, batch=None):
            self.runs += 1
            if self.runs == 1:
                raise threads.RateLimited(NOW + 120, 5000, secondary=True)
            if self.runs == 3:
                raise threads.RateLimited(NOW + 120, 5000, secondary=True)
            self.done = self.runs >= 4
            return self.done
    scan = TwoBatches()
    asyncio.run(A.run_discovery(scan))
    assert scan.runs == 4 and sum(activity_clock["sleeps"]) == 125 + 125  # a completed batch in between: back to 120 s


def test_run_discovery_waits_for_the_quota_when_below_the_margin(activity_clock, frozen_clock, monkeypatch):
    monkeypatch.setattr(A, "_github_quota", lambda: (A.GH_RATE_MARGIN - 1, 5000, NOW + 50))
    scan = FakeScan()
    asyncio.run(A.run_discovery(scan))
    assert scan.done and activity_clock["sleeps"] == [30, 25] and activity_clock["heartbeats"][0]["remaining"] == A.GH_RATE_MARGIN - 1
    monkeypatch.setattr(A, "_github_quota", lambda: None)  # gh cannot answer: the scan itself classifies the outage
    activity_clock["sleeps"].clear()
    asyncio.run(A.run_discovery(FakeScan()))
    assert activity_clock["sleeps"] == []
    assert A.GH_RATE_MARGIN == 300 and A.DISCOVERY_BATCH >= 1


def test_run_discovery_lets_other_unavailability_fail_the_attempt(activity_clock, monkeypatch):
    monkeypatch.setattr(A, "_github_quota", lambda: None)
    with pytest.raises(threads.DiscoveryUnavailable) as exc:
        asyncio.run(A.run_discovery(FakeScan([threads.DiscoveryUnavailable("server-error", "o/r", "PR 1", "CalledProcessError")])))
    assert exc.value.kind == "server-error" and activity_clock["sleeps"] == []


def test_discover_activity_runs_one_scan_at_a_time_per_worker(tmp_path, monkeypatch):
    from swesim.paths import Workspace
    monkeypatch.setattr(A, "WS", Workspace(tmp_path / "workspace"))
    monkeypatch.setattr(A, "_github_quota", lambda: None)
    monkeypatch.setattr(A, "DISCOVERY_SLOTS", asyncio.Semaphore(Q.DISCOVERY_CONCURRENCY))
    state = {"inflight": 0, "max": 0, "scans": 0}; guard = threading.Lock()

    def run(self, batch=None):
        with guard:
            state["inflight"] += 1; state["max"] = max(state["max"], state["inflight"]); state["scans"] += 1
        _REAL_SLEEP(0.02)
        with guard:
            state["inflight"] -= 1
        self.done = True
        return True
    monkeypatch.setattr(G.Discovery, "run", run)

    async def three():
        return await asyncio.gather(A.discover_activity({"repo": "x/y", "kind": "bugfix"}), A.discover_activity({"repo": "x/z", "kind": "bugfix"}),
                                    A.discover_activity({"repo": "x/w", "kind": "feature"}))
    results = asyncio.run(three())
    assert state["scans"] == 3 and state["max"] == 1 == Q.DISCOVERY_CONCURRENCY
    assert [r["written"] for r in results] == [0, 0, 0] and {r["source_key"] for r in results} == {"github-repo--x__y--bugfix", "github-repo--x__z--bugfix", "github-repo--x__w--feature"}
    assert isinstance(A.DISCOVERY_SLOTS, asyncio.Semaphore) and Q.DISCOVERY_CONCURRENCY == 1 and "DISCOVERY_CONCURRENCY" in Q.__doc__


# ---------------------------------------------------------------- the retry class and the act wiring

def test_discovery_retry_class_values():
    d = retries.DISCOVERY
    assert d.initial_interval == timedelta(seconds=60) and d.backoff_coefficient == 2.0
    assert d.maximum_interval == timedelta(minutes=15) and d.maximum_attempts == 8 and d.non_retryable_error_types is None
    assert retries.LLM.maximum_attempts == 4 and retries.LLM.maximum_interval == timedelta(minutes=2)  # the class discovery no longer uses is unchanged


def test_act_passes_the_discovery_timeouts_and_keeps_the_default_heartbeat_for_others(monkeypatch):
    seen = []

    async def execute(name, p, **kw):
        seen.append((name, kw)); return {}
    monkeypatch.setattr(W.workflow, "execute_activity", execute)
    asyncio.run(W.act("discover_activity", {"repo": "x/y"}, W.DISCOVERY_MINUTES, retry=retries.DISCOVERY, heartbeat_minutes=W.DISCOVERY_HEARTBEAT_MINUTES))
    name, kw = seen[0]
    assert kw["start_to_close_timeout"] == timedelta(hours=2) and kw["heartbeat_timeout"] == timedelta(minutes=3)
    assert kw["retry_policy"] is retries.DISCOVERY and kw["task_queue"] == Q.AUTHORING
    asyncio.run(W.act("judge_activity", {"x": 1}))
    assert seen[1][1]["heartbeat_timeout"] == timedelta(minutes=2) and seen[1][1]["start_to_close_timeout"] == timedelta(minutes=10)
    assert seen[1][1]["retry_policy"] is retries.LLM


def test_create_tasks_calls_discover_with_the_discovery_class_two_hours_and_a_three_minute_heartbeat(monkeypatch):
    from types import SimpleNamespace
    calls = []

    async def act(name, p, minutes=10, **kw):
        calls.append((name, minutes, kw))
        if name == "probe_models_activity":
            return {"resolved": {}, "substitutions": {}, "notes": [], "needs_human": []}
        if name == "discover_activity":
            raise RuntimeError("stop after discovery")
        return {}
    monkeypatch.setattr(W, "act", act)
    monkeypatch.setattr(W.workflow, "info", lambda: SimpleNamespace(parent=None, workflow_id="create-1", run_id="r1", workflow_type="CreateTasksWorkflow"))
    params = {"sources": [{"kind": "github-repo", "repo": "https://github.com/x/y", "pr_kind": "bugfix", "max_candidates": 20}], "judge_model": "j", "draft_model": "d"}
    try:
        asyncio.run(W.CreateTasksWorkflow().run(params))
    except Exception:  # noqa: BLE001 - the failed source is recorded; what the empty plan does afterwards is another test's concern
        pass
    discover = [c for c in calls if c[0] == "discover_activity"]
    assert len(discover) == 1
    _, minutes, kw = discover[0]
    assert minutes == W.DISCOVERY_MINUTES == 120 and kw["retry"] is retries.DISCOVERY and kw["heartbeat_minutes"] == W.DISCOVERY_HEARTBEAT_MINUTES == 3
    assert all("heartbeat_minutes" not in kw for name, _, kw in calls if name != "discover_activity")  # every other act call is unchanged
