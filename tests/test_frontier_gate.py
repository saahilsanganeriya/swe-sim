"""Frontier solver admission uses measured compatible baselines and a complete budget forecast."""
from decimal import Decimal

import pytest

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


def test_ordering_is_pooled_across_the_core_baselines_per_scenario():
    """§3.2 asks for the ordering to hold on a majority of *scenarios*; one noisy baseline at n=2 does not veto a scenario."""
    scenarios, records = evidence()
    for r in records:
        if r["model"] == BASELINE_MODELS[0]:  # this model alone reads hidden 2/2 and full 1/2 everywhere
            r["reward"] = {"success": 1, "outcome_code": 1} if r["condition"] == "hidden" else {"success": int(r["trial_id"].endswith("-0")), "outcome_code": 1 if r["trial_id"].endswith("-0") else 2}
    result = evaluate(plan(), scenarios, records, budget(), Decimal("1500"))
    assert result["allowed"] and result["ordered_scenarios"] == 15
    # pooled equality (every arm saturated) is not an ordering
    for r in records:
        r["reward"] = {"success": 1, "outcome_code": 1}
    assert evaluate(plan(), scenarios, records, budget(), Decimal("1500"))["ordered_scenarios"] == 0


def test_cap_in_force_is_the_lower_of_run_and_global_caps():
    from swesim.settings import Settings
    from swesim.stages.frontier import cap_in_force
    s = Settings.from_env({"SWESIM_SPEND_CAP_USD": "1500"})
    assert cap_in_force(s, {}) == Decimal("1500") and cap_in_force(s, {"spend_cap_usd": 200}) == Decimal("200")
    assert cap_in_force(s, {"spend_cap_usd": 5000}) == Decimal("1500") and cap_in_force(Settings.from_env({}), {}) is None
    assert cap_in_force(Settings.from_env({}), {"spend_cap_usd": "nan"}) is None


def test_preflight_without_evidence_refuses_and_records_a_verifiable_decision(tmp_path):
    from swesim.paths import Workspace
    from swesim.settings import Settings
    from swesim.stages import frontier as F
    ws = Workspace(tmp_path / "ws", tmp_path / "registry.json")
    request = {"scenario_ids": ["s1"], "models": ["openai/gpt-5.6-sol"], "conditions": ["hidden"], "k": 1}
    out = F.preflight(ws, Settings.from_env({"SWESIM_SPEND_CAP_USD": "100"}), request)
    assert out["allowed"] is False and out["identity"] == F.identity_of(F.request_of(request))
    assert out["evidence_path"] == str(F.decision_path(ws, out["identity"])) and "accounting" in out
    with pytest.raises(ValueError, match="frontier admission rejected"):
        F.verify_admission(ws, {**request, "scenario_id": "s1"}, {"evidence_path": out["evidence_path"]})
    # Tier A–C: no evidence, no file
    assert F.preflight(ws, Settings.from_env({}), {**request, "models": [BASELINE_MODELS[1]]})["allowed"] is True
    assert len(list(ws.layout.evidence_dir("sweep").glob("frontier-*.json"))) == 1
