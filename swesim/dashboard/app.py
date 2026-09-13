"""swesim dashboard: FastAPI routes over the workspace files (server-rendered HTML + vanilla JS, no build step).

Pages: ``/`` overview (live board, polls ``/api/overview``), ``/runs`` (filterable attempts, pass@k chart + table),
``/scenarios`` and ``/scenario/{id}`` (briefs with removed spans, ledger by discovery, controls vs validation), and
``/trial/{id}`` (chat + verifier panel; polls the live transcript while running; Timeline tab after completion).
JSON: ``/api/overview``, ``/api/trial/{id}/timeline``. Writes: only ``POST /trial/{id}/label``.

Honesty rules baked in: rows without timestamps are flagged and never ordered as facts; stakeholder fact annotations and
tool audit are marked evaluator-only; null metrics (n < k) read "n/a"; ``reviewed_equals_submitted`` null is explained as
the diff-only pilot; author-only facts, stakeholder fact annotations, tool audit and the raw record/transcript/spec files are
rendered only with ``?evaluator=1``. Reviewer tool: reached over an SSH forward, never exposed to agents. Secrets are read
from the server environment only and never rendered.

CSRF posture: every mutating route takes a JSON body only (an HTML form cannot produce one cross-site without a CORS
preflight, and no CORS middleware is installed) and additionally rejects a request whose ``Origin`` host differs from
its ``Host`` (``same_origin``). Residual: DNS rebinding against the SSH-forwarded localhost port, accepted for this tool.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx  # noqa: F401 - re-exported so tests can monkeypatch ``app.httpx.get`` (the module object is shared with data.py)
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from swesim.dashboard import console, data, render, render_console
from swesim.dashboard.data import (
    SCENARIO_FILES,
    TRIAL_RE,
    clock,
    fetch_live_transcript,
    iso,
    patience_of,
    read_json,
    to_ts,
)
from swesim.paths import atomic_json
from swesim.stages.score import OUTCOME_NAMES, normalize_record, outcome_code, unresolved_attempt

app = FastAPI(title="swesim dashboard", docs_url=None, redoc_url=None)
ROOT = Path(os.environ.get("SWESIM_WORKSPACE", "workspace")).resolve()
REGISTRY = Path(os.environ.get("SWESIM_REGISTRY", "registry.json")).resolve()
LABELS = render.LABELS
LIVE_TIMEOUT = data.LIVE_TIMEOUT
POLL_MS = 5000
STATIC = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {"app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}
# Files a reviewer may open from the episode page. Fixed names only; nothing else under the trial dir is served.
TRIAL_FILES = {
    "record.json": ("trial", "record.json"), "reward.json": ("trial", "reward.json"), "report.json": ("trial", "report.json"),
    "transcript.json": ("trial", "transcript.json"), "run_config.json": ("trial", "run_config.json"),
    "spec.json": ("trial", "spec.json"), "status.json": ("trial", "status.json"), "label.json": ("trial", "label.json"),
    "request.json": ("trial", "request.json"), "runner-error.json": ("trial", "runner-error.json"),
    "launch.json": ("trial", "launch.json"), "instruction.md": ("trial", "task/instruction.md"),
    "harbor-result.json": ("harbor", "result.json"), "trajectory.json": ("harbor", "agent/trajectory.json"),
    "verifier-reward.json": ("harbor", "verifier/reward.json"), "verifier-report.json": ("harbor", "verifier/report.json"),
}

__all__ = ["app", "clock", "fetch_live_transcript", "iso", "load_trial", "merge_timeline", "read_json", "to_ts", "trial_path"]


def trial_path(tid: str) -> Path:
    if not TRIAL_RE.fullmatch(tid):
        raise HTTPException(404)
    return ROOT / "trials" / tid


def same_origin(request: Request) -> None:
    """POST guard: when the browser sends an Origin, its host must be our Host (scheme/port aside). No Origin (curl, tests) passes."""
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        if origin == "null":
            raise HTTPException(403, detail="cross-site request rejected (null origin)")
        return
    host = (request.headers.get("host") or "").split(":")[0].lower()
    origin_host = origin.split("://", 1)[-1].split("/", 1)[0].split(":")[0].lower()
    if not host or origin_host != host:
        raise HTTPException(403, detail="cross-site request rejected (origin does not match host)")


EVALUATOR_TRIAL_FILES = set(render.EVALUATOR_FILES)
EVALUATOR_SCENARIO_FILES = set(render.SCENARIO_EVALUATOR_FILES)
AUDIT_META = ("disclosed_fact_ids", "tier", "patience_remaining", "tool_calls", "delivery")


def _public_rows(rows: list[dict]) -> list[dict]:
    """Timeline rows without evaluator-only material: no stakeholder tool-call rows, no audit fields in stakeholder meta."""
    out = []
    for r in rows:
        if r.get("kind") == "tool_call":
            continue
        if r.get("actor") == "stakeholder" and isinstance(r.get("meta"), dict):
            r = {**r, "meta": {k: v for k, v in r["meta"].items() if k not in AUDIT_META}}
        out.append(r)
    return out


# ----------------------------------------------------------------------------- timeline

def _row(ts, seq, actor, kind, text, meta) -> dict:
    return {"ts": ts, "ts_iso": iso(ts), "no_timestamp": ts is None, "seq": seq, "actor": actor, "kind": kind,
            "text": text, "meta": meta}


def merge_timeline(traj: dict, transcript: dict, report: dict | None = None, reward: dict | None = None,
                   status: dict | None = None) -> list[dict]:
    """Interleave ATIF agent steps (``timestamp``), stakeholder transcript messages (``ts``; each stakeholder
    ``tool_calls`` entry is its own row) and, when a report exists, one verifier outcome row. Sorted by time;
    rows without a timestamp sort last and carry ``no_timestamp: True``."""
    rows: list[dict] = []
    for i, s in enumerate((traj or {}).get("steps") or []):
        if not isinstance(s, dict):
            continue
        source = s.get("source", "agent")
        actor = "agent" if source == "agent" else "harness"
        kind = "agent-step" if source == "agent" else ("prompt" if source == "user" else "system")
        rows.append(_row(to_ts(s.get("timestamp")), i, actor, kind, str(s.get("message") or ""), s))
    last_exchange = None
    for i, e in enumerate((transcript or {}).get("events") or []):
        if not isinstance(e, dict) or e.get("kind") != "message":
            continue
        ts = to_ts(e.get("ts"))
        if e.get("role") == "user":
            actor, last_exchange = "agent", e.get("exchange") or last_exchange or "ask"
            kind = last_exchange
        else:
            actor, kind = "stakeholder", e.get("exchange") or last_exchange or "reply"
        rows.append(_row(ts, i, actor, kind, str(e.get("content") or ""), e))
        for tool in e.get("tool_calls") or []:
            rows.append(_row(ts, i, "stakeholder", "tool_call", json.dumps(tool), tool))
    report, reward, status = report or {}, reward or {}, status or {}
    if report.get("outcome") or reward.get("outcome_code") is not None:
        name = report.get("outcome") or OUTCOME_NAMES[outcome_code({"reward": reward})]
        text = name if not reward else f"{name}: reward={reward.get('reward')} success={reward.get('success')}"
        ts = next((to_ts(status.get(k)) for k in ("finished_at", "completed_at", "ended_at", "updated_at") if status.get(k)), None)
        ts = ts if ts is not None else to_ts(report.get("finished_at"))
        rows.append(_row(ts, 1 << 30, "verifier", "outcome", text, {"report": report, "reward": reward, "status": status}))
    return sorted(rows, key=lambda r: (r["ts"] is None, r["ts"] or 0.0, r["seq"]))


# ----------------------------------------------------------------------------- trial loading

def _trajectory(tid: str, record: dict) -> dict:
    if record.get("trial_dir"):
        traj = read_json(Path(record["trial_dir"]) / "agent" / "trajectory.json")
        if traj:
            return traj
    found = sorted((ROOT / "jobs" / "runs" / tid).glob("*/agent/trajectory.json"))
    return read_json(found[0]) if len(found) == 1 else {}


def load_trial(tid: str) -> dict:
    d = trial_path(tid)
    if not d.is_dir():
        raise HTTPException(404)
    status = read_json(d / "status.json")
    raw = read_json(d / "record.json") if (d / "record.json").exists() else {}
    has_record = bool(raw)
    record = normalize_record(raw, d) if has_record else {}
    unresolved = None if has_record else unresolved_attempt(d)  # no record: describe whatever files exist
    request = read_json(d / "request.json")
    rc = record.get("run_config") or request.get("run_config") or (unresolved or {}).get("run_config") or {}
    live = status.get("status") == "running" and not has_record
    live_error = None
    if live:
        transcript, live_error = fetch_live_transcript(tid)
    else:
        transcript = record.get("transcript") or read_json(d / "transcript.json")
    report = record.get("report") or read_json(d / "report.json")
    reward = record.get("reward") or read_json(d / "reward.json")
    traj = _trajectory(tid, record)
    ins = next((p for p in (d / "task" / "instruction.md",
                            Path(record["task_dir"]) / "instruction.md" if record.get("task_dir") else None)
                if p is not None and p.exists()), None)
    u = unresolved or {}
    meta = {
        "scenario_id": record.get("scenario_id") or request.get("scenario_id") or u.get("scenario_id"),
        "condition": record.get("condition") or request.get("condition") or u.get("condition"),
        "brief_profile": record.get("brief_profile") or rc.get("brief_profile") or u.get("brief_profile"),
        "model": record.get("model") or request.get("model") or rc.get("model") or u.get("model"),
        "purpose": record.get("purpose") or rc.get("purpose") or u.get("purpose"),
        "attempt_index": record.get("attempt_index", request.get("attempt_index")),
        "spec_hash": record.get("spec_hash") or request.get("spec_hash"),
        "run_config_hash": record.get("run_config_hash") or request.get("run_config_hash") or u.get("run_config_hash"),
        "compiled_hash": record.get("compiled_hash") or rc.get("compiled_hash") or u.get("compiled_hash"),
        "image_digest": (report or {}).get("image_digest"),
        "trial_dir": record.get("trial_dir"),
    }
    # ISO-8601 timestamps: status.json first, record.json as fallback (both written by the run stage when present).
    timestamps = {k: status.get(k) or record.get(k) for k in ("started_at", "finished_at")}
    meta.update(timestamps)
    status_view = {**{k: v for k, v in timestamps.items() if v}, **status}
    return {"trial_id": tid, "dir": d, "status": status_view, "record": record, "request": request, "live": live,
            "unresolved": unresolved, "live_transcript_error": live_error, "transcript": transcript or {},
            "report": report or {}, "reward": reward or {}, "trajectory": traj,
            "instruction": ins.read_text() if ins else None, "label": read_json(d / "label.json", default=None) or None,
            "meta": meta, "patience": patience_of(transcript or {}, rc), "files": list(TRIAL_FILES)}


def _resolve_file(t: dict, name: str) -> Path | None:
    base, rel = TRIAL_FILES[name]
    if base == "trial":
        return t["dir"] / rel
    trial_dir = t["meta"].get("trial_dir")
    if trial_dir:
        return Path(trial_dir) / rel
    found = sorted((ROOT / "jobs" / "runs" / t["trial_id"]).glob(f"*/{rel}"))
    return found[0] if len(found) == 1 else None


def _timeline_rows(t: dict) -> list[dict]:
    return merge_timeline(t["trajectory"], t["transcript"], report=t["report"], reward=t["reward"], status=t["status"])


# ----------------------------------------------------------------------------- routes: pages

@app.get("/", response_class=HTMLResponse)
def overview() -> HTMLResponse:
    ov = data.overview(ROOT, REGISTRY, live=False)  # the page never blocks on the stakeholder; the poll fetches live patience
    return HTMLResponse(render.overview_page(ov, ROOT, REGISTRY))


@app.get("/runs", response_class=HTMLResponse)
def runs() -> HTMLResponse:
    return HTMLResponse(render.runs_page(data.trial_board(ROOT), data.scenario_board(ROOT, REGISTRY)))


@app.get("/scenarios", response_class=HTMLResponse)
def scenarios() -> HTMLResponse:
    return HTMLResponse(render.scenarios_page(data.scenario_board(ROOT, REGISTRY)))


@app.get("/scenario/{sid}", response_class=HTMLResponse)
def scenario(sid: str, evaluator: int = 0) -> HTMLResponse:
    d = data.scenario_detail(ROOT, REGISTRY, sid, evaluator=bool(evaluator))
    if d is None:
        raise HTTPException(404)
    return HTMLResponse(render.scenario_page(d))


@app.get("/scenario/{sid}/file/{name}")
def scenario_file(sid: str, name: str, evaluator: int = 0) -> PlainTextResponse:
    if name not in SCENARIO_FILES or not data.ID_RE.fullmatch(sid):
        raise HTTPException(404)
    p = ROOT / "scenarios" / sid / name
    if not p.is_file():
        raise HTTPException(404)
    if name in EVALUATOR_SCENARIO_FILES and not evaluator:
        raise HTTPException(403, detail="evaluator-only file: add ?evaluator=1")
    return PlainTextResponse(p.read_text(), media_type="application/json")


@app.get("/trial/{tid}", response_class=HTMLResponse)
def episode(tid: str, evaluator: int = 0) -> HTMLResponse:
    t = load_trial(tid)
    return HTMLResponse(render.episode_page(t, _timeline_rows(t), _resolve_file, evaluator=bool(evaluator)))


@app.get("/static/{name}")
def static(name: str) -> Response:
    if name not in STATIC_TYPES:
        raise HTTPException(404)
    return Response((STATIC / name).read_bytes(), media_type=STATIC_TYPES[name], headers={"Cache-Control": "no-cache"})


# ----------------------------------------------------------------------------- routes: JSON

@app.get("/api/overview")
async def api_overview() -> dict:
    # Live patience for running trials is fetched concurrently within a fixed budget; the poll never waits on a hung stakeholder.
    return await data.overview_async(ROOT, REGISTRY)


@app.get("/api/trial/{tid}/timeline")
def timeline(tid: str, evaluator: int = 0) -> dict:
    t = load_trial(tid)
    rows = _timeline_rows(t)
    patience = t["patience"] if evaluator else {k: v for k, v in t["patience"].items() if k != "tier"}  # tier is a stakeholder internal
    return {"trial_id": tid, "status": t["status"], "live": t["live"], "live_transcript_error": t["live_transcript_error"],
            "unresolved": t["unresolved"], "evaluator": bool(evaluator),
            "timeline": rows if evaluator else _public_rows(rows), "report": t["report"], "reward": t["reward"],
            "label": t["label"], "patience": patience, "meta": t["meta"]}


@app.post("/trial/{tid}/label", dependencies=[Depends(same_origin)])
def label(tid: str, body: dict) -> dict:
    d = trial_path(tid)
    if not d.is_dir():
        raise HTTPException(404)
    if not isinstance(body, dict) or body.get("label") not in LABELS:
        raise HTTPException(400, detail=f"label must be one of {list(LABELS)}")
    atomic_json(d / "label.json", {"label": body["label"], "note": str(body.get("note", ""))[:2000]})
    return {"saved": True}


@app.get("/trial/{tid}/file/{name}")
def trial_file(tid: str, name: str, evaluator: int = 0) -> PlainTextResponse:
    if name not in TRIAL_FILES:
        raise HTTPException(404)
    t = load_trial(tid)  # 404 for unknown trials before the evaluator gate
    if name in EVALUATOR_TRIAL_FILES and not evaluator:
        raise HTTPException(403, detail="evaluator-only file: add ?evaluator=1")
    p = _resolve_file(t, name)
    if p is None or not p.is_file():
        raise HTTPException(404)
    media = "application/json" if name.endswith(".json") else "text/markdown; charset=utf-8"
    return PlainTextResponse(p.read_text(), media_type=media)


# ----------------------------------------------------------------------------- operator console

def _json_error(status: int, body: dict) -> JSONResponse:
    return JSONResponse(body, status_code=status)


def _unavailable(e: console.ConsoleUnavailable) -> JSONResponse:
    return _json_error(502, {"ok": False, "error": str(e), "cli": e.cli})


@app.get("/create", response_class=HTMLResponse)
def create_form() -> HTMLResponse:
    return HTMLResponse(render_console.create_page(console.defaults()))


@app.post("/api/create", dependencies=[Depends(same_origin)])
async def api_create(body: dict) -> JSONResponse:
    params, errors = console.validate_create(body)
    if errors:
        return _json_error(400, {"ok": False, "errors": errors, "cli": console.cli_for_create(params)})
    try:
        started = await console.start_create(params)
    except console.ConsoleUnavailable as e:
        return _unavailable(e)
    return JSONResponse({"ok": True, **started, "params": params, "cli": console.cli_for_create(params)})


@app.get("/run", response_class=HTMLResponse)
def run_form() -> HTMLResponse:
    return HTMLResponse(render_console.run_page(console.defaults(), console.valid_registered(REGISTRY)))


@app.post("/api/run", dependencies=[Depends(same_origin)])
async def api_run(body: dict) -> JSONResponse:
    params, errors = console.validate_run(body, REGISTRY)
    if errors:
        return _json_error(400, {"ok": False, "errors": errors, "cli": console.cli_for_run(params)})
    try:
        started = await console.start_run(params, ROOT)
    except console.ConsoleUnavailable as e:
        return _unavailable(e)
    return JSONResponse({"ok": True, **started, "params": params, "cli": console.cli_for_run(params)})


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page() -> HTMLResponse:
    try:
        jobs, error = await console.list_jobs(), None
    except console.ConsoleUnavailable as e:
        jobs, error = None, e
    return HTMLResponse(render_console.jobs_page(jobs, error))


@app.get("/api/jobs")
async def api_jobs() -> JSONResponse:
    try:
        return JSONResponse({"ok": True, "jobs": await console.list_jobs(), "generated_at": iso(console.now_ts())})
    except console.ConsoleUnavailable as e:
        return _unavailable(e)


def _workflow_id(wid: str) -> str:
    if not console.WORKFLOW_ID_RE.fullmatch(wid):
        raise HTTPException(404)
    return wid


@app.get("/jobs/{wid}", response_class=HTMLResponse)
async def job_page(wid: str) -> HTMLResponse:
    wid = _workflow_id(wid)
    job = None
    try:
        job = next((j for j in await console.list_jobs() if j["workflow_id"] == wid), None)
    except console.ConsoleUnavailable:
        pass
    try:
        progress, error = await console.job_progress(wid), None
    except console.ConsoleUnavailable as e:
        progress, error = None, e
    return HTMLResponse(render_console.job_page(wid, progress, error, job))


@app.get("/api/jobs/{wid}")
async def api_job(wid: str) -> JSONResponse:
    wid = _workflow_id(wid)
    try:
        return JSONResponse({"ok": True, "workflow_id": wid, "progress": await console.job_progress(wid)})
    except console.ConsoleUnavailable as e:
        return _unavailable(e)


@app.post("/api/jobs/{wid}/cancel", dependencies=[Depends(same_origin)])
async def api_cancel(wid: str, body: dict) -> JSONResponse:
    wid = _workflow_id(wid)
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return _json_error(400, {"ok": False, "error": "cancellation requires confirm: true"})
    try:
        return JSONResponse({"ok": True, **(await console.cancel_job(wid))})
    except console.ConsoleUnavailable as e:
        return _unavailable(e)


@app.get("/review", response_class=HTMLResponse)
async def review_queue() -> HTMLResponse:
    return HTMLResponse(render_console.review_queue_page(await console.review_queue(ROOT)))


@app.get("/api/review")
async def api_review_queue() -> JSONResponse:
    return JSONResponse({"ok": True, **(await console.review_queue(ROOT))})


@app.get("/review/{sid}", response_class=HTMLResponse)
async def review_item(sid: str, evaluator: int = 0, workflow_id: str = "") -> HTMLResponse:
    d = data.scenario_detail(ROOT, REGISTRY, sid, evaluator=bool(evaluator))
    if d is None or not d["exists"]:
        raise HTTPException(404)
    sdir = ROOT / "scenarios" / sid
    wid = workflow_id if workflow_id and console.WORKFLOW_ID_RE.fullmatch(workflow_id) else ""
    if not wid:
        try:  # is this draft held by a running creation job? then decisions go through the signal
            wid = next((i["workflow_id"] for i in (await console.review_queue(ROOT))["items"]
                        if i["scenario_id"] == sid and i.get("workflow_id")), "")
        except Exception:  # noqa: BLE001 - the queue is a convenience; the page still renders
            wid = ""
    return HTMLResponse(render_console.review_page(d, console.draft_hash(sdir), wid or None, read_json(sdir / "approval.json"),
                                                   read_json(sdir / "review.json"), evaluator=bool(evaluator)))


@app.post("/api/review/{sid}", dependencies=[Depends(same_origin)])
async def api_review(sid: str, body: dict) -> JSONResponse:
    if not data.ID_RE.fullmatch(sid):
        raise HTTPException(404)
    status, payload = await console.decide(ROOT, REGISTRY, sid, body)
    return JSONResponse(payload, status_code=status)
