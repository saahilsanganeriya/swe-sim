"""Model identities and roles: the id grammar, the providers, the brief's fallback chains and solver tiers, the
judge != draft resolution, and the one-request reachability probe.

Contract
- Model ids follow ``<provider>/<vendor-or-model>[/<model>]``: ``anthropic/<model>``, ``openai/<model>``,
  ``openrouter/<vendor>/<model>``. `parse_model_id` is the single parser (run.py, llm.py and costs.py use it); an id outside
  the grammar raises `ModelIdError` (a ValueError: invalid input, never retried). mini-swe-agent/litellm receive the id
  verbatim (`ModelId.litellm`); a provider's own API receives `ModelId.api_name` (OpenRouter: ``<vendor>/<model>``).
- `PROVIDERS` names, per provider, the API host the sandbox network allowlist must carry, the secret every caller reads
  through `swesim.settings.secret`, and the base URL when the provider is reached with the OpenAI SDK (OpenRouter).
- `ROLE_CHAINS` and `TIERS` are the brief's ids verbatim (2026-09-13 brief, sections 3.1 and 3.2): configured id first,
  then the chain in order. `ALL_PROBE_IDS` is every id the overnight sweep may use, once.
- `resolve_roles(probe)` takes the first reachable id per role, keeps judge and draft distinct (advance the draft chain,
  then the judge chain), records every step in `reasoning`, and lists what needs a person in `needs_human`. It never
  substitutes silently and never raises because a model is unreachable; the solver is not a role and is never substituted.
- `probe_id` sends one tiny request through the provider's own SDK and returns a `ProbeResult` that never carries a
  credential (provider error text is redacted). `probe_all` dedupes ids in order; `write_probe_evidence` writes the
  evidence file atomically and idempotently.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from swesim.models import Strict
from swesim.paths import atomic_json
from swesim.settings import MissingSecret, Settings, secret


class ModelIdError(ValueError):
    """The model id is outside the grammar `<provider>/<vendor-or-model>[/<model>]` or names an unknown provider."""


class Provider(Strict):
    name: str
    host: str          # API host the agent sandbox allowlist must contain
    key_env: str       # the secret name read through swesim.settings.secret
    base_url: str | None = None   # set when the provider is reached with the OpenAI SDK against another endpoint


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider(name="anthropic", host="api.anthropic.com", key_env="ANTHROPIC_API_KEY"),
    "openai": Provider(name="openai", host="api.openai.com", key_env="OPENAI_API_KEY"),
    "openrouter": Provider(name="openrouter", host="openrouter.ai", key_env="OPENROUTER_API_KEY", base_url="https://openrouter.ai/api/v1"),
}


class ModelId(Strict):
    provider: str
    vendor: str | None
    name: str

    @property
    def id(self) -> str:
        return "/".join(p for p in (self.provider, self.vendor, self.name) if p)

    @property
    def litellm(self) -> str:
        """What mini-swe-agent / litellm receive: the id verbatim. litellm 1.100 maps `openrouter/<vendor>/<model>` to
        provider `openrouter` and API model `<vendor>/<model>` (verified with litellm.get_llm_provider)."""
        return self.id

    @property
    def api_name(self) -> str:
        """What the provider's own API receives: the bare model for Anthropic/OpenAI, `<vendor>/<model>` for OpenRouter."""
        return f"{self.vendor}/{self.name}" if self.vendor else self.name


def parse_model_id(model_id: str) -> ModelId:
    """Parse and validate one model id against the grammar; `ModelIdError` for anything else."""
    parts = model_id.split("/")
    provider, rest = parts[0], parts[1:]
    if provider not in PROVIDERS:
        raise ModelIdError(f"unknown provider {provider!r} in model id {model_id!r}; known providers: {sorted(PROVIDERS)}")
    if provider == "openrouter":
        if len(rest) != 2 or not all(rest):
            raise ModelIdError(f"OpenRouter ids are openrouter/<vendor>/<model>, got {model_id!r}")
        return ModelId(provider=provider, vendor=rest[0], name=rest[1])
    if len(rest) != 1 or not rest[0]:
        raise ModelIdError(f"{provider} ids are {provider}/<model>, got {model_id!r}")
    return ModelId(provider=provider, vendor=None, name=rest[0])


def key_env_for(model_id: str) -> str:
    """The secret name the caller of this model must hold (e.g. OPENROUTER_API_KEY)."""
    return PROVIDERS[parse_model_id(model_id).provider].key_env


# ------------------------------------------------------------------ chains and tiers (brief 2026-09-13, sections 3.1 and 3.2, verbatim)

ROLE_CHAINS: dict[str, list[str]] = {
    "judge_model": ["anthropic/claude-opus-5", "openai/gpt-5.6-sol", "openai/gpt-5.5"],
    "draft_model": ["anthropic/claude-sonnet-5", "openai/gpt-5.6-sol"],
    "stakeholder_model": ["anthropic/claude-opus-5", "openai/gpt-5.6-sol", "anthropic/claude-sonnet-5"],
    "audit_model": ["anthropic/claude-opus-5", "openai/gpt-5.6-sol"],
    "screen_model": ["openrouter/deepseek/deepseek-v3.2"],
}

TIERS: dict[str, list[str]] = {
    "A": ["openrouter/deepseek/deepseek-chat-v3-0324", "openrouter/z-ai/glm-4.5", "openrouter/qwen/qwen3-coder",
          "openrouter/moonshotai/kimi-k2", "openrouter/minimax/minimax-m2", "openrouter/openai/gpt-oss-120b"],
    "B": ["openrouter/deepseek/deepseek-v3.2", "openrouter/deepseek/deepseek-v4-flash", "openrouter/deepseek/deepseek-v4-pro-0813",
          "openrouter/z-ai/glm-5.2", "openrouter/z-ai/glm-5.3", "openrouter/z-ai/glm-5.3-flash", "openrouter/qwen/qwen3.6-plus",
          "openrouter/moonshotai/kimi-k2.7-code", "openrouter/minimax/minimax-m3"],
    "C": ["openai/gpt-5-mini", "openai/gpt-5.6-luna", "anthropic/claude-haiku-4-5-20251001"],
    "D": ["anthropic/claude-sonnet-5", "openai/gpt-5.6-sol"],
    "E": ["openai/gpt-6-astra", "anthropic/claude-fable-5-1", "anthropic/claude-opus-5", "openai/gpt-5.5"],
}

# Probed alongside the tiers because the brief names it as a solver candidate served through OpenRouter.
EXTRA_PROBE_IDS: tuple[str, ...] = ("openrouter/openai/gpt-5.6-luna",)


def _dedupe(ids: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for m in ids:
        seen.setdefault(m, None)
    return tuple(seen)


ALL_PROBE_IDS: tuple[str, ...] = _dedupe([m for chain in ROLE_CHAINS.values() for m in chain]
                                         + [m for tier in TIERS.values() for m in tier] + list(EXTRA_PROBE_IDS))


# ------------------------------------------------------------------ resolution

class RoleResolution(Strict):
    configured: dict[str, str]      # the id each role started from (caller's choice or the chain head)
    roles: dict[str, str]           # the id each role uses; equals `configured` unless a substitution is recorded
    substitutions: dict[str, str]   # roles whose id differs from the configured one
    reasoning: list[str]            # one line per fallback or collision step
    needs_human: list[str]          # roles that could not be resolved; never silently equal, never silently missing


def resolve_roles(probe: Callable[[str], bool], configured: Mapping[str, str] | None = None) -> RoleResolution:
    """Resolve every role in `ROLE_CHAINS` against `probe(model_id) -> reachable`, calling `probe` at most once per id."""
    memo: dict[str, bool] = {}

    def ok(model_id: str) -> bool:
        if model_id not in memo:
            memo[model_id] = bool(probe(model_id))
        return memo[model_id]

    conf = {role: (configured or {}).get(role) or chain[0] for role, chain in ROLE_CHAINS.items()}

    def candidates(role: str) -> list[str]:
        return [conf[role]] + [m for m in ROLE_CHAINS[role] if m != conf[role]]

    def first_available(role: str, exclude: frozenset[str] = frozenset(), after: str | None = None) -> str | None:
        cands = candidates(role)
        if after in cands:
            cands = cands[cands.index(after) + 1:]
        return next((m for m in cands if m not in exclude and ok(m)), None)

    roles: dict[str, str] = {}
    reasoning: list[str] = []
    needs_human: list[str] = []
    for role in ROLE_CHAINS:
        chosen = first_available(role)
        if chosen is None:
            needs_human.append(f"{role}: {conf[role]} and its fallbacks {candidates(role)[1:]} are all unavailable")
            chosen = conf[role]
        elif chosen != conf[role]:
            reasoning.append(f"{role}: {conf[role]} unavailable; fell back to {chosen}")
        roles[role] = chosen
    if roles["judge_model"] == roles["draft_model"]:
        shared = roles["draft_model"]
        alt = first_available("draft_model", exclude=frozenset({shared}), after=shared)
        if alt is not None:
            reasoning.append(f"judge and draft both resolved to {shared}; advanced the draft chain to {alt} so their errors stay uncorrelated")
            roles["draft_model"] = alt
        else:
            alt = first_available("judge_model", exclude=frozenset({shared}), after=shared)
            if alt is not None:
                reasoning.append(f"judge and draft both resolved to {shared}; draft chain exhausted, advanced the judge chain to {alt}")
                roles["judge_model"] = alt
            else:
                needs_human.append(f"judge and draft both resolve to {shared} and no distinct model is available")
    substitutions = {role: m for role, m in roles.items() if m != conf[role]}
    return RoleResolution(configured=conf, roles=roles, substitutions=substitutions, reasoning=reasoning, needs_human=needs_human)


# ------------------------------------------------------------------ probe

PROBE_PROMPT = "Reply with the single word OK."
PROBE_MAX_TOKENS = 32
PROBE_TIMEOUT_SEC = 60.0


class ProbeResult(Strict):
    id: str
    provider: str
    reachable: bool
    latency_ms: int | None          # None when no request was sent (bad id, missing credential)
    error_class: str | None         # exception class name; the provider's own class when the SDK raised
    error: str | None               # redacted head of the error text; never a credential
    served_provider: str | None     # OpenRouter's upstream provider for this generation, when reported
    at: str                         # ISO-8601 UTC
    usage: dict[str, Any] | None    # {"input_tokens", "output_tokens", "cost_usd"}; cost only when the provider reports it (OpenRouter)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(text: str, secrets: Iterable[str]) -> str:
    for value in secrets:
        if value and len(value) >= 8:
            text = text.replace(value, "[redacted]")
    return text


def _extra(obj: Any, key: str) -> Any:
    """A field the SDK model exposes as an attribute or keeps under `model_extra` (OpenRouter's `provider`, `usage.cost`)."""
    if obj is None:
        return None
    value = getattr(obj, key, None)
    if value is None:
        value = (getattr(obj, "model_extra", None) or {}).get(key)
    return value


def _probe_anthropic(mid: ModelId, key: str, timeout: float) -> tuple[str | None, dict[str, Any]]:
    import anthropic

    client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=0)
    r = client.messages.create(model=mid.api_name, max_tokens=PROBE_MAX_TOKENS, messages=[{"role": "user", "content": PROBE_PROMPT}])
    u = getattr(r, "usage", None)
    # Anthropic reports cache reads/writes beside `input_tokens`; the cost model counts every prompt token as input.
    prompt = sum(int(getattr(u, f, 0) or 0) for f in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return None, {"input_tokens": prompt, "output_tokens": int(getattr(u, "output_tokens", 0) or 0), "cost_usd": None}


def _probe_openai_compatible(mid: ModelId, key: str, base_url: str | None, timeout: float) -> tuple[str | None, dict[str, Any]]:
    from openai import OpenAI

    client_kwargs: dict[str, Any] = {"api_key": key, "timeout": timeout, "max_retries": 0}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = OpenAI(**client_kwargs)
    create: dict[str, Any] = {"model": mid.api_name, "messages": [{"role": "user", "content": PROBE_PROMPT}]}
    if base_url:
        # OpenRouter: `max_tokens` is the portable cap across upstreams; usage accounting returns the generation's cost.
        create["max_tokens"] = PROBE_MAX_TOKENS
        create["extra_body"] = {"usage": {"include": True}}
    else:
        create["max_completion_tokens"] = PROBE_MAX_TOKENS   # gpt-5.x rejects `max_tokens`
    r = client.chat.completions.create(**create)
    u = getattr(r, "usage", None)
    cost = _extra(u, "cost")
    usage = {"input_tokens": int(getattr(u, "prompt_tokens", 0) or 0), "output_tokens": int(getattr(u, "completion_tokens", 0) or 0),
             "cost_usd": float(cost) if cost is not None else None}
    served = _extra(r, "provider") if base_url else None
    return (str(served) if served else None), usage


def probe_id(model_id: str, *, settings: Settings | None = None, environ: Mapping[str, str] | None = None,
             timeout: float = PROBE_TIMEOUT_SEC) -> ProbeResult:
    """One tiny request through the provider's own SDK. Never raises for a bad id, a missing credential or a provider
    failure: each becomes an unreachable `ProbeResult` with its class. The credential is read through `secret()` and
    never appears in the result."""
    at = _now()
    provider = model_id.partition("/")[0]
    try:
        mid = parse_model_id(model_id)
    except ModelIdError as e:  # outside the grammar: nothing to send
        return ProbeResult(id=model_id, provider=provider, reachable=False, latency_ms=None, error_class="ModelIdError", error=str(e)[:300],
                           served_provider=None, at=at, usage=None)
    prov = PROVIDERS[mid.provider]
    try:
        key = secret(prov.key_env, settings, environ)
    except MissingSecret as e:  # no credential: nothing to send, and the message names only the variable and the file
        return ProbeResult(id=model_id, provider=provider, reachable=False, latency_ms=None, error_class="MissingSecret", error=str(e)[:300],
                           served_provider=None, at=at, usage=None)
    t0 = time.monotonic()
    try:
        if mid.provider == "anthropic":
            served, usage = _probe_anthropic(mid, key, timeout)
        else:
            served, usage = _probe_openai_compatible(mid, key, prov.base_url, timeout)
    except Exception as e:  # provider, auth, quota and transport failures are the probe's finding: record the class, redact the text
        return ProbeResult(id=model_id, provider=provider, reachable=False, latency_ms=int((time.monotonic() - t0) * 1000),
                           error_class=type(e).__name__, error=_redact(str(e), [key])[:300], served_provider=None, at=at, usage=None)
    return ProbeResult(id=model_id, provider=provider, reachable=True, latency_ms=int((time.monotonic() - t0) * 1000), error_class=None,
                       error=None, served_provider=served, at=at, usage=usage)


def probe_all(ids: Iterable[str], *, probe: Callable[..., ProbeResult] = probe_id, settings: Settings | None = None,
              settings_for: Callable[[str], Settings | None] | None = None, environ: Mapping[str, str] | None = None) -> list[ProbeResult]:
    """Probe every id once, in order. `settings_for(provider)` selects the Settings (and so the env file) per provider so
    credentials kept in different files are read without merging them."""
    out: list[ProbeResult] = []
    for model_id in _dedupe(ids):
        s = settings_for(model_id.partition("/")[0]) if settings_for is not None else settings
        out.append(probe(model_id, settings=s, environ=environ))
    return out


def write_probe_evidence(path: Path, probes: list[ProbeResult], resolution: RoleResolution | None = None) -> dict:
    """Write the probe evidence (one row per id, the resolved roles with their reasoning, the chains and tiers) atomically."""
    roles_block = None
    if resolution is not None:
        roles_block = {"configured": resolution.configured, "resolved": resolution.roles, "substitutions": resolution.substitutions,
                       "reasoning": resolution.reasoning, "needs_human": resolution.needs_human}
    out = {"schema": "swesim.model-probe/2", "probed_at": _now(), "probes": [p.model_dump() for p in probes],
           "reachable": [p.id for p in probes if p.reachable], "unreachable": [p.id for p in probes if not p.reachable],
           "roles": roles_block, "chains": ROLE_CHAINS, "tiers": TIERS}
    atomic_json(Path(path), out)
    return out
