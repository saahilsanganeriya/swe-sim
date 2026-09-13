"""One stakeholder session: patience budget, request-id idempotency, transcript, freeze (spec section 8).

Patience is spent per reply. ``full`` above ``terse_below``, ``terse`` between ``terse_below`` and ``refuse_at``,
and a fixed refusal without a model call at or below ``refuse_at`` (or when the exchange cannot be afforded).
Every exchange, including refusals, is recorded; ``disclosed_fact_ids`` and ``tool_calls`` are worker-only
annotations that ``Reply.public`` never exposes to the agent.
"""
from __future__ import annotations

import asyncio, hashlib, json, time
from dataclasses import asdict, dataclass, field

from swesim.stakeholder import tools as review_tools
from swesim.stakeholder.prompts import REPLY_SCHEMA, system_prompt

REFUSAL = "I have to step away now. Please finish with the information available."
MAX_FILE_CHARS = 100_000


@dataclass
class Reply:
    reply: str
    disclosed_fact_ids: list
    patience_remaining: int
    tier: str
    tool_calls: list = field(default_factory=list)
    reviewed_diff_sha256: str | None = None

    def public(self) -> dict:
        """What the agent sees: the text and availability. Fact annotations and the tool audit stay worker-only."""
        out = {"reply": self.reply, "patience_remaining": self.patience_remaining, "tier": self.tier}
        if self.reviewed_diff_sha256 is not None: out["reviewed_diff_sha256"] = self.reviewed_diff_sha256
        return out


class Session:
    class Frozen(Exception): pass
    class Conflict(Exception): pass

    def __init__(self, trial_id: str, view: dict, cfg, image: str | None = None):
        self.trial_id = trial_id; self.view = view; self.cfg = cfg; self.image = image
        self.p = cfg.patience_profile.model_dump(); self.remaining = self.p["start"]
        self.events: list[dict] = []; self.cache: dict[str, dict] = {}
        self.frozen = False; self.transcript_hash: str | None = None; self.frozen_at: float | None = None
        self.created_at = time.time(); self.lock = asyncio.Lock()

    # -- tiers and transcript -------------------------------------------------------------------------------
    def _tier(self) -> str:
        if self.remaining <= self.p["refuse_at"]: return "refused"
        if self.remaining <= self.p["terse_below"]: return "terse"
        return "full"

    def _record(self, kind: str, **kw) -> None:
        self.events.append(dict(kind=kind, ts=time.time(), seq=len(self.events), **kw))

    # -- exchanges ------------------------------------------------------------------------------------------
    async def _exchange(self, rid: str, kind: str, content: str, cost: int, llm, fingerprint_source, sha=None, calls=None) -> Reply:
        fingerprint = hashlib.sha256(json.dumps(fingerprint_source, sort_keys=True).encode()).hexdigest()
        calls = list(calls or [])
        async with self.lock:
            if self.frozen: raise Session.Frozen()
            if rid in self.cache:
                old = self.cache[rid]
                if old["fingerprint"] != fingerprint: raise Session.Conflict()
                return Reply(**old["reply"])
            tier = self._tier()
            if tier != "refused" and self.remaining - cost < self.p["refuse_at"]: tier = "refused"
            if tier == "refused":
                reply = Reply(REFUSAL, [], self.remaining, tier, calls, sha)
            else:
                history = [{"role": e["role"], "content": e["content"]} for e in self.events if e["kind"] == "message"]
                system = system_prompt(self.view, self.view.get("persona", {}), {"tier": tier, "remaining": self.remaining}, self.cfg.review_mode)
                out = await asyncio.to_thread(llm, system, history + [{"role": "user", "content": content}], REPLY_SCHEMA)
                if not isinstance(out, dict) or not isinstance(out.get("reply"), str) or not out["reply"].strip():
                    raise ValueError("empty stakeholder reply")
                self.remaining -= cost
                allowed = {f["id"] for f in self.view.get("facts", [])}
                raw_ids = out.get("disclosed_fact_ids") or []
                ids: list[str] = []
                for i in (raw_ids if isinstance(raw_ids, list) else []):
                    if isinstance(i, str) and i in allowed and i not in ids: ids.append(i)
                reply = Reply(out["reply"], ids, self.remaining, tier, calls, sha)
            self._record("message", role="user", actor="coding-agent", content=content, request_id=rid, exchange=kind)
            self._record("message", role="assistant", actor="stakeholder", content=reply.reply, request_id=rid, exchange=kind,
                         disclosed_fact_ids=reply.disclosed_fact_ids, tier=reply.tier, patience_remaining=reply.patience_remaining,
                         tool_calls=calls, reviewed_diff_sha256=sha, delivery="generated")
            self.cache[rid] = {"fingerprint": fingerprint, "reply": asdict(reply)}
            return reply

    async def ask(self, rid: str, question: str, llm) -> Reply:
        return await self._exchange(rid, "ask", question, self.p["question_cost"], llm, ["ask", question])

    async def review(self, rid: str, diff: str, files: dict, llm, tools=None) -> Reply:
        if tools is not None: raise ValueError("checks review deferred")
        read = review_tools.read_diff(diff)                       # raises on oversized snapshots; never truncates silently
        sha = hashlib.sha256(diff.encode()).hexdigest()
        calls = [{"tool": "read_diff", "sha256": sha, "verified": False}]
        paths = review_tools.changed_paths(diff); extra: list[str] = []
        budget = max(0, int(self.cfg.max_tool_calls_per_activation) - 1)
        for path in paths:
            if len(calls) - 1 >= budget: break
            if path not in (files or {}): continue
            got = review_tools.read_changed_file(files, path, paths)
            text = str(got["content"])[:MAX_FILE_CHARS]
            calls.append({"tool": "read_changed_file", "path": path, "verified": False, "chars": len(text)})
            extra.append(f"\n--- agent-supplied content of {path} (unverified) ---\n{text}")
        content = "Review this agent-supplied diff; it is not verified against the workspace.\n" + read["content"] + "".join(extra)
        return await self._exchange(rid, "review", content, self.p["review_cost"], llm, ["review", diff, files or {}], sha, calls)

    # -- freeze ---------------------------------------------------------------------------------------------
    async def freeze(self) -> str:
        async with self.lock:
            if self.frozen and self.transcript_hash: return self.transcript_hash
            self.frozen = True; self.frozen_at = time.time()
            self.transcript_hash = hashlib.sha256(json.dumps(self.events, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            return self.transcript_hash

    # -- persistence ----------------------------------------------------------------------------------------
    def to_dict(self) -> dict:
        return dict(trial_id=self.trial_id, events=self.events, patience_start=self.p["start"], patience_end=self.remaining,
                    frozen=self.frozen, transcript_hash=self.transcript_hash, created_at=self.created_at, frozen_at=self.frozen_at)

    def snapshot(self) -> dict:
        return dict(**self.to_dict(), view=self.view, config=self.cfg.model_dump(), cache=self.cache, image=self.image)

    @classmethod
    def restore(cls, data: dict) -> "Session":
        from swesim.models import StakeholderConfig
        s = cls(data["trial_id"], data["view"], StakeholderConfig.model_validate(data["config"]), data.get("image"))
        s.events = list(data.get("events", [])); s.cache = dict(data.get("cache", {}))
        s.frozen = bool(data.get("frozen", False)); s.transcript_hash = data.get("transcript_hash")
        s.created_at = data.get("created_at", s.created_at); s.frozen_at = data.get("frozen_at")
        s.remaining = data["patience_end"]
        return s
