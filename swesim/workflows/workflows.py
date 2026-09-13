"""Temporal workflows (thin). Payloads carry ids, paths, hashes and model ids; files stay in the workspace.

TrialWorkflow id == trial_id(spec_hash, condition, run_config_hash, attempt_index); children start with
REJECT_DUPLICATE, so a trial cannot be started twice by a retry of a parent. Launch is never retried;
polling is. Full VM loss surfaces as an explicitly unresolved attempt, never a fresh solver launch.
"""
from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

ONE = RetryPolicy(maximum_attempts=1)
READ = RetryPolicy(maximum_attempts=3)
APPROVAL_TIMEOUT = timedelta(hours=24)
APPROVAL_MODES = {"auto", "signal", "human"}  # "human" is the brief's older spelling of "signal"
# Re-draft-once trigger. Structured: approve_scenario may return {"gate": "withholding-leak"}. Fallback on reason text:
# a reason starting with "withholding", or containing "leak" or "withheld" (the approve stage's "withheld brief still
# contains excerpt of <fact>"). Anything else (judge, load-bearing, controls, assets) moves to the next candidate.
LEAK_GATE = "withholding-leak"


def _is_leak_reason(reason) -> bool:
    r = str(reason).strip().lower()
    return r.startswith("withholding") or "leak" in r or "withheld" in r


def _leak_rejection(approved: dict) -> bool:
    return approved.get("gate") == LEAK_GATE or any(_is_leak_reason(r) for r in approved.get("reasons", []) or [])


async def act(name: str, p: dict, minutes: int = 10):
    return await workflow.execute_activity(name, p, start_to_close_timeout=timedelta(minutes=minutes),
                                           heartbeat_timeout=timedelta(minutes=2), retry_policy=ONE)


def _auto(p: dict) -> bool:
    # "auto" (default, unattended) or anything else ("signal" / "human"): wait for the approve_spec signal.
    return p.get("approval_mode", "auto") == "auto"


@workflow.defn
class TrialWorkflow:
    @workflow.run
    async def run(self, p: dict) -> dict:
        launch = None
        try:
            launch = await act("launch_trial_activity", p, 5)
        except Exception as e:  # noqa: BLE001 - a lost launch acknowledgement is ambiguous by design
            # Poll the same unit/id; never relaunch it. The await activity reads the recorded evidence.
            launch = {"trial_id": p.get("trial_id"), "launched": False, "error": str(e)[:500]}
        try:
            result = await workflow.execute_activity("await_trial_activity", p, start_to_close_timeout=timedelta(hours=3),
                                                     heartbeat_timeout=timedelta(minutes=2), retry_policy=READ)
        except Exception as e:  # noqa: BLE001 - deadline or repeated read failures: unresolved, not relaunched
            return {"trial_id": p.get("trial_id"), "status": "unresolved", "error": str(e)[:500], "launch": launch}
        return {**result, "launch": launch}


@workflow.defn
class RunMatrixWorkflow:
    @workflow.run
    async def run(self, p: dict) -> dict:
        results = []
        # Serial pilot runs are deliberate: global launch concurrency = 1, cheap recovery reasoning.
        for trial in p["trials"]:
            try:
                r = await workflow.execute_child_workflow(TrialWorkflow.run, trial, id=trial["trial_id"],
                                                          id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
            except WorkflowAlreadyStartedError:
                r = {"trial_id": trial["trial_id"], "status": "duplicate-rejected"}
            results.append(r)
        await act("score_activity", {"scenario_id": p["scenario_id"]})
        await act("export_activity", {"scenario_id": p["scenario_id"]})
        return {"scenario_id": p["scenario_id"], "trials": results}


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

    @workflow.run
    async def run(self, p: dict) -> dict:
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
            return {"status": "no-valid-candidate", "scenarios": [], **counts}
        max_scenarios = p.get("max_scenarios", 1); max_tried = p.get("max_candidates_tried", 3)
        solver_unavailable = list(probe.get("solver_unavailable") or [])
        for iid in judged["instance_ids"]:
            if counts["completed"] >= max_scenarios or counts["tried"] >= max_tried:
                break
            counts["tried"] += 1
            draft = await act("draft_activity", {**p, **judged, "instance_id": iid}, 30); sid = draft["scenario_id"]
            if _auto(p):
                self.state = {"stage": "auto-approving", **draft}
                approved = await act("auto_approve_activity", {"scenario_id": sid})  # code-owned gates; approval.json says auto-validators
                if not approved["approved"] and p.get("redraft_on_leak", True) and _leak_rejection(approved):
                    # Withholding leak: re-draft once with the violating reasons, never hand-edit the draft.
                    self.state = {"stage": "re-drafting", **draft, "reasons": approved.get("reasons", [])}
                    draft = await act("draft_activity", {**p, **judged, "instance_id": iid, "force": True, "reject_reasons": approved.get("reasons", [])}, 30)
                    approved = await act("auto_approve_activity", {"scenario_id": sid})
                if not approved["approved"]:
                    out.append({"scenario_id": sid, "status": "auto-approval-rejected", "reasons": approved.get("reasons", [])})
                    counts["rejected"] += 1
                    continue
                self.approvals[sid] = approved["spec_hash"]
            else:
                self.state = {"stage": "awaiting-approval", **draft}
                try:
                    # No Boolean is read from wait_condition: the signal payload and approval.json carry the truth.
                    await workflow.wait_condition(lambda sid=sid: sid in self.approvals, timeout=APPROVAL_TIMEOUT)
                except TimeoutError:
                    return {"status": "awaiting-approval-timeout", "scenarios": out, "pending": draft, **counts}
            accepted = await act("verify_approval_activity", {"scenario_id": sid, "spec_hash": self.approvals[sid]})
            step = {**p, **accepted}
            self.state = {"stage": "compiling", **accepted}
            await act("compile_activity", step, 30)
            self.state = {"stage": "validating", **accepted}
            validation = await act("validate_activity", step, 180)
            if validation["status"] != "valid":
                out.append({"scenario_id": sid, **validation})
                counts["invalid"] += 1
                continue
            self.state = {"stage": "registering", **accepted}
            await act("register_activity", step)
            if solver_unavailable:
                # The probe found the solver unavailable and nothing may substitute it silently: registered, no trial launched.
                out.append({"scenario_id": sid, "status": "complete-no-trials", "solver_unavailable": solver_unavailable,
                            "needs_human": probe.get("needs_human") or []})
                counts["completed"] += 1
                continue
            trials = await act("plan_trials_activity", step); results = []
            for trial in trials["trials"]:
                self.state = {"stage": "trial", **accepted, "trial_id": trial["trial_id"]}
                try:
                    r = await workflow.execute_child_workflow(TrialWorkflow.run, trial, id=trial["trial_id"],
                                                              id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
                except WorkflowAlreadyStartedError:
                    r = {"trial_id": trial["trial_id"], "status": "duplicate-rejected"}
                results.append(r)
            self.state = {"stage": "scoring", **accepted}
            await act("score_activity", step)
            await act("export_activity", step)
            out.append({"scenario_id": sid, "status": "complete", "trials": results})
            counts["completed"] += 1
        status = "complete" if counts["completed"] else "complete-with-failures"
        self.state = {"stage": status, **counts}
        return {"status": status, "scenarios": out, **counts}
