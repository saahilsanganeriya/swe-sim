"""Frontier solver admission uses measured compatible baselines and a complete budget forecast."""
from decimal import Decimal

from swesim.models import RunConfig
from swesim.stages.frontier import BASELINE_MODELS, evaluate


def evidence(count=15):
    scenarios, records = {}, []
    for i in range(count):
        sid = f"scenario-{i}"
        scenarios[sid] = {"valid": True, "audit_ok": True, "spec_hash": sid, "compiled_hash": "build",
                          "profile": "withheld", "difficulty": "informative"}
        for model in BASELINE_MODELS:
            for condition in ("full", "hidden", "interactive"):
                rc = RunConfig(condition=condition, model=model, compiled_hash="build",
                               purpose="comparison",
                               brief_profile="natural" if condition == "full" else "withheld").model_dump(mode="json")
                for attempt in range(2):
                    success = int(condition == "full" or attempt == 0)
                    records.append({"trial_id": f"{sid}-{model}-{condition}-{attempt}", "scenario_id": sid,
                                    "spec_hash": sid, "compiled_hash": "build", "origin": "cloudrun", "status": "completed",
                                    "condition": condition, "brief_profile": rc["brief_profile"], "model": model, "run_config": rc,
                                    "reward": {"success": success, "outcome_code": 1 if success else 2},
                                    "input_tokens": 1000, "output_tokens": 100})
    return scenarios, records


def plan(**extra):
    return {"scenario_ids": ["scenario-0"], "models": ["openai/gpt-5.6-sol"], "conditions": ["full", "hidden", "interactive"], "k": 2, **extra}


def budget(**extra):
    return {"known_usd": Decimal("10"), "complete": True, "unpriced_rows": 0, "unknown_usage_attempts": 0, **extra}


def test_low_tiers_do_not_need_frontier_evidence():
    assert evaluate(plan(models=[BASELINE_MODELS[0]]), {}, [], budget(), None)["allowed"]


def test_frontier_requires_fifteen_complete_current_scenarios():
    scenarios, records = evidence(14)
    result = evaluate(plan(), scenarios, records, budget(), Decimal("1500"))
    assert not result["allowed"] and result["qualified_scenarios"] == 14
    scenarios, records = evidence()
    assert evaluate(plan(), scenarios, records, budget(), Decimal("1500"))["allowed"]


def test_missing_arm_and_mixed_protocol_do_not_supply_coverage():
    scenarios, records = evidence()
    for r in records:
        if r["scenario_id"] == "scenario-0" and r["condition"] == "interactive":
            r["run_config"]["stakeholder"]["prompt_version"] = "v2"
    assert not evaluate(plan(), scenarios, records, budget(), Decimal("1500"))["allowed"]


def test_old_origins_builds_and_infrastructure_results_do_not_count():
    for mutation in ({"origin": "vm-systemd"}, {"compiled_hash": "old"}, {"reward": {"outcome_code": 7, "success": 1}}):
        scenarios, records = evidence()
        for r in records:
            if r["scenario_id"] == "scenario-0": r.update(mutation)
        assert not evaluate(plan(), scenarios, records, budget(), Decimal("1500"))["allowed"]


def test_ordering_must_hold_on_a_majority():
    scenarios, records = evidence()
    for r in records:
        if r["condition"] == "hidden": r["reward"] = {"success": 1, "outcome_code": 1}
    result = evaluate(plan(), scenarios, records, budget(), Decimal("1500"))
    assert not result["allowed"] and result["ordered_scenarios"] == 0


def test_budget_is_required_complete_and_large_enough():
    scenarios, records = evidence()
    for accounting, cap in ((budget(), None), (budget(complete=False), Decimal("1500")), (budget(), Decimal("10.001"))):
        assert not evaluate(plan(), scenarios, records, accounting, cap)["allowed"]


def test_saturated_targets_are_excluded_by_default():
    scenarios, records = evidence()
    scenarios["scenario-0"]["difficulty"] = "saturated"
    result = evaluate(plan(), scenarios, records, budget(), Decimal("1500"))
    assert not result["allowed"] and result["scenario_ids"] == []
    assert evaluate(plan(include_saturated=True), scenarios, records, budget(), Decimal("1500"))["allowed"]


def test_tier_e_starts_with_one_attempt_and_unknown_price_never_becomes_zero():
    scenarios, records = evidence()
    assert not evaluate(plan(models=["openai/gpt-6-astra"]), scenarios, records, budget(), Decimal("1500"))["allowed"]
    assert evaluate(plan(models=["openai/gpt-6-astra"], k=1), scenarios, records, budget(), Decimal("1500"))["allowed"]
