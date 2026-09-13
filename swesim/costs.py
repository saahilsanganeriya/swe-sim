"""Cost accounting for model calls (interfaces section 6).

Contract
- `PRICES` lists USD per million tokens for the verified model ids only, each with its `price_source` (a key of
  `PRICE_SOURCES`, the page or API it was read from) and the date it was read. An id without a row costs `None` with
  `price_source="unknown"`: a price is never guessed.
- `cost_of` prices uncached input, cached input and output separately; a provider that publishes no cache-read price
  bills cached tokens as input (the conservative reading). Cache-write tokens are counted as input at the base price.
- `Usage` is what a call or a trial consumed: tokens, the number of calls, the provider's own charge when it reports one
  per generation (OpenRouter `usage.cost`, source "openrouter-generation") and, separately, the agent's own estimate
  (mini-swe-agent / litellm `instance_cost`), which is informational and never the record's `usd`.
- `solver_usage_from_trajectory` reads mini-swe-agent's native trajectory (`agent/mini-swe-agent.trajectory.json`:
  per assistant message `extra.response.usage`), Harbor's ATIF conversion (`agent/trajectory.json`: `final_metrics`) or
  Harbor's `result.json` `agent_result`; `usage_from_trial_dir` tries them in that order and returns None when the
  trial left no usage evidence (unknown is never recorded as zero).
- `record_cost` appends one `CostRecord` (the `costs` row minus provenance) to `trials/<tid>/costs.json`, or to
  `costs/<workflow_id>.json` when the cost belongs to no trial, with a write-then-rename and a dedupe on
  (trial, workflow, role, model, tokens) so a retried activity is a no-op. `usd` is quantized to 6 decimals (numeric(12,6)).
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import field_serializer
from ulid import ULID

from swesim.models import Strict
from swesim.paths import atomic_json
from swesim.roles import ModelIdError, parse_model_id

Role = Literal["solver", "judge", "drafter", "stakeholder", "withholder", "audit", "probe", "validation"]
ROLES: frozenset[str] = frozenset(get_args(Role))

PRICE_SOURCES: dict[str, str] = {
    "anthropic-pricing-page": "https://platform.claude.com/docs/en/about-claude/pricing",
    "openai-pricing-page": "https://developers.openai.com/api/docs/pricing",
    "openrouter-models-api": "https://openrouter.ai/api/v1/models",
    # OpenRouter returns the generation's actual charge in `usage.cost` when the request carries usage accounting
    # (litellm's OpenRouter route always sends `usage: {include: true}`); it reflects the upstream provider that served it.
    "openrouter-generation": "https://openrouter.ai/docs/use-cases/usage-accounting",
}
AS_OF = date(2026, 9, 13)       # every row below was read on this date
USD_PLACES = Decimal("0.000001")
_M = Decimal(1_000_000)


class Price(Strict):
    provider: str
    model: str
    input_per_m: Decimal
    output_per_m: Decimal
    cached_input_per_m: Decimal | None
    price_source: str
    as_of: date


def _price(provider: str, model: str, inp: str, out: str, cached: str | None, source: str) -> Price:
    return Price(provider=provider, model=model, input_per_m=Decimal(inp), output_per_m=Decimal(out),
                 cached_input_per_m=None if cached is None else Decimal(cached), price_source=source, as_of=AS_OF)


def _anthropic(model: str, inp: str, out: str, cached: str) -> Price:
    return _price("anthropic", f"anthropic/{model}", inp, out, cached, "anthropic-pricing-page")


def _openai(model: str, inp: str, out: str, cached: str) -> Price:
    return _price("openai", f"openai/{model}", inp, out, cached, "openai-pricing-page")


def _openrouter(model: str, inp: str, out: str, cached: str | None) -> Price:
    """Catalog `pricing.prompt/completion/input_cache_read` are USD per token; the values here are those strings x 1e6."""
    return _price("openrouter", f"openrouter/{model}", inp, out, cached, "openrouter-models-api")


PRICES: tuple[Price, ...] = (
    # Anthropic model pricing table (base input / cache hits / output), read 2026-09-13.
    _anthropic("claude-fable-5-1", "10", "50", "0.25"),
    _anthropic("claude-fable-5", "10", "50", "1"),
    _anthropic("claude-opus-5", "5", "25", "0.50"),
    _anthropic("claude-sonnet-5", "2", "10", "0.20"),
    _anthropic("claude-haiku-4-5-20251001", "1", "5", "0.10"),
    # OpenAI standard tier (input / cached input / output), read 2026-09-13; gpt-5.6-sol is promotional through at least 2026-11-21.
    _openai("gpt-6-astra", "10", "50", "1.00"),
    _openai("gpt-5.6-sol", "4", "20", "0.40"),
    _openai("gpt-5.6-luna", "0.20", "1.20", "0.02"),
    _openai("gpt-5.6-terra", "2", "12", "0.20"),
    _openai("gpt-5.5", "5", "30", "0.50"),
    _openai("gpt-5-mini", "0.25", "2.00", "0.025"),
    # OpenRouter catalog default-route prices, read 2026-09-13 (per-token strings in comments); the served provider may differ,
    # which is why a trial's record prefers the per-generation `usage.cost` when the trajectory carries it.
    _openrouter("deepseek/deepseek-chat-v3-0324", "0.25", "1.00", None),          # 0.00000025 / 0.000001 / -
    _openrouter("z-ai/glm-4.5", "0.60", "2.20", "0.11"),                           # 0.0000006 / 0.0000022 / 0.00000011
    _openrouter("qwen/qwen3-coder", "0.30", "1.00", "0.10"),                       # 0.0000003 / 0.000001 / 0.0000001
    _openrouter("moonshotai/kimi-k2", "0.57", "2.30", None),                       # 0.00000057 / 0.0000023 / -
    _openrouter("minimax/minimax-m2", "0.255", "1.02", None),                      # 0.000000255 / 0.00000102 / -
    _openrouter("openai/gpt-oss-120b", "0.037", "0.17", None),                     # 0.000000037 / 0.00000017 / -
    _openrouter("deepseek/deepseek-v3.2", "0.269", "0.40", "0.1345"),              # 0.000000269 / 0.0000004 / 0.0000001345
    _openrouter("deepseek/deepseek-v4-flash", "0.04928", "0.09856", "0.009856"),   # 0.00000004928 / 0.00000009856 / 0.000000009856
    _openrouter("deepseek/deepseek-v4-pro-0813", "0.57816", "1.73448", "0.018396"),# 0.00000057816 / 0.00000173448 / 0.000000018396
    _openrouter("z-ai/glm-5.2", "0.60", "2.00", "0.15"),                           # 0.0000006 / 0.000002 / 0.00000015
    _openrouter("z-ai/glm-5.3", "1.092", "3.432", "0.2028"),                       # 0.000001092 / 0.000003432 / 0.0000002028
    _openrouter("z-ai/glm-5.3-flash", "0.15", "0.50", "0.03"),                     # 0.00000015 / 0.0000005 / 0.00000003
    _openrouter("qwen/qwen3.6-plus", "0.325", "1.95", None),                       # 0.000000325 / 0.00000195 / -
    _openrouter("moonshotai/kimi-k2.7-code", "0.71", "3.50", "0.15"),              # 0.00000071 / 0.0000035 / 0.00000015
    _openrouter("minimax/minimax-m3", "0.30", "1.20", "0.06"),                     # 0.0000003 / 0.0000012 / 0.00000006
    _openrouter("openai/gpt-5.6-luna", "0.20", "1.20", "0.02"),                    # 0.0000002 / 0.0000012 / 0.00000002
)
_BY_ID: dict[str, Price] = {p.model: p for p in PRICES}


def price_for(model_id: str) -> Price | None:
    """The verified price row for a model id, or None (unknown; never a guess)."""
    try:
        parse_model_id(model_id)
    except ModelIdError:
        return None
    return _BY_ID.get(model_id)


def cost_of(model_id: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> tuple[Decimal | None, str]:
    """USD for the tokens and the price source, or (None, "unknown"). `cached_tokens` is the cache-read subset of `input_tokens`."""
    if min(input_tokens, output_tokens, cached_tokens) < 0:
        raise ValueError("token counts are non-negative")
    if cached_tokens > input_tokens:
        raise ValueError(f"cached_tokens ({cached_tokens}) exceed input_tokens ({input_tokens}); cached tokens are a subset of the input")
    price = price_for(model_id)
    if price is None:
        return None, "unknown"
    cached_rate = price.cached_input_per_m if price.cached_input_per_m is not None else price.input_per_m
    usd = ((input_tokens - cached_tokens) * price.input_per_m + cached_tokens * cached_rate + output_tokens * price.output_per_m) / _M
    return usd, price.price_source


# ------------------------------------------------------------------ usage

class Usage(Strict):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int | None = None            # cache-read subset of input_tokens; None when the source does not say
    calls: int = 0
    reported_cost_usd: Decimal | None = None    # the provider's own charge, summed over every call (only when every call reported one)
    reported_cost_source: str | None = None     # "openrouter-generation" when reported_cost_usd came from OpenRouter
    agent_reported_cost_usd: Decimal | None = None   # the agent's own estimate (mini-swe-agent/litellm); informational, never the record's usd

    def __add__(self, other: "Usage") -> "Usage":
        def both(a: Decimal | None, b: Decimal | None) -> Decimal | None:
            return None if a is None or b is None else a + b

        same_source = self.reported_cost_source == other.reported_cost_source
        reported = both(self.reported_cost_usd, other.reported_cost_usd) if same_source else None
        return Usage(input_tokens=self.input_tokens + other.input_tokens, output_tokens=self.output_tokens + other.output_tokens,
                     cached_tokens=None if self.cached_tokens is None and other.cached_tokens is None else (self.cached_tokens or 0) + (other.cached_tokens or 0),
                     calls=self.calls + other.calls, reported_cost_usd=reported,
                     reported_cost_source=self.reported_cost_source if reported is not None else None,
                     agent_reported_cost_usd=both(self.agent_reported_cost_usd, other.agent_reported_cost_usd))


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _sum_if_complete(values: list[Any]) -> Decimal | None:
    """The sum when every call reported a value; None when any is missing (a partial sum would under-report)."""
    if not values or any(v is None for v in values):
        return None
    return sum((Decimal(str(v)) for v in values), Decimal(0))


def _from_mini_swe_agent(traj: dict) -> Usage:
    inp = out = cached = calls = 0
    generation_costs: list[Any] = []
    agent_costs: list[Any] = []
    for m in traj.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        extra = m.get("extra") or {}
        usage = ((extra.get("response") or {}).get("usage")) or {}
        if not usage:
            continue
        calls += 1
        inp += int(usage.get("prompt_tokens") or 0)
        out += int(usage.get("completion_tokens") or 0)
        cached += int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        generation_costs.append(usage.get("cost"))
        agent_costs.append(extra.get("cost"))
    reported = _sum_if_complete(generation_costs)
    agent = _sum_if_complete(agent_costs)
    if agent is None:
        agent = _decimal(((traj.get("info") or {}).get("model_stats") or {}).get("instance_cost"))
    return Usage(input_tokens=inp, output_tokens=out, cached_tokens=cached, calls=calls, reported_cost_usd=reported,
                 reported_cost_source="openrouter-generation" if reported is not None else None, agent_reported_cost_usd=agent)


def _from_atif(traj: dict) -> Usage:
    fm = traj.get("final_metrics") or {}
    steps = [s for s in traj.get("steps") or [] if isinstance(s, dict) and s.get("source") == "agent" and s.get("metrics")]
    cached = fm.get("total_cached_tokens")
    return Usage(input_tokens=int(fm.get("total_prompt_tokens") or 0), output_tokens=int(fm.get("total_completion_tokens") or 0),
                 cached_tokens=None if cached is None else int(cached), calls=len(steps) or int(fm.get("total_steps") or 0),
                 agent_reported_cost_usd=_decimal(fm.get("total_cost_usd")))


def _from_harbor(agent_result: dict) -> Usage:
    cached = agent_result.get("n_cache_tokens")
    return Usage(input_tokens=int(agent_result.get("n_input_tokens") or 0), output_tokens=int(agent_result.get("n_output_tokens") or 0),
                 cached_tokens=None if cached is None else int(cached), agent_reported_cost_usd=_decimal(agent_result.get("cost_usd")))


def solver_usage_from_trajectory(traj: dict) -> Usage:
    """Usage from mini-swe-agent's native trajectory, Harbor's ATIF trajectory or Harbor's `agent_result`; ValueError otherwise."""
    if isinstance(traj.get("messages"), list):
        return _from_mini_swe_agent(traj)
    if isinstance(traj.get("final_metrics"), dict):
        return _from_atif(traj)
    if "n_input_tokens" in traj:
        return _from_harbor(traj)
    raise ValueError("unrecognised trajectory: expected mini-swe-agent messages, an ATIF final_metrics block or Harbor's agent_result")


def usage_from_trial_dir(trial_dir: Path) -> Usage | None:
    """Usage evidence left by Harbor's trial directory, most detailed source first; None when there is none."""
    trial_dir = Path(trial_dir)
    for name in ("mini-swe-agent.trajectory.json", "trajectory.json"):
        p = trial_dir / "agent" / name
        if p.exists():
            try:
                return solver_usage_from_trajectory(json.loads(p.read_text()))
            except ValueError as e:  # includes json.JSONDecodeError: a corrupt artifact is an error, not a silent fallback
                raise ValueError(f"unreadable agent trajectory {p}: {e}") from e
    result = trial_dir / "result.json"
    if result.exists():
        agent_result = (json.loads(result.read_text() or "null") or {}).get("agent_result")
        if isinstance(agent_result, dict) and agent_result.get("n_input_tokens") is not None:
            return _from_harbor(agent_result)
    return None


# ------------------------------------------------------------------ the record

class CostRecord(Strict):
    """One `costs` row minus provenance (interfaces section 2), as written to trials/<tid>/costs.json."""
    cost_id: str
    trial_id: str | None
    workflow_id: str | None
    role: Role
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int | None
    usd: Decimal | None            # None = unknown price; never zero for "we could not tell"
    price_source: str
    at: datetime

    @field_serializer("at")
    def _serialize_at(self, value: datetime) -> str:
        return value.isoformat()


def _costs_path(layout: Any, trial_id: str | None, workflow_id: str | None) -> Path:
    """`Layout.trial_file(tid, "costs.json")` (a `Workspace` delegates to its `.layout`); workflow-level costs, which the
    layout does not name yet, go under `<root>/costs/<workflow_id>.json`."""
    lay = getattr(layout, "layout", layout)
    if trial_id:
        if hasattr(lay, "trial_file"):
            return Path(lay.trial_file(trial_id, "costs.json"))
        if hasattr(lay, "trial_dir"):
            return Path(lay.trial_dir(trial_id)) / "costs.json"
        return Path(lay.root) / "trials" / trial_id / "costs.json"
    if workflow_id:
        return Path(lay.root) / "costs" / f"{workflow_id}.json"
    raise ValueError("record_cost needs a trial_id or a workflow_id to file the cost under")


def read_costs(path: Path) -> list[CostRecord]:
    path = Path(path)
    if not path.exists():
        return []
    return [CostRecord.model_validate(r) for r in json.loads(path.read_text())]


def usd_for(model_id: str, usage: Usage) -> tuple[Decimal | None, str]:
    """The provider's own charge when the usage carries one, else the price table; (None, "unknown") when neither."""
    if usage.reported_cost_usd is not None and usage.reported_cost_source:
        return usage.reported_cost_usd, usage.reported_cost_source
    return cost_of(model_id, usage.input_tokens, usage.output_tokens, usage.cached_tokens or 0)


def record_cost(layout: Any, trial_id: str | None, workflow_id: str | None, role: str, model_id: str, usage: Usage) -> CostRecord:
    """Append one cost record (idempotent on trial, workflow, role, model and tokens). `layout` is the storage Layout
    (`trial_dir(trial_id)`) or a `Workspace` (`root`)."""
    if role not in ROLES:
        raise ValueError(f"unknown cost role {role!r}; roles: {sorted(ROLES)}")
    mid = parse_model_id(model_id)
    path = _costs_path(layout, trial_id, workflow_id)
    existing = read_costs(path)
    key = (trial_id, workflow_id, role, model_id, usage.input_tokens, usage.output_tokens, usage.cached_tokens)
    for rec in existing:
        if (rec.trial_id, rec.workflow_id, rec.role, rec.model, rec.input_tokens, rec.output_tokens, rec.cached_tokens) == key:
            return rec
    usd, source = usd_for(model_id, usage)
    rec = CostRecord(cost_id=str(ULID()), trial_id=trial_id, workflow_id=workflow_id, role=role, provider=mid.provider, model=model_id,
                     input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, cached_tokens=usage.cached_tokens,
                     usd=None if usd is None else usd.quantize(USD_PLACES, rounding=ROUND_HALF_EVEN), price_source=source,
                     at=datetime.now(timezone.utc))
    atomic_json(path, [r.model_dump(mode="json") for r in [*existing, rec]])
    return rec


def total_usd(records: Iterable[CostRecord]) -> tuple[Decimal, int]:
    """(sum of the known `usd` values, number of records whose price is unknown)."""
    total, unknown = Decimal(0), 0
    for r in records:
        if r.usd is None:
            unknown += 1
        else:
            total += r.usd
    return total, unknown
