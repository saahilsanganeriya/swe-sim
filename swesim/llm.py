"""Minimal JSON-returning chat client. Provider is chosen by the model id prefix (`anthropic/...`, `openai/...`).

Credentials: the process environment first, then only the required key from the python-dotenv file named by
SWESIM_ENV_FILE (default ~/Documents/.env). Nothing else from that file is exported and no value is ever logged.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


class LLMError(RuntimeError):
    pass

ANTHROPIC_THINKING_BUDGET = 8000  # max_tokens headroom for Claude's adaptive thinking block, on top of the caller's max_tokens
ANTHROPIC_EFFORT = "low"        # output_config.effort for structured-JSON calls (judge/draft/stakeholder), bounded latency


_SECRET_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
# OpenAI gpt-5.x and o-series reject non-default temperature; Anthropic SDK 1.x (Claude 5 family) has no temperature
# parameter at all. Probe evidence: workspace/evidence/model_probe.json (integrator commit 3b49b05).
_NO_TEMPERATURE_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _env_file() -> Path:
    return Path(os.environ.get("SWESIM_ENV_FILE", str(Path.home() / "Documents" / ".env")))


def _secret(name: str) -> str:
    """Return one required credential without exporting anything else from the env file."""
    value = os.environ.get(name)
    if not value:
        from dotenv import dotenv_values
        p = _env_file()
        value = dotenv_values(p).get(name) if p.is_file() else None
    if not value: raise LLMError(f"{name} is not set in the environment or in {_env_file()}")
    return value


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


def chat_json(model_id: str, system: str, messages: list[dict], schema_hint: str, temperature: float | None = None, max_tokens: int = 4000) -> dict:
    """messages: [{"role": "user"|"assistant", "content": str}]. Returns parsed JSON. Provider by id prefix.

    `temperature` is accepted for callers' config compatibility but only forwarded where the provider supports it
    (see _NO_TEMPERATURE_PREFIXES); it is never forwarded to the Anthropic SDK."""
    provider, _, name = model_id.partition("/")
    if provider not in _SECRET_KEYS or not name: raise LLMError(f"unknown provider in model id {model_id}")
    sys_prompt = system + "\n\nRespond with a single JSON object only, matching this schema:\n" + schema_hint
    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=_secret("ANTHROPIC_API_KEY"), timeout=120, max_retries=1)
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
            text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
            if text.strip():
                return _extract_json(text)
            last = f"stop_reason={r.stop_reason} blocks={[getattr(b, 'type', '?') for b in r.content]} thinking={a['thinking']['type']}"
        raise LLMError(f"no text block from {model_id}: {last}")
    from openai import OpenAI
    client = OpenAI(api_key=_secret("OPENAI_API_KEY"), timeout=120, max_retries=1)
    efforts = OPENAI_REASONING_EFFORTS if name.startswith(_NO_TEMPERATURE_PREFIXES) else (None,)
    last = None
    for effort in efforts:
        r = client.chat.completions.create(model=name, messages=[{"role": "system", "content": sys_prompt}, *messages], **_openai_kwargs(name, temperature, max_tokens, effort))
        text = r.choices[0].message.content or ""
        if text.strip():
            return _extract_json(text)
        last = f"finish_reason={r.choices[0].finish_reason} effort={effort}"
    raise LLMError(f"no text from {model_id}: {last}")
