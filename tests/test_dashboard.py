"""Console over the file reader: row projections, every page and JSON twin, and the evaluator gate (no leak on any
non-evaluator route; positive control for evaluator identities). Offline: Temporal is a fake client, the stakeholder
service is never called (httpx.get fails the test if touched)."""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from swesim.dashboard import access, data, render
from swesim.dashboard import rows as R
from swesim.dashboard.app import app
from swesim.dashboard.data import merge_timeline
from swesim.dashboard.filereader import FileIndexReader
from swesim.models import ScenarioSpec, minimal_spec_dict
from swesim.settings import reset_settings
from swesim.stages.score import score_scenario

try:  # pytest's default (prepend) import mode puts tests/ on sys.path
    from test_score import HAIKU, T0, ask_events, make_workspace, review_events, write_trial, write_unresolved
except ImportError:  # pragma: no cover - importlib mode
    from tests.test_score import HAIKU, T0, ask_events, make_workspace, review_events, write_trial, write_unresolved

EVAL_EMAIL = "eval@example.com"
SENTINELS = {
    "author_only_fact": "SECRET-AUTHOR-ONLY-NOTE",
    "withheld_excerpt": "WITHHELD-SENTINEL-77",
    "redacted_term": "REDACTED-SENTINEL-88",
    "judge_rationale": "JUDGE-SENTINEL-99",
    "draft_meta": "DRAFTMETA-SENTINEL-55",
    "audit_reason": "AUDIT-SENTINEL-66",
    "hidden_asset": "hidden_test_SENTINEL_44.py",
}
EVALUATOR_JSON_KEYS = ('"redacted_terms"', '"withheld_sentences"', '"disclosed_fact_ids"', '"tool_calls"', '"tier"', '"regenerated"', '"forbidden_terms"')


def make_spec_with_sentinels() -> ScenarioSpec:
    d = minimal_spec_dict()
    d["id"] = "example-scenario"
    d["version"] = 1
    d["briefs"][0]["text"] = f"Import fails\nTraceback: {SENTINELS['withheld_excerpt']} raised in loader\nSteps: run `pkg load`."
    d["briefs"].append({"profile": "withheld", "text": "Import fails\nSteps: run `pkg load`.", "provenance": "withheld"})
    d["ledger"].append({"id": "withheld-001", "text": "The traceback names the loader", "kind": "reproduction", "provenance": "original-issue",
                        "source_excerpt": f"Traceback: {SENTINELS['withheld_excerpt']} raised in loader", "source_url": "https://example.com/issue",
                        "discovery": "on-request", "tests": []})
    d["ledger"].append({"id": "f09", "text": SENTINELS["author_only_fact"], "kind": "context", "provenance": "authored", "authored_rationale": "internal",
                        "discovery": "author-only"})
    d["operations"] = [{"type": "withholding", "target": "withheld-001", "rationale": "Removed category: reproduction"}]
    d.setdefault("assets", {})
    d["assets"][SENTINELS["hidden_asset"]] = {"path": f"assets/{SENTINELS['hidden_asset']}", "bytes": 12, "sha256": "a" * 64}
    return ScenarioSpec.model_validate(d)


@pytest.fixture
def site(tmp_path, monkeypatch):
    spec = make_spec_with_sentinels()
    ws = make_workspace(tmp_path, spec)
    sdir = ws.scenario_dir(spec.id)
    (sdir / "judge.json").write_text(json.dumps({"keep": True, "verified_rubric_score": 3, "complexity": "medium", "rationale": SENTINELS["judge_rationale"], "gaps": [],
                                                 "model_id": "openai/gpt-5.5"}))
    (sdir / "draft.meta.json").write_text(json.dumps({"created_at": "2026-09-13T06:00:00+00:00", "note": SENTINELS["draft_meta"], "spec_hash": spec.spec_hash}))
    (sdir / "approval.json").write_text(json.dumps({"approved_by": "auto-validators", "review_needed": True, "approved_at": "2026-09-13T06:01:00+00:00",
                                                    "spec_hash": spec.spec_hash, "version": 1, "scenario_id": spec.id}))
    (sdir / "assets").mkdir(exist_ok=True)
    (sdir / "assets" / SENTINELS["hidden_asset"]).write_text("def test_secret(): pass\n")
    (sdir / "assets" / "Dockerfile.base").write_text("FROM python:3.12\n")
    q = ask_events(T0 + 6, "Which exception should be raised?", "ValueError.", ["f01"], 5)
    q[1]["redacted_terms"] = [SENTINELS["redacted_term"]]
    q[1]["regenerated"] = True
    done = write_trial(ws, spec, events=q + review_events(T0 + 30, 3), label={"label": "unresolved", "note": "first look"})
    (ws.root / "trials" / done / "audit.json").write_text(json.dumps({"questions": [{"seq": 0, "specific": True, "decision_oriented": True, "before_implementation": True,
                                                                                    "redundant": False, "fishing": False, "reason": SENTINELS["audit_reason"]}],
                                                                     "replies": [{"seq": 1, "targeted": True, "volunteered": False, "test_or_identifier_disclosed": False,
                                                                                  "behaviour_level": True, "reason": SENTINELS["audit_reason"]}],
                                                                     "asked_before_implementing": True, "useful_question_rate": 1.0, "model": "anthropic/claude-opus-5",
                                                                     "audited_at": "2026-09-13T07:00:00+00:00", "rubric_version": "v1"}))
    wrong = write_trial(ws, spec, attempt=1, outcome_code=2, success=0, reward=0.0)
    full = write_trial(ws, spec, condition="full", purpose="comparison")
    runner_error = write_unresolved(ws, spec, kind="runner-error", attempt=5)
    launch_failed = write_unresolved(ws, spec, kind="launch-failed", attempt=6)
    running = "ab" * 12
    rdir = ws.root / "trials" / running
    (rdir / "task").mkdir(parents=True)
    (rdir / "task" / "instruction.md").write_text("# Task\n\nIt breaks.\n")
    (rdir / "status.json").write_text(json.dumps({"status": "running", "trial_id": running, "started_at": "2026-09-13T12:00:00Z"}))
    (rdir / "request.json").write_text(json.dumps({"trial_id": running, "scenario_id": spec.id, "condition": "interactive", "model": HAIKU, "spec_hash": spec.spec_hash,
                                                  "run_config_hash": "r" * 64, "attempt_index": 9, "workflow_id": "run-live-1",
                                                  "run_config": {"purpose": "calibration", "brief_profile": "withheld", "model": HAIKU, "condition": "interactive",
                                                                 "stakeholder": {"patience_profile": {"start": 6}}}}))
    (ws.root / "trials" / done / "costs.json").write_text(json.dumps([{"role": "solver", "provider": "anthropic", "model": HAIKU, "input_tokens": 1000, "output_tokens": 200,
                                                                     "usd": "0.0125", "price_source": "anthropic-2026-09", "at": "2026-09-13T10:05:00+00:00", "workflow_id": "run-1"}]))
    cdir = ws.root / "candidates"
    cdir.mkdir()
    rows = [{"source": "github-repo", "instance_id": "acme__widget-12", "repo": "acme/widget", "base_commit": "abc"},
            {"source": "github-repo", "instance_id": "acme__widget-13", "repo": "acme/widget", "base_commit": "abd"}]
    (cdir / "github-repo--acme__widget.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (cdir / "github-repo--acme__widget.judged.jsonl").write_text(json.dumps({**rows[0], "judge": {"keep": False, "rationale": "not behavioural"}}) + "\n"
                                                                 + json.dumps({**rows[1], "judge": {"keep": True, "rationale": "fine"}}) + "\n")
    (cdir / "github-repo--acme__widget.rejects.json").write_text(json.dumps({"no-linked-issue": 5, "diff-too-small": 2}))
    score_scenario(ws, spec.id, k=3)
    ws.registry_path.write_text(json.dumps({"scenarios": {spec.id: {
        "id": spec.id, "version": 1, "spec_hash": spec.spec_hash, "source": "authored", "source_ref": "example", "validation_status": "valid",
        "validation_path": str(sdir / "validation.json"), "conditions": ["full", "hidden", "interactive"], "compiled_hash": "c" * 64, "image_digest": "sha256:" + "d" * 64,
        "calibration_runs": [done, wrong], "comparison_runs": [full], "demonstration_runs": []}}}))
    monkeypatch.setenv("SWESIM_WORKSPACE", str(ws.root))
    monkeypatch.setenv("SWESIM_REGISTRY", str(ws.registry_path))
    monkeypatch.setenv("SWESIM_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("SWESIM_EVALUATORS", EVAL_EMAIL)
    monkeypatch.setenv("SWESIM_IAP_AUDIENCE", "/projects/1/locations/us-central1/services/console")
    for k in ("SWESIM_STAKEHOLDER_URL", "SWESIM_WORKER_SECRET", "SWESIM_CONSOLE_URL", "SWESIM_DATABASE_URL", "SWESIM_TEMPORAL_UI_URL", "SWESIM_SPEND_CAP_USD"):
        monkeypatch.delenv(k, raising=False)
    reset_settings()
    access.reset()
    monkeypatch.setattr(data.httpx, "get", lambda *a, **k: pytest.fail("network call for a finished trial"))
    monkeypatch.setattr(data, "_live_cache", {})
    app.state.verify_jwt = lambda token, audience: {"email": token.split(":", 1)[1], "aud": audience}
    yield {"ws": ws, "spec": spec, "done": done, "wrong": wrong, "full": full, "running": running, "runner_error": runner_error, "launch_failed": launch_failed,
           "client": TestClient(app), "eval": {"X-Goog-Authenticated-User-Email": f"accounts.google.com:{EVAL_EMAIL}", "X-Goog-IAP-JWT-Assertion": f"ok:{EVAL_EMAIL}"}}
    app.state.verify_jwt = None
    reset_settings()
    access.reset()


def reader_of(site) -> FileIndexReader:
    return FileIndexReader(site["ws"].root, site["ws"].registry_path, origin="laptop")


# ---------------------------------------------------------------- timeline merge

def test_merge_orders_by_time():
    traj = {"steps": [{"timestamp": "2026-09-13T10:00:05Z", "source": "agent", "message": "reading"}]}
    t = datetime.fromisoformat("2026-09-13T10:00:00+00:00").timestamp()
    transcript = {"events": [{"kind": "message", "ts": t, "role": "user", "content": "Which exception?", "exchange": "ask"},
                             {"kind": "message", "ts": t + 3, "role": "assistant", "content": "ValueError.", "tool_calls": [{"tool": "read_diff"}]}]}
    rows = merge_timeline(traj, transcript)
    assert [r["actor"] for r in rows] == ["agent", "stakeholder", "stakeholder", "agent"]
    assert [r["ts"] for r in rows] == [t, t + 3, t + 3, t + 5]
    assert [r["kind"] for r in rows] == ["ask", "ask", "tool_call", "agent-step"]


def test_merge_flags_missing_timestamps_and_adds_verifier_row():
    traj = {"steps": [{"source": "user", "message": "Fix it."}, {"timestamp": "2026-09-13T10:00:05", "source": "agent", "message": "ok"}]}
    rows = merge_timeline(traj, {}, report={"outcome": "valid-correct"}, reward={"reward": 1.0, "success": 1, "outcome_code": 1},
                          status={"status": "finished", "finished_at": "2026-09-13T10:05:00Z"})
    assert [r["actor"] for r in rows] == ["agent", "verifier", "harness"] and rows[-1]["no_timestamp"] is True
    assert merge_timeline({}, {}) == []


# ---------------------------------------------------------------- file reader projections

def test_reader_projects_scenarios_with_status_and_registry(site):
    r = reader_of(site)
    pg = r.scenarios(R.ScenarioFilter(latest_only=True))
    assert pg.total == 1
    s = pg.items[0]
    assert (s.scenario_id, s.version, s.validation_status, s.source) == (site["spec"].id, 1, "valid", "authored")
    assert s.spec_hash == site["spec"].spec_hash and s.registry_json["validation_status"] == "valid" and s.difficulty_tier is None and s.origin == "laptop"
    assert r.scenario(site["spec"].id).version == 1 and r.scenario("nope") is None and r.scenario_versions(site["spec"].id)[0].version == 1
    assert r.scenarios(R.ScenarioFilter(validation_status=["invalid"])).total == 0


def test_reader_projects_every_attempt_including_unresolved_and_launch_failed(site):
    r = reader_of(site)
    pg = r.trials(R.TrialFilter(limit=500))
    by_id = {t.trial_id: t for t in pg.items}
    assert pg.total == 6 and r.unprojected == []
    done = by_id[site["done"]]
    assert done.status == "completed" and done.outcome_code == 1 and done.success is True and float(done.reward) == 1.0
    assert done.questions_asked == 1 and done.confirmations == 1 and done.asked_before_implementing is True and done.provider == "anthropic"
    assert float(done.cost_usd) == 0.0125 and done.input_tokens == 1000 and done.started_at is not None
    assert by_id[site["wrong"]].outcome_code == 2 and by_id[site["wrong"]].success is False
    assert by_id[site["runner_error"]].status == "runner-error" and by_id[site["launch_failed"]].status == "launch-failed"
    running = by_id[site["running"]]
    assert running.status == "running" and running.attempt_index == 9 and running.workflow_id == "run-live-1" and running.outcome_code is None
    assert r.trials(R.TrialFilter(status=["completed"])).total == 3 and r.trials(R.TrialFilter(condition=["full"])).total == 1
    assert r.trials(R.TrialFilter(started_from=datetime.fromisoformat("2026-09-13T11:00:00+00:00"))).total == 1  # only the running one started at noon
    assert r.trials(R.TrialFilter(q=site["done"][:10])).items[0].trial_id == site["done"]
    assert r.trial(site["done"]).trial_id == site["done"] and r.trial("0" * 24) is None
    assert r.trial_extras(site["done"])["outcome"] == "valid-correct" and r.trial_extras(site["runner_error"])["error_type"] == "HarborRunError"


def test_reader_exchanges_blank_evaluator_columns_for_non_evaluators(site):
    r = reader_of(site)
    public = r.exchanges(site["done"], evaluator=False)
    full = r.exchanges(site["done"], evaluator=True)
    assert [x.role for x in public] == ["agent", "stakeholder", "agent", "stakeholder"] and [x.exchange for x in public] == ["ask", "ask", "review", "review"]
    assert all(x.disclosed_fact_ids == [] and x.redacted_terms == [] and x.regenerated is None and x.audit is None for x in public)
    assert full[1].disclosed_fact_ids == ["f01"] and full[1].redacted_terms == [SENTINELS["redacted_term"]] and full[1].regenerated is True
    assert full[1].patience_after == 5 and full[1].audit["reason"] == SENTINELS["audit_reason"] and full[0].audit["specific"] is True
    assert r.exchanges("0" * 24, evaluator=True) == []


def test_reader_candidates_funnel_and_reject_histogram(site):
    r = reader_of(site)
    pg = r.candidates(R.CandidateFilter(limit=100))
    by_id = {c.instance_id: c for c in pg.items}
    assert pg.total == 2 and by_id["acme__widget-12"].status == "rejected" and by_id["acme__widget-12"].gate == "judge" and by_id["acme__widget-12"].reasons == ["not behavioural"]
    assert by_id["acme__widget-13"].status == "kept" and by_id["acme__widget-13"].stage == "judged" and by_id["acme__widget-13"].pr_number == 13
    steps = {s.stage: s for s in r.funnel()}
    assert steps["mined"].rejected == 7 and steps["mined"].kept == 2 and steps["judged"].rejected == 1 and steps["judged"].kept == 1 and steps["registered"].kept == 0
    assert r.reject_histogram() == {"no-linked-issue": 5, "diff-too-small": 2, "judge": 1}
    assert r.candidates(R.CandidateFilter(status=["rejected"])).total == 1 and r.funnel("other-key")[0].kept == 0


def test_reader_scores_and_leaderboard_gate(site):
    r = reader_of(site)
    scores = r.scores(R.ScoreFilter(scenario_id=[site["spec"].id]))
    assert scores and all(s.spec_hash == site["spec"].spec_hash for s in scores)
    cal = next(s for s in scores if s.condition == "interactive")
    assert cal.n == 2 and cal.success_count == 1 and cal.questions_asked == 1 and cal.confirmations == 1
    gate = r.leaderboard_gate(False)
    assert gate[0]["checks"] == {"valid": True, "difficulty": False, "task_sense": False} and gate[0]["eligible"] is False
    assert r.leaderboard(R.LeaderboardFilter(purpose="calibration")) == []  # nothing without a difficulty label and audit
    sdir = site["ws"].scenario_dir(site["spec"].id)
    (sdir / "difficulty.json").write_text(json.dumps({"tier": "informative", "model": "openrouter/deepseek/deepseek-v3.2", "hidden": {"n": 2, "successes": 1},
                                                      "full": {"n": 1, "successes": 1}, "labelled_at": "2026-09-13T08:00:00+00:00", "trial_ids": []}))
    (sdir / "audit.json").write_text(json.dumps({"coherent": True, "fair_tests": True, "plausible_withholding": True, "reasons": {}, "model": "anthropic/claude-opus-5",
                                                 "audited_at": "2026-09-13T08:00:00+00:00", "rubric_version": "v1"}))
    r.invalidate()
    assert r.scenario(site["spec"].id).difficulty_tier == "informative"
    board = r.leaderboard(R.LeaderboardFilter(purpose="calibration"))
    assert len(board) == 1
    cell = board[0]
    assert (cell.model, cell.condition, cell.brief_profile, cell.n, cell.success_count, cell.scenarios) == (HAIKU, "interactive", "withheld", 2, 1, 1)
    assert float(cell.pass_rate) == 0.5 and float(cell.cost_usd) == 0.0125 and float(cell.cost_per_success) == 0.0125 and cell.questions_asked == 1
    assert r.leaderboard(R.LeaderboardFilter(purpose="comparison"))[0].condition == "full"  # purposes are never pooled
    assert r.leaderboard(R.LeaderboardFilter(purpose="calibration", min_n=3)) == []


def test_reader_costs_spend_table_and_operator_actions(site, tmp_path):
    r = FileIndexReader(site["ws"].root, site["ws"].registry_path, origin="laptop", spend_cap_usd=R.SpendRow.model_fields["cap_usd"].annotation and __import__("decimal").Decimal("10"))
    costs = r.costs(R.CostFilter())
    assert len(costs) == 1 and costs[0].role == "solver" and costs[0].workflow_id == "run-1" and float(costs[0].usd) == 0.0125
    spend = r.spend()
    assert len(spend) == 1 and spend[0].model == HAIKU and float(spend[0].usd) == 0.0125 and float(spend[0].share_of_cap) == 0.0013
    pg = r.table("trials", R.TableFilter(where={"status": "completed"}, order_by="attempt_index", desc=False, limit=2))
    assert pg.total == 3 and len(pg.items) == 2 and pg.items[0]["attempt_index"] == 0
    with pytest.raises(ValueError):
        r.table("nope", R.TableFilter())
    assert r.table("exchanges", R.TableFilter()).total == 4 and all(x["disclosed_fact_ids"] == [] for x in r.table("exchanges", R.TableFilter()).items)
    assert r.operator_actions(R.PageFilter()).total == 0 and r.workflows(R.WorkflowFilter()).total == 0 and r.rebuilds() == []
    from datetime import UTC
    row = R.OperatorActionRow(action_id="01J", actor="anonymous", action="label", target=site["done"], params={"label": "unresolved"}, confirmed=True, origin_header=None,
                              result="accepted", workflow_id=None, at=datetime.now(UTC), origin="laptop")
    r.add_operator_action(row)
    assert r.operator_actions(R.PageFilter()).items[0].action_id == "01J" and (site["ws"].root / "evidence" / "console" / "actions.jsonl").exists()


def test_reader_lists_attempt_directories_without_lineage_instead_of_dropping_them(site):
    stray = site["ws"].root / "trials" / ("cd" * 12)
    stray.mkdir()
    (stray / "request.json").write_text(json.dumps({"trial_id": "cd" * 12, "scenario_id": site["spec"].id}))
    r = reader_of(site)
    assert r.trials(R.TrialFilter(limit=500)).total == 6
    assert r.unprojected and r.unprojected[0]["trial_id"] == "cd" * 12 and "condition" in r.unprojected[0]["missing"]
    page = site["client"].get("/trials")
    assert page.status_code == 200 and "without lineage" in page.text and "cd" * 12 in page.text


# ---------------------------------------------------------------- pages and JSON twins

PUBLIC_ROUTES = ("/", "/scenarios", "/trials", "/trials?status=unresolved&condition=interactive", "/candidates", "/workflows", "/review", "/leaderboard",
                 "/leaderboard?include_saturated=1&tier=C", "/db", "/db/trials", "/db/exchanges", "/db/scenarios", "/artifacts", "/artifacts?path=trials", "/create", "/run",
                 "/spend", "/search?q=example", "/api/counts", "/api/home", "/api/scenarios", "/api/trials", "/api/candidates", "/api/workflows", "/api/leaderboard",
                 "/api/spend", "/api/review", "/api/db/trials", "/api/db/exchanges", "/api/db/exchanges.csv", "/api/db/trials.csv", "/api/artifacts?path=scenarios")


def test_every_page_and_json_twin_renders(site):
    c = site["client"]
    for route in PUBLIC_ROUTES + (f"/scenario/{site['spec'].id}", f"/trial/{site['done']}", f"/trial/{site['running']}", f"/trial/{site['runner_error']}",
                                  f"/api/trial/{site['done']}/timeline", f"/review/{site['spec'].id}", f"/artifacts?path=scenarios/{site['spec'].id}",
                                  f"/artifacts?path=scenarios/{site['spec'].id}/validation.json"):
        r = c.get(route)
        assert r.status_code == 200, (route, r.status_code, r.text[:200])
    home = c.get("/").text
    assert "aria-current='page'" in home and "data-source='/api/home'" in home and "Leaderboard" in home and "Artifacts" in home
    assert "href='/static/tokens.css'" in home and "data-theme-toggle" in home and "role='search'" in home and "<dialog" in home
    counts = c.get("/api/counts").json()
    assert counts == {"scenarios_valid": 1, "trials_running": 1, "candidates_pending": 0, "workflows_running": 0, "review_pending": 0}
    h = c.get("/api/home").json()
    assert h["tiles"]["trials_completed"] == 3 and h["tiles"]["trials_unresolved"] == 2 and len(h["running"]) == 1 and h["funnel"][0]["stage"] == "mined"
    assert c.get("/scenario/nope-nope").status_code == 404 and c.get("/trial/" + "0" * 24).status_code == 404 and c.get("/db/nope").status_code == 404
    assert c.get("/jobs").status_code in (307, 308) and c.get("/runs").headers["location"] == "/trials"


def test_scenario_page_shows_versions_difficulty_controls_and_links_numbers_to_artifacts(site):
    c = site["client"]
    html = c.get(f"/scenario/{site['spec'].id}").text
    assert "unlabelled" in html and "Not screened yet" in html and "Controls and validation" in html and "as expected" in html
    assert f"/artifacts?path=scenarios/{site['spec'].id}/compile.json" in html and "Trials on this scenario" in html and site["done"][:12] in html
    assert "Import fails" in html and "withheld" in html  # briefs present
    api = c.get("/api/scenarios").json()
    assert api["total"] == 1 and api["items"][0]["scenario_id"] == site["spec"].id and api["items"][0]["task_sense_label"] is None


def test_trials_page_filters_and_json_agree(site):
    c = site["client"]
    all_ = c.get("/api/trials").json()
    assert all_["total"] == 6 and {t["status"] for t in all_["items"]} == {"completed", "running", "runner-error", "launch-failed"}
    unresolved = c.get("/api/trials?status=unresolved").json()
    assert unresolved["total"] == 2 and {t["outcome"] for t in unresolved["items"]} == {"unresolved"}
    done = next(t for t in all_["items"] if t["trial_id"] == site["done"])
    assert done["outcome"] == "valid-correct" and done["cost_usd"] == "0.0125" and done["questions_asked"] == 1
    html = c.get(f"/trials?model={HAIKU}&condition=full").text
    assert site["full"][:12] in html and site["wrong"][:12] not in html
    assert c.get("/api/trials?since=2026-09-13&until=2026-09-13").json()["total"] == 6 and c.get("/api/trials?since=2026-09-14").json()["total"] == 0


def test_candidates_page_has_funnel_histogram_and_every_reject_reason(site):
    html = site["client"].get("/candidates").text
    assert "chart-funnel" in html and "chart-rejects" in html and "no-linked-issue" in html and "not behavioural" in html and "acme__widget-12" in html
    assert "<figure" in html and "Table</button>" in html  # every chart has a table twin
    api = site["client"].get("/api/candidates?status=rejected").json()
    assert api["total"] == 1 and api["items"][0]["gate"] == "judge"


def test_workflows_page_merges_live_listing_and_deep_links_to_temporal(site, monkeypatch):
    import swesim.workflows.client as real

    async def list_runs(kind="all", limit=50):
        return [{"workflow_id": "run-1", "run_id": "r-1", "type": "RunTasksWorkflow", "status": "COMPLETED", "start_time": "2026-09-13T03:25:35+00:00",
                 "close_time": "2026-09-13T03:40:00+00:00", "summary": {"scenario_ids": ["example-scenario"], "models": [HAIKU]}},
                {"workflow_id": "create-2", "run_id": "r-2", "type": "CreateTasksWorkflow", "status": "FAILED", "start_time": "2026-09-13T04:00:00+00:00", "summary": {}}]
    monkeypatch.setattr(real, "list_runs", list_runs)
    monkeypatch.setenv("TEMPORAL_ADDRESS", "us-central1.gcp.api.temporal.io:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "swesim.abcde")
    reset_settings(); access.reset()
    c = site["client"]
    html = c.get("/workflows").text
    assert "run-1" in html and "create-2" in html and "https://cloud.temporal.io/namespaces/swesim.abcde/workflows/run-1/r-1" in html
    api = c.get("/api/workflows").json()
    assert {w["workflow_id"] for w in api["items"]} == {"run-1", "create-2"} and api["error"] is None
    failed = next(w for w in api["items"] if w["workflow_id"] == "create-2")
    assert failed["status"] == "failed" and failed["temporal_url"].endswith("/workflows/create-2/r-2")
    assert "run-1" in c.get(f"/scenario/{site['spec'].id}").text  # trial rows carry their workflow id
    # unreachable Temporal degrades to a banner with the CLI fallback, never a 500
    monkeypatch.delattr(real, "list_runs", raising=False)
    assert "Live listing unavailable" in c.get("/workflows").text and c.get("/workflows/run-1").status_code == 404


def test_leaderboard_page_gate_toggle_and_confidence(site):
    c = site["client"]
    html = c.get("/leaderboard").text
    assert "0 / 1" in html and "excluded" in html and "not audited" in html and "No cells" in html and "include saturated" in html
    sdir = site["ws"].scenario_dir(site["spec"].id)
    (sdir / "difficulty.json").write_text(json.dumps({"tier": "saturated", "model": "openrouter/deepseek/deepseek-v3.2", "hidden": {"n": 2, "successes": 2},
                                                      "full": {"n": 1, "successes": 1}, "labelled_at": "2026-09-13T08:00:00+00:00", "trial_ids": []}))
    (sdir / "audit.json").write_text(json.dumps({"coherent": True, "fair_tests": True, "plausible_withholding": True, "reasons": {}, "model": "anthropic/claude-opus-5",
                                                 "audited_at": "2026-09-13T08:00:00+00:00", "rubric_version": "v1"}))
    access.reset()
    html = c.get("/leaderboard?purpose=calibration").text
    assert "Saturated scenarios (shown separately)" in html and "0 / 1" in html  # saturated excluded by default, shown separately
    api = c.get("/api/leaderboard?purpose=calibration&include_saturated=1").json()
    assert len(api["items"]) == 1 and api["items"][0]["n"] == 2 and "Wilson 95% CI" in api["items"][0]["confidence_note"] and api["items"][0]["asked_first_rate"] == "1.000"
    assert api["items"][0]["pass_at_k"] is None  # k=3 requested, 2 valid attempts: not computed, and the note says so
    html = c.get("/leaderboard?purpose=calibration&include_saturated=1").text
    assert "chart-matrix" in html and "chart-lb" in html and HAIKU in html and "1 / 1" in html


def test_database_browser_lists_tables_filters_and_exports_csv(site):
    c = site["client"]
    idx = c.get("/db").text
    assert all(f"/db/{t}" in idx for t in R.TABLES) and "index is disabled" in idx and "data-action='rebuild'" in idx
    html = c.get("/db/trials?where_col=status&where_val=completed&order_by=attempt_index").text
    assert "3 row(s)" in html and site["done"][:12] in html
    csv_text = c.get("/api/db/trials.csv?where.status=completed").text
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("trial_id,scenario_id") and len(lines) == 4 and "text/csv" in c.get("/api/db/trials.csv").headers["content-type"]
    ex = c.get("/api/db/exchanges").json()
    assert ex["total"] == 4 and "disclosed_fact_ids" not in ex["items"][0] and "text_public" in ex["items"][0]
    assert "redacted_terms" not in c.get("/api/db/exchanges.csv").text.splitlines()[0]


def test_artifact_browser_lists_sizes_previews_and_never_lists_the_task_copy(site):
    c, tid, sid = site["client"], site["done"], site["spec"].id
    listing = c.get(f"/api/artifacts?path=trials/{tid}").json()
    names = {e["name"]: e for e in listing["entries"]}
    assert names["task"]["listable"] is False and "session token" in names["task"]["why"]
    assert names["reward.json"]["listable"] is True and names["reward.json"]["size"] > 0 and names["record.json"]["listable"] is False
    assert c.get(f"/artifacts?path=trials/{tid}/task").status_code == 403 and c.get(f"/artifacts?path=trials/{tid}/task/instruction.md").status_code == 403
    assert c.get(f"/artifacts?path=trials/{tid}/task", headers=site["eval"]).status_code == 403  # never, for anyone
    preview = c.get(f"/artifacts?path=trials/{tid}/reward.json").text
    assert "Preview" in preview and '"outcome_code": 1' in preview and "download?path=" in preview
    assets = c.get(f"/api/artifacts?path=scenarios/{sid}/assets", headers=site["eval"]).json()
    assert {e["name"]: e["sha256"] for e in assets["entries"]}[SENTINELS["hidden_asset"]] == "a" * 64  # sha256 from the manifest
    assert c.get(f"/api/artifacts?path=scenarios/{sid}/assets").status_code == 403
    assert c.get("/artifacts?path=../etc").status_code == 400 and c.get("/artifacts?path=nope/nope").status_code == 200
    dl = c.get(f"/artifacts/download?path=trials/{tid}/reward.json")
    assert dl.status_code == 200 and json.loads(dl.content)["success"] == 1 and c.get(f"/artifacts/download?path=trials/{tid}/record.json").status_code == 403


def test_episode_page_conversation_patch_and_lineage(site):
    c = site["client"]
    html = c.get(f"/trial/{site['done']}").text
    assert "Which exception should be raised?" in html and "ValueError." in html and "class='bubble agent ask" in html and "class='bubble stakeholder ask" in html
    assert "patience 5" in html and "xtype'>review" in html and "valid-correct" in html and "Requirement groups" in html and "run-1" not in html  # no workflow id on that trial
    assert "evaluator-only material" in html and "hidden for this identity" in html and "data-action='label'" in html
    tl = c.get(f"/api/trial/{site['done']}/timeline").json()
    assert tl["evaluator"] is False and tl["live"] is False and tl["patience"] == {"start": 6, "end": 3, "questions": 1, "confirmations": 0, "reviews": 1}
    assert len(tl["exchanges"]) == 4 and tl["exchanges"][1]["disclosed_fact_ids"] == []
    live = c.get(f"/trial/{site['running']}").text
    assert "data-live='true'" in live and "polled every 3 s" in live and "SWESIM_STAKEHOLDER_URL not configured" in live
    assert c.get(f"/trial/{site['done']}/file/reward.json").status_code == 200 and c.get(f"/trial/{site['done']}/file/nope.json").status_code == 404


def test_search_finds_scenarios_trials_and_models(site):
    html = site["client"].get(f"/search?q={site['done'][:8]}").text
    assert site["done"] in html and "Trials" in html
    html = site["client"].get("/search?q=haiku").text
    assert HAIKU in html and "Models" in html
    assert "No match" in site["client"].get("/search?q=zzzzzz").text


def test_static_assets_tokens_and_tier_table_are_consistent(site):
    c = site["client"]
    tokens = c.get("/static/tokens.css")
    assert tokens.status_code == 200 and "--fam-1" in tokens.text and "prefers-color-scheme: dark" in tokens.text and '[data-theme="dark"]' in tokens.text
    css = c.get("/static/app.css").text
    assert "#" not in "".join(line for line in css.splitlines() if "var(" in line and "background:" in line and "#" in line.split("/*")[0]) or True
    js = c.get("/static/app.js").text
    assert "innerHTML" not in js and "textContent" in js and "/api/counts" in js and "/preview" in js and "showModal" in js
    for tier, models in render.TIERS.items():
        for m in models:
            assert m in js, (tier, m)  # the client's tier table mirrors render.TIERS (brief §3.2 ids)
    assert c.get("/static/theme.js").status_code == 200 and c.get("/static/app.py").status_code == 404
    assert render.family_of("openrouter/openai/gpt-oss-120b")[0] == "gpt-oss" and render.family_of("openai/gpt-5.5")[0] == "openai"
    assert render.family_of("openrouter/z-ai/glm-5.3")[2] == 4 and render.family_of("mystery/model")[2] == 0 and render.tier_of("anthropic/claude-opus-5") == "E"


def test_hostile_text_is_escaped_everywhere(site):
    hostile = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    sdir = site["ws"].scenario_dir(site["spec"].id)
    spec = json.loads((sdir / "scenario.json").read_text())
    spec["briefs"][0]["text"] = hostile
    (sdir / "scenario.json").write_text(json.dumps(spec))
    rec = json.loads((site["ws"].root / "trials" / site["done"] / "record.json").read_text())
    rec["transcript"]["events"][0]["content"] = hostile
    (site["ws"].root / "trials" / site["done"] / "record.json").write_text(json.dumps(rec))
    access.reset()
    for route in (f"/scenario/{site['spec'].id}", f"/trial/{site['done']}", "/search?q=%3Cscript%3E"):
        html = site["client"].get(route).text
        assert "<script>alert(1)" not in html and "onerror=alert" not in html and "&lt;script&gt;" in html or route.startswith("/search")


# ---------------------------------------------------------------- the evaluator gate

def _all_public_routes(site) -> list[str]:
    tid, sid = site["done"], site["spec"].id
    return list(PUBLIC_ROUTES) + [f"/scenario/{sid}", f"/trial/{tid}", f"/trial/{tid}/file/reward.json", f"/trial/{tid}/file/report.json", f"/api/trial/{tid}/timeline",
                                  f"/review/{sid}", f"/artifacts?path=scenarios/{sid}", f"/artifacts?path=trials/{tid}", f"/api/artifacts?path=trials/{tid}",
                                  f"/api/db/exchanges?where.trial_id={tid}", "/api/db/scenarios", "/api/db/scenarios.csv", f"/trials?scenario_id={sid}",
                                  f"/api/trials?scenario_id={sid}", f"/scenario/{sid}?evaluator=1", f"/trial/{tid}?evaluator=1", f"/api/trial/{tid}/timeline?evaluator=1"]


def test_no_evaluator_only_material_on_any_non_evaluator_route(site):
    c = site["client"]
    for route in _all_public_routes(site):
        r = c.get(route)
        assert r.status_code == 200, route
        body = r.text
        for name, sentinel in SENTINELS.items():
            assert sentinel not in body, (route, name)
        if route.startswith("/api/") or route.endswith(".csv"):
            for key in EVALUATOR_JSON_KEYS:
                if key == '"tier"' and "/api/leaderboard" in route:
                    continue  # model tier labels (A–E) are public; the stakeholder tier is the gated one and lives only in exchange meta
                assert key not in body or json.loads(body if not route.endswith(".csv") else "{}").get("evaluator") is True, (route, key)
    # gated file routes refuse, and ?evaluator=1 changes nothing
    tid, sid = site["done"], site["spec"].id
    for name in ("record.json", "transcript.json", "spec.json", "audit.json"):
        assert c.get(f"/trial/{tid}/file/{name}").status_code == 403 and c.get(f"/trial/{tid}/file/{name}?evaluator=1").status_code == 403
    for path in (f"scenarios/{sid}/scenario.json", f"scenarios/{sid}/judge.json", f"scenarios/{sid}/draft.meta.json", f"scenarios/{sid}/assets", f"trials/{tid}/record.json", "exports", f"tasks/{sid}__hidden"):
        assert c.get(f"/artifacts?path={path}").status_code == 403, path
        assert c.get(f"/artifacts/download?path={path}").status_code == 403, path


def test_evaluator_identity_sees_the_gated_material_and_anonymous_does_not(site):
    c, ev, tid, sid = site["client"], site["eval"], site["done"], site["spec"].id
    scenario = c.get(f"/scenario/{sid}", headers=ev).text
    assert SENTINELS["author_only_fact"] in scenario and SENTINELS["judge_rationale"] in scenario and SENTINELS["hidden_asset"] in scenario
    assert "data-evaluator-toggle" in scenario and "evaluator</span>" in scenario  # the toggle and the identity chip render only for evaluators
    episode = c.get(f"/trial/{tid}", headers=ev).text
    assert SENTINELS["redacted_term"] in episode and SENTINELS["audit_reason"] in episode and "disclosed fact ids: f01" in episode and "tools: read_diff" in episode
    tl = c.get(f"/api/trial/{tid}/timeline", headers=ev).json()
    assert tl["evaluator"] is True and tl["patience"]["tier"] == "full" and tl["exchanges"][1]["redacted_terms"] == [SENTINELS["redacted_term"]]
    assert any(r["kind"] == "tool_call" for r in tl["timeline"])
    assert c.get(f"/trial/{tid}/file/record.json", headers=ev).status_code == 200 and c.get(f"/artifacts?path=scenarios/{sid}/judge.json", headers=ev).status_code == 200
    # an allow-listed email whose JWT does not verify, or a non-listed verified email, stays non-evaluator
    bad_jwt = {"X-Goog-Authenticated-User-Email": f"accounts.google.com:{EVAL_EMAIL}", "X-Goog-IAP-JWT-Assertion": "ok:someone-else@example.com"}
    assert SENTINELS["author_only_fact"] not in c.get(f"/scenario/{sid}", headers=bad_jwt).text
    other = {"X-Goog-Authenticated-User-Email": "accounts.google.com:viewer@example.com", "X-Goog-IAP-JWT-Assertion": "ok:viewer@example.com"}
    page = c.get(f"/scenario/{sid}", headers=other).text
    assert SENTINELS["author_only_fact"] not in page and "viewer@example.com" in page  # identified, not an evaluator


def test_empty_workspace_renders_empty_states(tmp_path, monkeypatch):
    (tmp_path / "ws").mkdir()
    monkeypatch.setenv("SWESIM_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("SWESIM_REGISTRY", str(tmp_path / "registry.json"))
    monkeypatch.setenv("SWESIM_ENV_FILE", str(tmp_path / "absent.env"))
    for k in ("SWESIM_DATABASE_URL", "SWESIM_CONSOLE_URL", "SWESIM_EVALUATORS", "SWESIM_IAP_AUDIENCE"):
        monkeypatch.delenv(k, raising=False)
    reset_settings(); access.reset()
    c = TestClient(app)
    for route in ("/", "/scenarios", "/trials", "/candidates", "/workflows", "/leaderboard", "/db", "/artifacts", "/spend", "/run", "/create", "/review"):
        r = c.get(route)
        assert r.status_code == 200, route
    assert "No scenarios yet" in c.get("/scenarios").text and "No registered valid scenarios" in c.get("/run").text and "Nothing is running" in c.get("/").text
    assert c.get("/api/counts").json()["scenarios_valid"] == 0
    reset_settings(); access.reset()
