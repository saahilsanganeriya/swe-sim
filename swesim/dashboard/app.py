"""swesim console: the operator surface for the whole record (FastAPI, server-rendered HTML + vanilla JS, no build step).

Pages answer one question each (design spec §1): Home (what is running and what changed), Scenarios, Trials/Episode,
Candidates, Workflows, Review, Leaderboard, Database, Artifacts, Create/Run, Spend, Search. Every list, filter, funnel
and score comes from the ``IndexReader`` (``access.reader_for``); the artifact browser is the one page that walks the
filesystem through ``Layout``. Live updates poll the ``/api/*`` JSON twins of each page.

Identity: IAP headers verified by ``identity.identity_from_headers`` (evaluator = allow-listed email); anonymous otherwise.
Evaluator-only material (facts, redactions, tool audit, judge/draft metadata, hidden assets, raw record/transcript/spec)
is never in a non-evaluator response; ``?evaluator=1`` does not exist.

Mutations (``/api/<action>/preview`` then ``/api/<action>``): strict Origin check (scheme+host+port), signed confirmation
token bound to the params and actor, an ``OperatorActionRow`` for every attempt, and the CLI equivalent in every reply.
Temporal is reached only through ``console.py`` (``ConsoleUnavailable`` becomes a 502 carrying the CLI fallback).
Configuration comes from ``swesim.settings.Settings``; secrets are read at the point of use and never rendered.

File convention this consumer depends on (tests/test_contracts.py): the episode page reads ``trials/<tid>/record.json``,
``reward.json``, ``report.json``, ``transcript.json``, ``status.json``, ``run_config.json`` and ``spec.json`` exactly as
``swesim/stages/run.py`` writes them (through ``data.load_trial``); ``merge_timeline`` is re-exported from ``data``.
"""
from __future__ import annotations

import asyncio
import csv
import io
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from swesim.dashboard import access, actions, console, data, pages, render, render_console, render_episode, render_scenario
from swesim.dashboard import health as health_cache, render_trends, trends
from swesim.dashboard import rows as R
from swesim.dashboard.data import merge_timeline  # noqa: F401 - contract re-export (tests/test_contracts.py)
from swesim.dashboard.identity import Identity, identity_from_headers
from swesim.dashboard.public import PRIVATE_KEYS, project
from swesim.dashboard.access import all_rows
from swesim.index.cohorts import cohort_of
from swesim.settings import Settings, get_settings

app = FastAPI(title="swesim console", docs_url=None, redoc_url=None)
app.state.verify_jwt = None  # tests inject a verifier; None means google.auth
STATIC = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {"tokens.css": "text/css; charset=utf-8", "app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                "theme.js": "text/javascript; charset=utf-8"}
LIVE_LIST_TIMEOUT = 5.0
LABELS = render_episode.LABELS

__all__ = ["app", "merge_timeline", "settings_dep", "reader_dep", "identity_dep"]


@app.exception_handler(ValueError)
async def invalid_input(_request: Request, error: ValueError) -> JSONResponse:
    return _json(400, {"error": str(error)})


@app.exception_handler(PermissionError)
async def denied_artifact(_request: Request, error: PermissionError) -> JSONResponse:
    return _json(403, {"error": str(error)})


# ----------------------------------------------------------------------------- dependencies

def settings_dep() -> Settings:
    return get_settings()


def reader_dep(settings: Settings = Depends(settings_dep)):
    return access.reader_for(settings)


def layout_dep(settings: Settings = Depends(settings_dep)):
    return access.layout_for(settings)


def identity_dep(request: Request, settings: Settings = Depends(settings_dep)) -> Identity:
    return identity_from_headers(request.headers, settings, verify=app.state.verify_jwt)


def origin_dep(request: Request, settings: Settings = Depends(settings_dep), reader=Depends(reader_dep), identity: Identity = Depends(identity_dep)) -> None:
    if settings.bucket and identity.anonymous:
        action = request.path_params.get("action")
        if action in actions.ACTIONS:
            access.record_action(reader, settings, actions.audit_row(identity=identity, action=action, target=None, params={}, confirmed=False,
                                                                     origin_header=request.headers.get("origin"), result="rejected", workflow_id=None, settings=settings))
        raise HTTPException(403, detail="A verified IAP operator identity is required")
    try:
        actions.check_origin(request.headers, settings, str(request.url))
    except actions.OriginError as e:
        action = request.path_params.get("action")
        if action in actions.ACTIONS:
            access.record_action(reader, settings, actions.audit_row(identity=identity, action=action, target=None, params={}, confirmed=False,
                                                                     origin_header=request.headers.get("origin"), result="rejected", workflow_id=None, settings=settings))
        raise HTTPException(403, detail=str(e)) from e


def _json(status: int, body: dict) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _dump(obj) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


def _lists(request: Request, *keys: str) -> dict[str, list[str] | None]:
    out: dict[str, list[str] | None] = {}
    for k in keys:
        vals = [v for v in request.query_params.getlist(k) if v]
        out[k] = vals or None
    return out


def _ints(vals: list[str] | None) -> list[int] | None:
    if not vals:
        return None
    out = []
    for v in vals:
        try:
            out.append(int(v))
        except ValueError:
            continue
    return out or None


def _date(v: str | None, end: bool = False) -> datetime | None:
    if not v:
        return None
    try:
        d = date.fromisoformat(v)
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC) if end else datetime(d.year, d.month, d.day, tzinfo=UTC)


def _paging(request: Request, default_limit: int = 100) -> tuple[int, int]:
    try:
        offset = max(0, int(request.query_params.get("offset", 0)))
        limit = min(500, max(1, int(request.query_params.get("limit", default_limit))))
    except ValueError:
        offset, limit = 0, default_limit
    return offset, limit


def page(p: render.Page, body: str, reader, identity: Identity, settings: Settings) -> HTMLResponse:
    return HTMLResponse(render.layout(p, body, counts=counts(reader, settings), identity=identity, temporal_url=settings.workflow_url("").rsplit("/workflows/", 1)[0] if settings.workflow_url("") else None))


# ----------------------------------------------------------------------------- counts and home data

def counts(reader, settings: Settings) -> dict:
    scen = reader.scenarios(R.ScenarioFilter(validation_status=["valid"], latest_only=True, limit=1))
    running = reader.trials(R.TrialFilter(status=["running", "launched"], limit=1))
    pending = reader.candidates(R.CandidateFilter(status=["pending"], limit=1))
    wf = reader.workflows(R.WorkflowFilter(status=["running"], limit=1))
    review = reader.scenarios(R.ScenarioFilter(validation_status=["drafting"], latest_only=True, limit=1)).total
    return {"scenarios_valid": scen.total, "trials_running": running.total, "candidates_pending": pending.total, "workflows_running": wf.total, "review_pending": review}


async def live_jobs(settings: Settings | None = None) -> tuple[list[dict], str | None]:
    """The Temporal listing through console.py, bounded in time; (jobs, error)."""
    if settings is not None and settings.index_enabled:
        return [], None
    try:
        return await asyncio.wait_for(console.list_jobs(), LIVE_LIST_TIMEOUT), None
    except console.ConsoleUnavailable as e:
        return [], f"{e} · {e.cli}"
    except asyncio.TimeoutError:
        return [], "Temporal listing timed out"


def workflow_views(reader, live: list[dict], settings: Settings, f: R.WorkflowFilter) -> tuple[list[dict], int]:
    indexed = reader.workflows(f)
    if not live:
        return [pages.workflow_view(project(_dump(w)), settings) for w in indexed.items], indexed.total
    by_id: dict[tuple[str, str], dict] = {}
    for w in all_rows(reader.workflows, f):
        d = project(_dump(w))
        by_id[d["workflow_id"], d["run_id"]] = d
    for j in live:
        j = project(j)
        if any(getattr(f, field) and j.get(field) not in getattr(f, field) for field in ("workflow_id", "run_id", "status", "type", "parent_workflow_id", "task_queue")):
            continue
        if f.q and f.q.lower() not in json.dumps(j).lower():
            continue
        key = j["workflow_id"], j.get("run_id", "")
        cur = by_id.get(key, {})
        merged = {**cur, **{k: v for k, v in j.items() if v not in (None, "", {}) or k not in cur}}
        merged.setdefault("params_summary", j.get("params_summary") or {})
        by_id[key] = merged
    views = [pages.workflow_view(w, settings) for w in by_id.values()]
    views.sort(key=lambda w: str(w.get("started_at") or ""), reverse=True)
    return views[f.offset:f.offset + f.limit], len(views)


async def home_data(reader, settings: Settings) -> dict:
    c = counts(reader, settings)
    running = reader.trials(R.TrialFilter(status=["running", "launched", "planned"], limit=100))
    completed = reader.trials(R.TrialFilter(status=["completed"], limit=1))
    unresolved = reader.trials(R.TrialFilter(status=["unresolved", "runner-error", "launch-failed", "unit-failed", "cancelled"], limit=1))
    recent = reader.trials(R.TrialFilter(order_by="finished_at", desc=True, limit=15))
    live, err = await live_jobs(settings)
    wviews, _ = workflow_views(reader, live, settings, R.WorkflowFilter(limit=15))
    spend = reader.spend()
    total = sum((Decimal(str(s.usd)) for s in spend), Decimal(0))
    tiles = {**c, "trials_completed": completed.total, "trials_unresolved": unresolved.total, "spend_usd": float(total), "spend_text": f"${total:,.2f}"}
    funnel = [_dump(s) for s in reader.funnel()]
    return {"tiles": tiles, "running": [pages.trial_view(t) for t in running.items],
            "recent": [pages.trial_view(t) for t in recent.items if t.status != "running"],
            "workflows": wviews[:15], "workflow_error": err, "funnel": funnel,
            "charts": {"chart-home-funnel": pages.charts.funnel(funnel, "chart-home-funnel")},
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds")}


# ----------------------------------------------------------------------------- pages: record

@app.get("/", response_class=HTMLResponse)
async def home(reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    d = await home_data(reader, settings)
    p = render.Page("Home", "home", "home", attrs={"source": "/api/home", "chart-target": "home-charts", "poll-ms": "5000"}, live="updated 0s ago")
    return page(p, pages.home_body(d, settings), reader, identity, settings)


@app.get("/api/home")
async def api_home(reader=Depends(reader_dep), settings: Settings = Depends(settings_dep)) -> JSONResponse:
    return _json(200, await home_data(reader, settings))


@app.get("/api/counts")
def api_counts(reader=Depends(reader_dep), settings: Settings = Depends(settings_dep)) -> JSONResponse:
    return _json(200, counts(reader, settings))


@app.get("/scenarios", response_class=HTMLResponse)
def scenarios(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    offset, limit = _paging(request, 200)
    f = R.ScenarioFilter(**_lists(request, "validation_status", "source", "difficulty_tier"), latest_only=request.query_params.get("latest") == "1", offset=offset, limit=limit)
    pg = reader.scenarios(f)
    p = render.Page("Scenarios", "scenarios", "scenarios", crumbs=(("Scenarios", None),))
    return page(p, render_scenario.scenarios_body(pg, counts(reader, settings)), reader, identity, settings)


@app.get("/api/scenarios")
def api_scenarios(request: Request, reader=Depends(reader_dep)) -> JSONResponse:
    offset, limit = _paging(request, 200)
    pg = reader.scenarios(R.ScenarioFilter(**_lists(request, "validation_status", "source", "difficulty_tier"), offset=offset, limit=limit))
    return _json(200, {"items": [render_scenario.scenario_row_view(r) for r in pg.items], "total": pg.total, "offset": pg.offset, "limit": pg.limit})


@app.get("/scenario/{sid}", response_class=HTMLResponse)
def scenario(sid: str, request: Request, reader=Depends(reader_dep), layout=Depends(layout_dep), identity: Identity = Depends(identity_dep),
             settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    if not access.ID_RE.fullmatch(sid):
        raise HTTPException(404)
    version = _ints([request.query_params.get("version", "")])
    row = reader.scenario(sid, version[0] if version else None)
    versions = reader.scenario_versions(sid)
    archived = row is not None and row.validation_status == "archived"
    d = data.scenario_detail(layout, sid, identity.evaluator, row.version if archived else None)
    if d is None and row is None:
        raise HTTPException(404)
    if d is None:  # registered, no directory in this copy
        d = {"scenario_id": sid, "version": row.version, "frozen": False, "spec_hash": row.spec_hash, "source": row.source, "source_ref": row.source_ref, "created_at": None,
             "natural_segments": [("", False)], "withheld": None, "removed_excerpts": [], "operations": [], "ledger_groups": {}, "hidden_author_only": 0, "evaluator": identity.evaluator,
             "requirements": [], "groups": [], "checks": [], "persona": {}, "environment": {}, "contract": {"fail_to_pass": 0, "pass_to_pass": 0, "protected_paths": 0},
             "controls": [], "validation": {}, "failures": [], "probe": {}, "approval": {}, "compile": {}, "assets": {}, "hidden_assets": 0, "difficulty": {}, "audit": {}, "judge": {},
             "draft_meta": {}, "review": {}, "files": [], "dir": None}
    trials = reader.trials(R.TrialFilter(scenario_id=[sid], limit=500))
    trials_html = render.table("scenario-trials", pages.TRIAL_COLUMNS, [pages.trial_view(t) for t in trials.items], key="trial_id",
                               empty="<strong>No trials on this scenario yet.</strong> Start one on <a href='/run'>Run</a>.", export=True)
    p = render.Page(f"Scenario {sid}", "scenarios", "scenario", crumbs=(("Scenarios", "/scenarios"), (sid, None)))
    return page(p, render_scenario.scenario_body(d, row, versions, trials_html, identity.evaluator), reader, identity, settings)


def _trial_filter(request: Request, default_limit: int = 200) -> R.TrialFilter:
    offset, limit = _paging(request, default_limit)
    lists = _lists(request, "scenario_id", "model", "condition", "brief_profile", "status", "purpose", "origin", "trial_id")
    status = lists.pop("status")
    if status and "unresolved" in status:
        status = sorted(set(status) | {"runner-error", "launch-failed", "unit-failed", "cancelled"})
    return R.TrialFilter(**lists, status=status, version=_ints(request.query_params.getlist("version")), outcome_code=_ints(request.query_params.getlist("outcome_code")),
                         started_from=_date(request.query_params.get("since")), started_to=_date(request.query_params.get("until"), end=True),
                         order_by=request.query_params.get("order_by") or None, desc=request.query_params.get("asc") != "1", offset=offset, limit=limit)


@app.get("/trials", response_class=HTMLResponse)
def trials(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    f = _trial_filter(request)
    pg = reader.trials(f)
    views = [pages.trial_view(t) for t in pg.items]
    everything = reader.trials(R.TrialFilter(limit=500)).items
    facets = {"scenario_id": sorted({t.scenario_id for t in everything}), "version": sorted({t.version for t in everything if t.version is not None}),
              "model": sorted({t.model for t in everything}), "status": sorted({t.status for t in everything} | {"unresolved"}),
              "outcome_code": sorted({t.outcome_code for t in everything if t.outcome_code is not None}), "origin": sorted({t.origin for t in everything})}
    current = {k: request.query_params.getlist(k) for k in ("scenario_id", "version", "model", "condition", "brief_profile", "status", "outcome_code", "purpose", "origin")}
    current.update(since=request.query_params.get("since"), until=request.query_params.get("until"))
    p = render.Page("Trials", "trials", "trials", crumbs=(("Trials", None),))
    return page(p, pages.trials_body(pg, views, facets, current), reader, identity, settings)


@app.get("/api/trials")
def api_trials(request: Request, reader=Depends(reader_dep)) -> JSONResponse:
    pg = reader.trials(_trial_filter(request))
    return _json(200, {"items": [pages.trial_view(t) for t in pg.items], "total": pg.total, "offset": pg.offset, "limit": pg.limit})


@app.get("/candidates", response_class=HTMLResponse)
def candidates(request: Request, reader=Depends(reader_dep), layout=Depends(layout_dep), identity: Identity = Depends(identity_dep),
               settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    offset, limit = _paging(request, 200)
    key = request.query_params.get("source_key") or None
    f = R.CandidateFilter(source_key=[key] if key else None, **_lists(request, "stage", "status", "gate"), offset=offset, limit=limit)
    pg = reader.candidates(f)
    steps = [_dump(s) for s in reader.funnel(key)]
    everything = list(all_rows(reader.candidates, R.CandidateFilter()))
    hist = pages.reject_histogram(reader, everything, key)
    keys = {r.source_key for r in everything}
    if "discoveries" in R.TABLES:
        keys.update(row["source_key"] for row in all_rows(lambda f: reader.table("discoveries", f), R.TableFilter()))
    keys = sorted(keys)
    p = render.Page("Candidates", "candidates", "candidates", crumbs=(("Candidates", None),),
                    attrs={"source": "/api/candidates?" + str(request.query_params), "chart-target": "candidate-charts", "poll-ms": "8000"}, live="updated 0s ago")
    body = pages.candidates_body(pg, [pages.candidate_view(r) for r in pg.items], steps, hist, keys, key)
    if not callable(getattr(reader, "reject_histogram", None)):
        body += "<p class='muted'>Discovery-time reject counts are unavailable in this reader; the histogram shows indexed candidate gates.</p>"
    return page(p, body, reader, identity, settings)


@app.get("/api/candidates")
def api_candidates(request: Request, reader=Depends(reader_dep)) -> JSONResponse:
    offset, limit = _paging(request, 200)
    key = request.query_params.get("source_key") or None
    pg = reader.candidates(R.CandidateFilter(source_key=[key] if key else None, **_lists(request, "stage", "status", "gate"), offset=offset, limit=limit))
    steps = [_dump(s) for s in reader.funnel(key)]
    rows = list(all_rows(reader.candidates, R.CandidateFilter(source_key=[key] if key else None)))
    hist = pages.reject_histogram(reader, rows, key)
    return _json(200, {"items": [pages.candidate_view(r) for r in pg.items], "total": pg.total, "funnel": steps,
                       "charts": {"funnel": pages.charts.funnel(steps, "chart-funnel"),
                                  "rejects": pages.charts.hbars(list(hist.items()), "chart-rejects", "rejects by gate")}})


@app.get("/workflows", response_class=HTMLResponse)
async def workflows(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    offset, limit = _paging(request, 200)
    live, err = await live_jobs(settings)
    views, total = workflow_views(reader, live, settings, R.WorkflowFilter(**_lists(request, "status", "type"), offset=offset, limit=limit))
    p = render.Page("Workflows", "workflows", "workflows", crumbs=(("Workflows", None),), attrs={"poll-ms": "8000"}, live="updated 0s ago")
    return page(p, pages.workflows_body(views, err, total, settings), reader, identity, settings)


@app.get("/api/workflows")
async def api_workflows(request: Request, reader=Depends(reader_dep), settings: Settings = Depends(settings_dep)) -> JSONResponse:
    offset, limit = _paging(request, 200)
    live, err = await live_jobs(settings)
    views, total = workflow_views(reader, live, settings, R.WorkflowFilter(**_lists(request, "status", "type"), offset=offset, limit=limit))
    return _json(200, {"items": views, "total": total, "error": err})


def _wid(wid: str) -> str:
    if not console.WORKFLOW_ID_RE.fullmatch(wid):
        raise HTTPException(404)
    return wid


@app.get("/workflows/{wid}", response_class=HTMLResponse)
async def workflow(wid: str, request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    wid = _wid(wid)
    run_id = request.query_params.get("run_id")
    matches = reader.workflows(R.WorkflowFilter(workflow_id=[wid], run_id=[run_id] if run_id else None, limit=1)).items
    row = matches[0] if matches else None
    live, _err = await live_jobs(settings)
    job = next((j for j in live if j["workflow_id"] == wid and (not run_id or j.get("run_id") == run_id)), None)
    progress, perr = None, None
    try:
        if not run_id and (row is None or row.status == "running"):
            progress = await asyncio.wait_for(console.job_progress(wid), LIVE_LIST_TIMEOUT)
    except console.ConsoleUnavailable as e:
        perr = f"{e} · {e.cli}"
    except asyncio.TimeoutError:
        perr = "progress query timed out"
    if row is None and job is None and progress is None:
        raise HTTPException(404)
    children = [pages.workflow_view(_dump(w), settings) for w in reader.workflows(R.WorkflowFilter(parent_workflow_id=[wid], limit=200)).items]
    trials = [pages.trial_view(t) for t in reader.trials(R.TrialFilter(limit=500)).items if t.workflow_id == wid]
    p = render.Page(f"Workflow {wid}", "workflows", "workflow", crumbs=(("Workflows", "/workflows"), (wid, None)))
    if not identity.evaluator:
        row = row.model_copy(update={"params_summary": project(row.params_summary)}) if row else None
        job, progress = project(job), project(progress)
    return page(p, pages.workflow_body(wid, row, job, progress, perr, children, trials, settings, console.redact), reader, identity, settings)


@app.get("/api/workflows/{wid}")
async def api_workflow(wid: str, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> JSONResponse:
    wid = _wid(wid)
    try:
        progress = await asyncio.wait_for(console.job_progress(wid), LIVE_LIST_TIMEOUT)
    except console.ConsoleUnavailable as e:
        return _json(502, {"ok": False, "error": str(e), "cli": e.cli})
    except asyncio.TimeoutError:
        return _json(504, {"ok": False, "error": "progress query timed out"})
    row = reader.workflow(wid)
    result = {"ok": True, "workflow_id": wid, "progress": progress, "row": _dump(row), "temporal_url": settings.workflow_url(wid, row.run_id if row else None)}
    return _json(200, result if identity.evaluator else project(result))


# compatibility redirects for the previous console's paths
@app.get("/jobs")
def jobs_redirect() -> RedirectResponse:
    return RedirectResponse("/workflows", status_code=308)


@app.get("/jobs/{wid}")
def job_redirect(wid: str) -> RedirectResponse:
    return RedirectResponse(f"/workflows/{_wid(wid)}", status_code=308)


@app.get("/runs")
def runs_redirect() -> RedirectResponse:
    return RedirectResponse("/trials", status_code=308)


# ----------------------------------------------------------------------------- leaderboard

def _lb_filter(request: Request, reader) -> tuple[R.LeaderboardFilter, dict]:
    lists = _lists(request, "brief_profile", "source", "condition", "tier")
    tiers = lists.pop("tier")
    include_sat = request.query_params.get("include_saturated") == "1"
    difficulty = request.query_params.get("difficulty_tier") or None
    if difficulty and difficulty not in ("informative", "saturated"):
        raise HTTPException(400, "difficulty_tier must be informative or saturated")
    if difficulty == "saturated":
        include_sat = True
    purpose = request.query_params.get("purpose") or "comparison"
    if purpose not in ("calibration", "comparison", "demonstration", "screen"):
        purpose = "comparison"
    models = sorted({m for t in (tiers or []) for m in render.TIERS.get(t, ())}) or None  # model tier -> the brief's verbatim ids
    requested_models = _lists(request, "model")["model"]
    if requested_models:
        models = [m for m in requested_models if not tiers or m in (models or [])] or ["__no_model__"]
    metric_k = request.query_params.get("metric_k") or None
    f = R.LeaderboardFilter(**lists, model=models, purpose=purpose, include_saturated=include_sat,
                           evaluation_cohort=request.query_params.get("evaluation_cohort") or None,
                           metric_k=int(metric_k) if metric_k else None)
    if difficulty:
        ids = [s.scenario_id for s in all_rows(reader.scenarios, R.ScenarioFilter(difficulty_tier=[difficulty]))]
        f = f.model_copy(update={"scenario_id": ids or ["__no_scenarios__"]})
    current = {k: v or [] for k, v in lists.items()}
    options = dict(cohort_of(t) for t in all_rows(reader.trials, R.TrialFilter(purpose=[purpose], status=["completed"])))
    current.update(tier=tiers or [], include_saturated=include_sat, purpose=purpose, difficulty_tier=[difficulty] if difficulty else [],
                   evaluation_cohort=f.evaluation_cohort, cohort_options=sorted(options.items()), metric_k=f.metric_k)
    return f, current


def leaderboard_gate(reader, include_saturated: bool) -> list[dict]:
    """Interfaces §9.1 per current scenario version, with the per-check breakdown the page explains; ``eligible`` is the
    reader's own ``reports.gate_passes`` so the board and its explanation can never disagree."""
    from swesim.index.reports import gate_passes
    out = []
    for s in all_rows(reader.scenarios, R.ScenarioFilter(latest_only=True)):
        ts = s.task_sense if isinstance(s.task_sense, dict) else {}
        checks = {"valid": s.validation_status == "valid",
                  "difficulty": s.difficulty_tier == "informative" or (include_saturated and s.difficulty_tier == "saturated"),
                  "task_sense": bool(ts.get("coherent")) and bool(ts.get("fair_tests"))}
        out.append({"scenario_id": s.scenario_id, "version": s.version, "validation_status": s.validation_status, "difficulty_tier": s.difficulty_tier, "source": s.source,
                    "task_sense": {k: ts.get(k) for k in ("coherent", "fair_tests", "plausible_withholding")} if ts else None, "checks": checks,
                    "eligible": gate_passes(s, include_saturated)})
    return out


def _wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n <= 0:
        return None
    import math
    p = successes / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def board_trials(reader, f: R.LeaderboardFilter) -> list:
    """Completed, attributable trials admitted by the shared version-aware leaderboard gate."""
    from swesim.index.reports import gate_passes, scenario_for_trial
    versions = {}
    for s in all_rows(reader.scenarios, R.ScenarioFilter(scenario_id=f.scenario_id)):
        versions.setdefault(s.scenario_id, []).append(s)
    selected = []
    for t in all_rows(reader.trials, R.TrialFilter(scenario_id=f.scenario_id, purpose=[f.purpose], status=["completed"],
                                                 condition=f.condition, brief_profile=f.brief_profile, model=f.model, outcome_code=[1, 2, 3, 4])):
        s = scenario_for_trial(t, versions.get(t.scenario_id, []))
        if f.evaluation_cohort and cohort_of(t)[0] != f.evaluation_cohort:
            continue
        if s is not None and gate_passes(s, f.include_saturated) and (not f.source or s.source in f.source):
            selected.append(t)
    return selected


def board_views(reader, rows: list, eligible: set[str], f: R.LeaderboardFilter) -> list[dict]:
    """Recompute requested estimators within each admitted spec/config/cohort group."""
    from swesim.stages.score import pass_at_k, pass_pow_k
    trials = [t for t in board_trials(reader, f) if t.scenario_id in eligible]
    views = []
    for r in rows:
        d = r.model_dump(mode="json")
        selected = [t for t in trials if (t.model, t.condition, t.brief_profile, cohort_of(t)[0]) ==
                    (r.model, r.condition, r.brief_profile, r.evaluation_cohort)]
        groups = {}
        for t in selected:
            groups.setdefault((t.scenario_id, t.spec_hash, t.run_config_hash), []).append(t)
        estimates = []
        for (sid, spec_hash, config_hash), attempts in groups.items():
            scores = reader.scores(R.ScoreFilter(scenario_id=[sid], spec_hash=[spec_hash], run_config_hash=[config_hash],
                                                 condition=[r.condition], brief_profile=[r.brief_profile], model=[r.model]))
            metric_k = f.metric_k or (scores[0].requested_k if scores else None)
            if metric_k is not None:
                n, wins = len(attempts), sum(bool(t.success) for t in attempts)
                estimates.append((metric_k, pass_at_k(n, wins, metric_k), pass_pow_k(n, wins, metric_k)))
        ks = {k for k, _, _ in estimates}
        common_k = next(iter(ks)) if len(ks) == 1 else None
        pk = [Decimal(str(a)) for _, a, _ in estimates if a is not None] if common_k else []
        ppk = [Decimal(str(b)) for _, _, b in estimates if b is not None] if common_k else []
        d["requested_k"] = common_k or f.metric_k
        d["pass_at_k"] = str((sum(pk) / len(pk)).quantize(Decimal("0.001"))) if pk else None
        d["pass_pow_k"] = str((sum(ppk) / len(ppk)).quantize(Decimal("0.001"))) if ppk else None
        flags = [t.asked_before_implementing for t in selected if t.asked_before_implementing is not None]
        d["asked_first_rate"] = f"{sum(bool(x) for x in flags) / len(flags):.3f}" if flags else None
        ci = _wilson(r.success_count, r.n)
        d["confidence"] = f"{ci[0]:.2f}–{ci[1]:.2f}" if ci else None
        d["confidence_note"] = (f"n={r.n} valid attempts over {r.scenarios} scenarios; {r.cohort_label}. "
                                f"Mean per-spec/config estimator k={common_k or 'unknown/mixed'}; {len(pk)}/{len(groups)} groups have sufficient n. "
                                + (f"Wilson 95% CI {ci[0]:.2f}–{ci[1]:.2f}; attempt independence approximation." if ci else ""))
        d["key"] = f"{r.model}|{r.condition}|{r.brief_profile}|{r.evaluation_cohort}"
        views.append(d)
    return views


def leaderboard_charts(trials: list, rows: list, cohort: str | None = None) -> tuple[str, str]:
    if cohort is None:
        parts = []
        for key, label in sorted({cohort_of(t) for t in trials}):
            matrix, chart = leaderboard_charts([t for t in trials if cohort_of(t)[0] == key],
                                                [r for r in rows if r.evaluation_cohort == key], key)
            heading = f"<h3>{render.h(label)}</h3>"
            parts.append((heading + matrix, heading + chart))
        return ("".join(p[0] for p in parts), "".join(p[1] for p in parts)) if parts else ("<p class='empty'>No eligible trials.</p>", "")
    eligible = {t.scenario_id for t in trials}
    models = sorted({t.model for t in trials}, key=lambda m: (render.family_of(m)[2] or 99, m))
    by_cell: dict[tuple[str, str, str, str], list] = {}
    for t in trials:
        by_cell.setdefault((t.scenario_id, t.model, t.condition, t.brief_profile), []).append(t)

    def cell(sid, model, condition, profile):
        ts = by_cell.get((sid, model, condition, profile), [])
        if not ts:
            return None, 0, f"{sid} · {model}: no valid attempts"
        return sum(1 for t in ts if t.success) / len(ts), len(ts), f"{sid} · {model}: {sum(1 for t in ts if t.success)}/{len(ts)} valid attempts succeeded"

    matrices = []
    for condition, profile in sorted({(t.condition, t.brief_profile) for t in trials}):
        matrices.append(f"<h3>{render.h(condition)} · {render.h(profile)}</h3>" + pages.charts.matrix(
            sorted(eligible), models, lambda sid, model, c=condition, p=profile: cell(sid, model, c, p),
            chart_id=f"chart-matrix-{condition}-{profile}-{cohort}", title=f"{condition} / {profile}: pass rate per scenario × model",
            col_label=lambda m: m.split("/")[-1][:18]))
    matrix_html = "".join(matrices) or "<p class='empty'>No eligible scenario has completed trials for this purpose.</p>"
    chart_rows = [{"model": r.model, "condition": r.condition, "pass_rate": float(r.pass_rate) if r.pass_rate is not None else None, "n": r.n} for r in rows]
    chart_html = pages.charts.grouped_bars(chart_rows, metric="pass_rate", chart_id="chart-lb-" + cohort, title="pass rate per model per condition")
    return matrix_html, chart_html


@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    f, current = _lb_filter(request, reader)
    rows = reader.leaderboard(f)
    gate = leaderboard_gate(reader, f.include_saturated)
    trials = board_trials(reader, f)
    eligible = {t.scenario_id for t in trials}
    saturated_views: list[dict] = []
    if not f.include_saturated:
        sat_ids = {g["scenario_id"] for g in leaderboard_gate(reader, True) if g["eligible"] and g["difficulty_tier"] == "saturated"}
        if sat_ids:
            sat_rows = reader.leaderboard(R.LeaderboardFilter(purpose=f.purpose, condition=f.condition, brief_profile=f.brief_profile, model=f.model, source=f.source,
                                                              scenario_id=sorted(sat_ids), include_saturated=True, evaluation_cohort=f.evaluation_cohort, metric_k=f.metric_k))
            saturated_views = board_views(reader, sat_rows, sat_ids, f.model_copy(update={"include_saturated": True, "scenario_id": sorted(sat_ids)}))
    views = board_views(reader, rows, eligible, f)
    for key in ("condition", "brief_profile", "model"):
        want = getattr(f, key)
        if want:
            trials = [t for t in trials if getattr(t, key) in want]
    matrix_html, chart_html = leaderboard_charts(trials, rows)
    p = render.Page("Leaderboard", "leaderboard", "leaderboard", crumbs=(("Leaderboard", None),),
                    attrs={"source": "/api/leaderboard?" + str(request.query_params), "chart-target": "leaderboard-charts", "poll-ms": "10000"}, live="updated 0s ago")
    return page(p, pages.leaderboard_body(views, gate, current, matrix_html, chart_html, saturated_views), reader, identity, settings)


@app.get("/leaderboard/evidence", response_class=HTMLResponse)
def leaderboard_evidence(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    f, _ = _lb_filter(request, reader)
    selected = board_trials(reader, f)
    offset, limit = _paging(request)
    body = (f"<h1>Leaderboard evidence</h1><p class='lead'>{len(selected)} admitted attempts in the selected evaluation cohort.</p>"
            + render.table("leaderboard-evidence", pages.TRIAL_COLUMNS, [pages.trial_view(t) for t in selected[offset:offset + limit]],
                           key="trial_id", total=len(selected), offset=offset, limit=limit, empty="No admitted attempts match this cell."))
    return page(render.Page("Leaderboard evidence", "leaderboard", "leaderboard-evidence", crumbs=(("Leaderboard", "/leaderboard"), ("Evidence", None))), body, reader, identity, settings)


@app.get("/api/leaderboard")
def api_leaderboard(request: Request, reader=Depends(reader_dep)) -> JSONResponse:
    f, _ = _lb_filter(request, reader)
    gate = leaderboard_gate(reader, f.include_saturated)
    trials = board_trials(reader, f)
    eligible = {t.scenario_id for t in trials}
    rows = reader.leaderboard(f)
    views = board_views(reader, rows, eligible, f)
    matrix, chart = leaderboard_charts(trials, rows)
    return _json(200, {"items": views, "gate": gate, "charts": {"matrix": "<h2>Per-scenario matrix</h2>" + matrix,
                                                              "pass": "<h2>Pass rate by condition</h2>" + chart},
                       "fragments": {"leaderboard-comparison": {"table": pages.leaderboard_comparison(views)}}})


# ----------------------------------------------------------------------------- episode

def _trends_data(request: Request, reader) -> tuple[dict, dict]:
    f, current = _lb_filter(request, reader)
    query = {k: request.query_params.getlist(k) for k in ("source", "tier", "difficulty_tier", "include_saturated") if request.query_params.get(k)}
    query["purpose"] = f.purpose
    if f.evaluation_cohort:
        query["evaluation_cohort"] = f.evaluation_cohort
    scenarios = list(all_rows(reader.scenarios, R.ScenarioFilter(source=f.source, latest_only=True)))
    payload = trends.summarize(board_trials(reader, f), scenarios, query)
    payload["charts"] = render_trends.chart_fragments(payload)
    payload["coverage"] = render_trends.coverage(payload)
    payload["generated_at"] = datetime.now(UTC).isoformat()
    return payload, current


@app.get("/trends", response_class=HTMLResponse)
def trends_page(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    payload, current = _trends_data(request, reader)
    source = "/api/trends" + ("?" + str(request.query_params) if request.query_params else "")
    p = render.Page("Trends", "trends", "trends", crumbs=(("Leaderboard", "/leaderboard"), ("Trends", None)),
                    attrs={"source": source, "chart-target": "trend-charts", "poll-ms": "10000"}, live="updated 0s ago")
    return page(p, render_trends.body(payload, current), reader, identity, settings)


@app.get("/api/trends")
def api_trends(request: Request, reader=Depends(reader_dep)) -> JSONResponse:
    payload, _ = _trends_data(request, reader)
    return _json(200, payload)


@app.get("/trends/evidence", response_class=HTMLResponse)
def trend_evidence(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    f, _ = _lb_filter(request, reader)
    through = date.fromisoformat(request.query_params.get("through", ""))
    cohort = request.query_params.get("cohort")
    if cohort not in (None, "asked", "did-not-ask"):
        raise HTTPException(400, "cohort must be asked or did-not-ask")
    selected = [t for t in board_trials(reader, f) if t.finished_at is not None and t.finished_at.astimezone(UTC).date() <= through]
    if cohort:
        selected = [t for t in selected if t.questions_asked is not None and (t.questions_asked > 0) == (cohort == "asked")]
    offset, limit = _paging(request)
    body = (f"<h1>Trend evidence</h1><p class='lead'>{len(selected)} admitted {render.h(f.purpose)} attempts completed through {through.isoformat()} UTC. "
            "These are the supporting records for the selected cumulative measurement.</p>"
            + render.table("trend-evidence", pages.TRIAL_COLUMNS, [pages.trial_view(t) for t in selected[offset:offset + limit]], key="trial_id",
                           total=len(selected), offset=offset, limit=limit, empty="No admitted trials match this measurement."))
    return page(render.Page("Trend evidence", "trends", "trend-evidence", crumbs=(("Trends", "/trends"), ("Evidence", None))), body, reader, identity, settings)


@app.get("/health", response_class=HTMLResponse)
def health_page(identity: Identity = Depends(identity_dep)) -> HTMLResponse:
    p = render.Page("Worker queue health", "health", "health", crumbs=(("Worker health", None),))
    return HTMLResponse(render.layout(p, "<h1>Worker queue health</h1>" + pages.health_panel(), identity=identity))


@app.get("/trends/difficulty", response_class=HTMLResponse)
def trend_difficulty(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    label = request.query_params.get("label")
    if label not in ("informative", "saturated", "broken", "unlabelled"):
        raise HTTPException(400, "invalid difficulty label")
    selected = [s for s in all_rows(reader.scenarios, R.ScenarioFilter(validation_status=["valid"], latest_only=True, **_lists(request, "source")))
                if (s.difficulty_tier or "unlabelled") == label]
    offset, limit = _paging(request)
    body = f"<h1>Difficulty evidence</h1><p class='lead'>{len(selected)} current valid scenarios labelled {render.h(label)}.</p>"
    body += render.table("difficulty-evidence", render_scenario.SCENARIO_COLUMNS, [render_scenario.scenario_row_view(s) for s in selected[offset:offset + limit]],
                         key="scenario_id", total=len(selected), offset=offset, limit=limit, empty="No current valid scenarios have this difficulty label.")
    return page(render.Page("Difficulty evidence", "trends", "difficulty-evidence", crumbs=(("Trends", "/trends"), ("Difficulty", None))), body, reader, identity, settings)


@app.get("/api/health")
async def api_health(settings: Settings = Depends(settings_dep)) -> JSONResponse:
    return _json(200, await health_cache.get_health(settings))

def _timeline_rows(t: dict) -> list[dict]:
    return data.merge_timeline(t["trajectory"], t["transcript"], report=t["report"], reward=t["reward"], status=t["status"])


@app.get("/trial/{tid}", response_class=HTMLResponse)
def episode(tid: str, reader=Depends(reader_dep), layout=Depends(layout_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    t = data.load_trial(layout, tid, settings)
    if t is None:
        raise HTTPException(404)
    if not identity.evaluator:
        t = {**t, "report": project(t["report"]), "reward": project(t["reward"]), "status": project(t["status"])}
    row = reader.trial(tid)
    body = render_episode.episode_body(t, row, _timeline_rows(t), lambda tt, name: data.resolve_trial_file(layout, tt, name), identity.evaluator)
    p = render.Page(f"Episode {tid}", "trials", "episode", crumbs=(("Trials", "/trials"), (tid, None)),
                    attrs={"trial": tid, "live": "true" if t["live"] else "false", "poll-ms": "3000"}, live="live" if t["live"] else None)
    return page(p, body, reader, identity, settings)


@app.get("/api/trial/{tid}/timeline")
def timeline(tid: str, reader=Depends(reader_dep), layout=Depends(layout_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> JSONResponse:
    t = data.load_trial(layout, tid, settings)
    if t is None:
        raise HTTPException(404)
    rows = _timeline_rows(t)
    ev = identity.evaluator
    patience = t["patience"] if ev else {k: v for k, v in t["patience"].items() if k != "tier"}
    # the public timeline never carries the evaluator-only columns, not even blanked (brief §2.5, Codex B-P2-6)
    exchanges = [_dump(x) if ev else {k: v for k, v in _dump(x).items() if k not in R.EVALUATOR_ONLY_KEYS} for x in reader.exchanges(tid, ev)]
    result = {"trial_id": tid, "status": t["status"], "live": t["live"], "live_transcript_error": t["live_transcript_error"], "unresolved": t["unresolved"],
                       "evaluator": ev, "timeline": rows if ev else render_episode.public_rows(rows), "report": t["report"], "reward": t["reward"], "label": t["label"],
                       "patience": patience, "meta": t["meta"], "exchanges": exchanges, "patch": t.get("patch")}
    return _json(200, result if ev else project(result))


@app.get("/trial/{tid}/file/{name}")
def trial_file(tid: str, name: str, layout=Depends(layout_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> PlainTextResponse:
    if name not in data.TRIAL_FILES:
        raise HTTPException(404)
    t = data.load_trial(layout, tid, settings)
    if t is None:
        raise HTTPException(404)
    if name in data.EVALUATOR_TRIAL_FILES and not identity.evaluator:
        raise HTTPException(403, detail="evaluator-only file")
    p = data.resolve_trial_file(layout, t, name)
    if p is None or not p.is_file():
        raise HTTPException(404)
    media = "application/json" if name.endswith(".json") else "text/plain; charset=utf-8"
    if media == "application/json" and not identity.evaluator:
        return PlainTextResponse(json.dumps(project(data.read_json(p)), indent=2), media_type=media)
    return PlainTextResponse(p.read_text(errors="replace"), media_type=media)


# ----------------------------------------------------------------------------- review

@app.get("/review", response_class=HTMLResponse)
async def review_queue(reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    q = await console.review_queue(settings.workspace, reader=reader)
    p = render.Page("Review queue", "review", "review", crumbs=(("Review", None),))
    return page(p, render_console.review_queue_body(q), reader, identity, settings)


@app.get("/api/review")
async def api_review_queue(reader=Depends(reader_dep), settings: Settings = Depends(settings_dep)) -> JSONResponse:
    return _json(200, {"ok": True, **(await console.review_queue(settings.workspace, reader=reader))})


@app.get("/review/{sid}", response_class=HTMLResponse)
async def review_item(sid: str, workflow_id: str = "", reader=Depends(reader_dep), layout=Depends(layout_dep), identity: Identity = Depends(identity_dep),
                      settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    if not access.ID_RE.fullmatch(sid):
        raise HTTPException(404)
    d = data.scenario_detail(layout, sid, identity.evaluator)
    if d is None:
        raise HTTPException(404)
    sdir = layout.scenario_dir(sid)
    wid = workflow_id if workflow_id and console.WORKFLOW_ID_RE.fullmatch(workflow_id) else ""
    if not wid:
        try:
            wid = next((i["workflow_id"] for i in (await console.review_queue(settings.workspace, reader=reader))["items"] if i["scenario_id"] == sid and i.get("workflow_id")), "")
        except console.ConsoleUnavailable:
            wid = ""
    body = render_console.review_body(d, reader.scenario(sid), reader.scenario_versions(sid), console.draft_hash(sdir), wid or None,
                                      data.read_json(sdir / "approval.json"), data.read_json(sdir / "review.json"), identity.evaluator)
    p = render.Page(f"Review {sid}", "review", "review-item", crumbs=(("Review", "/review"), (sid, None)))
    return page(p, body, reader, identity, settings)


# ----------------------------------------------------------------------------- create / run / spend / search

@app.get("/create", response_class=HTMLResponse)
def create_form(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    repos = sorted({r.source_ref for r in reader.scenarios(R.ScenarioFilter(source=["github-repo"], latest_only=True, limit=500)).items if str(r.source_ref).startswith("http")})
    p = render.Page("Create tasks", "create", "create", crumbs=(("Create", None),))
    defaults = {**console.defaults(), "rebuild": request.query_params.get("rebuild") == "1"}
    return page(p, render_console.create_body(defaults, repos, settings.spend_cap_usd), reader, identity, settings)


@app.get("/run", response_class=HTMLResponse)
def run_form(reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    valid = [r for r in reader.scenarios(R.ScenarioFilter(validation_status=["valid"], latest_only=True, limit=500)).items if r.registry_json]
    p = render.Page("Run trials", "run", "run", crumbs=(("Run", None),))
    return page(p, render_console.run_body(console.defaults(), valid, settings.spend_cap_usd), reader, identity, settings)


@app.get("/spend", response_class=HTMLResponse)
def spend(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    wid = request.query_params.get("workflow_id") or None
    costs = reader.costs(R.CostFilter(workflow_id=[wid] if wid else None, limit=500))
    by_run: dict[str, dict] = {}
    for c in all_rows(lambda f: reader.table("costs", f), R.TableFilter(where={"workflow_id": wid} if wid else {})):
        if c.get("workflow_id"):
            a = by_run.setdefault(c["workflow_id"], {"workflow_id": c["workflow_id"], "usd": Decimal(0), "records": 0, "unpriced_records": 0})
            if c.get("usd") is None:
                a["unpriced_records"] += 1
            else:
                a["usd"] += Decimal(str(c["usd"]))
            a["records"] += 1
    pools_fn = getattr(reader, "worker_pools", None)
    pools = pools_fn() if callable(pools_fn) else None
    p = render.Page("Spend", "spend", "spend", crumbs=(("Spend", None),), attrs={"source": "/api/spend", "poll-ms": "10000"}, live="updated 0s ago")
    return page(p, pages.spend_body(reader.spend(), costs, settings.spend_cap_usd, [{**v, "usd": str(v["usd"])} for v in by_run.values()], pools,
                                     accounting=_spend_accounting(reader, settings)), reader, identity, settings)


def _spend_accounting(reader, settings: Settings) -> dict:
    from swesim.costs import accounting_status
    status = accounting_status(access.layout_for(settings))
    unknown = reader.table("costs", R.TableFilter(where={"usd": None}, limit=1)).total
    return {**status, "unpriced_records": unknown, "spend_complete": status["usage_complete"] and unknown == 0}


@app.get("/api/spend")
def api_spend(reader=Depends(reader_dep), settings: Settings = Depends(settings_dep)) -> JSONResponse:
    rows = [_dump(s) for s in reader.spend()]
    total = sum((Decimal(str(s["usd"])) for s in rows), Decimal(0))
    return _json(200, {"spend": rows, "accounting": _spend_accounting(reader, settings),
                       "tiles": {"spend_usd": float(total), "spend_text": f"${total:,.2f}"}, "cap_usd": str(settings.spend_cap_usd) if settings.spend_cap_usd is not None else None})


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    q = (request.query_params.get("q") or "").strip()[:200]
    hits: dict = {}
    if q:
        hits["scenarios"] = reader.scenarios(R.ScenarioFilter(q=q, limit=50)).items
        trials = reader.trials(R.TrialFilter(q=q, limit=50)).items
        hits["trials"] = trials
        live, _ = await live_jobs(settings)
        hits["workflows"], _ = workflow_views(reader, live, settings, R.WorkflowFilter(q=q, limit=50))
        hits["models"] = sorted({t.model for t in reader.trials(R.TrialFilter(limit=500)).items if q.lower() in t.model.lower()})
    p = render.Page("Search", "", "search", crumbs=(("Search", None),))
    return page(p, pages.search_body(q, hits), reader, identity, settings)


# ----------------------------------------------------------------------------- database browser

def _table_filter(request: Request) -> R.TableFilter:
    offset, limit = _paging(request, 100)
    where = {k[6:]: v for k, v in request.query_params.items() if k.startswith("where.") and v}
    col, val = request.query_params.get("where_col"), request.query_params.get("where_val")
    if col and val:
        where[col] = val
    return R.TableFilter(where=where, q=request.query_params.get("q") or None, order_by=request.query_params.get("order_by") or None,
                         desc=request.query_params.get("asc") != "1", offset=offset, limit=limit)


@app.get("/db", response_class=HTMLResponse)
def db_index(reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    cnt = {}
    for t in R.TABLES:
        try:
            cnt[t] = reader.table(t, R.TableFilter(limit=1)).total
        except ValueError:
            cnt[t] = "—"
    p = render.Page("Database", "db", "db", crumbs=(("Database", None),))
    return page(p, pages.db_index_body(list(R.TABLES), cnt, reader.rebuilds(), settings.index_enabled), reader, identity, settings)


@app.get("/db/{name}", response_class=HTMLResponse)
def db_table(name: str, request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    if name not in R.TABLES:
        raise HTTPException(404)
    f = _table_filter(request)
    pg = reader.table(name, f)
    cols = list(R.ROW_OF[name].model_fields) if name in R.ROW_OF else (list(pg.items[0]) if pg.items else [])
    if not identity.evaluator:
        cols = [c for c in cols if c not in PRIVATE_KEYS]
        pg = pg.model_copy(update={"items": [project(r) for r in pg.items]})
    p = render.Page(f"Table {name}", "db", "db-table", crumbs=(("Database", "/db"), (name, None)))
    return page(p, pages.db_table_body(name, pg, cols, f.where or {}, f.q, f.order_by), reader, identity, settings)


@app.get("/api/db/{name}.csv")
def api_db_csv(name: str, request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep)) -> Response:
    if name not in R.TABLES:
        raise HTTPException(404)
    f = _table_filter(request)
    buf = io.StringIO()
    writer = None
    offset = 0
    while True:
        pg = reader.table(name, f.model_copy(update={"offset": offset, "limit": 500}))
        items = pg.items if identity.evaluator else [project(r) for r in pg.items]
        if writer is None:
            cols = list(R.ROW_OF[name].model_fields) if name in R.ROW_OF else (list(items[0]) if items else [])
            if not identity.evaluator:
                cols = [c for c in cols if c not in PRIVATE_KEYS]
            writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
        for r in items:
            writer.writerow({k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
        offset += 500
        if offset >= pg.total or not pg.items:
            break
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename={name}.csv"})


@app.get("/api/db/{name}")
def api_db(name: str, request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep)) -> JSONResponse:
    if name not in R.TABLES:
        raise HTTPException(404)
    pg = reader.table(name, _table_filter(request))
    items = pg.items if identity.evaluator else [project(r) for r in pg.items]
    return _json(200, {"items": items, "total": pg.total, "offset": pg.offset, "limit": pg.limit})


# ----------------------------------------------------------------------------- artifacts

def _rel(path: str | None) -> Path:
    rel = Path((path or "").strip("/"))
    if any(part in ("..", "") for part in rel.parts):
        raise HTTPException(400, detail="invalid path")
    return rel


@app.get("/artifacts", response_class=HTMLResponse)
def artifacts(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    rel = _rel(request.query_params.get("path"))
    root = settings.workspace
    ok, why = pages.is_listable(rel, identity.evaluator) if rel.parts else (True, "")
    if not ok:
        raise HTTPException(403, detail=why)
    offset, _ = _paging(request, pages.PAGE_SIZE)
    target = pages.artifact_path(root, rel)
    preview = None
    if target.is_file():
        preview = pages.preview_file(root, rel, evaluator=identity.evaluator)
        listing = pages.list_dir(root, rel.parent, identity.evaluator, offset)
        crumb_rel = rel.parent
    else:
        listing = pages.list_dir(root, rel, identity.evaluator, offset) if rel.parts else {"rel": "", "entries": [], "total": 0, "offset": 0, "missing": False}
        crumb_rel = rel
    p = render.Page("Artifacts", "artifacts", "artifacts", crumbs=(("Artifacts", "/artifacts"),) + tuple((part, None) for part in rel.parts[-1:]))
    return page(p, pages.artifacts_body(root, listing, preview, crumb_rel if not preview else rel), reader, identity, settings)


@app.get("/api/artifacts")
def api_artifacts(request: Request, identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> JSONResponse:
    rel = _rel(request.query_params.get("path"))
    ok, why = pages.is_listable(rel, identity.evaluator) if rel.parts else (True, "")
    if not ok:
        raise HTTPException(403, detail=why)
    offset, _ = _paging(request, pages.PAGE_SIZE)
    return _json(200, pages.list_dir(settings.workspace, rel, identity.evaluator, offset))


@app.get("/artifacts/download")
def artifact_download(request: Request, identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> FileResponse:
    rel = _rel(request.query_params.get("path"))
    ok, why = pages.is_listable(rel, identity.evaluator)
    if not ok:
        raise HTTPException(403, detail=why)
    p = pages.artifact_path(settings.workspace, rel)
    if not p.is_file():
        raise HTTPException(404)
    if p.suffix == ".json" and not identity.evaluator:
        return Response(json.dumps(project(data.read_json(p)), indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{p.name}"'})
    return FileResponse(p, filename=p.name, media_type="application/octet-stream")


# ----------------------------------------------------------------------------- static

@app.get("/static/{name}")
def static(name: str) -> Response:
    if name not in STATIC_TYPES:
        raise HTTPException(404)
    return Response((STATIC / name).read_bytes(), media_type=STATIC_TYPES[name], headers={"Cache-Control": "no-cache"})


# ----------------------------------------------------------------------------- actions (two-step)

def _describe(action: str, params: dict, reader, settings: Settings) -> tuple[dict, list[str], str, str, str | None]:
    """(validated params, errors, CLI equivalent, what, target) for one action."""
    if action == "create":
        p, errs = console.validate_create(params)
        return p, errs, console.cli_for_create(p), "Start a creation job (CreateTasksWorkflow) over the listed sources.", ",".join(p.get("repos") or p.get("instance_ids") or ["swebench:A"])
    if action == "run":
        valid = {r.scenario_id for r in reader.scenarios(R.ScenarioFilter(validation_status=["valid"], latest_only=True, limit=500)).items if r.registry_json}
        p, errs = console.validate_run(params, valid)
        return p, errs, console.cli_for_run(p), "Start a run (RunTasksWorkflow): k attempts per model per condition on the selected scenarios.", ",".join(p.get("scenario_ids") or [])
    if action == "cancel":
        wid = str(params.get("workflow_id") or "")
        errs = [] if console.WORKFLOW_ID_RE.fullmatch(wid) else ["invalid workflow id"]
        return {"workflow_id": wid}, errs, f"temporal workflow cancel --workflow-id {console._q(wid)}", "Request cancellation of this workflow and its children.", wid
    if action == "review":
        sid, decision = str(params.get("scenario_id") or ""), str(params.get("decision") or "")
        errs = []
        if not access.ID_RE.fullmatch(sid):
            errs.append("invalid scenario id")
        if decision not in ("approve", "reject"):
            errs.append("decision must be approve or reject")
        p = {"scenario_id": sid, "decision": decision, "spec_hash": str(params.get("spec_hash") or ""), "workflow_id": str(params.get("workflow_id") or ""),
             "reason": str(params.get("reason") or params.get("note") or "")[:2000]}
        if not p["reason"].strip():
            errs.append("a reason is required for approval or rejection")
        if decision == "approve" and not p["workflow_id"]:
            errs.append("approval requires the holding workflow id; task creation is executed through Temporal")
        if p["workflow_id"] and not console.WORKFLOW_ID_RE.fullmatch(p["workflow_id"]):
            errs.append("invalid holding workflow id")
        cli_params = {"scenario_id": sid, "decision": decision, "spec_hash": p["spec_hash"], "workflow_id": p["workflow_id"], "note": p["reason"]}
        cli = "uv run swesim workflows action review --params-json " + console._q(json.dumps(cli_params))
        return p, errs, cli, f"{decision.capitalize()} the held draft {sid} (spec hash {p['spec_hash'][:12]}…).", sid
    if action == "label":
        tid, label = str(params.get("trial_id") or ""), str(params.get("label") or "")
        errs = []
        if not access.TRIAL_RE.fullmatch(tid):
            errs.append("invalid trial id")
        if label not in LABELS:
            errs.append(f"label must be one of {list(LABELS)}")
        p = {"trial_id": tid, "label": label, "note": str(params.get("note") or "")[:2000]}
        cli = "uv run swesim workflows action label --params-json " + console._q(json.dumps(p))
        return p, errs, cli, "Record a reviewer label on this episode (evaluator annotation; never changes reward).", tid
    if action == "retry":
        ref = params.get("retry_of") if isinstance(params.get("retry_of"), dict) else params  # the confirm step carries the previewed params
        iid, key = str(ref.get("instance_id") or ""), str(ref.get("source_key") or "")
        cand = next((c for c in reader.candidates(R.CandidateFilter(source_key=[key] if key else None, limit=500)).items if c.instance_id == iid), None)
        p, errs = console.validate_retry(params, _dump(cand) if cand else None)
        return p, errs, console.cli_for_create(p) if p else "", f"Retry candidate {iid}: a creation job over its source only.", iid
    if action == "rebuild":
        return {}, [], "uv run swesim index rebuild", "Rebuild the index from the record files and report the diff.", None
    return {}, [f"unknown action {action!r}"], "", "", None


async def _execute(action: str, p: dict, reader, layout, settings: Settings) -> tuple[int, dict]:
    if action == "create":
        started = await console.start_create(p, settings)
        return 200, {"ok": True, **started, "params": p}
    if action == "run":
        started = await console.start_run(p, settings, settings.workspace)
        return 200, {"ok": True, **started, "params": p}
    if action == "cancel":
        return 200, {"ok": True, **(await console.cancel_job(p["workflow_id"])), "workflow_id": p["workflow_id"]}
    if action == "review":
        status, payload = await console.decide(settings.workspace, settings.registry_path, p["scenario_id"],
                                               {"decision": p["decision"], "spec_hash": p["spec_hash"], "workflow_id": p["workflow_id"], "note": p["reason"], "actor": p["_actor"], "request_id": p["_request_id"]})
        return status, payload
    if action == "label":
        result = await console.submit_record_action("label", {k: p[k] for k in ("trial_id", "label", "note")}, p["_actor"], p["_request_id"])
        return 200, result
    if action == "retry":
        started = await console.start_create(p, settings)
        return 200, {"ok": True, **started, "params": p}
    if action == "rebuild":
        try:
            from swesim.index.store import IndexStore  # type: ignore
        except ImportError:
            inv = getattr(reader, "invalidate", None)
            if callable(inv):
                inv()
            return 200, {"ok": True, "message": "Index disabled on this host: the file reader re-projected the record on the next request (no Postgres to rebuild).", "indexed": False}
        store = IndexStore.from_settings(settings)
        if store is None:
            return 200, {"ok": True, "message": "Index disabled (no SWESIM_DATABASE_URL); nothing to rebuild.", "indexed": False}
        row = store.rebuild_from_layout(layout, settings.registry_path)
        return 200, {"ok": True, "message": "Index rebuilt.", "rebuild": _dump(row)}
    return 400, {"ok": False, "error": f"unknown action {action!r}"}


@app.post("/api/{action}/preview", dependencies=[Depends(origin_dep)])
async def api_preview(action: str, request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> JSONResponse:
    if action not in actions.ACTIONS:
        raise HTTPException(404)
    body = await _body(request)
    p, errs, cli, what, target = _describe(action, body, reader, settings)
    if errs:
        access.record_action(reader, settings, actions.audit_row(identity=identity, action=action, target=target, params=p, confirmed=False,
                                                                 origin_header=request.headers.get("origin"), result="rejected", workflow_id=None, settings=settings))
        return _json(400, {"ok": False, "errors": errs, "cli": cli})
    token, exp = actions.issue_token(action, p, identity, settings)
    _key, shared = actions.signing_key(settings)
    return _json(200, {"ok": True, "action": action, "what": what, "cli": cli, "params": p, "token": token, "expires_at": exp.isoformat(timespec="seconds"),
                       "actor": identity.actor, "token_scope": "all console instances" if shared else "this console process only (SWESIM_WORKER_SECRET unset)"})


@app.post("/api/{action}", dependencies=[Depends(origin_dep)])
async def api_action(action: str, request: Request, reader=Depends(reader_dep), layout=Depends(layout_dep), identity: Identity = Depends(identity_dep),
                     settings: Settings = Depends(settings_dep)) -> JSONResponse:
    if action not in actions.ACTIONS:
        raise HTTPException(404)
    body = await _body(request)
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    origin_header = request.headers.get("origin")
    p, errs, cli, _what, target = _describe(action, params, reader, settings)

    def audit(result: str, wid: str | None, confirmed: bool) -> None:
        access.record_action(reader, settings, actions.audit_row(identity=identity, action=action, target=target, params=p or params, confirmed=confirmed,
                                                                 origin_header=origin_header, result=result, workflow_id=wid, settings=settings))
    try:
        actions.verify_token(body.get("token"), action, p if not errs else params, identity, settings)
    except actions.TokenError as e:
        audit("rejected", None, False)
        return _json(409 if "expired" in str(e) or "changed" in str(e) else 403, {"ok": False, "error": str(e), "cli": cli})
    if errs:
        audit("rejected", None, True)
        return _json(400, {"ok": False, "errors": errs, "cli": cli})
    try:
        status, payload = await _execute(action, {**p, "_actor": identity.actor, "_request_id": hashlib.sha256(body["token"].encode()).hexdigest()}, reader, layout, settings)
    except console.ConsoleUnavailable as e:
        audit("failed", None, True)
        return _json(502, {"ok": False, "error": str(e), "cli": e.cli, "kind": e.kind})
    except (OSError, RuntimeError) as e:
        audit("failed", None, True)
        return _json(500, {"ok": False, "error": f"Action failed: {type(e).__name__}", "cli": cli})
    wids = payload.get("workflow_ids") or ([payload["workflow_id"]] if payload.get("workflow_id") else [])
    audit("accepted" if status < 400 and payload.get("ok", True) else ("failed" if status >= 500 else "rejected"), wids[0] if wids else None, True)
    payload.setdefault("cli", cli)
    return _json(status, payload)


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as e:
        raise HTTPException(400, detail="JSON body required") from e
    if not isinstance(body, dict):
        raise HTTPException(400, detail="JSON object required")
    return body
