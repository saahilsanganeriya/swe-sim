"""FastAPI stakeholder service hosted on Modal: /sessions, /ask, /review, /freeze, /transcript, /health.

Locking model: one ``asyncio.Lock`` per trial session (registry ``_SESSION_LOCKS``, created under the short
global ``LOCK``) serializes that session's ask, review and freeze, so "exchange and freeze use the same lock"
holds per session and a slow model reply for trial A never delays trial B. The global ``LOCK`` is held only for
the short persistence steps (RELOAD + read, write + SYNC) and the registry, never across a model call.
``/transcript`` and ``/health`` read under the short global lock only.

Durability model: a JSON snapshot per trial under ``STORE`` is committed before any HTTP reply. No global holds a
live Session; every request restores from the snapshot, so a container restart loses nothing that was
acknowledged. ``SYNC``/``RELOAD`` are hooks the Modal app points at ``Volume.commit``/``Volume.reload`` (sync or
async callables are both accepted); write + SYNC and RELOAD + read are each atomic under ``LOCK`` so a reload can
never observe an uncommitted write from another session.
"""
from __future__ import annotations

import asyncio, hashlib, hmac, inspect, json, os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from swesim.llm import chat_json
from swesim.models import StakeholderConfig
from swesim.paths import atomic_json
from swesim.stakeholder.session import Session

app = FastAPI(title="swesim stakeholder")
LOCK = asyncio.Lock()                                  # short: persistence steps and the session-lock registry only
_SESSION_LOCKS: dict[str, asyncio.Lock] = {}           # one lock per trial id: ask, review and freeze of that session
STORE = Path(os.environ.get("SWESIM_SESSION_STORE", "workspace/stakeholder"))
SYNC = lambda: None      # noqa: E731  (replaced by the Modal app with volume.commit)
RELOAD = lambda: None    # noqa: E731  (replaced by the Modal app with volume.reload)
TRIAL_ID_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")
MAX_FILES_CHARS = 500_000
MAX_DIFF_BYTES = 200_000
REQUIRED_VIEW_KEYS = {"brief": str, "facts": list, "requirements": list, "persona": dict}


def call_llm(system: str, messages: list[dict], schema: str, cfg: StakeholderConfig) -> dict:
    return chat_json(cfg.model_id, system, messages, schema, temperature=cfg.temperature)


async def _maybe(fn) -> None:
    out = fn()
    if inspect.isawaitable(out): await out


# -- persistence (each step atomic under the short global lock) -----------------------------------------------
async def _session_lock(tid: str) -> asyncio.Lock:
    async with LOCK:
        lock = _SESSION_LOCKS.get(tid)
        if lock is None: lock = _SESSION_LOCKS[tid] = asyncio.Lock()
        return lock


async def _read(p: Path) -> dict | None:
    async with LOCK:
        await _maybe(RELOAD)
        return json.loads(p.read_text()) if p.exists() else None


async def _write(p: Path, d: dict) -> None:
    async with LOCK:
        atomic_json(p, d); await _maybe(SYNC)


# -- auth ------------------------------------------------------------------------------------------------------
def _worker(secret: str | None) -> None:
    expected = os.environ.get("SWESIM_WORKER_SECRET", "")
    if not secret or not expected or not hmac.compare_digest(secret.encode(), expected.encode()): raise HTTPException(401)


def _valid_tid(tid: str) -> bool:
    return bool(tid) and len(tid) <= 128 and all(c in TRIAL_ID_CHARS for c in tid)


def _path(tid: str) -> Path:
    if not _valid_tid(tid): raise HTTPException(400, "trial id")
    return STORE / (tid + ".json")


def _token(tid: str, binding: str) -> str:
    return tid + "." + hmac.new(os.environ["SWESIM_WORKER_SECRET"].encode(), binding.encode(), hashlib.sha256).hexdigest()


def _bearer(auth: str | None) -> tuple[str, str]:
    """(token, trial id) from the Authorization header; 401 on anything malformed."""
    tok = (auth or "").removeprefix("Bearer ").strip(); tid = tok.split(".")[0]
    if not _valid_tid(tid) or not os.environ.get("SWESIM_WORKER_SECRET"): raise HTTPException(401)
    return tok, tid


async def _load(auth: str | None):
    tok, tid = _bearer(auth); p = _path(tid); d = await _read(p)
    if d is None: raise HTTPException(401)
    if not hmac.compare_digest(tok.encode(), _token(tid, d["binding"]).encode()): raise HTTPException(401)
    return p, d


# -- request bodies --------------------------------------------------------------------------------------------
class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewSession(Strict):
    trial_id: str; spec_hash: str; condition: str; run_config_hash: str
    visible_view: dict; stakeholder_config: dict; image: str | None = None


class Ask(Strict):
    request_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=20000)


class Review(Strict):
    request_id: str = Field(min_length=1, max_length=100)
    diff: str = Field(max_length=200000)
    files: dict[str, str] = Field(default_factory=dict)

    @field_validator("diff")
    @classmethod
    def _diff_bytes(cls, v: str) -> str:
        if len(v.encode()) > MAX_DIFF_BYTES: raise ValueError("review diff exceeds 200000 bytes")
        return v

    @field_validator("files")
    @classmethod
    def _bounded(cls, v: dict[str, str]) -> dict[str, str]:
        if sum(len(k) + len(c) for k, c in v.items()) > MAX_FILES_CHARS: raise ValueError("review files too large")
        return v


def _check_view(view: dict) -> None:
    for key, typ in REQUIRED_VIEW_KEYS.items():
        if not isinstance(view.get(key), typ): raise HTTPException(400, "invalid visible view")
    for f in view["facts"]:
        if not isinstance(f, dict) or not isinstance(f.get("id"), str) or f.get("discovery") == "author-only":
            raise HTTPException(400, "invalid visible view")


# -- endpoints -------------------------------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"ok": True, "service": "swesim-stakeholder"}


@app.post("/sessions")
async def new_session(body: NewSession, x_worker_secret: str | None = Header(default=None)):
    _worker(x_worker_secret)
    if body.condition != "interactive": raise HTTPException(400, "interactive only")
    _check_view(body.visible_view)
    try: cfg = StakeholderConfig.model_validate(body.stakeholder_config)
    except Exception: raise HTTPException(400, "invalid stakeholder config")
    binding = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    p = _path(body.trial_id)
    async with await _session_lock(body.trial_id):
        d = await _read(p)
        if d is not None:
            if d["binding"] != binding: raise HTTPException(409, "session binding changed")
        else:
            s = Session(body.trial_id, body.visible_view, cfg, body.image)
            s._record("session", spec_hash=body.spec_hash, condition=body.condition, run_config_hash=body.run_config_hash)
            await _write(p, {"binding": binding, "session": s.snapshot(), "pending": None})
        return {"token": _token(body.trial_id, binding)}


async def exchange(kind: str, body, auth: str | None) -> dict:
    _, tid = _bearer(auth)
    async with await _session_lock(tid):
        p, d = await _load(auth); s = Session.restore(d["session"])
        payload = (body.question,) if kind == "ask" else (body.diff, body.files)
        try: hit = s.lookup(body.request_id, kind, *payload)          # frozen / cached / conflict: no writes, no commits
        except Session.Frozen: raise HTTPException(410, "frozen")
        except Session.Conflict: raise HTTPException(409, "request id changed")
        if hit is not None: return hit.public()
        if d.get("pending"):
            raise HTTPException(503, "interrupted prior request; simulator failure requires worker adjudication")
        d["pending"] = {"request_id": body.request_id, "kind": kind}; await _write(p, d)
        try:
            fn = lambda sy, m, sc: call_llm(sy, m, sc, s.cfg)  # noqa: E731
            reply = await s.ask(body.request_id, body.question, fn) if kind == "ask" else await s.review(body.request_id, body.diff, body.files, fn)
        except Session.Frozen:
            d["pending"] = None; await _write(p, d); raise HTTPException(410, "frozen")
        except Session.Conflict:
            d["pending"] = None; await _write(p, d); raise HTTPException(409, "request id changed")
        except Session.BadRequest as e:                              # agent-supplied input rejected: never a service failure
            d["pending"] = None; await _write(p, d); raise HTTPException(422, str(e))
        except Exception as e:
            count = int((d.get("failure") or {}).get("count", 0)) + 1
            d["failure"] = {"request_id": body.request_id, "kind": type(e).__name__, "count": count}
            d["pending"] = None; await _write(p, d)
            raise HTTPException(503, "stakeholder failure")
        d.update(session=s.snapshot(), pending=None); await _write(p, d)
        return reply.public()


@app.post("/ask")
async def ask(body: Ask, authorization: str | None = Header(default=None)):
    return await exchange("ask", body, authorization)


@app.post("/review")
async def review(body: Review, authorization: str | None = Header(default=None)):
    return await exchange("review", body, authorization)


@app.post("/freeze")
async def freeze(authorization: str | None = Header(default=None)):
    _, tid = _bearer(authorization)
    async with await _session_lock(tid):                          # waits for this session's in-flight reply only
        p, d = await _load(authorization); s = Session.restore(d["session"]); h = await s.freeze()
        d["session"] = s.snapshot(); await _write(p, d)
        return {"frozen": True, "transcript_hash": h}


@app.get("/transcript/{trial_id}")
async def transcript(trial_id: str, x_worker_secret: str | None = Header(default=None)):
    _worker(x_worker_secret)
    d = await _read(_path(trial_id))                              # snapshot read; never waits on a model call
    if d is None: raise HTTPException(404)
    s = Session.restore(d["session"])
    return {**s.to_dict(), "service_failure": d.get("failure"), "pending": d.get("pending")}
