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


def _openai_kwargs(name: str, temperature: float | None, max_tokens: int) -> dict:
    kwargs: dict = {"response_format": {"type": "json_object"}, "max_completion_tokens": max_tokens}
    if temperature is not None and not name.startswith(_NO_TEMPERATURE_PREFIXES): kwargs["temperature"] = temperature
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
        r = client.messages.create(model=name, system=sys_prompt, messages=messages, max_tokens=max_tokens)
        return _extract_json("".join(b.text for b in r.content if getattr(b, "type", "") == "text"))
    from openai import OpenAI
    client = OpenAI(api_key=_secret("OPENAI_API_KEY"), timeout=120, max_retries=1)
    r = client.chat.completions.create(model=name, messages=[{"role": "system", "content": sys_prompt}, *messages], **_openai_kwargs(name, temperature, max_tokens))
    return _extract_json(r.choices[0].message.content or "")
