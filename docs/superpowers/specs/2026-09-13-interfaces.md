# Frozen interfaces — Phase 0 (2026-09-13, integrator)

Binding for every stream tonight. The brief (`2026-09-13-hosted-pipeline-brief.md`) says what; the working model
(`../plans/2026-09-13-overnight-working-model.md`) says how; this file pins the names, signatures, paths and payloads the
streams share so they can work in parallel without waiting on each other. A stream that needs a change here asks the
integrator (its report's `concerns`), never edits another stream's file.

Conventions: Python 3.12, Pydantic v2 `Strict` models (`extra="forbid"`), `from __future__ import annotations`, typed
signatures, module docstrings stating the contract. Errors: `ValueError` subclasses for invalid input (non-retryable),
`RuntimeError` subclasses for transient failures (retryable). No `os.environ` reads outside `swesim/settings.py`; take a
`Settings` (or the values you need) as a parameter or call `get_settings()` once at the boundary.

## 0. `swesim/settings.py` (done, on main)

`Settings.from_env()`, `get_settings()`, `reset_settings()`, `secret(name)`, `has_secret(name)`, `MissingSecret`,
`ENV_VARS`, `render_env_example()`, `QUEUES`. Fields: `env_file, workspace, registry_path, bucket, database_url
(SecretStr|None), task_queues, max_concurrent_activities, trial_launcher, origin, worker_image_digest, gcp_project,
gcp_region, evaluators, spend_cap_usd, stakeholder_url, console_url, temporal_address, temporal_namespace,
temporal_ui_url, direct_stages`; properties `index_enabled`, `temporal_cloud`; `workflow_url(workflow_id, run_id=None)`.
Every module that today reads `SWESIM_*`/`TEMPORAL_*`/`~/Documents/.env` switches to this (owner of the file does it).

## 1. `swesim/storage/layout.py` — the record's layout (owner: storage)

The bucket *is* the workspace: `gs://<bucket>/` mounted at `SWESIM_WORKSPACE`. Paths are derived here and nowhere else;
`swesim/paths.py:Workspace` becomes a thin adapter delegating to `Layout` (same method names kept for callers).

```python
class Layout(Strict):
    root: Path                                   # SWESIM_WORKSPACE
    # scenarios
    def scenario_dir(self, scenario_id: str) -> Path                  # root/scenarios/<sid>
    def scenario_file(self, scenario_id, name: ScenarioFile) -> Path  # scenario.json | scenario.draft.json | draft.meta.json | judge.json | approval.json | compile.json | validation.json
    def scenario_assets_dir(self, scenario_id) -> Path                # root/scenarios/<sid>/assets
    def scenario_archive_dir(self, scenario_id, version: int) -> Path # root/scenarios/<sid>/archive/v<version>
    # compiled tasks
    def task_dir(self, scenario_id, condition: Condition) -> Path     # root/tasks/<sid>__<condition>
    # trials
    def trial_dir(self, trial_id: str) -> Path                        # root/trials/<tid>
    def trial_file(self, trial_id, name: TrialFile) -> Path           # request.json launch.json status.json record.json report.json reward.json run_config.json spec.json transcript.json runner-error.json launcher.log
    def trial_task_dir(self, trial_id) -> Path                        # root/trials/<tid>/task   (holds the session token: never listed by the console)
    # exports
    def export_path(self, trial_id) -> Path                           # root/exports/<tid>.episode.json
    # candidates (creation funnel files, one family per source key)
    def candidates_dir(self) -> Path                                  # root/candidates
    def candidates_file(self, source_key: str, stage: CandidateStage) -> Path
        # stage: mined -> <key>.jsonl | judged -> <key>.judged.jsonl | probed -> <key>.judged.probed.jsonl
        #        probe_summary -> <key>.judged.probed.summary.json | rejects -> <key>.rejects.json | status -> <key>.status
    def probe_dir(self, instance_id: str) -> Path                     # root/probes/<iid>
    # evidence, calibration, jobs
    def evidence_dir(self, kind: str) -> Path                         # root/evidence/<kind>      kinds: cloudrun cloud sweep console audit model-probe
    def calibration_path(self, judge_model: str, day: date) -> Path   # root/calibration/<judge-model-slug>/<YYYY-MM-DD>.json
    def jobs_dir(self, kind: JobKind) -> Path                         # root/jobs/<probe|runs|validate>
    def registry_path(self) -> Path                                   # root/registry.json  (Settings.registry_path may override for laptops)
    def dataset_path(self) -> Path                                    # root/dataset.json
    def relative(self, path: Path) -> str                             # bucket object name for a path under root (str, POSIX)
```
Ids are validated (`ID_RE` for scenario ids, `^[0-9a-f]{24}$` for trial ids); invalid ids raise `LayoutError(ValueError)`.
Tests pin every path string. `atomic_json` stays in `swesim/paths.py` (write-then-`os.replace`, FUSE-safe).
Retired: `Workspace.jobs_dir`/`candidates_dir` properties remain as adapters; `swesim/workflows/activities.py:trial_dir`
delegates to `Layout`.

## 2. `swesim/index/` — the Postgres index (owner: storage)

The files are the record; the index is a disposable, rebuildable projection. One Pydantic row model per table in
`models.py`, one SQLAlchemy `Table` per model in `schema.py` (same names, same columns), Alembic under
`swesim/index/migrations/` (forward-only). Provenance columns on every table: `created_at timestamptz`, `updated_at
timestamptz`, `workflow_id text null`, `run_id text null`, `worker_image_digest text null`, `origin text` (Origin enum).

```python
class ScenarioRow(Strict):      # table scenarios, pk (scenario_id, version)
    scenario_id: str; version: int; spec_hash: str | None; source: str; source_ref: str
    validation_status: Literal["drafting","approved","compiling","validating","valid","invalid","rejected","archived"]
    compiled_hash: str | None; image_digest: str | None
    difficulty_tier: Literal["saturated","informative","broken"] | None
    difficulty_screen: dict | None   # {model, hidden: {n, successes}, full: {n, successes}, labelled_at}
    task_sense: dict | None          # audit labels (stream task-sense): {coherent, fair_tests, plausible_withholding, reasons, audited_at, model}
    registry_json: dict | None; archived_at: datetime | None; + provenance
class CandidateRow(Strict):     # table candidates, pk (instance_id, source_key)
    instance_id: str; source_key: str; source: str; repo: str | None; pr_number: int | None; pr_kind: str | None
    stage: Literal["mined","judged","probed","drafted","approved","compiled","validated","registered","rejected","failed"]
    status: Literal["kept","rejected","error","pending"]; gate: str | None; reasons: list[str]
    judged_at: datetime | None; scenario_id: str | None; version: int | None; + provenance
class TrialRow(Strict):         # table trials, pk trial_id
    trial_id: str; scenario_id: str; version: int | None; spec_hash: str; condition: Condition
    brief_profile: Literal["natural","withheld"]; model: str; provider: str; run_config_hash: str; attempt_index: int
    purpose: Literal["calibration","comparison","demonstration","screen"]
    status: Literal["planned","launched","running","completed","runner-error","launch-failed","unit-failed","unresolved","cancelled"]
    outcome_code: int | None; reward: Decimal | None; success: bool | None; integrity_signal_count: int | None
    questions_asked: int | None; confirmations: int | None; asked_before_implementing: bool | None
    started_at: datetime | None; finished_at: datetime | None; launcher: Launcher | None; job_name: str | None
    cost_usd: Decimal | None; input_tokens: int | None; output_tokens: int | None
    stakeholder_model: str | None; stakeholder_prompt_version: str | None; + provenance
class ExchangeRow(Strict):      # table exchanges, pk (trial_id, seq); evaluator_only columns: disclosed_fact_ids, redacted_terms, regenerated, audit
    trial_id: str; seq: int; role: Literal["agent","stakeholder","system"]; exchange: Literal["ask","confirm","review","system"]
    ts: datetime | None; request_id: str | None; patience_after: int | None; text_public: str
    disclosed_fact_ids: list[str]; regenerated: bool | None; redacted_terms: list[str]
    audit: dict | None   # task-sense labels per reply/question
class ScoreRow(Strict):         # table scores, pk (scenario_id, spec_hash, condition, brief_profile, model, run_config_hash)
    scenario_id: str; spec_hash: str; condition: Condition; brief_profile: str; model: str; run_config_hash: str
    n: int; success_count: int; pass_at_k: Decimal | None; pass_pow_k: Decimal | None; mean_reward: Decimal | None
    questions_asked: int; confirmations: int; computed_at: datetime; + provenance
class WorkflowRow(Strict):      # table workflows, pk (workflow_id, run_id)
    workflow_id: str; run_id: str; type: str; parent_workflow_id: str | None; status: Literal["running","completed","failed","cancelled","terminated","timed_out","continued_as_new"]
    params_summary: dict; failure: str | None; started_at: datetime; closed_at: datetime | None
    task_queue: str | None; cost_usd: Decimal | None; search_attributes: dict; + provenance
class CostRow(Strict):          # table costs, pk cost_id (ulid)
    cost_id: str; trial_id: str | None; workflow_id: str | None; role: Literal["solver","judge","drafter","stakeholder","withholder","audit","probe","validation"]
    provider: str; model: str; input_tokens: int; output_tokens: int; cached_tokens: int | None; usd: Decimal
    price_source: str; at: datetime; + provenance
class OperatorActionRow(Strict):# table operator_actions, pk action_id (ulid)
    action_id: str; actor: str; action: str; target: str | None; params: dict; confirmed: bool; origin_header: str | None
    result: Literal["accepted","rejected","failed"]; workflow_id: str | None; at: datetime; + provenance
class IndexRebuildRow(Strict):  # table index_rebuilds, pk rebuild_id
    rebuild_id: str; started_at: datetime; finished_at: datetime | None; counts: dict; diff: dict | None; + provenance
```

```python
class IndexStore:                                       # swesim/index/store.py
    def __init__(self, url: str | SecretStr)            # SQLAlchemy engine; pool_pre_ping; `postgresql+psycopg://`
    @classmethod
    def from_settings(cls, s: Settings) -> IndexStore | None   # None when the index is disabled
    def migrate(self) -> None                           # alembic upgrade head
    def upsert_scenario(self, row: ScenarioRow) -> None # every upsert is idempotent on the pk; updated_at bumps
    def upsert_candidate / upsert_trial / upsert_exchanges(rows) / upsert_score / upsert_workflow / add_cost / add_operator_action
    def advisory_lock(self, key: str) -> ContextManager[None]   # pg_advisory_xact_lock(hashtext(key)); replaces registry.json.lock
    def rebuild_from_layout(self, layout: Layout, registry_path: Path | None = None) -> IndexRebuildRow
                                                        # projects every file into rows; returns counts + diff vs previous rows
class IndexReader(Protocol):                            # swesim/index/reads.py — what the console codes against
    def scenarios(self, f: ScenarioFilter) -> Page[ScenarioRow]
    def scenario(self, scenario_id: str, version: int | None = None) -> ScenarioRow | None
    def scenario_versions(self, scenario_id: str) -> list[ScenarioRow]
    def candidates(self, f: CandidateFilter) -> Page[CandidateRow]
    def funnel(self, source_key: str | None = None) -> list[FunnelStep]   # FunnelStep(stage, kept, rejected, error, pending)
    def trials(self, f: TrialFilter) -> Page[TrialRow]
    def trial(self, trial_id: str) -> TrialRow | None
    def exchanges(self, trial_id: str, evaluator: bool) -> list[ExchangeRow]  # evaluator=False blanks the evaluator-only columns
    def scores(self, f: ScoreFilter) -> list[ScoreRow]
    def leaderboard(self, f: LeaderboardFilter) -> list[LeaderboardRow]    # per model × condition × brief_profile with n; excludes saturated unless asked
    def workflows(self, f: WorkflowFilter) -> Page[WorkflowRow]
    def workflow(self, workflow_id: str) -> WorkflowRow | None
    def costs(self, f: CostFilter) -> list[CostRow]
    def spend(self) -> list[SpendRow]                                       # per provider/model totals + cap from Settings
    def operator_actions(self, f: PageFilter) -> Page[OperatorActionRow]
    def table(self, name: str, f: TableFilter) -> Page[dict]               # generic database browser; name validated against the schema
    def rebuilds(self) -> list[IndexRebuildRow]
```
`Page[T] = Strict(items: list[T], total: int, offset: int, limit: int)`. Filters are Strict models with optional fields
(equality lists, date ranges, free-text `q`, `order_by`, `offset`, `limit<=500`). `IndexStore` implements
`IndexReader`. A `FileIndexReader(layout, registry_path)` implements the same protocol over the files (today's
`dashboard/data.py` logic) so the console works without Postgres and tests are offline.

`swesim/index/activities.py` — Temporal activities on queue `swesim-control`, registered by the worker, called by the
workflows at the end of each stage/trial (idempotent; `p` carries ids and paths only):
`index_scenario_activity({"scenario_id","version"})`, `index_candidate_activity({"source_key","instance_id"})`,
`index_trial_activity({"trial_id"})` (also writes exchanges, costs from the record), `index_score_activity({"scenario_id"})`,
`index_workflow_activity({"workflow_id","run_id","type","status","params_summary","failure","parent_workflow_id"})`,
`index_rebuild_activity({})`. Each reads the files through `Layout`, builds the row, upserts; when the index is disabled
they return `{"indexed": False, "reason": "index disabled"}` and never fail the workflow.

CLI: `swesim index migrate|rebuild|status` (Typer sub-app in `swesim/index/cli.py`; integrator mounts it).
Import: `scripts/cloud/import_vm.sh` (rsync the VM record into the bucket, then `swesim index rebuild --origin vm-systemd`).

## 3. `swesim/workflows/launchers/` — trial launcher protocol (owner: launcher+temporal)

```python
# base.py
@dataclass(frozen=True)
class TrialEvidence:            # moved from activities.py unchanged in meaning; `unit_*` renamed to runtime_*
    has_record: bool; has_runner_error: bool; status: str | None; launch_phase: str | None; launch_returncode: int | None
    runtime_state: Literal["absent","running","succeeded","failed","unknown"]     # replaces load/active/sub triple
    runtime_detail: str                                                            # systemd states or Cloud Run execution name/condition
def decide_launch(ev: TrialEvidence) -> Literal["ingest","runner-error","reconcile","unresolved","launch-failed","launch"]  # pure, same table as today
class Launcher(Protocol):
    name: Launcher                                                  # "systemd" | "cloudrun" | "subprocess"
    def runtime_state(self, trial_id: str, launch: dict) -> tuple[RuntimeState, str]
    def start(self, trial_id: str, request_path: Path, intent: dict) -> LaunchAck   # LaunchAck(returncode, stdout, stderr, handle)
    def cancel(self, trial_id: str, launch: dict) -> None
    def cleanup(self, trial_id: str, launch: dict) -> None          # cloudrun: delete the Job after the record exists
def launch_once(layout: Layout, launcher: Launcher, p: dict, *, image_digest: str | None) -> dict   # the same algorithm as today's activities.launch_once
def read_evidence(layout: Layout, launcher: Launcher, trial_id: str) -> TrialEvidence
# systemd.py: today's systemd code behind the protocol, unchanged behaviour.  subprocess.py: local smoke only.
# cloudrun.py (stream deliverable): Jobs API v2 (google-cloud-run), job name `swesim-trial-<tid>`, 409 == already launched,
#   labels swesim-trial-id/swesim-scenario-id/swesim-run-workflow-id, task_count=1, parallelism=1, max_retries=0,
#   timeout = solver_timeout + 2h, secrets as Secret Manager env refs, volume mount of the bucket at /workspace,
#   `executions.create` once; runtime_state from the execution's conditions; cleanup deletes the Job.
def get_launcher(s: Settings) -> Launcher
```
`launch.json` gains `"launcher": name, "handle": <job/execution name or unit>, "image_digest"`. Every activity result and
every `record.json` gets `worker_image_digest` (from `Settings.worker_image_digest`) and `origin`.

## 4. `swesim/workflows/queues.py` and `retries.py` (owner: launcher+temporal)

```python
AUTHORING, BUILD, TRIALS, CONTROL = "swesim-authoring", "swesim-build", "swesim-trials", "swesim-control"
QUEUE_OF: dict[str, str] = {  # activity name -> queue; a test asserts every registered activity is present
  "probe_models_activity": AUTHORING, "discover_activity": AUTHORING, "mine_activity": AUTHORING, "judge_one_activity": AUTHORING,
  "list_candidates_activity": AUTHORING, "draft_activity": AUTHORING, "precheck_draft_activity": AUTHORING, "auto_approve_activity": AUTHORING,
  "human_approve_activity": AUTHORING, "verify_approval_activity": AUTHORING, "scenario_version_activity": AUTHORING,
  "archive_scenario_activity": AUTHORING, "register_activity": AUTHORING, "plan_trials_activity": AUTHORING, "score_activity": AUTHORING,
  "export_activity": AUTHORING, "difficulty_label_activity": AUTHORING,
  "probe_candidate_activity": BUILD, "compile_activity": BUILD, "validate_control_activity": BUILD, "evaluate_controls_activity": BUILD, "validate_activity": BUILD,
  "launch_trial_activity": TRIALS, "await_trial_activity": TRIALS, "cancel_trial_activity": TRIALS, "cleanup_trial_activity": TRIALS,
  "index_scenario_activity": CONTROL, "index_candidate_activity": CONTROL, "index_trial_activity": CONTROL, "index_score_activity": CONTROL,
  "index_workflow_activity": CONTROL, "index_rebuild_activity": CONTROL, "record_cost_activity": CONTROL, "spend_check_activity": CONTROL,
  "audit_transcript_activity": CONTROL, "audit_scenario_activity": CONTROL }
CONCURRENCY: dict[str, int] = {AUTHORING: 32, BUILD: 8, TRIALS: 64, CONTROL: 16}
def queue_for(activity_name: str) -> str        # KeyError -> UnroutedActivity(ValueError)
def activities_for(queue: str) -> list[str]
```
```python
# retries.py — every execute_activity passes one of these; act(name, p, minutes, cls=...) in workflows.py resolves queue + policy
LLM      = RetryPolicy(initial_interval=10s, backoff=2.0, maximum_interval=2min, maximum_attempts=4)   # provider 429/5xx/timeouts
BUILD    = RetryPolicy(initial_interval=30s, backoff=2.0, maximum_interval=5min, maximum_attempts=3)   # Modal/Harbor infrastructure
READ     = RetryPolicy(initial_interval=5s,  backoff=2.0, maximum_interval=1min, maximum_attempts=5)   # polls, index writes
ONCE     = RetryPolicy(maximum_attempts=1)                                                             # launch (reconcile via decide_launch)
def non_retryable(msg: str, *, kind: str) -> ApplicationError     # gate rejections, schema failures, "not kept": type=kind, non_retryable=True
def transient(msg: str, *, kind: str) -> ApplicationError         # explicit retryable failure with a type
```
Worker (`worker.py`): one `Worker` per queue in `Settings.task_queues`, `activities=[A for A in ALL if queue_for(A.__name__)==queue]`,
`max_concurrent_activities=Settings.max_concurrent_activities or CONCURRENCY[queue]`, workflows registered on every queue
(workflows themselves run on the queue they were started on: operator workflows on AUTHORING, children inherit unless
`task_queue=` is passed; children are started on AUTHORING).

## 5. `swesim/workflows/search_attributes.py` (owner: launcher+temporal)

```python
SCENARIO_ID, MODEL, CONDITION, STATUS, VERSION, PURPOSE = "swesim_scenario_id", "swesim_model", "swesim_condition", "swesim_status", "swesim_version", "swesim_purpose"
KEYS = (...six...)                                   # Keyword attributes, already registered on swesim.abcde; `swesim workflows ensure-search-attributes` registers them elsewhere (local dev server)
def attributes(*, scenario_id=None, model=None, condition=None, status=None, version=None, purpose=None) -> TypedSearchAttributes
def upsert_status(status: str) -> None                # inside a workflow: workflow.upsert_search_attributes([...])
```
Every `start_workflow`/`start_child_workflow` passes `search_attributes=attributes(...)`; status changes call `upsert_status`.

## 6. `swesim/costs.py` (owner: models)

```python
class Price(Strict): provider: str; model: str; input_per_m: Decimal; output_per_m: Decimal; cached_input_per_m: Decimal | None; price_source: str; as_of: date
PRICES: tuple[Price, ...]        # verified ids only; unknown -> price_source "unknown", usd None (never a guess)
class CostRecord(Strict):        # the same fields as CostRow minus provenance; written to trials/<tid>/costs.json and exported to the index
def price_for(model_id: str) -> Price | None
def cost_of(model_id: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> tuple[Decimal | None, str]
def solver_usage_from_trajectory(traj: dict) -> Usage           # mini-swe-agent per-step usage / OpenRouter generation cost when present
def record_cost(layout: Layout, trial_id: str | None, workflow_id: str | None, role: str, model_id: str, usage: Usage) -> CostRecord
```
`swesim/llm.py:chat_json` returns usage (`ChatResult(data, usage)`) or exposes `last_usage()`; the pipeline roles record
costs with `record_cost(role=...)`. Provider table for solvers lives in `swesim/stages/run.py:PROVIDER_HOSTS` extended with
`"openrouter": "openrouter.ai"`; the model id grammar is `<provider>/<vendor-or-model>[/<model>]`.

## 7. Model roles (owner: models) — `swesim/roles.py`

```python
ROLE_CHAINS = {"judge_model": ["anthropic/claude-opus-5", "openai/gpt-5.6-sol", "openai/gpt-5.5"],
               "draft_model": ["anthropic/claude-sonnet-5", "openai/gpt-5.6-sol"],
               "stakeholder_model": ["anthropic/claude-opus-5", "openai/gpt-5.6-sol", "anthropic/claude-sonnet-5"],
               "audit_model": ["anthropic/claude-opus-5", "openai/gpt-5.6-sol"],
               "screen_model": ["openrouter/deepseek/deepseek-v3.2"]}
TIERS = {"A": [...], "B": [...], "C": [...], "D": [...], "E": [...]}    # brief §3.2, verbatim ids
def resolve_roles(probe: Callable[[str], bool]) -> dict[str, str]        # judge != draft rule kept; records reasoning
```
`activities.FALLBACK_CHAINS` is replaced by `ROLE_CHAINS` (integrator wires the import). `StakeholderConfig.model_id`
default becomes `anthropic/claude-opus-5` with `prompt_version = "v3"` (integrator edits models.py).

## 8. Console data access (owner: console consumes; storage provides)

The console imports only `swesim.index.reads.IndexReader`, `swesim.index.models.*`, `swesim.settings`, and the
Temporal client. `get_reader(settings) -> IndexReader` returns `IndexStore` when `index_enabled`, else `FileIndexReader`.
Evaluator identity: `X-Goog-Authenticated-User-Email` (`accounts.google.com:<email>`) verified via the IAP JWT
(`X-Goog-IAP-JWT-Assertion`, audience = the service's IAP audience from `SWESIM_IAP_AUDIENCE`, added to ENV_VARS by the
console stream via the integrator) against `Settings.evaluators`. Without IAP headers (local), evaluator = False, and
`?evaluator=1` is gone. Every mutating route: confirmation token (two-step), scheme+host+port Origin check, and an
`OperatorActionRow` written through the store (or a JSONL under `evidence/console/actions.jsonl` when the index is off).

## 9. Infra ⇄ code contract (owner: infra)

Terraform outputs (`infra/outputs.json` via `terraform output -json`): `project`, `region`, `bucket`,
`worker_service_account`, `deployer_service_account`, `artifact_repo` (`us-central1-docker.pkg.dev/<p>/swesim`),
`sql_connection_name`, `database_name`, `database_user`, `console_url`, `iap_audience`, `worker_pools` (map queue → name),
`wif_provider`. Env set on pools/Jobs/console (names from `ENV_VARS`): `SWESIM_WORKSPACE=/workspace`,
`SWESIM_REGISTRY=/workspace/registry.json`, `SWESIM_BUCKET`, `SWESIM_TASK_QUEUE=<queue>`, `SWESIM_TRIAL_LAUNCHER=cloudrun`,
`SWESIM_ORIGIN=cloudrun`, `SWESIM_WORKER_IMAGE_DIGEST=<digest>`, `SWESIM_GCP_PROJECT`, `SWESIM_GCP_REGION`,
`SWESIM_EVALUATORS`, `SWESIM_SPEND_CAP_USD`, `SWESIM_STAKEHOLDER_URL`, `SWESIM_CONSOLE_URL`, `TEMPORAL_ADDRESS`,
`TEMPORAL_NAMESPACE`, `SWESIM_IAP_AUDIENCE`; secrets as Secret Manager references: `TEMPORAL_API_KEY`,
`SWESIM_WORKER_SECRET`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GH_TOKEN`, `MODAL_TOKEN_ID`,
`MODAL_TOKEN_SECRET`, `SWESIM_DATABASE_URL` (the full URL `postgresql+psycopg://swesim:<pw>@/swesim?host=/cloudsql/<conn>`
stored as one secret so the code never assembles credentials). Worker entrypoint: `python -m swesim.workflows.worker`.
Console entrypoint: `uvicorn swesim.dashboard.app:app --host 0.0.0.0 --port 8080`. Trial Job command:
`python -m swesim.workflows.activities run-trial /workspace/trials/<tid>/request.json`. Bucket mounted read-write on
pools and Jobs, read-only on the console.

## 10. Ownership (disjoint; ask the integrator for anything else)

| Stream | Owns |
|---|---|
| integrator | `swesim/models.py`, `swesim/cli.py` (mounting sub-apps), `swesim/settings.py`, this file, ledger, merges |
| infra | `infra/**`, `scripts/cloud/**`, `Dockerfile.worker`, `cloudbuild.yaml`, `Makefile`, `.github/workflows/**`, `.dockerignore` |
| storage+index | `swesim/storage/**`, `swesim/index/**`, `swesim/paths.py`, `tests/test_layout.py`, `tests/test_index*.py` |
| launcher+temporal | `swesim/workflows/**` (except `client.py` shared with console: launcher stream owns), `tests/test_workflows.py`, `tests/test_launchers*.py`, `tests/test_queues.py` |
| console | `swesim/dashboard/**`, `tests/test_dashboard*.py`, `docs/superpowers/specs/2026-09-13-console-design.md` |
| review-fixes | `swesim/stakeholder/**`, `swesim/stages/draft_spec.py`, `swesim/stages/approve.py`, `swesim/sources/**`, `swesim/stages/score.py`, `swesim/stages/compile.py`, `swesim/verifier/**`, `swesim/tamper.py`, their tests |
| models | `swesim/llm.py`, `swesim/costs.py`, `swesim/roles.py`, `swesim/stages/run.py`, `swesim/stages/judge.py` (calibration), `swesim/templates/**`, `tests/test_llm.py`, `tests/test_run.py`, `tests/test_costs.py`, `tests/test_roles.py` |
| task-sense | `swesim/audit/**`, `tests/test_audit*.py`, `docs/grading.md` audit section |
| docs | `docs/setup.md`, `docs/runbook.md`, `README.md`, `docs/architecture.md`, `docs/ops.md`, `docs/grading.md` (except audit section), `.env.example` text |
