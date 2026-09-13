"""The reading pass of the task-sense audit: the judgements that need reading, made by ``anthropic/claude-opus-5``.

Contract
- Prompts are versioned constants (``SCENARIO_PROMPT_VERSION``, ``TRIAL_PROMPT_VERSION``) that render the rubric from
  ``swesim.audit.rubric.RUBRIC`` so the docs, the prompt and the labels share one definition. A prompt change bumps the
  version, which is part of the audit's inputs hash.
- Inputs are evaluator-only and passed as one JSON payload: the natural brief, the withheld brief and removed spans, the
  stakeholder-visible facts (author-only facts listed separately, marked), the requirements with their fact grounding, the
  hidden test source, the contract ids with the FAIL_TO_PASS and PASS_TO_PASS test bodies found in the assets (for the
  ``fact_scope`` judgement), the oracle diff (scenario) or the transcript (trial). Large fields are clipped at
  ``MAX_FIELD_CHARS``, PASS_TO_PASS ids at ``MAX_P2P_IDS``. The deny-list (``forbidden_terms``) is worker material and never
  enters a prompt.
- ``Judge`` calls ``swesim.llm.chat_json_with_usage`` on the configured model and follows the role fallback chain on ``LLMError``;
  when both fail it raises ``AuditLLMError`` (retryable). Every call is recorded in ``Judge.calls`` for the usage report;
  provider usage is retained and priced through the shared price table. Character estimates are labelled for older injected clients.
- Model output is coerced, never trusted: labels become booleans or ``None`` (unknown), ids become string lists, reasons
  become strings; a structurally unusable answer raises ``AuditOutputError``.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from swesim.audit.record import ScenarioBundle, TrialBundle, strip_confirm_prefix
from swesim.audit.rubric import parse_test_id, rubric_text, test_bodies
from swesim.costs import Usage, usd_for
from swesim.llm import ChatResult, LLMError, chat_json_with_usage, take_last_usage

AUDIT_MODEL = "anthropic/claude-opus-5"
AUDIT_FALLBACK_MODEL = "openai/gpt-5.6-sol"
SCENARIO_PROMPT_VERSION = "task-sense-scenario-v2"     # v2 (2026-09-13): per-fact `fact_scope` judgement with F2P/P2P bodies in context
TRIAL_PROMPT_VERSION = "task-sense-trial-v1"
MAX_FIELD_CHARS = 40_000
MAX_P2P_IDS = 300            # PASS_TO_PASS ids listed in the payload (the count is always given in full)
MAX_P2P_BODIES = 40          # PASS_TO_PASS bodies found in the assets that enter the payload
MAX_TOKENS = 6000
CHARS_PER_TOKEN = 4.0
# Anthropic first-party list price for claude-opus-5 (input, output) per million tokens; the fallback model has no price
# on record here and is estimated at zero, which the report states.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {AUDIT_MODEL: (5.0, 25.0)}
PRICE_SOURCE = "claude-api skill model table cached 2026-06-24 (claude-opus-5: $5 in / $25 out per MTok); tokens estimated at 4 chars/token"

SCENARIO_SYSTEM = f"""You are the evaluator of an interactive software-engineering benchmark ({SCENARIO_PROMPT_VERSION}).
A task is built from a real issue: the agent receives a brief (the natural issue text, or a withheld variant with spans
removed), can question a simulated stakeholder who knows only the brief, the stakeholder-visible facts and the requirements,
and is graded by hidden tests the agent never sees. You see everything, including the hidden tests and the oracle diff.
Decide four things about the task itself and give one sentence of evidence for each:
{rubric_text()}
Rules: judge against the payload only; a test is unfair only if nothing in the brief, facts or requirements lets a careful
developer arrive at what it checks (name every such test in `unfair`); when `withheld_brief` is null return null for
plausible_withholding; be concrete and quote the payload where it helps. For `fact_scope` judge every entry of
`stakeholder_visible_facts` (never the author-only ones) and answer: does this fact assert behaviour the hidden FAIL_TO_PASS
tests (`fail_to_pass`, `fail_to_pass_bodies`) do not require, or that a PASS_TO_PASS test (`pass_to_pass`,
`pass_to_pass_bodies`) already pins or contradicts? Such a fact is outside the contract (`within_contract` false): an agent
acting on it changes more than the failing tests ask and can regress a passing test. A parametrised test's sibling ids
share one body; only the ids in `fail_to_pass` are required. Name the test that makes the fact out of scope in the reason."""

SCENARIO_SCHEMA = ('{"coherent": {"label": true|false, "reason": "one sentence"}, '
                   '"fair_tests": {"label": true|false, "reason": "one sentence", "unfair": [{"test": "test id from fail_to_pass", "why": "one sentence"}]}, '
                   '"plausible_withholding": {"label": true|false|null, "reason": "one sentence"}, '
                   '"fact_scope": [{"fact_id": "id from stakeholder_visible_facts", "within_contract": true|false, "reason": "one sentence naming the test"}]}')

TRIAL_SYSTEM = f"""You are the evaluator of an interactive software-engineering benchmark ({TRIAL_PROMPT_VERSION}).
The agent received `brief_seen_by_agent` and could ask a simulated stakeholder (`exchanges`, role agent/stakeholder, kinds
`ask` = question, `confirm` = the agent describes the behaviour it built and the stakeholder confirms or names an unmet
requirement, `review` = legacy diff review). The stakeholder knew only `natural_brief`, `stakeholder_visible_facts` and
`requirements`; it must speak as the reporter, in behaviour terms, and never name tests, verification steps, identifiers
outside the brief, or pick among implementation options the agent offers. You also see `hidden_tests`, which neither party
saw. Label every agent `ask` (questions) and every stakeholder turn (replies) with one sentence of evidence each:
{rubric_text()}
Rules: `before_implementation` is computed by code, do not return it. `redundant` means the brief the agent saw already
answers the question. `test_or_identifier_disclosed` is true when the reply names a test, a verification step, a function,
class, file or path not present in the brief/facts/requirements, or states a behaviour or value that only `hidden_tests`
pin (compare against the brief, facts and requirements; a requirement with `fact_id` null and only `tests` behind it counts
as test-derived). `behaviour_level` is false when the reply picks an implementation option, prescribes a code path, or
describes how the change will be checked. `targeted` is null when the reply disclosed no facts; list volunteered fact ids
(disclosed but not asked about) in `volunteered`. Use the `seq` of each exchange."""

TRIAL_SCHEMA = ('{"questions": [{"seq": int, "specific": true|false, "decision_oriented": true|false, "fishing": true|false, '
                '"redundant": true|false, "reason": "one sentence"}], '
                '"replies": [{"seq": int, "targeted": true|false|null, "volunteered": ["fact ids"], "test_or_identifier_disclosed": true|false, '
                '"behaviour_level": true|false, "reason": "one sentence"}]}')


class AuditLLMError(RuntimeError):
    """Every model in the chain failed; transient, the activity may retry."""


class AuditOutputError(ValueError):
    """The model returned something that is not an audit answer."""


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    return text if len(text) <= MAX_FIELD_CHARS else text[:MAX_FIELD_CHARS] + "\n[truncated]"


def contract_bodies(fail_to_pass: list[str], pass_to_pass: list[str], test_texts: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """(FAIL_TO_PASS id -> body, PASS_TO_PASS id -> body) for the ids whose test function is in ``test_texts``; sibling
    parametrisations map to the same body. PASS_TO_PASS bodies are capped at ``MAX_P2P_BODIES``."""
    bodies = test_bodies(test_texts)
    f2p = {t: bodies[parse_test_id(t).name] for t in fail_to_pass if parse_test_id(t).name in bodies}
    p2p: dict[str, str] = {}
    for t in pass_to_pass:
        if len(p2p) >= MAX_P2P_BODIES:
            break
        name = parse_test_id(t).name
        if name in bodies:
            p2p[t] = bodies[name]
    return f2p, p2p


def scenario_payload(b: ScenarioBundle) -> dict:
    """Evaluator-only inputs for the scenario judgement (see the module contract)."""
    f2p_bodies, p2p_bodies = contract_bodies(b.fail_to_pass, b.pass_to_pass, b.test_assets)
    return {
        "scenario_id": b.scenario_id, "spec_hash": b.spec_hash, "source": b.spec.source.value, "source_ref": b.spec.source_ref,
        "persona": {"role": b.spec.persona.role, "summary": b.spec.persona.summary},
        "natural_brief": _clip(b.natural_brief), "withheld_brief": _clip(b.withheld_brief), "removed_spans": b.removed_spans,
        "stakeholder_visible_facts": b.visible_facts, "author_only_facts": b.author_only_facts, "requirements": b.requirements,
        "fail_to_pass": b.fail_to_pass, "pass_to_pass": b.pass_to_pass[:MAX_P2P_IDS], "pass_to_pass_count": len(b.pass_to_pass),
        "fail_to_pass_bodies": {t: _clip(body) for t, body in f2p_bodies.items()},
        "pass_to_pass_bodies": {t: _clip(body) for t, body in p2p_bodies.items()},
        "hidden_tests": {name: _clip(text) for name, text in b.test_assets.items()},
        "oracle_diff": _clip(b.oracle_diff),
    }


def trial_payload(b: TrialBundle) -> dict:
    """Evaluator-only inputs for the transcript judgement (see the module contract)."""
    exchanges = []
    for e in b.messages:
        role = "agent" if e.get("role") == "user" else "stakeholder"
        item = {"seq": e.get("seq"), "role": role, "exchange": e.get("exchange"),
                "content": strip_confirm_prefix(e.get("content", "")) if role == "agent" else e.get("content", "")}
        if role == "stakeholder":
            item["disclosed_fact_ids"] = list(e.get("disclosed_fact_ids") or [])
            item["patience_remaining"] = e.get("patience_remaining")
        exchanges.append(item)
    return {
        "trial_id": b.trial_id, "scenario_id": b.scenario_id, "solver_model": b.solver_model, "brief_profile": b.brief_profile,
        "brief_seen_by_agent": _clip(b.brief_seen_by_agent), "natural_brief": _clip(b.natural_brief),
        "stakeholder_visible_facts": b.visible_facts, "requirements": b.requirements, "fail_to_pass": b.fail_to_pass,
        "hidden_tests": {name: _clip(text) for name, text in b.test_assets.items()}, "exchanges": exchanges,
    }


# ---------------------------------------------------------------- coercion

def _bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("true", "yes"):
            return True
        if low in ("false", "no"):
            return False
    return None


def _int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _ids(v) -> list[str]:
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, list):
        return [x for x in v if isinstance(x, str) and x]
    return []


def _reason(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def coerce_scenario(out) -> dict:
    """{coherent, fair_tests, plausible_withholding: bool|None, reasons: {label: str}, unfair_tests: [str],
    fact_scope: {fact_id: {within_contract: bool|None, reason: str}}} (first entry per fact id wins; malformed items dropped)."""
    if not isinstance(out, dict):
        raise AuditOutputError(f"scenario judgement is not an object: {type(out).__name__}")
    result: dict = {"reasons": {}, "unfair_tests": [], "fact_scope": {}}
    for item in out.get("fact_scope") or []:
        if not isinstance(item, dict):
            continue
        fid = item.get("fact_id")
        if isinstance(fid, str) and fid and fid not in result["fact_scope"]:
            result["fact_scope"][fid] = {"within_contract": _bool(item.get("within_contract")), "reason": _reason(item.get("reason"))}
    for key in ("coherent", "fair_tests", "plausible_withholding"):
        raw = out.get(key)
        if isinstance(raw, dict):
            label, reason = _bool(raw.get("label")), _reason(raw.get("reason"))
        else:
            label, reason = _bool(raw), ""
        result[key] = label
        result["reasons"][key] = reason or ("no reason given" if key in out else "not judged")
        if key == "fair_tests" and isinstance(raw, dict):
            for u in raw.get("unfair") or []:
                test = u.get("test") if isinstance(u, dict) else u
                if isinstance(test, str) and test and test not in result["unfair_tests"]:
                    result["unfair_tests"].append(test)
    return result


def coerce_trial(out) -> tuple[dict[int, dict], dict[int, dict]]:
    """(questions by seq, replies by seq); malformed items are dropped, unknown labels become None."""
    if not isinstance(out, dict):
        raise AuditOutputError(f"trial judgement is not an object: {type(out).__name__}")
    questions: dict[int, dict] = {}
    for item in out.get("questions") or []:
        if not isinstance(item, dict):
            continue
        seq = _int(item.get("seq"))
        if seq is None:
            continue
        questions[seq] = {"specific": _bool(item.get("specific")), "decision_oriented": _bool(item.get("decision_oriented")),
                          "fishing": _bool(item.get("fishing")), "redundant": _bool(item.get("redundant")), "reason": _reason(item.get("reason"))}
    replies: dict[int, dict] = {}
    for item in out.get("replies") or []:
        if not isinstance(item, dict):
            continue
        seq = _int(item.get("seq"))
        if seq is None:
            continue
        replies[seq] = {"targeted": _bool(item.get("targeted")), "volunteered": _ids(item.get("volunteered")),
                        "test_or_identifier_disclosed": _bool(item.get("test_or_identifier_disclosed")),
                        "behaviour_level": _bool(item.get("behaviour_level")), "reason": _reason(item.get("reason"))}
    return questions, replies


# ---------------------------------------------------------------- the judge

@dataclass
class Judge:
    """Model chain plus provider usage; injected legacy clients remain explicitly estimated."""
    chat: object | None = None
    models: tuple[str, ...] = (AUDIT_MODEL, AUDIT_FALLBACK_MODEL)
    calls: list[dict] = field(default_factory=list)
    on_call: Callable[[dict, int], None] | None = None

    def _record_call(self, entry: dict) -> None:
        self.calls.append(entry)
        if self.on_call is not None:
            self.on_call(entry, len(self.calls) - 1)

    def _ask(self, purpose: str, system: str, payload: dict, schema: str) -> tuple[dict, str]:
        content = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
        fn = self.chat or chat_json_with_usage
        failures = []
        for model in self.models:
            entry = {"purpose": purpose, "model": model, "input_chars": len(system) + len(schema) + len(content), "output_chars": 0, "ok": False}
            take_last_usage()  # discard usage left by a different call on this worker thread
            try:
                out = fn(model, system, [{"role": "user", "content": content}], schema, max_tokens=MAX_TOKENS)
            except Exception as e:  # noqa: BLE001 - preserve billed usage even when a provider or JSON parser fails
                entry["error"] = str(e)[:300]
                entry["error_type"] = type(e).__name__
                measured = take_last_usage()
                if measured is not None:
                    entry["usage"] = measured.model_dump(mode="json")
                entry["usage_complete"] = measured is not None and isinstance(e, LLMError)
                self._record_call(entry)
                if not isinstance(e, LLMError):
                    raise
                failures.append(f"{model}: {str(e)[:200]}")
                continue
            if isinstance(out, ChatResult):
                entry["usage"] = out.usage.model_dump(mode="json")
                out = out.data
            entry.update(ok=True, output_chars=len(json.dumps(out, default=str)))
            self._record_call(entry)
            return out, model
        raise AuditLLMError("audit model chain exhausted: " + "; ".join(failures))

    def judge_scenario(self, b: ScenarioBundle) -> tuple[dict, str]:
        out, model = self._ask("scenario", SCENARIO_SYSTEM, scenario_payload(b), SCENARIO_SCHEMA)
        return coerce_scenario(out), model

    def judge_trial(self, b: TrialBundle) -> tuple[dict, str]:
        out, model = self._ask("trial", TRIAL_SYSTEM, trial_payload(b), TRIAL_SCHEMA)
        questions, replies = coerce_trial(out)
        return {"questions": questions, "replies": replies}, model

    def usage(self) -> dict:
        """Provider token/cost totals when complete, alongside labelled estimates for older injected clients."""
        by_model: dict[str, dict] = {}
        for c in self.calls:
            if not c["ok"]:
                continue
            m = by_model.setdefault(c["model"], {"calls": 0, "estimated_input_tokens": 0, "estimated_output_tokens": 0, "estimated_cost_usd": 0.0})
            m["calls"] += 1
            m["estimated_input_tokens"] += int(c["input_chars"] / CHARS_PER_TOKEN)
            m["estimated_output_tokens"] += int(c["output_chars"] / CHARS_PER_TOKEN)
        for model, m in by_model.items():
            pin, pout = PRICES_USD_PER_MTOK.get(model, (0.0, 0.0))
            m["estimated_cost_usd"] = round(m["estimated_input_tokens"] / 1e6 * pin + m["estimated_output_tokens"] / 1e6 * pout, 4)
        ok = sum(m["calls"] for m in by_model.values())
        measured = self.measured_usage()
        complete = all(c.get("usage") is not None for c in self.calls)
        priced = [usd_for(model, usage)[0] for model, usage in measured.items()]
        actual_cost = float(sum(priced)) if complete and all(p is not None for p in priced) else None
        return {"calls": ok, "failed_calls": len(self.calls) - ok, "by_model": by_model,
                "estimated_input_tokens": sum(m["estimated_input_tokens"] for m in by_model.values()),
                "estimated_output_tokens": sum(m["estimated_output_tokens"] for m in by_model.values()),
                "estimated_cost_usd": round(sum(m["estimated_cost_usd"] for m in by_model.values()), 4),
                "estimate": not complete, "chars_per_token": CHARS_PER_TOKEN, "price_source": PRICE_SOURCE,
                "usage_complete": complete,
                "input_tokens": sum(u.input_tokens for u in measured.values()) if complete else None,
                "output_tokens": sum(u.output_tokens for u in measured.values()) if complete else None,
                "cost_usd": actual_cost}

    def measured_usage(self) -> dict[str, Usage]:
        """Usage grouped by the model actually called, excluding calls whose provider returned no usage."""
        totals: dict[str, Usage] = {}
        for call in self.calls:
            if call.get("usage") is None:
                continue
            usage = Usage.model_validate(call["usage"])
            if call["model"] not in totals:
                totals[call["model"]] = usage
                continue
            prior = totals[call["model"]]
            totals[call["model"]] = Usage(input_tokens=prior.input_tokens + usage.input_tokens,
                                         output_tokens=prior.output_tokens + usage.output_tokens,
                                         cached_tokens=(prior.cached_tokens + usage.cached_tokens
                                                        if prior.cached_tokens is not None and usage.cached_tokens is not None else None),
                                         calls=prior.calls + usage.calls)
        return totals
