from __future__ import annotations
import json, os, re
class LLMError(RuntimeError): pass

def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m: raise LLMError(f"no JSON in model output: {text[:200]}")
    return json.loads(m.group(0))

def chat_json(model_id: str, system: str, messages: list[dict], schema_hint: str, temperature: float | None = None, max_tokens: int = 4000) -> dict:
    """messages: [{"role": "user"|"assistant", "content": str}]. Returns parsed JSON. Provider by id prefix.

    `temperature` is accepted for callers' config compatibility but only forwarded where the provider supports it:
    the Anthropic SDK 1.x (Claude 5 family) has no temperature parameter, and OpenAI gpt-5.x / o-series reject
    non-default values, so it is dropped for those models (probe evidence: workspace/evidence/model_probe.json)."""
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(os.environ.get('SWESIM_ENV_FILE',str(Path.home()/'Documents/.env')))
    provider, _, name = model_id.partition("/")
    sys_prompt = system + "\n\nRespond with a single JSON object only, matching this schema:\n" + schema_hint
    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"],timeout=120,max_retries=1)
        r = client.messages.create(model=name, system=sys_prompt, messages=messages, max_tokens=max_tokens)
        return _extract_json("".join(b.text for b in r.content if getattr(b, "type", "") == "text"))
    if provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"],timeout=120,max_retries=1)
        kwargs: dict = {"response_format": {"type": "json_object"}, "max_completion_tokens": max_tokens}
        if temperature is not None and not name.startswith(("gpt-5", "o1", "o3", "o4")):
            kwargs["temperature"] = temperature
        r = client.chat.completions.create(model=name, messages=[{"role": "system", "content": sys_prompt}, *messages], **kwargs)
        return _extract_json(r.choices[0].message.content or "")
    raise LLMError(f"unknown provider in model id {model_id}")
