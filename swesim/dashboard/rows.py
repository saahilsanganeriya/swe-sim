"""Row models and filters the console codes against (interfaces spec §2), resolved from ``swesim.index`` when the
storage stream's package is present, else defined here verbatim from the spec so the console runs and tests stay offline.

Contract
- Field names of the row models are copied from ``docs/superpowers/specs/2026-09-13-interfaces.md`` §2; nothing is
  invented. Provenance columns (``created_at, updated_at, workflow_id, run_id, worker_image_digest, origin``) are on
  every row.
- ``Page[T]`` is ``Strict(items, total, offset, limit)``. Filters are Strict models with optional equality lists, date
  ranges, free-text ``q``, ``order_by``, ``offset`` and ``limit <= 500``. The spec names the filter classes but not their
  fields; the field names below are the console's choice and are listed in the stream report for the integrator to
  reconcile with ``swesim/index/models.py``.
- ``LeaderboardRow``, ``SpendRow`` and ``FunnelStep`` follow the spec's one-line descriptions.
- ``SHIM`` is True when the local definitions are in use (tests assert the two never disagree once storage is merged).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

try:  # the storage stream's package (interfaces §2); the console prefers it whenever it is importable
    from swesim.index import models as _m  # type: ignore
    SHIM = False
except ImportError:  # pragma: no cover - exercised until the storage stream is merged
    _m = None
    SHIM = True

Condition = Literal["full", "hidden", "interactive"]
Launcher = Literal["systemd", "cloudrun", "subprocess"]
Origin = Literal["laptop", "vm-systemd", "cloudrun"]
T = TypeVar("T")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Provenance(Strict):
    created_at: datetime
    updated_at: datetime
    workflow_id: str | None = None
    run_id: str | None = None
    worker_image_digest: str | None = None
    origin: str = "laptop"


if _m is not None:
    ScenarioRow = _m.ScenarioRow
    CandidateRow = _m.CandidateRow
    TrialRow = _m.TrialRow
    ExchangeRow = _m.ExchangeRow
    ScoreRow = _m.ScoreRow
    WorkflowRow = _m.WorkflowRow
    CostRow = _m.CostRow
    OperatorActionRow = _m.OperatorActionRow
    IndexRebuildRow = _m.IndexRebuildRow
else:
    class ScenarioRow(_Provenance):  # table scenarios, pk (scenario_id, version)
        scenario_id: str
        version: int
        spec_hash: str | None
        source: str
        source_ref: str
        validation_status: Literal["drafting", "approved", "compiling", "validating", "valid", "invalid", "rejected", "archived"]
        compiled_hash: str | None
        image_digest: str | None
        difficulty_tier: Literal["saturated", "informative", "broken"] | None
        difficulty_screen: dict | None
        task_sense: dict | None
        registry_json: dict | None
        archived_at: datetime | None

    class CandidateRow(_Provenance):  # table candidates, pk (instance_id, source_key)
        instance_id: str
        source_key: str
        source: str
        repo: str | None
        pr_number: int | None
        pr_kind: str | None
        stage: Literal["mined", "judged", "probed", "drafted", "approved", "compiled", "validated", "registered", "rejected", "failed"]
        status: Literal["kept", "rejected", "error", "pending"]
        gate: str | None
        reasons: list[str]
        judged_at: datetime | None
        scenario_id: str | None
        version: int | None

    class TrialRow(_Provenance):  # table trials, pk trial_id
        trial_id: str
        scenario_id: str
        version: int | None
        spec_hash: str
        condition: Condition
        brief_profile: Literal["natural", "withheld"]
        model: str
        provider: str
        run_config_hash: str
        attempt_index: int
        purpose: Literal["calibration", "comparison", "demonstration", "screen"]
        status: Literal["planned", "launched", "running", "completed", "runner-error", "launch-failed", "unit-failed", "unresolved", "cancelled"]
        outcome_code: int | None
        reward: Decimal | None
        success: bool | None
        integrity_signal_count: int | None
        questions_asked: int | None
        confirmations: int | None
        asked_before_implementing: bool | None
        started_at: datetime | None
        finished_at: datetime | None
        launcher: Launcher | None
        job_name: str | None
        cost_usd: Decimal | None
        input_tokens: int | None
        output_tokens: int | None
        stakeholder_model: str | None
        stakeholder_prompt_version: str | None

    class ExchangeRow(_Provenance):  # table exchanges, pk (trial_id, seq); evaluator-only: disclosed_fact_ids, redacted_terms, regenerated, audit
        trial_id: str
        seq: int
        role: Literal["agent", "stakeholder", "system"]
        exchange: Literal["ask", "confirm", "review", "system"]
        ts: datetime | None
        request_id: str | None
        patience_after: int | None
        text_public: str
        disclosed_fact_ids: list[str]
        regenerated: bool | None
        redacted_terms: list[str]
        audit: dict | None

    class ScoreRow(_Provenance):  # table scores
        scenario_id: str
        spec_hash: str
        condition: Condition
        brief_profile: str
        model: str
        run_config_hash: str
        n: int
        success_count: int
        pass_at_k: Decimal | None
        pass_pow_k: Decimal | None
        mean_reward: Decimal | None
        questions_asked: int
        confirmations: int
        computed_at: datetime

    class WorkflowRow(_Provenance):  # table workflows, pk (workflow_id, run_id)
        workflow_id: str
        run_id: str
        type: str
        parent_workflow_id: str | None
        status: Literal["running", "completed", "failed", "cancelled", "terminated", "timed_out", "continued_as_new"]
        params_summary: dict
        failure: str | None
        started_at: datetime
        closed_at: datetime | None
        task_queue: str | None
        cost_usd: Decimal | None
        search_attributes: dict

    class CostRow(_Provenance):  # table costs, pk cost_id (ulid)
        cost_id: str
        trial_id: str | None
        workflow_id: str | None
        role: Literal["solver", "judge", "drafter", "stakeholder", "withholder", "audit", "probe", "validation"]
        provider: str
        model: str
        input_tokens: int
        output_tokens: int
        cached_tokens: int | None
        usd: Decimal
        price_source: str
        at: datetime

    class OperatorActionRow(_Provenance):  # table operator_actions, pk action_id (ulid)
        action_id: str
        actor: str
        action: str
        target: str | None
        params: dict
        confirmed: bool
        origin_header: str | None
        result: Literal["accepted", "rejected", "failed"]
        workflow_id: str | None
        at: datetime

    class IndexRebuildRow(_Provenance):  # table index_rebuilds, pk rebuild_id
        rebuild_id: str
        started_at: datetime
        finished_at: datetime | None
        counts: dict
        diff: dict | None


# ----------------------------------------------------------------------------- page, filters, aggregates

def _shared(name: str, fallback):
    """Prefer the storage stream's class of the same name (reads.py or models.py) when it exists."""
    for mod in ("swesim.index.reads", "swesim.index.models"):
        try:
            module = __import__(mod, fromlist=[name])
        except ImportError:
            continue
        cls = getattr(module, name, None)
        if cls is not None:
            return cls
    return fallback


class _Page(Strict, Generic[T]):
    items: list[T]
    total: int
    offset: int
    limit: int


class _PageFilter(Strict):
    q: str | None = None
    order_by: str | None = None
    offset: int = Field(0, ge=0)
    limit: int = Field(100, ge=1, le=500)


class _ScenarioFilter(_PageFilter):
    scenario_id: list[str] | None = None
    validation_status: list[str] | None = None
    source: list[str] | None = None
    difficulty_tier: list[str] | None = None
    include_archived: bool = False


class _CandidateFilter(_PageFilter):
    source_key: list[str] | None = None
    source: list[str] | None = None
    stage: list[str] | None = None
    status: list[str] | None = None
    gate: list[str] | None = None
    scenario_id: list[str] | None = None


class _TrialFilter(_PageFilter):
    trial_id: list[str] | None = None
    scenario_id: list[str] | None = None
    version: list[int] | None = None
    model: list[str] | None = None
    condition: list[str] | None = None
    brief_profile: list[str] | None = None
    status: list[str] | None = None
    outcome_code: list[int] | None = None
    purpose: list[str] | None = None
    origin: list[str] | None = None
    since: datetime | None = None
    until: datetime | None = None


class _ScoreFilter(_PageFilter):
    scenario_id: list[str] | None = None
    condition: list[str] | None = None
    brief_profile: list[str] | None = None
    model: list[str] | None = None


class _LeaderboardFilter(Strict):
    difficulty_tier: list[str] | None = None      # default: informative only (interfaces §9.1)
    include_saturated: bool = False
    brief_profile: list[str] | None = None
    source: list[str] | None = None               # scenario source: github-repo | swebench-annotated | authored
    tier: list[str] | None = None                 # model tiers A-E (brief §3.2)
    scenario_id: list[str] | None = None
    condition: list[str] | None = None


class _WorkflowFilter(_PageFilter):
    workflow_id: list[str] | None = None
    type: list[str] | None = None
    status: list[str] | None = None
    parent_workflow_id: list[str] | None = None
    since: datetime | None = None
    until: datetime | None = None


class _CostFilter(_PageFilter):
    trial_id: list[str] | None = None
    workflow_id: list[str] | None = None
    role: list[str] | None = None
    provider: list[str] | None = None
    model: list[str] | None = None
    since: datetime | None = None
    until: datetime | None = None


class _TableFilter(_PageFilter):
    where: dict[str, str] | None = None           # column -> equality value


class _FunnelStep(Strict):
    stage: str
    kept: int
    rejected: int
    error: int
    pending: int


class _LeaderboardRow(Strict):
    """One model × condition × brief_profile cell over the gated scenarios (interfaces §9.1)."""
    model: str
    condition: str
    brief_profile: str
    n: int
    success_count: int
    k: int | None
    pass_at_k: Decimal | None
    pass_pow_k: Decimal | None
    mean_reward: Decimal | None
    scenario_ids: list[str]
    questions_asked: int
    confirmations: int
    asked_before_implementing_rate: Decimal | None
    cost_usd: Decimal | None
    cost_per_solved_usd: Decimal | None
    confidence_note: str


class _SpendRow(Strict):
    provider: str
    model: str
    role: str | None
    usd: Decimal
    input_tokens: int
    output_tokens: int
    records: int
    cap_usd: Decimal | None


Page = _shared("Page", _Page)
PageFilter = _shared("PageFilter", _PageFilter)
ScenarioFilter = _shared("ScenarioFilter", _ScenarioFilter)
CandidateFilter = _shared("CandidateFilter", _CandidateFilter)
TrialFilter = _shared("TrialFilter", _TrialFilter)
ScoreFilter = _shared("ScoreFilter", _ScoreFilter)
LeaderboardFilter = _shared("LeaderboardFilter", _LeaderboardFilter)
WorkflowFilter = _shared("WorkflowFilter", _WorkflowFilter)
CostFilter = _shared("CostFilter", _CostFilter)
TableFilter = _shared("TableFilter", _TableFilter)
FunnelStep = _shared("FunnelStep", _FunnelStep)
LeaderboardRow = _shared("LeaderboardRow", _LeaderboardRow)
SpendRow = _shared("SpendRow", _SpendRow)

TABLES: tuple[str, ...] = ("scenarios", "candidates", "trials", "exchanges", "scores", "workflows", "costs", "operator_actions", "index_rebuilds")
ROW_OF: dict[str, type] = {"scenarios": ScenarioRow, "candidates": CandidateRow, "trials": TrialRow, "exchanges": ExchangeRow, "scores": ScoreRow,
                           "workflows": WorkflowRow, "costs": CostRow, "operator_actions": OperatorActionRow, "index_rebuilds": IndexRebuildRow}
# Evaluator-only columns/keys (brief §2.5, Codex B-P2-6). Blanked for non-evaluators everywhere; the leak test enumerates routes.
EVALUATOR_ONLY_KEYS: frozenset[str] = frozenset({"disclosed_fact_ids", "redacted_terms", "regenerated", "audit", "withheld_sentences",
                                                 "forbidden_terms", "tier", "tool_calls", "delivery", "task_sense"})


def public_exchange(row) -> dict:
    """An exchange row with the evaluator-only columns blanked (the reader does this too; the console never trusts one layer)."""
    d = row.model_dump(mode="json") if hasattr(row, "model_dump") else dict(row)
    d["disclosed_fact_ids"] = []
    d["redacted_terms"] = []
    d["regenerated"] = None
    d["audit"] = None
    return d
