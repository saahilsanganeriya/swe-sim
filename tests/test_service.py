"""FastAPI stakeholder service: durable snapshots, worker/session auth, one lock for exchange and freeze."""
import asyncio, hashlib, json, time

import httpx, pytest
from fastapi.testclient import TestClient

from swesim.stakeholder import service

VIEW = {"brief": "b", "facts": [{"id": "f01", "text": "x", "discovery": "on-request", "kind": "interface"}], "requirements": [],
        "persona": {"summary": "m", "tone": "t", "disclosure_style": "d", "role": "maintainer"}, "checks": []}
CFG = {"model_id": "openai/gpt-5.5", "prompt_version": "v1", "temperature": 0.0,
       "patience_profile": {"start": 2, "question_cost": 1, "review_cost": 2, "terse_below": 1, "refuse_at": 0},
       "review_mode": "diff-only", "max_tool_calls_per_activation": 4}


def body(**over):
    return {"trial_id": "t1", "spec_hash": "h", "condition": "interactive", "run_config_hash": "r", "visible_view": VIEW,
            "stakeholder_config": CFG, "image": "ubuntu:24.04", **over}


@pytest.fixture
def svc(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "STORE", tmp_path)
    monkeypatch.setattr(service, "LOCK", asyncio.Lock())      # an asyncio.Lock binds to the first loop that contends it
    monkeypatch.setenv("SWESIM_WORKER_SECRET", "s3")
    monkeypatch.setattr(service, "call_llm", lambda system, messages, schema, cfg: {"reply": "ValueError.", "disclosed_fact_ids": ["f01"]})
    return tmp_path


def test_session_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "STORE", tmp_path)
    monkeypatch.setenv("SWESIM_WORKER_SECRET", "s3")
    monkeypatch.setattr(service, "call_llm", lambda system, messages, schema, cfg: {"reply": "ValueError.", "disclosed_fact_ids": ["f01"]})
    c = TestClient(service.app)
    r = c.post("/sessions", headers={"X-Worker-Secret": "s3"}, json={"trial_id": "t1", "spec_hash": "h", "condition": "interactive", "run_config_hash": "r",
        "visible_view": {"brief": "b", "facts": [{"id": "f01", "text": "x", "discovery": "on-request", "kind": "interface"}], "requirements": [], "persona": {"summary": "m", "tone": "t", "disclosure_style": "d", "role": "maintainer"}, "checks": []},
        "stakeholder_config": {"model_id": "openai/gpt-5.5", "prompt_version": "v1", "temperature": 0.0, "patience_profile": {"start": 2, "question_cost": 1, "review_cost": 2, "terse_below": 1, "refuse_at": 0}, "review_mode": "diff-only", "max_tool_calls_per_activation": 4}, "image": "ubuntu:24.04"})
    tok = r.json()["token"]; h = {"Authorization": f"Bearer {tok}"}
    a = c.post("/ask", headers=h, json={"request_id": "1", "question": "which exception?"}).json(); assert a["reply"] == "ValueError." and a["patience_remaining"] == 1
    f = c.post("/freeze", headers=h).json(); assert f["frozen"] and len(f["transcript_hash"]) == 64
    assert c.post("/ask", headers=h, json={"request_id": "2", "question": "x"}).status_code == 410
    # Service uses persisted snapshots, not globals holding session instances.
    c2=TestClient(service.app)
    assert c2.post("/freeze",headers=h).json()==f
    assert c2.post("/sessions",headers={"X-Worker-Secret":"wrong"},json={}).status_code in (401,422)

    t = c.get("/transcript/t1", headers={"X-Worker-Secret": "s3"}).json(); assert t["transcript_hash"] == f["transcript_hash"] and len(t["events"]) >= 2


def test_public_reply_hides_worker_only_annotations(svc):
    c = TestClient(service.app)
    tok = c.post("/sessions", headers={"X-Worker-Secret": "s3"}, json=body()).json()["token"]; h = {"Authorization": f"Bearer {tok}"}
    a = c.post("/ask", headers=h, json={"request_id": "1", "question": "which exception?"}).json()
    assert set(a) == {"reply", "patience_remaining", "tier"} and a["tier"] == "full"
    assert c.get("/health").json()["ok"] is True


def test_review_returns_sha_and_costs_review(svc, monkeypatch):
    c = TestClient(service.app)
    tok = c.post("/sessions", headers={"X-Worker-Secret": "s3"}, json=body()).json()["token"]; h = {"Authorization": f"Bearer {tok}"}
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    r = c.post("/review", headers=h, json={"request_id": "v1", "diff": diff, "files": {"a.py": "y\n"}})
    assert r.status_code == 200
    j = r.json(); assert j["reviewed_diff_sha256"] == hashlib.sha256(diff.encode()).hexdigest() and j["patience_remaining"] == 0 and j["tier"] == "full"
    t = c.get("/transcript/t1", headers={"X-Worker-Secret": "s3"}).json()
    asst = [e for e in t["events"] if e["kind"] == "message" and e["role"] == "assistant"][0]
    assert asst["disclosed_fact_ids"] == ["f01"] and asst["tool_calls"][0]["tool"] == "read_diff" and t["patience_end"] == 0
    # Budget exhausted: fixed refusal, no model call, transcript records it.
    calls = []
    def counting(system, messages, schema, cfg): calls.append(1); return {"reply": "x", "disclosed_fact_ids": []}
    monkeypatch.setattr(service, "call_llm", counting)
    a = c.post("/ask", headers=h, json={"request_id": "2", "question": "more?"}).json()
    assert a["tier"] == "refused" and calls == [] and a["patience_remaining"] == 0
    assert c.post("/review", headers=h, json={"request_id": "v2", "diff": "x" * 200_001, "files": {}}).status_code == 422
    assert c.post("/review", headers=h, json={"request_id": "v3", "diff": "d", "files": {"a": "x" * 600_000}}).status_code == 422


def test_request_id_conflict_and_auth_failures(svc):
    c = TestClient(service.app)
    tok = c.post("/sessions", headers={"X-Worker-Secret": "s3"}, json=body()).json()["token"]; h = {"Authorization": f"Bearer {tok}"}
    assert c.post("/ask", headers=h, json={"request_id": "1", "question": "a"}).status_code == 200
    assert c.post("/ask", headers=h, json={"request_id": "1", "question": "a"}).json()["patience_remaining"] == 1   # cached, no spend
    assert c.post("/ask", headers=h, json={"request_id": "1", "question": "b"}).status_code == 409
    assert c.post("/ask", headers={"Authorization": "Bearer t1.deadbeef"}, json={"request_id": "3", "question": "a"}).status_code == 401
    assert c.post("/ask", headers={"Authorization": "Bearer ../etc"}, json={"request_id": "3", "question": "a"}).status_code == 401
    assert c.post("/ask", json={"request_id": "3", "question": "a"}).status_code == 401
    assert c.post("/ask", headers=h, json={"request_id": "3", "question": "a", "extra": 1}).status_code == 422
    assert c.get("/transcript/t1").status_code == 401
    assert c.get("/transcript/t1", headers={"X-Worker-Secret": "nope"}).status_code == 401
    assert c.get("/transcript/none", headers={"X-Worker-Secret": "s3"}).status_code == 404
    assert c.post("/freeze", headers={"Authorization": "Bearer t1.deadbeef"}).status_code == 401


def test_session_binding_is_idempotent_and_conflicts_on_change(svc):
    c = TestClient(service.app); w = {"X-Worker-Secret": "s3"}
    t1 = c.post("/sessions", headers=w, json=body()).json()["token"]
    assert c.post("/sessions", headers=w, json=body()).json()["token"] == t1
    assert c.post("/sessions", headers=w, json=body(run_config_hash="other")).status_code == 409
    assert c.post("/sessions", headers=w, json=body(trial_id="t2", condition="hidden")).status_code == 400
    leaky = dict(VIEW); leaky["facts"] = VIEW["facts"] + [{"id": "f09", "text": "S", "discovery": "author-only", "kind": "interface"}]
    assert c.post("/sessions", headers=w, json=body(trial_id="t3", visible_view=leaky)).status_code == 400
    assert c.post("/sessions", headers=w, json=body(trial_id="t4", visible_view={"facts": []})).status_code == 400
    assert c.post("/sessions", headers=w, json=body(trial_id="Bad/Id")).status_code == 400
    t = c.get("/transcript/t1", headers=w).json()
    first = t["events"][0]; assert first["kind"] == "session" and first["spec_hash"] == "h" and first["run_config_hash"] == "r" and first["condition"] == "interactive"


def test_state_persisted_before_reply_and_failure_recorded(svc, monkeypatch):
    c = TestClient(service.app); w = {"X-Worker-Secret": "s3"}
    tok = c.post("/sessions", headers=w, json=body()).json()["token"]; h = {"Authorization": f"Bearer {tok}"}
    def boom(system, messages, schema, cfg): raise RuntimeError("provider down")
    monkeypatch.setattr(service, "call_llm", boom)
    r = c.post("/ask", headers=h, json={"request_id": "1", "question": "a"}); assert r.status_code == 503
    t = c.get("/transcript/t1", headers=w).json()
    assert t["service_failure"]["request_id"] == "1" and t["service_failure"]["kind"] == "RuntimeError" and t["patience_end"] == 2
    # A transient failure does not brick the session: a fresh request id works once the model is back.
    monkeypatch.setattr(service, "call_llm", lambda *a: {"reply": "ok", "disclosed_fact_ids": []})
    assert c.post("/ask", headers=h, json={"request_id": "2", "question": "a"}).status_code == 200
    # The persisted snapshot on disk is the source of truth and contains the reply before the HTTP response was sent.
    d = json.loads((svc / "t1.json").read_text())
    assert d["pending"] is None and d["session"]["patience_end"] == 1 and d["session"]["cache"]["2"]["reply"]["reply"] == "ok"
    # An interrupted request (pending marker left by a crash) is surfaced, not silently retried.
    d["pending"] = {"request_id": "9", "kind": "ask"}; (svc / "t1.json").write_text(json.dumps(d))
    assert c.post("/ask", headers=h, json={"request_id": "3", "question": "a"}).status_code == 503
    t = c.get("/transcript/t1", headers=w).json(); assert t["pending"] == {"request_id": "9", "kind": "ask"}
    assert c.post("/freeze", headers=h).json()["frozen"] is True      # freeze still works


@pytest.mark.asyncio
async def test_exchange_and_freeze_share_one_lock(svc, monkeypatch):
    def slow(system, messages, schema, cfg): time.sleep(0.15); return {"reply": "slow answer", "disclosed_fact_ids": []}
    monkeypatch.setattr(service, "call_llm", slow)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app), base_url="http://svc") as c:
        tok = (await c.post("/sessions", headers={"X-Worker-Secret": "s3"}, json=body())).json()["token"]; h = {"Authorization": f"Bearer {tok}"}
        ask = asyncio.create_task(c.post("/ask", headers=h, json={"request_id": "1", "question": "a"}))
        await asyncio.sleep(0.03)
        frz = asyncio.create_task(c.post("/freeze", headers=h))
        a, f = await asyncio.gather(ask, frz)
        assert f.status_code == 200 and a.status_code in (200, 410)
        t = (await c.get("/transcript/t1", headers={"X-Worker-Secret": "s3"})).json()
        assert t["frozen"] and t["transcript_hash"] == f.json()["transcript_hash"]
        assert t["transcript_hash"] == hashlib.sha256(json.dumps(t["events"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        replied = any(e.get("request_id") == "1" for e in t["events"])
        assert replied == (a.status_code == 200)          # a reply after freeze can never exist
        assert (await c.post("/freeze", headers=h)).json() == f.json()
