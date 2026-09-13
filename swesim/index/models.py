"""Row models of the index (interfaces §2) plus the page, filter and report shapes the console codes against.

Contract
- One `Strict` row model per table; the field list is the column list (`swesim.index.schema` derives the tables from
  these models and a test asserts they agree). Nullable means "unknown", never "false" or "not applicable".
- Money and rates are `Decimal` (never float); every datetime is tz-aware (`AwareDatetime`), stored as UTC.
- Every row carries provenance: `created_at`, `updated_at`, `workflow_id`, `run_id`, `worker_image_digest`, `origin`.
  `origin` is required so a row can never be labelled by accident; the writer supplies `Settings.origin` unless the
  file carries one.
- Filters are `Strict` models with optional equality lists, date ranges, free text `q`, `order_by`, `offset` and
  `limit <= 500`. `LeaderboardFilter.purpose` is a single value: calibration and comparison runs are never pooled.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import AwareDatetime, Field, model_validator

from swesim.models import Strict
from swesim.settings import Launcher, Origin
from swesim.storage.layout import Condition, SEGMENT_RE

ValidationStatus = Literal["drafting", "approved", "compiling", "validating", "valid", "invalid", "rejected", "archived"]
DifficultyTier = Literal["saturated", "informative", "broken"]
FunnelStage = Literal["mined", "judged", "probed", "drafted", "approved", "compiled", "validated", "registered", "rejected", "failed"]
CandidateStatus = Literal["kept", "rejected", "error", "pending", "superseded"]
BriefProfile = Literal["natural", "withheld"]
Purpose = Literal["calibration", "comparison", "demonstration", "screen"]
TrialStatus = Literal["planned", "launched", "running", "completed", "runner-error", "launch-failed", "unit-failed", "unresolved", "cancelled"]
ExchangeRole = Literal["agent", "stakeholder", "system"]
ExchangeKind = Literal["ask", "confirm", "review", "system"]
# `unknown`: the reconciler (`swesim.workflows.client.reconcile_workflows`) found no execution in Temporal for a `running` row
# (retention passed, or a row imported from another namespace); terminal, `lifecycle_source == "temporal"`.
WorkflowStatus = Literal["running", "completed", "failed", "cancelled", "terminated", "timed_out", "continued_as_new", "unknown"]
WorkflowLifecycleSource = Literal["workflow", "temporal", "harbor"]
CostRole = Literal["solver", "judge", "drafter", "stakeholder", "withholder", "audit", "probe", "validation"]
ActionResult = Literal["accepted", "rejected", "failed"]
DiscoveryBand = Literal["bugfix", "feature"]
DiscoveryIdentity = Literal["workflow", "legacy"]
FUNNEL_ORDER: tuple[str, ...] = ("mined", "judged", "probed", "drafted", "approved", "compiled", "validated", "registered")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Provenance(Strict):
    """Columns every table carries; `origin` has no default on purpose."""

    created_at: AwareDatetime = Field(default_factory=utcnow)
    updated_at: AwareDatetime = Field(default_factory=utcnow)
    workflow_id: str | None = None
    run_id: str | None = None
    worker_image_digest: str | None = None
    origin: Origin


# ----------------------------------------------------------------------------- rows

class ScenarioRow(Provenance):
    """table scenarios, pk (scenario_id, version)"""

    scenario_id: str
    version: int
    spec_hash: str | None
    source: str
    source_ref: str
    validation_status: ValidationStatus
    compiled_hash: str | None
    image_digest: str | None
    difficulty_tier: DifficultyTier | None
    difficulty_screen: dict | None
    task_sense: dict | None
    registry_json: dict | None
    archived_at: AwareDatetime | None


class CandidateRow(Provenance):
    """table candidates, pk (instance_id, source_key); one row per candidate of a source, describing its current version.

    `stage`/`status` is the terminal decision at the last stage the current version reached: `kept` only at the last
    stage (registered); `rejected` (gate + reasons) or `error` at a gate; `pending` when it was kept at this stage and
    never advanced (`reasons[0]` says why: not-selected | cap-cut | workflow-stopped | in-progress | unknown).
    `earlier_versions` lists the archived versions of the same candidate (`archive/v<n>/`), each
    `{version, stage, status, gate, reasons}` with `status == "superseded"` unless that version was itself rejected or
    errored; the funnel counts each version once (`swesim.index.reports.funnel`)."""

    instance_id: str
    source_key: str
    source: str
    repo: str | None
    pr_number: int | None
    pr_kind: str | None
    stage: FunnelStage
    status: CandidateStatus
    gate: str | None
    reasons: list[str]
    judged_at: AwareDatetime | None
    scenario_id: str | None
    version: int | None
    candidates_path: str | None = None
    earlier_versions: list[dict] | None = None


CANDIDATE_EVENT_IDENTITY: tuple[str, ...] = ("source_key", "instance_id", "workflow_id", "run_id", "phase", "stage", "status", "gate",
                                             "reasons", "result_status", "terminal", "scenario_id", "version", "spec_hash")


def candidate_event_identity(fields: dict) -> str:
    """The receipt's id: SHA256 of its decision content and workflow/run, so an activity retry lands on the same row and a
    genuinely different decision (another run, other reasons) is another receipt. `occurred_at` is deliberately excluded."""
    data = {key: fields.get(key) for key in CANDIDATE_EVENT_IDENTITY}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class CandidateEventRow(Provenance):
    """One immutable stage/decision receipt of a candidate (P1-7); table candidate_events, pk event_id.

    Written by `index_candidate_event_activity` at every gate the workflows decide at (judge, probe, precheck,
    auto-approval, human review, approval timeout, validation, registration, an infrastructure error). `terminal` says
    the workflow stopped working on the candidate there; `project_candidates` lets the latest terminal negative receipt
    (status rejected | error) decide the candidate's row, so a rejected candidate is never shown as kept because its
    scenario files reached a later stage."""

    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_key: str
    instance_id: str
    candidates_path: str | None = None
    scenario_id: str | None = None
    version: int | None = None
    spec_hash: str | None = None
    phase: str
    result_status: str | None = None
    stage: FunnelStage
    status: CandidateStatus
    gate: str | None = None
    reasons: list[str] = Field(default_factory=list)
    terminal: bool
    occurred_at: AwareDatetime
    workflow_id: str
    run_id: str


def discovery_identity(source_key: str, artifact_sha256: str, workflow_id: str | None, run_id: str | None) -> str:
    """Content/run identity; legacy observations carry explicit null workflow and run ids."""
    data = {"source_key": source_key, "artifact_sha256": artifact_sha256, "workflow_id": workflow_id, "run_id": run_id}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DiscoveryRow(Provenance):
    """One immutable discovery observation; table discoveries, pk (source_key, discovery_id)."""

    source_key: str = Field(pattern=SEGMENT_RE.pattern)
    discovery_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: str
    repo: str | None
    band: DiscoveryBand | None
    identity_kind: DiscoveryIdentity
    rejects: dict[Annotated[str, Field(min_length=1)], Annotated[int, Field(strict=True, ge=0)]]
    artifact_path: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    discovered_at: AwareDatetime

    @model_validator(mode="after")
    def _identity(self):
        if bool(self.workflow_id) != bool(self.run_id):
            raise ValueError("discovery workflow and run ids must both be recorded or both unknown")
        kind = "workflow" if self.workflow_id else "legacy"
        if self.identity_kind != kind or self.discovery_id != discovery_identity(self.source_key, self.artifact_sha256, self.workflow_id, self.run_id):
            raise ValueError("discovery identity does not match its source artifact and workflow")
        return self


class TrialRow(Provenance):
    """table trials, pk trial_id"""

    trial_id: str
    scenario_id: str
    version: int | None
    spec_hash: str
    condition: Condition
    brief_profile: BriefProfile
    model: str
    provider: str
    run_config_hash: str
    attempt_index: int
    purpose: Purpose
    status: TrialStatus
    outcome_code: int | None = None
    reward: Decimal | None = None
    success: bool | None = None
    integrity_signal_count: int | None = None
    questions_asked: int | None = None
    confirmations: int | None = None
    asked_before_implementing: bool | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    launcher: Launcher | None = None
    job_name: str | None = None
    cost_usd: Decimal | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    stakeholder_model: str | None = None
    stakeholder_prompt_version: str | None = None
    evaluation_config: dict | None = None
    failure: str | None = None  # why nothing was graded (runner-error / launch-failed / unresolved): runner-error.json or launch.json `error`


class ExchangeRow(Provenance):
    """table exchanges, pk (trial_id, seq); evaluator-only columns: disclosed_fact_ids, redacted_terms, regenerated, audit"""

    trial_id: str
    seq: int
    role: ExchangeRole
    exchange: ExchangeKind
    ts: AwareDatetime | None
    request_id: str | None
    patience_after: int | None
    text_public: str
    disclosed_fact_ids: list[str]
    regenerated: bool | None
    redacted_terms: list[str]
    audit: dict | None


EVALUATOR_ONLY_EXCHANGE_COLUMNS: tuple[str, ...] = ("disclosed_fact_ids", "redacted_terms", "regenerated", "audit")


class ScoreRow(Provenance):
    """table scores, pk (scenario_id, spec_hash, condition, brief_profile, model, run_config_hash)"""

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
    computed_at: AwareDatetime
    requested_k: int | None = Field(default=None, ge=1)


class WorkflowRow(Provenance):
    """table workflows, pk (workflow_id, run_id)"""

    workflow_id: str
    run_id: str
    type: str
    parent_workflow_id: str | None
    status: WorkflowStatus
    params_summary: dict
    failure: str | None
    started_at: AwareDatetime
    closed_at: AwareDatetime | None
    task_queue: str | None
    cost_usd: Decimal | None
    search_attributes: dict
    lifecycle_source: WorkflowLifecycleSource | None = None


class CostRow(Provenance):
    """table costs, pk cost_id (ulid)"""

    cost_id: str
    trial_id: str | None
    workflow_id: str | None
    role: CostRole
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int | None
    usd: Decimal | None  # None = the price is unknown (price_source says why); never zero for "could not tell"
    price_source: str
    at: AwareDatetime


class OperatorActionRow(Provenance):
    """table operator_actions, pk action_id (ulid)"""

    action_id: str
    actor: str
    action: str
    target: str | None
    params: dict
    confirmed: bool
    origin_header: str | None
    result: ActionResult
    workflow_id: str | None
    at: AwareDatetime


class IndexRebuildRow(Provenance):
    """table index_rebuilds, pk rebuild_id"""

    rebuild_id: str
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    counts: dict
    diff: dict | None


# ----------------------------------------------------------------------------- pages, filters, reports

T = TypeVar("T")


class Page(Strict, Generic[T]):
    items: list[T]
    total: int
    offset: int
    limit: int


class PageFilter(Strict):
    offset: int = Field(0, ge=0)
    limit: int = Field(100, ge=1, le=500)
    order_by: str | None = None
    desc: bool = True
    q: str | None = None


class ScenarioFilter(PageFilter):
    scenario_id: list[str] | None = None
    source: list[str] | None = None
    validation_status: list[ValidationStatus] | None = None
    difficulty_tier: list[DifficultyTier] | None = None
    origin: list[Origin] | None = None
    latest_only: bool = False


class CandidateFilter(PageFilter):
    source_key: list[str] | None = None
    instance_id: list[str] | None = None
    source: list[str] | None = None
    repo: list[str] | None = None
    stage: list[FunnelStage] | None = None
    status: list[CandidateStatus] | None = None
    gate: list[str] | None = None
    scenario_id: list[str] | None = None
    judged_from: AwareDatetime | None = None
    judged_to: AwareDatetime | None = None


class TrialFilter(PageFilter):
    trial_id: list[str] | None = None
    scenario_id: list[str] | None = None
    source: list[str] | None = None  # the trial's scenario source (join on scenario_id): the console's task-origin facet
    version: list[int] | None = None
    spec_hash: list[str] | None = None
    condition: list[Condition] | None = None
    brief_profile: list[BriefProfile] | None = None
    model: list[str] | None = None
    provider: list[str] | None = None
    purpose: list[Purpose] | None = None
    status: list[TrialStatus] | None = None
    outcome_code: list[int] | None = None
    success: bool | None = None
    origin: list[Origin] | None = None
    launcher: list[Launcher] | None = None
    started_from: AwareDatetime | None = None
    started_to: AwareDatetime | None = None


class ScoreFilter(Strict):
    scenario_id: list[str] | None = None
    spec_hash: list[str] | None = None
    condition: list[Condition] | None = None
    brief_profile: list[str] | None = None
    model: list[str] | None = None
    run_config_hash: list[str] | None = None
    min_n: int = Field(0, ge=0)
    limit: int = Field(500, ge=1, le=500)


class LeaderboardFilter(Strict):
    """One purpose per board: calibration and comparison are never pooled. `difficulty_tier=None` means the gate's
    default (`informative`); `include_saturated` adds saturated scenarios; broken scenarios never appear."""

    purpose: Purpose = "comparison"
    condition: list[Condition] | None = None
    brief_profile: list[BriefProfile] | None = None
    model: list[str] | None = None
    provider: list[str] | None = None
    source: list[str] | None = None
    scenario_id: list[str] | None = None
    include_saturated: bool = False
    min_n: int = Field(1, ge=1)
    evaluation_cohort: str | None = None
    metric_k: int | None = Field(default=None, ge=1, le=10)


class WorkflowFilter(PageFilter):
    workflow_id: list[str] | None = None
    run_id: list[str] | None = None
    type: list[str] | None = None
    status: list[WorkflowStatus] | None = None
    parent_workflow_id: list[str] | None = None
    task_queue: list[str] | None = None
    started_from: AwareDatetime | None = None
    started_to: AwareDatetime | None = None


class CostFilter(Strict):
    trial_id: list[str] | None = None
    workflow_id: list[str] | None = None
    role: list[CostRole] | None = None
    provider: list[str] | None = None
    model: list[str] | None = None
    at_from: AwareDatetime | None = None
    at_to: AwareDatetime | None = None
    limit: int = Field(500, ge=1, le=500)


class TableFilter(PageFilter):
    """Generic browser: equality on any column of the named table (validated against the schema by the reader)."""

    where: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class FunnelStep(Strict):
    """One stage of the creation funnel (`swesim.index.reports.funnel`): `entered` candidate versions reached this stage;
    `kept` advanced to the next one (at the last stage: are registered); the rest stopped here as `rejected`, `error`,
    `pending` (kept, never advanced) or `superseded` (a later version replaced it). `entered == kept + rejected + error
    + pending + superseded`, and the next stage's `entered` equals this stage's `kept`. `ids` names the kept candidates
    (`<scenario_id>@v<version>`), so the last stage lists the registered scenarios."""

    stage: FunnelStage
    kept: int
    rejected: int
    error: int
    pending: int
    superseded: int = 0
    entered: int = 0
    ids: list[str] = Field(default_factory=list)


class LeaderboardRow(Strict):
    """One cell: model x condition x brief_profile for one purpose, over gated scenarios only."""

    model: str
    provider: str
    condition: Condition
    brief_profile: str
    purpose: Purpose
    n: int
    scenarios: int
    success_count: int
    pass_rate: Decimal | None
    mean_reward: Decimal | None
    questions_asked: int
    confirmations: int
    cost_usd: Decimal | None
    cost_per_success: Decimal | None
    evaluation_cohort: str = "unknown"
    cohort_label: str = "unknown"


class SpendRow(Strict):
    provider: str
    model: str
    records: int
    input_tokens: int
    output_tokens: int
    usd: Decimal
    cap_usd: Decimal | None
    share_of_cap: Decimal | None
