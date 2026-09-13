"""Historical measurement rows derived only from the leaderboard's admitted IndexReader trials.

One purpose per request; conditions and brief profiles stay separate. Points are
cumulative through the UTC completion day. Unknown questions/confirmations remain
unknown, incomplete cost coverage produces a null cost-per-success, and undated
trials are reported separately. Each point links to the exact supporting trial view.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC
from decimal import Decimal
from urllib.parse import urlencode


def _rate(numerator, denominator: int) -> float | None:
    return round(float(numerator) / denominator, 6) if denominator else None


def evidence_url(query: dict, model: str, condition: str, profile: str, day: str, cohort: str | None = None) -> str:
    values = {**query, "model": model, "condition": condition, "brief_profile": profile, "through": day}
    if cohort:
        values["cohort"] = cohort
    return "/trends/evidence?" + urlencode(values, doseq=True)


def summarize(trials: list, scenarios: list, query: dict) -> dict:
    """Cumulative daily rows, interaction cohorts and current valid-scenario difficulty counts."""
    groups = defaultdict(list)
    for t in trials:
        if t.finished_at is not None:
            groups[(t.model, t.condition, t.brief_profile)].append(t)
    history, interaction = [], []
    for (model, condition, profile), rows in sorted(groups.items()):
        days = defaultdict(list)
        for t in sorted(rows, key=lambda t: (t.finished_at, t.trial_id)):
            days[t.finished_at.astimezone(UTC).date().isoformat()].append(t)
        accumulated = []
        for day, finished in sorted(days.items()):
            accumulated.extend(finished)
            successes = sum(bool(t.success) for t in accumulated)
            successes_known = sum(t.success is not None for t in accumulated)
            questions = [t.questions_asked for t in accumulated if t.questions_asked is not None]
            confirmations = [t.confirmations for t in accumulated if t.confirmations is not None]
            costs = [t.cost_usd for t in accumulated if t.cost_usd is not None]
            usd = sum(costs, Decimal(0))
            shared = {"model": model, "condition": condition, "brief_profile": profile, "day": day}
            history.append({**shared, "n": len(accumulated), "success_count": successes, "success_known_n": successes_known,
                            "scenarios": len({t.scenario_id for t in accumulated}), "configurations": len({t.run_config_hash for t in accumulated}),
                            "pass_rate": _rate(successes, len(accumulated)) if successes_known == len(accumulated) else None, "questions_per_trial": _rate(sum(questions), len(questions)),
                            "questions_n": len(questions), "confirmations_per_trial": _rate(sum(confirmations), len(confirmations)),
                            "confirmations_n": len(confirmations), "cost_usd": str(usd), "cost_known_n": len(costs),
                            "cost_per_success": _rate(usd, successes) if len(costs) == len(accumulated) == successes_known else None,
                            "evidence_url": evidence_url(query, model, condition, profile, day)})
            for cohort, asked in (("asked", True), ("did-not-ask", False)):
                selected = [t for t in accumulated if t.questions_asked is not None and (t.questions_asked > 0) == asked]
                if selected:
                    wins = sum(bool(t.success) for t in selected)
                    interaction.append({**shared, "cohort": cohort, "n": len(selected), "success_count": wins,
                                        "scenarios": len({t.scenario_id for t in selected}), "pass_rate": _rate(wins, len(selected)) if all(t.success is not None for t in selected) else None,
                                        "evidence_url": evidence_url(query, model, condition, profile, day, cohort)})
    difficulty = Counter(s.difficulty_tier or "unlabelled" for s in scenarios if s.validation_status == "valid")
    distribution = []
    for label in ("informative", "saturated", "broken", "unlabelled"):
        params = {"label": label}
        if query.get("source"):
            params["source"] = query["source"]
        distribution.append({"label": label, "n": difficulty[label], "evidence_url": "/trends/difficulty?" + urlencode(params, doseq=True)})
    return {"history": history, "interaction": interaction, "difficulty": distribution, "trial_count": len(trials),
            "undated_trials": sum(t.finished_at is None for t in trials),
            "unknown_questions": sum(t.questions_asked is None for t in trials), "query": query}
