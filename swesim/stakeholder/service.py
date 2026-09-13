"""FastAPI stakeholder service hosted on Modal: /sessions, /ask, /review, /freeze, /transcript, /health.

Durability model: one active container, one asyncio lock for every transaction (exchange and freeze), a JSON
snapshot per trial under ``STORE`` committed before any HTTP reply. No global holds a live Session; every request
restores from the snapshot, so a container restart loses nothing that was acknowledged. ``SYNC``/``RELOAD`` are
hooks the Modal app points at ``Volume.commit``/``Volume.reload`` (sync or async callables are both accepted).
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
LOCK = asyncio.Lock()
# One active Modal container, serialized transactions. Snapshots committed before HTTP replies.
STORE = Path(os.environ.get("SWESIM_SESSION_STORE", "workspace/stakeholder"))
SYNC = lambda: None      # noqa: E731  (replaced by the Modal app with volume.commit)
RELOAD = lambda: None    # noqa: E731  (replaced by the Modal app with volume.reload)
TRIAL_ID_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")
MAX_FILES_CHARS = 500_000
REQUIRED_VIEW_KEYS = {"brief": str, "facts": list, "requirements": list, "persona": dict}


def call_llm(system: str, messages: list[dict], schema: str, cfg: StakeholderConfig) -> dict:
    return chat_json(cfg.model_id, system, messages, schema, temperature=cfg.temperature)


async def _maybe(fn) -> None:
    out = fn()
    if inspect.isawaitable(out): await out


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


def _load(auth: str | None):
    tok = (auth or "").removeprefix("Bearer ").strip(); tid = tok.split(".")[0]
    if not _valid_tid(tid) or not os.environ.get("SWESIM_WORKER_SECRET"): raise HTTPException(401)
    p = _path(tid)
    if not p.exists(): raise HTTPException(401)
    d = json.loads(p.read_text())
    if not hmac.compare_digest(tok.encode(), _token(tid, d["binding"]).encode()): raise HTTPException(401)
    return p, d


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
    async with LOCK:
        await _maybe(RELOAD); p = _path(body.trial_id)
        if p.exists():
            if json.loads(p.read_text())["binding"] != binding: raise HTTPException(409, "session binding changed")
        else:
            s = Session(body.trial_id, body.visible_view, cfg, body.image)
            s._record("session", spec_hash=body.spec_hash, condition=body.condition, run_config_hash=body.run_config_hash)
            atomic_json(p, {"binding": binding, "session": s.snapshot(), "pending": None}); await _maybe(SYNC)
        return {"token": _token(body.trial_id, binding)}


async def exchange(kind: str, body, auth: str | None) -> dict:
    async with LOCK:
        await _maybe(RELOAD); p, d = _load(auth); s = Session.restore(d["session"])
        if d.get("pending"):
            raise HTTPException(503, "interrupted prior request; simulator failure requires worker adjudication")
        d["pending"] = {"request_id": body.request_id, "kind": kind}; atomic_json(p, d); await _maybe(SYNC)
        try:
            fn = lambda sy, m, sc: call_llm(sy, m, sc, s.cfg)  # noqa: E731
            reply = await s.ask(body.request_id, body.question, fn) if kind == "ask" else await s.review(body.request_id, body.diff, body.files, fn)
        except Session.Frozen:
            d["pending"] = None; atomic_json(p, d); await _maybe(SYNC); raise HTTPException(410, "frozen")
        except Session.Conflict:
            d["pending"] = None; atomic_json(p, d); await _maybe(SYNC); raise HTTPException(409, "request id changed")
        except Exception as e:
            count = int((d.get("failure") or {}).get("count", 0)) + 1
            d["failure"] = {"request_id": body.request_id, "kind": type(e).__name__, "count": count}
            d["pending"] = None; atomic_json(p, d); await _maybe(SYNC)
            raise HTTPException(503, "stakeholder failure")
        d.update(session=s.snapshot(), pending=None); atomic_json(p, d); await _maybe(SYNC)
        return reply.public()


@app.post("/ask")
async def ask(body: Ask, authorization: str | None = Header(default=None)):
    return await exchange("ask", body, authorization)


@app.post("/review")
async def review(body: Review, authorization: str | None = Header(default=None)):
    return await exchange("review", body, authorization)


@app.post("/freeze")
async def freeze(authorization: str | None = Header(default=None)):
    async with LOCK:
        await _maybe(RELOAD); p, d = _load(authorization); s = Session.restore(d["session"]); h = await s.freeze()
        d["session"] = s.snapshot(); atomic_json(p, d); await _maybe(SYNC)
        return {"frozen": True, "transcript_hash": h}


@app.get("/transcript/{trial_id}")
async def transcript(trial_id: str, x_worker_secret: str | None = Header(default=None)):
    _worker(x_worker_secret)
    async with LOCK:
        await _maybe(RELOAD); p = _path(trial_id)
        if not p.exists(): raise HTTPException(404)
        d = json.loads(p.read_text()); s = Session.restore(d["session"])
        return {**s.to_dict(), "service_failure": d.get("failure"), "pending": d.get("pending")}
