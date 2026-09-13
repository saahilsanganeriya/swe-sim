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
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from swesim.dashboard import access, actions, console, data, pages, render, render_console, render_episode, render_scenario
from swesim.dashboard import rows as R
from swesim.dashboard.identity import Identity, identity_from_headers
from swesim.paths import atomic_json
from swesim.settings import Settings, get_settings

app = FastAPI(title="swesim console", docs_url=None, redoc_url=None)
app.state.verify_jwt = None  # tests inject a verifier; None means google.auth
STATIC = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {"tokens.css": "text/css; charset=utf-8", "app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                "theme.js": "text/javascript; charset=utf-8"}
LIVE_LIST_TIMEOUT = 5.0
LABELS = render_episode.LABELS

__all__ = ["app", "settings_dep", "reader_dep", "identity_dep"]


# ----------------------------------------------------------------------------- dependencies

def settings_dep() -> Settings:
    return get_settings()


def reader_dep(settings: Settings = Depends(settings_dep)):
    return access.reader_for(settings)


def layout_dep(settings: Settings = Depends(settings_dep)):
    return access.layout_for(settings)


def identity_dep(request: Request, settings: Settings = Depends(settings_dep)) -> Identity:
    return identity_from_headers(request.headers, settings, verify=app.state.verify_jwt)


def origin_dep(request: Request, settings: Settings = Depends(settings_dep)) -> None:
    try:
        actions.check_origin(request.headers, settings, str(request.url))
    except actions.OriginError as e:
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
    scen = reader.scenarios(R.ScenarioFilter(validation_status=["valid"], limit=1))
    running = reader.trials(R.TrialFilter(status=["running", "launched"], limit=1))
    pending = reader.candidates(R.CandidateFilter(status=["pending"], limit=1))
    wf = reader.workflows(R.WorkflowFilter(status=["running"], limit=1))
    review = len(console.local_drafts(settings.workspace))
    return {"scenarios_valid": scen.total, "trials_running": running.total, "candidates_pending": pending.total, "workflows_running": wf.total, "review_pending": review}


async def live_jobs() -> tuple[list[dict], str | None]:
    """The Temporal listing through console.py, bounded in time; (jobs, error)."""
    try:
        return await asyncio.wait_for(console.list_jobs(), LIVE_LIST_TIMEOUT), None
    except console.ConsoleUnavailable as e:
        return [], f"{e} · {e.cli}"
    except asyncio.TimeoutError:
        return [], "Temporal listing timed out"


def workflow_views(reader, live: list[dict], settings: Settings, f: R.WorkflowFilter) -> tuple[list[dict], int]:
    indexed = reader.workflows(f)
    by_id: dict[str, dict] = {}
    for w in indexed.items:
        d = _dump(w)
        by_id[d["workflow_id"]] = d
    for j in live:
        cur = by_id.get(j["workflow_id"], {})
        merged = {**cur, **{k: v for k, v in j.items() if v not in (None, "", {}) or k not in cur}}
        merged.setdefault("params_summary", j.get("params_summary") or {})
        by_id[j["workflow_id"]] = merged
    views = [pages.workflow_view(w, settings) for w in by_id.values()]
    views.sort(key=lambda w: str(w.get("started_at") or ""), reverse=True)
    return views, max(indexed.total, len(views))


async def home_data(reader, settings: Settings) -> dict:
    c = counts(reader, settings)
    running = reader.trials(R.TrialFilter(status=["running", "launched", "planned"], limit=100))
    completed = reader.trials(R.TrialFilter(status=["completed"], limit=1))
    unresolved = reader.trials(R.TrialFilter(status=["unresolved", "runner-error", "launch-failed", "unit-failed", "cancelled"], limit=1))
    recent = reader.trials(R.TrialFilter(order_by="-finished_at", limit=15))
    live, err = await live_jobs()
    wviews, _ = workflow_views(reader, live, settings, R.WorkflowFilter(limit=15))
    spend = reader.spend()
    total = sum((Decimal(str(s.usd)) for s in spend), Decimal(0))
    ex = getattr(reader, "trial_extras", lambda tid: {})
    tiles = {**c, "trials_completed": completed.total, "trials_unresolved": unresolved.total, "spend_usd": float(total), "spend_text": f"${total:,.2f}"}
    return {"tiles": tiles, "running": [pages.trial_view(t, ex(t.trial_id)) for t in running.items],
            "recent": [pages.trial_view(t, ex(t.trial_id)) for t in recent.items if t.status != "running"],
            "workflows": wviews[:15], "workflow_error": err, "funnel": [_dump(s) for s in reader.funnel()],
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds")}


# ----------------------------------------------------------------------------- pages: record

@app.get("/", response_class=HTMLResponse)
async def home(reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    d = await home_data(reader, settings)
    p = render.Page("Home", "home", "home", attrs={"source": "/api/home", "poll-ms": "5000"}, live="updated 0s ago")
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
    f = R.ScenarioFilter(**_lists(request, "validation_status", "source", "difficulty_tier"), include_archived=request.query_params.get("archived") == "1", offset=offset, limit=limit)
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
    ex = getattr(reader, "trial_extras", lambda tid: {})
    trials = reader.trials(R.TrialFilter(scenario_id=[sid], limit=500))
    trials_html = render.table("scenario-trials", pages.TRIAL_COLUMNS, [pages.trial_view(t, ex(t.trial_id)) for t in trials.items], key="trial_id",
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
                         since=_date(request.query_params.get("since")), until=_date(request.query_params.get("until"), end=True),
                         order_by=request.query_params.get("order_by") or None, offset=offset, limit=limit)


@app.get("/trials", response_class=HTMLResponse)
def trials(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    f = _trial_filter(request)
    pg = reader.trials(f)
    ex = getattr(reader, "trial_extras", lambda tid: {})
    views = [pages.trial_view(t, ex(t.trial_id)) for t in pg.items]
    everything = reader.trials(R.TrialFilter(limit=500)).items
    facets = {"scenario_id": sorted({t.scenario_id for t in everything}), "version": sorted({t.version for t in everything if t.version is not None}),
              "model": sorted({t.model for t in everything}), "status": sorted({t.status for t in everything} | {"unresolved"}),
              "outcome_code": sorted({t.outcome_code for t in everything if t.outcome_code is not None}), "origin": sorted({t.origin for t in everything})}
    current = {k: request.query_params.getlist(k) for k in ("scenario_id", "version", "model", "condition", "brief_profile", "status", "outcome_code", "purpose", "origin")}
    current.update(since=request.query_params.get("since"), until=request.query_params.get("until"))
    p = render.Page("Trials", "trials", "trials", crumbs=(("Trials", None),))
    return page(p, pages.trials_body(pg, views, facets, current, list(getattr(reader, "unprojected", []))), reader, identity, settings)


@app.get("/api/trials")
def api_trials(request: Request, reader=Depends(reader_dep)) -> JSONResponse:
    pg = reader.trials(_trial_filter(request))
    ex = getattr(reader, "trial_extras", lambda tid: {})
    return _json(200, {"items": [pages.trial_view(t, ex(t.trial_id)) for t in pg.items], "total": pg.total, "offset": pg.offset, "limit": pg.limit})


@app.get("/candidates", response_class=HTMLResponse)
def candidates(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    offset, limit = _paging(request, 200)
    key = request.query_params.get("source_key") or None
    f = R.CandidateFilter(source_key=[key] if key else None, **_lists(request, "stage", "status", "gate"), offset=offset, limit=limit)
    pg = reader.candidates(f)
    steps = [_dump(s) for s in reader.funnel(key)]
    hist_fn = getattr(reader, "reject_histogram", None)
    hist = hist_fn(key) if callable(hist_fn) else dict(Counter(r.gate for r in pg.items if r.status == "rejected" and r.gate).most_common())
    keys = sorted({r.source_key for r in reader.candidates(R.CandidateFilter(limit=500)).items})
    p = render.Page("Candidates", "candidates", "candidates", crumbs=(("Candidates", None),))
    return page(p, pages.candidates_body(pg, [pages.candidate_view(r) for r in pg.items], steps, hist, keys, key), reader, identity, settings)


@app.get("/api/candidates")
def api_candidates(request: Request, reader=Depends(reader_dep)) -> JSONResponse:
    offset, limit = _paging(request, 200)
    key = request.query_params.get("source_key") or None
    pg = reader.candidates(R.CandidateFilter(source_key=[key] if key else None, **_lists(request, "stage", "status", "gate"), offset=offset, limit=limit))
    return _json(200, {"items": [pages.candidate_view(r) for r in pg.items], "total": pg.total, "funnel": [_dump(s) for s in reader.funnel(key)]})


@app.get("/workflows", response_class=HTMLResponse)
async def workflows(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    offset, limit = _paging(request, 200)
    live, err = await live_jobs()
    views, total = workflow_views(reader, live, settings, R.WorkflowFilter(**_lists(request, "status", "type"), offset=offset, limit=limit))
    p = render.Page("Workflows", "workflows", "workflows", crumbs=(("Workflows", None),), attrs={"poll-ms": "8000"}, live="updated 0s ago")
    return page(p, pages.workflows_body(views, err, total, settings), reader, identity, settings)


@app.get("/api/workflows")
async def api_workflows(request: Request, reader=Depends(reader_dep), settings: Settings = Depends(settings_dep)) -> JSONResponse:
    offset, limit = _paging(request, 200)
    live, err = await live_jobs()
    views, total = workflow_views(reader, live, settings, R.WorkflowFilter(**_lists(request, "status", "type"), offset=offset, limit=limit))
    return _json(200, {"items": views, "total": total, "error": err})


def _wid(wid: str) -> str:
    if not console.WORKFLOW_ID_RE.fullmatch(wid):
        raise HTTPException(404)
    return wid


@app.get("/workflows/{wid}", response_class=HTMLResponse)
async def workflow(wid: str, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    wid = _wid(wid)
    row = reader.workflow(wid)
    live, _err = await live_jobs()
    job = next((j for j in live if j["workflow_id"] == wid), None)
    progress, perr = None, None
    try:
        progress = await asyncio.wait_for(console.job_progress(wid), LIVE_LIST_TIMEOUT)
    except console.ConsoleUnavailable as e:
        perr = f"{e} · {e.cli}"
    except asyncio.TimeoutError:
        perr = "progress query timed out"
    if row is None and job is None and progress is None:
        raise HTTPException(404)
    children = [pages.workflow_view(_dump(w), settings) for w in reader.workflows(R.WorkflowFilter(parent_workflow_id=[wid], limit=200)).items]
    ex = getattr(reader, "trial_extras", lambda tid: {})
    trials = [pages.trial_view(t, ex(t.trial_id)) for t in reader.trials(R.TrialFilter(limit=500)).items if t.workflow_id == wid]
    p = render.Page(f"Workflow {wid}", "workflows", "workflow", crumbs=(("Workflows", "/workflows"), (wid, None)))
    return page(p, pages.workflow_body(wid, row, job, progress, perr, children, trials, settings, console.redact), reader, identity, settings)


@app.get("/api/workflows/{wid}")
async def api_workflow(wid: str, reader=Depends(reader_dep), settings: Settings = Depends(settings_dep)) -> JSONResponse:
    wid = _wid(wid)
    try:
        progress = await asyncio.wait_for(console.job_progress(wid), LIVE_LIST_TIMEOUT)
    except console.ConsoleUnavailable as e:
        return _json(502, {"ok": False, "error": str(e), "cli": e.cli})
    except asyncio.TimeoutError:
        return _json(504, {"ok": False, "error": "progress query timed out"})
    row = reader.workflow(wid)
    return _json(200, {"ok": True, "workflow_id": wid, "progress": progress, "row": _dump(row), "temporal_url": settings.workflow_url(wid, row.run_id if row else None)})


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

def _lb_filter(request: Request) -> tuple[R.LeaderboardFilter, dict]:
    lists = _lists(request, "brief_profile", "source", "tier", "condition")
    include_sat = request.query_params.get("include_saturated") == "1"
    f = R.LeaderboardFilter(**lists, include_saturated=include_sat)
    current = {k: v or [] for k, v in lists.items()}
    current["include_saturated"] = include_sat
    return f, current


@app.get("/leaderboard", response_class=HTMLResponse)
def leaderboard(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    f, current = _lb_filter(request)
    rows = reader.leaderboard(f)
    gate_fn = getattr(reader, "leaderboard_gate", None)
    gate = gate_fn(f.include_saturated) if callable(gate_fn) else []
    eligible = {g["scenario_id"] for g in gate if g["eligible"]} if gate else {sid for r in rows for sid in r.scenario_ids}
    saturated_rows = []
    if not f.include_saturated:
        sat = reader.leaderboard(R.LeaderboardFilter(**{k: getattr(f, k) for k in ("brief_profile", "source", "tier", "condition")}, include_saturated=True))
        informative_ids = eligible
        saturated_rows = [r for r in sat if not set(r.scenario_ids) <= informative_ids]
    trials = [t for t in reader.trials(R.TrialFilter(scenario_id=sorted(eligible) or None, status=["completed"], limit=500)).items if t.scenario_id in eligible] if eligible else []
    if f.condition:
        trials = [t for t in trials if t.condition in f.condition]
    if f.brief_profile:
        trials = [t for t in trials if t.brief_profile in f.brief_profile]
    models = sorted({t.model for t in trials}, key=lambda m: (render.family_of(m)[2] or 99, m))
    by_cell: dict[tuple[str, str], list] = {}
    for t in trials:
        by_cell.setdefault((t.scenario_id, t.model), []).append(t)

    def cell(sid, model):
        ts = [t for t in by_cell.get((sid, model), []) if t.outcome_code in (1, 2, 3, 4)]
        if not ts:
            return None, 0, f"{sid} · {model}: no valid attempts"
        rate = sum(1 for t in ts if t.success) / len(ts)
        return rate, len(ts), f"{sid} · {model}: {sum(1 for t in ts if t.success)}/{len(ts)} valid attempts succeeded"

    matrix_html = pages.charts.matrix(sorted(eligible), models, cell, chart_id="chart-matrix", title="pass rate per scenario × model",
                                      col_label=lambda m: m.split("/")[-1][:18]) if eligible and models else "<p class='empty'>No eligible scenario has completed trials.</p>"
    chart_rows = [{"model": r.model, "condition": r.condition, "pass_at_k": float(r.pass_at_k) if r.pass_at_k is not None else (r.success_count / r.n if r.n else None), "n": r.n} for r in rows]
    chart_html = pages.charts.grouped_bars(chart_rows, metric="pass_at_k", chart_id="chart-lb", title="pass@k (or pass rate when k is not uniform) per model per condition")
    p = render.Page("Leaderboard", "leaderboard", "leaderboard", crumbs=(("Leaderboard", None),))
    return page(p, pages.leaderboard_body(rows, gate, current, matrix_html, chart_html, saturated_rows), reader, identity, settings)


@app.get("/api/leaderboard")
def api_leaderboard(request: Request, reader=Depends(reader_dep)) -> JSONResponse:
    f, _ = _lb_filter(request)
    return _json(200, {"items": [_dump(r) for r in reader.leaderboard(f)]})


# ----------------------------------------------------------------------------- episode

def _timeline_rows(t: dict) -> list[dict]:
    return data.merge_timeline(t["trajectory"], t["transcript"], report=t["report"], reward=t["reward"], status=t["status"])


@app.get("/trial/{tid}", response_class=HTMLResponse)
def episode(tid: str, reader=Depends(reader_dep), layout=Depends(layout_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    t = data.load_trial(layout, tid, settings)
    if t is None:
        raise HTTPException(404)
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
    return _json(200, {"trial_id": tid, "status": t["status"], "live": t["live"], "live_transcript_error": t["live_transcript_error"], "unresolved": t["unresolved"],
                       "evaluator": ev, "timeline": rows if ev else render_episode.public_rows(rows), "report": t["report"], "reward": t["reward"], "label": t["label"],
                       "patience": patience, "meta": t["meta"], "exchanges": [_dump(x) for x in reader.exchanges(tid, ev)]})


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
    return PlainTextResponse(p.read_text(errors="replace"), media_type=media)


# ----------------------------------------------------------------------------- review

@app.get("/review", response_class=HTMLResponse)
async def review_queue(reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    q = await console.review_queue(settings.workspace)
    p = render.Page("Review queue", "review", "review", crumbs=(("Review", None),))
    return page(p, render_console.review_queue_body(q), reader, identity, settings)


@app.get("/api/review")
async def api_review_queue(settings: Settings = Depends(settings_dep)) -> JSONResponse:
    return _json(200, {"ok": True, **(await console.review_queue(settings.workspace))})


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
            wid = next((i["workflow_id"] for i in (await console.review_queue(settings.workspace))["items"] if i["scenario_id"] == sid and i.get("workflow_id")), "")
        except console.ConsoleUnavailable:
            wid = ""
    body = render_console.review_body(d, reader.scenario(sid), reader.scenario_versions(sid), console.draft_hash(sdir), wid or None,
                                      data.read_json(sdir / "approval.json"), data.read_json(sdir / "review.json"), identity.evaluator)
    p = render.Page(f"Review {sid}", "review", "review-item", crumbs=(("Review", "/review"), (sid, None)))
    return page(p, body, reader, identity, settings)


# ----------------------------------------------------------------------------- create / run / spend / search

@app.get("/create", response_class=HTMLResponse)
def create_form(reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    repos = sorted({r.source_ref for r in reader.scenarios(R.ScenarioFilter(source=["github-repo"], limit=500)).items if str(r.source_ref).startswith("http")})
    p = render.Page("Create tasks", "create", "create", crumbs=(("Create", None),))
    return page(p, render_console.create_body(console.defaults(), repos, settings.spend_cap_usd), reader, identity, settings)


@app.get("/run", response_class=HTMLResponse)
def run_form(reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    valid = [r for r in reader.scenarios(R.ScenarioFilter(validation_status=["valid"], limit=500)).items if r.registry_json]
    p = render.Page("Run trials", "run", "run", crumbs=(("Run", None),))
    return page(p, render_console.run_body(console.defaults(), valid, settings.spend_cap_usd), reader, identity, settings)


@app.get("/spend", response_class=HTMLResponse)
def spend(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    wid = request.query_params.get("workflow_id") or None
    costs = reader.costs(R.CostFilter(workflow_id=[wid] if wid else None, order_by="-at", limit=500))
    by_run: dict[str, dict] = {}
    for c in costs:
        if c.workflow_id:
            a = by_run.setdefault(c.workflow_id, {"workflow_id": c.workflow_id, "usd": Decimal(0), "records": 0})
            a["usd"] += c.usd
            a["records"] += 1
    pools_fn = getattr(reader, "worker_pools", None)
    pools = pools_fn() if callable(pools_fn) else None
    p = render.Page("Spend", "spend", "spend", crumbs=(("Spend", None),), attrs={"source": "/api/spend", "poll-ms": "10000"}, live="updated 0s ago")
    return page(p, pages.spend_body(reader.spend(), costs, settings.spend_cap_usd, [{**v, "usd": str(v["usd"])} for v in by_run.values()], pools), reader, identity, settings)


@app.get("/api/spend")
def api_spend(reader=Depends(reader_dep), settings: Settings = Depends(settings_dep)) -> JSONResponse:
    rows = [_dump(s) for s in reader.spend()]
    total = sum((Decimal(str(s["usd"])) for s in rows), Decimal(0))
    return _json(200, {"spend": rows, "tiles": {"spend_usd": float(total), "spend_text": f"${total:,.2f}"}, "cap_usd": str(settings.spend_cap_usd) if settings.spend_cap_usd is not None else None})


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, reader=Depends(reader_dep), identity: Identity = Depends(identity_dep), settings: Settings = Depends(settings_dep)) -> HTMLResponse:
    q = (request.query_params.get("q") or "").strip()[:200]
    hits: dict = {}
    if q:
        hits["scenarios"] = reader.scenarios(R.ScenarioFilter(q=q, limit=50)).items
        trials = reader.trials(R.TrialFilter(q=q, limit=50)).items
        hits["trials"] = trials
        live, _ = await live_jobs()
        hits["workflows"] = [w for w in live if q.lower() in json.dumps(w, default=str).lower()][:50]
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
    return R.TableFilter(where=where or None, q=request.query_params.get("q") or None, order_by=request.query_params.get("order_by") or None, offset=offset, limit=limit)


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
    if name == "exchanges":
        cols = [c for c in cols if c not in R.EVALUATOR_ONLY_KEYS]
        pg = R.Page(items=[{k: v for k, v in r.items() if k not in R.EVALUATOR_ONLY_KEYS} for r in pg.items], total=pg.total, offset=pg.offset, limit=pg.limit)
    p = render.Page(f"Table {name}", "db", "db-table", crumbs=(("Database", "/db"), (name, None)))
    return page(p, pages.db_table_body(name, pg, cols, f.where or {}, f.q, f.order_by), reader, identity, settings)


@app.get("/api/db/{name}.csv")
def api_db_csv(name: str, request: Request, reader=Depends(reader_dep)) -> Response:
    if name not in R.TABLES:
        raise HTTPException(404)
    f = _table_filter(request)
    buf = io.StringIO()
    writer = None
    offset = 0
    while offset < 10_000:
        pg = reader.table(name, R.TableFilter(where=f.where, q=f.q, order_by=f.order_by, offset=offset, limit=500))
        items = [{k: v for k, v in r.items() if not (name == "exchanges" and k in R.EVALUATOR_ONLY_KEYS)} for r in pg.items]
        if writer is None:
            cols = list(R.ROW_OF[name].model_fields) if name in R.ROW_OF else (list(items[0]) if items else [])
            if name == "exchanges":
                cols = [c for c in cols if c not in R.EVALUATOR_ONLY_KEYS]
            writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
        for r in items:
            writer.writerow({k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
        offset += 500
        if offset >= pg.total or not pg.items:
            break
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename={name}.csv"})


@app.get("/api/db/{name}")
def api_db(name: str, request: Request, reader=Depends(reader_dep)) -> JSONResponse:
    if name not in R.TABLES:
        raise HTTPException(404)
    pg = reader.table(name, _table_filter(request))
    items = [{k: v for k, v in r.items() if not (name == "exchanges" and k in R.EVALUATOR_ONLY_KEYS)} for r in pg.items]
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
    target = root / rel
    preview = None
    if target.is_file():
        preview = pages.preview_file(root, rel)
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
    p = (settings.workspace / rel).resolve()
    if not str(p).startswith(str(settings.workspace.resolve())) or not p.is_file():
        raise HTTPException(404)
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
        valid = {r.scenario_id for r in reader.scenarios(R.ScenarioFilter(validation_status=["valid"], limit=500)).items if r.registry_json}
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
        cli = (f"uv run swesim workflows approve {console._q(p['workflow_id'])} {console._q(sid)} {console._q(p['spec_hash'])} --decision {decision}" if p["workflow_id"]
               else (f"uv run swesim approve {console._q(sid)}   # supervised approval of the local draft" if decision == "approve" else f"# reject: writes scenarios/{sid}/review.json with decision=reject"))
        return p, errs, cli, f"{decision.capitalize()} the held draft {sid} (spec hash {p['spec_hash'][:12]}…).", sid
    if action == "label":
        tid, label = str(params.get("trial_id") or ""), str(params.get("label") or "")
        errs = []
        if not access.TRIAL_RE.fullmatch(tid):
            errs.append("invalid trial id")
        if label not in LABELS:
            errs.append(f"label must be one of {list(LABELS)}")
        p = {"trial_id": tid, "label": label, "note": str(params.get("note") or "")[:2000]}
        cli = f"printf '%s' {console._q(json.dumps({'label': label, 'note': p['note']}))} > {console._q(str(settings.workspace / 'trials' / tid / 'label.json'))}"
        return p, errs, cli, "Record a reviewer label on this episode (evaluator annotation; never changes reward).", tid
    if action == "retry":
        iid, key = str(params.get("instance_id") or ""), str(params.get("source_key") or "")
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
                                               {"decision": p["decision"], "spec_hash": p["spec_hash"], "workflow_id": p["workflow_id"], "note": p["reason"]})
        return status, payload
    if action == "label":
        d = layout.trial_dir(p["trial_id"])
        if not d.is_dir():
            return 404, {"ok": False, "error": "no such trial"}
        atomic_json(d / "label.json", {"label": p["label"], "note": p["note"]})
        return 200, {"ok": True, "saved": True, "message": f"Label {p['label']} saved."}
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
    p, errs, cli, what, _target = _describe(action, body, reader, settings)
    if errs:
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
        status, payload = await _execute(action, p, reader, layout, settings)
    except console.ConsoleUnavailable as e:
        audit("failed", None, True)
        return _json(502, {"ok": False, "error": str(e), "cli": e.cli, "kind": e.kind})
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
