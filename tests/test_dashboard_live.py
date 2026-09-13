"""Historical measurements, live chart payloads and bounded queue-poller observations; no network."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from swesim.dashboard import health as cached_health, trends
from swesim.settings import Settings
from swesim.workflows import health
from test_dashboard import reader_of, site as site

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


def cloud_settings(tmp_path):
    return Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "absent"),
                              "TEMPORAL_ADDRESS": "us-central1.gcp.api.temporal.io:7233", "TEMPORAL_NAMESPACE": "test.ns"})


def poller(at):
    stamp = Timestamp()
    stamp.FromDatetime(at)
    return SimpleNamespace(last_access_time=stamp, identity="must-not-be-returned")


def test_health_observes_every_queue_and_requires_recent_timestamps(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "secret", lambda *args: "private-key")
    requests = []

    async def describe(request):
        requests.append(request)
        if request.task_queue.name == "swesim-trials":
            return SimpleNamespace(pollers=[poller(NOW - timedelta(minutes=10))])
        return SimpleNamespace(pollers=[poller(NOW - timedelta(seconds=12)), SimpleNamespace()])

    client = SimpleNamespace(workflow_service=SimpleNamespace(describe_task_queue=describe))
    result = asyncio.run(health.check_temporal(cloud_settings(tmp_path), client, now=NOW))
    assert len(requests) == 5 and {r.namespace for r in requests} == {"test.ns"}
    assert not result["passed"] and result["state"] == "degraded"
    assert result["pollers"]["swesim-trials/activity"] == 0
    assert result["pollers"]["swesim-authoring/workflow"] == 1
    assert result["resource_status"] == "not_measured"
    assert "private-key" not in json.dumps(result) and "must-not-be-returned" not in json.dumps(result)
    stale = next(r for r in result["queues"] if r["queue"] == "swesim-trials")
    assert stale["state"] == "no_recent_pollers" and stale["last_polled_at"]


def test_health_authentication_timeout_and_provider_errors_are_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "secret", lambda *args: "private-key")
    seen = {}

    async def connect(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("private-key https://secret.example")

    monkeypatch.setattr(health.Client, "connect", connect)
    result = asyncio.run(health.check_temporal(cloud_settings(tmp_path), now=NOW))
    assert result["state"] == "unavailable" and result["error_class"] == "RuntimeError"
    assert seen["api_key"] == "private-key" and seen["tls"] is True and seen["rpc_metadata"] == {"temporal-namespace": "test.ns"}
    assert "private-key" not in json.dumps(result) and "secret.example" not in json.dumps(result)

    async def hangs(request):
        await asyncio.sleep(60)

    monkeypatch.setattr(health, "RPC_TIMEOUT_SECONDS", 0.01)
    client = SimpleNamespace(workflow_service=SimpleNamespace(describe_task_queue=hangs))
    result = asyncio.run(health.check_temporal(cloud_settings(tmp_path), client, now=NOW))
    assert result["state"] == "unavailable"
    assert all(r["recent_pollers"] is None and r["error_class"] == "TimeoutError" for r in result["queues"])


def test_health_cache_coalesces_requests_and_preserves_stale_observation(tmp_path, monkeypatch):
    cached_health.reset()
    clock = [NOW]
    calls = []

    async def check(settings):
        calls.append(1)
        await asyncio.sleep(0)
        return {"state": "ok" if len(calls) == 1 else "unavailable", "passed": len(calls) == 1,
                "checked_at": clock[0].isoformat(), "queues": [{"queue": "swesim-trials", "kind": "activity", "recent_pollers": 2}],
                "pollers": {"swesim-trials/activity": 2}, "error_class": None if len(calls) == 1 else "TimeoutError"}

    monkeypatch.setattr(cached_health, "check_temporal", check)
    settings = cloud_settings(tmp_path)

    async def exercise():
        first, second = await asyncio.gather(cached_health.get_health(settings, now=clock[0]), cached_health.get_health(settings, now=clock[0]))
        assert len(calls) == 1 and first["state"] == second["state"] == "ok"
        hit = await cached_health.get_health(settings, now=clock[0] + timedelta(seconds=3))
        assert hit["cached"] and hit["age_seconds"] == 3
        clock[0] += timedelta(seconds=cached_health.CACHE_TTL_SECONDS + 1)
        failed = await cached_health.get_health(settings, now=clock[0])
        assert failed["stale"] and failed["state"] == "stale" and failed["last_success_at"] == NOW.isoformat()
        assert failed["last_success"]["pollers"]["swesim-trials/activity"] == 2

    asyncio.run(exercise())
    cached_health.reset()


def test_historical_metrics_preserve_denominators_unknowns_and_chronology(site):
    base = reader_of(site).trial(site["done"])
    records = [base.model_copy(update={"trial_id": "1" * 24, "finished_at": NOW, "success": True, "questions_asked": 2, "confirmations": None, "cost_usd": Decimal("1.5")}),
               base.model_copy(update={"trial_id": "2" * 24, "scenario_id": "second-scenario", "finished_at": NOW + timedelta(days=1), "success": False,
                                       "questions_asked": 0, "confirmations": 1, "cost_usd": None}),
               base.model_copy(update={"trial_id": "3" * 24, "finished_at": None, "questions_asked": None})]
    data = trends.summarize(records, [], {"purpose": "calibration"})
    points = data["history"]
    assert [p["day"] for p in points] == ["2026-09-13", "2026-09-14"]
    assert points[-1]["n"] == 2 and points[-1]["scenarios"] == 2 and points[-1]["pass_rate"] == 0.5
    assert points[-1]["questions_per_trial"] == 1 and points[-1]["confirmations_per_trial"] == 1 and points[-1]["confirmations_n"] == 1
    assert points[0]["cost_per_success"] == 1.5 and points[-1]["cost_per_success"] is None
    assert data["undated_trials"] == 1
    final = [p for p in data["interaction"] if p["day"] == "2026-09-14"]
    assert {(p["cohort"], p["n"], p["pass_rate"]) for p in final} == {("asked", 1, 1.0), ("did-not-ask", 1, 0.0)}
    assert all("purpose=calibration" in p["evidence_url"] for p in points)
    missing_success = base.model_copy(update={"finished_at": NOW + timedelta(days=2), "success": None})
    unknown = trends.summarize([*records, missing_success], [], {})["history"][-1]
    assert unknown["n"] == 3 and unknown["success_known_n"] == 2 and unknown["pass_rate"] is None


def test_trend_routes_use_gate_filters_and_serve_table_twins(site):
    sdir = site["ws"].scenario_dir(site["spec"].id)
    (sdir / "difficulty.json").write_text(json.dumps({"tier": "informative"}))
    (sdir / "audit.json").write_text(json.dumps({"coherent": True, "fair_tests": True}))
    response = site["client"].get("/api/trends?purpose=calibration&condition=interactive")
    assert response.status_code == 200
    data = response.json()
    assert data["history"] and all(r["condition"] == "interactive" for r in data["history"])
    assert data["charts"] and all("<table" in html and "<svg" in html for html in data["charts"].values())
    assert all("redacted_terms" not in html and "withheld_sentences" not in html for html in data["charts"].values())
    assert site["client"].get("/trends?purpose=calibration").status_code == 200
    evidence = site["client"].get(data["history"][-1]["evidence_url"])
    assert evidence.status_code == 200 and site["done"][:12] in evidence.text
    assert not site["client"].get("/api/trends?source=github-repo").json()["history"]


def test_discovery_only_sources_are_filterable_without_fake_candidates(site):
    key = "github-repo--only__rejects--feature"
    path = site["ws"].root / "candidates" / f"{key}.rejects.json"
    path.write_text(json.dumps({"thin-thread": 7}))
    page = site["client"].get("/candidates")
    assert f"<option value='{key}'" in page.text
    filtered = site["client"].get(f"/api/candidates?source_key={key}").json()
    assert filtered["items"] == [] and filtered["total"] == 0
    assert "discovery:thin-thread" in filtered["charts"]["rejects"]
    assert site["client"].get("/db/discoveries").status_code == 200


def test_health_page_and_api_are_independent_of_index_availability(site, monkeypatch):
    from swesim.dashboard import app as app_module
    cached_health.reset()
    # The default local fixture intentionally has no Temporal Cloud connection.
    result = site["client"].get("/api/health")
    assert result.status_code == 200 and result.json()["state"] == "unavailable"
    assert result.json()["resource_status"] == "not_measured" and result.json()["checked_at"]
    assert result.headers["cache-control"] == "no-store"
    monkeypatch.setattr(app_module.access, "reader_for", lambda *args: pytest.fail("health page queried unavailable index"))
    assert site["client"].get("/health").status_code == 200
    cached_health.reset()
