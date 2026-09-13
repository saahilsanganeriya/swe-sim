"""Suite-wide fixtures. The gh wrapper paces real calls `GH_PACING_SEC` apart and takes each call's token from a
process-wide `TokenPool` whose quota view is cached for 30 s per token; offline tests drive it with fakes, get a fresh
one-token pool (so no test sees another's quota or the operator's env file), must not sleep, and must never reach the
real `gh` binary."""
import subprocess

import pytest

from swesim.sources import github_auth, threads

_REAL_RUN = subprocess.run
FAKE_TOKEN = "fake-test-token"


@pytest.fixture(autouse=True)
def gh_wrapper_offline(monkeypatch):
    monkeypatch.setattr(threads, "GH_PACING_SEC", 0.0)
    monkeypatch.setattr(threads, "_POOL", github_auth.TokenPool([github_auth.StaticSource("GH_TOKEN", FAKE_TOKEN)]))
    original = threads._run_gh

    def guarded(cmd, timeout, token=None):
        if subprocess.run is _REAL_RUN:
            raise AssertionError(f"offline test reached a real gh call: {cmd[:3]}; fake subprocess.run or the caller")
        return original(cmd, timeout, token)
    monkeypatch.setattr(threads, "_run_gh", guarded)
