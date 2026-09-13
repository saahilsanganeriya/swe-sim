"""Capability gradient: the Trends page's measurement rows, computed from the leaderboard's admitted trials.

The generation axis is the solver tier of `swesim.roles.TIERS` (A older open, B newer open, C small closed, D mid
frontier, E frontier; models outside the table sort last as "other"). The expectation the page tests is that accuracy
is non-decreasing with tier: frontier models should score high and last-generation models should not match them.

Contract
- One evaluation cohort at a time (`swesim.index.cohorts.cohort_of`): origins, stakeholder versions, scaffolds and
  solver settings are never pooled. Conditions and brief profiles stay separate ("arms"); an arm is
  (condition, brief_profile). Purpose is fixed by the caller's query (never pooled).
- Every cell carries its n; a cell exists only where at least one admitted trial exists (nothing below n = 1).
  Accuracy is successes / n over agent-attributable completed trials, the leaderboard's definition.
- `gradient_state` labels a scenario's row of one arm: `saturated` (every model with n >= 2 is at 100%), `suspicious`
  (a tier A/B model's accuracy exceeds the best tier D/E model's by more than one trial's worth at the available n,
  i.e. by more than max(1/n_low, 1/n_high)), `holds` (>= 2 tiers with data and the best accuracy per tier is
  non-decreasing A->E), `not-monotone` (>= 2 tiers, a dip that is not the A/B-over-D/E kind), or None (one tier only).
  The labels are informational chips; they gate nothing.
- Deltas (full - hidden, interactive - hidden, full - interactive) are in percentage points and computed on the
  scenarios the two arms share for that model, so a delta never compares different task sets; each carries both n.
- Cost per solved task needs a known cost on every trial of the cell and at least one success; cost per trial is the
  mean over the trials with a known cost (coverage is reported). Integrity counts use `integrity_signal_count`
  (unknown stays unknown). Questions use `questions_asked`; the asked-before-implementing rate uses the audit label
  `asked_before_implementing` and exists only where labels exist.
- `frontier_gap` is the Home tile: best tier D/E accuracy minus best tier A accuracy on the same hidden scenarios of
  one cohort and brief profile (the group with the most shared scenarios), with n.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from urllib.parse import urlencode

from swesim.dashboard.render import TIER_ORDER, family_of, tier_of
from swesim.index.cohorts import cohort_of

CONDITIONS: tuple[str, ...] = ("full", "hidden", "interactive")
LOW_TIERS: frozenset[str] = frozenset({"A", "B"})
HIGH_TIERS: frozenset[str] = frozenset({"D", "E"})
GRADIENT_STATES: tuple[str, ...] = ("holds", "suspicious", "saturated", "not-monotone")
DELTAS: tuple[tuple[str, str, str], ...] = (("full_minus_hidden", "full", "hidden"), ("interactive_minus_hidden", "interactive", "hidden"),
                                            ("full_minus_interactive", "full", "interactive"))
EPS = 1e-9


# ----------------------------------------------------------------------------- ordering

def tier_rank(model: str | None) -> int:
    """A=0 … E=4; a model outside `roles.TIERS` sorts after every tier."""
    tier = tier_of(model)
    return TIER_ORDER.index(tier) if tier in TIER_ORDER else len(TIER_ORDER)


def model_order(model: str) -> tuple[int, int, str]:
    """Tier first (the generation axis), then the family's fixed categorical slot, then the id."""
    return tier_rank(model), family_of(model)[2] or 99, model


def sort_models(models) -> list[str]:
    return sorted(set(models), key=model_order)


def tier_label(model: str) -> str:
    return tier_of(model) or "other"


def arm_order(arm: tuple[str, str]) -> tuple[int, str]:
    """Arms in condition order (full, hidden, interactive), then brief profile."""
    condition, profile = arm
    return (CONDITIONS.index(condition) if condition in CONDITIONS else len(CONDITIONS)), profile


def _acc(successes: int, n: int) -> float | None:
    return round(successes / n, 6) if n else None


def _pp(a: float, b: float) -> float:
    return round((a - b) * 100, 1)


def evidence_url(query: dict, **params) -> str:
    """The leaderboard evidence list for one cell (the same admitted trials, the same filters)."""
    values = {k: v for k, v in {**query, **params}.items() if v not in (None, "", [])}
    return "/leaderboard/evidence?" + urlencode(values, doseq=True)


# ----------------------------------------------------------------------------- one cell

def _cell(trials: list) -> dict:
    n = len(trials)
    successes = sum(1 for t in trials if t.success)
    return {"n": n, "success_count": successes, "accuracy": _acc(successes, n), "scenarios": len({t.scenario_id for t in trials})}


# ----------------------------------------------------------------------------- (a) accuracy by model, tier-ordered, per arm

def accuracy_cells(trials: list, query: dict) -> list[dict]:
    groups: dict[tuple[str, str, str], list] = defaultdict(list)
    for t in trials:
        groups[(t.condition, t.brief_profile, t.model)].append(t)
    out = []
    for (condition, profile, model), rows in sorted(groups.items(), key=lambda kv: (arm_order(kv[0][:2]), model_order(kv[0][2]))):
        out.append({"condition": condition, "brief_profile": profile, "model": model, "tier": tier_label(model), "family": family_of(model)[1],
                    **_cell(rows), "evidence_url": evidence_url(query, model=model, condition=condition, brief_profile=profile)})
    return out


# ----------------------------------------------------------------------------- (b) per-scenario matrix with the gradient indicator

def gradient_state(cells: dict[str, tuple[int, int]]) -> tuple[str | None, str]:
    """``cells``: model -> (n, successes) for one scenario in one arm. Returns (state, reason); see the module contract."""
    present = {m: (n, s) for m, (n, s) in cells.items() if n >= 1}
    if not present:
        return None, "no admitted trials"
    with_n2 = {m: s / n for m, (n, s) in present.items() if n >= 2}
    if with_n2 and all(acc >= 1.0 - EPS for acc in with_n2.values()):
        return "saturated", f"every model with n ≥ 2 ({len(with_n2)}) is at 100%"
    highs = [(m, n, s) for m, (n, s) in present.items() if tier_of(m) in HIGH_TIERS]
    lows = [(m, n, s) for m, (n, s) in present.items() if tier_of(m) in LOW_TIERS]
    if highs and lows:
        best_m, best_n, best_s = max(highs, key=lambda x: (x[2] / x[1], x[1]))
        best_acc = best_s / best_n
        for m, n, s in sorted(lows, key=lambda x: -(x[2] / x[1])):
            margin = max(1 / n, 1 / best_n)
            if s / n - best_acc > margin + EPS:
                return "suspicious", (f"{m} (tier {tier_of(m)}) {s}/{n} beats the best tier D/E model {best_m} {best_s}/{best_n} "
                                      f"by {_pp(s / n, best_acc):+.0f} pp, more than one trial's worth ({margin * 100:.0f} pp) at these n")
    by_tier: dict[str, float] = {}
    for m, (n, s) in present.items():
        tier = tier_of(m)
        if tier in TIER_ORDER:
            by_tier[tier] = max(by_tier.get(tier, -1.0), s / n)
    tiers = [t for t in TIER_ORDER if t in by_tier]
    if len(tiers) < 2:
        return None, "one tier with data: no gradient to read"
    path = " → ".join(f"{t} {by_tier[t]:.2f}" for t in tiers)
    if all(by_tier[a] <= by_tier[b] + EPS for a, b in zip(tiers, tiers[1:])):
        return "holds", f"best accuracy per tier is non-decreasing: {path}"
    return "not-monotone", f"best accuracy per tier dips somewhere: {path}"


def matrices(trials: list, scenarios: dict) -> list[dict]:
    """One matrix per arm: rows scenarios, columns models in tier order, each cell (n, successes, accuracy), each row its
    gradient chip. ``scenarios``: scenario_id -> ScenarioRow (source, difficulty label)."""
    arms: dict[tuple[str, str], list] = defaultdict(list)
    for t in trials:
        arms[(t.condition, t.brief_profile)].append(t)
    out = []
    for (condition, profile), rows in sorted(arms.items(), key=lambda kv: arm_order(kv[0])):
        models = sort_models(t.model for t in rows)
        by_cell: dict[tuple[str, str], list] = defaultdict(list)
        for t in rows:
            by_cell[(t.scenario_id, t.model)].append(t)
        counts: dict[str, int] = {state: 0 for state in GRADIENT_STATES}
        scenario_rows = []
        for sid in sorted({t.scenario_id for t in rows}):
            cells = {m: _cell(by_cell[(sid, m)]) for m in models if by_cell.get((sid, m))}
            state, reason = gradient_state({m: (c["n"], c["success_count"]) for m, c in cells.items()})
            if state:
                counts[state] += 1
            s = scenarios.get(sid)
            scenario_rows.append({"scenario_id": sid, "source": getattr(s, "source", None), "difficulty_tier": getattr(s, "difficulty_tier", None),
                                  "gradient": {"state": state, "reason": reason}, "cells": cells})
        out.append({"condition": condition, "brief_profile": profile, "models": models,
                    "tiers": [tier_label(m) for m in models], "scenarios": scenario_rows, "flags": counts})
    return out


# ----------------------------------------------------------------------------- (c) the three deltas per model

def _shared_delta(a: list, b: list) -> dict | None:
    """Accuracy of arm ``a`` minus arm ``b`` on the scenarios both arms have trials for; None without a shared scenario."""
    shared = {t.scenario_id for t in a} & {t.scenario_id for t in b}
    if not shared:
        return None
    ca = _cell([t for t in a if t.scenario_id in shared])
    cb = _cell([t for t in b if t.scenario_id in shared])
    return {"pp": _pp(ca["accuracy"], cb["accuracy"]), "shared_scenarios": len(shared),
            "a": {"n": ca["n"], "success_count": ca["success_count"], "accuracy": ca["accuracy"]},
            "b": {"n": cb["n"], "success_count": cb["success_count"], "accuracy": cb["accuracy"]}}


def deltas(trials: list, query: dict) -> list[dict]:
    """Rows model × brief profile: the three arms' figures (all trials) and the three deltas on shared scenarios. Full
    uses the natural brief by contract, so it pairs with every hidden/interactive profile."""
    by_model: dict[str, list] = defaultdict(list)
    for t in trials:
        by_model[t.model].append(t)
    out = []
    for model in sort_models(by_model):
        rows = by_model[model]
        full = [t for t in rows if t.condition == "full"]
        profiles = sorted({t.brief_profile for t in rows if t.condition != "full"}) or (["natural"] if full else [])
        for profile in profiles:
            arms = {"full": full, "hidden": [t for t in rows if t.condition == "hidden" and t.brief_profile == profile],
                    "interactive": [t for t in rows if t.condition == "interactive" and t.brief_profile == profile]}
            figures = {name: ({**_cell(ts), "evidence_url": evidence_url(query, model=model, condition=name, brief_profile="natural" if name == "full" else profile)}
                              if ts else None) for name, ts in arms.items()}
            out.append({"model": model, "tier": tier_label(model), "family": family_of(model)[1], "brief_profile": profile, "arms": figures,
                        "deltas": {key: _shared_delta(arms[a], arms[b]) for key, a, b in DELTAS}})
    return out


# ----------------------------------------------------------------------------- (d) cost per solved task and per trial

def _cost_cell(rows: list) -> dict:
    known = [t.cost_usd for t in rows if t.cost_usd is not None]
    usd = sum(known, Decimal(0))
    c = _cell(rows)
    per_trial = float(usd / len(known)) if known else None
    per_success = float(usd / c["success_count"]) if known and len(known) == len(rows) and c["success_count"] else None
    reason = (None if per_success is not None else "no cost recorded" if not known else
              f"cost known on {len(known)}/{len(rows)} trials" if len(known) != len(rows) else "no solved task")
    return {**c, "cost_known_n": len(known), "cost_usd": float(usd) if known else None, "cost_per_trial": per_trial,
            "cost_per_success": per_success, "cost_per_success_reason": reason}


def cost_rows(trials: list, query: dict) -> dict:
    """``by_model`` pools the cohort's conditions (cost is a property of the trial); ``by_model_condition`` keeps them apart."""
    by_model: dict[str, list] = defaultdict(list)
    by_mc: dict[tuple[str, str], list] = defaultdict(list)
    for t in trials:
        by_model[t.model].append(t)
        by_mc[(t.model, t.condition)].append(t)
    models = sort_models(by_model)
    return {"by_model": [{"model": m, "tier": tier_label(m), "family": family_of(m)[1], **_cost_cell(by_model[m]), "evidence_url": evidence_url(query, model=m)} for m in models],
            "by_model_condition": [{"model": m, "tier": tier_label(m), "condition": c, **_cost_cell(by_mc[(m, c)]),
                                    "evidence_url": evidence_url(query, model=m, condition=c)}
                                   for m in models for c in CONDITIONS if (m, c) in by_mc]}


# ----------------------------------------------------------------------------- (e) integrity flags per model

def integrity_rows(trials: list, query: dict) -> list[dict]:
    by_model: dict[str, list] = defaultdict(list)
    for t in trials:
        by_model[t.model].append(t)
    out = []
    for model in sort_models(by_model):
        rows = by_model[model]
        known = [t for t in rows if t.integrity_signal_count is not None]
        flagged = [t for t in known if t.integrity_signal_count > 0]
        params = {k: v for k, v in query.items() if v not in (None, "", [])}
        out.append({"model": model, "tier": tier_label(model), "family": family_of(model)[1], "n": len(rows), "known_n": len(known),
                    "flagged_n": len(flagged), "signals": sum(t.integrity_signal_count for t in flagged),
                    "rate": _acc(len(flagged), len(known)), "flagged_trial_ids": sorted(t.trial_id for t in flagged),
                    "evidence_url": "/trends/integrity?" + urlencode({**params, "model": model}, doseq=True)})
    return out


# ----------------------------------------------------------------------------- (f) asking behaviour (interactive arms)

def asking_rows(trials: list, query: dict) -> list[dict]:
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for t in trials:
        if t.condition == "interactive":
            groups[(t.model, t.brief_profile)].append(t)
    out = []
    for (model, profile), rows in sorted(groups.items(), key=lambda kv: (model_order(kv[0][0]), kv[0][1])):
        questions = [t.questions_asked for t in rows if t.questions_asked is not None]
        asked_any = [q for q in questions if q > 0]
        labels = [t.asked_before_implementing for t in rows if t.asked_before_implementing is not None]
        out.append({"model": model, "tier": tier_label(model), "family": family_of(model)[1], "brief_profile": profile, "n": len(rows),
                    "questions_known_n": len(questions), "questions": sum(questions),
                    "questions_per_trial": round(sum(questions) / len(questions), 3) if questions else None,
                    "asked_any_rate": _acc(len(asked_any), len(questions)),
                    "asked_first_labelled_n": len(labels), "asked_first_n": sum(1 for x in labels if x),
                    "asked_first_rate": _acc(sum(1 for x in labels if x), len(labels)),
                    "evidence_url": evidence_url(query, model=model, condition="interactive", brief_profile=profile)})
    return out


# ----------------------------------------------------------------------------- the page payload

def summarize(trials: list, scenarios: list, query: dict) -> dict:
    """Per evaluation cohort (sorted by label): the six measurement blocks over the admitted trials of the current facet."""
    by_id = {s.scenario_id: s for s in scenarios}
    cohorts: dict[tuple[str, str], list] = defaultdict(list)
    for t in trials:
        cohorts[cohort_of(t)].append(t)
    out = {"trial_count": len(trials), "models": sort_models(t.model for t in trials), "scenario_count": len({t.scenario_id for t in trials}),
           "query": query, "cohorts": []}
    for (key, label), rows in sorted(cohorts.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        q = {**query, "evaluation_cohort": key}
        out["cohorts"].append({"evaluation_cohort": key, "cohort_label": label, "trial_count": len(rows), "models": sort_models(t.model for t in rows),
                               "accuracy": accuracy_cells(rows, q), "matrices": matrices(rows, by_id), "deltas": deltas(rows, q),
                               "cost": cost_rows(rows, q), "integrity": integrity_rows(rows, q), "asking": asking_rows(rows, q)})
    return out


# ----------------------------------------------------------------------------- the Home tile

def frontier_gap(trials: list) -> dict | None:
    """Best tier D/E accuracy minus best tier A accuracy on the same hidden scenarios, within one cohort and brief profile
    (the group with the most shared scenarios, then the most trials). None until both tiers have hidden trials on a
    shared scenario. Percentage points; n is the trials behind the two best models."""
    groups: dict[tuple[str, str, str], list] = defaultdict(list)
    for t in trials:
        if t.condition == "hidden":
            key, label = cohort_of(t)
            groups[(key, label, t.brief_profile)].append(t)
    best: dict | None = None
    for (key, label, profile), rows in groups.items():
        highs = {t.scenario_id for t in rows if tier_of(t.model) in HIGH_TIERS}
        lows = {t.scenario_id for t in rows if tier_of(t.model) == "A"}
        shared = highs & lows
        if not shared:
            continue
        on_shared = [t for t in rows if t.scenario_id in shared]
        per_model: dict[str, list] = defaultdict(list)
        for t in on_shared:
            per_model[t.model].append(t)

        def top(tiers: frozenset[str]) -> dict | None:
            cells = [{"model": m, "tier": tier_of(m), **_cell(ts)} for m, ts in per_model.items() if tier_of(m) in tiers]
            return max(cells, key=lambda c: (c["accuracy"], c["n"])) if cells else None

        high, low = top(HIGH_TIERS), top(frozenset({"A"}))
        if high is None or low is None:
            continue
        candidate = {"gap_pp": _pp(high["accuracy"], low["accuracy"]), "best_high": high, "best_low": low, "n": high["n"] + low["n"],
                     "scenarios": len(shared), "brief_profile": profile, "evaluation_cohort": key, "cohort_label": label}
        if best is None or (candidate["scenarios"], candidate["n"]) > (best["scenarios"], best["n"]):
            best = candidate
    return best
