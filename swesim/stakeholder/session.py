"""One stakeholder session: patience budget, request-id idempotency, transcript, freeze (spec section 8).

Patience is spent per reply. ``full`` above ``terse_below``, ``terse`` between ``terse_below`` and ``refuse_at``,
and a fixed refusal without a model call at or below ``refuse_at`` (or when the exchange cannot be afforded).
Every exchange, including refusals, is recorded; ``disclosed_fact_ids`` and ``tool_calls`` are worker-only
annotations that ``Reply.public`` never exposes to the agent.

Channels: ``ask`` (question, ``question_cost``), ``confirm`` (a plain-language behavior description the reporter
accepts or not at requirement level, ``review_cost``) and ``review`` (diff review, ``review_cost``). The reporter
cannot read code, so ``review`` is available only when ``StakeholderConfig.review_mode == "diff-only"`` is set
explicitly; otherwise it raises ``Unavailable`` and the shim tells the agent to use ``confirm-behavior``.

Reply filter (code-owned): ``view["forbidden_terms"]`` is a deny-list of tokens computed by the worker from the
hidden test assets (identifiers absent from the brief, visible facts and requirements, plus fixed test vocabulary).
If a model reply contains one, or picks an implementation option the agent offered (``(a)``/``option b``), the
session regenerates once with a stricter instruction, then redacts every sentence still containing a hit to
``[withheld]``. ``redacted_terms``, ``redaction_count``, ``regenerated`` and ``withheld_sentences`` are recorded on
the transcript event (worker-only); the agent sees only the redacted text. The deny-list is never rendered into
the model prompt.

Error classes: ``Frozen`` (session closed), ``Conflict`` (request id reused with a different body), ``BadRequest``
(agent-supplied input rejected by a tool, e.g. an oversized diff) and ``Unavailable`` (channel disabled by the run
config). All are the caller's fault and must never be recorded as a stakeholder service failure.

Tool budget: ``max_tool_calls_per_activation`` counts every tool call of a review, ``read_diff`` included; a
budget of 0 therefore makes reviews unavailable (``BadRequest``).
"""
from __future__ import annotations

import asyncio, hashlib, json, re, time
from dataclasses import asdict, dataclass, field

from swesim.stakeholder import tools as review_tools
from swesim.stakeholder.prompts import REPLY_SCHEMA, system_prompt

REFUSAL = "I have to step away now. Please finish with the information available."
WITHHELD = "[withheld]"
DIFF_REVIEW_UNAVAILABLE = "diff review is not available for this stakeholder; use confirm-behavior"
MAX_FILE_CHARS = 100_000
MAX_CONFIRM_CHARS = 4000
REVIEW_PREFIX = "Review this agent-supplied diff; it is not verified against the workspace.\n"
CONFIRM_PREFIX = ("The agent describes the behavior change it made or intends to make. Say whether it matches what you asked for "
                  "and which requirement still seems unmet, in your own words; do not name code.\n")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OPTION = re.compile(r"(?<![A-Za-z0-9])\(([a-eA-E])\)|\b[Oo]ption\s+\(?([a-eA-E])\)?(?![A-Za-z0-9])")


# -- reply filter ---------------------------------------------------------------------------------------------------
def option_markers(text: str) -> list[str]:
    """Implementation-option markers such as ``(a)`` or ``option B``, normalized to ``(a)``..``(e)``."""
    return sorted({f"({(m.group(1) or m.group(2)).lower()})" for m in _OPTION.finditer(text)})


def term_pattern(term: str) -> re.Pattern:
    """Identifiers match as whole tokens (``Series`` does not hit ``TimeSeries``); an identifier ending in ``_`` is a
    prefix (``test_`` hits ``test_foo``); option markers ``(a)`` also match ``option a``; anything else (``_setup(``,
    ``tests/``) matches as a literal substring."""
    if len(term) == 3 and term[0] == "(" and term[2] == ")" and term[1] in "abcde":
        letter = term[1]
        return re.compile(rf"(?<![A-Za-z0-9])\({letter}\)|\b[Oo]ption\s+\(?{letter}\)?(?![A-Za-z0-9])", re.IGNORECASE)
    if _IDENT.match(term):
        left = r"(?<![A-Za-z0-9_])"; right = "" if term.endswith("_") else r"(?![A-Za-z0-9_])"
        return re.compile(left + re.escape(term) + right)
    if " " in term:                                                    # multi-word test vocabulary: word-bounded, any case
        return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])", re.IGNORECASE)
    return re.compile(re.escape(term))


def find_forbidden(text: str, terms: list[str]) -> list[str]:
    return sorted({t for t in terms if term_pattern(t).search(text)})


def _segments(text: str) -> list[str]:
    """Sentence-ish pieces: a piece ends at ``[.!?]+`` followed by whitespace or the end, or at a newline (kept as its
    own piece). Periods inside identifiers such as ``Continuous()._setup`` do not split."""
    out: list[str] = []; start = 0; i = 0; n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            out.append(text[start:i]); out.append("\n"); i += 1; start = i; continue
        if ch in ".!?":
            j = i
            while j < n and text[j] in ".!?": j += 1
            if j >= n or text[j].isspace(): out.append(text[start:j]); start = i = j; continue
            i = j; continue
        i += 1
    if start < n: out.append(text[start:])
    return out


def redact(text: str, terms: list[str]) -> tuple[str, list[str]]:
    """Replace every sentence (or line) containing a forbidden term with ``[withheld]``; returns the withheld sentences."""
    pats = [term_pattern(t) for t in terms]; withheld: list[str] = []; pieces: list[str] = []
    for seg in _segments(text):
        if seg == "\n" or not seg.strip() or not any(p.search(seg) for p in pats): pieces.append(seg); continue
        withheld.append(seg.strip()); lead = len(seg) - len(seg.lstrip()); pieces.append(seg[:lead] + WITHHELD)
    out = "".join(pieces)
    return (out if out.strip() else WITHHELD), withheld


def _valid(out) -> bool:
    return isinstance(out, dict) and isinstance(out.get("reply"), str) and bool(out["reply"].strip())


@dataclass
class Reply:
    reply: str
    disclosed_fact_ids: list
    patience_remaining: int
    tier: str
    tool_calls: list = field(default_factory=list)
    reviewed_diff_sha256: str | None = None
    redacted_terms: list = field(default_factory=list)      # worker-only annotations, appended after the frozen fields
    redaction_count: int = 0
    regenerated: bool = False
    withheld_sentences: list = field(default_factory=list)

    def public(self) -> dict:
        """What the agent sees: the text and availability. Fact annotations, the tool audit and redaction details stay worker-only."""
        out = {"reply": self.reply, "patience_remaining": self.patience_remaining, "tier": self.tier}
        if self.reviewed_diff_sha256 is not None: out["reviewed_diff_sha256"] = self.reviewed_diff_sha256
        return out


class Session:
    class Frozen(Exception): pass
    class Conflict(Exception): pass
    class BadRequest(ValueError): pass
    class Unavailable(Exception): pass

    def __init__(self, trial_id: str, view: dict, cfg, image: str | None = None):
        self.trial_id = trial_id; self.view = view; self.cfg = cfg; self.image = image
        self.p = cfg.patience_profile.model_dump(); self.remaining = self.p["start"]
        self.terms = [t for t in (view.get("forbidden_terms") or []) if isinstance(t, str) and t]
        self.events: list[dict] = []; self.cache: dict[str, dict] = {}
        self.frozen = False; self.transcript_hash: str | None = None; self.frozen_at: float | None = None
        self.created_at = time.time(); self.lock = asyncio.Lock()

    @property
    def diff_review_enabled(self) -> bool:
        return getattr(self.cfg, "review_mode", None) == "diff-only"

    # -- tiers and transcript -------------------------------------------------------------------------------
    def _tier(self) -> str:
        if self.remaining <= self.p["refuse_at"]: return "refused"
        if self.remaining <= self.p["terse_below"]: return "terse"
        return "full"

    def _record(self, kind: str, **kw) -> None:
        self.events.append(dict(kind=kind, ts=time.time(), seq=len(self.events), **kw))

    @staticmethod
    def _fingerprint(kind: str, *payload) -> str:
        return hashlib.sha256(json.dumps([kind, *payload], sort_keys=True).encode()).hexdigest()

    def lookup(self, rid: str, kind: str, *payload) -> Reply | None:
        """Frozen/cache/conflict check without side effects; the service calls it before persisting a pending marker."""
        if self.frozen: raise Session.Frozen()
        old = self.cache.get(rid)
        if old is None: return None
        if old["fingerprint"] != self._fingerprint(kind, *payload): raise Session.Conflict()
        return Reply(**old["reply"])

    def _history(self) -> list[dict]:
        """Prior exchanges for the model. Earlier review bodies are replaced by a sha256 stub; the transcript keeps them."""
        out = []
        for e in self.events:
            if e["kind"] != "message": continue
            content = e["content"]
            if e["role"] == "user" and e.get("exchange") == "review":
                content = f"[review request {e['request_id']}: diff sha256 {e.get('reviewed_diff_sha256')}]"
            out.append({"role": e["role"], "content": content})
        return out

    def _hits(self, reply: str, agent_options: list[str]) -> list[str]:
        picks = [m for m in option_markers(reply) if m in agent_options]
        return sorted(set(find_forbidden(reply, self.terms)) | set(picks))

    async def _generate(self, tier: str, content: str, llm) -> tuple[dict, list[str], bool, list[str]]:
        """Model call plus the code-owned filter: (final output, redacted_terms, regenerated, withheld_sentences)."""
        persona = self.view.get("persona", {}); patience = {"tier": tier, "remaining": self.remaining}
        mode = getattr(self.cfg, "review_mode", "behavior")
        messages = self._history() + [{"role": "user", "content": content}]
        agent_options = option_markers(content)
        out = await asyncio.to_thread(llm, system_prompt(self.view, persona, patience, mode), messages, REPLY_SCHEMA)
        if not _valid(out): raise ValueError("empty stakeholder reply")
        hits = self._hits(out["reply"], agent_options); regenerated = False
        if hits:
            try:                                                        # one stricter attempt; the first draft stays the fallback
                again = await asyncio.to_thread(llm, system_prompt(self.view, persona, patience, mode, strict=True), messages, REPLY_SCHEMA)
                if _valid(again): out, regenerated = again, True; hits = self._hits(out["reply"], agent_options)
            except Exception: pass
        withheld: list[str] = []
        if hits:
            text, withheld = redact(out["reply"], hits); out = {**out, "reply": text}
        return out, hits, regenerated, withheld

    # -- exchanges ------------------------------------------------------------------------------------------
    async def _exchange(self, rid: str, kind: str, content: str, cost: int, llm, fingerprint: str, sha=None, calls=None) -> Reply:
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
                out, hits, regenerated, withheld = await self._generate(tier, content, llm)
                self.remaining -= cost
                allowed = {f["id"] for f in self.view.get("facts", [])}
                raw_ids = out.get("disclosed_fact_ids") or []
                ids: list[str] = []
                for i in (raw_ids if isinstance(raw_ids, list) else []):
                    if isinstance(i, str) and i in allowed and i not in ids: ids.append(i)
                reply = Reply(out["reply"], ids, self.remaining, tier, calls, sha, hits, len(withheld), regenerated, withheld)
            user_extra = {"reviewed_diff_sha256": sha} if kind == "review" else {}
            self._record("message", role="user", actor="coding-agent", content=content, request_id=rid, exchange=kind, **user_extra)
            self._record("message", role="assistant", actor="stakeholder", content=reply.reply, request_id=rid, exchange=kind,
                         disclosed_fact_ids=reply.disclosed_fact_ids, tier=reply.tier, patience_remaining=reply.patience_remaining,
                         tool_calls=calls, reviewed_diff_sha256=sha, delivery="generated",
                         redacted_terms=reply.redacted_terms, redaction_count=reply.redaction_count, regenerated=reply.regenerated,
                         withheld_sentences=reply.withheld_sentences)
            self.cache[rid] = {"fingerprint": fingerprint, "reply": asdict(reply)}
            return reply

    async def ask(self, rid: str, question: str, llm) -> Reply:
        return await self._exchange(rid, "ask", question, self.p["question_cost"], llm, self._fingerprint("ask", question))

    async def confirm(self, rid: str, description: str, llm) -> Reply:
        """Behavioral acceptance check: the agent describes a behavior change in plain language; costs review_cost."""
        if not description.strip(): raise Session.BadRequest("empty behavior description")
        if len(description) > MAX_CONFIRM_CHARS: raise Session.BadRequest(f"behavior description exceeds {MAX_CONFIRM_CHARS} characters")
        return await self._exchange(rid, "confirm", CONFIRM_PREFIX + description, self.p["review_cost"], llm, self._fingerprint("confirm", description))

    async def review(self, rid: str, diff: str, files: dict, llm, tools=None) -> Reply:
        if tools is not None: raise ValueError("checks review deferred")
        if not self.diff_review_enabled: raise Session.Unavailable(DIFF_REVIEW_UNAVAILABLE)
        files = files or {}
        budget = int(self.cfg.max_tool_calls_per_activation)
        if budget < 1: raise Session.BadRequest("reviews are unavailable: max_tool_calls_per_activation is 0")
        try:
            read = review_tools.read_diff(diff)                   # raises on oversized snapshots; never truncates silently
            sha = hashlib.sha256(diff.encode()).hexdigest()
            calls = [{"tool": "read_diff", "sha256": sha, "verified": False}]
            paths = review_tools.changed_paths(diff); extra: list[str] = []
            for path in paths:
                if len(calls) >= budget: break
                if path not in files: continue
                got = review_tools.read_changed_file(files, path, paths)
                text = str(got["content"])[:MAX_FILE_CHARS]
                calls.append({"tool": "read_changed_file", "path": path, "verified": False, "chars": len(text)})
                extra.append(f"\n--- agent-supplied content of {path} (unverified) ---\n{text}")
        except ValueError as e:
            raise Session.BadRequest(str(e)) from e
        content = REVIEW_PREFIX + read["content"] + "".join(extra)
        return await self._exchange(rid, "review", content, self.p["review_cost"], llm, self._fingerprint("review", diff, files), sha, calls)

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
