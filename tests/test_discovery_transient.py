"""gh being unavailable fails the discovery attempt (Temporal retries it); only PR facts are PR decisions. No network."""
import subprocess

import pytest

from swesim.sources import github_repo as G, threads
from test_github_repo import FakeGH, _routes


def gh_error(stderr: str):
    return subprocess.CalledProcessError(1, ["gh", "api"], stderr=stderr)


def gh_route_error(stderr: str):
    """FakeGH turns an exception route into a CalledProcessError whose stderr is str(exception)."""
    return RuntimeError(stderr)


@pytest.fixture(autouse=True)
def no_rate_limit_sleeps(monkeypatch):
    monkeypatch.setattr(threads, "RATE_LIMIT_RETRIES", 0)
    monkeypatch.setattr(threads.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("discovery must not sleep in tests")))


@pytest.mark.parametrize("stderr, kind", [
    ("gh: API rate limit exceeded for user ID 1 (HTTP 403)", "rate-limit"),
    ("HTTP 429: too many requests", "rate-limit"),
    ("You have exceeded a secondary rate limit; abuse detection", "rate-limit"),
    ("gh: Internal Server Error (HTTP 500) secret detail", "server-error"),
    ("HTTP 502 Bad Gateway", "server-error"),
    ("error connecting to api.github.com\ncheck your internet connection", "network"),
    ("Post \"https://api.github.com/graphql\": dial tcp: lookup api.github.com: no such host", "network"),
    ("read tcp 10.0.0.2: i/o timeout", "network"),
    ("", "network"),
    ("gh: Bad credentials (HTTP 401)", "auth"),
    ("HTTP 403: Resource not accessible by integration", "auth"),
])
def test_unavailability_kinds(stderr, kind):
    assert G.unavailable_kind(gh_error(stderr)) == kind


def test_answers_are_not_unavailability():
    assert G.unavailable_kind(gh_error("gh: Not Found (HTTP 404)")) is None
    assert G.unavailable_kind(gh_error("gh: Validation Failed (HTTP 422)")) is None
    assert G.unavailable_kind(ValueError("expected page array")) is None
    assert G.unavailable_kind(subprocess.TimeoutExpired(["gh"], 120)) == "timeout"


@pytest.mark.parametrize("route, stderr, kind, where", [
    ("repos/o/r/pulls/42", "API rate limit exceeded (HTTP 403) secret detail", "rate-limit", "PR 42"),
    ("repos/o/r/pulls/42", "HTTP 503 Service Unavailable", "server-error", "PR 42"),
    (("repos/o/r/pulls/42", "diff"), "error connecting to api.github.com", "network", "PR 42 diff"),
    ("repos/o/r/issues/40", "HTTP 500", "server-error", "PR 42 links"),
    ("repos/o/r/issues/40/comments", "HTTP 502", "server-error", "PR 42 thread"),
    ("repos/o/r/pulls", "HTTP 500 secret detail", "server-error", "list page 1"),
])
def test_unavailable_gh_fails_the_attempt_instead_of_rejecting_prs(monkeypatch, route, stderr, kind, where):
    routes = _routes()
    routes[route] = gh_route_error(stderr)
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    observations = []
    with pytest.raises(G.DiscoveryUnavailable) as exc:
        G.discover("o/r", since="2026-06-01", max_prs=150, kind="feature", observations=observations)
    assert exc.value.kind == kind and exc.value.repo == "o/r" and exc.value.where == where
    assert exc.value.error_class == "CalledProcessError" and isinstance(exc.value, RuntimeError) and not isinstance(exc.value, ValueError)
    assert "secret detail" not in str(exc.value) and "secret detail" not in repr(exc.value)
    assert not any(o.reason == "fetch-error" for o in observations)  # nothing was counted against a PR


def test_timeout_is_unavailability(monkeypatch):
    routes = _routes()

    def slow(q):
        raise subprocess.TimeoutExpired(["gh", "api"], 120)
    routes["repos/o/r/pulls/42"] = slow
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    with pytest.raises(G.DiscoveryUnavailable) as exc:
        G.discover("o/r", since="2026-06-01", max_prs=150, kind="feature")
    assert exc.value.kind == "timeout" and exc.value.error_class == "TimeoutExpired"


def test_not_found_is_a_pr_decision_and_malformed_answers_stay_fetch_errors(monkeypatch):
    routes = _routes()
    del routes["repos/o/r/pulls/42"]                               # FakeGH answers 404 for an unknown route
    routes["repos/o/r/pulls/7"] = {"number": 8}                     # malformed: detail names another PR
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    observations = []
    cands, rejects = G.discover("o/r", since="2026-06-01", max_prs=150, kind="feature", observations=observations)
    assert cands == [] and rejects == {"not-found": 1, "fetch-error": 1, "no-linked-issue": 1, "linked-target-is-pr": 1}
    by_pr = {o.pr_number: o for o in observations}
    assert by_pr[42].status == "rejected" and by_pr[42].reason == "not-found" and by_pr[42].error_class is None
    assert by_pr[7].status == "error" and by_pr[7].reason == "fetch-error" and by_pr[7].error_class == "ValueError"


def test_not_found_after_the_detail_is_also_a_decision(monkeypatch):
    routes = _routes()
    del routes[("repos/o/r/pulls/42", "diff")]
    monkeypatch.setattr(subprocess, "run", FakeGH(routes))
    observations = []
    cands, rejects = G.discover("o/r", since="2026-06-01", max_prs=150, kind="feature", observations=observations)
    assert cands == [] and rejects["not-found"] == 1 and "fetch-error" not in rejects
    assert next(o for o in observations if o.pr_number == 42).reason == "not-found"
