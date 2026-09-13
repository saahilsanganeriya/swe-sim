"""Regression coverage for public projections, index-only lists and complete execution history."""
from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime

import pytest

from swesim.dashboard import console, rows as R
from swesim.settings import get_settings
from test_dashboard import SENTINELS, reader_of, site as site

routes = importlib.import_module("swesim.dashboard.app")


def test_nested_evaluator_metadata_is_removed_from_all_public_json_artifacts(site):
    tid = site["done"]
    trial = site["ws"].root / "trials" / tid
    report = json.loads((trial / "report.json").read_text())
    report["diagnostics"] = {"redacted_terms": [SENTINELS["redacted_term"]], "safe": "public result"}
    (trial / "report.json").write_text(json.dumps(report))
    record = json.loads((trial / "record.json").read_text())
    record["report"] = report
    record["transcript"]["events"][0]["extra"] = {"withheld_sentences": [SENTINELS["withheld_excerpt"]]}
    (trial / "record.json").write_text(json.dumps(record))
    urls = [f"/api/trial/{tid}/timeline", f"/trial/{tid}/file/report.json", f"/trial/{tid}",
            f"/artifacts?path=trials/{tid}/report.json", f"/artifacts/download?path=trials/{tid}/report.json"]
    for url in urls:
        public = site["client"].get(url)
        assert public.status_code == 200, url
        assert SENTINELS["redacted_term"] not in public.text, url
        assert SENTINELS["withheld_excerpt"] not in public.text, url
    assert SENTINELS["redacted_term"] in site["client"].get(urls[0], headers=site["eval"]).text


def test_database_projects_nested_fields_but_evaluator_can_inspect_them(site):
    reader = reader_of(site)
    original = reader.table

    def table(name, f):
        page = original(name, f)
        if name == "scenarios":
            for row in page.items:
                row["registry_json"] = {"draft_meta": {"note": SENTINELS["draft_meta"]}}
                row["task_sense"] = {"coherent": True, "reasons": {"note": SENTINELS["audit_reason"]}}
        return page

    reader.table = table
    routes.app.dependency_overrides[routes.reader_dep] = lambda: reader
    try:
        for url in ("/db/scenarios", "/api/db/scenarios", "/api/db/scenarios.csv"):
            assert SENTINELS["draft_meta"] not in site["client"].get(url).text
            assert SENTINELS["audit_reason"] not in site["client"].get(url).text
            assert SENTINELS["draft_meta"] in site["client"].get(url, headers=site["eval"]).text
        assert "redacted_terms" in site["client"].get("/api/db/exchanges", headers=site["eval"]).text
    finally:
        routes.app.dependency_overrides.clear()


def test_artifact_aliases_and_private_families_cannot_bypass_identity(site, tmp_path):
    root = site["ws"].root
    sibling = root.parent / (root.name + "-private")
    sibling.mkdir()
    (sibling / "private.json").write_text(json.dumps({"secret": SENTINELS["author_only_fact"]}))
    (root / "escape").symlink_to(sibling, target_is_directory=True)
    (root / "alias").symlink_to(root / "scenarios" / site["spec"].id / "judge.json")
    client = site["client"]
    for path in ("escape/private.json", "alias", "candidates", "jobs", "probes", "calibration", "evidence"):
        for prefix in ("/api/artifacts?path=", "/artifacts?path=", "/artifacts/download?path="):
            response = client.get(prefix + path)
            assert response.status_code == 403, (prefix, path, response.status_code)
    for headers in ({}, site["eval"]):
        result = client.get(f"/api/artifacts?path=trials/{site['done']}", headers=headers).json()
        assert "task" not in {entry["name"] for entry in result["entries"]}


def test_workflow_executions_keep_run_identity_and_filters(site):
    now = datetime.now(UTC)
    common = dict(parent_workflow_id=None, closed_at=None, task_queue=None, cost_usd=None, search_attributes={})
    rows = [R.WorkflowRow(workflow_id="repeat", run_id="first", type="CreateTasksWorkflow", status="failed", params_summary={},
                          failure="first failed", started_at=now, origin="laptop", **common),
            R.WorkflowRow(workflow_id="repeat", run_id="second", type="CreateTasksWorkflow", status="running", params_summary={},
                          started_at=now, failure=None, origin="laptop", **common)]

    class Reader:
        def workflows(self, f):
            selected = [r for r in rows if not f.status or r.status in f.status]
            return R.Page(items=selected[f.offset:f.offset + f.limit], total=len(selected), offset=f.offset, limit=f.limit)

    views, total = routes.workflow_views(Reader(), [{**rows[1].model_dump(mode="json"), "summary": "live"}], get_settings(), R.WorkflowFilter())
    assert total == 2 and {r["run_id"] for r in views} == {"first", "second"}
    assert all(f"run_id={v['run_id']}" in v["console_url"] for v in views)
    views, total = routes.workflow_views(Reader(), [rows[1].model_dump(mode="json")], get_settings(), R.WorkflowFilter(status=["failed"]))
    assert total == 1 and [r["run_id"] for r in views] == ["first"]


def test_bad_browser_filters_and_origins_return_client_errors(site):
    for url in ("/api/trials?status=bogus", "/api/db/trials?order_by=bogus", "/api/db/trials?where.bogus=x"):
        assert site["client"].get(url).status_code == 400, url
    for origin in ("http://testserver:bad", "http://testserver/path", "http://user@testserver", "http://[broken"):
        response = site["client"].post("/api/rebuild/preview", json={}, headers={"Origin": origin})
        assert response.status_code == 403, origin


def test_review_counts_do_not_scan_workspace(site, monkeypatch):
    monkeypatch.setattr(console, "local_drafts", lambda *args: pytest.fail("list view scanned workspace"))
    assert site["client"].get("/api/counts").status_code == 200
    assert site["client"].get("/api/review").status_code == 200


def test_model_defaults_follow_current_shared_roles():
    from swesim import cli
    from swesim.models import StakeholderConfig
    defaults = console.defaults()
    assert defaults["judge_model"] == cli.JUDGE_MODEL_DEFAULT
    assert defaults["draft_model"] == cli.DRAFT_MODEL_DEFAULT
    assert defaults["solver_model"] == cli.SOLVER_MODEL_DEFAULT
    assert defaults["stakeholder_model"] == StakeholderConfig().model_id


def test_leaderboard_scores_are_bound_to_admitted_spec_and_configuration(site):
    from decimal import Decimal
    sdir = site["ws"].scenario_dir(site["spec"].id)
    (sdir / "difficulty.json").write_text(json.dumps({"tier": "informative"}))
    (sdir / "audit.json").write_text(json.dumps({"coherent": True, "fair_tests": True}))
    reader = reader_of(site)
    original = reader.scores
    calls = []

    def scores(f):
        calls.append(f)
        selected = original(f)
        # A different purpose/config has a perfect score; an unbound query would include it.
        if not f.run_config_hash and selected:
            selected.append(selected[0].model_copy(update={"run_config_hash": "other", "pass_at_k": Decimal(1), "pass_pow_k": Decimal(1)}))
        return selected

    reader.scores = scores
    f = R.LeaderboardFilter(purpose="calibration")
    cells = routes.board_views(reader, reader.leaderboard(f), {site["spec"].id}, f)
    assert cells[0]["n"] == 2 and cells[0]["pass_at_k"] is None
    assert calls and all(f.spec_hash == [site["spec"].spec_hash] and f.run_config_hash for f in calls)


def test_iap_verifier_checks_issuer_in_addition_to_signature_and_audience(monkeypatch):
    from google.oauth2 import id_token
    from swesim.dashboard.identity import _default_verify, IAP_CERTS_URL
    seen = {}

    def verify(token, request, **kwargs):
        seen.update(kwargs)
        return {"email": "person@example.com", "iss": "https://accounts.google.com"}

    monkeypatch.setattr(id_token, "verify_token", verify)
    with pytest.raises(ValueError, match="issuer"):
        _default_verify("token", "audience")
    assert seen == {"audience": "audience", "certs_url": IAP_CERTS_URL}


def test_screen_defaults_and_review_approval_cannot_bypass_temporal(site):
    from swesim.roles import ROLE_CHAINS
    sid = site["spec"].id
    params, errors = console.validate_run({"scenario_ids": [sid], "purpose": "screen"}, {sid})
    assert not errors and params["models"] == [ROLE_CHAINS["screen_model"][0]]
    assert params["conditions"] == ["hidden", "full"] and params["k"] == 2
    response = site["client"].post("/api/review/preview", json={"scenario_id": sid, "decision": "approve", "reason": "reviewed"})
    assert response.status_code == 400 and "holding workflow" in response.text


def test_cross_origin_rejections_are_audited_without_executing(site):
    response = site["client"].post("/api/rebuild/preview", headers={"Origin": "https://other.example"}, json={})
    assert response.status_code == 403
    rows = reader_of(site).operator_actions(R.PageFilter()).items
    assert len(rows) == 1 and rows[0].action == "rebuild" and rows[0].result == "rejected" and not rows[0].confirmed
