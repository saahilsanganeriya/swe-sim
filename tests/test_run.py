"""Run stage: visible view, per-trial task materialization, run_trial with mocked Harbor and stakeholder."""
import json, tomllib
from decimal import Decimal

import pytest

from swesim import harbor_cli
from swesim.models import RunConfig, ScenarioSpec, minimal_spec_dict, trial_id
from swesim.paths import Workspace
from swesim.stages import run
from swesim.stages.run import TrialRecord, materialize_trial_task, run_trial
from swesim.stakeholder.view import FIXED_FORBIDDEN, forbidden_terms, visible_view

URL = "https://swesim-stakeholder-web.modal.run"

HIDDEN_TEST_PATCH = """diff --git a/tests/test_scales.py b/tests/test_scales.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/tests/test_scales.py
@@ -0,0 +1,8 @@
+import pytest
+from pkg.scales import Continuous, IntervalProperty
+
+
+def test_bool_series_maps_to_interval(bool_series):
+    out = Continuous()._setup(bool_series, IntervalProperty())
+    assert out == [1, 0, 0]  # a ValueError must never be raised here
"""


def test_forbidden_terms_come_from_hidden_tests_minus_what_the_reporter_said(tmp_path):
    d = minimal_spec_dict()
    d["ledger"] += [{"id": "f02", "text": "The mapping should use IntervalProperty semantics", "kind": "behavior", "provenance": "authored", "authored_rationale": "x", "discovery": "on-request"},
                    {"id": "f09", "text": "Continuous is the class to patch", "kind": "interface", "provenance": "authored", "authored_rationale": "x", "discovery": "author-only"}]
    spec = ScenarioSpec.model_validate(d)
    (tmp_path / "assets").mkdir(); (tmp_path / "assets" / "test.patch").write_text(HIDDEN_TEST_PATCH)
    terms = forbidden_terms(spec, tmp_path)
    assert terms == sorted(set(terms))                                                   # deterministic, unique
    for t in ("Continuous", "_setup", "bool_series", "test_bool_series_maps_to_interval", "pkg", "scales", "out", "must", "never", "raised", "here"):
        assert t in terms, t                                                            # in the hidden test, unknown to the reporter
    for t in FIXED_FORBIDDEN: assert t in terms, t
    assert "ValueError" in terms and "Raise" not in terms                                # in the test; requirements / facts never exempt a term (A-P1-2)
    assert "IntervalProperty" in terms                                                  # named in a visible fact, not by the reporter
    assert "Continuous" in terms                                                        # author-only facts do not count as visible
    for t in ("import", "from", "def", "diff", "git", "index", "mode", "dev", "null", "file"): assert t not in terms, t   # keywords and patch framing
    assert all(t.casefold() not in {"raise", "breaks"} for t in terms)


def test_visible_view_drops_author_only():
    d = minimal_spec_dict(); d["ledger"].append({"id": "f09", "text": "SECRET", "kind": "interface", "provenance": "authored", "authored_rationale": "x", "discovery": "author-only"})
    v = visible_view(ScenarioSpec.model_validate(d)); assert all(f["id"] != "f09" for f in v["facts"]) and v["brief"] == "It breaks."


def test_visible_view_structurally_excludes_oracle_and_tests():
    d = minimal_spec_dict()
    d["ledger"] += [{"id": "f02", "text": "BRIEFFACT", "kind": "behavior", "provenance": "original-issue", "source_excerpt": "It", "source_url": "u", "discovery": "brief"},
                    {"id": "f03", "text": "REVIEWFACT", "kind": "constraint", "provenance": "authored", "authored_rationale": "x", "discovery": "volunteered-at-review"},
                    {"id": "f04", "text": "REPOFACT", "kind": "context", "provenance": "authored", "authored_rationale": "x", "discovery": "repo-derivable"},
                    {"id": "f09", "text": "AUTHORONLY", "kind": "interface", "provenance": "authored", "authored_rationale": "x", "discovery": "author-only"}]
    d["checks"] = [{"id": "c1", "description": "reporter script", "command": "python repro.py", "expected_observation": "ok"}]
    spec = ScenarioSpec.model_validate(d); v = visible_view(spec)
    assert set(v) == {"brief", "facts", "requirements", "persona", "checks", "repo_dir"}
    assert [f["id"] for f in v["facts"]] == ["f01", "f02", "f03", "f04"] and all(set(f) == {"id", "text", "kind", "discovery"} for f in v["facts"])
    dump = json.dumps(v)
    assert "AUTHORONLY" not in dump and "oracle.diff" not in dump and "test.patch" not in dump and "t1" not in json.dumps(v["facts"])
    assert v["requirements"] == [{"id": "r01", "text": "Raise ValueError"}] and v["persona"]["role"] == "maintainer" and v["repo_dir"] == "/app"
    assert v["checks"][0]["id"] == "c1"


def test_materialize_sets_token(tmp_path):
    ws = Workspace(tmp_path); src = tmp_path / "task"; src.mkdir(); (src / "task.toml").write_text('[environment.env]\nSWESIM_SESSION_TOKEN = ""\nSWESIM_TRIAL_ID = ""\n')
    out = materialize_trial_task(ws, src, "abc123", "tok")
    t = tomllib.loads((out / "task.toml").read_text()); assert t["environment"]["env"]["SWESIM_SESSION_TOKEN"] == "tok" and t["environment"]["env"]["SWESIM_TRIAL_ID"] == "abc123"
    assert t["metadata"]["trial_id"] == "abc123" and out == ws.root / "trials" / "abc123" / "task"
    assert (out.stat().st_mode & 0o777) == 0o700 and ((out / "task.toml").stat().st_mode & 0o777) == 0o600
    with pytest.raises(ValueError): materialize_trial_task(ws, src, "abc123", "tok")   # never overwrite an existing trial task


def _compiled_task_toml(hosts):
    return ("[task]\nname = \"swesim/x--interactive\"\n\n[metadata]\nsource = \"authored\"\ncondition = \"interactive\"\n\n"
            "[agent]\ntimeout_sec = 2400\nuser = \"agent\"\nnetwork_mode = \"allowlist\"\nallowed_hosts = " + json.dumps(hosts) + "\n\n"
            "[environment]\nnetwork_mode = \"allowlist\"\nallowed_hosts = " + json.dumps(hosts) + "\n\n"
            "[environment.env]\nUV_OFFLINE = \"1\"\nSWESIM_STAKEHOLDER_URL = \"" + URL + "\"\nSWESIM_SESSION_TOKEN = \"\"\nSWESIM_TRIAL_ID = \"\"\nSWESIM_REPO_DIR = \"/app\"\n")


@pytest.fixture
def scene(tmp_path, monkeypatch):
    """A validated scenario with a compiled interactive task, fake Harbor and fake stakeholder HTTP."""
    ws = Workspace(tmp_path / "ws"); spec = ScenarioSpec.model_validate(minimal_spec_dict())
    sdir = ws.scenario_dir(spec.id); sdir.mkdir(parents=True)
    (sdir / "compile.json").write_text(json.dumps({"compiled_hash": "c" * 8, "image_digest": "sha256:" + "d" * 64}))
    (sdir / "validation.json").write_text(json.dumps({"status": "valid", "spec_hash": spec.spec_hash, "compiled_hash": "c" * 8}))
    (sdir / "assets").mkdir(); (sdir / "assets" / "test.patch").write_text(HIDDEN_TEST_PATCH)   # the hidden test asset
    from pathlib import Path as _P
    scaffold = _P(run.__file__).resolve().parent.parent / "templates" / "mini_swe_agent.interactive.yaml"
    for cond in ("interactive", "hidden"):
        tdir = ws.task_dir(spec.id, cond); (tdir / "environment").mkdir(parents=True)
        (tdir / "task.toml").write_text(_compiled_task_toml(["api.openai.com", "swesim-stakeholder-web.modal.run"]))
        (tdir / "instruction.md").write_text("compiled instruction")
        (tdir / "instruction.natural.md").write_text(f"INSTRUCTION {cond} natural")   # compile-time per-profile render
        if cond == "interactive": (tdir / "agent_config.yaml").write_bytes(scaffold.read_bytes())  # compile-time scaffold copy
    calls = {"post": [], "get": [], "run": []}
    H = "a" * 64
    state = {"transcript_hash": H, "frozen": True, "report": {"transcript_hash": H, "patch_sha256": "p"}, "exception": None, "trajectory": None}

    def fake_post(url, body, headers):
        calls["post"].append((url, body, headers))
        if url.endswith("/sessions"): return {"token": "t1.tok"}
        if url.endswith("/freeze"): return {"frozen": True, "transcript_hash": state["transcript_hash"]}
        raise AssertionError(url)

    def fake_get(url, headers):
        calls["get"].append((url, headers))
        return {"trial_id": url.rsplit("/", 1)[1], "events": [{"kind": "session"}, {"kind": "message", "role": "user", "exchange": "ask", "request_id": "1", "ts": 1.0, "content": "q"}],
                "patience_start": 6, "patience_end": 5, "frozen": state["frozen"], "transcript_hash": state["transcript_hash"], "service_failure": None, "pending": None}

    def fake_run(task_dir, agent, jobs_dir, job_name, model=None, agent_kwargs=None, env_provider="modal", timeout_sec=10800):
        calls["run"].append((task_dir, agent, jobs_dir, job_name, model, timeout_sec))
        d = jobs_dir / job_name / "trial-0"; (d / "verifier").mkdir(parents=True)
        (d / "result.json").write_text(json.dumps({"exception_info": state["exception"]}))
        if state["exception"] is None:
            (d / "verifier" / "reward.json").write_text(json.dumps({"reward": 1.0, "success": 1, "outcome_code": 1}))
            (d / "verifier" / "report.json").write_text(json.dumps(state["report"]))
        if state["trajectory"] is not None:                                     # the agent's own trajectory, as Harbor's adapter leaves it
            (d / "agent").mkdir()
            name = "mini-swe-agent.trajectory.json" if "messages" in state["trajectory"] else "trajectory.json"
            (d / "agent" / name).write_text(json.dumps(state["trajectory"]))
        return d

    monkeypatch.setattr(run, "_post", fake_post); monkeypatch.setattr(run, "_get", fake_get)
    monkeypatch.setattr(harbor_cli, "run_task", fake_run)
    monkeypatch.setattr(run, "has_secret", lambda name, settings=None: True)   # every provider key present unless a test says otherwise
    return ws, spec, calls, state


def test_run_trial_interactive_end_to_end(scene):
    ws, spec, calls, state = scene
    rc = RunConfig(condition="interactive", model="openai/gpt-5.5")
    rec = run_trial(ws, spec, rc, 0, URL, "secret")
    assert isinstance(rec, TrialRecord) and rec.status == "completed" and rec.reward == {"reward": 1.0, "success": 1, "outcome_code": 1}
    import hashlib as _h
    scaffold_id = "mini-swe-agent.interactive:" + _h.sha256((ws.task_dir(spec.id, "interactive") / "agent_config.yaml").read_bytes()).hexdigest()[:16]
    effective = RunConfig.model_validate({**rc.model_dump(), "compiled_hash": "c" * 8, "brief_profile": "natural", "agent_config": scaffold_id})
    tid = trial_id(spec.spec_hash, "interactive", effective.run_config_hash, 0)
    assert rec.trial_id == tid and rec.run_config_hash == effective.run_config_hash and rec.compiled_hash == "c" * 8 and rec.brief_profile == "natural"
    assert rec.spec_hash == spec.spec_hash and rec.model == "openai/gpt-5.5" and rec.purpose == "demonstration" and rec.scenario_id == spec.id
    # Session creation posted the visible view with the worker secret; the freeze safety net used the token.
    url, body, headers = calls["post"][0]
    assert url == URL + "/sessions" and headers == {"X-Worker-Secret": "secret"} and body["trial_id"] == tid and body["condition"] == "interactive"
    assert body["spec_hash"] == spec.spec_hash and body["run_config_hash"] == effective.run_config_hash and body["image"] == "ubuntu:24.04"
    assert body["visible_view"]["facts"][0]["id"] == "f01" and body["stakeholder_config"]["patience_profile"]["start"] == 6
    ft = body["visible_view"]["forbidden_terms"]                                        # deny-list of tokens from the hidden test
    assert {"Continuous", "IntervalProperty", "_setup", "_setup(", "assert", "pytest", "tests/", "test_", "ValueError"} <= set(ft) and "breaks" not in ft
    assert HIDDEN_TEST_PATCH.splitlines()[-1].strip("+ ") not in json.dumps(body)        # tokens, never test content
    assert calls["post"][1][0] == URL + "/freeze" and calls["post"][1][2] == {"Authorization": "Bearer t1.tok"}
    assert calls["get"] == [(URL + f"/transcript/{tid}", {"X-Worker-Secret": "secret"})]
    # Materialized task: token and trial id in the env, metadata carries lineage, instruction re-rendered, provider host allowed.
    tdir = ws.root / "trials" / tid; task = tdir / "task"
    assert rec.task_dir == str(task) and rec.trial_dir is not None
    tc = tomllib.loads((task / "task.toml").read_text())
    assert tc["environment"]["env"]["SWESIM_SESSION_TOKEN"] == "t1.tok" and tc["environment"]["env"]["SWESIM_TRIAL_ID"] == tid
    assert tc["metadata"]["trial_id"] == tid and tc["metadata"]["run_config_hash"] == effective.run_config_hash and tc["metadata"]["compiled_hash"] == "c" * 8 and tc["metadata"]["brief_profile"] == "natural"
    assert "api.openai.com" in tc["agent"]["allowed_hosts"] and "swesim-stakeholder-web.modal.run" in tc["environment"]["allowed_hosts"]
    assert (task / "instruction.md").read_text() == "INSTRUCTION interactive natural"
    # Harbor was launched once on the materialized copy with the run's model.
    assert len(calls["run"]) == 1 and calls["run"][0][0] == task and calls["run"][0][3] == tid and calls["run"][0][4] == "openai/gpt-5.5" and calls["run"][0][2] == ws.jobs_dir / "runs"
    # Written artifacts with the frozen names.
    for name in ("reward.json", "report.json", "transcript.json", "status.json", "record.json", "run_config.json", "spec.json"): assert (tdir / name).exists(), name
    st = json.loads((tdir / "status.json").read_text())
    assert set(st) == {"status", "trial_id", "started_at", "finished_at"} and st["status"] == "completed" and st["trial_id"] == tid
    assert json.loads((tdir / "reward.json").read_text()) == rec.reward
    report = json.loads((tdir / "report.json").read_text())
    assert report["transcript_binding_valid"] is True and report["reviewed_equals_submitted"] is None and report["simulator_validity"] == "unadjudicated"
    assert report["image_digest"] == "sha256:" + "d" * 64 and report["harbor_exception"] is None and "transcript_hash_mismatch" not in report
    assert json.loads((tdir / "transcript.json").read_text())["transcript_hash"] == "a" * 64
    record = json.loads((tdir / "record.json").read_text()); assert record["trial_id"] == tid and record["transcript"]["patience_end"] == 5
    assert json.loads((tdir / "run_config.json").read_text())["compiled_hash"] == "c" * 8 and json.loads((tdir / "spec.json").read_text())["id"] == spec.id
    # Idempotent: a second call returns the prior record without relaunching or re-creating a session.
    again = run_trial(ws, spec, rc, 0, URL, "secret")
    assert again == rec and len(calls["run"]) == 1 and len(calls["post"]) == 2


def test_run_trial_records_timestamps(scene, monkeypatch):
    """status.json carries started_at/finished_at (finished_at null while running); record.json carries both as ISO-8601 UTC."""
    from datetime import datetime, timezone
    ws, spec, calls, state = scene
    inner = harbor_cli.run_task; seen = {}
    def spying(task_dir, *a, **k):
        seen["running"] = json.loads((task_dir.parent / "status.json").read_text()); return inner(task_dir, *a, **k)
    monkeypatch.setattr(harbor_cli, "run_task", spying)
    rec = run_trial(ws, spec, RunConfig(condition="interactive", model="openai/gpt-5.5"), 0, URL, "secret")
    running = seen["running"]
    assert running["status"] == "running" and running["finished_at"] is None and datetime.fromisoformat(running["started_at"]).tzinfo is not None
    tdir = ws.root / "trials" / rec.trial_id
    status = json.loads((tdir / "status.json").read_text()); record = json.loads((tdir / "record.json").read_text())
    for d in (status, record):
        start, end = datetime.fromisoformat(d["started_at"]), datetime.fromisoformat(d["finished_at"])
        assert start.tzinfo == timezone.utc and end.tzinfo == timezone.utc and start <= end
    assert status["started_at"] == running["started_at"] == record["started_at"] == rec.started_at and record["finished_at"] == rec.finished_at
    # appended after the frozen TrialRecord fields, in order: timestamps, then the solver cost (interfaces section 6)
    assert list(record)[-6:] == ["started_at", "finished_at", "cost_usd", "input_tokens", "output_tokens", "price_source"]
    assert TrialRecord(**record) == rec and run_trial(ws, spec, RunConfig(condition="interactive", model="openai/gpt-5.5"), 0, URL, "secret") == rec


def test_run_trial_transcript_mismatch_marks_binding_invalid(scene):
    ws, spec, calls, state = scene
    state["report"] = {"transcript_hash": "b" * 64}
    rec = run_trial(ws, spec, RunConfig(condition="interactive", model="openai/gpt-5.5"), 1, URL, "secret")
    assert rec.report["transcript_binding_valid"] is False and rec.report["training_eligible"] is False and rec.report["transcript_hash_mismatch"] is True
    assert rec.reward["success"] == 1          # correctness scores are kept; a missing binding does not erase the code outcome


def test_run_trial_pending_marker_is_simulator_evidence(scene, monkeypatch):
    ws, spec, calls, state = scene
    orig = run._get
    def with_pending(url, headers): return {**orig(url, headers), "pending": {"request_id": "9", "kind": "ask"}}
    monkeypatch.setattr(run, "_get", with_pending)
    rec = run_trial(ws, spec, RunConfig(condition="interactive", model="openai/gpt-5.5"), 2, URL, "secret")
    assert rec.report["transcript_binding_valid"] is True                                     # hashes still agree
    assert rec.report["training_eligible"] is False and rec.report["interrupted_request"] == {"request_id": "9", "kind": "ask"}
    assert "service_failure_evidence" not in rec.report and rec.reward["success"] == 1      # dict when present, omitted otherwise


def test_run_trial_unexpected_exception_writes_error_status(scene, monkeypatch):
    ws, spec, calls, state = scene
    def broken(*a, **k): raise OSError("disk full")
    monkeypatch.setattr(harbor_cli, "run_task", broken)
    rc = RunConfig(condition="interactive", model="openai/gpt-5.5")
    with pytest.raises(OSError): run_trial(ws, spec, rc, 0, URL, "secret")
    import hashlib as _h
    scaffold_id = "mini-swe-agent.interactive:" + _h.sha256((ws.task_dir(spec.id, "interactive") / "agent_config.yaml").read_bytes()).hexdigest()[:16]
    effective = RunConfig.model_validate({**rc.model_dump(), "compiled_hash": "c" * 8, "brief_profile": "natural", "agent_config": scaffold_id})
    tdir = ws.root / "trials" / trial_id(spec.spec_hash, "interactive", effective.run_config_hash, 0)
    st = json.loads((tdir / "status.json").read_text())
    assert st["status"] == "error" and st["error"] == "OSError" and st["finished_at"] and st["started_at"] and st["trial_id"] == tdir.name
    assert not (tdir / "record.json").exists()                                                # reconcile, never silently retry
    with pytest.raises(ValueError): run_trial(ws, spec, rc, 0, URL, "secret")               # task dir exists without a record


def test_run_trial_hidden_condition_skips_stakeholder(scene):
    ws, spec, calls, state = scene
    rec = run_trial(ws, spec, RunConfig(condition="hidden", model="openai/gpt-5.5"), 0, URL, "secret")
    assert rec.condition == "hidden" and calls["post"] == [] and calls["get"] == [] and rec.transcript == {}
    assert rec.report["simulator_validity"] == "n/a" and "transcript_binding_valid" not in rec.report
    tc = tomllib.loads((ws.root / "trials" / rec.trial_id / "task" / "task.toml").read_text())
    assert tc["environment"]["env"]["SWESIM_SESSION_TOKEN"] == "" and tc["environment"]["env"]["SWESIM_TRIAL_ID"] == rec.trial_id


def test_run_trial_launch_failure_and_agent_exception(scene, monkeypatch):
    ws, spec, calls, state = scene
    def failing(*a, **k): raise harbor_cli.HarborRunError("no trial dir")
    monkeypatch.setattr(harbor_cli, "run_task", failing)
    rec = run_trial(ws, spec, RunConfig(condition="interactive", model="openai/gpt-5.5"), 0, URL, "secret")
    assert rec.status == "launch-failed" and rec.reward == {"reward": 0.0, "success": 0, "outcome_code": 7} and rec.trial_dir is None
    assert rec.report["outcome"] == "evaluator-failure" and rec.report["transcript_binding_valid"] is False
    assert json.loads((ws.root / "trials" / rec.trial_id / "status.json").read_text())["status"] == "launch-failed"
    # Agent timeout recorded by Harbor: outcome code 3, reward zero, no verifier output.
    state["exception"] = {"exception_type": "AgentTimeoutError", "exception_message": "timed out"}
    monkeypatch.setattr(harbor_cli, "run_task", scene_run(calls, state))
    rec2 = run_trial(ws, spec, RunConfig(condition="interactive", model="openai/gpt-5.5"), 1, URL, "secret")
    assert rec2.status == "completed" and rec2.reward == {"reward": 0.0, "success": 0, "outcome_code": 3} and rec2.report["outcome"] == "agent-invalid-submission"


def scene_run(calls, state):
    def fake_run(task_dir, agent, jobs_dir, job_name, model=None, agent_kwargs=None, env_provider="modal", timeout_sec=10800):
        calls["run"].append((task_dir, agent, jobs_dir, job_name, model, timeout_sec))
        d = jobs_dir / job_name / "trial-0"; (d / "verifier").mkdir(parents=True)
        (d / "result.json").write_text(json.dumps({"exception_info": state["exception"]}))
        return d
    return fake_run


def test_run_trial_refuses_unvalidated_or_mismatched_builds(scene):
    ws, spec, calls, state = scene
    rc = RunConfig(condition="interactive", model="openai/gpt-5.5")
    with pytest.raises(ValueError): run_trial(ws, spec, rc, 0, "https://other-host.modal.run", "secret")          # stakeholder host not compiled in
    with pytest.raises(ValueError): run_trial(ws, spec, RunConfig(condition="interactive", model="openai/gpt-5.5", solver_timeout_sec=600), 0, URL, "secret")
    with pytest.raises(ValueError): run_trial(ws, spec, RunConfig(condition="interactive", model="mistral/x"), 0, URL, "secret")
    sdir = ws.scenario_dir(spec.id); val = json.loads((sdir / "validation.json").read_text()); val["status"] = "invalid"
    (sdir / "validation.json").write_text(json.dumps(val))
    with pytest.raises(ValueError): run_trial(ws, spec, rc, 0, URL, "secret")
    assert calls["post"] == [] and calls["run"] == []


def test_render_instruction_reads_the_compiled_profile_file(tmp_path):
    """Instructions are rendered at compile time per brief profile; the run stage reads the compiled file for the effective
    profile (full always maps to natural), falls back to the condition's default instruction.md, and never re-renders."""
    spec = ScenarioSpec.model_validate(minimal_spec_dict())
    compiled = tmp_path / "task"; compiled.mkdir()
    (compiled / "instruction.md").write_text("default"); (compiled / "instruction.natural.md").write_text("natural text")
    assert run.render_instruction(spec, RunConfig(condition="interactive", brief_profile="natural"), compiled) == "natural text"
    assert run.render_instruction(spec, RunConfig(condition="full", brief_profile="withheld"), compiled) == "natural text"
    (compiled / "instruction.natural.md").unlink()
    assert run.render_instruction(spec, RunConfig(condition="hidden", brief_profile="natural"), compiled) == "default"
    with pytest.raises(ValueError):
        run.render_instruction(spec, RunConfig(condition="hidden"))



def test_interactive_runs_layer_the_stakeholder_scaffold_config(scene, monkeypatch):
    """The packaged mini-swe-agent workflow never lists asking the stakeholder (10 of 10 pilot trials asked nothing), so
    interactive trials ship the swesim scaffold config through Harbor's `config_file` kwarg and record its sha256; other
    conditions and agents do not."""
    import hashlib
    from pathlib import Path
    ws, spec, calls, state = scene
    seen = []
    orig = harbor_cli.run_task
    def spy(task_dir, agent, jobs_dir, job_name, model=None, agent_kwargs=None, env_provider="modal", timeout_sec=10800):
        seen.append(dict(agent_kwargs or {})); return orig(task_dir, agent, jobs_dir, job_name, model=model, agent_kwargs=agent_kwargs, timeout_sec=timeout_sec)
    monkeypatch.setattr(harbor_cli, "run_task", spy)
    tpl = Path(run.__file__).resolve().parent.parent / "templates" / "mini_swe_agent.interactive.yaml"
    assert "ask-user" in tpl.read_text() and "{{task}}" in tpl.read_text()
    rec = run_trial(ws, spec, RunConfig(condition="interactive", model="openai/gpt-5.5"), 0, URL, "secret")
    kw = seen[-1]; cfg = Path(kw["config_file"])
    assert kw["version"] == "2.4.6" and cfg.name == "agent_config.yaml" and cfg.read_bytes() == tpl.read_bytes()
    assert cfg.parent == ws.root / "trials" / rec.trial_id / "task"
    assert rec.report["agent_config_sha256"] == hashlib.sha256(tpl.read_bytes()).hexdigest()
    rec2 = run_trial(ws, spec, RunConfig(condition="hidden", model="openai/gpt-5.5"), 0, URL, "secret")
    assert "config_file" not in seen[-1] and rec2.report["agent_config_sha256"] is None


# ------------------------------------------------------------------ providers, OpenRouter and the solver cost (interfaces sections 6-7)

def _mini_traj(or_costs=None, model="openrouter/deepseek/deepseek-v3.2"):
    msgs = []
    for i in range(2):
        usage = {"prompt_tokens": 1000 * (i + 1), "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 800 * i}}
        if or_costs is not None:
            usage["cost"] = or_costs[i]
        msgs.append({"role": "assistant", "content": "a", "extra": {"actions": [], "cost": 0.0, "timestamp": 1.0, "response": {"usage": usage}}})
        msgs.append({"role": "tool", "content": "o", "extra": {"returncode": 0}})
    return {"info": {"model_stats": {"instance_cost": 0.0, "api_calls": 2}, "config": {"model": {"model_name": model}}}, "messages": msgs,
            "trajectory_format": "mini-swe-agent-1.1"}


def test_provider_table_and_the_env_each_solver_needs_forwarded():
    assert run.PROVIDER_HOSTS == {"anthropic": "api.anthropic.com", "openai": "api.openai.com", "openrouter": "openrouter.ai"}
    assert run.agent_env_names("openrouter/deepseek/deepseek-v3.2") == ("OPENROUTER_API_KEY",)
    assert run.agent_env_names("anthropic/claude-haiku-4-5-20251001") == ("ANTHROPIC_API_KEY",) and run.agent_env_names("openai/gpt-5.5") == ("OPENAI_API_KEY",)
    with pytest.raises(ValueError):
        run.agent_env_names("mistral/x")
    assert issubclass(run.ProviderKeyMissing, ValueError)                    # configuration, not a transient failure: never retried


def test_openrouter_solver_allowlists_openrouter_ai_passes_the_id_verbatim_and_records_the_generation_cost(scene):
    ws, spec, calls, state = scene
    state["trajectory"] = _mini_traj(or_costs=(0.000012, 0.00002))
    rc = RunConfig(condition="hidden", model="openrouter/deepseek/deepseek-v3.2")
    rec = run_trial(ws, spec, rc, 0, URL, "secret")
    tdir = ws.root / "trials" / rec.trial_id
    tc = tomllib.loads((tdir / "task" / "task.toml").read_text())
    assert "openrouter.ai" in tc["agent"]["allowed_hosts"] and "openrouter.ai" in tc["environment"]["allowed_hosts"]
    assert "api.openai.com" in tc["agent"]["allowed_hosts"]                    # compiled hosts are kept, the run's provider is added
    assert calls["run"][0][4] == "openrouter/deepseek/deepseek-v3.2"           # mini-swe-agent/litellm take the id verbatim
    assert rec.status == "completed" and rec.model == rc.model
    assert rec.cost_usd == pytest.approx(0.000032) and rec.price_source == "openrouter-generation" and (rec.input_tokens, rec.output_tokens) == (3000, 100)
    rows = json.loads((tdir / "costs.json").read_text())
    assert len(rows) == 1 and rows[0]["role"] == "solver" and rows[0]["model"] == rc.model and rows[0]["trial_id"] == rec.trial_id
    assert rows[0]["usd"] == "0.000032" and rows[0]["price_source"] == "openrouter-generation" and rows[0]["provider"] == "openrouter"
    record = json.loads((tdir / "record.json").read_text())
    assert record["cost_usd"] == rec.cost_usd and record["price_source"] == "openrouter-generation" and record["input_tokens"] == 3000
    assert run_trial(ws, spec, rc, 0, URL, "secret") == rec and len(json.loads((tdir / "costs.json").read_text())) == 1   # idempotent


def test_solver_cost_comes_from_the_price_table_when_the_provider_reports_none(scene):
    ws, spec, calls, state = scene
    state["trajectory"] = {"schema_version": "ATIF-v1.6", "session_id": "s", "agent": {"name": "mini-swe-agent", "model_name": "openai/gpt-5.5"}, "steps": [],
                           "final_metrics": {"total_prompt_tokens": 1_000_000, "total_completion_tokens": 100_000, "total_cached_tokens": 0,
                                             "total_cost_usd": 7.5, "total_steps": 1}}
    rec = run_trial(ws, spec, RunConfig(condition="hidden", model="openai/gpt-5.5"), 0, URL, "secret")
    assert rec.cost_usd == 8.0 and rec.price_source == "openai-pricing-page"     # $5 x 1M input + $30 x 100k output; the agent's own 7.5 estimate is not the record
    rows = json.loads((ws.root / "trials" / rec.trial_id / "costs.json").read_text())
    assert Decimal(rows[0]["usd"]) == Decimal("8")


def test_trial_without_usage_evidence_records_an_unknown_cost_never_zero(scene):
    ws, spec, calls, state = scene
    rec = run_trial(ws, spec, RunConfig(condition="hidden", model="openai/gpt-5.5"), 0, URL, "secret")
    assert rec.cost_usd is None and rec.price_source is None and rec.input_tokens is None and rec.output_tokens is None
    assert not (ws.root / "trials" / rec.trial_id / "costs.json").exists()


def test_missing_provider_key_stops_the_trial_before_any_session_or_launch(scene, monkeypatch):
    ws, spec, calls, state = scene
    monkeypatch.setattr(run, "has_secret", lambda name, settings=None: name != "OPENROUTER_API_KEY")
    rc = RunConfig(condition="interactive", model="openrouter/deepseek/deepseek-v3.2")
    with pytest.raises(run.ProviderKeyMissing, match="OPENROUTER_API_KEY"):
        run_trial(ws, spec, rc, 0, URL, "secret")
    assert calls["post"] == [] and calls["run"] == [] and not (ws.root / "trials").exists()   # nothing created: reconcile has nothing to see
    rec = run_trial(ws, spec, RunConfig(condition="hidden", model="anthropic/claude-haiku-4-5-20251001"), 0, URL, "secret")
    assert rec.status == "completed"                                                        # other providers unaffected
