"""Provider adaptation in swesim.llm.chat_json (offline: the SDK clients are faked)."""
from __future__ import annotations

import types

import pytest

from swesim import llm


class _Block:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class _Resp:
    def __init__(self, blocks, stop_reason="end_turn"):
        self.content = blocks
        self.stop_reason = stop_reason


def _fake_anthropic(monkeypatch, responses, calls):
    class Messages:
        def create(self, **kw):
            calls.append(kw)
            return responses.pop(0)

    class Client:
        def __init__(self, **kw):
            self.messages = Messages()

    monkeypatch.setitem(__import__("sys").modules, "anthropic", types.SimpleNamespace(Anthropic=Client))
    monkeypatch.setattr(llm, "_secret", lambda key: "x")


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


def test_openai_gpt5_gets_no_temperature_and_max_completion_tokens():
    kw = llm._openai_kwargs("gpt-5.5", 0.0, 123)
    assert "temperature" not in kw and kw["max_completion_tokens"] == 123
    kw = llm._openai_kwargs("gpt-4.1", 0.0, 5)
    assert kw["temperature"] == 0.0
