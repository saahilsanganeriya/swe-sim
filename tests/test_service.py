"""FastAPI stakeholder service: durable snapshots, worker/session auth, one lock for exchange and freeze."""
import asyncio, hashlib, json, pathlib, socket, subprocess, sys, threading, time

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


def test_oversized_multibyte_diff_is_rejected_not_a_service_failure(svc, monkeypatch):
    """Important fix: a >200000-byte but <200000-char diff is the agent's input error (422), never service_failure."""
    c = TestClient(service.app); w = {"X-Worker-Secret": "s3"}
    tok = c.post("/sessions", headers=w, json=body()).json()["token"]; h = {"Authorization": f"Bearer {tok}"}
    big = "é" * 150_000; assert len(big) < 200_000 < len(big.encode())
    calls = []
    monkeypatch.setattr(service, "call_llm", lambda *a: calls.append(1) or {"reply": "x", "disclosed_fact_ids": []})
    r = c.post("/review", headers=h, json={"request_id": "v1", "diff": big, "files": {}})
    assert r.status_code == 422 and calls == []
    t = c.get("/transcript/t1", headers=w).json()
    assert t["service_failure"] is None and t["pending"] is None and t["patience_end"] == 2 and len(t["events"]) == 1
    d = json.loads((svc / "t1.json").read_text()); assert "failure" not in d and d["pending"] is None
    # Tool-level rejections inside Session.review are 4xx too (no failure record), e.g. reviews disabled by a zero tool budget.
    cfg0 = {**CFG, "max_tool_calls_per_activation": 0}
    tok0 = c.post("/sessions", headers=w, json=body(trial_id="t0", stakeholder_config=cfg0)).json()["token"]
    r0 = c.post("/review", headers={"Authorization": f"Bearer {tok0}"}, json={"request_id": "v1", "diff": "diff --git a/a b/a\n", "files": {}})
    assert r0.status_code == 422 and "unavailable" in r0.json()["detail"] and c.get("/transcript/t0", headers=w).json()["service_failure"] is None
    # Service errors keep the 503 + failure path.
    monkeypatch.setattr(service, "call_llm", lambda *a: {"reply": "", "disclosed_fact_ids": []})
    assert c.post("/ask", headers=h, json={"request_id": "2", "question": "a"}).status_code == 503
    assert c.get("/transcript/t1", headers=w).json()["service_failure"]["kind"] == "ValueError"


def test_frozen_and_cached_requests_do_not_write_pending(svc):
    c = TestClient(service.app); w = {"X-Worker-Secret": "s3"}
    tok = c.post("/sessions", headers=w, json=body()).json()["token"]; h = {"Authorization": f"Bearer {tok}"}
    assert c.post("/ask", headers=h, json={"request_id": "1", "question": "a"}).status_code == 200
    before = (svc / "t1.json").read_bytes(); st = (svc / "t1.json").stat()
    assert c.post("/ask", headers=h, json={"request_id": "1", "question": "a"}).status_code == 200      # cached
    assert c.post("/ask", headers=h, json={"request_id": "1", "question": "b"}).status_code == 409      # conflict
    assert (svc / "t1.json").read_bytes() == before and (svc / "t1.json").stat().st_ino == st.st_ino
    c.post("/freeze", headers=h); frozen = (svc / "t1.json").read_bytes()
    assert c.post("/ask", headers=h, json={"request_id": "2", "question": "x"}).status_code == 410
    assert c.post("/review", headers=h, json={"request_id": "3", "diff": "d", "files": {}}).status_code == 410
    assert (svc / "t1.json").read_bytes() == frozen                                                     # no pending marker written


SHIMS = pathlib.Path(service.__file__).parent / "shims"


def _shim(name, *args, env=None, cwd=None):
    base_env = {"PATH": "/usr/bin:/bin"}; base_env.update(env or {})
    return subprocess.run([sys.executable, "-I", str(SHIMS / name), *args], env=base_env, cwd=cwd, capture_output=True, text=True, timeout=60)


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port


def test_shim_without_session_exits_quietly():
    p = _shim("ask-user", "hi"); assert p.returncode == 0 and p.stdout.strip() == "No stakeholder is available in this task."
    p = _shim("request-review", env={"SWESIM_STAKEHOLDER_URL": "http://127.0.0.1:1"}); assert p.returncode == 0 and "No stakeholder" in p.stdout


def test_shim_transport_failure_is_one_line_not_a_traceback():
    env = {"SWESIM_STAKEHOLDER_URL": f"http://127.0.0.1:{_free_port()}", "SWESIM_SESSION_TOKEN": "t1.x", "SWESIM_TRIAL_ID": "t1"}
    p = _shim("ask-user", "which", "exception?", env=env)
    assert p.returncode == 1 and p.stderr.strip().startswith("Stakeholder transport failed:") and "Traceback" not in p.stderr and p.stdout == ""
    p = _shim("ask-user", env=env); assert p.returncode == 1 and p.stderr.strip().startswith("usage:")


@pytest.mark.skipif(not pathlib.Path("/usr/bin/git").exists(), reason="shim uses /usr/bin/git")
def test_review_shim_guards_repo_dir_and_git(tmp_path):
    env = {"SWESIM_STAKEHOLDER_URL": f"http://127.0.0.1:{_free_port()}", "SWESIM_SESSION_TOKEN": "t1.x", "SWESIM_TRIAL_ID": "t1"}
    p = _shim("request-review", env=env)
    assert p.returncode == 1 and "SWESIM_REPO_DIR" in p.stderr and "Traceback" not in p.stderr
    p = _shim("request-review", env={**env, "SWESIM_REPO_DIR": str(tmp_path / "missing")})
    assert p.returncode == 1 and "SWESIM_REPO_DIR" in p.stderr and "Traceback" not in p.stderr
    p = _shim("request-review", env={**env, "SWESIM_REPO_DIR": str(tmp_path)})                # a directory that is not a git repo
    assert p.returncode == 1 and p.stderr.strip().startswith("request-review: git failed") and "Traceback" not in p.stderr


@pytest.mark.skipif(not pathlib.Path("/usr/bin/git").exists(), reason="shim uses /usr/bin/git")
def test_shims_end_to_end_against_local_service(svc, monkeypatch, tmp_path):
    """The real shim files against the service on 127.0.0.1 with a fake model: reply, review, 4xx detail, 410 after freeze."""
    import uvicorn
    monkeypatch.setattr(service, "call_llm", lambda *a: {"reply": "Raise ValueError naming the field.", "disclosed_fact_ids": ["f01"]})
    port = _free_port(); server = uvicorn.Server(uvicorn.Config(service.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True); th.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try: httpx.get(base + "/health", timeout=1); break
        except Exception: time.sleep(0.05)
    try:
        cfg = {**CFG, "patience_profile": {"start": 4, "question_cost": 1, "review_cost": 2, "terse_below": 1, "refuse_at": 0}}
        tok = httpx.post(base + "/sessions", headers={"X-Worker-Secret": "s3"}, json=body(stakeholder_config=cfg)).json()["token"]
        repo = tmp_path / "repo"; repo.mkdir()
        def git(*a): subprocess.run(["/usr/bin/git", "-c", "user.email=a@b", "-c", "user.name=a", *a], cwd=repo, check=True, capture_output=True)
        git("init", "-q"); (repo / "a.py").write_text("raise KeyError\n"); git("add", "."); git("commit", "-q", "-m", "base")
        env = {"SWESIM_STAKEHOLDER_URL": base, "SWESIM_SESSION_TOKEN": tok, "SWESIM_TRIAL_ID": "t1", "SWESIM_REPO_DIR": str(repo)}
        p = _shim("ask-user", "Which", "exception?", env=env)
        assert p.returncode == 0 and p.stdout.splitlines() == ["Raise ValueError naming the field.", "[availability remaining: 3]"]
        p = _shim("request-review", env=env); assert p.returncode == 1 and "No changes to review" in p.stderr
        (repo / "a.py").write_text("raise ValueError('f')\n"); (repo / "new.py").write_text("x = 1\n"); (repo / "blob.bin").write_bytes(b"\0\1")
        p = _shim("request-review", env=env); assert p.returncode == 0 and p.stdout.splitlines()[1] == "[availability remaining: 1]"
        t = httpx.get(base + "/transcript/t1", headers={"X-Worker-Secret": "s3"}).json()
        rev = [e for e in t["events"] if e["kind"] == "message" and e["role"] == "assistant" and e["exchange"] == "review"][0]
        tools = [c["tool"] for c in rev["tool_calls"]]
        assert tools[0] == "read_diff" and set(tools[1:]) == {"read_changed_file"} and len(tools) <= 4
        assert {c["path"] for c in rev["tool_calls"][1:]} == {"a.py", "new.py"}                  # binary blob.bin excluded by the shim
        p = _shim("ask-user", "more?", env=env); assert p.returncode == 0 and "[availability remaining: 0]" in p.stdout
        httpx.post(base + "/freeze", headers={"Authorization": "Bearer " + tok}, json={})
        p = _shim("ask-user", "still there?", env=env); assert p.returncode == 0 and p.stdout.strip() == "Stakeholder session closed."
        p = _shim("ask-user", "x", env={**env, "SWESIM_SESSION_TOKEN": "t1.bad"})
        assert p.returncode == 1 and p.stderr.strip().startswith("Stakeholder rejected the request (HTTP 401)") and "Traceback" not in p.stderr
    finally:
        server.should_exit = True; th.join(timeout=10)


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
