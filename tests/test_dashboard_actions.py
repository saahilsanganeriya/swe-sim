"""Identity (IAP), the strict Origin check, two-step confirmation tokens, the audit row and every operator action's flow
through the console (create, run, cancel, review, label, retry, rebuild) with a fake Temporal client. Offline."""
from __future__ import annotations

import json

import pytest

from swesim.dashboard import access, actions
from swesim.dashboard import rows as R
from swesim.dashboard.identity import ANONYMOUS, Identity, identity_from_headers
from swesim.settings import Settings, reset_settings

try:
    from test_dashboard import EVAL_EMAIL, HAIKU, site  # noqa: F401 - fixture reuse
except ImportError:  # pragma: no cover
    from tests.test_dashboard import EVAL_EMAIL, HAIKU, site  # noqa: F401


def settings_with(**env) -> Settings:
    base = {"SWESIM_WORKSPACE": "/tmp/ws", "SWESIM_REGISTRY": "/tmp/r.json", "SWESIM_ENV_FILE": "/nonexistent.env", "SWESIM_EVALUATORS": f"{EVAL_EMAIL}, Other@Example.com"}
    return Settings.from_env({**base, **env})


# ---------------------------------------------------------------- identity

def test_identity_is_anonymous_without_iap_headers_and_ignores_query_flags():
    s = settings_with(SWESIM_IAP_AUDIENCE="/projects/1/x")
    assert identity_from_headers({}, s) == ANONYMOUS and identity_from_headers({"x-goog-authenticated-user-email": "accounts.google.com:a@b.c"}, s).anonymous
    assert ANONYMOUS.actor == "anonymous" and ANONYMOUS.evaluator is False


def test_identity_requires_a_verified_jwt_whose_email_matches_and_an_allow_listed_email():
    s = settings_with(SWESIM_IAP_AUDIENCE="/projects/1/x")
    hdr = {"X-Goog-Authenticated-User-Email": f"accounts.google.com:{EVAL_EMAIL.upper()}", "X-Goog-IAP-JWT-Assertion": "jwt"}
    seen = {}

    def verify(token, audience):
        seen.update(token=token, audience=audience)
        return {"email": EVAL_EMAIL}
    ident = identity_from_headers(hdr, s, verify=verify)
    assert ident == Identity(EVAL_EMAIL, True) and seen == {"token": "jwt", "audience": "/projects/1/x"}
    assert identity_from_headers(hdr, s, verify=lambda t, a: {"email": "other@example.com"}).anonymous  # claim mismatch
    assert identity_from_headers(hdr, s, verify=lambda t, a: {}).reason.startswith("IAP JWT email claim")
    assert identity_from_headers(hdr, s, verify=lambda t, a: (_ for _ in ()).throw(ValueError("bad signature"))).reason == "IAP JWT did not verify: ValueError"
    viewer = {"X-Goog-Authenticated-User-Email": "accounts.google.com:viewer@example.com", "X-Goog-IAP-JWT-Assertion": "jwt"}
    v = identity_from_headers(viewer, s, verify=lambda t, a: {"email": "viewer@example.com"})
    assert v.email == "viewer@example.com" and v.evaluator is False
    other = identity_from_headers({**viewer, "X-Goog-Authenticated-User-Email": "accounts.google.com:other@example.com"}, s, verify=lambda t, a: {"email": "other@example.com"})
    assert other.evaluator is True  # allow-list is case-insensitive
    # no audience configured: headers cannot be trusted (fail closed), and the reason says why
    no_aud = identity_from_headers(hdr, settings_with(), verify=lambda t, a: {"email": EVAL_EMAIL})
    assert no_aud.anonymous and "SWESIM_IAP_AUDIENCE" in no_aud.reason


# ---------------------------------------------------------------- origin

def test_origin_check_compares_scheme_host_and_port():
    s = settings_with()
    ok = actions.check_origin
    ok({"host": "console.example:8080", "origin": "http://console.example:8080"}, s)
    ok({"host": "console.example", "origin": "http://console.example"}, s)  # default ports agree
    ok({"host": "console.example", "origin": "http://console.example:80"}, s)
    ok({}, s)  # no Origin: curl/scripts
    for headers in ({"host": "console.example:8080", "origin": "http://console.example:9"}, {"host": "console.example", "origin": "https://console.example"},
                    {"host": "console.example", "origin": "http://other.example"}, {"host": "console.example", "origin": "null"}, {"host": "console.example", "origin": "not a url"},
                    {"host": "[::1]:8080", "origin": "http://[::2]:8080"}):
        with pytest.raises(actions.OriginError):
            ok(headers, s)
    ok({"host": "[::1]:8080", "origin": "http://[::1]:8080"}, s)
    # behind IAP/Cloud Run the public scheme comes from X-Forwarded-Proto
    ok({"host": "console.example", "x-forwarded-proto": "https", "origin": "https://console.example"}, s)
    with pytest.raises(actions.OriginError):
        ok({"host": "console.example", "x-forwarded-proto": "https", "origin": "http://console.example"}, s)
    # with SWESIM_CONSOLE_URL set, only that origin is accepted regardless of Host
    s2 = settings_with(SWESIM_CONSOLE_URL="https://swesim.example.nip.io")
    ok({"host": "anything", "origin": "https://swesim.example.nip.io"}, s2)
    ok({"host": "anything", "origin": "https://swesim.example.nip.io:443"}, s2)
    for origin in ("http://swesim.example.nip.io", "https://swesim.example.nip.io:8443", "https://evil.example"):
        with pytest.raises(actions.OriginError):
            ok({"host": "swesim.example.nip.io", "origin": origin}, s2)


# ---------------------------------------------------------------- tokens

def test_confirmation_token_binds_action_params_actor_and_expiry(monkeypatch):
    s = settings_with()
    me, you = Identity("me@example.com", False), Identity("you@example.com", False)
    params = {"scenario_ids": ["a"], "k": 2}
    token, exp = actions.issue_token("run", params, me, s, now=1000.0)
    actions.verify_token(token, "run", {"k": 2, "scenario_ids": ["a"]}, me, s, now=1500.0)  # key order does not matter
    with pytest.raises(actions.TokenError, match="different action"):
        actions.verify_token(token, "create", params, me, s, now=1500.0)
    with pytest.raises(actions.TokenError, match="different identity"):
        actions.verify_token(token, "run", params, you, s, now=1500.0)
    with pytest.raises(actions.TokenError, match="changed"):
        actions.verify_token(token, "run", {**params, "k": 3}, me, s, now=1500.0)
    with pytest.raises(actions.TokenError, match="expired"):
        actions.verify_token(token, "run", params, me, s, now=1000.0 + actions.TOKEN_TTL_S + 1)
    body, sig = token.split(".")
    with pytest.raises(actions.TokenError, match="signature"):
        actions.verify_token(f"{body}.{sig[:-2]}xx", "run", params, me, s, now=1500.0)
    for bad in (None, "", "no-dot", "a.b.c"):
        with pytest.raises(actions.TokenError):
            actions.verify_token(bad, "run", params, me, s)
    with pytest.raises(actions.TokenError, match="unknown action"):
        actions.issue_token("nuke", {}, me, s)
    assert exp.timestamp() == 1000.0 + actions.TOKEN_TTL_S
    # a shared worker secret yields a key valid across processes; without it the process key is used and said so
    assert actions.signing_key(s)[1] is False
    monkeypatch.setenv("SWESIM_WORKER_SECRET", "s3cret-shared-key")
    assert actions.signing_key(s) == (b"s3cret-shared-key", True)
    monkeypatch.delenv("SWESIM_WORKER_SECRET")


# ---------------------------------------------------------------- action flows through the app

class _Client:
    """Offline stand-in for swesim.workflows.client (same signatures and return shapes)."""

    def __init__(self, fail=False):
        self.calls, self.fail, self.jobs, self.progress_by_id = [], fail, [], {}

    async def start_create(self, params):
        self.calls.append(("start_create", params))
        if self.fail:
            raise ConnectionRefusedError("temporal 7233 refused")
        return "create-abc"

    async def start_run(self, params):
        self.calls.append(("start_run", params))
        return "run-1"

    async def list_runs(self, kind="all", limit=50):
        return self.jobs

    async def progress(self, wid):
        return self.progress_by_id.get(wid, {"stage": "starting"})

    async def cancel(self, wid):
        self.calls.append(("cancel", wid))

    async def operator_action(self, p, request_id):
        from swesim.workflows import operator_actions as O
        from swesim.paths import atomic_json
        request = {**p, 'request_id': request_id}
        if p['action'] == 'review':
            q = p['params']
            target = await self.approve_candidate(q['workflow_id'], q['scenario_id'], q['spec_hash'], q['decision'], q['note']) if q.get('workflow_id') else None
            prepared = {'request': request, 'signal_target': target, 'decided_at': '2026-09-13T00:00:00+00:00', 'worker_image_digest': None, 'origin': 'laptop'}
            _, directory = O._request(request)
            atomic_json(directory / 'request.json', prepared)
        else:
            prepared = await O.prepare_operator_action_activity(request)
        return {**await O.commit_operator_action_activity(prepared), 'workflow_id': 'operator-' + request_id}

    async def approve_candidate(self, wid, sid, spec_hash, decision="approve", note=""):
        self.calls.append(("approve_candidate", wid, sid, spec_hash, decision, note))
        return f"create:{sid}"


def _install(monkeypatch, client):
    import swesim.workflows.client as real
    for name in ("operator_action", "start_create", "start_run", "list_runs", "progress", "cancel", "approve_candidate", "start_pipeline", "start_matrix", "approve"):
        if hasattr(client, name):
            monkeypatch.setattr(real, name, getattr(client, name), raising=False)
        else:
            monkeypatch.delattr(real, name, raising=False)


def _actions_log(site) -> list[dict]:
    p = site["ws"].root / "evidence" / "console" / "actions.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def run_action(c, action, params, headers=None):
    pv = c.post(f"/api/{action}/preview", json=params, headers=headers or {})
    if pv.status_code != 200:
        return pv, None
    return pv, c.post(f"/api/{action}", json={"token": pv.json()["token"], "params": pv.json()["params"]}, headers=headers or {})


def test_label_flow_preview_confirm_audit_and_rejections(site, monkeypatch):
    _install(monkeypatch, _Client())
    c, tid = site["client"], site["done"]
    pv, res = run_action(c, "label", {"trial_id": tid, "label": "no-question", "note": "asked nothing"})
    assert pv.status_code == 200 and pv.json()["cli"].startswith("uv run swesim workflows action label") and pv.json()["token_scope"].startswith("this console process")
    assert pv.json()["what"].startswith("Record a reviewer label") and pv.json()["expires_at"].endswith("+00:00")
    assert res.status_code == 200 and res.json()["ok"] is True
    assert json.loads((site["ws"].root / "trials" / tid / "label.json").read_text())["label"] == "no-question"
    log = _actions_log(site)
    assert len(log) == 1 and log[0]["action"] == "label" and log[0]["result"] == "accepted" and log[0]["actor"] == "anonymous" and log[0]["target"] == tid and log[0]["confirmed"] is True
    # tampered params, wrong actor, missing token: rejected and audited, nothing written
    tampered = c.post("/api/label", json={"token": pv.json()["token"], "params": {**pv.json()["params"], "label": "unresolved"}})
    assert tampered.status_code == 409 and "changed" in tampered.json()["error"]
    other = c.post("/api/label", json={"token": pv.json()["token"], "params": pv.json()["params"]}, headers=site["eval"])
    assert other.status_code == 403 and "different identity" in other.json()["error"]
    assert c.post("/api/label", json={"params": pv.json()["params"]}).status_code == 403
    assert json.loads((site["ws"].root / "trials" / tid / "label.json").read_text())["label"] == "no-question"
    assert [e["result"] for e in _actions_log(site)] == ["accepted", "rejected", "rejected", "rejected"] and _actions_log(site)[2]["actor"] == EVAL_EMAIL
    # validation errors come back from the preview with the CLI equivalent
    bad = c.post("/api/label/preview", json={"trial_id": tid, "label": "nope"})
    assert bad.status_code == 400 and "label must be one of" in bad.json()["errors"][0]
    assert c.post("/api/nuke/preview", json={}).status_code == 404 and c.post("/api/label/preview", content="[]", headers={"content-type": "application/json"}).status_code == 400


def test_mutations_reject_cross_origin_requests_strictly(site):
    c, tid = site["client"], site["done"]
    body = {"trial_id": tid, "label": "unresolved"}
    assert c.post("/api/label/preview", json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/label/preview", json=body, headers={"Origin": "null"}).status_code == 403
    assert c.post("/api/label/preview", json=body, headers={"Origin": "https://testserver"}).status_code == 403  # scheme differs
    assert c.post("/api/label/preview", json=body, headers={"Origin": "http://testserver:81"}).status_code == 403  # port differs
    assert c.post("/api/label/preview", json=body, headers={"Origin": "http://testserver"}).status_code == 200
    assert c.post("/api/label", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.get("/api/counts", headers={"Origin": "http://evil.example"}).status_code == 200  # reads are not gated by Origin


def test_create_flow_shows_exact_cli_starts_workflow_and_audits(site, monkeypatch):
    client = _Client()
    _install(monkeypatch, client)
    monkeypatch.setenv("SWESIM_STAKEHOLDER_URL", "https://stakeholder.example.test")
    reset_settings(); access.reset()
    c = site["client"]
    form = {"source": "github-repo", "repos": "https://github.com/py-pdf/pypdf\nacme/widget", "since": "2026-06-01", "max_candidates": "3", "pr_kind": "feature",
            "judge_model": "openai/gpt-5.5", "draft_model": "anthropic/claude-sonnet-5", "approval_mode": "hold", "max_parallel": "2", "rebuild": ["1"]}
    pv, res = run_action(c, "create", form, headers=site["eval"])
    cli = pv.json()["cli"]
    assert cli.startswith("uv run swesim create --source https://github.com/py-pdf/pypdf --source https://github.com/acme/widget") and "--pr-kind feature" in cli and "--approval-mode hold" in cli and cli.endswith("--rebuild")
    assert res.status_code == 200 and res.json()["workflow_ids"] == ["create-abc"] and res.json()["via"].startswith("start_create")
    name, params = client.calls[-1]
    assert name == "start_create" and params["stakeholder_url"] == "https://stakeholder.example.test" and params["rebuild"] is True and params["approval_mode"] == "hold"
    assert [s["repo"] for s in params["sources"]] == ["https://github.com/py-pdf/pypdf", "https://github.com/acme/widget"] and params["sources"][0]["pr_kind"] == "feature"
    assert "stakeholder.example.test" not in pv.text and "stakeholder.example.test" not in res.text  # never echoed
    log = _actions_log(site)[-1]
    assert log["action"] == "create" and log["result"] == "accepted" and log["workflow_id"] == "create-abc" and log["actor"] == EVAL_EMAIL and log["params"]["pr_kind"] == "feature"
    # validation failures never reach Temporal
    bad, _ = run_action(c, "create", {"source": "github-repo", "repos": "not a repo!!", "judge_model": "x", "draft_model": "x"})
    assert bad.status_code == 400 and any("GitHub" in e for e in bad.json()["errors"]) and any("differ" in e for e in bad.json()["errors"]) and len(client.calls) == 1
    # unreachable Temporal: 502 with the CLI fallback, audited as failed
    _install(monkeypatch, _Client(fail=True))
    pv, res = run_action(c, "create", form)
    assert res.status_code == 502 and res.json()["cli"].startswith("uv run swesim create") and "refused" in res.json()["error"] and _actions_log(site)[-1]["result"] == "failed"


def test_run_flow_validates_against_the_index_and_supports_screen_purpose(site, monkeypatch):
    client = _Client()
    _install(monkeypatch, client)
    c = site["client"]
    html = c.get("/run").text
    assert site["spec"].id in html and "unlabelled" in html and "Tier A" in html and "openrouter/deepseek/deepseek-v3.2" in html and "screen" in html
    form = {"scenario_ids": [site["spec"].id], "models": ["openrouter/deepseek/deepseek-v3.2"], "models_extra": "", "conditions": ["hidden", "full"], "k": "2",
            "purpose": "screen", "brief_profile": "withheld", "stakeholder_model": "anthropic/claude-opus-5", "max_parallel": "4", "attempt_offset": "0"}
    pv, res = run_action(c, "run", form)
    assert pv.status_code == 200 and "--purpose screen" in pv.json()["cli"] and "--conditions hidden,full" in pv.json()["cli"]
    assert res.status_code == 200 and res.json()["workflow_ids"] == ["run-1"]
    assert client.calls[-1][1]["max_parallel_trials"] == 4 and client.calls[-1][1]["purpose"] == "screen"
    bad, _ = run_action(c, "run", {**form, "scenario_ids": ["not-registered"], "purpose": "party"})
    assert bad.status_code == 400 and any("not a registered valid scenario" in e for e in bad.json()["errors"]) and any("purpose" in e for e in bad.json()["errors"])


def test_cancel_review_retry_and_rebuild_flows(site, monkeypatch):
    client = _Client()
    _install(monkeypatch, client)
    c, sid = site["client"], site["spec"].id
    pv, res = run_action(c, "cancel", {"workflow_id": "run-1"})
    assert pv.json()["cli"] == "temporal workflow cancel --workflow-id run-1" and res.status_code == 200 and client.calls[-1] == ("cancel", "run-1")
    bad, _ = run_action(c, "cancel", {"workflow_id": "bad id!"})
    assert bad.status_code == 400
    # review: the held draft is approved through the approve_candidate signal path with the reason as the note
    sdir = site["ws"].scenario_dir(sid)
    (sdir / "approval.json").unlink()
    (sdir / "scenario.draft.json").write_text((sdir / "scenario.json").read_text())
    (sdir / "scenario.json").unlink()
    site["ws"].registry_path.write_text(json.dumps({"scenarios": {}}))  # a held draft has not been registered
    for name in ("validation.json", "compile.json"):
        (sdir / name).unlink(missing_ok=True)
    access.reset()
    spec_hash = site["spec"].spec_hash
    page = c.get(f"/review/{sid}").text
    assert "awaiting approval" in page and "data-action='review'" in page and c.get("/api/counts").json()["review_pending"] == 1
    pv, res = run_action(c, "review", {"scenario_id": sid, "spec_hash": spec_hash, "workflow_id": "create-abc", "decision": "approve", "reason": "looks right"})
    assert pv.json()["cli"].startswith(f"uv run swesim workflows approve create-abc {sid} {spec_hash} --decision approve")
    assert res.status_code == 200 and res.json()["via"] == "control workflow" and client.calls[-1] == ("approve_candidate", "create-abc", sid, spec_hash, "approve", "looks right")
    assert json.loads((sdir / "review.json").read_text())["decision"] == "approve"
    stale, res2 = run_action(c, "review", {"scenario_id": sid, "spec_hash": "0" * 64, "workflow_id": "create-abc", "decision": "reject", "reason": "no"})
    assert res2.status_code == 409 and "changed since" in res2.json()["error"]
    # retry a rejected candidate: a creation job over its source only, with the CLI shown
    pv, res = run_action(c, "retry", {"instance_id": "acme__widget-12", "source_key": "github-repo--acme__widget"})
    assert pv.status_code == 200 and "--source https://github.com/acme/widget" in pv.json()["cli"] and "--max-candidates 1" in pv.json()["cli"]
    assert res.status_code == 200 and client.calls[-1][0] == "start_create" and pv.json()["params"]["retry_of"]["instance_id"] == "acme__widget-12"
    assert run_action(c, "retry", {"instance_id": "nope", "source_key": "x"})[0].status_code == 400
    # rebuild with the index disabled: explicit, no Postgres touched
    pv, res = run_action(c, "rebuild", {})
    assert pv.json()["cli"] == "uv run swesim index rebuild" and res.status_code == 200 and res.json()["indexed"] is False
    results = [e["result"] for e in _actions_log(site)]
    assert results.count("accepted") >= 4 and "rejected" in results
    assert all(set(e) >= {"action_id", "actor", "action", "params", "confirmed", "origin_header", "result", "at", "origin"} for e in _actions_log(site))
    assert R.OperatorActionRow.model_validate(_actions_log(site)[0])  # every audit line is a valid row


def test_workflow_page_offers_cancel_only_while_open_and_shows_failure_text(site, monkeypatch):
    client = _Client()
    client.jobs = [{"workflow_id": "run-1", "run_id": "r-1", "type": "RunTasksWorkflow", "status": "RUNNING", "start_time": "2026-09-13T03:25:35+00:00", "summary": {"k": 2}},
                   {"workflow_id": "create-9", "run_id": "r-9", "type": "CreateTasksWorkflow", "status": "FAILED", "start_time": "2026-09-13T03:00:00+00:00",
                    "close_time": "2026-09-13T03:10:00+00:00", "summary": {}, "failure": "ApplicationError: judge rejected https://secret.example/x token=abc"}]
    client.progress_by_id["run-1"] = {"stage": "trial", "trials": {site["done"]: {"status": "completed"}}}
    _install(monkeypatch, client)
    c = site["client"]
    running = c.get("/workflows/run-1").text
    assert "data-action='cancel'" in running and "Progress" in running and "k</dt>" in running
    failed = c.get("/workflows/create-9").text
    assert "data-action='cancel'" not in failed and "Failure" in failed and "judge rejected" in failed and "secret.example" not in failed and "token=abc" not in failed
    assert c.get("/api/workflows/run-1").json()["progress"]["stage"] == "trial"
