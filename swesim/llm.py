"""Minimal JSON-returning chat client for the pipeline roles (judge, drafter, stakeholder). The provider is chosen by
the model id (`swesim.roles.parse_model_id`); roles run on Anthropic or OpenAI only.

Credentials are read at the point of use with `swesim.settings.secret(name, settings)`: the process environment first,
then the env file named by `Settings.env_file` (SWESIM_ENV_FILE). Nothing else from that file is exported and no value is
ever logged. A missing credential raises `swesim.settings.MissingSecret` before any client is built.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from swesim.costs import Usage
from swesim.roles import ModelIdError, parse_model_id
from swesim.settings import MissingSecret, Settings
from swesim.settings import secret as _settings_secret


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatResult:
    data: dict      # the parsed JSON object
    usage: Usage    # tokens summed over every attempt the call made (a fallback attempt is billed too)


class MissingCredential(LLMError, MissingSecret):
    """The role's credential is neither in the process environment nor in the env file. Names the variable, never a value."""

ANTHROPIC_THINKING_BUDGET = 8000  # max_tokens headroom for Claude's adaptive thinking block, on top of the caller's max_tokens
ANTHROPIC_EFFORT = "low"        # output_config.effort for structured-JSON calls (judge/draft/stakeholder), bounded latency


ROLE_PROVIDERS = ("anthropic", "openai")   # pipeline roles never run through OpenRouter (brief section 3.3)
# OpenAI gpt-5.x and o-series reject non-default temperature; Anthropic SDK 1.x (Claude 5 family) has no temperature
# parameter at all. Probe evidence: workspace/evidence/model_probe.json (integrator commit 3b49b05).
_NO_TEMPERATURE_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _secret(name: str, settings: Settings | None = None) -> str:
    """One required credential through `swesim.settings.secret` (tests stub this seam). Without an explicit `settings`
    the current process environment decides the env file, so a caller that sets SWESIM_ENV_FILE late is honoured."""
    try:
        return _settings_secret(name, settings or Settings.from_env())
    except MissingSecret as e:
        raise MissingCredential(str(e)) from e


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m: raise LLMError(f"no JSON in model output: {text[:200]}")
    try:
        data = json.loads(m.group(0))
    except ValueError as e:
        raise LLMError(f"model output is not valid JSON: {e}") from e
    if not isinstance(data, dict): raise LLMError("model output JSON is not an object")
    return data


OPENAI_REASONING_HEADROOM = 8000   # completion-token headroom for gpt-5/o-series reasoning tokens, on top of the caller's max_tokens
OPENAI_REASONING_EFFORTS = ("low", "minimal")  # first attempt, then the retry when the answer came back empty (finish_reason length)


def _openai_kwargs(name: str, temperature: float | None, max_tokens: int, effort: str | None = None) -> dict:
    kwargs: dict = {"response_format": {"type": "json_object"}, "max_completion_tokens": max_tokens}
    if temperature is not None and not name.startswith(_NO_TEMPERATURE_PREFIXES): kwargs["temperature"] = temperature
    if name.startswith(_NO_TEMPERATURE_PREFIXES):
        # Reasoning models spend completion tokens on hidden reasoning first; observed live: gpt-5.5 returned empty
        # content under a 4000-token cap on the judge prompt. Bound the effort and add headroom.
        kwargs["max_completion_tokens"] = max_tokens + OPENAI_REASONING_HEADROOM
        kwargs["reasoning_effort"] = effort or OPENAI_REASONING_EFFORTS[0]
    return kwargs


def _int(obj, name: str) -> int:
    return int(getattr(obj, name, 0) or 0)


def _anthropic_usage(r) -> Usage:
    """Anthropic reports cache reads and writes beside `input_tokens`; every prompt token counts as input, reads as cached."""
    u = getattr(r, "usage", None)
    if u is None:
        return Usage(calls=1)
    read = _int(u, "cache_read_input_tokens")
    return Usage(input_tokens=_int(u, "input_tokens") + read + _int(u, "cache_creation_input_tokens"), output_tokens=_int(u, "output_tokens"),
                 cached_tokens=read, calls=1)


def _openai_usage(r) -> Usage:
    u = getattr(r, "usage", None)
    if u is None:
        return Usage(calls=1)
    details = getattr(u, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    return Usage(input_tokens=_int(u, "prompt_tokens"), output_tokens=_int(u, "completion_tokens"),
                 cached_tokens=None if cached is None else int(cached), calls=1)


def chat_json_with_usage(model_id: str, system: str, messages: list[dict], schema_hint: str, temperature: float | None = None,
                         max_tokens: int = 4000, *, settings: Settings | None = None) -> ChatResult:
    """messages: [{"role": "user"|"assistant", "content": str}]. Returns the parsed JSON object with the usage of every
    attempt the call made. Provider by id prefix (roles: Anthropic or OpenAI).

    `temperature` is accepted for callers' config compatibility but only forwarded where the provider supports it
    (see _NO_TEMPERATURE_PREFIXES); it is never forwarded to the Anthropic SDK. `settings` selects the env file the
    credential is read from (default: the process settings)."""
    try:
        mid = parse_model_id(model_id)
    except ModelIdError as e:
        raise LLMError(str(e)) from e
    if mid.provider not in ROLE_PROVIDERS:
        raise LLMError(f"pipeline roles run on {ROLE_PROVIDERS} only; {model_id!r} is a {mid.provider} id")
    name = mid.api_name
    sys_prompt = system + "\n\nRespond with a single JSON object only, matching this schema:\n" + schema_hint
    used = Usage()
    if mid.provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=_secret("ANTHROPIC_API_KEY", settings), timeout=120, max_retries=1)
        # Claude 5 models think by default and the thinking block shares max_tokens with the answer: an 8k budget can
        # be consumed entirely by thinking (observed: stop_reason max_tokens, blocks ['thinking'], no text). Claude 5
        # only accepts thinking.type=adaptive (with output_config.effort) or disabled. Try adaptive thinking at low
        # effort with headroom for the thinking block, then fall back to no thinking if no text block arrives.
        attempts = ({"thinking": {"type": "adaptive"}, "output_config": {"effort": ANTHROPIC_EFFORT}, "headroom": ANTHROPIC_THINKING_BUDGET},
                    {"thinking": {"type": "disabled"}, "headroom": 0})
        last = None
        for i, a in enumerate(attempts):
            kwargs = {k: v for k, v in a.items() if k != "headroom"}
            try:
                r = client.messages.create(model=name, system=sys_prompt, messages=messages, max_tokens=max_tokens + a["headroom"], **kwargs)
            except Exception as e:  # a model that rejects adaptive thinking / output_config (400) falls back to the plain request
                if i == 0 and type(e).__name__ in ("BadRequestError", "NotFoundError", "UnprocessableEntityError"):
                    last = f"{type(e).__name__}: {str(e)[:200]} thinking={a['thinking']['type']}"
                    continue
                raise
            used = used + _anthropic_usage(r)
            text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
            if text.strip():
                return ChatResult(_extract_json(text), used)
            last = f"stop_reason={r.stop_reason} blocks={[getattr(b, 'type', '?') for b in r.content]} thinking={a['thinking']['type']}"
        raise LLMError(f"no text block from {model_id}: {last}")
    from openai import OpenAI
    client = OpenAI(api_key=_secret("OPENAI_API_KEY", settings), timeout=120, max_retries=1)
    efforts = OPENAI_REASONING_EFFORTS if name.startswith(_NO_TEMPERATURE_PREFIXES) else (None,)
    last = None
    for effort in efforts:
        r = client.chat.completions.create(model=name, messages=[{"role": "system", "content": sys_prompt}, *messages], **_openai_kwargs(name, temperature, max_tokens, effort))
        used = used + _openai_usage(r)
        text = r.choices[0].message.content or ""
        if text.strip():
            return ChatResult(_extract_json(text), used)
        last = f"finish_reason={r.choices[0].finish_reason} effort={effort}"
    raise LLMError(f"no text from {model_id}: {last}")


def chat_json(model_id: str, system: str, messages: list[dict], schema_hint: str, temperature: float | None = None, max_tokens: int = 4000,
              *, settings: Settings | None = None) -> dict:
    """`chat_json_with_usage(...).data`: the parsed JSON object for callers that do not record cost."""
    return chat_json_with_usage(model_id, system, messages, schema_hint, temperature, max_tokens, settings=settings).data
