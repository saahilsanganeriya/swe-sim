"""The LLM-assisted part of the task-sense audit: prompt payloads, model fallback, output coercion, usage accounting.

Offline: ``chat_json`` is replaced by a fake that records every call.
"""
from __future__ import annotations

import json

import pytest

from swesim.audit import llm_audit as L
from swesim.audit import rubric as R
from swesim.audit.record import ScenarioBundle, TrialBundle
from swesim.llm import LLMError
from swesim.models import ScenarioSpec, minimal_spec_dict
from swesim.stakeholder.session import CONFIRM_PREFIX


class FakeChat:
    """Stands in for ``swesim.llm.chat_json``: records calls, answers by schema, fails for listed models."""

    def __init__(self, scenario: dict | None = None, trial: dict | None = None, fail: set[str] = frozenset()):
        self.calls: list[dict] = []
        self.scenario = scenario if scenario is not None else {
            "coherent": {"label": True, "reason": "one symptom, one wanted behaviour"},
            "fair_tests": {"label": False, "reason": "t1 pins a private name", "unfair": [{"test": "t1", "why": "private name"}]},
            "plausible_withholding": {"label": True, "reason": "the traceback is a natural omission"}}
        self.trial = trial if trial is not None else {
            "questions": [{"seq": 1, "specific": True, "decision_oriented": True, "fishing": False, "redundant": False,
                           "reason": "asks which exception type"}],
            "replies": [{"seq": 2, "targeted": True, "volunteered": [], "test_or_identifier_disclosed": False,
                         "behaviour_level": True, "reason": "names the behaviour only"}]}
        self.fail = set(fail)

    def __call__(self, model_id, system, messages, schema_hint, temperature=None, max_tokens=4000):
        self.calls.append({"model": model_id, "system": system, "messages": messages, "schema": schema_hint, "max_tokens": max_tokens})
        if model_id in self.fail:
            raise LLMError(f"{model_id} unavailable")
        return self.scenario if "coherent" in schema_hint else self.trial


def spec_with_withholding() -> ScenarioSpec:
    d = minimal_spec_dict()
    d["briefs"][0]["text"] = "Import fails\nTraceback: KeyError raised in loader\nSteps: run `pkg load`."
    d["briefs"].append({"profile": "withheld", "text": "Import fails\nSteps: run `pkg load`.", "provenance": "withheld"})
    d["ledger"].append({"id": "withheld-001", "text": "Reporter's original statement: Traceback: KeyError raised in loader",
                        "kind": "reproduction", "provenance": "original-issue", "source_excerpt": "Traceback: KeyError raised in loader",
                        "source_url": "https://example.com/issue", "discovery": "on-request", "tests": []})
    d["ledger"].append({"id": "f09", "text": "SECRET author-only note about the oracle", "kind": "context", "provenance": "authored",
                        "authored_rationale": "internal", "discovery": "author-only"})
    d["operations"] = [{"type": "withholding", "target": "withheld-001", "rationale": "Removed category: reproduction"}]
    # a requirement with no grounding fact: pinned by the hidden test alone (the B-P1-2 shape)
    d["requirements"].append({"id": "r02", "text": "Bound the backward scan so crafted files fail quickly rather than scanning indefinitely",
                              "fact_id": None, "tests": ["t1"]})
    return ScenarioSpec.model_validate(d)


def scenario_bundle(spec: ScenarioSpec | None = None) -> ScenarioBundle:
    spec = spec or spec_with_withholding()
    return ScenarioBundle.from_spec("example-scenario", spec, test_assets={"test.patch": "def test_raises_value_error(loader):\n    assert 1"},
                                    oracle_diff="--- a/pkg/x.py\n+++ b/pkg/x.py\n-raise KeyError\n+raise ValueError",
                                    forbidden_terms=["test_raises_value_error", "loader"], validation_status="valid",
                                    asset_hashes={"test.patch": "a" * 64, "oracle.diff": "b" * 64})


def trial_bundle(spec: ScenarioSpec | None = None, events: list[dict] | None = None) -> TrialBundle:
    spec = spec or spec_with_withholding()
    events = events if events is not None else [
        {"kind": "message", "ts": 1.0, "seq": 1, "role": "user", "exchange": "ask", "request_id": "r1", "content": "Which exception should be raised?"},
        {"kind": "message", "ts": 2.0, "seq": 2, "role": "assistant", "exchange": "ask", "request_id": "r1", "content": "ValueError.",
         "disclosed_fact_ids": ["f01"], "tier": "full", "patience_remaining": 5, "regenerated": False, "redacted_terms": []},
        {"kind": "message", "ts": 3.0, "seq": 3, "role": "user", "exchange": "confirm", "request_id": "r2",
         "content": CONFIRM_PREFIX + "The loader raises ValueError for a missing key."},
        {"kind": "message", "ts": 4.0, "seq": 4, "role": "assistant", "exchange": "confirm", "request_id": "r2", "content": "Yes, that is it.",
         "disclosed_fact_ids": ["f01"], "tier": "full", "patience_remaining": 3, "regenerated": False, "redacted_terms": []}]
    transcript = {"trial_id": "t" * 24, "events": events, "patience_start": 6, "patience_end": 3, "transcript_hash": "h" * 64}
    return TrialBundle.from_parts("t" * 24, spec, run_config={"condition": "interactive", "brief_profile": "withheld",
                                                              "model": "anthropic/claude-sonnet-5", "stakeholder": {"prompt_version": "v1"}},
                                  record={"scenario_id": "example-scenario", "spec_hash": spec.spec_hash},
                                  report={"applied_paths": ["pkg/x.py"], "integrity_signals": []}, transcript=transcript,
                                  trajectory=None, trajectory_source=None, forbidden_terms=["test_raises_value_error", "loader"],
                                  test_assets={"test.patch": "def test_raises_value_error(loader):\n    assert 1"})


# ---------------------------------------------------------------- payloads (evaluator-only inputs)

def test_scenario_payload_carries_the_evaluator_only_inputs_and_marks_author_only_facts():
    p = L.scenario_payload(scenario_bundle())
    assert "Traceback: KeyError" in p["natural_brief"] and "Traceback" not in p["withheld_brief"]
    assert [r["fact_id"] for r in p["removed_spans"]] == ["withheld-001"]
    assert {f["id"] for f in p["stakeholder_visible_facts"]} == {"f01", "withheld-001"}
    assert [f["id"] for f in p["author_only_facts"]] == ["f09"]
    assert p["requirements"][0]["id"] == "r01" and p["requirements"][0]["fact_id"] == "f01"
    assert p["fail_to_pass"] == ["t1"] and "test_raises_value_error" in p["hidden_tests"]["test.patch"]
    assert "raise ValueError" in p["oracle_diff"]
    assert "forbidden_terms" not in p  # the deny-list is worker material, not part of the reading pass


def test_scenario_payload_without_withholding_says_so():
    p = L.scenario_payload(scenario_bundle(ScenarioSpec.model_validate(minimal_spec_dict())))
    assert p["withheld_brief"] is None and p["removed_spans"] == []


def test_trial_payload_shows_the_brief_the_agent_saw_and_strips_the_confirm_prefix():
    p = L.trial_payload(trial_bundle())
    assert p["brief_profile"] == "withheld" and "Traceback" not in p["brief_seen_by_agent"]
    assert "Traceback: KeyError" in p["natural_brief"]
    ex = {e["seq"]: e for e in p["exchanges"]}
    assert ex[1]["exchange"] == "ask" and ex[1]["role"] == "agent"
    assert ex[3]["content"].startswith("The loader raises") and CONFIRM_PREFIX not in ex[3]["content"]
    assert ex[2]["disclosed_fact_ids"] == ["f01"] and ex[2]["role"] == "stakeholder"
    assert {f["id"] for f in p["stakeholder_visible_facts"]} == {"f01", "withheld-001"}
    assert "test_raises_value_error" in p["hidden_tests"]["test.patch"] and p["fail_to_pass"] == ["t1"]


def test_payloads_are_bounded():
    b = scenario_bundle()
    b.test_assets["test.patch"] = "x" * (L.MAX_FIELD_CHARS + 10_000)
    p = L.scenario_payload(b)
    assert len(p["hidden_tests"]["test.patch"]) <= L.MAX_FIELD_CHARS + 100 and p["hidden_tests"]["test.patch"].endswith("[truncated]")


# ---------------------------------------------------------------- the judge: prompts, fallback, usage

def test_judge_uses_opus_first_with_versioned_prompts_and_the_rubric():
    chat = FakeChat()
    judge = L.Judge(chat=chat)
    out, model = judge.judge_scenario(scenario_bundle())
    assert model == "anthropic/claude-opus-5" == L.AUDIT_MODEL and out["coherent"] is True and out["fair_tests"] is False
    call = chat.calls[0]
    assert L.SCENARIO_PROMPT_VERSION in call["system"] and R.RUBRIC["fair_tests"] in call["system"]
    payload = json.loads(call["messages"][0]["content"])
    assert "hidden_tests" in payload and payload["fail_to_pass"] == ["t1"]
    out, model = judge.judge_trial(trial_bundle())
    assert model == L.AUDIT_MODEL and out["questions"][1]["specific"] is True and out["replies"][2]["behaviour_level"] is True
    assert L.TRIAL_PROMPT_VERSION in chat.calls[1]["system"] and R.RUBRIC["fishing"] in chat.calls[1]["system"]


def test_judge_falls_back_to_the_second_model_and_reports_both_failures():
    chat = FakeChat(fail={L.AUDIT_MODEL})
    judge = L.Judge(chat=chat)
    out, model = judge.judge_scenario(scenario_bundle())
    assert model == L.AUDIT_FALLBACK_MODEL == "openai/gpt-5.6-sol" and out["coherent"] is True
    assert [c["model"] for c in chat.calls] == [L.AUDIT_MODEL, L.AUDIT_FALLBACK_MODEL]
    both = FakeChat(fail={L.AUDIT_MODEL, L.AUDIT_FALLBACK_MODEL})
    with pytest.raises(L.AuditLLMError, match="gpt-5.6-sol"):
        L.Judge(chat=both).judge_scenario(scenario_bundle())
    usage = L.Judge(chat=both).usage()
    assert usage["calls"] == 0


def test_usage_counts_calls_and_estimates_cost_from_prompt_size():
    chat = FakeChat()
    judge = L.Judge(chat=chat)
    judge.judge_scenario(scenario_bundle())
    judge.judge_trial(trial_bundle())
    u = judge.usage()
    assert u["calls"] == 2 and u["by_model"][L.AUDIT_MODEL]["calls"] == 2 and u["failed_calls"] == 0
    assert u["estimated_input_tokens"] > 0 and u["estimated_cost_usd"] > 0
    assert u["price_source"] == L.PRICE_SOURCE and u["estimate"] is True


def test_production_audit_keeps_provider_usage_and_prices_fallback(monkeypatch):
    from swesim.costs import Usage, usd_for
    from swesim.llm import ChatResult
    usage = Usage(input_tokens=1234, output_tokens=56, cached_tokens=100, calls=1)
    payload = FakeChat()(L.AUDIT_FALLBACK_MODEL, "system", [], "schema")
    monkeypatch.setattr(L, "chat_json_with_usage", lambda *a, **kw: ChatResult(payload, usage))
    judge = L.Judge(models=(L.AUDIT_FALLBACK_MODEL,))
    judge.judge_scenario(scenario_bundle())
    result = judge.usage()
    assert result["input_tokens"] == 1234 and result["output_tokens"] == 56
    assert result["usage_complete"] is True and result["estimate"] is False
    assert result["cost_usd"] == float(usd_for(L.AUDIT_FALLBACK_MODEL, usage)[0]) > 0


# ---------------------------------------------------------------- coercion of model output

def test_coerce_scenario_accepts_strings_and_fills_unknowns():
    out = L.coerce_scenario({"coherent": {"label": "true", "reason": "ok"}, "fair_tests": {"label": "no"},
                             "plausible_withholding": {"label": None, "reason": "n/a"}})
    assert out["coherent"] is True and out["fair_tests"] is False and out["plausible_withholding"] is None
    assert out["reasons"]["coherent"] == "ok" and out["reasons"]["fair_tests"] and out["unfair_tests"] == []
    out = L.coerce_scenario({"coherent": True, "fair_tests": {"label": False, "unfair": [{"test": "t1"}, "t2"]}})
    assert out["coherent"] is True and out["unfair_tests"] == ["t1", "t2"]
    with pytest.raises(L.AuditOutputError):
        L.coerce_scenario([])


def test_coerce_trial_keys_labels_by_seq_and_ignores_unknown_shapes():
    qs, rs = L.coerce_trial({"questions": [{"seq": "1", "specific": "yes", "decision_oriented": False, "fishing": None, "reason": "r"},
                                           {"seq": None}, "junk"],
                             "replies": [{"seq": 2, "targeted": "false", "volunteered": "f02", "test_or_identifier_disclosed": True,
                                          "behaviour_level": 1, "reason": "names a test"}]})
    assert qs[1] == {"specific": True, "decision_oriented": False, "fishing": None, "redundant": None, "reason": "r"}
    assert rs[2]["targeted"] is False and rs[2]["volunteered"] == ["f02"] and rs[2]["behaviour_level"] is None
    assert rs[2]["test_or_identifier_disclosed"] is True and rs[2]["reason"] == "names a test"
    assert L.coerce_trial({}) == ({}, {})
