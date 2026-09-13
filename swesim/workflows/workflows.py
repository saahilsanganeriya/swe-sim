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
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError

ONE = RetryPolicy(maximum_attempts=1)
READ = RetryPolicy(maximum_attempts=3)
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


async def act(name: str, p: dict, minutes: int = 10):
    return await workflow.execute_activity(name, p, start_to_close_timeout=timedelta(minutes=minutes),
                                           heartbeat_timeout=timedelta(minutes=2), retry_policy=ONE)


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


async def build_scenario(set_stage, p: dict, sid: str, spec_hash: str) -> dict:
    """verify the approval hash -> compile -> validate -> register. Returns status valid | invalid with reasons."""
    accepted = await act("verify_approval_activity", {"scenario_id": sid, "spec_hash": spec_hash})
    step = {**p, **accepted}
    await set_stage("compiling", **accepted)
    await act("compile_activity", step, 30)
    await set_stage("validating", **accepted)
    validation = await act("validate_activity", step, 180)
    if validation["status"] != "valid":
        return {"scenario_id": sid, "spec_hash": accepted["spec_hash"], "status": "invalid", "reasons": list(validation.get("failures", [])),
                "validation": validation, "step": step}
    await set_stage("registering", **accepted)
    await act("register_activity", step)
    return {"scenario_id": sid, "spec_hash": accepted["spec_hash"], "status": "valid", "reasons": [], "validation": validation, "step": step}


async def run_trials(set_trial_status, trials: list[dict], sem: asyncio.Semaphore) -> list[dict]:
    """TrialWorkflow children with REJECT_DUPLICATE, at most `sem` at a time; results keep the trial order."""
    async def one(trial: dict) -> dict:
        async with sem:
            await set_trial_status(trial, "running")
            try:
                r = await workflow.execute_child_workflow(TrialWorkflow.run, trial, id=trial["trial_id"], id_reuse_policy=REJECT_DUP)
            except WorkflowAlreadyStartedError:
                r = {"trial_id": trial["trial_id"], "status": "duplicate-rejected"}
            except Exception as e:  # noqa: BLE001 - one failed child never stops its siblings
                r = {"trial_id": trial["trial_id"], "status": "error", "error": _err(e)}
            await set_trial_status(trial, r.get("status", "unknown"))
            return r
    return list(await asyncio.gather(*(one(t) for t in trials)))


# ------------------------------------------------------------------ trials

@workflow.defn
class TrialWorkflow:
    def __init__(self) -> None:
        self.state: dict = {"stage": "starting"}

    @workflow.query
    def progress(self) -> dict:
        return self.state

    @workflow.run
    async def run(self, p: dict) -> dict:
        launch = None
        self.state = {"stage": "launching", "trial_id": p.get("trial_id")}
        try:
            launch = await act("launch_trial_activity", p, 5)
        except Exception as e:  # noqa: BLE001 - a lost launch acknowledgement is ambiguous by design
            # Poll the same unit/id; never relaunch it. The await activity reads the recorded evidence.
            launch = {"trial_id": p.get("trial_id"), "launched": False, "error": str(e)[:500]}
        self.state = {"stage": "awaiting", "trial_id": p.get("trial_id"), "launch": launch}
        try:
            result = await workflow.execute_activity("await_trial_activity", p, start_to_close_timeout=timedelta(hours=3),
                                                     heartbeat_timeout=timedelta(minutes=2), retry_policy=READ)
        except Exception as e:  # noqa: BLE001 - deadline or repeated read failures: unresolved, not relaunched
            self.state = {"stage": "done", "trial_id": p.get("trial_id"), "status": "unresolved", "launch": launch}
            return {"trial_id": p.get("trial_id"), "status": "unresolved", "error": str(e)[:500], "launch": launch}
        self.state = {"stage": "done", "trial_id": p.get("trial_id"), "status": result.get("status"), "launch": launch}
        return {**result, "launch": launch}


@workflow.defn
class RunMatrixWorkflow:
    @workflow.run
    async def run(self, p: dict) -> dict:
        results = []
        # Serial pilot runs are deliberate: global launch concurrency = 1, cheap recovery reasoning.
        for trial in p["trials"]:
            try:
                r = await workflow.execute_child_workflow(TrialWorkflow.run, trial, id=trial["trial_id"], id_reuse_policy=REJECT_DUP)
            except WorkflowAlreadyStartedError:
                r = {"trial_id": trial["trial_id"], "status": "duplicate-rejected"}
            results.append(r)
        await act("score_activity", {"scenario_id": p["scenario_id"]})
        await act("export_activity", {"scenario_id": p["scenario_id"]})
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
        return {"stage": self.state["stage"], "scenarios": self.scenarios, "trials": self.trials}

    @workflow.run
    async def run(self, p: dict) -> dict:
        p = dict(p or {})
        sem = asyncio.Semaphore(max(1, int(p.get("max_parallel_trials", 1))))
        plan_keys = ("models", "conditions", "k", "purpose", "brief_profile", "stakeholder_model", "attempt_offset", "stakeholder_url")
        plan_p = {k: p[k] for k in plan_keys if p.get(k) is not None}
        results: dict[str, dict] = {}

        async def set_trial_status(trial: dict, status: str) -> None:
            self.trials[trial["trial_id"]] = {**self.trials.get(trial["trial_id"], {}), "status": status}

        async def one_scenario(sid: str) -> None:
            self.scenarios[sid] = {"status": "planning", "trials": 0}
            try:
                planned = await act("plan_trials_activity", {**plan_p, "scenario_id": sid}, 5)
            except Exception as e:  # noqa: BLE001 - an unplannable scenario (not validated) never stops the others
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
                await act("score_activity", {"scenario_id": sid})
                await act("export_activity", {"scenario_id": sid})
                self.scenarios[sid]["status"] = "complete"
            except Exception as e:  # noqa: BLE001
                self.scenarios[sid].update(status="score-export-error", error=_err(e))
            results[sid] = {"status": self.scenarios[sid]["status"], "trials": trial_results}

        self.state = {"stage": "running"}
        await asyncio.gather(*(one_scenario(sid) for sid in p.get("scenario_ids", [])))
        self.state = {"stage": "complete"}
        ok = all(r["status"] == "complete" for r in results.values()) if results else False
        return {"status": "complete" if ok else "complete-with-failures", "scenarios": results, "trials": self.trials}


# ------------------------------------------------------------------ task creation

@workflow.defn
class CandidateWorkflow:
    """judge -> draft -> approval -> compile -> validate -> register for one candidate row.
    Reports every stage to its parent (CreateTasksWorkflow.candidate_stage) and answers `progress` itself."""

    def __init__(self) -> None:
        self.state: dict = {"stage": "starting", "refusals": []}
        self.decisions: list[dict] = []

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
        info = workflow.info()
        if info.parent is not None:
            payload = {"instance_id": self.state.get("instance_id"), "scenario_id": self.state.get("scenario_id"),
                       "spec_hash": self.state.get("spec_hash"), "stage": stage, "refusal": keep.get("refusal")}
            await workflow.get_external_workflow_handle(info.parent.workflow_id).signal("candidate_stage", payload)

    def _done(self, sid, status: str, reasons: list, spec_hash) -> dict:
        self.state = {**self.state, "stage": "done", "status": status, "scenario_id": sid, "spec_hash": spec_hash, "reasons": reasons}
        return {"scenario_id": sid, "instance_id": self.state.get("instance_id"), "status": status, "reasons": reasons, "spec_hash": spec_hash}

    async def _hold_for_reviewer(self, p: dict, judged: dict, iid: str) -> tuple[str, dict | None, dict]:
        """Hold mode: draft, pre-check (schema/assets/leak; one re-draft on a leak), then wait for `approve`.
        Returns (sid, approved-or-None, terminal-result-if-not-approved)."""
        draft = await act("draft_activity", {**p, **judged, "instance_id": iid}, 30); sid = draft["scenario_id"]
        await self._stage("pre-checking", **draft)
        pre = await act("precheck_draft_activity", {"scenario_id": sid}, 5)
        if not pre["ok"] and p.get("redraft_on_leak", True) and any(_is_leak_reason(r) for r in pre.get("reasons", [])):
            await self._stage("re-drafting", **draft, reasons=pre.get("reasons", []))
            await act("draft_activity", {**p, **judged, "instance_id": iid, "force": True, "reject_reasons": pre.get("reasons", [])}, 30)
            pre = await act("precheck_draft_activity", {"scenario_id": sid}, 5)
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
                    approved = await act("human_approve_activity", {"scenario_id": sid, "spec_hash": d.get("spec_hash"), "note": d.get("note")}, 5)
                    return sid, approved, {}
                except Exception as e:
                    if not _refusable(e):  # cancellation or infrastructure failure: propagate, never re-enter the wait
                        raise
                    refusal = f"approve refused: {_err(e)}"
            self.state["refusals"] = [*self.state.get("refusals", []), refusal]

    @workflow.run
    async def run(self, p: dict) -> dict:
        p = dict(p or {}); iid = p["instance_id"]; sid = None
        self.state = {**self.state, "instance_id": iid}
        if p.get("approval_mode", "auto") not in CREATE_APPROVAL_MODES:
            raise ApplicationError(f"approval_mode must be one of {sorted(CREATE_APPROVAL_MODES)}", non_retryable=True)
        try:
            await self._stage("judging", instance_id=iid)
            judged = await act("judge_one_activity", p, 30); sid = judged.get("scenario_id")
            if not judged.get("keep"):
                return self._done(sid, "rejected", list(judged.get("reasons") or ["judge did not keep the candidate"]), None)
            # Measure FAIL_TO_PASS / PASS_TO_PASS on base and gold (Harbor on Modal) for every source kind: SWE-bench's
            # published lists do not always reproduce offline. One attempt; harbor_cli reconciles an existing job.
            await self._stage("probing", scenario_id=sid)
            probed = await act("probe_candidate_activity", {"judged_path": judged["judged_path"], "instance_id": iid, "scenario_id": sid,
                                                            "max_builds": p.get("max_builds", 1)}, 160)  # probe_tests' Harbor deadline is 150 min
            if not probed.get("fail_to_pass"):
                return self._done(sid, "not-reproducible-offline", [probed.get("reason") or "measured FAIL_TO_PASS is empty: no test fails on base and passes on gold"], None)
            judged = {**judged, "judged_path": probed["judged_path"]}  # the probed row (measured lists) is what draft reads
            if p.get("rebuild") and p.get("previous_version"):
                # Rebuild as version p["version"]: the previous version's spec/compile/validation/approval/judge/draft-meta files
                # move to archive/v<previous>/ so draft (with version=next), approve, compile, validate and register run afresh.
                # Judged rows and probe outputs are retained. (The force/reject_reasons re-draft path is unchanged.)
                await self._stage("archiving", scenario_id=sid)
                await act("archive_scenario_activity", {"scenario_id": sid, "previous_version": p["previous_version"]}, 5)
            if p.get("approval_mode", "auto") == "hold":
                sid, approved, terminal = await self._hold_for_reviewer(p, judged, iid)
                if approved is None:
                    return terminal
            else:
                draft, approved = await draft_and_auto_approve(self._stage, p, judged, iid); sid = draft["scenario_id"]
                if not approved["approved"]:
                    return self._done(sid, "rejected", list(approved.get("reasons", [])), None)
            built = await build_scenario(self._stage, p, sid, approved["spec_hash"])
            return self._done(sid, built["status"], built["reasons"], built["spec_hash"])
        except Exception as e:
            # Infrastructure failure (LLM, Harbor, a lost parent): FAIL the child so `create:<sid>` can be started again
            # (ALLOW_DUPLICATE_FAILED_ONLY); the parent records `error` from the cause and siblings are unaffected.
            stage = self.state.get("stage")
            self._done(sid, "error", [f"{stage}: {_err(e)}"], None)
            raise ApplicationError(f"candidate {iid} failed at stage {stage}: {_err(e)}", non_retryable=True) from e


def _mine_params(src: dict) -> dict:
    if src.get("kind") == "github-repo":
        out = {"source": "github-repo", "repo": src["repo"], "since": src.get("since"), "max_candidates": src.get("max_candidates"), "pr": src.get("pr")}
    else:
        out = {"source": "swebench-annotated", "pool": src.get("pool", "A"), "instance_ids": src.get("instance_ids"), "limit": src.get("limit")}
    return {k: v for k, v in out.items() if v is not None}


def _normalize_repo(repo: str) -> str:
    """`https://github.com/X/A/`, `x/a.git`, `git@github.com:x/a` -> `x/a` (the same spellings mine merges into one output file)."""
    r = str(repo).strip().casefold()
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:", "github.com/"):
        if r.startswith(prefix):
            r = r[len(prefix):]
    r = r.rstrip("/")
    if r.endswith(".git"):
        r = r[:-4]
    return r.rstrip("/")


def candidate_child_id(sid: str, version=None) -> str:
    """`create:<sid>` for a first build; `create:<sid>@v<n>` when an existing scenario is rebuilt as version n (n >= 2)."""
    return f"create:{sid}" if not version or int(version) <= 1 else f"create:{sid}@v{int(version)}"


def _source_key(src: dict) -> str:
    kind = src.get("kind", "swebench-annotated")
    if kind == "github-repo":
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
                "probe": self.state.get("probe", {}), "candidates": self.candidates,
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
        keys = [_source_key(src) for src in p.get("sources", [])]
        dups = sorted({k for k in keys if keys.count(k) > 1})
        if dups:
            raise ApplicationError(f"duplicate sources in one batch: {dups}; merge them into one source or run separate batches", non_retryable=True)
        if not keys:
            raise ApplicationError("sources is empty", non_retryable=True)
        sem = asyncio.Semaphore(max(1, int(p.get("max_parallel", 8))))
        rebuild = bool(p.get("rebuild"))
        self.state = {"stage": "probing-models", "sources": {}, "probe": {}, "rebuild": rebuild}
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
                         "approval_mode": mode, "redraft_on_leak": p.get("redraft_on_leak", True), "version": 1}
                child_id = candidate_child_id(sid)
                if rebuild:
                    # Rebuild: an existing scenario (scenario.json or registry entry) is re-created as version n+1 under a new
                    # child id; nothing existing behaves like a first build (version 1, plain id).
                    try:
                        ver = await act("scenario_version_activity", {"scenario_id": sid}, 5)
                    except Exception as e:  # noqa: BLE001 - one unreadable scenario never stops its siblings
                        r = {"scenario_id": sid, "instance_id": cand["instance_id"], "status": "error", "reasons": [_err(e)], "spec_hash": None}
                        self.candidates[sid].update(status="error", stage="done", reasons=r["reasons"]); self.counts["error"] = self.counts.get("error", 0) + 1
                        results[sid] = r
                        return
                    if ver.get("exists"):
                        child.update(rebuild=True, version=ver["next"], previous_version=ver["version"])
                        child_id = candidate_child_id(sid, ver["next"])
                        self.candidates[sid].update(version=ver["next"], previous_version=ver["version"], candidate_workflow_id=child_id)
                try:
                    r = await workflow.execute_child_workflow(CandidateWorkflow.run, child, id=child_id, id_reuse_policy=DUP_FAILED_ONLY)
                except WorkflowAlreadyStartedError:
                    r = {"scenario_id": sid, "instance_id": cand["instance_id"], "status": "duplicate-rejected", "spec_hash": None,
                         "reasons": ["a CandidateWorkflow with this id already completed; read its result instead"]}
                except Exception as e:  # noqa: BLE001 - one failed child never stops its siblings; a failed id may be started again
                    r = {"scenario_id": sid, "instance_id": cand["instance_id"], "status": "error", "reasons": [_err(e)], "spec_hash": None}
                self.candidates[sid].update(status=r["status"], stage="done", spec_hash=r.get("spec_hash"), reasons=r.get("reasons", []))
                self.counts[r["status"]] = self.counts.get(r["status"], 0) + 1
                results[sid] = r

        async def one_source(src: dict) -> None:
            key = _source_key(src)
            self.state["sources"][key] = {"status": "mining"}
            try:
                mined = await act("mine_activity", {**p, **_mine_params(src)}, 180)
                listed = await act("list_candidates_activity", {"candidates_path": mined["candidates_path"],
                                                                "max_candidates": src.get("max_candidates") or src.get("limit")}, 5)
            except Exception as e:  # noqa: BLE001 - a failed source never stops the others
                self.state["sources"][key] = {"status": "error", "error": _err(e)}
                return
            self.state["sources"][key] = {"status": "mined", "candidates": len(listed["candidates"]), "candidates_path": mined["candidates_path"]}
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
        self.state = {"stage": stage, **extra}

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
            trials = await act("plan_trials_activity", step); results = []
            for trial in trials["trials"]:
                self.state = {"stage": "trial", **accepted, "trial_id": trial["trial_id"]}
                try:
                    r = await workflow.execute_child_workflow(TrialWorkflow.run, trial, id=trial["trial_id"], id_reuse_policy=REJECT_DUP)
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
