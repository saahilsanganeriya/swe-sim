"""Discovery under several GitHub quota buckets: every `gh` call takes its token from `threads`' `TokenPool` (through the
subprocess environment, never argv); a token GitHub refuses is set aside until its reset and the call is retried at once
with another token that has quota; only when every token is out does `RateLimited` reach the activity's heartbeat-wait;
`rate_limit_status()` reads each token's quota and reports the best. No network, no sleeping, fake clocks."""
from __future__ import annotations

import json
import logging
import subprocess
import time
from subprocess import CalledProcessError, CompletedProcess

import pytest

from swesim.sources import github_auth as GA, threads

RESET = 1_800_000_000.0
NOW = RESET - 900.0
TOK_A, TOK_B = "ghp_fake_token_a", "ghs_fake_token_b"


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr(threads, "_now", lambda: clock["now"])
    monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError(f"real sleep({s}) in a token test")))
    return clock


@pytest.fixture
def pool(monkeypatch):
    pool = GA.TokenPool([GA.StaticSource("GH_TOKEN", TOK_A), GA.StaticSource("app:987654321", TOK_B)])
    monkeypatch.setattr(threads, "_POOL", pool)
    return pool


def _headers(reset: float, remaining: int = 0) -> str:
    return f"HTTP/2.0 403 Forbidden\nX-Ratelimit-Limit: 5000\nX-Ratelimit-Remaining: {remaining}\nX-Ratelimit-Reset: {int(reset)}\n"


class TokenAwareGH:
    """subprocess.run stand-in that answers per token. `limited` maps a token to the reset of its exhausted quota (refused
    with a 403 + headers while the clock is before it); `secondary` names tokens refused with the burst-limit text;
    `quotas` answers `gh api rate_limit` per token as (remaining, reset). Other calls answer {"by": token}."""

    def __init__(self, limited=None, quotas=None, secondary=()):
        self.limited, self.quotas, self.secondary, self.calls = dict(limited or {}), dict(quotas or {}), set(secondary), []

    def __call__(self, cmd, **kw):
        assert cmd[:2] == ["gh", "api"] and not any(TOK_A in a or TOK_B in a for a in cmd), "tokens never travel on the command line"
        token = (kw.get("env") or {}).get("GH_TOKEN")
        self.calls.append((cmd[2], token))
        if cmd[2] == "rate_limit":
            remaining, reset = self.quotas[token]
            return CompletedProcess(cmd, 0, stdout=json.dumps({"resources": {"core": {"limit": 5000, "remaining": remaining, "reset": int(reset)}}}), stderr="")
        if token in self.secondary:
            raise CalledProcessError(1, cmd, output="", stderr="gh: You have exceeded a secondary rate limit. Please wait a few minutes before you try again. (HTTP 403)")
        if token in self.limited and threads._now() < self.limited[token]:
            raise CalledProcessError(1, cmd, output=_headers(self.limited[token]), stderr="gh: API rate limit exceeded for user ID 1 (HTTP 403)")
        return CompletedProcess(cmd, 0, stdout=json.dumps({"by": token}), stderr="")


def test_a_refused_token_is_swapped_for_one_with_quota_within_the_same_call(pool, monkeypatch, caplog):
    gh = TokenAwareGH(limited={TOK_A: RESET})
    monkeypatch.setattr(subprocess, "run", gh)
    with caplog.at_level(logging.WARNING, logger="swesim.sources.threads"):
        assert threads._gh("repos/o/r/pulls/1") == {"by": TOK_B}
    assert gh.calls == [("repos/o/r/pulls/1", TOK_A), ("repos/o/r/pulls/1", TOK_B)]  # refused under A, answered under B, one caller-visible call
    assert threads._gh("repos/o/r/pulls/2") == {"by": TOK_B} and gh.calls[-1] == ("repos/o/r/pulls/2", TOK_B) and len(gh.calls) == 3  # A is remembered as out
    assert pool.select(threads._now()).name == "app:987654321"
    assert any("GH_TOKEN" in r.getMessage() and "app:987654321" in r.getMessage() for r in caplog.records)
    assert not any(TOK_A in r.getMessage() or TOK_B in r.getMessage() for r in caplog.records)


def test_every_token_exhausted_raises_rate_limited_with_the_soonest_reset(pool, monkeypatch, frozen_clock):
    gh = TokenAwareGH(limited={TOK_A: RESET, TOK_B: RESET - 600})
    monkeypatch.setattr(subprocess, "run", gh)
    with pytest.raises(threads.RateLimited) as exc:
        threads._gh("repos/o/r/pulls/1")
    e = exc.value
    assert e.reset_at == RESET - 600 and e.remaining == 0 and e.secondary is False and e.authoritative and e.wait_seconds() == 300
    assert e.kind == "rate-limit" and e.source == "app:987654321" and "every GitHub token" in str(e) and "GH_TOKEN" in str(e)
    assert TOK_A not in str(e) and TOK_B not in str(e)
    assert [t for _, t in gh.calls] == [TOK_A, TOK_B]
    with pytest.raises(threads.RateLimited) as again:
        threads._gh("repos/o/r/pulls/2")
    assert len(gh.calls) == 2 and again.value.reset_at == RESET - 600  # both known to be out: no gh call, the same horizon
    frozen_clock["now"] = RESET - 600
    assert threads._gh("repos/o/r/pulls/3") == {"by": TOK_B} and gh.calls[-1] == ("repos/o/r/pulls/3", TOK_B)  # B's window reset: used again


def test_a_secondary_limit_on_one_token_rotates_as_well(pool, monkeypatch):
    gh = TokenAwareGH(secondary={TOK_A}, quotas={TOK_A: (4990, RESET), TOK_B: (5000, RESET)})
    monkeypatch.setattr(subprocess, "run", gh)
    assert threads._gh("repos/o/r/pulls?state=closed&page=1") == {"by": TOK_B}
    assert [(p, t) for p, t in gh.calls] == [("repos/o/r/pulls?state=closed&page=1", TOK_A), ("rate_limit", TOK_A), ("repos/o/r/pulls?state=closed&page=1", TOK_B)]
    state = pool.summary(threads._now())[0]
    assert state["name"] == "GH_TOKEN" and state["secondary"] is True and state["blocked_until"] == NOW + threads.SECONDARY_LIMIT_WAIT and state["remaining"] == 4990


def test_rate_limit_status_reads_every_token_and_reports_the_best(pool, monkeypatch, frozen_clock):
    gh = TokenAwareGH(quotas={TOK_A: (4000, RESET), TOK_B: (4500, RESET - 100)})
    monkeypatch.setattr(subprocess, "run", gh)
    assert threads.rate_limit_status() == (4500, 5000, RESET - 100)
    assert gh.calls == [("rate_limit", TOK_A), ("rate_limit", TOK_B)]
    frozen_clock["now"] += 29
    assert threads.rate_limit_status() == (4500, 5000, RESET - 100) and len(gh.calls) == 2  # cached per token
    assert threads.rate_limit_status(source="GH_TOKEN") == (4000, 5000, RESET) and len(gh.calls) == 2
    gh.quotas[TOK_A] = (0, RESET)
    gh.quotas[TOK_B] = (0, RESET - 100)
    frozen_clock["now"] += 2
    assert threads.rate_limit_status() == (0, 5000, RESET - 100) and len(gh.calls) == 4  # both empty: the soonest reset
    assert threads.rate_limit_status(source="GH_TOKEN", fresh=True) == (0, 5000, RESET) and len(gh.calls) == 5
    with pytest.raises(threads.RateLimited) as exc:
        threads._gh("repos/o/r/pulls/1")  # nothing to try: the readings already say every bucket is empty
    assert exc.value.reset_at == RESET - 100 and len(gh.calls) == 5
    assert threads.pool() is pool and threads.pool().names == ("GH_TOKEN", "app:987654321")


def test_a_token_that_cannot_be_produced_is_set_aside_and_the_others_carry_on(monkeypatch, caplog):
    class Broken:
        name = "app:9"

        def token(self):
            raise GA.TokenError("network", "installation token request failed: ConnectError")
    monkeypatch.setattr(threads, "_POOL", GA.TokenPool([Broken(), GA.StaticSource("GH_TOKEN", TOK_A)]))
    gh = TokenAwareGH()
    monkeypatch.setattr(subprocess, "run", gh)
    with caplog.at_level(logging.WARNING, logger="swesim.sources.threads"):
        assert threads._gh("repos/o/r/pulls/1") == {"by": TOK_A} and gh.calls == [("repos/o/r/pulls/1", TOK_A)]
    assert any("app:9" in r.getMessage() and "network" in r.getMessage() for r in caplog.records)
    assert threads.pool().summary(threads._now())[0]["blocked_until"] == NOW + threads.TOKEN_ERROR_SET_ASIDE_SEC
    monkeypatch.setattr(threads, "_POOL", GA.TokenPool([Broken()]))
    with pytest.raises(threads.DiscoveryUnavailable) as exc:
        threads._gh("repos/o/r/pulls/1")
    assert exc.value.kind == "network" and exc.value.error_class == "TokenError" and not isinstance(exc.value, threads.RateLimited)
    assert threads.unavailable_kind(exc.value) == "network"


def test_gh_s_own_credential_is_used_when_no_token_is_configured(monkeypatch):
    monkeypatch.setattr(threads, "_POOL", GA.TokenPool([GA.AmbientSource()]))
    seen = []

    def run(cmd, **kw):
        seen.append(kw.get("env"))
        return CompletedProcess(cmd, 0, stdout="{}", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    assert threads._gh("repos/o/r/pulls/1") == {} and seen == [None]  # no env override: gh's keyring


def test_the_pool_is_built_from_settings_once_per_process(monkeypatch, tmp_path):
    from swesim import settings as S
    monkeypatch.setattr(threads, "_POOL", None)
    env_file = tmp_path / ".env"
    env_file.write_text("GH_TOKEN=ghp_one\nGH_TOKEN_2=ghp_two\n")
    monkeypatch.setenv("SWESIM_ENV_FILE", str(env_file))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    S.reset_settings()
    try:
        built = threads.pool()
        assert built.names == ("GH_TOKEN", "GH_TOKEN_2") and threads.pool() is built
    finally:
        S.reset_settings()
