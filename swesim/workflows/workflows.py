"""Temporal workflows (thin). Payloads carry ids, paths, hashes and model ids; files stay in the workspace.

Task creation and task running are separate:
- CreateTasksWorkflow(sources, ...) : probe models once, mine each source, fan out one CandidateWorkflow per candidate
  (id `create:<scenario_id>`, REJECT_DUPLICATE) bounded by `max_parallel`; `progress` lists every candidate's stage.
- CandidateWorkflow(candidate)      : judge -> draft -> approval (auto gates, or "hold": wait for a reviewer's `approve`
  signal after schema/asset/leak pre-checks) -> compile -> validate -> register.
- RunTasksWorkflow(scenario_ids, models, ...): plan_trials per scenario, TrialWorkflow children (id = trial id,
  REJECT_DUPLICATE) bounded by `max_parallel_trials`, then score + export per scenario; `progress` lists every trial.
- PipelineWorkflow                  : the original single-candidate composition, built from the same pieces.
TrialWorkflow id == trial_id(spec_hash, condition, run_config_hash, attempt_index). Launch is never retried; polling is.
Full VM loss surfaces as an explicitly unresolved attempt, never a fresh solver launch.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    TemporalError,
    WorkflowAlreadyStartedError,
    is_cancelled_exception,
)

from swesim.workflows import queues, retries
from swesim.workflows import search_attributes as SA
from swesim.workflows.payloads import check_payload

ONE = RetryPolicy(maximum_attempts=1)          # pre-patch command shape, kept for replay of executions started before PATCH_QUEUES
LEGACY_READ = RetryPolicy(maximum_attempts=3)  # idem, for await_trial_activity
# Versioning discipline (brief §2.8 item 4): every change to a workflow's command sequence ships behind a patch id
# until no pre-change execution remains; `_patched` answers True for new executions and for direct (non-Temporal) calls.
PATCH_QUEUES = "20260913-queues"               # act(): task queue per activity + retry classes (items 1-2)
PATCH_HOOKS = "20260913-bookkeeping"           # index / cost / spend hooks and trial cleanup after every stage and trial (item 3)
PATCH_SPLIT = "20260913-split-activities"      # discover (GitHub only) + per-control validation; TrialWorkflow stops its runtime on cancel (item 4)
PATCH_BATCH = "20260913-bounded-batches"
PATCH_SEARCH = "20260913-search-attributes"
PATCH_DIFFICULTY = "20260913-difficulty-screen"
PATCH_AUDIT = "20260913-automatic-audits"
PATCH_FRONTIER = "20260913-frontier-admission"
PATCH_SCORE_K = "20260913-requested-score-k"
PATCH_FROZEN_RETRY = "20260913-frozen-candidate-retry"
CHILDREN_PER_RUN = 100
HOOK_MINUTES = 2
CONTROL_MINUTES = 60                           # one Harbor control run on Modal (validate_scenario's whole set had 180)
APPROVAL_TIMEOUT = timedelta(hours=24)   # PipelineWorkflow signal mode
HOLD_TIMEOUT = timedelta(days=7)         # CreateTasksWorkflow approval_mode="hold"
APPROVAL_MODES = {"auto", "signal", "human"}  # PipelineWorkflow; "human" is the brief's older spelling of "signal"
CREATE_APPROVAL_MODES = {"auto", "hold"}
# Re-draft-once trigger. Structured: approve_scenario may return {"gate": "withholding-leak"}. Fallback on reason text:
# a reason starting with "withholding", or containing "leak" or "withheld" (the approve stage's "withheld brief still
# contains excerpt of <fact>"). Anything else (judge, load-bearing, controls, assets) moves to the next candidate.
LEAK_GATE = "withholding-leak"
REJECT_DUP = WorkflowIDReusePolicy.REJECT_DUPLICATE
# Candidate children: a terminal outcome (valid/invalid/rejected/not-reproducible/...) completes the child, so the id
# `create:<scenario_id>` is consumed and a later CreateTasksWorkflow records `duplicate-rejected`. An infrastructure
# failure (LLM/Harbor/cancel) FAILS the child, so a later run may start the same id again and recover the candidate.
DUP_FAILED_ONLY = WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY


def _refusable(e: BaseException) -> bool:
    """True for an activity that ended with an ApplicationError (a refused decision); cancellations and infrastructure
    failures are not refusals and must propagate."""
    return isinstance(e, ActivityError) and isinstance(e.cause, ApplicationError)


def _is_leak_reason(reason) -> bool:
    r = str(reason).strip().lower()
    return r.startswith("withholding") or "leak" in r or "withheld" in r


def _leak_rejection(approved: dict) -> bool:
    return approved.get("gate") == LEAK_GATE or any(_is_leak_reason(r) for r in approved.get("reasons", []) or [])


def _patched(name: str) -> bool:
    """`workflow.patched` inside a workflow. A workflow body run as a plain coroutine (direct tests) is always new code."""
    return workflow.patched(name) if workflow.in_workflow() else True


async def act(name: str, p: dict, minutes: int = 10, *, retry: RetryPolicy = retries.LLM):
    """One activity on its queue (`queues.queue_for`) with its retry class. Call sites pick the class: LLM for judge /
    draft / approve / stakeholder-bound work, BUILD for probe / compile / validate, READ for polls and local reads,
    ONCE for launch. The pre-patch shape (inherited queue, one attempt) is kept for replay."""
    try:
        check_payload(p, f"{name} input")
    except ValueError as e:
        raise ApplicationError(str(e), non_retryable=True) from e
    timeouts = {"start_to_close_timeout": timedelta(minutes=minutes), "heartbeat_timeout": timedelta(minutes=2)}
    if _patched(PATCH_QUEUES):
        if name == "launch_trial_activity" and _patched(PATCH_SPLIT):
            timeouts["cancellation_type"] = workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
        return await workflow.execute_activity(name, p, task_queue=queues.queue_for(name), retry_policy=retry, **timeouts)
    return await workflow.execute_activity(name, p, retry_policy=LEGACY_READ if name == "await_trial_activity" else ONE, **timeouts)


def params_summary(params: dict) -> dict:
    """Small, secret-free summary of a workflow's params (ids, model ids, counts only): the start memo and the index row."""
    out: dict = {}
    if "sources" in params:
        out["sources"] = [s.get("repo") or f"{s.get('kind', 'swebench-annotated')}:{s.get('pool', 'A')}" for s in params.get("sources", [])]
    for k in ("scenario_ids", "models", "conditions", "k", "judge_model", "draft_model", "stakeholder_model", "approval_mode", "purpose", "source", "repo",
              "instance_id", "scenario_id", "trial_id", "model", "condition", "attempt_index", "version", "brief_profile"):
        if params.get(k) not in (None, "", []):
            out[k] = params[k]
    return out


async def hook(name: str, p: dict) -> dict:
    """A CONTROL bookkeeping activity (index write, cost record, spend check) that never fails its caller. A missing activity
    type (the owning stream is not merged: the worker fails it as NotFoundError), a timeout while no control worker polls, or
    an exhausted retry is recorded as `{"indexed": False, "reason": ...}` so the workflow's progress shows the gap. Executions
    started before the hooks existed replay without them."""
    if not _patched(PATCH_HOOKS):
        return {"indexed": False, "reason": "execution predates the bookkeeping hooks"}
    try:
        r = await act(name, p, HOOK_MINUTES, retry=retries.HOOK)
    except Exception as e:  # noqa: BLE001 - bookkeeping never decides a workflow's outcome; the record on disk is the truth
        _raise_if_cancelled(e)
        return {"indexed": False, "reason": _err(e)}
    return r if isinstance(r, dict) else {"indexed": True, "result": r}


def _workflow_identity() -> dict | None:
    """workflow_id / run_id / type / parent of the running workflow; None for a body run as a plain coroutine (direct tests)."""
    try:
        info = workflow.info()
    except (RuntimeError, TemporalError):  # "Not in workflow event loop"
        return None
    parent = getattr(info, "parent", None)
    return {"workflow_id": getattr(info, "workflow_id", None), "run_id": getattr(info, "run_id", None), "type": getattr(info, "workflow_type", None),
            "parent_workflow_id": getattr(parent, "workflow_id", None) if parent is not None else None}


async def index_workflow(status: str, summary: dict, failure: str | None = None) -> dict:
    """`index_workflow_activity` at a workflow's start and at every status change (interfaces §2), parent id included."""
    if _patched(PATCH_SEARCH):
        SA.upsert_status(status)
    ident = _workflow_identity()
    if ident is None:
        return {"indexed": False, "reason": "not running inside a workflow"}
    return await hook("index_workflow_activity", {**ident, "status": status, "params_summary": summary, "failure": failure})


def _indexed(*results: dict) -> bool:
    return all(bool(r.get("indexed", True)) for r in results)


def _auto(p: dict) -> bool:
    # "auto" (default, unattended) or anything else ("signal" / "human"): wait for the approve_spec signal.
    return p.get("approval_mode", "auto") == "auto"


def _err(e: BaseException) -> str:
    """Type and message of an exception and its Temporal cause chain (ActivityError -> ApplicationError -> ...)."""
    parts = []; cur: BaseException | None = e
    while cur is not None and len(parts) < 4:
        parts.append(f"{type(cur).__name__}: {str(cur)[:200]}")
        cur = getattr(cur, "cause", None) or cur.__cause__
    return " <- ".join(parts)[:500]


# ------------------------------------------------------------------ shared building blocks

async def draft_and_auto_approve(set_stage, p: dict, judged: dict, iid: str) -> tuple[dict, dict]:
    """draft -> code-owned gates -> one re-draft on a withholding leak -> (draft, approved)."""
    draft = await act("draft_activity", {**p, **judged, "instance_id": iid}, 30); sid = draft["scenario_id"]
    await set_stage("auto-approving", **draft)
    approved = await act("auto_approve_activity", {"scenario_id": sid})  # approval.json says auto-validators
    if not approved["approved"] and p.get("redraft_on_leak", True) and _leak_rejection(approved):
        # Withholding leak: re-draft once with the violating reasons, never hand-edit the draft.
        await set_stage("re-drafting", **draft, reasons=approved.get("reasons", []))
        draft = await act("draft_activity", {**p, **judged, "instance_id": iid, "force": True, "reject_reasons": approved.get("reasons", [])}, 30)
        approved = await act("auto_approve_activity", {"scenario_id": sid})
    return draft, approved


async def validate_per_control(step: dict, controls: list[str], set_control=None) -> dict:
    """One BUILD activity per control, concurrently (the build queue's concurrency bounds Modal), then the verdict. An infrastructure
    failure retries one control (BUILD class) and, if it persists, fails the candidate as rerunnable instead of marking the scenario
    invalid: nothing was measured."""
    async def one(name: str) -> tuple[str, dict]:
        if set_control is not None:
            await set_control(name, "running")
        r = await act("validate_control_activity", {**step, "control": name}, CONTROL_MINUTES, retry=retries.BUILD)
        if set_control is not None:
            await set_control(name, "done" if r.get("outcome_code") is None else f"outcome {r['outcome_code']}")
        return name, r
    results = dict(await asyncio.gather(*(one(n) for n in controls)))
    verdict = await act("evaluate_controls_activity", {**step, "trial_dirs": {n: r["trial_dir"] for n, r in results.items()}}, 10, retry=retries.READ)
    return {**verdict, "runs": {n: {k: r.get(k) for k in ("outcome_code", "reward", "success", "agent_failed")} for n, r in results.items()}}


async def build_scenario(set_stage, p: dict, sid: str, spec_hash: str, *, split: bool = False, set_control=None) -> dict:
    """verify the approval hash -> compile -> validate -> register. Returns status valid | invalid with reasons. `split` runs the
    validation as one activity per control (CandidateWorkflow); the legacy whole-scenario activity remains for PipelineWorkflow."""
    accepted = await act("verify_approval_activity", {"scenario_id": sid, "spec_hash": spec_hash}, retry=retries.READ)
    step = {**p, **accepted}
    await set_stage("compiling", **accepted)
    compiled = await act("compile_activity", step, 30, retry=retries.BUILD)
    await set_stage("validating", **accepted)
    controls = list((compiled or {}).get("controls") or [])
    if split and controls:
        validation = await validate_per_control({**step, "compiled_hash": compiled.get("compiled_hash")}, controls, set_control)
    else:
        validation = await act("validate_activity", step, 180, retry=retries.BUILD)
    if validation["status"] != "valid":
        return {"scenario_id": sid, "spec_hash": accepted["spec_hash"], "status": "invalid", "reasons": list(validation.get("failures", [])),
                "validation": validation, "step": step}
    await set_stage("registering", **accepted)
    await act("register_activity", step, retry=retries.READ)
    audit = await audit_stage("audit_scenario_activity", {"scenario_id": sid, "audit_model": p.get("audit_model")})
    return {"scenario_id": sid, "spec_hash": accepted["spec_hash"], "status": "valid", "reasons": [], "validation": validation, "step": step, "audit": audit}


def _run_workflow_id() -> str | None:
    """This RunTasksWorkflow's id, carried into each trial request for the Job label `swesim-run-workflow-id`."""
    return workflow.info().workflow_id if workflow.in_workflow() else None


async def run_trials(set_trial_status, trials: list[dict], sem: asyncio.Semaphore) -> list[dict]:
    """TrialWorkflow children with REJECT_DUPLICATE, at most `sem` at a time; results keep the trial order."""
    async def one(trial: dict) -> dict:
        async with sem:
            await set_trial_status(trial, "running")
            try:
                child = {**trial, "run_workflow_id": _run_workflow_id()}
                r = await workflow.execute_child_workflow(TrialWorkflow.run, child, id=trial["trial_id"], id_reuse_policy=REJECT_DUP, **_child_options(child))
            except WorkflowAlreadyStartedError:
                r = {"trial_id": trial["trial_id"], "status": "duplicate-rejected"}
            except Exception as e:  # noqa: BLE001 - one failed child never stops its siblings
                _raise_if_cancelled(e)
                r = {"trial_id": trial["trial_id"], "status": "error", "error": _err(e)}
            await set_trial_status(trial, r.get("status", "unknown"))
            return r
    return list(await asyncio.gather(*(one(t) for t in trials)))


# ------------------------------------------------------------------ trials

@workflow.defn
class TrialWorkflow:
    def __init__(self) -> None:
        self.state: dict = {"stage": "starting"}
        self.params: dict = {}

    @workflow.query
    def progress(self) -> dict:
        return self.state

    async def _bookkeeping(self, p: dict, result: dict) -> dict:
        """After the terminal status: record the solver cost, index the trial, release the runtime (Cloud Run deletes the Job;
        the activity refuses until terminal evidence exists). None of these decides the trial's outcome."""
        tid = p.get("trial_id"); ident = _workflow_identity() or {}
        cost = await hook("record_cost_activity", {"trial_id": tid, "workflow_id": ident.get("workflow_id"), "model": p.get("model")})
        audit = None
        if result.get("status") == "completed" and p.get("condition") == "interactive":
            audit = await audit_stage("audit_transcript_activity", {"trial_id": tid, "audit_model": p.get("audit_model")})
        index = await hook("index_trial_activity", {"trial_id": tid})
        cleanup: dict
        if _patched(PATCH_HOOKS):
            try:
                cleanup = await act("cleanup_trial_activity", {"trial_id": tid}, HOOK_MINUTES, retry=retries.HOOK)
            except Exception as e:  # noqa: BLE001 - a Job left behind is quota, not evidence; the operator sees the reason
                _raise_if_cancelled(e)
                cleanup = {"cleaned": False, "reason": _err(e)}
        else:
            cleanup = {"cleaned": False, "reason": "execution predates the bookkeeping hooks"}
        return {"indexed": _indexed(index), "cost": cost, "index": index, "cleanup": cleanup, "audit": audit}

    async def _on_cancel(self, tid: str | None, launch: dict | None) -> None:
        """Workflow cancellation while the trial runs: ask the launcher to stop the runtime (Cloud Run cancels the execution, systemd
        stops the unit), shielded from the cancellation itself; then the workflow ends cancelled. The trial id stays consumed."""
        self.state = {"stage": "cancelling", "trial_id": tid, "launch": launch}
        if not _patched(PATCH_SPLIT):
            return
        try:
            self.state["cancel"] = await asyncio.shield(act("cancel_trial_activity", {"trial_id": tid}, HOOK_MINUTES, retry=retries.HOOK))
        except Exception as e:  # noqa: BLE001 - the runtime may already be gone; the operator reads the reason
            _raise_if_cancelled(e)
            self.state["cancel"] = {"cancelled": False, "reason": _err(e)}
        self.state["index"] = await asyncio.shield(hook("index_trial_activity", {"trial_id": tid}))
        await asyncio.shield(index_workflow("cancelled", params_summary(self.params)))
        self.state["stage"] = "cancelled"

    @workflow.run
    async def run(self, p: dict) -> dict:
        launch = None; tid = p.get("trial_id"); self.params = p
        self.state = {"stage": "launching", "trial_id": tid}
        await index_workflow("running", params_summary(p))
        try:
            try:
                launch = await act("launch_trial_activity", p, 5, retry=retries.ONCE)
            except Exception as e:  # noqa: BLE001 - a lost launch acknowledgement is ambiguous by design
                _raise_if_cancelled(e)
                # Poll the same unit/id; never relaunch it. The await activity reads the recorded evidence.
                launch = {"trial_id": tid, "launched": False, "error": str(e)[:500]}
            self.state = {"stage": "awaiting", "trial_id": tid, "launch": launch}
            try:
                result = await act("await_trial_activity", p, 180, retry=retries.READ)
            except Exception as e:  # noqa: BLE001 - deadline or repeated read failures: unresolved, not relaunched
                _raise_if_cancelled(e)
                result = {"trial_id": tid, "status": "unresolved", "error": str(e)[:500]}
        except asyncio.CancelledError:
            await self._on_cancel(tid, launch)
            raise
        try:
            out = {**result, "launch": launch}
            out["bookkeeping"] = await self._bookkeeping(p, out)
            self.state = {"stage": "done", "trial_id": tid, "status": out.get("status"), "launch": launch, "indexed": out["bookkeeping"]["indexed"]}
            self.state["audit"] = out["bookkeeping"]["audit"]
            await index_workflow("completed", {**params_summary(p), "status": out.get("status")})
            return out
        except asyncio.CancelledError:
            await self._on_cancel(tid, launch)
            raise


@workflow.defn
class RunMatrixWorkflow:
    @workflow.run
    async def run(self, p: dict) -> dict:
        _search_status("running")
        results = []
        # Serial pilot runs are deliberate: global launch concurrency = 1, cheap recovery reasoning.
        for trial in p["trials"]:
            try:
                r = await workflow.execute_child_workflow(TrialWorkflow.run, trial, id=trial["trial_id"], id_reuse_policy=REJECT_DUP, **_child_options(trial))
            except WorkflowAlreadyStartedError:
                r = {"trial_id": trial["trial_id"], "status": "duplicate-rejected"}
            results.append(r)
        await act("score_activity", {"scenario_id": p["scenario_id"]}, retry=retries.READ)
        await act("export_activity", {"scenario_id": p["scenario_id"]}, retry=retries.READ)
        _search_status("completed")
        return {"scenario_id": p["scenario_id"], "trials": results}


@workflow.defn
class RunTasksWorkflow:
    """Run selected scenario ids on selected models. Trials across all scenarios share one concurrency bound."""

    def __init__(self) -> None:
        self.state: dict = {"stage": "starting"}
        self.scenarios: dict[str, dict] = {}
        self.trials: dict[str, dict] = {}

    @workflow.query
    def progress(self) -> dict:
        return {"stage": self.state["stage"], "scenarios": self.scenarios, "trials": self.trials, "spend": self.state.get("spend", {}),
                "stopped_reason": self.state.get("stopped_reason"), "summary": self.state.get("summary", {}),
                "checkpoint_path": self.state.get("checkpoint_path")}

    async def _spend_exceeded(self, p: dict) -> bool:
        """`spend_check_activity` against the cap (params `spend_cap_usd`, else Settings); an unreadable ledger never stops the run
        but is visible in progress. Once exceeded, no new trials are planned and `stopped_reason` is `spend-cap`."""
        if self.state.get("stopped_reason") == "spend-cap":
            return True
        check = await hook("spend_check_activity", {"cap_usd": p.get("spend_cap_usd")})
        self.state["spend"] = check
        if check.get("exceeded"):
            self.state["stopped_reason"] = "spend-cap"
            return True
        return False

    @workflow.run
    async def run(self, p: dict) -> dict:
        p = dict(p or {})
        if _patched(PATCH_BATCH):
            return await _run_batched(self, p)
        sem = asyncio.Semaphore(max(1, int(p.get("max_parallel_trials", 1))))
        plan_keys = ("models", "conditions", "k", "purpose", "brief_profile", "stakeholder_model", "attempt_offset", "stakeholder_url")
        plan_p = {k: p[k] for k in plan_keys if p.get(k) is not None}
        results: dict[str, dict] = {}

        async def set_trial_status(trial: dict, status: str) -> None:
            self.trials[trial["trial_id"]] = {**self.trials.get(trial["trial_id"], {}), "status": status}

        async def one_scenario(sid: str) -> None:
            self.scenarios[sid] = {"status": "planning", "trials": 0}
            if await self._spend_exceeded(p):
                self.scenarios[sid] = {"status": "skipped", "reason": "spend-cap", "trials": 0}
                results[sid] = {"status": "skipped", "reason": "spend-cap", "trials": []}
                return
            try:
                planned = await act("plan_trials_activity", {**plan_p, "scenario_id": sid}, 5, retry=retries.READ)
            except Exception as e:  # noqa: BLE001 - an unplannable scenario (not validated) never stops the others
                _raise_if_cancelled(e)
                self.scenarios[sid] = {"status": "error", "error": _err(e), "trials": 0}
                results[sid] = {"status": "error", "error": _err(e), "trials": []}
                return
            trials = planned["trials"]
            for t in trials:
                self.trials[t["trial_id"]] = {"scenario_id": sid, "model": t["model"], "condition": t["condition"],
                                              "attempt_index": t["attempt_index"], "status": "planned"}
            self.scenarios[sid] = {"status": "running", "trials": len(trials)}
            trial_results = await run_trials(set_trial_status, trials, sem)
            self.scenarios[sid]["status"] = "scoring"
            try:
                await act("score_activity", {"scenario_id": sid}, retry=retries.READ)
                await act("export_activity", {"scenario_id": sid}, retry=retries.READ)
                self.scenarios[sid]["status"] = "complete"
            except Exception as e:  # noqa: BLE001
                _raise_if_cancelled(e)
                self.scenarios[sid].update(status="score-export-error", error=_err(e))
            self.scenarios[sid]["indexed"] = _indexed(await hook("index_score_activity", {"scenario_id": sid}))
            results[sid] = {"status": self.scenarios[sid]["status"], "trials": trial_results}

        self.state = {"stage": "running"}
        await index_workflow("running", params_summary(p))
        await self._spend_exceeded(p)
        await asyncio.gather(*(one_scenario(sid) for sid in p.get("scenario_ids", [])))
        self.state["stage"] = "complete"
        ok = all(r["status"] == "complete" for r in results.values()) if results else False
        out = {"status": "complete" if ok else "complete-with-failures", "scenarios": results, "trials": self.trials,
               "stopped_reason": self.state.get("stopped_reason"), "spend": self.state.get("spend", {})}
        await index_workflow("completed", {**params_summary(p), "status": out["status"], "stopped_reason": out["stopped_reason"]})
        return out


# ------------------------------------------------------------------ task creation

@workflow.defn
class CandidateWorkflow:
    """judge -> draft -> approval -> compile -> validate -> register for one candidate row.
    Reports every stage to its parent (CreateTasksWorkflow.candidate_stage) and answers `progress` itself."""

    def __init__(self) -> None:
        self.state: dict = {"stage": "starting", "refusals": []}
        self.decisions: list[dict] = []
        self.params: dict = {}

    @workflow.signal
    def approve(self, p: dict) -> None:
        """Reviewer decision in hold mode: {scenario_id, spec_hash, decision: approve|reject, note}."""
        self.decisions.append(dict(p or {}))

    @workflow.query
    def progress(self) -> dict:
        return self.state

    async def _stage(self, stage: str, **extra) -> None:
        keep = {k: v for k, v in extra.items() if k in ("scenario_id", "spec_hash", "reasons", "instance_id", "refusal")}
        self.state = {**self.state, "stage": stage, **keep}
        if _patched(PATCH_SEARCH):
            SA.upsert_status(stage)
        info = workflow.info()
        if info.parent is not None:
            payload = {"instance_id": self.state.get("instance_id"), "scenario_id": self.state.get("scenario_id"),
                       "spec_hash": self.state.get("spec_hash"), "stage": stage, "refusal": keep.get("refusal")}
            await workflow.get_external_workflow_handle(info.parent.workflow_id).signal("candidate_stage", payload)
        await self._index()  # the previous stage completed: project its files into the index

    async def _index(self, *, version: int | None = None) -> None:
        """index_candidate_activity (funnel row) and, once a scenario id exists, index_scenario_activity; never fails the candidate."""
        p = self.params
        rows = [await hook("index_candidate_activity", {"source_key": p.get("source_key"), "candidates_path": p.get("candidates_path"), "instance_id": p.get("instance_id")})]
        if self.state.get("scenario_id"):
            rows.append(await hook("index_scenario_activity", {"scenario_id": self.state["scenario_id"], "version": version or p.get("version") or 1}))
        self.state["indexed"] = _indexed(*rows)

    def _done(self, sid, status: str, reasons: list, spec_hash) -> dict:
        self.state = {**self.state, "stage": "done", "status": status, "scenario_id": sid, "spec_hash": spec_hash, "reasons": reasons}
        result = {"scenario_id": sid, "instance_id": self.state.get("instance_id"), "status": status, "reasons": reasons, "spec_hash": spec_hash}
        if "audit" in self.state:
            result["audit"] = self.state["audit"]
        return result

    async def _finish(self, result: dict, failure: str | None = None) -> dict:
        """Terminal bookkeeping: the candidate's and scenario's rows, then the workflow row; the result is returned unchanged."""
        await self._index()
        await index_workflow("failed" if failure else "completed", {**params_summary(self.params), "status": result.get("status")}, failure)
        return result

    async def _control(self, name: str, status: str) -> None:
        """Per-control validation progress (the console shows it through `progress`)."""
        self.state["controls"] = {**self.state.get("controls", {}), name: status}

    async def _hold_for_reviewer(self, p: dict, judged: dict, iid: str) -> tuple[str, dict | None, dict]:
        """Hold mode: draft, pre-check (schema/assets/leak; one re-draft on a leak), then wait for `approve`.
        Returns (sid, approved-or-None, terminal-result-if-not-approved)."""
        draft = await act("draft_activity", {**p, **judged, "instance_id": iid}, 30); sid = draft["scenario_id"]
        await self._stage("pre-checking", **draft)
        pre = await act("precheck_draft_activity", {"scenario_id": sid}, 5, retry=retries.READ)
        if not pre["ok"] and p.get("redraft_on_leak", True) and any(_is_leak_reason(r) for r in pre.get("reasons", [])):
            await self._stage("re-drafting", **draft, reasons=pre.get("reasons", []))
            await act("draft_activity", {**p, **judged, "instance_id": iid, "force": True, "reject_reasons": pre.get("reasons", [])}, 30)
            pre = await act("precheck_draft_activity", {"scenario_id": sid}, 5, retry=retries.READ)
        if not pre["ok"]:
            return sid, None, self._done(sid, "rejected", list(pre.get("reasons", [])), pre.get("spec_hash"))
        draft_hash = pre["spec_hash"]; refusal = None
        while True:
            # Every (re-)entry into the wait tells the parent the state, plus the refusal that caused a re-entry.
            await self._stage("awaiting-approval", scenario_id=sid, spec_hash=draft_hash, refusal=refusal); refusal = None
            try:
                await workflow.wait_condition(lambda: bool(self.decisions), timeout=HOLD_TIMEOUT)
            except TimeoutError:
                return sid, None, self._done(sid, "approval-timeout", ["no reviewer decision within the hold window"], draft_hash)
            d = self.decisions.pop(0)
            if d.get("scenario_id") not in (None, sid):
                refusal = f"decision for {d.get('scenario_id')} ignored by {sid}"
            elif d.get("decision") not in ("approve", "reject"):
                refusal = f"unknown decision {d.get('decision')!r} ignored"
            elif d.get("spec_hash") != draft_hash:
                # Both decisions are bound to the reviewed draft; a stale hash means the reviewer saw something else.
                refusal = f"{d.get('decision')} refused: signalled spec_hash differs from the draft on disk"
            elif d.get("decision") == "reject":
                return sid, None, self._done(sid, "rejected-by-human", [d.get("note") or "rejected by reviewer"], draft_hash)
            else:
                try:
                    # The activity re-reads the draft on disk, compares its hash to the signalled one and writes approval.json
                    # (approved_by human-command, review_needed false). A mismatch is refused and the candidate keeps waiting.
                    approved = await act("human_approve_activity", {"scenario_id": sid, "spec_hash": d.get("spec_hash"), "note": d.get("note")}, 5,
                                         retry=retries.READ)
                    return sid, approved, {}
                except Exception as e:
                    _raise_if_cancelled(e)
                    if not _refusable(e):  # cancellation or infrastructure failure: propagate, never re-enter the wait
                        raise
                    refusal = f"approve refused: {_err(e)}"
            self.state["refusals"] = [*self.state.get("refusals", []), refusal]

    @workflow.run
    async def run(self, p: dict) -> dict:
        p = dict(p or {}); iid = p["instance_id"]; sid = None; self.params = p
        self.state = {**self.state, "instance_id": iid}
        if p.get("approval_mode", "auto") not in CREATE_APPROVAL_MODES:
            raise ApplicationError(f"approval_mode must be one of {sorted(CREATE_APPROVAL_MODES)}", non_retryable=True)
        await index_workflow("running", params_summary(p))
        try:
            await self._stage("judging", instance_id=iid)
            judged = await act("judge_one_activity", p, 30); sid = judged.get("scenario_id")
            if not judged.get("keep"):
                return await self._finish(self._done(sid, "rejected", list(judged.get("reasons") or ["judge did not keep the candidate"]), None))
            # Measure FAIL_TO_PASS / PASS_TO_PASS on base and gold (Harbor on Modal) for every source kind: SWE-bench's
            # published lists do not always reproduce offline. One attempt; harbor_cli reconciles an existing job.
            await self._stage("probing", scenario_id=sid)
            probed = await act("probe_candidate_activity", {"judged_path": judged["judged_path"], "instance_id": iid, "scenario_id": sid,
                                                            "max_builds": p.get("max_builds", 1)}, 160, retry=retries.BUILD)  # probe_tests' Harbor deadline is 150 min
            if not probed.get("fail_to_pass"):
                return await self._finish(self._done(sid, "not-reproducible-offline", [probed.get("reason") or "measured FAIL_TO_PASS is empty: no test fails on base and passes on gold"], None))
            judged = {**judged, "judged_path": probed["judged_path"]}  # the probed row (measured lists) is what draft reads
            if p.get("rebuild") and p.get("previous_version"):
                # Rebuild as version p["version"]: the previous version's spec/compile/validation/approval/judge/draft-meta files
                # move to archive/v<previous>/ so draft (with version=next), approve, compile, validate and register run afresh.
                # Judged rows and probe outputs are retained. (The force/reject_reasons re-draft path is unchanged.)
                await self._stage("archiving", scenario_id=sid)
                await act("archive_scenario_activity", {"scenario_id": sid, "previous_version": p["previous_version"]}, 5, retry=retries.READ)
            if p.get("approval_mode", "auto") == "hold":
                sid, approved, terminal = await self._hold_for_reviewer(p, judged, iid)
                if approved is None:
                    return await self._finish(terminal)
            else:
                draft, approved = await draft_and_auto_approve(self._stage, p, judged, iid); sid = draft["scenario_id"]
                if not approved["approved"]:
                    return await self._finish(self._done(sid, "rejected", list(approved.get("reasons", [])), None))
            built = await build_scenario(self._stage, p, sid, approved["spec_hash"], split=_patched(PATCH_SPLIT), set_control=self._control)
            if built.get("audit") is not None:
                self.state["audit"] = built["audit"]
            return await self._finish(self._done(sid, built["status"], built["reasons"], built["spec_hash"]))
        except Exception as e:
            _raise_if_cancelled(e)
            # Infrastructure failure (LLM, Harbor, a lost parent): FAIL the child so `create:<sid>` can be started again
            # (ALLOW_DUPLICATE_FAILED_ONLY); the parent records `error` from the cause and siblings are unaffected.
            stage = self.state.get("stage")
            failure = f"candidate {iid} failed at stage {stage}: {_err(e)}"
            await self._finish(self._done(sid, "error", [f"{stage}: {_err(e)}"], None), failure)
            raise ApplicationError(failure, non_retryable=True) from e


def _mine_params(src: dict) -> dict:
    if src.get("kind") == "github-repo":
        out = {"source": "github-repo", "repo": src["repo"], "since": src.get("since"), "max_candidates": src.get("max_candidates"), "pr": src.get("pr"),
               "kind": src.get("pr_kind")}  # discovery band; github_repo.discover defaults to "feature" when absent
    else:
        out = {"source": "swebench-annotated", "pool": src.get("pool", "A"), "instance_ids": src.get("instance_ids"), "limit": src.get("limit")}
    return {k: v for k, v in out.items() if v is not None}


def _normalize_repo(repo: str) -> str:
    """`https://github.com/X/A/`, `x/a.git`, `git@github.com:x/a` -> `x/a` (the same spellings mine merges into one output file)."""
    r = str(repo).strip().casefold()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:", "github.com/"):
        r = r.removeprefix(prefix)
    r = r.rstrip("/")
    r = r.removesuffix(".git")
    return r.rstrip("/")


def candidate_child_id(sid: str, version=None) -> str:
    """`create:<sid>` for a first build; `create:<sid>@v<n>` when an existing scenario is rebuilt as version n (n >= 2)."""
    return f"create:{sid}" if not version or int(version) <= 1 else f"create:{sid}@v{int(version)}"


def _source_key(src: dict, *, include_band: bool = False) -> str:
    kind = src.get("kind", "swebench-annotated")
    if kind == "frozen-candidate":
        return src["source_key"]
    if kind == "github-repo":
        if include_band:
            return f"github-repo--{_normalize_repo(src.get('repo') or '').replace('/', '__')}--{src.get('pr_kind') or 'feature'}"
        return f"{kind}:{_normalize_repo(src.get('repo') or '')}"
    return f"{kind}:{str(src.get('pool', 'A')).strip().casefold()}"


@workflow.defn
class CreateTasksWorkflow:
    """Mine each source, then one CandidateWorkflow per candidate (id create:<scenario_id>), at most `max_parallel` at a time."""

    def __init__(self) -> None:
        self.state: dict = {"stage": "starting", "sources": {}, "probe": {}}
        self.candidates: dict[str, dict] = {}
        self.counts: dict[str, int] = {"candidates": 0, "valid": 0, "invalid": 0, "rejected": 0, "error": 0, "duplicate-rejected": 0, "duplicate-in-batch": 0}

    @workflow.signal
    def candidate_stage(self, p: dict) -> None:
        c = self.candidates.get(p.get("scenario_id") or "")
        if c is None:
            c = next((x for x in self.candidates.values() if x.get("instance_id") == p.get("instance_id")), None)
        if c is None:
            return
        c["stage"] = p.get("stage"); c["stages"] = [*c.get("stages", []), p.get("stage")]
        if p.get("spec_hash"):
            c["spec_hash"] = p["spec_hash"]
        # Status follows the child's latest stage: in the review queue only while it waits; "running" otherwise.
        c["status"] = "awaiting-approval" if p.get("stage") == "awaiting-approval" else "running"
        if p.get("refusal"):
            c["refusals"] = [*c.get("refusals", []), p["refusal"]]

    @workflow.query
    def progress(self) -> dict:
        return {"stage": self.state["stage"], "counts": self.counts, "sources": self.state.get("sources", {}), "rebuild": self.state.get("rebuild", False),
                "probe": self.state.get("probe", {}), "candidates": self.candidates, "checkpoint_path": self.state.get("checkpoint_path"),
                "awaiting_approval": [{"scenario_id": sid, "spec_hash": c.get("spec_hash"), "candidate_workflow_id": c.get("candidate_workflow_id")}
                                      for sid, c in self.candidates.items() if c.get("status") == "awaiting-approval"]}

    @workflow.run
    async def run(self, p: dict) -> dict:
        p = dict(p or {})
        mode = p.get("approval_mode", "auto")
        if mode not in CREATE_APPROVAL_MODES:
            raise ApplicationError(f"approval_mode must be one of {sorted(CREATE_APPROVAL_MODES)}", non_retryable=True)
        # Mine outputs are one file per pool/repo, so two sources with the same key in one batch would race on that
        # file while mining concurrently. Guard: duplicate keys are rejected before any activity runs.
        keys = [_source_key(src, include_band=_patched(PATCH_BATCH)) for src in p.get("sources", [])]
        dups = sorted({k for k in keys if keys.count(k) > 1})
        if dups:
            raise ApplicationError(f"duplicate sources in one batch: {dups}; merge them into one source or run separate batches", non_retryable=True)
        if not keys:
            raise ApplicationError("sources is empty", non_retryable=True)
        if _patched(PATCH_BATCH):
            return await _create_batched(self, p)
        sem = asyncio.Semaphore(max(1, int(p.get("max_parallel", 8))))
        rebuild = bool(p.get("rebuild"))
        self.state = {"stage": "probing-models", "sources": {}, "probe": {}, "rebuild": rebuild}
        await index_workflow("running", params_summary(p))
        probe = await act("probe_models_activity", p, 5)
        p = {**p, **(probe.get("substitutions") or {}), **(probe.get("resolved") or {})}
        self.state["probe"] = {"resolved": probe.get("resolved") or {}, "substitutions": probe.get("substitutions") or {},
                               "notes": probe.get("notes") or [], "needs_human": probe.get("needs_human") or []}
        self.state["stage"] = "mining"
        results: dict[str, dict] = {}

        async def one_candidate(sid: str, cand: dict) -> None:
            async with sem:
                self.candidates[sid].update(status="running", stage="starting")
                child = {"candidates_path": cand["candidates_path"], "instance_id": cand["instance_id"], "judge_model": p.get("judge_model"),
                         "draft_model": p.get("draft_model"), "stakeholder_model": p.get("stakeholder_model"), "stakeholder_url": p.get("stakeholder_url"),
                         "approval_mode": mode, "redraft_on_leak": p.get("redraft_on_leak", True), "version": 1, "source_key": self.candidates[sid].get("source")}
                child_id = candidate_child_id(sid)
                if rebuild:
                    # Rebuild: an existing scenario (scenario.json or registry entry) is re-created as version n+1 under a new
                    # child id; nothing existing behaves like a first build (version 1, plain id).
                    try:
                        ver = await act("scenario_version_activity", {"scenario_id": sid}, 5, retry=retries.READ)
                    except Exception as e:  # noqa: BLE001 - one unreadable scenario never stops its siblings
                        _raise_if_cancelled(e)
                        r = {"scenario_id": sid, "instance_id": cand["instance_id"], "status": "error", "reasons": [_err(e)], "spec_hash": None}
                        self.candidates[sid].update(status="error", stage="done", reasons=r["reasons"]); self.counts["error"] = self.counts.get("error", 0) + 1
                        results[sid] = r
                        return
                    if ver.get("exists"):
                        child.update(rebuild=True, version=ver["next"], previous_version=ver["version"])
                        child_id = candidate_child_id(sid, ver["next"])
                        self.candidates[sid].update(version=ver["next"], previous_version=ver["version"], candidate_workflow_id=child_id)
                try:
                    r = await workflow.execute_child_workflow(CandidateWorkflow.run, child, id=child_id, id_reuse_policy=DUP_FAILED_ONLY, **_child_options({**child, "scenario_id": sid}))
                except WorkflowAlreadyStartedError:
                    r = {"scenario_id": sid, "instance_id": cand["instance_id"], "status": "duplicate-rejected", "spec_hash": None,
                         "reasons": ["a CandidateWorkflow with this id already completed; read its result instead"]}
                except Exception as e:  # noqa: BLE001 - one failed child never stops its siblings; a failed id may be started again
                    _raise_if_cancelled(e)
                    r = {"scenario_id": sid, "instance_id": cand["instance_id"], "status": "error", "reasons": [_err(e)], "spec_hash": None}
                self.candidates[sid].update(status=r["status"], stage="done", spec_hash=r.get("spec_hash"), reasons=r.get("reasons", []))
                self.counts[r["status"]] = self.counts.get(r["status"], 0) + 1
                results[sid] = r

        async def one_source(src: dict) -> None:
            key = _source_key(src)
            self.state["sources"][key] = {"status": "mining"}
            try:
                if src.get("kind") == "github-repo" and _patched(PATCH_SPLIT):
                    # Discovery only (GitHub API); every candidate is probed in its own CandidateWorkflow (probe_candidate_activity).
                    mined = await act("discover_activity", {**p, **_mine_params(src)}, 30)
                    self.state["sources"][key].update(discovered=mined.get("discovered"), rejects=mined.get("rejects", {}))
                else:
                    mined = await act("mine_activity", {**p, **_mine_params(src)}, 180)
                listed = await act("list_candidates_activity", {"candidates_path": mined["candidates_path"],
                                                                "max_candidates": src.get("max_candidates") or src.get("limit")}, 5, retry=retries.READ)
            except Exception as e:  # noqa: BLE001 - a failed source never stops the others
                _raise_if_cancelled(e)
                self.state["sources"][key] = {"status": "error", "error": _err(e)}
                return
            self.state["sources"][key] = {**self.state["sources"][key], "status": "mined", "candidates": len(listed["candidates"]), "candidates_path": mined["candidates_path"]}
            self.state["stage"] = "creating"
            tasks = []
            for cand in listed["candidates"]:
                sid = cand["scenario_id"]
                if sid in self.candidates:  # the same candidate from two sources in one batch: keep the first
                    self.counts["duplicate-in-batch"] += 1
                    continue
                self.counts["candidates"] += 1
                self.candidates[sid] = {"instance_id": cand["instance_id"], "source": key, "status": "queued", "stage": "queued", "stages": [],
                                        "candidate_workflow_id": f"create:{sid}", "spec_hash": None}
                tasks.append(one_candidate(sid, {**cand, "candidates_path": mined["candidates_path"]}))
            await asyncio.gather(*tasks)

        await asyncio.gather(*(one_source(src) for src in p.get("sources", [])))
        self.state["stage"] = "complete"
        status = "complete" if self.counts.get("valid") else "complete-with-failures"
        await index_workflow("completed", {**params_summary(p), "status": status, "counts": self.counts})
        return {"status": status, "counts": self.counts, "candidates": results, "sources": self.state["sources"], "probe": self.state["probe"]}


# ------------------------------------------------------------------ the original single-candidate composition

@workflow.defn
class PipelineWorkflow:
    def __init__(self) -> None:
        self.approvals: dict[str, str] = {}
        self.state: dict = {"stage": "starting"}

    @workflow.signal
    def approve_spec(self, p: dict) -> None:
        self.approvals[p["scenario_id"]] = p["spec_hash"]

    @workflow.query
    def progress(self) -> dict:
        return self.state

    async def _set_stage(self, stage: str, **extra) -> None:
        if _patched(PATCH_SEARCH):
            SA.upsert_status(stage)
        self.state = {"stage": stage, **extra}

    @workflow.run
    async def run(self, p: dict) -> dict:
        _search_status("running")
        p = dict(p or {})
        if p.get("approval_mode", "auto") not in APPROVAL_MODES:
            raise ApplicationError(f"approval_mode must be one of {sorted(APPROVAL_MODES)}", non_retryable=True)
        self.state = {"stage": "probing-models"}
        probe = await act("probe_models_activity", p, 5)
        # Resolved judge/draft/stakeholder ids flow to every later activity (judge, draft, plan_trials -> RunConfig.stakeholder).
        p = {**p, **(probe.get("substitutions") or {}), **(probe.get("resolved") or {})}
        self.state = {"stage": "mining", "resolved": probe.get("resolved") or {}, "substitutions": probe.get("substitutions") or {},
                      "notes": probe.get("notes") or [], "needs_human": probe.get("needs_human") or []}
        mined = await act("mine_activity", p, 180)
        self.state = {"stage": "judging", **mined}
        judged = await act("judge_activity", {**p, **mined}, 60)
        out: list[dict] = []
        counts = {"tried": 0, "completed": 0, "rejected": 0, "invalid": 0}
        if not judged["instance_ids"]:
            self.state = {"stage": "no-valid-candidate", **counts}
            _search_status("completed")
            return {"status": "no-valid-candidate", "scenarios": [], **counts}
        max_scenarios = p.get("max_scenarios", 1); max_tried = p.get("max_candidates_tried", 3)
        solver_unavailable = list(probe.get("solver_unavailable") or [])
        for iid in judged["instance_ids"]:
            if counts["completed"] >= max_scenarios or counts["tried"] >= max_tried:
                break
            counts["tried"] += 1
            if _auto(p):
                draft, approved = await draft_and_auto_approve(self._set_stage, p, judged, iid); sid = draft["scenario_id"]
                if not approved["approved"]:
                    out.append({"scenario_id": sid, "status": "auto-approval-rejected", "reasons": approved.get("reasons", [])})
                    counts["rejected"] += 1
                    continue
                self.approvals[sid] = approved["spec_hash"]
            else:
                draft = await act("draft_activity", {**p, **judged, "instance_id": iid}, 30); sid = draft["scenario_id"]
                self.state = {"stage": "awaiting-approval", **draft}
                try:
                    # No Boolean is read from wait_condition: the signal payload and approval.json carry the truth.
                    await workflow.wait_condition(lambda sid=sid: sid in self.approvals, timeout=APPROVAL_TIMEOUT)
                except TimeoutError:
                    _search_status("completed")
                    return {"status": "awaiting-approval-timeout", "scenarios": out, "pending": draft, **counts}
            built = await build_scenario(self._set_stage, p, sid, self.approvals[sid])
            if built["status"] != "valid":
                out.append({"scenario_id": sid, **built["validation"]})
                counts["invalid"] += 1
                continue
            step = built["step"]; accepted = {"scenario_id": sid, "spec_hash": built["spec_hash"]}
            if solver_unavailable:
                # The probe found the solver unavailable and nothing may substitute it silently: registered, no trial launched.
                out.append({"scenario_id": sid, "status": "complete-no-trials", "solver_unavailable": solver_unavailable,
                            "needs_human": probe.get("needs_human") or []})
                counts["completed"] += 1
                continue
            trials = await act("plan_trials_activity", step, retry=retries.READ); results = []
            for trial in trials["trials"]:
                self.state = {"stage": "trial", **accepted, "trial_id": trial["trial_id"]}
                try:
                    r = await workflow.execute_child_workflow(TrialWorkflow.run, trial, id=trial["trial_id"], id_reuse_policy=REJECT_DUP, **_child_options(trial))
                except WorkflowAlreadyStartedError:
                    r = {"trial_id": trial["trial_id"], "status": "duplicate-rejected"}
                results.append(r)
            self.state = {"stage": "scoring", **accepted}
            await act("score_activity", step, retry=retries.READ)
            await act("export_activity", step, retry=retries.READ)
            out.append({"scenario_id": sid, "status": "complete", "trials": results})
            counts["completed"] += 1
        status = "complete" if counts["completed"] else "complete-with-failures"
        self.state = {"stage": status, **counts}
        _search_status("completed")
        return {"status": status, "scenarios": out, **counts}


async def _create_candidate(self, p, mode, rebuild, sem, results, sid: str, cand: dict) -> None:
    async with sem:
        self.candidates[sid].update(status="running", stage="starting")
        child = {"candidates_path": cand["candidates_path"], "instance_id": cand["instance_id"], "judge_model": p.get("judge_model"),
                 "draft_model": p.get("draft_model"), "audit_model": p.get("audit_model"), "stakeholder_model": p.get("stakeholder_model"), "stakeholder_url": p.get("stakeholder_url"),
                 "approval_mode": mode, "redraft_on_leak": p.get("redraft_on_leak", True), "version": 1, "source_key": self.candidates[sid].get("source")}
        child_id = candidate_child_id(sid)
        if rebuild:
            # Rebuild: an existing scenario (scenario.json or registry entry) is re-created as version n+1 under a new
            # child id; nothing existing behaves like a first build (version 1, plain id).
            try:
                ver = await act("scenario_version_activity", {"scenario_id": sid}, 5, retry=retries.READ)
            except Exception as e:  # noqa: BLE001 - one unreadable scenario never stops its siblings
                _raise_if_cancelled(e)
                r = {"scenario_id": sid, "instance_id": cand["instance_id"], "status": "error", "reasons": [_err(e)], "spec_hash": None}
                self.candidates[sid].update(status="error", stage="done", reasons=r["reasons"]); self.counts["error"] = self.counts.get("error", 0) + 1
                results[sid] = r
                return
            if ver.get("exists"):
                child.update(rebuild=True, version=ver["next"], previous_version=ver["version"])
                child_id = candidate_child_id(sid, ver["next"])
                self.candidates[sid].update(version=ver["next"], previous_version=ver["version"], candidate_workflow_id=child_id)
        try:
            r = await workflow.execute_child_workflow(CandidateWorkflow.run, child, id=child_id, id_reuse_policy=DUP_FAILED_ONLY, **_child_options({**child, "scenario_id": sid}))
        except WorkflowAlreadyStartedError:
            r = {"scenario_id": sid, "instance_id": cand["instance_id"], "status": "duplicate-rejected", "spec_hash": None,
                 "reasons": ["a CandidateWorkflow with this id already completed; read its result instead"]}
        except Exception as e:  # noqa: BLE001 - one failed child never stops its siblings; a failed id may be started again
            _raise_if_cancelled(e)
            r = {"scenario_id": sid, "instance_id": cand["instance_id"], "status": "error", "reasons": [_err(e)], "spec_hash": None}
        self.candidates[sid].update(status=r["status"], stage="done", spec_hash=r.get("spec_hash"), reasons=r.get("reasons", []))
        if r.get("audit") is not None:
            self.candidates[sid]["audit"] = r["audit"]
        self.counts[r["status"]] = self.counts.get(r["status"], 0) + 1
        results[sid] = r



async def _checkpoint(p: dict, state: dict) -> str:
    ident = _workflow_identity() or {'workflow_id': 'direct', 'run_id': 'direct'}
    result = await act('checkpoint_workflow_activity', {**ident, 'state': state}, retry=retries.READ)
    return result['checkpoint_path']


async def _continue(p: dict, continuation: dict, state: dict) -> None:
    # All children have been awaited. Candidate stage signals are synchronous, so no handlers remain in flight.
    path = await _checkpoint(p, {**state, 'previous_checkpoint': continuation.get('previous_checkpoint')})
    continuation = {**continuation, 'previous_checkpoint': path}
    await index_workflow('continued_as_new', {**params_summary(p), 'checkpoint_path': path})
    next_params = {**p, '_continuation': continuation}
    check_payload(next_params, 'continue-as-new input')
    workflow.continue_as_new(next_params)


async def _create_batched(self: CreateTasksWorkflow, p: dict) -> dict:
    resume = p.get('_continuation') or {}
    mode = p.get('approval_mode', 'auto'); rebuild = bool(p.get('rebuild'))
    sem = asyncio.Semaphore(max(1, int(p.get('max_parallel', 8))))
    self.state = {'stage': 'starting', 'sources': resume.get('sources', {}), 'probe': resume.get('probe', {}), 'rebuild': rebuild,
                  'checkpoint_path': resume.get('previous_checkpoint')}
    await index_workflow('running', params_summary(p))
    if resume:
        self.counts = dict(resume['counts']); prepared = resume['plan']
    else:
        self.state['stage'] = 'probing-models'
        probe = await act('probe_models_activity', p, 5)
        p = {**p, **(probe.get('substitutions') or {}), **(probe.get('resolved') or {})}
        self.state['probe'] = {k: probe.get(k) or ({} if k in ('resolved', 'substitutions') else [])
                               for k in ('resolved', 'substitutions', 'notes', 'needs_human')}
        sources = []
        # Discovery is sequential per repository; children begin from a frozen, deduplicated candidate plan.
        for src in p['sources']:
            key = _source_key(src, include_band=True)
            self.state['sources'][key] = {'status': 'mining'}
            try:
                name = 'discover_activity' if src.get('kind') == 'github-repo' else 'mine_activity'
                if src.get('kind') == 'frozen-candidate' and _patched(PATCH_FROZEN_RETRY):
                    name = 'read_candidate_source_activity'
                    mined = await act(name, src, 5, retry=retries.READ)
                else:
                    mined = await act(name, {**p, **_mine_params(src)}, 30 if name == 'discover_activity' else 180)
                source_key = mined.get('source_key') or mined['candidates_path'].rsplit('/', 1)[-1].removesuffix('.jsonl')
                sources.append({'source_key': source_key, 'candidates_path': mined['candidates_path'], 'limit': src.get('max_candidates') or src.get('limit')})
                self.state['sources'][key] = {'status': 'mined', 'candidates_path': mined['candidates_path'],
                                              'candidates': mined.get('written'), 'discovered': mined.get('discovered'), 'rejects': mined.get('rejects', {})}
                if name == 'discover_activity':
                    self.state['sources'][key]['index'] = await hook('index_discovery_activity', {
                        **(_workflow_identity() or {}), 'source_key': mined.get('source_key') or key,
                        'artifact_sha256': mined.get('rejects_sha256'),
                        'artifact_path': mined.get('rejects_path'),
                        'repo': mined.get('repo') or _normalize_repo(src['repo']), 'band': mined.get('band') or src.get('pr_kind') or 'feature'})
            except Exception as e:  # noqa: BLE001 - isolate a failed stage; cancellation propagates
                _raise_if_cancelled(e)
                self.state['sources'][key] = {'status': 'error', 'error': _err(e)}
        prepared = await act('prepare_candidate_plan_activity', {'workflow_id': _run_workflow_id() or 'direct', 'sources': sources}, retry=retries.READ)
        self.counts['candidates'] = prepared['total']; self.counts['duplicate-in-batch'] = prepared.get('duplicates', 0)
    page = await act('read_plan_page_activity', {'plan_path': prepared['plan_path'], 'offset': resume.get('offset', 0), 'limit': CHILDREN_PER_RUN}, retry=retries.READ)
    self.state['stage'] = 'creating'; results = {}
    for cand in page['items']:
        sid = cand['scenario_id']
        self.candidates[sid] = {'instance_id': cand['instance_id'], 'source': cand['source_key'], 'status': 'queued', 'stage': 'queued',
                                'stages': [], 'candidate_workflow_id': candidate_child_id(sid), 'spec_hash': None}
    await asyncio.gather(*(_create_candidate(self, p, mode, rebuild, sem, results, c['scenario_id'], c) for c in page['items']))
    checkpoint_state = {'counts': self.counts, 'candidates': self.candidates, 'results': results, 'plan_path': prepared['plan_path'], 'offset': page['next_offset']}
    if page['next_offset'] < page['total']:
        await _continue(p, {'plan': prepared, 'offset': page['next_offset'], 'counts': self.counts, 'sources': self.state['sources'],
                            'probe': self.state['probe'], 'previous_checkpoint': resume.get('previous_checkpoint')}, checkpoint_state)
    self.state['stage'] = 'complete'
    status = 'complete' if self.counts.get('valid') else 'complete-with-failures'
    path = await _checkpoint(p, {**checkpoint_state, 'previous_checkpoint': resume.get('previous_checkpoint')})
    self.state['checkpoint_path'] = path
    await index_workflow('completed', {**params_summary(p), 'status': status, 'counts': self.counts})
    return {'status': status, 'counts': self.counts, 'candidates': results, 'sources': self.state['sources'], 'probe': self.state['probe'], 'checkpoint_path': path}


async def _run_batched(self: RunTasksWorkflow, p: dict) -> dict:
    resume = p.get('_continuation') or {}
    if not resume:
        if _patched(PATCH_FRONTIER):
            admission = await act('frontier_gate_activity', p, 5, retry=retries.READ)
            if not admission['allowed']:
                self.state = {'stage': 'complete', 'stopped_reason': 'frontier-gate', 'admission': admission}
                await index_workflow('completed', {**params_summary(p), 'stopped_reason': 'frontier-gate'})
                return {'status': 'blocked', 'scenarios': {}, 'trials': {}, 'admission': admission}
            p = {**p, 'scenario_ids': admission.get('scenario_ids', p.get('scenario_ids', []))}
        self.state = {'stage': 'probing-models'}
        probe = await act('probe_models_activity', p, 5)
        p = {**p, **(probe.get('substitutions') or {}), **(probe.get('resolved') or {})}
        if probe.get('solver_unavailable'):
            self.state = {'stage': 'complete', 'stopped_reason': 'model-unavailable', 'probe': probe}
            await index_workflow('completed', {**params_summary(p), 'stopped_reason': 'model-unavailable'})
            return {'status': 'complete-with-failures', 'scenarios': {}, 'trials': {}, 'stopped_reason': 'model-unavailable', 'probe': probe}
    sem = asyncio.Semaphore(max(1, int(p.get('max_parallel_trials', 1))))
    keys = ('models', 'conditions', 'k', 'purpose', 'brief_profile', 'stakeholder_model', 'audit_model', 'attempt_offset', 'stakeholder_url')
    plan_p = {k: p[k] for k in keys if p.get(k) is not None}
    results = {}; children = 0; summary = dict(resume.get('summary') or {'children': 0, 'scenarios': 0, 'failed_scenarios': 0})
    self.state = {'stage': 'running', 'summary': summary, 'checkpoint_path': resume.get('previous_checkpoint')}
    await index_workflow('running', params_summary(p))
    await self._spend_exceeded(p)
    async def set_status(trial, status):
        self.trials[trial['trial_id']] = {**self.trials.get(trial['trial_id'], {}), 'status': status}
    sids = p.get('scenario_ids', [])
    start = int(resume.get('scenario_index', 0))
    for index in range(start, len(sids)):
        sid = sids[index]
        if await self._spend_exceeded(p):
            self.scenarios[sid] = {'status': 'skipped', 'reason': 'spend-cap', 'trials': 0}
            results[sid] = {**self.scenarios[sid], 'trials': []}; summary['failed_scenarios'] += 1
            continue
        self.scenarios[sid] = {'status': 'planning', 'trials': 0}
        try:
            prepared = resume.get('plan') if index == start else None
            prepared = prepared or await act('prepare_trial_plan_activity', {**plan_p, 'scenario_id': sid, 'workflow_id': _run_workflow_id() or 'direct'}, 5, retry=retries.READ)
        except Exception as e:  # noqa: BLE001 - isolate a failed stage; cancellation propagates
            _raise_if_cancelled(e)
            self.scenarios[sid] = {'status': 'error', 'error': _err(e), 'trials': 0}
            results[sid] = {**self.scenarios[sid], 'trials': []}; summary['failed_scenarios'] += 1
            continue
        offset = int(resume.get('offset', 0)) if index == start else 0
        page = await act('read_plan_page_activity', {'plan_path': prepared['plan_path'], 'offset': offset, 'limit': CHILDREN_PER_RUN - children}, retry=retries.READ)
        trials = page['items']
        for t in trials:
            self.trials[t['trial_id']] = {k: t[k] for k in ('scenario_id', 'model', 'condition', 'attempt_index')}
            self.trials[t['trial_id']]['status'] = 'planned'
        self.scenarios[sid] = {'status': 'running', 'trials': prepared['total']}
        raw = await run_trials(set_status, trials, sem)
        compact = [{k: r[k] for k in ('trial_id', 'status', 'reward', 'error') if k in r} for r in raw]
        results[sid] = {'status': 'running', 'trials': compact}
        children += len(trials); summary['children'] += len(trials)
        more_trials = page['next_offset'] < page['total']
        if not more_trials:
            self.scenarios[sid]['status'] = 'scoring'
            try:
                await act('score_activity', {'scenario_id': sid, **({'k': int(p.get('metric_k') or (1 if p.get('purpose') == 'screen' else p.get('k', 1)))} if _patched(PATCH_SCORE_K) else {})}, retry=retries.READ)
                await act('export_activity', {'scenario_id': sid}, retry=retries.READ)
                if p.get('purpose') == 'screen' and _patched(PATCH_DIFFICULTY):
                    self.scenarios[sid]['difficulty'] = await act('difficulty_label_activity', {'scenario_id': sid, 'plan_path': prepared['plan_path']}, retry=retries.READ)
                    self.scenarios[sid]['scenario_index'] = await hook('index_scenario_activity', {'scenario_id': sid})
                self.scenarios[sid]['status'] = 'complete'
            except Exception as e:  # noqa: BLE001 - isolate a failed stage; cancellation propagates
                _raise_if_cancelled(e)
                self.scenarios[sid].update(status='score-export-error', error=_err(e)); summary['failed_scenarios'] += 1
            self.scenarios[sid]['indexed'] = _indexed(await hook('index_score_activity', {'scenario_id': sid}))
            summary['scenarios'] += 1
            results[sid]['status'] = self.scenarios[sid]['status']
        if children == CHILDREN_PER_RUN and (more_trials or index + 1 < len(sids)):
            await _continue(p, {'scenario_index': index if more_trials else index + 1, 'offset': page['next_offset'] if more_trials else 0,
                                'plan': prepared if more_trials else None, 'summary': summary, 'previous_checkpoint': resume.get('previous_checkpoint')},
                            {'scenarios': self.scenarios, 'trials': self.trials, 'results': results, 'summary': summary})
    self.state['stage'] = 'complete'
    ok = bool(summary['scenarios']) and not summary['failed_scenarios']
    out = {'status': 'complete' if ok else 'complete-with-failures', 'scenarios': results, 'trials': self.trials, 'summary': summary,
           'stopped_reason': self.state.get('stopped_reason'), 'spend': self.state.get('spend', {})}
    out['checkpoint_path'] = await _checkpoint(p, {**out, 'previous_checkpoint': resume.get('previous_checkpoint')})
    self.state['checkpoint_path'] = out['checkpoint_path']
    await index_workflow('completed', {**params_summary(p), 'status': out['status'], 'stopped_reason': out['stopped_reason']})
    return out


def _child_options(p: dict) -> dict:
    return {'search_attributes': SA.for_params(p)} if _patched(PATCH_SEARCH) else {}


def _raise_if_cancelled(error: BaseException) -> None:
    if _patched(PATCH_SPLIT) and is_cancelled_exception(error):
        raise asyncio.CancelledError() from error


async def audit_stage(name: str, p: dict) -> dict | None:
    """Audit after grading/registration; an outage leaves a visible pending audit and the measured reward unchanged."""
    if not _patched(PATCH_AUDIT):
        return None
    try:
        result = await act(name, p, 30, retry=retries.AUDIT)
    except Exception as error:  # noqa: BLE001 - keep measurements and report missing audit evidence
        _raise_if_cancelled(error)
        return {'audited': False, 'status': 'pending', 'reason': _err(error)}
    return {**result, 'audited': True, 'status': 'completed'}


def _search_status(status: str) -> None:
    if _patched(PATCH_SEARCH):
        SA.upsert_status(status)
