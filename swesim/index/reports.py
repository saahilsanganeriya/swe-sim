"""Report shapes computed over rows (pure functions), shared by the SQL and the file reader so both give the same
funnel, leaderboard, spend and evaluator blanking.

Contract
- `funnel` (alias `funnel_steps`): the creation funnel over candidate rows, one entity per candidate *version*: the row's
  current version plus each of its `earlier_versions`. A candidate mined by two source keys (a legacy file and a frozen
  plan, a per-instance probe file) is one entity, identified by its scenario id (`scenario_id_for(instance_id)`), and
  the furthest stage wins. An entity at stage S `entered` every stage up to S, was `kept` at every stage before S and is
  counted by its status at S; `kept` at S itself only at the last stage. Invariant: `entered[n+1] == kept[n]` and
  `entered == kept + rejected + error + pending + superseded` at every stage. Discovery-time rejects (PRs filtered
  before a candidate row exists) have no id and are not rows.
- `compute_leaderboard`: the gate of interfaces §9.1. A trial counts only when its scenario version is
  `validation_status == "valid"`, `difficulty_tier == "informative"` (or `saturated` when asked; `broken` never) and
  `task_sense.coherent and task_sense.fair_tests`; the trial is `completed` with an agent-attributable outcome
  (1-4, the pass@k denominator of `swesim.stages.score`) and the board's single `purpose`. Cells are
  model x condition x brief_profile.
- `blank_exchange`: the public view of an exchange (evaluator-only columns emptied).
"""
from __future__ import annotations

from collections.abc import Iterable
from collections import Counter
from decimal import ROUND_HALF_EVEN, Decimal

from swesim.index import models as M
from swesim.index.cohorts import cohort_of
from swesim.index.project import scenario_id_for

AGENT_OUTCOMES: frozenset[int] = frozenset({1, 2, 3, 4})
_Q6 = Decimal("0.000001")


def q6(value: Decimal) -> Decimal:
    return value.quantize(_Q6, rounding=ROUND_HALF_EVEN)


def reject_histogram(discoveries: Iterable[M.DiscoveryRow], candidates: Iterable[M.CandidateRow]) -> dict[str, int]:
    """Latest discovery counts per source plus one current reject gate per candidate identity.

    `discovery:` counts PRs filtered before candidate creation; `candidate:` counts later gate rejections, not reasons
    or intermediate stage files. Counts describe decisions within each source scan; distinct source keys may have
    scanned the same PR, so the combined histogram is not a global unique-PR count.
    """
    latest: dict[str, M.DiscoveryRow] = {}
    for row in discoveries:
        prior = latest.get(row.source_key)
        if prior is None or (row.discovered_at, row.discovery_id) > (prior.discovered_at, prior.discovery_id):
            latest[row.source_key] = row
    counts: Counter[str] = Counter()
    for row in latest.values():
        counts.update({f"discovery:{reason}": count for reason, count in row.rejects.items() if count})
    unique = {(row.source_key, row.instance_id): row for row in candidates}
    for row in unique.values():
        if row.status == "rejected" and row.gate:
            counts[f"candidate:{row.gate}"] += 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


STATUS_RANK: dict[str, int] = {"kept": 5, "superseded": 4, "pending": 3, "error": 2, "rejected": 1}
_POSITION: dict[str, int] = {s: i for i, s in enumerate(M.FUNNEL_ORDER)}
_LAST = len(M.FUNNEL_ORDER) - 1


def funnel_identity(row: M.CandidateRow) -> str:
    """The candidate's scenario id (the drafter's rule on its instance id), the same whichever source key mined it."""
    return row.scenario_id or scenario_id_for(row.instance_id) or row.instance_id


def funnel_entities(rows: Iterable[M.CandidateRow]) -> dict[tuple[str, int | None], tuple[int, str]]:
    """(identity, version) -> (stage index, status): every candidate version once. A stage outside the funnel order
    (`failed`) counts at the first stage; `kept` before the last stage never advanced and reads `pending`. Duplicates
    (two source keys, a row without a version beside one with) fold into the furthest stage, then the most advanced status."""
    found: dict[tuple[str, int | None], tuple[int, str]] = {}

    def add(identity: str, version: int | None, stage: str, status: str) -> None:
        i = _POSITION.get(stage, 0)
        if status == "kept" and i < _LAST or status not in STATUS_RANK:
            status = "pending"
        key = (identity, version)
        if key not in found or (i, STATUS_RANK[status]) > (found[key][0], STATUS_RANK[found[key][1]]):
            found[key] = (i, status)

    for r in rows:
        identity = funnel_identity(r)
        add(identity, r.version, r.stage, r.status)
        for ev in r.earlier_versions or []:
            if isinstance(ev, dict) and isinstance(ev.get("version"), int) and not isinstance(ev["version"], bool):
                add(identity, ev["version"], str(ev.get("stage")), str(ev.get("status")))
    # a version-less observation of an identity that has versioned ones is the same candidate, not another
    unknown = [key for key in found if key[1] is None and any(k[0] == key[0] and k[1] is not None for k in found)]
    for identity, _none in unknown:
        latest = max(v for (idn, v) in found if idn == identity and v is not None)
        stage, status = found.pop((identity, None))
        add(identity, latest, M.FUNNEL_ORDER[stage], status)
    return found


def _label(identity: str, version: int | None) -> str:
    return f"{identity}@v{version}" if version is not None else identity


def funnel(rows: Iterable[M.CandidateRow]) -> list[M.FunnelStep]:
    entities = funnel_entities(rows)
    steps: list[M.FunnelStep] = []
    for i, stage in enumerate(M.FUNNEL_ORDER):
        counts = {"kept": 0, "rejected": 0, "error": 0, "pending": 0, "superseded": 0}
        ids: list[str] = []
        entered = 0
        for (identity, version), (j, status) in entities.items():
            if j < i:
                continue
            entered += 1
            if j > i or status == "kept":
                counts["kept"] += 1
                ids.append(_label(identity, version))
            else:
                counts[status] += 1
        steps.append(M.FunnelStep(stage=stage, entered=entered, ids=sorted(ids), **counts))
    return steps


funnel_steps = funnel


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
    if t.spec_hash:
        return None
    if t.version is not None:
        for s in versions:
            if s.version == t.version:
                return s
    return max(versions, key=lambda s: s.version)


def compute_leaderboard(trials: Iterable[M.TrialRow], scenarios: Iterable[M.ScenarioRow], f: M.LeaderboardFilter) -> list[M.LeaderboardRow]:
    by_sid: dict[str, list[M.ScenarioRow]] = {}
    for s in scenarios:
        by_sid.setdefault(s.scenario_id, []).append(s)
    cells: dict[tuple[str, str, str, str], dict] = {}
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
        cohort, label = cohort_of(t)
        if f.evaluation_cohort and cohort != f.evaluation_cohort:
            continue
        cell = cells.setdefault((t.model, t.condition, t.brief_profile, cohort), {
            "cohort_label": label,
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
    for (model, condition, brief_profile, cohort), c in cells.items():
        if c["n"] < f.min_n:
            continue
        cost = c["cost"] if c["cost_known"] else None
        rows.append(M.LeaderboardRow(
            evaluation_cohort=cohort, cohort_label=c["cohort_label"], model=model, provider=c["provider"], condition=condition, brief_profile=brief_profile, purpose=f.purpose, n=c["n"],
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
        t[3] += c.usd if c.usd is not None else Decimal(0)  # an unknown price adds nothing to the ledger; the row stays visible
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
