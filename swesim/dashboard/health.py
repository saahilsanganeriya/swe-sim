"""Short-lived queue-health cache; coalesce concurrent checks and retain dated stale evidence.

Cache entries contain observations only. Refresh failures preserve the last complete
observation separately; they never present its counts as current queue activity.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime

from swesim.settings import Settings
from swesim.workflows.health import check_temporal

CACHE_TTL_SECONDS = 15
_cache: dict[tuple, dict] = {}
_inflight: dict[tuple, asyncio.Task] = {}


def reset() -> None:
    _cache.clear()
    _inflight.clear()


async def get_health(settings: Settings, *, now: datetime | None = None) -> dict:
    """Observe at most once per 15 seconds per connection; mark retained failed-refresh data stale."""
    current = now or datetime.now(UTC)
    key = (settings.temporal_address, settings.temporal_namespace, str(settings.env_file))
    previous = _cache.get(key)
    cached = previous is not None and (current - previous["at"]).total_seconds() < CACHE_TTL_SECONDS
    if not cached:
        task_key = (*key, id(asyncio.get_running_loop()))
        task = _inflight.get(task_key)
        if task is None:
            task = asyncio.create_task(check_temporal(settings))
            _inflight[task_key] = task
        try:
            observation = await asyncio.shield(task)
        finally:
            if task.done():
                _inflight.pop(task_key, None)
        complete = observation["state"] in ("ok", "degraded") and all(not q.get("error_class") for q in observation["queues"])
        last_success = observation if complete else (previous or {}).get("last_success")
        previous = {"at": current if now else datetime.now(UTC), "observation": observation, "last_success": last_success}
        if len(_cache) >= 16 and key not in _cache:
            _cache.pop(next(iter(_cache)))
        _cache[key] = previous
    result = deepcopy(previous["observation"])
    result.update(cached=cached, age_seconds=max(0, round((current - previous["at"]).total_seconds())), cache_ttl_seconds=CACHE_TTL_SECONDS,
                  last_success_at=previous["last_success"]["checked_at"] if previous["last_success"] else None, stale=False)
    if result["state"] == "unavailable" and previous["last_success"]:
        result.update(state="stale", stale=True, last_success=deepcopy(previous["last_success"]))
    return result
