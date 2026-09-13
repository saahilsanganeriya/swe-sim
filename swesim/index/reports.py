"""Report shapes computed over rows (pure functions), shared by the SQL and the file reader so both give the same
funnel, leaderboard, spend and evaluator blanking.

Contract
- `funnel_steps`: cumulative funnel. A candidate at stage S counts as `kept` at every earlier stage and by its
  `status` at S. Discovery-time rejects (PRs filtered before a candidate row exists) have no id and are not rows.
- `compute_leaderboard`: the gate of interfaces §9.1. A trial counts only when its scenario version is
  `validation_status == "valid"`, `difficulty_tier == "informative"` (or `saturated` when asked; `broken` never) and
  `task_sense.coherent and task_sense.fair_tests`; the trial is `completed` with an agent-attributable outcome
  (1-4, the pass@k denominator of `swesim.stages.score`) and the board's single `purpose`. Cells are
  model x condition x brief_profile.
- `blank_exchange`: the public view of an exchange (evaluator-only columns emptied).
"""
from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_EVEN, Decimal

from swesim.index import models as M

AGENT_OUTCOMES: frozenset[int] = frozenset({1, 2, 3, 4})
_Q6 = Decimal("0.000001")


def q6(value: Decimal) -> Decimal:
    return value.quantize(_Q6, rounding=ROUND_HALF_EVEN)


def funnel_steps(rows: Iterable[M.CandidateRow]) -> list[M.FunnelStep]:
    order = M.FUNNEL_ORDER
    position = {s: i for i, s in enumerate(order)}
    counts = {s: {"kept": 0, "rejected": 0, "error": 0, "pending": 0} for s in order}
    for r in rows:
        i = position.get(r.stage)
        if i is None:
            continue
        for s in order[:i]:
            counts[s]["kept"] += 1
        counts[r.stage][r.status] += 1
    return [M.FunnelStep(stage=s, **counts[s]) for s in order]


def gate_passes(s: M.ScenarioRow, include_saturated: bool = False) -> bool:
    if s.validation_status != "valid":
        return False
    if s.difficulty_tier != "informative" and not (include_saturated and s.difficulty_tier == "saturated"):
        return False
    task_sense = s.task_sense or {}
    return bool(task_sense.get("coherent")) and bool(task_sense.get("fair_tests"))


def scenario_for_trial(t: M.TrialRow, versions: list[M.ScenarioRow]) -> M.ScenarioRow | None:
    """The scenario version a trial ran against: by spec hash, else by version number, else the latest."""
    if not versions:
        return None
    for s in versions:
        if s.spec_hash and s.spec_hash == t.spec_hash:
            return s
    if t.version is not None:
        for s in versions:
            if s.version == t.version:
                return s
    return max(versions, key=lambda s: s.version)


def compute_leaderboard(trials: Iterable[M.TrialRow], scenarios: Iterable[M.ScenarioRow], f: M.LeaderboardFilter) -> list[M.LeaderboardRow]:
    by_sid: dict[str, list[M.ScenarioRow]] = {}
    for s in scenarios:
        by_sid.setdefault(s.scenario_id, []).append(s)
    cells: dict[tuple[str, str, str], dict] = {}
    for t in trials:
        if t.purpose != f.purpose or t.status != "completed" or t.outcome_code not in AGENT_OUTCOMES:
            continue
        if (f.condition and t.condition not in f.condition) or (f.brief_profile and t.brief_profile not in f.brief_profile):
            continue
        if (f.model and t.model not in f.model) or (f.provider and t.provider not in f.provider) or (f.scenario_id and t.scenario_id not in f.scenario_id):
            continue
        s = scenario_for_trial(t, by_sid.get(t.scenario_id, []))
        if s is None or not gate_passes(s, f.include_saturated) or (f.source and s.source not in f.source):
            continue
        cell = cells.setdefault((t.model, t.condition, t.brief_profile), {
            "provider": t.provider, "n": 0, "scenarios": set(), "success": 0, "reward": Decimal(0), "questions": 0, "confirmations": 0,
            "cost": Decimal(0), "cost_known": False})
        cell["n"] += 1
        cell["scenarios"].add(t.scenario_id)
        cell["success"] += 1 if t.success else 0
        cell["reward"] += t.reward if t.reward is not None else Decimal(0)
        cell["questions"] += t.questions_asked or 0
        cell["confirmations"] += t.confirmations or 0
        if t.cost_usd is not None:
            cell["cost"] += t.cost_usd
            cell["cost_known"] = True
    rows: list[M.LeaderboardRow] = []
    for (model, condition, brief_profile), c in cells.items():
        if c["n"] < f.min_n:
            continue
        cost = c["cost"] if c["cost_known"] else None
        rows.append(M.LeaderboardRow(
            model=model, provider=c["provider"], condition=condition, brief_profile=brief_profile, purpose=f.purpose, n=c["n"],
            scenarios=len(c["scenarios"]), success_count=c["success"], pass_rate=q6(Decimal(c["success"]) / Decimal(c["n"])),
            mean_reward=q6(c["reward"] / Decimal(c["n"])), questions_asked=c["questions"], confirmations=c["confirmations"],
            cost_usd=cost, cost_per_success=q6(cost / Decimal(c["success"])) if cost is not None and c["success"] else None))
    rows.sort(key=lambda r: (r.condition, r.brief_profile, -(r.pass_rate or Decimal(0)), r.model))
    return rows


def cost_totals(costs: Iterable[M.CostRow]) -> list[tuple[str, str, int, int, int, Decimal]]:
    """(provider, model, records, input_tokens, output_tokens, usd) per provider/model, largest spend first."""
    totals: dict[tuple[str, str], list] = {}
    for c in costs:
        t = totals.setdefault((c.provider, c.model), [0, 0, 0, Decimal(0)])
        t[0] += 1
        t[1] += c.input_tokens
        t[2] += c.output_tokens
        t[3] += c.usd
    out = [(p, m, t[0], t[1], t[2], t[3]) for (p, m), t in totals.items()]
    out.sort(key=lambda r: (-r[5], r[0], r[1]))
    return out


def spend_rows(totals: Iterable[tuple[str, str, int, int, int, Decimal]], cap: Decimal | None) -> list[M.SpendRow]:
    rows = []
    for provider, model, records, tokens_in, tokens_out, usd in totals:
        share = q6(usd / cap) if cap is not None and cap > 0 else None
        rows.append(M.SpendRow(provider=provider, model=model, records=records, input_tokens=tokens_in, output_tokens=tokens_out, usd=usd,
                               cap_usd=cap, share_of_cap=share))
    return rows


def blank_exchange(row: M.ExchangeRow) -> M.ExchangeRow:
    """The public view: evaluator-only columns emptied, everything else unchanged."""
    return row.model_copy(update={"disclosed_fact_ids": [], "redacted_terms": [], "regenerated": None, "audit": None})
