"""Suite-wide fixtures. The gh wrapper paces real calls `GH_PACING_SEC` apart and caches the quota for 30 s; offline tests
drive it with fakes, must not sleep, must not see another test's quota, and must never reach the real `gh` binary."""
import subprocess

import pytest

from swesim.sources import threads

_REAL_RUN = subprocess.run


@pytest.fixture(autouse=True)
def gh_wrapper_offline(monkeypatch):
    monkeypatch.setattr(threads, "GH_PACING_SEC", 0.0)
    monkeypatch.setattr(threads, "_status_cache", None)
    original = threads._run_gh

    def guarded(cmd, timeout):
        if subprocess.run is _REAL_RUN:
            raise AssertionError(f"offline test reached a real gh call: {cmd[:3]}; fake subprocess.run or the caller")
        return original(cmd, timeout)
    monkeypatch.setattr(threads, "_run_gh", guarded)
