"""Cost accounting: the verified price table with its sources, cost_of from tokens, solver usage from mini-swe-agent /
ATIF / Harbor artifacts (OpenRouter's per-generation cost preferred), and the atomic, idempotent per-trial costs.json."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from swesim import costs, roles
from swesim.paths import Workspace


# ------------------------------------------------------------------ prices

def test_price_table_covers_every_probed_id_with_a_dated_source_and_never_guesses():
    for m in roles.ALL_PROBE_IDS:
        p = costs.price_for(m)
        assert p is not None, m
        assert p.price_source in costs.PRICE_SOURCES and p.as_of == date(2026, 9, 13) and p.model == m and p.provider == m.split("/")[0]
        assert p.input_per_m >= 0 and p.output_per_m >= 0 and (p.cached_input_per_m is None or p.cached_input_per_m >= 0)
    assert costs.price_for("anthropic/claude-opus-5") == costs.Price(provider="anthropic", model="anthropic/claude-opus-5", input_per_m=Decimal("5"),
                                                                    output_per_m=Decimal("25"), cached_input_per_m=Decimal("0.50"),
                                                                    price_source="anthropic-pricing-page", as_of=date(2026, 9, 13))
    assert costs.price_for("openai/gpt-5.6-terra").input_per_m == Decimal("2") and costs.price_for("openai/gpt-5.5").output_per_m == Decimal("30")
    assert costs.price_for("openrouter/deepseek/deepseek-v4-flash").input_per_m == Decimal("0.04928")    # catalog per-token string x 1e6, exact
    assert costs.price_for("openrouter/deepseek/deepseek-chat-v3-0324").cached_input_per_m is None       # the catalog lists no cache-read price
    assert costs.price_for("anthropic/claude-unknown-9") is None and costs.price_for("mistral/x") is None
    assert costs.cost_of("anthropic/claude-unknown-9", 1000, 10) == (None, "unknown")
    assert all(src.startswith("https://") for src in costs.PRICE_SOURCES.values())


def test_cost_of_prices_uncached_input_cached_input_and_output_separately():
    usd, src = costs.cost_of("anthropic/claude-opus-5", 1_000_000, 100_000, cached_tokens=400_000)
    assert usd == Decimal("5.70") and src == "anthropic-pricing-page"          # 600k x $5 + 400k x $0.50 + 100k x $25
    usd2, _ = costs.cost_of("openrouter/deepseek/deepseek-chat-v3-0324", 1_000_000, 0, cached_tokens=500_000)
    assert usd2 == Decimal("0.25")                                               # no sourced cache-read price: cached tokens bill as input
    assert costs.cost_of("openai/gpt-5-mini", 0, 0) == (Decimal("0"), "openai-pricing-page")
    with pytest.raises(ValueError):
        costs.cost_of("openai/gpt-5-mini", 10, 0, cached_tokens=11)              # cached tokens are a subset of the input
    with pytest.raises(ValueError):
        costs.cost_of("openai/gpt-5-mini", -1, 0)


# ------------------------------------------------------------------ usage from the agent's artifacts

def mini_traj(costs_per_call=(0.0011, 0.0012), or_costs=None, model="anthropic/claude-haiku-4-5-20251001", served=None):
    """A mini-swe-agent 2.4.6 trajectory shaped like workspace/jobs/runs/*/*/agent/mini-swe-agent.trajectory.json. `served`
    names the upstream per call the way OpenRouter's top-level `provider` reaches the dumped litellm ModelResponse."""
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for i, c in enumerate(costs_per_call):
        usage = {"prompt_tokens": 1000 * (i + 1), "completion_tokens": 50, "total_tokens": 1000 * (i + 1) + 50,
                 "prompt_tokens_details": {"cached_tokens": 800 * i, "cache_creation_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 0}}
        if or_costs is not None:
            usage["cost"] = or_costs[i]
        response = {"id": "x", "model": model.split("/", 1)[1], "usage": usage}
        if served is not None and served[i] is not None:
            response["provider"] = served[i]
        msgs.append({"role": "assistant", "content": "a", "extra": {"actions": [], "cost": c, "timestamp": 1.0, "response": response}})
        msgs.append({"role": "tool", "content": "o", "extra": {"returncode": 0}})
    return {"info": {"model_stats": {"instance_cost": sum(costs_per_call), "api_calls": len(costs_per_call)},
                     "config": {"model": {"model_name": model}}, "exit_status": "Submitted", "submission": ""},
            "messages": msgs, "trajectory_format": "mini-swe-agent-1.1"}


def test_solver_usage_from_a_mini_swe_agent_trajectory_sums_assistant_usage_and_keeps_the_agents_own_estimate():
    u = costs.solver_usage_from_trajectory(mini_traj())
    assert (u.input_tokens, u.output_tokens, u.cached_tokens, u.calls) == (3000, 100, 800, 2)
    assert u.reported_cost_usd is None and u.reported_cost_source is None       # Anthropic reports no per-generation cost
    assert u.agent_reported_cost_usd == Decimal("0.0023")                        # mini-swe-agent's litellm estimate: informational only


def test_solver_usage_prefers_openrouters_per_generation_cost_only_when_every_call_carries_it():
    u = costs.solver_usage_from_trajectory(mini_traj(or_costs=(0.000012, 0.00002)))
    assert u.reported_cost_usd == Decimal("0.000032") and u.reported_cost_source == "openrouter-generation"
    partial = costs.solver_usage_from_trajectory(mini_traj(or_costs=(0.000012, None)))
    assert partial.reported_cost_usd is None and partial.reported_cost_source is None   # a sum over some calls would under-report
    free = costs.solver_usage_from_trajectory(mini_traj(or_costs=(0.0, 0.0)))
    assert free.reported_cost_usd == Decimal("0") and free.reported_cost_source == "openrouter-generation"   # a free model is a real zero


def test_solver_usage_records_every_upstream_openrouter_names_in_the_response():
    """OpenRouter's top-level `provider` survives litellm's ModelResponse dump (verified against litellm 1.100.1), so the
    native trajectory says which upstream served each call; Anthropic/OpenAI responses name none."""
    one = costs.solver_usage_from_trajectory(mini_traj(or_costs=(0.000012, 0.00002), served=("DeepInfra", "DeepInfra"), model="openrouter/deepseek/deepseek-v3.2"))
    assert one.served_providers == ("DeepInfra",)
    mixed = costs.solver_usage_from_trajectory(mini_traj(or_costs=(0.000012, 0.00002), served=("DeepInfra", "Fireworks")))
    assert mixed.served_providers == ("DeepInfra", "Fireworks")                 # every upstream that served a call, first appearance first
    partial = costs.solver_usage_from_trajectory(mini_traj(served=("DeepInfra", None)))
    assert partial.served_providers == ("DeepInfra",)                           # a call without the field adds nothing, and hides nothing
    assert costs.solver_usage_from_trajectory(mini_traj()).served_providers == ()
    odd = costs.solver_usage_from_trajectory(mini_traj(served=("Google (Vertex), EU", 7)))
    assert odd.served_providers == ("Google Vertex EU",)                        # names are made safe for the price-source detail; non-strings are ignored


def test_usage_addition_merges_served_providers_without_duplicates():
    a = costs.Usage(input_tokens=1, output_tokens=1, calls=1, served_providers=("DeepInfra",))
    b = costs.Usage(input_tokens=1, output_tokens=1, calls=1, served_providers=("Fireworks", "DeepInfra"))
    assert (a + b).served_providers == ("DeepInfra", "Fireworks") and (a + costs.Usage()).served_providers == ("DeepInfra",)
    assert costs.Usage.model_validate({"input_tokens": 1, "output_tokens": 1}).served_providers == ()   # stakeholder usage payloads predate the field
    assert "served_providers" not in costs.Usage(input_tokens=1).model_dump(mode="json")                  # and stay byte-identical when none is named
    dumped = a.model_dump(mode="json")
    assert dumped["served_providers"] == ["DeepInfra"] and costs.Usage.model_validate(dumped) == a


def test_served_provider_is_surfaced_in_the_price_source_detail_and_parses_back(tmp_path):
    ws = Workspace(tmp_path / "ws")
    usage = costs.solver_usage_from_trajectory(mini_traj(or_costs=(0.000012, 0.00002), served=("DeepInfra", "DeepInfra")))
    usd, source = costs.usd_for("openrouter/deepseek/deepseek-v3.2", usage)
    assert usd == Decimal("0.000032") and source == "openrouter-generation (served by DeepInfra)"
    assert costs.price_source_key(source) == "openrouter-generation" and costs.served_providers_of(source) == ("DeepInfra",)
    rec = costs.record_cost(ws, "a" * 24, None, "solver", "openrouter/deepseek/deepseek-v3.2", usage, idempotency_key="solver-final")
    assert rec.price_source == source and costs.price_source_key(rec.price_source) in costs.PRICE_SOURCES
    assert costs.read_costs(ws.root / "trials" / ("a" * 24) / "costs.json")[0].price_source == source          # the detail reaches the ledger
    # the catalog's default-route price is not necessarily the served upstream's price, so table-priced calls carry the detail too
    table = costs.Usage(input_tokens=1000, output_tokens=100, served_providers=("Fireworks", "DeepInfra"))
    _, src = costs.usd_for("openrouter/deepseek/deepseek-v3.2", table)
    assert src == "openrouter-models-api (served by Fireworks, DeepInfra)" and costs.served_providers_of(src) == ("Fireworks", "DeepInfra")
    # an unknown price stays the bare documented sentinel: knowing who served the call does not price it
    assert costs.usd_for("openrouter/vendor/unlisted-model", costs.Usage(input_tokens=1, output_tokens=1, served_providers=("X",))) == (None, "unknown")
    assert costs.price_source_key("unknown") == "unknown" and costs.served_providers_of("anthropic-pricing-page") == ()
    assert costs.price_source_key("anthropic-pricing-page") == "anthropic-pricing-page"


def test_solver_usage_reads_atif_final_metrics_and_harbor_agent_result():
    atif = {"schema_version": "ATIF-v1.6", "session_id": "s", "agent": {"name": "mini-swe-agent", "version": "2.4.6", "model_name": "openai/gpt-5.5"},
            "steps": [{"step_id": 1, "source": "agent", "metrics": {"prompt_tokens": 10, "completion_tokens": 1}}, {"step_id": 2, "source": "user"},
                      {"step_id": 3, "source": "agent", "metrics": {"prompt_tokens": 20, "completion_tokens": 2}}],
            "final_metrics": {"total_prompt_tokens": 30, "total_completion_tokens": 3, "total_cached_tokens": 12, "total_cost_usd": 0.5, "total_steps": 3}}
    u = costs.solver_usage_from_trajectory(atif)
    assert (u.input_tokens, u.output_tokens, u.cached_tokens, u.calls) == (30, 3, 12, 2)
    assert u.agent_reported_cost_usd == Decimal("0.5") and u.reported_cost_usd is None
    h = costs.solver_usage_from_trajectory({"n_input_tokens": 7, "n_cache_tokens": 5, "n_output_tokens": 2, "cost_usd": 0.01, "model_usage": {}})
    assert (h.input_tokens, h.output_tokens, h.cached_tokens) == (7, 2, 5) and h.agent_reported_cost_usd == Decimal("0.01")
    with pytest.raises(ValueError):
        costs.solver_usage_from_trajectory({"unexpected": 1})


def test_usage_from_trial_dir_prefers_the_native_trajectory_then_atif_then_harbor_result(tmp_path):
    d = tmp_path / "trial"
    (d / "agent").mkdir(parents=True)
    assert costs.usage_from_trial_dir(d) is None                                            # no evidence: unknown, never zero
    (d / "result.json").write_text(json.dumps({"agent_result": None, "exception_info": None}))
    assert costs.usage_from_trial_dir(d) is None
    (d / "result.json").write_text(json.dumps({"agent_result": {"n_input_tokens": 7, "n_cache_tokens": 0, "n_output_tokens": 2, "cost_usd": None}}))
    assert costs.usage_from_trial_dir(d).input_tokens == 7
    (d / "agent" / "trajectory.json").write_text(json.dumps({"final_metrics": {"total_prompt_tokens": 30, "total_completion_tokens": 3,
                                                                                "total_cached_tokens": 0, "total_cost_usd": None, "total_steps": 1}, "steps": []}))
    assert costs.usage_from_trial_dir(d).input_tokens == 30
    (d / "agent" / "mini-swe-agent.trajectory.json").write_text(json.dumps(mini_traj()))
    assert costs.usage_from_trial_dir(d).input_tokens == 3000
    (d / "agent" / "mini-swe-agent.trajectory.json").write_text("{not json")
    with pytest.raises(ValueError):
        costs.usage_from_trial_dir(d)                                                        # a corrupt artifact is an error, not a silent fallback


def test_usage_addition_sums_tokens_and_keeps_reported_costs_only_when_both_sides_have_them():
    a = costs.Usage(input_tokens=1, output_tokens=2, calls=1)
    b = costs.Usage(input_tokens=3, output_tokens=4, cached_tokens=2, calls=1, reported_cost_usd=Decimal("0.1"), reported_cost_source="openrouter-generation")
    s = a + b
    assert (s.input_tokens, s.output_tokens, s.cached_tokens, s.calls) == (4, 6, 2, 2) and s.reported_cost_usd is None
    t = b + b
    assert t.reported_cost_usd == Decimal("0.2") and t.reported_cost_source == "openrouter-generation" and t.cached_tokens == 4
    assert costs.Usage() + costs.Usage() == costs.Usage()


# ------------------------------------------------------------------ the per-trial record

def test_record_cost_appends_atomically_dedupes_and_prices_by_source(tmp_path):
    ws = Workspace(tmp_path / "ws")
    tid = "a" * 24
    u = costs.Usage(input_tokens=1_000_000, output_tokens=100_000, cached_tokens=400_000, calls=3)
    rec = costs.record_cost(ws, tid, None, "solver", "anthropic/claude-opus-5", u)
    assert rec.usd == Decimal("5.70") and rec.price_source == "anthropic-pricing-page" and rec.role == "solver" and rec.provider == "anthropic"
    assert rec.model == "anthropic/claude-opus-5" and rec.trial_id == tid and rec.workflow_id is None and len(rec.cost_id) == 26
    assert (rec.input_tokens, rec.output_tokens, rec.cached_tokens) == (1_000_000, 100_000, 400_000) and rec.at.tzinfo is not None
    p = ws.root / "trials" / tid / "costs.json"
    saved = json.loads(p.read_text())
    assert len(saved) == 1 and saved[0]["usd"] == "5.700000" and saved[0]["cost_id"] == rec.cost_id and saved[0]["at"] == rec.at.isoformat()
    again = costs.record_cost(ws, tid, None, "solver", "anthropic/claude-opus-5", u)                  # a retried activity is a no-op
    assert again == rec and len(json.loads(p.read_text())) == 1
    orr = costs.record_cost(ws, tid, "wf-1", "judge", "openrouter/deepseek/deepseek-v3.2",
                            costs.Usage(input_tokens=10, output_tokens=1, reported_cost_usd=Decimal("0.000032"), reported_cost_source="openrouter-generation"))
    assert orr.usd == Decimal("0.000032") and orr.price_source == "openrouter-generation" and orr.workflow_id == "wf-1"
    unknown = costs.record_cost(ws, tid, None, "solver", "anthropic/claude-unknown-9", costs.Usage(input_tokens=5, output_tokens=5))
    assert unknown.usd is None and unknown.price_source == "unknown"
    rows = costs.read_costs(p)
    assert [r.role for r in rows] == ["solver", "judge", "solver"] and rows[0] == rec and rows[2] == unknown
    assert costs.total_usd(rows) == (Decimal("5.700032"), 1)                                           # known total, count of unknowns
    with pytest.raises(ValueError):
        costs.record_cost(ws, tid, None, "gardener", "anthropic/claude-opus-5", u)                     # roles are the CostRow enum


def test_record_cost_without_a_trial_needs_a_workflow_and_writes_under_costs(tmp_path):
    ws = Workspace(tmp_path / "ws")
    with pytest.raises(ValueError):
        costs.record_cost(ws, None, None, "judge", "anthropic/claude-opus-5", costs.Usage())
    rec = costs.record_cost(ws, None, "calibration-x", "judge", "anthropic/claude-opus-5", costs.Usage(input_tokens=1, output_tokens=1))
    assert (ws.root / "costs" / "calibration-x.json").exists() and rec.trial_id is None and rec.workflow_id == "calibration-x"
    assert costs.read_costs(ws.root / "costs" / "calibration-x.json") == [rec]
    assert costs.read_costs(ws.root / "costs" / "absent.json") == []


def test_record_cost_with_the_storage_layout_writes_the_trial_costs_file(tmp_path):
    from swesim.storage.layout import Layout

    layout = Layout(root=tmp_path / "ws")
    tid = "c" * 24
    rec = costs.record_cost(layout, tid, None, "solver", "openai/gpt-5-mini", costs.Usage(input_tokens=1_000_000, output_tokens=0))
    assert costs.read_costs(layout.trial_file(tid, "costs.json")) == [rec]
    ws = Workspace(tmp_path / "ws2")                                                       # the adapter delegates to its Layout
    rec2 = costs.record_cost(ws, tid, None, "solver", "openai/gpt-5-mini", costs.Usage(input_tokens=1_000_000, output_tokens=0))
    assert costs.read_costs(ws.layout.trial_file(tid, "costs.json")) == [rec2]
    with pytest.raises(ValueError):
        costs.record_cost(layout, "not-a-trial-id", None, "solver", "openai/gpt-5-mini", costs.Usage())   # LayoutError is a ValueError


def test_record_cost_accepts_a_layout_exposing_trial_dir(tmp_path):
    class Layout:                                             # the storage stream's Layout: trial_dir(trial_id)
        root = tmp_path

        def trial_dir(self, trial_id):
            return tmp_path / "t" / trial_id

    rec = costs.record_cost(Layout(), "b" * 24, None, "solver", "openai/gpt-5-mini", costs.Usage(input_tokens=1_000_000, output_tokens=0))
    assert (tmp_path / "t" / ("b" * 24) / "costs.json").exists() and rec.usd == Decimal("0.25")


def test_cost_identity_distinguishes_paid_events_and_retries(tmp_path):
    ws = Workspace(tmp_path)
    usage = costs.Usage(input_tokens=1000, output_tokens=100, calls=1)
    first = costs.record_cost(ws, None, 'workflow', 'audit', 'openai/gpt-5.6-sol', usage, idempotency_key='attempt-1/call-1')
    again = costs.record_cost(ws, None, 'workflow', 'audit', 'openai/gpt-5.6-sol', usage, idempotency_key='attempt-1/call-1')
    second = costs.record_cost(ws, None, 'workflow', 'audit', 'openai/gpt-5.6-sol', usage, idempotency_key='attempt-2/call-1')
    assert first == again and first.cost_id != second.cost_id
    assert len(costs.read_costs(ws.root / 'costs' / 'workflow.json')) == 2
    with pytest.raises(ValueError, match='different usage'):
        costs.record_cost(ws, None, 'workflow', 'audit', 'openai/gpt-5.6-sol', costs.Usage(input_tokens=999, output_tokens=1), idempotency_key='attempt-1/call-1')


def test_missing_native_metrics_fall_back_without_inventing_zero(tmp_path):
    (tmp_path / 'agent').mkdir()
    native = mini_traj()
    native['messages'][2]['extra']['response'].pop('usage')
    (tmp_path / 'agent' / 'mini-swe-agent.trajectory.json').write_text(json.dumps(native))
    assert costs.usage_from_trial_dir(tmp_path) is None
    (tmp_path / 'result.json').write_text(json.dumps({'agent_result': {'n_input_tokens': 9000, 'n_output_tokens': 500}}))
    found = costs.usage_from_trial_dir(tmp_path)
    assert found.input_tokens == 9000 and found.output_tokens == 500
    with pytest.raises(costs.MissingUsage):
        costs.solver_usage_from_trajectory({'messages': [{'role': 'assistant', 'content': 'no usage'}]})


def test_stakeholder_costs_use_frozen_session_events_and_do_not_bill_duplicate_requests(tmp_path):
    import asyncio
    from swesim.stakeholder.session import Session
    from test_session import VIEW, cfg
    ws = Workspace(tmp_path); tid = 'c' * 24
    session = Session(tid, VIEW, cfg(start=3))
    usage = costs.Usage(input_tokens=1000, output_tokens=100, calls=1)
    def reply(*args): return {'reply': 'ValueError.', 'disclosed_fact_ids': ['f01'], 'usage': usage.model_dump(mode='json')}
    asyncio.run(session.ask('q1', 'Which exception?', reply))
    asyncio.run(session.ask('q1', 'Which exception?', reply))
    asyncio.run(session.ask('q2', 'Anything else?', reply))
    asyncio.run(session.freeze())
    transcript = session.snapshot()
    first = costs.record_stakeholder_usage(ws, tid, transcript, 'openai/gpt-5.6-sol')
    again = costs.record_stakeholder_usage(ws, tid, transcript, 'openai/gpt-5.6-sol')
    rows = costs.read_costs(ws.root / 'trials' / tid / 'costs.json')
    assert first == again and first['measured_events'] == 2 and first['usage_complete'] is True
    assert len(rows) == 2 and {row.role for row in rows} == {'stakeholder'}
    assert sum(row.usd for row in rows) == Decimal('0.012000')
    assert sum(row.input_tokens for row in rows) == 2000
