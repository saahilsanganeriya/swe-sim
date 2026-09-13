"""Model identities and roles: the id grammar, the fallback chains and tiers verbatim from the brief, judge != draft
resolution with recorded reasoning, and the provider-aware reachability probe (offline: the SDK clients are faked)."""
from __future__ import annotations

import json
import sys
import types

import pytest

from swesim import roles
from swesim.settings import Settings

OPUS, SOL, GPT55 = "anthropic/claude-opus-5", "openai/gpt-5.6-sol", "openai/gpt-5.5"
SONNET = "anthropic/claude-sonnet-5"
SCREEN = "openrouter/deepseek/deepseek-v3.2"
ENV = {"ANTHROPIC_API_KEY": "sk-ant-fake-1234567890", "OPENAI_API_KEY": "sk-oa-fake-1234567890", "OPENROUTER_API_KEY": "sk-or-fake-1234567890"}


# ------------------------------------------------------------------ id grammar

def test_model_id_grammar_is_provider_slash_vendor_or_model_slash_model():
    m = roles.parse_model_id("openrouter/deepseek/deepseek-v3.2")
    assert (m.provider, m.vendor, m.name) == ("openrouter", "deepseek", "deepseek-v3.2")
    assert m.litellm == "openrouter/deepseek/deepseek-v3.2" and m.api_name == "deepseek/deepseek-v3.2"   # what OpenRouter's API receives
    a = roles.parse_model_id("anthropic/claude-haiku-4-5-20251001")
    assert (a.provider, a.vendor, a.name, a.api_name) == ("anthropic", None, "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001")
    assert roles.parse_model_id("openai/gpt-5.5").api_name == "gpt-5.5"
    for bad in ("mistral/x", "anthropic", "anthropic/", "openrouter/deepseek", "openrouter/deepseek/", "anthropic/a/b", "openrouter//m"):
        with pytest.raises(roles.ModelIdError):
            roles.parse_model_id(bad)
    assert issubclass(roles.ModelIdError, ValueError)                                      # invalid input: non-retryable


def test_providers_carry_the_host_and_the_secret_name_each_solver_needs():
    assert roles.PROVIDERS["openrouter"].host == "openrouter.ai" and roles.PROVIDERS["openrouter"].key_env == "OPENROUTER_API_KEY"
    assert roles.PROVIDERS["openrouter"].base_url == "https://openrouter.ai/api/v1"
    assert roles.PROVIDERS["anthropic"].host == "api.anthropic.com" and roles.PROVIDERS["openai"].host == "api.openai.com"
    assert roles.PROVIDERS["anthropic"].base_url is None and roles.PROVIDERS["openai"].base_url is None
    assert roles.key_env_for("openrouter/z-ai/glm-5.3") == "OPENROUTER_API_KEY" and roles.key_env_for(OPUS) == "ANTHROPIC_API_KEY"


# ------------------------------------------------------------------ chains and tiers (verbatim, brief section 3)

def test_chains_and_tiers_are_the_briefs_ids_verbatim():
    assert roles.ROLE_CHAINS == {
        "judge_model": ["anthropic/claude-opus-5", "openai/gpt-5.6-sol", "openai/gpt-5.5"],
        "draft_model": ["anthropic/claude-sonnet-5", "openai/gpt-5.6-sol"],
        "stakeholder_model": ["anthropic/claude-opus-5", "openai/gpt-5.6-sol", "anthropic/claude-sonnet-5"],
        "audit_model": ["anthropic/claude-opus-5", "openai/gpt-5.6-sol"],
        "screen_model": ["openrouter/deepseek/deepseek-v3.2"],
    }
    assert roles.TIERS["A"] == ["openrouter/deepseek/deepseek-chat-v3-0324", "openrouter/z-ai/glm-4.5", "openrouter/qwen/qwen3-coder",
                                "openrouter/moonshotai/kimi-k2", "openrouter/minimax/minimax-m2", "openrouter/openai/gpt-oss-120b"]
    assert roles.TIERS["B"] == ["openrouter/deepseek/deepseek-v3.2", "openrouter/deepseek/deepseek-v4-flash", "openrouter/deepseek/deepseek-v4-pro-0813",
                                "openrouter/z-ai/glm-5.2", "openrouter/z-ai/glm-5.3", "openrouter/z-ai/glm-5.3-flash", "openrouter/qwen/qwen3.6-plus",
                                "openrouter/moonshotai/kimi-k2.7-code", "openrouter/minimax/minimax-m3"]
    assert roles.TIERS["C"] == ["openai/gpt-5-mini", "openai/gpt-5.6-luna", "anthropic/claude-haiku-4-5-20251001"]
    assert roles.TIERS["D"] == ["anthropic/claude-sonnet-5", "openai/gpt-5.6-sol"]
    assert roles.TIERS["E"] == ["openai/gpt-6-astra", "anthropic/claude-fable-5-1", "anthropic/claude-opus-5", "openai/gpt-5.5"]
    ids = roles.ALL_PROBE_IDS
    assert len(ids) == len(set(ids)) and "openrouter/openai/gpt-5.6-luna" in ids
    for chain in roles.ROLE_CHAINS.values():
        assert set(chain) <= set(ids)
    for tier in roles.TIERS.values():
        assert set(tier) <= set(ids)
    for m in ids:
        roles.parse_model_id(m)                                                            # every id parses


# ------------------------------------------------------------------ resolution

def _probe(unavailable: set[str]):
    calls: list[str] = []

    def probe(model_id: str) -> bool:
        calls.append(model_id)
        return model_id not in unavailable

    probe.calls = calls  # type: ignore[attr-defined]
    return probe


def test_resolve_roles_all_available_picks_chain_heads_and_probes_each_id_once():
    p = _probe(set())
    r = roles.resolve_roles(p)
    assert r.roles == {"judge_model": OPUS, "draft_model": SONNET, "stakeholder_model": OPUS, "audit_model": OPUS, "screen_model": SCREEN}
    assert r.configured == r.roles and r.substitutions == {} and r.reasoning == [] and r.needs_human == []
    assert len(p.calls) == len(set(p.calls))


def test_resolve_roles_falls_back_along_each_chain_and_records_why():
    r = roles.resolve_roles(_probe({OPUS}))
    assert r.roles["judge_model"] == SOL and r.roles["stakeholder_model"] == SOL and r.roles["audit_model"] == SOL and r.roles["draft_model"] == SONNET
    assert r.substitutions == {"judge_model": SOL, "stakeholder_model": SOL, "audit_model": SOL}
    assert any("judge_model" in line and OPUS in line and SOL in line for line in r.reasoning)
    assert r.needs_human == []


def test_resolve_roles_honours_configured_ids_before_the_chain():
    r = roles.resolve_roles(_probe(set()), configured={"judge_model": GPT55})
    assert r.roles["judge_model"] == GPT55 and r.configured["judge_model"] == GPT55 and r.substitutions == {} and r.reasoning == []
    r2 = roles.resolve_roles(_probe({GPT55}), configured={"judge_model": GPT55})
    assert r2.roles["judge_model"] == OPUS and r2.substitutions == {"judge_model": OPUS}       # chain head is the first fallback


def test_resolve_roles_keeps_judge_and_draft_distinct_by_advancing_the_draft_chain():
    r = roles.resolve_roles(_probe(set()), configured={"judge_model": SONNET})
    assert r.roles["judge_model"] == SONNET and r.roles["draft_model"] == SOL and r.roles["judge_model"] != r.roles["draft_model"]
    assert any("draft" in line and SOL in line for line in r.reasoning) and r.needs_human == []


def test_resolve_roles_advances_the_judge_chain_when_the_draft_chain_is_exhausted():
    r = roles.resolve_roles(_probe({SOL}), configured={"judge_model": SONNET})
    assert r.roles["draft_model"] == SONNET and r.roles["judge_model"] == OPUS
    assert any("judge" in line and OPUS in line for line in r.reasoning) and r.needs_human == []


def test_resolve_roles_reports_needs_human_instead_of_equal_ids_or_silent_gaps():
    r = roles.resolve_roles(_probe({OPUS, SOL, GPT55, SCREEN}), configured={"judge_model": SONNET})
    assert r.roles["judge_model"] == SONNET and r.roles["draft_model"] == SONNET                 # best effort is recorded, never hidden
    assert any("judge" in line and "draft" in line and "no distinct" in line for line in r.needs_human)
    assert any("screen_model" in line and SCREEN in line for line in r.needs_human)
    assert r.roles["screen_model"] == SCREEN


# ------------------------------------------------------------------ probe (fake SDKs)

@pytest.fixture
def settings(tmp_path):
    return Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env")})


def _fake_anthropic(monkeypatch, calls, response=None, error=None):
    class Messages:
        def create(self, **kw):
            calls[-1]["create"] = kw
            if error is not None:
                raise error
            return response

    class Client:
        def __init__(self, **kw):
            calls.append({"client": kw})
            self.messages = Messages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))


def _fake_openai(monkeypatch, calls, response=None, error=None):
    class Completions:
        def create(self, **kw):
            calls[-1]["create"] = kw
            if error is not None:
                raise error
            return response

    class Client:
        def __init__(self, **kw):
            calls.append({"client": kw})
            self.chat = types.SimpleNamespace(completions=Completions())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=Client))


def _no_sdk(monkeypatch):
    class Never:
        def __init__(self, **kw):
            raise AssertionError("no client may be constructed")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Never))
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=Never))


def test_probe_anthropic_id_uses_the_anthropic_sdk_and_records_latency_and_usage(monkeypatch, settings):
    calls: list[dict] = []
    usage = types.SimpleNamespace(input_tokens=8, output_tokens=1, cache_read_input_tokens=2, cache_creation_input_tokens=0)
    _fake_anthropic(monkeypatch, calls, response=types.SimpleNamespace(content=[], stop_reason="end_turn", usage=usage))
    r = roles.probe_id(OPUS, settings=settings, environ=ENV)
    assert r.reachable is True and r.provider == "anthropic" and r.error_class is None and r.error is None and r.served_provider is None
    assert isinstance(r.latency_ms, int) and r.latency_ms >= 0 and r.at.endswith("+00:00")
    assert calls[0]["client"]["api_key"] == ENV["ANTHROPIC_API_KEY"] and calls[0]["client"]["max_retries"] == 0
    assert calls[0]["create"]["model"] == "claude-opus-5" and calls[0]["create"]["max_tokens"] <= 64
    assert r.usage == {"input_tokens": 10, "output_tokens": 1, "cost_usd": None}            # cache reads count as input


def test_probe_openai_id_uses_the_openai_sdk_without_a_base_url(monkeypatch, settings):
    calls: list[dict] = []
    usage = types.SimpleNamespace(prompt_tokens=8, completion_tokens=0, prompt_tokens_details=None, model_extra=None)
    _fake_openai(monkeypatch, calls, response=types.SimpleNamespace(choices=[], usage=usage, model_extra=None))
    r = roles.probe_id(SOL, settings=settings, environ=ENV)
    assert r.reachable and r.provider == "openai" and r.served_provider is None
    assert calls[0]["client"]["api_key"] == ENV["OPENAI_API_KEY"] and calls[0]["client"].get("base_url") is None
    assert calls[0]["create"]["model"] == "gpt-5.6-sol" and "extra_body" not in calls[0]["create"]


def test_probe_openrouter_id_goes_through_the_openai_sdk_at_the_openrouter_base_url(monkeypatch, settings):
    calls: list[dict] = []
    usage = types.SimpleNamespace(prompt_tokens=8, completion_tokens=1, prompt_tokens_details=None, model_extra={"cost": 0.000012})
    _fake_openai(monkeypatch, calls, response=types.SimpleNamespace(choices=[], usage=usage, model_extra={"provider": "DeepInfra"}))
    r = roles.probe_id(SCREEN, settings=settings, environ=ENV)
    assert r.reachable and r.provider == "openrouter" and r.served_provider == "DeepInfra"
    c = calls[0]
    assert c["client"]["api_key"] == ENV["OPENROUTER_API_KEY"] and c["client"]["base_url"] == "https://openrouter.ai/api/v1"
    assert c["create"]["model"] == "deepseek/deepseek-v3.2" and c["create"]["extra_body"] == {"usage": {"include": True}}
    assert r.usage == {"input_tokens": 8, "output_tokens": 1, "cost_usd": 0.000012}       # OpenRouter's per-generation cost


def test_probe_unreachable_id_records_the_error_class_and_never_the_key(monkeypatch, settings):
    class NotFoundError(Exception):
        pass

    calls: list[dict] = []
    _fake_openai(monkeypatch, calls, error=NotFoundError(f"model not found; authorization: Bearer {ENV['OPENAI_API_KEY']}"))
    r = roles.probe_id("openai/gpt-nope", settings=settings, environ=ENV)
    assert r.reachable is False and r.error_class == "NotFoundError" and isinstance(r.latency_ms, int)
    assert ENV["OPENAI_API_KEY"] not in r.error and "model not found" in r.error and "[redacted" in r.error


def test_probe_missing_secret_or_bad_id_fails_without_constructing_a_client(monkeypatch, settings):
    _no_sdk(monkeypatch)
    r = roles.probe_id(GPT55, settings=settings, environ={})
    assert r.reachable is False and r.error_class == "MissingSecret" and r.latency_ms is None and "sk-" not in (r.error or "")
    r2 = roles.probe_id("mistral/x", settings=settings, environ=ENV)
    assert r2.reachable is False and r2.error_class == "ModelIdError" and r2.provider == "mistral"
    r3 = roles.probe_id("openrouter/deepseek", settings=settings, environ=ENV)
    assert r3.reachable is False and r3.error_class == "ModelIdError"


def test_probe_all_dedupes_ids_and_the_evidence_file_carries_probes_roles_chains_and_tiers(tmp_path):
    def fake(model_id, **kw):
        return roles.ProbeResult(id=model_id, provider=model_id.split("/")[0], reachable=model_id != GPT55, latency_ms=5,
                                 error_class=None if model_id != GPT55 else "RateLimitError", error=None, served_provider=None,
                                 at="2026-09-13T08:00:00+00:00", usage=None)

    results = roles.probe_all([OPUS, GPT55, OPUS], probe=fake)
    assert [r.id for r in results] == [OPUS, GPT55]
    reachable = {r.id for r in results if r.reachable}
    res = roles.resolve_roles(lambda m: m in reachable or m in (SONNET, SOL, SCREEN))
    path = tmp_path / "evidence" / "model-probe.json"
    out = roles.write_probe_evidence(path, results, res)
    saved = json.loads(path.read_text())
    assert saved == out and saved["probes"][1]["reachable"] is False and saved["probes"][1]["error_class"] == "RateLimitError"
    assert saved["reachable"] == [OPUS] and saved["unreachable"] == [GPT55]
    assert saved["roles"]["resolved"] == res.roles and saved["roles"]["reasoning"] == res.reasoning and saved["roles"]["needs_human"] == res.needs_human
    assert saved["chains"] == roles.ROLE_CHAINS and saved["tiers"] == roles.TIERS and saved["probed_at"]
    assert "sk-" not in path.read_text()
    roles.write_probe_evidence(path, results, res)                                        # idempotent: same content, no error
    assert json.loads(path.read_text())["probes"] == saved["probes"]
