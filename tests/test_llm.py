"""Provider adaptation in swesim.llm.chat_json (offline: the SDK clients are faked); credentials come from
`swesim.settings.secret`, never from a hard-coded env file."""
from __future__ import annotations

import sys
import types

import pytest

from swesim import llm
from swesim.settings import MissingSecret, Settings


class _Block:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class _Resp:
    def __init__(self, blocks, stop_reason="end_turn"):
        self.content = blocks
        self.stop_reason = stop_reason


def _fake_anthropic(monkeypatch, responses, calls, clients=None, stub_secret=True):
    class Messages:
        def create(self, **kw):
            calls.append(kw)
            return responses.pop(0)

    class Client:
        def __init__(self, **kw):
            if clients is not None:
                clients.append(kw)
            self.messages = Messages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))
    if stub_secret:
        monkeypatch.setattr(llm, "_secret", lambda key, settings=None: "x")


def test_anthropic_thinking_only_response_falls_back_to_no_thinking(monkeypatch):
    calls = []
    _fake_anthropic(monkeypatch, [_Resp([_Block("thinking")], "max_tokens"), _Resp([_Block("text", '{"ok": true}')])], calls)
    assert llm.chat_json("anthropic/claude-sonnet-5", "sys", [{"role": "user", "content": "q"}], "{}", max_tokens=100) == {"ok": True}
    assert calls[0]["thinking"] == {"type": "adaptive"} and calls[0]["max_tokens"] == 100 + llm.ANTHROPIC_THINKING_BUDGET
    assert calls[0]["output_config"] == {"effort": llm.ANTHROPIC_EFFORT} and "temperature" not in calls[0]
    assert calls[1]["thinking"] == {"type": "disabled"} and calls[1]["max_tokens"] == 100 and "output_config" not in calls[1]


def test_anthropic_no_text_twice_raises_llmerror_with_evidence(monkeypatch):
    calls = []
    _fake_anthropic(monkeypatch, [_Resp([_Block("thinking")], "max_tokens"), _Resp([], "max_tokens")], calls)
    with pytest.raises(llm.LLMError, match="stop_reason=max_tokens"):
        llm.chat_json("anthropic/claude-sonnet-5", "sys", [{"role": "user", "content": "q"}], "{}", max_tokens=50)
    assert len(calls) == 2


def test_openai_gpt5_gets_no_temperature_and_reasoning_headroom():
    kw = llm._openai_kwargs("gpt-5.5", 0.0, 123)
    assert "temperature" not in kw and kw["max_completion_tokens"] == 123 + llm.OPENAI_REASONING_HEADROOM
    assert kw["reasoning_effort"] == "low"
    kw = llm._openai_kwargs("gpt-4.1", 0.0, 5)
    assert kw["temperature"] == 0.0 and kw["max_completion_tokens"] == 5 and "reasoning_effort" not in kw


def _fake_openai(monkeypatch, responses, calls, clients=None, stub_secret=True):
    class Choice:
        def __init__(self, content, finish):
            self.message = types.SimpleNamespace(content=content); self.finish_reason = finish
    class Completions:
        def create(self, **kw):
            calls.append(kw); content, finish, *rest = responses.pop(0)
            return types.SimpleNamespace(choices=[Choice(content, finish)], usage=rest[0] if rest else None)
    class Client:
        def __init__(self, **kw):
            if clients is not None:
                clients.append(kw)
            self.chat = types.SimpleNamespace(completions=Completions())
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=Client))
    if stub_secret:
        monkeypatch.setattr(llm, "_secret", lambda key, settings=None: "x")


def test_openai_empty_reasoning_answer_retries_with_minimal_effort(monkeypatch):
    calls = []
    _fake_openai(monkeypatch, [("", "length"), ('{"ok": 1}', "stop")], calls)
    assert llm.chat_json("openai/gpt-5.5", "sys", [{"role": "user", "content": "q"}], "{}", max_tokens=100) == {"ok": 1}
    assert [c["reasoning_effort"] for c in calls] == ["low", "minimal"]


def test_openai_empty_twice_raises(monkeypatch):
    calls = []
    _fake_openai(monkeypatch, [("", "length"), ("", "length")], calls)
    with pytest.raises(llm.LLMError, match="finish_reason=length"):
        llm.chat_json("openai/gpt-5.5", "sys", [{"role": "user", "content": "q"}], "{}", max_tokens=100)


def test_anthropic_api_rejection_of_thinking_falls_back_to_plain_request(monkeypatch):
    """A model that rejects adaptive thinking / output_config with a 400 gets the plain request instead of a hard failure."""
    calls = []
    class BadRequestError(Exception): pass
    class Messages:
        def create(self, **kw):
            calls.append(kw)
            if "thinking" in kw and kw["thinking"].get("type") == "adaptive": raise BadRequestError("thinking.type.adaptive not supported")
            return _Resp([_Block("text", '{"ok": 2}')])
    class Client:
        def __init__(self, **kw): self.messages = Messages()
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client, BadRequestError=BadRequestError))
    monkeypatch.setattr(llm, "_secret", lambda key, settings=None: "x")
    assert llm.chat_json("anthropic/claude-haiku-4-5-20251001", "sys", [{"role": "user", "content": "q"}], "{}", max_tokens=50) == {"ok": 2}
    assert calls[0]["thinking"] == {"type": "adaptive"} and calls[1]["thinking"] == {"type": "disabled"} and "output_config" not in calls[1]


# ------------------------------------------------------------------ credentials come from Settings, never a hard-coded path

def test_credentials_are_read_through_settings_env_file_when_absent_from_the_process(monkeypatch, tmp_path):
    f = tmp_path / "creds.env"
    f.write_text("ANTHROPIC_API_KEY=sk-from-file-0001\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = Settings.from_env({"SWESIM_ENV_FILE": str(f)})
    calls, clients = [], []
    _fake_anthropic(monkeypatch, [_Resp([_Block("text", '{"ok": 3}')])], calls, clients, stub_secret=False)
    assert llm.chat_json("anthropic/claude-sonnet-5", "sys", [{"role": "user", "content": "q"}], "{}", settings=s) == {"ok": 3}
    assert clients[0]["api_key"] == "sk-from-file-0001"


def test_missing_credential_raises_missing_secret_without_the_value(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "absent.env")})
    calls, clients = [], []
    _fake_openai(monkeypatch, [('{"ok": 1}', "stop")], calls, clients, stub_secret=False)
    with pytest.raises(MissingSecret) as e:
        llm.chat_json("openai/gpt-5.5", "sys", [{"role": "user", "content": "q"}], "{}", settings=s)
    assert "OPENAI_API_KEY" in str(e.value) and clients == []                            # no client is built without a key
    assert isinstance(e.value, llm.LLMError)                                             # callers catching LLMError still see it


def test_chat_json_with_usage_sums_anthropic_usage_over_both_attempts(monkeypatch):
    calls = []
    r1 = _Resp([_Block("thinking")], "max_tokens")
    r1.usage = types.SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=20, cache_creation_input_tokens=5)
    r2 = _Resp([_Block("text", '{"ok": true}')])
    r2.usage = types.SimpleNamespace(input_tokens=100, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    _fake_anthropic(monkeypatch, [r1, r2], calls)
    res = llm.chat_json_with_usage("anthropic/claude-sonnet-5", "sys", [{"role": "user", "content": "q"}], "{}", max_tokens=100)
    assert res.data == {"ok": True}
    # both attempts are billed: prompt tokens include cache reads and writes; cached = cache reads
    assert (res.usage.input_tokens, res.usage.output_tokens, res.usage.cached_tokens, res.usage.calls) == (225, 60, 20, 2)
    assert res.usage.reported_cost_usd is None and res.usage.agent_reported_cost_usd is None


def test_chat_json_with_usage_reads_openai_usage_and_cached_tokens(monkeypatch):
    calls = []
    u1 = types.SimpleNamespace(prompt_tokens=30, completion_tokens=5, prompt_tokens_details=types.SimpleNamespace(cached_tokens=10))
    u2 = types.SimpleNamespace(prompt_tokens=40, completion_tokens=7, prompt_tokens_details=None)
    _fake_openai(monkeypatch, [("", "length", u1), ('{"ok": 1}', "stop", u2)], calls)
    res = llm.chat_json_with_usage("openai/gpt-5.5", "sys", [{"role": "user", "content": "q"}], "{}", max_tokens=100)
    assert res.data == {"ok": 1} and (res.usage.input_tokens, res.usage.output_tokens, res.usage.cached_tokens, res.usage.calls) == (70, 12, 10, 2)


def test_chat_json_with_usage_tolerates_a_response_without_usage(monkeypatch):
    calls = []
    _fake_anthropic(monkeypatch, [_Resp([_Block("text", '{"ok": true}')])], calls)          # _Resp carries no usage attribute
    res = llm.chat_json_with_usage("anthropic/claude-sonnet-5", "sys", [{"role": "user", "content": "q"}], "{}")
    assert res.data == {"ok": True} and (res.usage.input_tokens, res.usage.output_tokens, res.usage.calls) == (0, 0, 1) and res.usage.cached_tokens is None


def test_unknown_provider_is_rejected_before_any_network(monkeypatch):
    with pytest.raises(llm.LLMError):
        llm.chat_json("mistral/x", "sys", [{"role": "user", "content": "q"}], "{}")
    with pytest.raises(llm.LLMError):
        llm.chat_json("openrouter/deepseek/deepseek-v3.2", "sys", [{"role": "user", "content": "q"}], "{}")   # roles are Anthropic/OpenAI only
