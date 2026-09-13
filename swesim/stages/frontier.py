"""Measured admission policy for Tier D/E solvers (hosted brief §3.2, §5, §6).

Admission requires fifteen current, audited scenarios with two outcomes per arm
for each core cheap baseline, a strict full-over-hidden ordering on a majority,
and a complete spend forecast below the configured cap. Saturated targets are
excluded unless requested. Pipeline judge/draft/stakeholder roles are unaffected.
"""
from __future__ import annotations

import json
import hashlib
from collections import defaultdict
from decimal import Decimal

from swesim.costs import cost_of
from swesim.roles import TIERS
from swesim.paths import Workspace, atomic_json
from swesim.settings import Settings

BASELINE_MODELS = (
    "openrouter/deepseek/deepseek-chat-v3-0324", "openrouter/deepseek/deepseek-v3.2",
    "openrouter/deepseek/deepseek-v4-flash", "openrouter/z-ai/glm-4.5",
    "openrouter/z-ai/glm-5.3", "openai/gpt-5-mini",
)
MIN_SCENARIOS = 15
MIN_PER_ARM = 2
ARMS = ("full", "hidden", "interactive")


def tier_of(model: str) -> str | None:
    """Recognize the configured models even when a model is exposed through another provider prefix."""
    name = model.rsplit("/", 1)[-1]
    return next((tier for tier, models in TIERS.items() if any(m.rsplit("/", 1)[-1] == name for m in models)), None)


def frontier_requested(params: dict) -> bool:
    return any(tier_of(model) in {"D", "E"} for model in params.get("models", []))


def _usable(record: dict, scenario: dict) -> bool:
    return (scenario.get("valid") is True and scenario.get("audit_ok") is True
            and scenario.get("difficulty") in {"informative", "saturated"}
            and record.get("status") == "completed" and record.get("origin") == "cloudrun"
            and record.get("spec_hash") == scenario.get("spec_hash")
            and record.get("compiled_hash") == scenario.get("compiled_hash")
            and (record.get("reward") or {}).get("outcome_code") in {1, 2, 3, 4}
            and isinstance(record.get("run_config"), dict))


def _success(rows: list[dict]) -> float:
    return sum((r.get("reward") or {}).get("outcome_code") == 1 and
               (r.get("reward") or {}).get("success") == 1 for r in rows) / len(rows)


def _coverage(scenario: dict, records: list[dict]) -> tuple[bool, bool]:
    """Never combine different protocols or source builds to create the minimum sample count."""
    groups = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for record in records:
        if not _usable(record, scenario) or record.get("model") not in BASELINE_MODELS:
            continue
        arm = record.get("condition")
        if arm not in ARMS or record.get("brief_profile") != ("natural" if arm == "full" else scenario.get("profile", "natural")):
            continue
        rc = record["run_config"]
        if rc.get("purpose") != "comparison":
            continue
        protocol = {k: v for k, v in rc.items() if k not in {"condition", "brief_profile", "agent_config", "k"}}
        protocol["origin"] = record["origin"]
        key = (record["model"], json.dumps(protocol, sort_keys=True, separators=(",", ":")))
        groups[key][arm][rc.get("agent_config", "")].append(record)
    selected: dict[str, tuple[str, bool]] = {}
    for (model, protocol), arms in groups.items():
        # The interactive scaffold is a condition treatment, but repetitions within each arm must share it.
        interactive = [rows for rows in arms["interactive"].values() if len(rows) >= MIN_PER_ARM]
        if not interactive:
            continue
        for scaffold in arms["full"].keys() & arms["hidden"].keys():
            full, hidden = arms["full"][scaffold], arms["hidden"][scaffold]
            if min(len(full), len(hidden)) < MIN_PER_ARM:
                continue
            latest = max(str(r.get("finished_at") or "") for r in full + hidden)
            candidate = (latest + protocol, _success(full) > _success(hidden))
            if model not in selected or candidate[0] > selected[model][0]:
                selected[model] = candidate
    covered = set(BASELINE_MODELS) <= selected.keys()
    return covered, covered and all(selected[m][1] for m in BASELINE_MODELS)


def evaluate(params: dict, scenarios: dict[str, dict], records: list[dict], accounting: dict,
             cap: Decimal | None) -> dict:
    """A reviewable decision from frozen evidence; never launches a trial or substitutes a model."""
    targets = list(dict.fromkeys(params.get("scenario_ids") or []))
    result = {"allowed": True, "reasons": [], "scenario_ids": targets, "excluded_scenarios": {},
              "qualified_scenarios": 0, "ordered_scenarios": 0, "forecast_usd": None}
    if not frontier_requested(params):
        return result
    by_scenario = defaultdict(list)
    for record in records:
        by_scenario[record.get("scenario_id")].append(record)
    for sid, scenario in scenarios.items():
        covered, ordered = _coverage(scenario, by_scenario[sid])
        result["qualified_scenarios"] += int(covered)
        result["ordered_scenarios"] += int(ordered)
    reasons = result["reasons"]
    if result["qualified_scenarios"] < MIN_SCENARIOS:
        reasons.append("Requires 15 current audited scenarios with at least two outcomes per arm on every core Tier A–C baseline.")
    if result["ordered_scenarios"] * 2 <= result["qualified_scenarios"]:
        reasons.append("Full must exceed hidden on a majority of the qualified scenarios.")
    selected = []
    for sid in targets:
        scenario = scenarios.get(sid, {})
        if not scenario.get("valid") or not scenario.get("audit_ok") or scenario.get("difficulty") not in {"informative", "saturated"}:
            result["excluded_scenarios"][sid] = "Requires valid controls, task-sense audit and a completed difficulty screen."
        elif scenario.get("difficulty") == "saturated" and not params.get("include_saturated", False):
            result["excluded_scenarios"][sid] = "Saturated scenarios are excluded by default."
        else:
            selected.append(sid)
    result["scenario_ids"] = selected
    if not selected:
        reasons.append("No eligible target scenarios remain.")
    k = int(params.get("k", 3))
    if k < 1:
        reasons.append("Attempt count must be positive.")
    for model in params.get("models", []):
        prior = any(r.get("model") == model and (r.get("run_config") or {}).get("k") == 1
                    and _usable(r, scenarios.get(r.get("scenario_id"), {})) for r in records)
        if tier_of(model) == "E" and k > 1 and not prior:
            reasons.append(f"{model} requires a completed k=1 trial before increasing repeats.")
    if cap is None or cap <= 0:
        reasons.append("A positive spend cap is required for frontier runs.")
    if not accounting.get("complete") or accounting.get("unpriced_rows") or accounting.get("unknown_usage_attempts"):
        reasons.append("Spend accounting is incomplete; unknown usage cannot be treated as zero.")
    samples = [r for r in records if r.get("model") in BASELINE_MODELS
               and _usable(r, scenarios.get(r.get("scenario_id"), {}))
               and isinstance(r.get("input_tokens"), int) and isinstance(r.get("output_tokens"), int)
               and r["input_tokens"] > 0 and r["output_tokens"] >= 0]
    if not samples:
        reasons.append("A forecast requires measured baseline token usage.")
    else:
        prompt, completion = max(r["input_tokens"] for r in samples), max(r["output_tokens"] for r in samples)
        forecast = Decimal(0)
        for model in params.get("models", []):
            price, _ = cost_of(model, prompt, completion)
            if price is None:
                reasons.append(f"No verified price for {model}.")
            else:
                forecast += price * len(selected) * len(params.get("conditions") or ARMS) * max(k, 0)
        forecast *= Decimal("1.25")  # explicit allowance above the largest observed solver token counts
        result["forecast_usd"] = float(forecast)
        result["forecast_basis"] = {"input_tokens_per_trial": prompt, "output_tokens_per_trial": completion, "allowance": 1.25}
        if cap is not None and Decimal(str(accounting.get("known_usd", 0))) + forecast > cap:
            reasons.append("Projected spend exceeds the remaining cap.")
    result["allowed"] = not reasons
    return result


def preflight(ws: Workspace, settings: Settings, params: dict) -> dict:
    """Load authoritative evidence, persist the admission decision, and return only bounded summary data."""
    if not frontier_requested(params):
        return {"allowed": True, "scenario_ids": params.get("scenario_ids") or [params.get("scenario_id")], "reasons": []}
    from swesim import costs
    from swesim.models import ScenarioSpec
    from swesim.stages.score import load_trial_records
    request = {**params, "scenario_ids": params.get("scenario_ids") or [params.get("scenario_id")]}
    registry = json.loads(ws.registry_path.read_text())
    scenarios = {}
    for sid, entry in registry.get("scenarios", {}).items():
        directory = ws.scenario_dir(sid)
        try:
            spec = ScenarioSpec.model_validate_json((directory / "scenario.json").read_text())
            validation = json.loads((directory / "validation.json").read_text())
            compiled = json.loads((directory / "compile.json").read_text())
            audit = json.loads((directory / "audit.json").read_text())
            difficulty = json.loads((directory / "difficulty.json").read_text())
        except (OSError, ValueError):
            continue  # Incomplete evidence cannot qualify a scenario.
        scenarios[sid] = {"valid": (entry.get("validation_status") == validation.get("status") == "valid"
                                    and entry.get("spec_hash") == validation.get("spec_hash") == spec.spec_hash
                                    and compiled.get("compiled_hash") == validation.get("compiled_hash") == entry.get("compiled_hash")),
                          "spec_hash": spec.spec_hash, "compiled_hash": compiled.get("compiled_hash"),
                          "audit_ok": audit.get("spec_hash") == spec.spec_hash and audit.get("coherent") is True and audit.get("fair_tests") is True,
                          "profile": "withheld" if any(b.profile == "withheld" for b in spec.briefs) else "natural",
                          "difficulty": difficulty.get("tier") if difficulty.get("spec_hash") == spec.spec_hash
                          and difficulty.get("compiled_hash") == compiled.get("compiled_hash") else None}
    result = evaluate(request, scenarios, load_trial_records(ws), costs.accounting_summary(ws.layout), settings.spend_cap_usd)
    identity = hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()[:24]
    path = ws.layout.evidence_dir("sweep") / f"frontier-{identity}.json"
    atomic_json(path, {"request": request, "decision": result, "baseline_models": list(BASELINE_MODELS)})
    return {**result, "evidence_path": str(path)}
