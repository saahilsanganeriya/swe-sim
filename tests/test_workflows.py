"""Offline, deterministic tests for the Temporal orchestration stream (Tasks 11 and 13).

No Modal, no Harbor, no LLM, no network. Stage modules owned by other streams are
replaced with fake modules in sys.modules; systemctl/systemd-run are replaced with a
fake runner. Temporal's time-skipping test server is used when it can start; otherwise
the local dev server on PATH; otherwise those tests skip with an explicit reason.
"""
from __future__ import annotations

import asyncio
import configparser
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
from temporalio.common import WorkflowIDReusePolicy

from swesim.models import RunConfig, ScenarioSpec, minimal_spec_dict, trial_id
from swesim.paths import Workspace, atomic_json
from swesim.settings import QUEUES
from swesim.workflows import activities as A
from swesim.workflows import queues as Q
from swesim.workflows import workflows as W
from swesim.workflows.launchers import systemd as SYS
from test_launchers import FakeSystemd

ROOT = Path(__file__).resolve().parents[1]


def stamped(d: dict) -> dict:
    """Every activity result carries the worker's image digest and origin (interfaces §3)."""
    return {**d, "worker_image_digest": A.SETTINGS.worker_image_digest, "origin": A.SETTINGS.origin}


@contextlib.asynccontextmanager
async def workers(env, workflows, activities, **worker_options):
    """One Worker per swesim queue: `act()` routes each activity to its queue, so every queue needs a poller (fakes everywhere).
    `worker_options` are forwarded to every `Worker` (e.g. `max_heartbeat_throttle_interval` for a test that depends on prompt
    activity-cancellation delivery)."""
    from temporalio.worker import Worker  # noqa: PLC0415 - kept local so the offline tests import without a worker
    from temporalio import activity
    from swesim.workflows.search_attributes import ensure_registered
    from temporalio.service import RPCError, RPCStatusCode
    try:
        await ensure_registered(env.client)
    except RPCError as e:
        if e.status != RPCStatusCode.UNIMPLEMENTED:
            raise
        pytest.skip('Temporal test server cannot register the required search attributes')
    # Keep the established stage fakes; adapt their in-memory outputs to the new immutable-plan activity boundaries.
    mapping = {activity._Definition.from_callable(a).name: a for a in activities}; plans = {}
    extra = []; paged = False
    if W.RunTasksWorkflow in workflows and 'probe_models_activity' not in mapping:
        @activity.defn(name='probe_models_activity')
        async def probe_for_run(p): return {'resolved': {}, 'solver_unavailable': []}
        extra.append(probe_for_run)
    if W.RunTasksWorkflow in workflows and 'frontier_gate_activity' not in mapping:
        # The gate's policy is tested in test_frontier_gate.py and through A.plan_trials; this stand-in admits Tier A–C requests
        # only, so an env test that names a frontier model without a real decision is blocked, never waved through.
        from swesim.stages.frontier import frontier_requested
        @activity.defn(name='frontier_gate_activity')
        async def gate_for_run(p):
            if frontier_requested(p): return {'allowed': False, 'scenario_ids': [], 'reasons': ['no frontier evidence in this test environment']}
            return {'allowed': True, 'scenario_ids': list(p.get('scenario_ids') or []), 'reasons': []}
        extra.append(gate_for_run)
    if W.RunTasksWorkflow in workflows and 'spend_check_activity' not in mapping:
        @activity.defn(name='spend_check_activity')
        async def spend_for_run(p): return {'spend_usd': 0.0, 'cap_usd': p.get('cap_usd'), 'exceeded': False, 'spend_complete': True}
        extra.append(spend_for_run)
    if 'plan_trials_activity' in mapping and 'prepare_trial_plan_activity' not in mapping:
        @activity.defn(name='prepare_trial_plan_activity')
        async def prepare_trials(p):
            result = await mapping['plan_trials_activity'](p)
            path = p['scenario_id']; plans[path] = result['trials']
            return {'plan_path': path, 'total': len(plans[path])}
        extra.append(prepare_trials); paged = True
    if 'list_candidates_activity' in mapping and 'prepare_candidate_plan_activity' not in mapping:
        @activity.defn(name='prepare_candidate_plan_activity')
        async def prepare_candidates(p):
            items = []; seen = set(); duplicates = 0
            for source in p['sources']:
                result = await mapping['list_candidates_activity'](source)
                for c in result['candidates']:
                    if c['scenario_id'] in seen:
                        duplicates += 1
                        continue
                    seen.add(c['scenario_id']); items.append({**source, **c})
            plans['candidates'] = items
            return {'plan_path': 'candidates', 'total': len(items), 'duplicates': duplicates}
        extra.append(prepare_candidates); paged = True
    if paged and 'read_plan_page_activity' not in mapping:
        @activity.defn(name='read_plan_page_activity')
        async def read_page(p):
            items = plans[p['plan_path']]; offset = p.get('offset', 0)
            page = items[offset:offset + p['limit']]
            return {'items': page, 'next_offset': offset + len(page), 'total': len(items)}
        extra.append(read_page)
    if 'checkpoint_workflow_activity' not in mapping:
        @activity.defn(name='checkpoint_workflow_activity')
        async def checkpoint(p): return {'checkpoint_path': '/checkpoint/' + p['run_id']}
        extra.append(checkpoint)
    activities = [*activities, *extra]
    async with contextlib.AsyncExitStack() as stack:
        for q in QUEUES:
            await stack.enter_async_context(Worker(env.client, task_queue=q, workflows=workflows, activities=activities, **worker_options))
        yield
BIG = {"candidates_path": "c", "judged_path": "j", "instance_ids": ["a"], "scenario_id": "aaa", "draft_path": "d",
       "status": "valid", "failures": [], "trials": [], "spec_hash": "h"}


# ---------------------------------------------------------------- brief's three tests

def test_approval_wait_none_is_success(monkeypatch):
    async def scenario():
        w = W.PipelineWorkflow(); calls = []
        async def act(name, p, minutes=10, **kw):
            calls.append(name); return dict(BIG)
        async def wait(predicate, timeout):
            w.approve_spec({"scenario_id": "aaa", "spec_hash": "h"}); assert predicate(); return None
        monkeypatch.setattr(W, "act", act); monkeypatch.setattr(W.workflow, "wait_condition", wait)
        result = await w.run({"approval_mode": "human"})
        assert result["scenarios"][0]["status"] == "complete" and "verify_approval_activity" in calls and "validate_activity" in calls
    asyncio.run(scenario())


@pytest.mark.parametrize("approved", [True, False])
def test_auto_approval_never_waits_and_rejection_stops_validation(monkeypatch, approved):
    async def scenario():
        w = W.PipelineWorkflow(); calls = []
        async def act(name, p, minutes=10, **kw):
            calls.append(name)
            if name == "auto_approve_activity":
                return {"approved": approved, "spec_hash": "h", "reasons": [] if approved else ["gate rejected"]}
            return dict(BIG)
        async def wait(*args, **kwargs):
            raise AssertionError("automatic approval must not wait for a person")
        monkeypatch.setattr(W, "act", act); monkeypatch.setattr(W.workflow, "wait_condition", wait)
        result = await w.run({})
        assert "auto_approve_activity" in calls
        assert ("verify_approval_activity" in calls) == approved
        assert ("validate_activity" in calls) == approved
        assert ("register_activity" in calls) == approved
        expected = "complete" if approved else "auto-approval-rejected"
        assert result["scenarios"][0]["status"] == expected
    asyncio.run(scenario())


def test_matrix_uses_precomputed_distinct_model_ids(monkeypatch):
    async def scenario():
        ids = []
        async def child(fn, p, **kw):
            ids.append(kw["id"]); assert kw["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
            return {"trial_id": kw["id"]}
        async def act(*args, **kw): return {}
        monkeypatch.setattr(W.workflow, "execute_child_workflow", child); monkeypatch.setattr(W, "act", act)
        await W.RunMatrixWorkflow().run({"scenario_id": "aaa", "trials": [{"trial_id": "model-a"}, {"trial_id": "model-b"}]})
        assert ids == ["model-a", "model-b"]
    asyncio.run(scenario())


# ---------------------------------------------------------------- more pipeline behaviour (direct, no Temporal)

def test_signal_mode_verifies_the_signalled_hash_and_times_out_cleanly(monkeypatch):
    async def scenario():
        w = W.PipelineWorkflow(); seen = {}
        async def act(name, p, minutes=10, **kw):
            if name == "verify_approval_activity": seen.update(p)
            return dict(BIG)
        async def wait(predicate, timeout):
            w.approve_spec({"scenario_id": "aaa", "spec_hash": "signalled"}); return None
        monkeypatch.setattr(W, "act", act); monkeypatch.setattr(W.workflow, "wait_condition", wait)
        await w.run({"approval_mode": "signal"})
        assert seen == {"scenario_id": "aaa", "spec_hash": "signalled"}
        # Timeout: no boolean is read; the TimeoutError is the only signal of a missing approval.
        w2 = W.PipelineWorkflow()
        async def wait_timeout(predicate, timeout): raise TimeoutError()
        monkeypatch.setattr(W.workflow, "wait_condition", wait_timeout)
        r = await w2.run({"approval_mode": "signal"})
        assert r["status"] == "awaiting-approval-timeout" and r["pending"]["scenario_id"] == "aaa"
        assert w2.progress()["stage"] == "awaiting-approval"
    asyncio.run(scenario())


def test_unknown_approval_mode_fails_fast_instead_of_waiting(monkeypatch):
    from temporalio.exceptions import ApplicationError
    async def scenario():
        async def act(name, p, minutes=10, **kw): raise AssertionError("no activity may run with an invalid approval mode")
        monkeypatch.setattr(W, "act", act)
        with pytest.raises(ApplicationError, match="approval_mode"): await W.PipelineWorkflow().run({"approval_mode": "atuo"})
    asyncio.run(scenario())


def test_invalid_validation_skips_register_and_trials(monkeypatch):
    async def scenario():
        w = W.PipelineWorkflow(); calls = []
        async def act(name, p, minutes=10, **kw):
            calls.append(name)
            if name == "auto_approve_activity": return {"approved": True, "spec_hash": "h", "reasons": []}
            if name == "validate_activity": return {"status": "invalid", "failures": ["oracle earned 0"]}
            return dict(BIG)
        monkeypatch.setattr(W, "act", act)
        r = await w.run({})
        assert r["scenarios"][0]["status"] == "invalid" and "register_activity" not in calls and "plan_trials_activity" not in calls
    asyncio.run(scenario())


def test_auto_rejection_for_withholding_leak_redrafts_once_then_moves_on(monkeypatch):
    async def scenario():
        w = W.PipelineWorkflow(); calls = []
        async def act(name, p, minutes=10, **kw):
            calls.append((name, p.get("force", False)))
            if name == "auto_approve_activity": return {"approved": False, "spec_hash": "h", "reasons": ["withheld brief still contains excerpt of f01"]}
            if name == "judge_activity": return {"judged_path": "j", "instance_ids": ["a", "b", "c", "d"]}
            return dict(BIG)
        monkeypatch.setattr(W, "act", act)
        r = await w.run({"max_scenarios": 1, "max_candidates_tried": 2})
        drafts = [f for n, f in calls if n == "draft_activity"]
        assert drafts == [False, True, False, True]          # two candidates tried, one re-draft each
        assert [s["status"] for s in r["scenarios"]] == ["auto-approval-rejected"] * 2
    asyncio.run(scenario())


@pytest.mark.parametrize("reasons,gate,redraft", [
    (["withheld brief still contains excerpt of f01"], None, True),      # the approve stage's actual wording
    (["withholding: removed excerpt visible"], None, True),
    (["Brief leak detected in profile withheld"], None, True),
    (["judge did not accept (no non-derivable gap)"], None, False),
    (["controls incomplete"], None, False),
    (["controls incomplete"], "withholding-leak", True),                 # structured gate wins over the text
])
def test_redraft_trigger_is_structured(monkeypatch, reasons, gate, redraft):
    # Fix round 1, Minor 7.
    async def scenario():
        w = W.PipelineWorkflow(); calls = []
        async def act(name, p, minutes=10, **kw):
            calls.append(name)
            if name == "auto_approve_activity":
                out = {"approved": False, "spec_hash": "h", "reasons": reasons}
                if gate: out["gate"] = gate
                return out
            return dict(BIG)
        monkeypatch.setattr(W, "act", act)
        await w.run({"max_candidates_tried": 1})
        assert calls.count("draft_activity") == (2 if redraft else 1)
    asyncio.run(scenario())
    assert W._is_leak_reason("Withholding leak") and not W._is_leak_reason("judge rejected")


def test_pipeline_result_counts_and_failure_status(monkeypatch):
    # Fix round 1, Minor 8.
    async def scenario():
        async def act(name, p, minutes=10, **kw):
            if name == "judge_activity": return {"judged_path": "j", "instance_ids": ["a", "b", "c"]}
            if name == "draft_activity": return {"scenario_id": p["instance_id"], "draft_path": "d"}
            if name == "auto_approve_activity": return {"approved": p["scenario_id"] != "a", "spec_hash": "h", "reasons": ["judge did not accept"]}
            if name == "verify_approval_activity": return {"scenario_id": p["scenario_id"], "spec_hash": p["spec_hash"]}
            if name == "validate_activity": return {"status": "invalid" if p["scenario_id"] == "b" else "valid", "failures": ["oracle earned 0"]}
            return dict(BIG)
        monkeypatch.setattr(W, "act", act)
        r = await W.PipelineWorkflow().run({"max_scenarios": 1, "max_candidates_tried": 3})
        assert (r["tried"], r["completed"], r["rejected"], r["invalid"]) == (3, 1, 1, 1) and r["status"] == "complete"
        assert [s["status"] for s in r["scenarios"]] == ["auto-approval-rejected", "invalid", "complete"]
        r2 = await W.PipelineWorkflow().run({"max_scenarios": 1, "max_candidates_tried": 1})
        assert r2["status"] == "complete-with-failures" and (r2["tried"], r2["completed"], r2["rejected"]) == (1, 0, 1)
    asyncio.run(scenario())


def test_solver_unavailable_registers_but_launches_nothing(monkeypatch):
    # Fix round 1, Minor 9.
    async def scenario():
        w = W.PipelineWorkflow(); calls = []
        async def act(name, p, minutes=10, **kw):
            calls.append(name)
            if name == "probe_models_activity":
                return {"substitutions": {}, "solver_unavailable": ["openai/gpt-5.5"], "needs_human": ["solver model openai/gpt-5.5 unavailable; no silent substitution"]}
            if name == "auto_approve_activity": return {"approved": True, "spec_hash": "h", "reasons": []}
            return dict(BIG)
        async def child(*a, **k): raise AssertionError("no trial may launch with an unavailable solver")
        monkeypatch.setattr(W, "act", act); monkeypatch.setattr(W.workflow, "execute_child_workflow", child)
        r = await w.run({"models": ["openai/gpt-5.5"]})
        s = r["scenarios"][0]
        assert s["status"] == "complete-no-trials" and s["solver_unavailable"] == ["openai/gpt-5.5"] and s["needs_human"]
        assert "register_activity" in calls and "plan_trials_activity" not in calls and "score_activity" not in calls
        assert r["completed"] == 1 and r["status"] == "complete"
    asyncio.run(scenario())


def test_probe_substitutions_flow_into_later_activities(monkeypatch):
    async def scenario():
        w = W.PipelineWorkflow(); seen = {}
        async def act(name, p, minutes=10, **kw):
            if name == "probe_models_activity": return {"substitutions": {"judge_model": "anthropic/claude-sonnet-5"}}
            if name == "judge_activity": seen["judge_model"] = p.get("judge_model")
            if name == "auto_approve_activity": return {"approved": False, "spec_hash": "h", "reasons": ["x"]}
            return dict(BIG)
        monkeypatch.setattr(W, "act", act)
        await w.run({"judge_model": "openai/gpt-5.5"})
        assert seen["judge_model"] == "anthropic/claude-sonnet-5"
    asyncio.run(scenario())


def test_matrix_records_duplicate_rejection_and_still_scores(monkeypatch):
    from temporalio.exceptions import WorkflowAlreadyStartedError
    async def scenario():
        acts = []
        async def child(fn, p, **kw):
            if p["trial_id"] == "dup": raise WorkflowAlreadyStartedError("dup", "TrialWorkflow", run_id=None)
            return {"trial_id": p["trial_id"], "status": "completed"}
        async def act(name, p, minutes=10, **kw): acts.append(name); return {}
        monkeypatch.setattr(W.workflow, "execute_child_workflow", child); monkeypatch.setattr(W, "act", act)
        r = await W.RunMatrixWorkflow().run({"scenario_id": "aaa", "trials": [{"trial_id": "dup"}, {"trial_id": "ok"}]})
        assert [t["status"] for t in r["trials"]] == ["duplicate-rejected", "completed"]
        assert acts == ["score_activity", "export_activity"]
    asyncio.run(scenario())


def test_trial_workflow_launch_failure_is_swallowed_and_await_decides(monkeypatch):
    async def scenario():
        calls = []
        async def act(name, p, minutes=10, **kw):
            calls.append((name, kw.get("retry")))
            if name == "launch_trial_activity": raise RuntimeError("launch requires reconciliation")
            if name == "cleanup_trial_activity": return {"trial_id": p["trial_id"], "cleaned": True}
            return {"trial_id": p["trial_id"], "status": "launch-failed"}
        monkeypatch.setattr(W, "act", act)
        w = W.TrialWorkflow(); r = await w.run({"trial_id": "t1"})
        main = [c for c in calls if c[0] in ("launch_trial_activity", "await_trial_activity")]
        assert main == [("launch_trial_activity", W.retries.ONCE), ("await_trial_activity", W.retries.READ)] and r["status"] == "launch-failed"
        # bookkeeping after the terminal status: cost, index, cleanup (hooks use the HOOK class so a missing activity fails fast)
        tail = [c for c in calls if c[0] in ("record_cost_activity", "index_trial_activity", "cleanup_trial_activity")]
        assert tail == [("record_cost_activity", W.retries.HOOK), ("index_trial_activity", W.retries.HOOK), ("cleanup_trial_activity", W.retries.HOOK)]
        assert r["bookkeeping"]["cleanup"] == {"trial_id": "t1", "cleaned": True} and r["bookkeeping"]["indexed"] is True
        prog = w.progress()
        assert prog["stage"] == "done" and prog["trial_id"] == "t1" and prog["status"] == "launch-failed" and prog["launch"]["launched"] is False
    asyncio.run(scenario())


def _activity_error(cause):
    from temporalio.exceptions import ActivityError
    return ActivityError("activity failed", scheduled_event_id=1, started_event_id=2, identity="w", activity_type="x", activity_id="1", retry_state=None)


def test_bookkeeping_hooks_never_fail_the_workflow_and_record_indexed_false(monkeypatch):
    """A CONTROL activity the worker does not have (the owning stream is not merged) or that times out is recorded, not raised."""
    from temporalio.exceptions import ApplicationError, TimeoutError as TTimeoutError, TimeoutType
    async def scenario():
        async def act(name, p, minutes=10, **kw):
            if name == "index_trial_activity":
                e = _activity_error(None); e.__cause__ = ApplicationError("Activity function index_trial_activity ... is not registered on this worker", type="NotFoundError"); raise e
            if name == "record_cost_activity":
                e = _activity_error(None); e.__cause__ = TTimeoutError("schedule to close", type=TimeoutType.SCHEDULE_TO_CLOSE, last_heartbeat_details=[]); raise e
            if name == "cleanup_trial_activity": raise RuntimeError("no trials worker")
            if name == "launch_trial_activity": return {"trial_id": p["trial_id"], "launched": True}
            return {"trial_id": p["trial_id"], "status": "completed", "reward": {"reward": 1.0, "success": 1}}
        monkeypatch.setattr(W, "act", act)
        w = W.TrialWorkflow(); r = await w.run({"trial_id": "t1", "model": "m"})
        assert r["status"] == "completed"                                                           # the trial's outcome is untouched
        b = r["bookkeeping"]
        assert b["indexed"] is False and b["index"]["indexed"] is False and "not registered" in b["index"]["reason"]
        assert b["cost"]["indexed"] is False and "TimeoutError" in b["cost"]["reason"]
        assert b["cleanup"]["cleaned"] is False and "no trials worker" in b["cleanup"]["reason"]
        assert w.progress()["indexed"] is False and w.progress()["status"] == "completed"
        # the hook helper itself: a successful hook passes the activity's dict through
        async def ok(name, p, minutes=10, **kw): return {"indexed": True, "rows": 1}
        monkeypatch.setattr(W, "act", ok)
        assert await W.hook("index_score_activity", {"scenario_id": "s"}) == {"indexed": True, "rows": 1}
        assert (await W.index_workflow("running", {}))["indexed"] is False                           # no workflow identity as a plain coroutine
    asyncio.run(scenario())


def test_run_tasks_stops_planning_when_the_spend_cap_is_exceeded(monkeypatch):
    async def scenario():
        calls = []; state = {"exceeded": False}
        async def act(name, p, minutes=10, **kw):
            calls.append(name)
            if name == "frontier_gate_activity":
                return {"allowed": True, "scenario_ids": p["scenario_ids"], "reasons": []}       # Tier A–C: no evidence needed
            if name == "spend_check_activity":
                state["exceeded"] = state["exceeded"] or len([c for c in calls if c == "prepare_trial_plan_activity"]) >= 1
                return {"spend_usd": 1600.0 if state["exceeded"] else 10.0, "cap_usd": p.get("cap_usd") or 1500.0, "exceeded": state["exceeded"]}
            if name == "prepare_trial_plan_activity":
                return {"plan_path": p["scenario_id"], "total": 1}
            if name == "read_plan_page_activity":
                return {"items": [{"trial_id": p["plan_path"] + "-t0", "scenario_id": p["plan_path"], "model": "m", "condition": "hidden", "attempt_index": 0}], "total": 1, "next_offset": 1}
            if name == "checkpoint_workflow_activity":
                return {"checkpoint_path": "/checkpoint/test"}
            return {}
        async def child(fn, arg, **kw): return {"trial_id": arg["trial_id"], "status": "completed"}
        monkeypatch.setattr(W, "act", act); monkeypatch.setattr(W.workflow, "execute_child_workflow", child)
        # the scenarios are planned one after another: the first is planned, the check before the second sees the cap exceeded
        w = W.RunTasksWorkflow()
        real_gather = asyncio.gather
        async def serial(*coros):
            out = []
            for c in coros: out.append(await c)
            return out
        monkeypatch.setattr(W.asyncio, "gather", serial)
        try:
            r = await w.run({"scenario_ids": ["s1", "s2", "s3"], "models": ["m"], "conditions": ["hidden"], "k": 1, "stakeholder_url": "https://s", "spend_cap_usd": 1500})
        finally:
            monkeypatch.setattr(W.asyncio, "gather", real_gather)
        assert r["stopped_reason"] == "spend-cap" and r["spend"]["exceeded"] is True and r["spend"]["cap_usd"] == 1500.0
        assert r["scenarios"]["s1"]["status"] == "complete" and r["scenarios"]["s2"] == {"status": "skipped", "reason": "spend-cap", "trials": []} and r["scenarios"]["s3"]["status"] == "skipped"
        assert calls.count("prepare_trial_plan_activity") == 1 and r["status"] == "complete-with-failures"
        assert w.progress()["stopped_reason"] == "spend-cap" and w.progress()["scenarios"]["s2"]["status"] == "skipped"
        # once exceeded, later checks are not even asked (the run has stopped planning)
        assert calls.count("spend_check_activity") == 3   # at start, before s1, before s2 (exceeded); s3 short-circuits
    asyncio.run(scenario())


def test_run_tasks_frontier_gate_blocks_or_binds_every_planning_step(monkeypatch):
    async def scenario():
        calls = []
        decision = {"allowed": True, "scenario_ids": ["s2"], "reasons": [], "evidence_path": "/ws/evidence/sweep/frontier-abc.json", "identity": "abc",
                    "excluded_scenarios": {"s1": "Saturated scenarios are excluded by default."}}
        async def act(name, p, minutes=10, **kw):
            calls.append((name, p))
            if name == "frontier_gate_activity": return dict(decision)
            if name == "probe_models_activity": return {"resolved": {}, "solver_unavailable": []}
            if name == "spend_check_activity": return {"spend_usd": 1.0, "cap_usd": 100.0, "exceeded": False}
            if name == "prepare_trial_plan_activity": return {"plan_path": p["scenario_id"], "total": 1}
            if name == "read_plan_page_activity":
                return {"items": [{"trial_id": p["plan_path"] + "-t0", "scenario_id": p["plan_path"], "model": "openai/gpt-5.5", "condition": "hidden", "attempt_index": 0}], "total": 1, "next_offset": 1}
            if name == "checkpoint_workflow_activity": return {"checkpoint_path": "/checkpoint/test"}
            return {}
        async def child(fn, arg, **kw): return {"trial_id": arg["trial_id"], "status": "completed"}
        monkeypatch.setattr(W, "act", act); monkeypatch.setattr(W.workflow, "execute_child_workflow", child)
        params = {"scenario_ids": ["s1", "s2"], "models": ["openai/gpt-5.5"], "conditions": ["hidden"], "k": 1, "stakeholder_url": "https://s"}
        r = await W.RunTasksWorkflow().run(params)
        prepared = [p for n, p in calls if n == "prepare_trial_plan_activity"]
        assert [p["scenario_id"] for p in prepared] == ["s2"]                                             # the excluded scenario is never planned
        assert all(p["admission"] == {"evidence_path": decision["evidence_path"], "identity": "abc"} for p in prepared)   # plan_trials verifies this file
        assert r["status"] == "complete" and set(r["scenarios"]) == {"s2"}
        # a refused gate blocks before any probe or plan
        decision.update(allowed=False, scenario_ids=[], reasons=["Requires 15 current audited scenarios with at least two outcomes per arm on every core Tier A–C baseline."]); calls.clear()
        w = W.RunTasksWorkflow(); r2 = await w.run(params)
        assert r2["status"] == "blocked" and r2["stopped_reason"] == "frontier-gate" and r2["admission"]["reasons"] == decision["reasons"]
        assert not [n for n, _ in calls if n in ("probe_models_activity", "prepare_trial_plan_activity")] and w.progress()["stopped_reason"] == "frontier-gate"
        # a Tier A–C request passes the gate without a decision file and carries no admission reference
        decision.update(allowed=True, scenario_ids=["s1", "s2"], reasons=[]); decision.pop("evidence_path"); decision.pop("identity"); calls.clear()
        r3 = await W.RunTasksWorkflow().run({**params, "models": ["openrouter/deepseek/deepseek-v3.2"]})
        assert r3["status"] == "complete" and all("admission" not in p for n, p in calls if n == "prepare_trial_plan_activity")
    asyncio.run(scenario())


def test_operator_workflows_write_running_first_and_a_terminal_row_on_every_exit(monkeypatch):
    """P1-6: the running row precedes any probe/gate; failure, cancellation and invalid input end as recorded rows with the reason;
    continue-as-new is not a failure (its `continued_as_new` row is written by `_continue`)."""
    from types import SimpleNamespace
    async def scenario():
        monkeypatch.setattr(W.workflow, "info", lambda: SimpleNamespace(parent=None, workflow_id="run-1", run_id="r1", workflow_type="RunTasksWorkflow"))
        calls = []
        def fake(raise_on_gate):
            async def act(name, p, minutes=10, **kw):
                calls.append((name, p))
                if name == "frontier_gate_activity": raise raise_on_gate
                return {}
            return act
        params = {"scenario_ids": ["s1"], "models": ["m"], "stakeholder_url": "https://s"}
        monkeypatch.setattr(W, "act", fake(RuntimeError("control pool down")))
        with pytest.raises(RuntimeError): await W.RunTasksWorkflow().run(params)
        names = [n for n, _ in calls]
        assert names.index("index_workflow_activity") < names.index("frontier_gate_activity")          # running before the gate/probe
        rows = [p for n, p in calls if n == "index_workflow_activity"]
        assert [r["status"] for r in rows] == ["running", "failed"] and rows[-1]["workflow_id"] == "run-1" and rows[-1]["run_id"] == "r1"
        assert "RuntimeError: control pool down" in rows[-1]["failure"] and rows[0]["failure"] is None and rows[0]["params_summary"]["models"] == ["m"]
        # cancellation: a cancelled row (no failure text), then the cancellation propagates
        calls.clear(); monkeypatch.setattr(W, "act", fake(asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError): await W.RunTasksWorkflow().run(params)
        assert [(p["status"], p["failure"]) for n, p in calls if n == "index_workflow_activity"] == [("running", None), ("cancelled", None)]
        # continue-as-new passes through: no failed row from the wrapper
        class _CAN(W.workflow.ContinueAsNewError): pass
        calls.clear(); monkeypatch.setattr(W, "act", fake(_CAN()))
        with pytest.raises(_CAN): await W.RunTasksWorkflow().run(params)
        assert [p["status"] for n, p in calls if n == "index_workflow_activity"] == ["running"]
        # CreateTasksWorkflow: invalid input is a recorded failed execution, not one that never appears
        monkeypatch.setattr(W.workflow, "info", lambda: SimpleNamespace(parent=None, workflow_id="create-1", run_id="r1", workflow_type="CreateTasksWorkflow"))
        calls.clear(); monkeypatch.setattr(W, "act", fake(RuntimeError("unused")))
        with pytest.raises(W.ApplicationError, match="sources is empty"): await W.CreateTasksWorkflow().run({"sources": []})
        rows = [p for n, p in calls if n == "index_workflow_activity"]
        assert [r["status"] for r in rows] == ["running", "failed"] and "sources is empty" in rows[-1]["failure"] and rows[-1]["workflow_id"] == "create-1"
        # ... and a probe outage on a valid request
        calls.clear()
        async def probe_down(name, p, minutes=10, **kw):
            calls.append((name, p))
            if name == "probe_models_activity": raise RuntimeError("provider probe timed out")
            return {}
        monkeypatch.setattr(W, "act", probe_down)
        with pytest.raises(RuntimeError): await W.CreateTasksWorkflow().run({"sources": [{"kind": "swebench-annotated", "pool": "A"}], "judge_model": "j", "draft_model": "d"})
        names = [n for n, _ in calls]
        assert names.index("index_workflow_activity") < names.index("probe_models_activity")
        assert [p["status"] for n, p in calls if n == "index_workflow_activity"] == ["running", "failed"]
        # executions started before the patch replay without the wrapper's rows
        monkeypatch.setattr(W, "_patched", lambda name: name != W.PATCH_LIFECYCLE)
        calls.clear(); monkeypatch.setattr(W, "act", fake(RuntimeError("control pool down")))
        with pytest.raises(RuntimeError): await W.RunTasksWorkflow().run(params)
        assert not [n for n, _ in calls if n == "index_workflow_activity"]
    asyncio.run(scenario())


def test_operator_workflows_record_the_resolved_models_after_the_probe(monkeypatch):
    """The Workflows page shows the models actually used: once probe_models resolves the roles, a second `running` row carries
    `resolved_models` and `substitutions` and its role keys name the resolved ids (behind PATCH_RESOLVED; pre-patch executions
    replay without it)."""
    from types import SimpleNamespace
    async def scenario():
        calls = []
        probe = {"resolved": {"judge_model": "openai/gpt-5.5", "draft_model": "anthropic/claude-sonnet-5", "stakeholder_model": "openai/gpt-5.5"},
                 "substitutions": {"judge_model": "openai/gpt-5.5"}, "notes": ["judge j unavailable"], "needs_human": [], "solver_unavailable": ["m"]}
        async def act(name, p, minutes=10, **kw):
            calls.append((name, p))
            if name == "probe_models_activity": return probe
            if name == "frontier_gate_activity": return {"allowed": True, "scenario_ids": list(p.get("scenario_ids") or []), "reasons": []}
            if name == "prepare_candidate_plan_activity": raise RuntimeError("stopped after the probe for the test")
            return {}
        monkeypatch.setattr(W, "act", act)
        monkeypatch.setattr(W.workflow, "info", lambda: SimpleNamespace(parent=None, workflow_id="run-1", run_id="r1", workflow_type="RunTasksWorkflow"))
        run_params = {"scenario_ids": ["s1"], "models": ["m"], "judge_model": "j", "stakeholder_url": "https://s"}
        r = await W.RunTasksWorkflow().run(run_params)
        assert r["stopped_reason"] == "model-unavailable"
        rows = [p for n, p in calls if n == "index_workflow_activity"]
        assert [x["status"] for x in rows] == ["running", "running", "completed"]
        assert "resolved_models" not in rows[0]["params_summary"] and rows[0]["params_summary"]["judge_model"] == "j"
        assert rows[1]["params_summary"]["resolved_models"] == probe["resolved"] and rows[1]["params_summary"]["substitutions"] == probe["substitutions"]
        assert rows[1]["params_summary"]["judge_model"] == "openai/gpt-5.5" and rows[1]["params_summary"]["models"] == ["m"]
        names = [n for n, _ in calls]
        assert names.index("probe_models_activity") < names.index("index_workflow_activity", names.index("probe_models_activity"))
        # CreateTasksWorkflow: the same row after its probe, before any mining
        monkeypatch.setattr(W.workflow, "info", lambda: SimpleNamespace(parent=None, workflow_id="create-1", run_id="r1", workflow_type="CreateTasksWorkflow"))
        calls.clear()
        with pytest.raises(RuntimeError, match="stopped after the probe"):
            await W.CreateTasksWorkflow().run({"sources": [{"kind": "swebench-annotated", "pool": "A"}], "judge_model": "j", "draft_model": "d"})
        rows = [p for n, p in calls if n == "index_workflow_activity"]
        assert [x["status"] for x in rows] == ["running", "running", "failed"]
        assert rows[1]["params_summary"]["resolved_models"] == probe["resolved"] and rows[1]["params_summary"]["draft_model"] == "anthropic/claude-sonnet-5"
        names = [n for n, _ in calls]
        assert names.index("probe_models_activity") < names.index("index_workflow_activity", names.index("probe_models_activity")) < names.index("mine_activity")
        # executions started before the patch replay without the extra row
        monkeypatch.setattr(W, "_patched", lambda name: name != W.PATCH_RESOLVED)
        monkeypatch.setattr(W.workflow, "info", lambda: SimpleNamespace(parent=None, workflow_id="run-1", run_id="r1", workflow_type="RunTasksWorkflow"))
        calls.clear(); await W.RunTasksWorkflow().run(run_params)
        assert [p["status"] for n, p in calls if n == "index_workflow_activity"] == ["running", "completed"]
    asyncio.run(scenario())


def test_candidate_hooks_carry_source_key_and_scenario_and_tolerate_failure(monkeypatch):
    from types import SimpleNamespace
    async def scenario():
        monkeypatch.setattr(W.workflow, "info", lambda: SimpleNamespace(parent=None, workflow_id="create:a--1", run_id="r1", workflow_type="CandidateWorkflow"))
        hooks = []
        async def act(name, p, minutes=10, **kw):
            if name.startswith("index_"):
                hooks.append((name, p))
                if name == "index_scenario_activity": raise RuntimeError("index down")
                return {"indexed": True}
            if name == "judge_one_activity": return {"judged_path": "j", "instance_id": p["instance_id"], "scenario_id": "a--1", "keep": True, "reasons": []}
            if name == "probe_candidate_activity": return {"judged_path": "j", "fail_to_pass": ["t"], "pass_to_pass": [], "reproducible": True}
            if name == "draft_activity": return {"scenario_id": "a--1", "draft_path": "d"}
            if name == "auto_approve_activity": return {"approved": False, "spec_hash": "h", "reasons": ["judge did not accept"]}
            return dict(BIG)
        monkeypatch.setattr(W, "act", act)
        w = W.CandidateWorkflow()
        r = await w.run({"instance_id": "a__1", "candidates_path": "/c/x.jsonl", "source_key": "github-repo:x/a", "judge_model": "j", "draft_model": "d"})
        assert r["status"] == "rejected"                                                                     # the outcome ignores the failing scenario hook
        cand = [p for n, p in hooks if n == "index_candidate_activity"]
        assert cand and all(p == {"source_key": "github-repo:x/a", "candidates_path": "/c/x.jsonl", "instance_id": "a__1"} for p in cand)
        scen = [p for n, p in hooks if n == "index_scenario_activity"]
        assert scen and all(p == {"scenario_id": "a--1", "version": 1} for p in scen)
        wf = [p for n, p in hooks if n == "index_workflow_activity"]
        assert [p["status"] for p in wf] == ["running", "completed"] and wf[-1]["workflow_id"] == "create:a--1" and wf[-1]["params_summary"]["instance_id"] == "a__1"
        assert w.progress()["indexed"] is False                                                             # the failed scenario hook is visible
    asyncio.run(scenario())


# ---------------------------------------------------------------- candidate decision receipts (P1-7): one per terminal path

CAND = {"instance_id": "a__1", "candidates_path": "/c/x.jsonl", "source_key": "github-repo:x/a", "judge_model": "j", "draft_model": "d"}
CAND_IDENT = {"workflow_id": "create:a--1", "run_id": "r1"}
CAND_ACTS = {"judge_one_activity": {"judged_path": "j", "instance_id": "a__1", "scenario_id": "a--1", "keep": True, "reasons": []},
             "probe_candidate_activity": {"judged_path": "j", "fail_to_pass": ["t"], "pass_to_pass": [], "reproducible": True},
             "draft_activity": {"scenario_id": "a--1", "draft_path": "d"},
             "auto_approve_activity": {"approved": True, "spec_hash": "h", "reasons": []},
             "precheck_draft_activity": {"ok": True, "spec_hash": "h", "reasons": []},
             "verify_approval_activity": {"scenario_id": "a--1", "spec_hash": "h"},
             "compile_activity": {"compile_path": "c", "controls": []},
             "validate_activity": {"status": "valid", "failures": []}}


def _candidate_acts(monkeypatch, receipts: list, over: dict | None = None, receipt_error: BaseException | None = None):
    """Every CandidateWorkflow activity kept/valid unless `over` says otherwise (a BaseException value is raised);
    index_candidate_event_activity payloads land in `receipts`."""
    from types import SimpleNamespace
    monkeypatch.setattr(W.workflow, "info", lambda: SimpleNamespace(parent=None, workflow_type="CandidateWorkflow", **CAND_IDENT))
    async def act(name, p, minutes=10, **kw):
        if name == "index_candidate_event_activity":
            receipts.append(p)
            if receipt_error is not None: raise receipt_error
            return {"indexed": True, "rows": 2, "event_id": "e" * 64}
        if name.startswith("index_") or name == "audit_scenario_activity": return {"indexed": True}
        r = {**CAND_ACTS, **(over or {})}.get(name, {})
        if isinstance(r, BaseException): raise r
        return dict(r)
    monkeypatch.setattr(W, "act", act)


def _receipt(phase, stage, status, gate, reasons, *, scenario_id="a--1", spec_hash=None, result_status=None) -> dict:
    """The exact payload `candidate_receipt` sends: ids from the params, the decision, the workflow identity. A body run as a plain
    coroutine has no `workflow.now()`, so `occurred_at` is None and the activity stamps its own time."""
    return {"source_key": "github-repo:x/a", "instance_id": "a__1", "candidates_path": "/c/x.jsonl", "scenario_id": scenario_id, "version": 1,
            "spec_hash": spec_hash, "phase": phase, "stage": stage, "status": status, "gate": gate, "reasons": reasons, "terminal": True,
            "result_status": result_status, "occurred_at": None, **CAND_IDENT}


@pytest.mark.parametrize("over,mode,status,expected", [
    ({"judge_one_activity": {**CAND_ACTS["judge_one_activity"], "keep": False, "reasons": ["no non-derivable gap"]}}, "auto", "rejected",
     _receipt("judge", "judged", "rejected", "judge", ["no non-derivable gap"])),
    ({"judge_one_activity": {**CAND_ACTS["judge_one_activity"], "keep": False, "reasons": []}}, "auto", "rejected",
     _receipt("judge", "judged", "rejected", "judge", ["judge did not keep the candidate"])),
    ({"probe_candidate_activity": {"judged_path": "j", "fail_to_pass": [], "reason": "no test fails on base"}}, "auto", "not-reproducible-offline",
     _receipt("probe", "probed", "rejected", "probe", ["no test fails on base"])),
    ({"precheck_draft_activity": {"ok": False, "spec_hash": "p", "reasons": ["schema: brief missing"]}}, "hold", "rejected",
     _receipt("precheck", "drafted", "rejected", "precheck", ["schema: brief missing"], spec_hash="p")),
    ({"auto_approve_activity": {"approved": False, "spec_hash": "h", "reasons": ["controls incomplete"]}}, "auto", "rejected",
     _receipt("auto-approval", "approved", "rejected", "auto-approval", ["controls incomplete"])),
    ({"validate_activity": {"status": "invalid", "failures": ["oracle earned 0"]}}, "auto", "invalid",
     _receipt("validate", "validated", "rejected", "validation", ["oracle earned 0"], spec_hash="h", result_status="invalid")),
    ({}, "auto", "valid", _receipt("register", "registered", "kept", None, [], spec_hash="h")),
], ids=["judge-reject", "judge-reject-default-reason", "probe-not-reproducible", "precheck-reject", "auto-approval-reject", "validation-invalid", "registered"])
def test_candidate_workflow_leaves_one_decision_receipt_per_terminal_path(monkeypatch, tmp_path, over, mode, status, expected):
    from swesim.index import activities as IA
    from swesim.settings import Settings
    async def scenario():
        receipts = []; _candidate_acts(monkeypatch, receipts, over)
        w = W.CandidateWorkflow()
        r = await w.run({**CAND, "approval_mode": mode})
        assert r["status"] == status
        assert receipts == [expected]                                                     # exactly one receipt, the whole payload
        assert w.progress()["receipt"] == {"indexed": True, "rows": 2, "event_id": "e" * 64}
        # the payload is what the activity's contract accepts (stage/status literals, required ids), without Temporal
        settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_WORKSPACE": str(tmp_path / "record")})
        row = IA.candidate_event_row(receipts[0], settings)
        assert (row.stage, row.status, row.gate, row.terminal, row.workflow_id, row.run_id) == (expected["stage"], expected["status"], expected["gate"], True, "create:a--1", "r1")
    asyncio.run(scenario())


def test_hold_mode_timeout_and_human_rejection_leave_receipts(monkeypatch):
    async def scenario():
        receipts = []; _candidate_acts(monkeypatch, receipts)
        async def timeout(predicate, timeout): raise TimeoutError()
        monkeypatch.setattr(W.workflow, "wait_condition", timeout)
        w = W.CandidateWorkflow(); r = await w.run({**CAND, "approval_mode": "hold"})
        assert r["status"] == "approval-timeout"
        assert receipts == [_receipt("hold", "approved", "rejected", "approval-timeout", ["no reviewer decision within the hold window"], spec_hash="h")]
        receipts.clear(); w = W.CandidateWorkflow()
        async def reject(predicate, timeout):
            w.approve({"scenario_id": "a--1", "spec_hash": "h", "decision": "reject", "note": "not grounded"}); assert predicate()
        monkeypatch.setattr(W.workflow, "wait_condition", reject)
        r = await w.run({**CAND, "approval_mode": "hold"})
        assert r["status"] == "rejected-by-human"
        assert receipts == [_receipt("human-review", "approved", "rejected", "human-review", ["not grounded"], spec_hash="h")]
    asyncio.run(scenario())


def test_infrastructure_error_leaves_an_error_receipt_naming_the_funnel_stage(monkeypatch):
    from temporalio.exceptions import ApplicationError
    from swesim.index import models as IM
    async def scenario():
        receipts = []; _candidate_acts(monkeypatch, receipts, {"probe_candidate_activity": RuntimeError("modal down")})
        with pytest.raises(ApplicationError, match="failed at stage probing"):
            await W.CandidateWorkflow().run(dict(CAND))
        assert receipts == [_receipt("error", "probed", "error", "probing", ["probing: RuntimeError: modal down"])]
    asyncio.run(scenario())
    assert set(W.FUNNEL_OF_STAGE.values()) <= set(IM.FunnelStage.__args__) and W.FUNNEL_OF_STAGE.get("starting", "failed") == "failed"
    assert {W.FUNNEL_OF_STAGE[s] for s in ("pre-checking", "re-drafting")} == {"drafted"} and {W.FUNNEL_OF_STAGE[s] for s in ("auto-approving", "awaiting-approval")} == {"approved"}


def test_receipt_failure_never_changes_the_outcome_and_pre_patch_executions_skip_it(monkeypatch):
    async def scenario():
        receipts = []
        _candidate_acts(monkeypatch, receipts, {"judge_one_activity": {**CAND_ACTS["judge_one_activity"], "keep": False}}, receipt_error=RuntimeError("index down"))
        w = W.CandidateWorkflow(); r = await w.run(dict(CAND))
        assert r["status"] == "rejected" and len(receipts) == 1                         # the outcome ignores the failing receipt
        assert w.progress()["receipt"]["indexed"] is False and "index down" in w.progress()["receipt"]["reason"]
        monkeypatch.setattr(W, "_patched", lambda name: name != W.PATCH_RECEIPTS)         # an execution started before the patch
        receipts.clear()
        r = await W.candidate_receipt(CAND, "judge", "judged", "rejected", "judge", ["x"])
        assert r == {"indexed": False, "reason": "execution predates the candidate receipts"} and receipts == []
    asyncio.run(scenario())


def test_pipeline_inline_decisions_leave_receipts_naming_the_mined_source(monkeypatch):
    async def scenario():
        receipts = []
        async def act(name, p, minutes=10, **kw):
            if name == "index_candidate_event_activity": receipts.append(p); return {"indexed": True}
            if name == "auto_approve_activity": return {"approved": False, "spec_hash": "h", "reasons": ["controls incomplete"]}
            if name == "mine_activity": return {"candidates_path": "/c/swebench-annotated:a.jsonl"}
            return dict(BIG)
        monkeypatch.setattr(W, "act", act)
        r = await W.PipelineWorkflow().run({"max_candidates_tried": 1})
        assert r["scenarios"][0]["status"] == "auto-approval-rejected"
        assert receipts == [{"source_key": "swebench-annotated:a", "instance_id": "a", "candidates_path": "/c/swebench-annotated:a.jsonl", "scenario_id": "aaa",
                             "version": 1, "spec_hash": "h", "phase": "auto-approval", "stage": "approved", "status": "rejected", "gate": "auto-approval",
                             "reasons": ["controls incomplete"], "terminal": True, "result_status": None, "occurred_at": None, "workflow_id": None, "run_id": None}]
        receipts.clear()
        async def wait_timeout(predicate, timeout): raise TimeoutError()
        monkeypatch.setattr(W.workflow, "wait_condition", wait_timeout)
        r = await W.PipelineWorkflow().run({"approval_mode": "signal"})
        assert r["status"] == "awaiting-approval-timeout"
        assert [(p["phase"], p["stage"], p["status"], p["gate"], p["reasons"], p["scenario_id"], p["source_key"]) for p in receipts] == \
               [("hold", "approved", "rejected", "approval-timeout", ["no reviewer decision within the hold window"], "aaa", "swebench-annotated:a")]
    asyncio.run(scenario())


def test_env_candidate_receipt_reaches_the_real_index_activity_and_the_record(tmp_path, monkeypatch):
    """CandidateWorkflow -> index_candidate_event_activity (swesim.index.activities.ALL on the control queue) -> the immutable receipt
    snapshot in the record, carrying the child's workflow/run ids and `workflow.now()`. The SQL index is disabled here: the record alone
    keeps the decision."""
    from temporalio import activity
    from swesim.index import activities as IA
    from swesim.settings import reset_settings
    from swesim.storage import history as H
    from swesim.storage.layout import Layout
    for key in ("SWESIM_DATABASE_URL", "SWESIM_REGISTRY", "SWESIM_BUCKET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SWESIM_ENV_FILE", str(tmp_path / "none.env")); monkeypatch.setenv("SWESIM_WORKSPACE", str(tmp_path / "record"))
    reset_settings(); IA.reset_store()
    @activity.defn(name="judge_one_activity")
    async def judge(p): return {"judged_path": "j", "instance_id": p["instance_id"], "scenario_id": "a--1", "keep": False, "reasons": ["no non-derivable gap"]}
    async def scenario(env):
        async with workers(env, workflows=[W.CandidateWorkflow], activities=[judge, *IA.ALL]):
            handle = await env.client.start_workflow(W.CandidateWorkflow.run, dict(CAND), id="create:a--1", task_queue=Q.AUTHORING)
            r = await handle.result()
            assert r["status"] == "rejected"
            receipt = (await handle.query(W.CandidateWorkflow.progress))["receipt"]
            assert receipt["indexed"] is False and receipt["reason"] == "index disabled" and len(receipt["event_id"]) == 64
            rows = H.read_rows(Layout(root=tmp_path / "record"), "candidate_events")
            assert [row.event_id for row in rows] == [receipt["event_id"]]
            row = rows[0]
            assert (row.source_key, row.instance_id, row.scenario_id, row.phase, row.stage, row.status, row.gate, row.reasons, row.terminal) == \
                   ("github-repo:x/a", "a__1", "a--1", "judge", "judged", "rejected", "judge", ["no non-derivable gap"], True)
            assert row.workflow_id == "create:a--1" and row.run_id == handle.result_run_id and row.occurred_at.tzinfo is not None
    try:
        _run_in_env(scenario)
    finally:
        reset_settings(); IA.reset_store()


# ---------------------------------------------------------------- fixtures for activities

@pytest.fixture
def ws(tmp_path, monkeypatch):
    w = Workspace(tmp_path / "workspace", tmp_path / "registry.json")
    monkeypatch.setattr(A, "WS", w)
    monkeypatch.setattr(A, "LAUNCHER", SYS.SystemdLauncher(workspace=w.root, registry_path=w.registry_path, env_file=tmp_path / "env", repo_root=tmp_path))
    monkeypatch.setattr(A, "POLL_SECONDS", 0.0); monkeypatch.setattr(A, "LAUNCH_GRACE_POLLS", 2)
    return w


@pytest.fixture
def systemd(monkeypatch):
    fake = FakeSystemd(); monkeypatch.setattr(SYS, "RUN", fake); return fake


def frozen_scenario(ws: Workspace, sid="example-scenario", compiled_hash="c" * 64):
    spec = ScenarioSpec.model_validate(minimal_spec_dict()); sdir = ws.scenario_dir(sid); sdir.mkdir(parents=True)
    atomic_json(sdir / "scenario.json", spec.model_dump(mode="json"))
    atomic_json(sdir / "approval.json", {"spec_hash": spec.spec_hash, "approved_by": "auto-validators", "review_needed": True})
    atomic_json(sdir / "compile.json", {"compiled_hash": compiled_hash, "spec_hash": spec.spec_hash, "image_digest": "sha256:abc"})
    return spec


def trial_request(ws, spec, model="anthropic/claude-haiku-4-5-20251001", condition="interactive", attempt=0):
    rc = RunConfig(condition=condition, model=model, compiled_hash="c" * 64, brief_profile="natural")
    tid = trial_id(spec.spec_hash, condition, rc.run_config_hash, attempt)
    return dict(trial_id=tid, scenario_id=spec.id, spec_hash=spec.spec_hash, condition=condition, model=model,
                run_config=rc.model_dump(mode="json"), run_config_hash=rc.run_config_hash, attempt_index=attempt, stakeholder_url="https://stake.example")


# ---------------------------------------------------------------- launch once through the activities module (the launcher protocol itself: tests/test_launchers.py)

class _Records(logging.Handler):
    def __init__(self): super().__init__(level=logging.INFO); self.messages: list[str] = []
    def emit(self, record): self.messages.append(record.getMessage())


def test_activities_launch_once_uses_the_configured_launcher_and_stamps(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec)
    records = _Records(); A.log.addHandler(records); A.log.setLevel(logging.INFO)
    try:
        launched = asyncio.run(A.launch_trial_activity({**p}))
        assert launched["launched"] is True
        assert any(f"decision=launch trial={p['trial_id']}" in m and "launcher=systemd" in m for m in records.messages)   # the Cloud Logging metric's literals
        records.messages.clear()
        assert asyncio.run(A.launch_trial_activity({**p}))["decision"] == "reconcile"
        assert any(f"decision=reconcile trial={p['trial_id']}" in m for m in records.messages)
    finally:
        A.log.removeHandler(records)
    # the plain function path (no activity wrapper) behaves the same and writes launch.json once
    import shutil; shutil.rmtree(ws.root / "trials"); systemd.units.clear(); systemd.calls.clear()
    first = A.launch_once(p)
    assert first["decision"] == "launch" and first["launched"] is True and first["launcher"] == "systemd" and first["unit"] == "swesim-trial-" + p["trial_id"]
    assert first["worker_image_digest"] == A.SETTINGS.worker_image_digest and first["origin"] == A.SETTINGS.origin
    launch = json.loads((ws.root / "trials" / p["trial_id"] / "launch.json").read_text())
    assert launch["phase"] == "acknowledged" and launch["launcher"] == "systemd" and launch["origin"] == A.SETTINGS.origin
    assert A.launch_once(p)["decision"] == "reconcile" and len([c for c in systemd.calls if c[0] == "systemd-run"]) == 1
    assert A.trial_dir(p["trial_id"]) == ws.root / "trials" / p["trial_id"]
    r = asyncio.run(A.launch_trial_activity({**p}))            # the activity wrapper stamps the result too
    assert r["decision"] == "reconcile" and r["origin"] == A.SETTINGS.origin


def test_activity_wrapper_turns_value_errors_into_non_retryable_failures(ws, systemd):
    from temporalio.exceptions import ApplicationError
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]
    atomic_json(d / "request.json", {**p, "model": "someone-else"})
    with pytest.raises(ApplicationError) as info: asyncio.run(A.launch_trial_activity(p))
    assert info.value.non_retryable is True and info.value.type == "TrialBindingConflict"
    # infrastructure failures stay retryable (a RuntimeError subclass propagates unchanged)
    systemd.systemctl_failure = "Failed to connect to bus"; atomic_json(d / "request.json", p)
    with pytest.raises(A.UnitStateUnavailable): asyncio.run(A.launch_trial_activity(p))


# ---------------------------------------------------------------- await_trial_activity outcomes

def test_await_returns_record_when_present(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]
    atomic_json(d / "record.json", {"trial_id": p["trial_id"], "status": "completed", "reward": {"reward": 1.0, "success": 1}})
    r = asyncio.run(A.await_trial_activity(p))
    assert r == stamped({"trial_id": p["trial_id"], "status": "completed", "reward": {"reward": 1.0, "success": 1}})


def test_await_reports_runner_error_launch_failed_and_unresolved(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]
    atomic_json(d / "runner-error.json", {"trial_id": p["trial_id"], "error": "ValueError('frozen spec changed')"})
    assert asyncio.run(A.await_trial_activity(p))["status"] == "runner-error"
    (d / "runner-error.json").unlink()
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": "u", "phase": "acknowledged", "returncode": 1, "launcher": "systemd"})
    assert asyncio.run(A.await_trial_activity(p))["status"] == "launch-failed"
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": "u", "phase": "acknowledged", "returncode": 0, "launcher": "systemd"})
    r = asyncio.run(A.await_trial_activity(p))
    assert r["status"] == "unresolved" and "never relaunch" in r["reason"]


def test_await_with_no_launch_evidence_reports_launch_failed_after_grace(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec)
    r = asyncio.run(A.await_trial_activity(p))
    assert r["status"] == "launch-failed" and "no launch evidence" in r["reason"]


def test_await_polls_running_unit_then_ingests(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]
    unit = "swesim-trial-" + p["trial_id"]
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": unit, "phase": "acknowledged", "returncode": 0, "launcher": "systemd"})
    systemd.units[unit] = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success"}
    polls = {"n": 0}
    real_read = A.read_evidence
    def read_evidence(tid):
        polls["n"] += 1
        if polls["n"] == 3:
            atomic_json(d / "record.json", {"trial_id": tid, "status": "completed", "reward": {"reward": 0.0, "success": 0}})
            systemd.units[unit]["SubState"] = "exited"
        return real_read(tid)
    A.read_evidence = read_evidence
    try: r = asyncio.run(A.await_trial_activity(p))
    finally: A.read_evidence = real_read
    assert r["status"] == "completed" and polls["n"] == 3


def test_await_unit_failed_without_record_is_unit_failed(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]; unit = "swesim-trial-" + p["trial_id"]
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": unit, "phase": "acknowledged", "returncode": 0, "launcher": "systemd"})
    systemd.units[unit] = {"LoadState": "loaded", "ActiveState": "failed", "SubState": "failed", "Result": "exit-code"}
    r = asyncio.run(A.await_trial_activity(p))
    assert r["status"] == "unit-failed" and r["unit"] == unit


def test_await_terminal_status_without_record_returns_status_unless_unit_still_running(ws, systemd):
    # Fix round 1, Important 1: a terminal status.json with no record.json must not poll to the 3 h deadline.
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]; unit = "swesim-trial-" + p["trial_id"]
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": unit, "phase": "acknowledged", "returncode": 0, "launcher": "systemd"})
    atomic_json(d / "status.json", {"status": "launch-failed", "trial_id": p["trial_id"]})
    r = asyncio.run(A.await_trial_activity(p))                       # unit gone: the terminal status is the answer, now
    assert r["status"] == "launch-failed" and r["reward"] == {} and "record.json" in r["reason"]
    atomic_json(d / "status.json", {"status": "completed", "trial_id": p["trial_id"]})
    systemd.units[unit] = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success"}
    polls = {"n": 0}; real_read = A.read_evidence
    def read_evidence(tid):                                           # unit still running: keep polling until the record lands
        polls["n"] += 1
        if polls["n"] == 3: atomic_json(d / "record.json", {"trial_id": tid, "status": "completed", "reward": {"reward": 1.0, "success": 1}})
        return real_read(tid)
    A.read_evidence = read_evidence
    try: r = asyncio.run(A.await_trial_activity(p))
    finally: A.read_evidence = real_read
    assert r["status"] == "completed" and r["reward"]["success"] == 1 and polls["n"] == 3
    assert not hasattr(A, "TERMINAL_STATUSES")


def test_await_tolerates_a_partially_written_record_while_the_unit_runs(ws, systemd):
    # run_trial writes record.json non-atomically: an empty/partial file is "still being written" while the unit runs.
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]; unit = "swesim-trial-" + p["trial_id"]
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": unit, "phase": "acknowledged", "returncode": 0, "launcher": "systemd"})
    systemd.units[unit] = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "Result": "success"}
    (d / "record.json").write_text("")
    polls = {"n": 0}; real_read = A.read_evidence
    def read_evidence(tid):
        polls["n"] += 1
        if polls["n"] == 3: (d / "record.json").write_text(json.dumps({"trial_id": tid, "status": "completed", "reward": {"reward": 0.5, "success": 0}}))
        return real_read(tid)
    A.read_evidence = read_evidence
    try: r = asyncio.run(A.await_trial_activity(p))
    finally: A.read_evidence = real_read
    assert r["status"] == "completed" and r["reward"]["reward"] == 0.5 and polls["n"] == 3
    # unit finished and the record is still unreadable: incomplete evidence, reported, never relaunched
    (d / "record.json").write_text("{partial"); systemd.units[unit]["SubState"] = "exited"
    r2 = asyncio.run(A.await_trial_activity(p))
    assert r2["status"] == "unknown" and "record.json unreadable" in r2["reason"] and r2["reward"] == {}


def test_await_systemctl_failure_is_retryable_error_not_unit_gone(ws, systemd):
    # Fix round 1, Important 2: a failing systemctl must never read as "unit not found".
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]; unit = "swesim-trial-" + p["trial_id"]
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "unit": unit, "phase": "acknowledged", "returncode": 0, "launcher": "systemd"})
    systemd.systemctl_failure = "Failed to connect to bus: No medium found"
    with pytest.raises(A.UnitStateUnavailable, match="Failed to connect to bus"): A.read_evidence(p["trial_id"])
    with pytest.raises(A.UnitStateUnavailable): asyncio.run(A.await_trial_activity(p))
    assert not [c for c in systemd.calls if c[0] == "systemd-run"]


def test_await_launch_by_another_runtime_is_unresolved_not_relaunched(ws, systemd):
    # A VM-era record (launcher systemd) read by a worker configured for another launcher: reconcile by hand, never relaunch.
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]
    atomic_json(d / "launch.json", {"trial_id": p["trial_id"], "phase": "acknowledged", "returncode": 0, "launcher": "cloudrun", "handle": "swesim-trial-x"})
    r = asyncio.run(A.await_trial_activity(p))
    assert r["status"] == "unresolved" and "cannot be seen" in r["reason"] and "launched by cloudrun" in r["runtime"]
    assert A.launch_once(p)["decision"] == "reconcile" and not [c for c in systemd.calls if c[0] == "systemd-run"]


def test_cancel_and_cleanup_activities_follow_the_evidence(ws, systemd):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]; unit = "swesim-trial-" + p["trial_id"]
    assert asyncio.run(A.cancel_trial_activity(p))["cancelled"] is False                       # nothing launched: nothing to stop
    A.launch_once(p)
    r = asyncio.run(A.cleanup_trial_activity(p))
    assert r["cleaned"] is False and "decision=reconcile" in r["reason"] and "runtime=running" in r["reason"]   # the runtime is still the evidence
    c = asyncio.run(A.cancel_trial_activity(p))
    assert c["cancelled"] is True and c["launcher"] == "systemd" and c["handle"] == unit and ["systemctl", "--user", "stop", unit] in systemd.calls
    assert asyncio.run(A.cleanup_trial_activity(p))["cleaned"] is False                        # stopped without a record: still reconcile-by-hand evidence
    atomic_json(d / "record.json", {"trial_id": p["trial_id"], "status": "completed", "reward": {"reward": 1.0, "success": 1}})
    done = asyncio.run(A.cleanup_trial_activity(p))
    assert done["cleaned"] is True and done["launcher"] == "systemd" and done["origin"] == A.SETTINGS.origin


def test_spend_check_sums_cost_files_and_compares_with_the_cap(ws, monkeypatch):
    from decimal import Decimal
    trials = ws.root / "trials"
    atomic_json(trials / ("a" * 24) / "costs.json", [{"role": "solver", "usd": 1.25}, {"role": "stakeholder", "usd": 0.25}])
    atomic_json(trials / ("b" * 24) / "costs.json", [{"role": "solver", "usd": None, "price_source": "unknown"}])
    (trials / ("c" * 24)).mkdir(); (trials / ("c" * 24) / "costs.json").write_text("{not json")
    r = asyncio.run(A.spend_check_activity({}))
    assert r["spend_usd"] == 1.5 and r["records"] == 3 and r["unpriced_records"] == 1 and r["source"] == "files"
    assert r["cap_usd"] is None and r["exceeded"] is False                                           # no cap configured: never exceeded
    assert asyncio.run(A.spend_check_activity({"cap_usd": 1.0})) | {} == {**r, "cap_usd": 1.0, "exceeded": True} | {}
    monkeypatch.setattr(A, "SETTINGS", A.SETTINGS.model_copy(update={"spend_cap_usd": Decimal("2")}))
    assert asyncio.run(A.spend_check_activity({}))["exceeded"] is False and asyncio.run(A.spend_check_activity({"cap_usd": 1.4}))["exceeded"] is True


def test_record_cost_activity_is_idempotent_and_reports_missing_pieces(ws, monkeypatch):
    import swesim
    from types import SimpleNamespace
    spec = frozen_scenario(ws); p = trial_request(ws, spec); tid = p["trial_id"]; d = ws.root / "trials" / tid
    monkeypatch.setitem(sys.modules, "swesim.costs", None)                                   # a worker without the models stream's module
    monkeypatch.setattr(swesim, "costs", None, raising=False)
    r = asyncio.run(A.record_cost_activity({"trial_id": tid}))
    assert r["recorded"] is False and "swesim.costs" in r["reason"]
    written = []
    def record_cost(layout, trial_id_, workflow_id, role, model_id, usage):
        rec = {"role": role, "model": model_id, "usd": 0.42, "price_source": "table", "input_tokens": usage["input"], "output_tokens": usage["output"]}
        path = layout.trial_dir(trial_id_) / "costs.json"; rows = json.loads(path.read_text()) if path.exists() else []; rows.append(rec); atomic_json(path, rows)
        written.append((trial_id_, workflow_id, role, model_id)); return SimpleNamespace(usd=0.42, price_source="table")
    fake_costs = fake_module("swesim.costs", solver_usage_from_trajectory=lambda traj: {"input": traj["in"], "output": traj["out"]}, record_cost=record_cost)
    monkeypatch.setitem(sys.modules, "swesim.costs", fake_costs)
    monkeypatch.setattr(swesim, "costs", fake_costs, raising=False)
    assert asyncio.run(A.record_cost_activity({"trial_id": tid}))["reason"] == "no record.json"
    atomic_json(d / "record.json", {"trial_id": tid, "status": "completed", "model": p["model"], "reward": {"reward": 1.0, "success": 1}})
    assert asyncio.run(A.record_cost_activity({"trial_id": tid}))["reason"] == "no trajectory or agent result to read usage from"
    atomic_json(d / "trajectory.json", {"in": 1000, "out": 50})
    r = asyncio.run(A.record_cost_activity({"trial_id": tid, "workflow_id": tid}))
    assert r["recorded"] is True and r["usd"] == 0.42 and r["model"] == p["model"] and written == [(tid, tid, "solver", p["model"])]
    again = asyncio.run(A.record_cost_activity({"trial_id": tid}))
    assert again["recorded"] is False and again["reason"] == "solver cost already recorded" and len(written) == 1
    assert json.loads((d / "costs.json").read_text())[0]["input_tokens"] == 1000


def test_record_cost_activity_with_the_real_costs_module_reads_harbor_agent_result(ws):
    """The models stream's swesim.costs: usage from the Harbor trial dir the record names, a CostRecord row in trials/<tid>/costs.json."""
    import swesim.costs as costs
    spec = frozen_scenario(ws); p = trial_request(ws, spec, model="anthropic/claude-haiku-4-5-20251001"); tid = p["trial_id"]; d = ws.root / "trials" / tid
    harbor = ws.root / "jobs" / "runs" / tid / "trial-1"; harbor.mkdir(parents=True)
    (harbor / "result.json").write_text(json.dumps({"agent_result": {"n_input_tokens": 12000, "n_output_tokens": 800, "n_cache_tokens": 2000, "cost_usd": 0.05}}))
    atomic_json(d / "record.json", {"trial_id": tid, "status": "completed", "model": p["model"], "trial_dir": str(harbor), "reward": {"reward": 1.0, "success": 1}})
    r = asyncio.run(A.record_cost_activity({"trial_id": tid, "workflow_id": tid}))
    assert r["recorded"] is True and r["model"] == p["model"] and r["records"] == 1
    rows = costs.read_costs(d / "costs.json")
    assert len(rows) == 1 and rows[0].role == "solver" and rows[0].trial_id == tid and rows[0].workflow_id == tid
    assert rows[0].input_tokens == 12000 and rows[0].output_tokens == 800 and rows[0].cached_tokens == 2000 and rows[0].provider == "anthropic"
    assert (rows[0].usd is None) == (r["usd"] is None) and r["price_source"] == rows[0].price_source
    assert asyncio.run(A.record_cost_activity({"trial_id": tid}))["reason"] == "solver cost already recorded"
    spend = asyncio.run(A.spend_check_activity({"cap_usd": 1000}))
    assert spend["records"] == 1 and spend["exceeded"] is False and spend["unpriced_records"] == (1 if rows[0].usd is None else 0)


def test_judge_one_records_a_judge_cost_row_when_the_judge_reports_usage(ws, monkeypatch):
    """C2 (models stream): one `judge` cost row per judged candidate through swesim.costs; a ledger problem never fails the judge."""
    import swesim.costs as costs
    def judge_candidate(row, model_id):
        return {"gaps": [], "verified_rubric_score": 2, "keep": True, "model_id": model_id, "usage": {"input_tokens": 5000, "output_tokens": 300, "calls": 1}}
    monkeypatch.setitem(sys.modules, "swesim.stages.judge", fake_module("swesim.stages.judge", judge_candidate=judge_candidate))
    cand = ws.candidates_dir / "github-repo--x__y.jsonl"; cand.parent.mkdir(parents=True)
    cand.write_text(json.dumps({"instance_id": "a__1", "repo": "x/y"}) + "\n" + json.dumps({"instance_id": "b__2", "repo": "x/y"}) + "\n")
    j = asyncio.run(A.judge_one_activity({"candidates_path": str(cand), "instance_id": "a__1", "judge_model": OPUS}))
    assert j["keep"] is True and j["cost"]["recorded"] is True and j["cost"]["workflow_id"] == "direct"   # no Temporal activity context here
    rows = costs.read_costs(ws.root / "costs" / "direct.json")
    assert len(rows) == 1 and rows[0].role == "judge" and rows[0].model.endswith("claude-opus-5") and rows[0].input_tokens == 5000 and rows[0].trial_id is None
    again = asyncio.run(A.judge_one_activity({"candidates_path": str(cand), "instance_id": "a__1", "judge_model": OPUS}))
    assert again["cost"] is None and len(costs.read_costs(ws.root / "costs" / "direct.json")) == 1                          # retained row: no second charge
    bad = asyncio.run(A.judge_one_activity({"candidates_path": str(cand), "instance_id": "b__2", "judge_model": "not-a-model-id"}))
    assert bad["keep"] is True and bad["cost"]["recorded"] is False and "ModelIdError" in bad["cost"]["reason"]              # judged; the ledger problem is reported


def test_register_activity_result_is_json_serializable(ws, monkeypatch):
    # Fix round 1, Important 5.
    spec = frozen_scenario(ws)
    def register_scenario(ws_, spec_): return {"id": spec_.id, "validation_path": Path("/x/validation.json"), "runs": [], "nested": {"p": Path("/y")}}
    monkeypatch.setitem(sys.modules, "swesim.stages.register", fake_module("swesim.stages.register", register_scenario=register_scenario))
    r = asyncio.run(A.register_activity({"scenario_id": spec.id}))
    assert r == stamped({"registry_entry": {"id": spec.id, "validation_path": "/x/validation.json", "runs": [], "nested": {"p": "/y"}}})
    json.dumps(r)


# ---------------------------------------------------------------- plan_trials, verify_approval, judge, probe

def admit(ws, p: dict, *, allowed: bool = True, scenario_ids=None) -> dict:
    """A recorded frontier decision for `p` (tests only): the same file `frontier_gate_activity` writes, so `plan_trials` verifies
    it exactly as it verifies a workflow's. Returns the `admission` reference a RunTasksWorkflow would carry."""
    from swesim.stages import frontier as F
    request = F.request_of(p); path = F.decision_path(ws, F.identity_of(request))
    atomic_json(path, {"request": request, "decision": {"allowed": allowed, "reasons": [] if allowed else ["test refusal"],
                                                         "scenario_ids": request["scenario_ids"] if scenario_ids is None else scenario_ids}})
    return {"evidence_path": str(path)}


def test_plan_trials_distinct_ids_per_model_and_profile_rule(ws):
    spec = frozen_scenario(ws)
    request = {"scenario_id": spec.id, "models": ["anthropic/claude-haiku-4-5-20251001", "openai/gpt-5.5"], "conditions": ["full", "hidden"],
               "k": 2, "purpose": "comparison", "brief_profile": "withheld", "stakeholder_url": "https://s"}
    out = A.plan_trials({**request, "admission": admit(ws, request)})    # gpt-5.5 is Tier E: planned only under a recorded admission
    trials = out["trials"]; assert len(trials) == 8 and len({t["trial_id"] for t in trials}) == 8
    for t in trials:
        rc = RunConfig.model_validate(t["run_config"])
        assert rc.compiled_hash == "c" * 64 and rc.purpose == "comparison" and rc.model == t["model"]
        assert rc.brief_profile == "natural"                   # minimal spec has no withheld brief; full always natural
        assert t["trial_id"] == trial_id(spec.spec_hash, t["condition"], rc.run_config_hash, t["attempt_index"])
        assert t["run_config_hash"] == rc.run_config_hash
    assert {t["model"] for t in trials} == {"anthropic/claude-haiku-4-5-20251001", "openai/gpt-5.5"}
    with pytest.raises(ValueError): A.plan_trials({"scenario_id": spec.id, "models": ["m", "m"], "stakeholder_url": "https://s"})
    # A manual retry is a new attempt index (new trial id), never a Temporal retry of the old one.
    more = A.plan_trials({"scenario_id": spec.id, "models": ["m"], "conditions": ["hidden"], "k": 2, "attempt_offset": 3, "stakeholder_url": "https://s"})["trials"]
    assert [t["attempt_index"] for t in more] == [3, 4] and not {t["trial_id"] for t in more} & {t["trial_id"] for t in trials}


def test_plan_trials_enforces_frontier_admission(ws):
    """Brief §3.2: a Tier D/E request is planned only under a frontier admission; a recorded decision admits exactly the request it
    was made for; Tier A–C requests are untouched."""
    spec = frozen_scenario(ws)
    base = {"scenario_id": spec.id, "models": ["openai/gpt-5.5"], "conditions": ["hidden"], "k": 1, "purpose": "comparison", "stakeholder_url": "https://s"}
    # no evidence: the fresh evaluation refuses and records why, durably, under evidence/sweep
    with pytest.raises(ValueError, match="frontier admission rejected"): A.plan_trials(base)
    written = list(ws.layout.evidence_dir("sweep").glob("frontier-*.json")); assert len(written) == 1
    decision = json.loads(written[0].read_text())["decision"]
    assert decision["allowed"] is False and any("15 current audited scenarios" in r for r in decision["reasons"])
    # a recorded decision binds its request: other k, another scenario, a refusal, or a file outside evidence/sweep do not admit
    with pytest.raises(ValueError, match="different request"): A.plan_trials({**base, "admission": admit(ws, {**base, "k": 3})})
    with pytest.raises(ValueError, match="not admitted"): A.plan_trials({**base, "admission": admit(ws, {**base, "scenario_id": "other-scenario"})})
    with pytest.raises(ValueError, match="frontier admission rejected"): A.plan_trials({**base, "admission": admit(ws, base, allowed=False)})
    with pytest.raises(ValueError, match="under evidence/sweep"): A.plan_trials({**base, "admission": {"evidence_path": str(ws.root / "frontier-x.json")}})
    with pytest.raises(ValueError, match="lacks evidence_path"): A.plan_trials({**base, "admission": {"allowed": True}})
    # a decision that admits the request but excludes this scenario (saturated) refuses the scenario with its reason
    with pytest.raises(ValueError, match="not admitted"): A.plan_trials({**base, "admission": admit(ws, base, scenario_ids=[])})
    # the matching decision plans, and its identity travels with the plan
    out = A.plan_trials({**base, "admission": admit(ws, base)})
    assert len(out["trials"]) == 1 and out["admission"]["evidence_path"].startswith(str(ws.layout.evidence_dir("sweep")))
    # cached plan pages are under the same check: the frontier request's plan is not returned without an admission
    A.WS = ws
    async def go():
        prepared = await A.prepare_trial_plan_activity({**base, "workflow_id": "run-x", "admission": admit(ws, base)})
        assert prepared["total"] == 1
        with pytest.raises(Exception, match="frontier admission rejected"):
            await A.prepare_trial_plan_activity({**base, "workflow_id": "run-x"})
        assert (await A.prepare_trial_plan_activity({**base, "workflow_id": "run-x", "admission": admit(ws, base)}))["total"] == 1
    asyncio.run(go())
    # Tier A–C requests need no admission and carry none
    assert "admission" not in A.plan_trials({**base, "models": ["openrouter/deepseek/deepseek-v3.2"]})


def test_verify_approval_matches_frozen_spec_hash(ws):
    spec = frozen_scenario(ws)
    r = asyncio.run(A.verify_approval_activity({"scenario_id": spec.id, "spec_hash": spec.spec_hash}))
    assert r == stamped({"scenario_id": spec.id, "spec_hash": spec.spec_hash})
    with pytest.raises(Exception, match="approval hash differs"): asyncio.run(A.verify_approval_activity({"scenario_id": spec.id, "spec_hash": "0" * 64}))
    (ws.scenario_dir(spec.id) / "approval.json").unlink()
    with pytest.raises(Exception, match="approval.json"): asyncio.run(A.verify_approval_activity({"scenario_id": spec.id, "spec_hash": spec.spec_hash}))


def fake_module(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items(): setattr(m, k, v)
    return m


def test_judge_activity_is_idempotent_and_filters_keep(ws, monkeypatch):
    calls = []
    def judge_candidate(row, model_id):
        calls.append((row["instance_id"], model_id)); return {"gaps": [], "verified_rubric_score": 2, "keep": row["instance_id"] == "a__1"}
    monkeypatch.setitem(sys.modules, "swesim.stages.judge", fake_module("swesim.stages.judge", judge_candidate=judge_candidate))
    cand = ws.candidates_dir / "swebench-annotated.jsonl"; cand.parent.mkdir(parents=True)
    cand.write_text("".join(json.dumps({"instance_id": i}) + "\n" for i in ("a__1", "b__2", "c__3")))
    r = asyncio.run(A.judge_activity({"candidates_path": str(cand), "judge_model": "openai/gpt-5.5", "max_candidates": 2}))
    assert r["instance_ids"] == ["a__1"] and Path(r["judged_path"]).name == "swebench-annotated.judged.jsonl" and len(calls) == 2
    r2 = asyncio.run(A.judge_activity({"candidates_path": str(cand), "judge_model": "openai/gpt-5.5"}))
    assert r2 == r and len(calls) == 2                       # retained output; no second LLM pass


def test_draft_activity_derives_scenario_id_and_supports_forced_redraft(ws, monkeypatch):
    drafted = []
    def draft(row, judged, model_id, out_dir, **kw):
        drafted.append((row["instance_id"], model_id, kw)); out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "scenario.draft.json").write_text("{}"); return out_dir / "scenario.draft.json"
    monkeypatch.setitem(sys.modules, "swesim.stages.draft_spec", fake_module("swesim.stages.draft_spec", draft=draft))
    judged = ws.candidates_dir / "x.judged.jsonl"; judged.parent.mkdir(parents=True)
    judged.write_text(json.dumps({"instance_id": "Py-Pdf__pypdf-3011", "judge": {"keep": True}}) + "\n")
    r = asyncio.run(A.draft_activity({"judged_path": str(judged), "instance_id": "Py-Pdf__pypdf-3011", "draft_model": "m"}))
    assert r["scenario_id"] == "py-pdf--pypdf-3011" and Path(r["draft_path"]).exists() and len(drafted) == 1
    asyncio.run(A.draft_activity({"judged_path": str(judged), "instance_id": "Py-Pdf__pypdf-3011", "draft_model": "m"}))
    assert len(drafted) == 1                                   # existing draft retained
    r3 = asyncio.run(A.draft_activity({"judged_path": str(judged), "instance_id": "Py-Pdf__pypdf-3011", "draft_model": "m", "force": True, "reject_reasons": ["leak"]}))
    assert len(drafted) == 2 and (Path(r3["draft_path"]).parent / "scenario.draft.rejected-1.json").exists()


def test_mine_activity_adapts_to_either_mine_signature(ws, monkeypatch):
    seen = {}
    def mine_swebench_annotated(ws_, csv_path, limit=None, pytest_repos_only=True):
        seen.update(csv_path=str(csv_path), limit=limit, pytest_repos_only=pytest_repos_only); return ws_.candidates_dir / "swebench-annotated.jsonl"
    def mine_github_repo(ws_, repo, since="2026-06-01", pr=None, max_candidates=20):
        seen.update(repo=repo, pr=pr, since=since); return ws_.candidates_dir / "github-repo--x.jsonl"
    monkeypatch.setitem(sys.modules, "swesim.stages.mine", fake_module("swesim.stages.mine", mine_swebench_annotated=mine_swebench_annotated, mine_github_repo=mine_github_repo))
    r = asyncio.run(A.mine_activity({"source": "swebench-annotated", "limit": 5}))
    assert r["candidates_path"].endswith("swebench-annotated.jsonl") and seen["limit"] == 5 and seen["pytest_repos_only"] is True
    r = asyncio.run(A.mine_activity({"source": "github-repo", "repo": "https://github.com/py-pdf/pypdf", "pr": 12, "since": "2026-07-01", "unknown_key": 1}))
    assert r["candidates_path"].endswith("github-repo--x.jsonl") and seen["pr"] == 12 and seen["since"] == "2026-07-01"


GPT, OPUS, SONNET, HAIKU = "openai/gpt-5.5", "anthropic/claude-opus-5", "anthropic/claude-sonnet-5", "anthropic/claude-haiku-4-5-20251001"
SOL = "openai/gpt-5.6-sol"  # second link of every pipeline-role chain in swesim.roles.ROLE_CHAINS


def _probe_failing(unavailable):
    """A stand-in for roles.probe_id: reachable unless the id is in `unavailable`; the error text carries a fake key, as a provider's would."""
    from swesim.roles import ProbeResult
    def probe_id(model_id, **kw):
        if model_id in unavailable:
            return ProbeResult(id=model_id, provider=model_id.partition("/")[0], reachable=False, latency_ms=12, error_class="AuthenticationError",
                               error=f"401 unauthorized for {model_id} with key fake-openai-key-value-1234", served_provider=None, at="2026-09-13T08:00:00+00:00", usage=None)
        return ProbeResult(id=model_id, provider=model_id.partition("/")[0], reachable=True, latency_ms=12, error_class=None, error=None,
                           served_provider=None, at="2026-09-13T08:00:00+00:00", usage=None)
    return probe_id


def test_probe_chains_are_role_chains():
    from swesim.roles import ROLE_CHAINS
    assert A.FALLBACK_CHAINS is ROLE_CHAINS and {"judge_model", "draft_model", "stakeholder_model", "audit_model", "screen_model"} <= set(ROLE_CHAINS)


def test_probe_chains_gpt_unavailable_judge_becomes_opus_draft_stays_sonnet(ws, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-value-1234")
    monkeypatch.setattr("swesim.roles.probe_id", _probe_failing({GPT}))
    r = A.probe_models({"judge_model": GPT, "draft_model": SONNET, "models": [GPT, HAIKU]})
    three = lambda d: {k: d[k] for k in ("judge_model", "draft_model", "stakeholder_model")}
    assert three(r["resolved"]) == {"judge_model": OPUS, "draft_model": SONNET, "stakeholder_model": OPUS}
    assert r["substitutions"] == {"judge_model": OPUS}
    assert three(r["configured"]) == {"judge_model": GPT, "draft_model": SONNET, "stakeholder_model": OPUS}   # stakeholder default is the chain head
    assert r["resolved"]["screen_model"] == A.FALLBACK_CHAINS["screen_model"][0] and r["resolved"]["audit_model"] == OPUS   # every role in the chains
    assert r["models"][GPT]["ok"] is False and r["models"][OPUS]["ok"] is True and r["models"][SONNET]["ok"] is True and r["models"][OPUS]["latency_ms"] == 12
    assert r["needs_human"] == [f"solver model {GPT} unavailable; no silent substitution"] and r["solver_unavailable"] == [GPT]
    assert any("judge_model" in n and "fell back to " + OPUS in n for n in r["notes"])
    err = r["models"][GPT]["error"]
    assert "fake-openai-key-value-1234" not in err and "[redacted:OPENAI_API_KEY]" in err and err.startswith("AuthenticationError")   # redacted
    saved = json.loads((ws.root / "evidence" / "model-probe.json").read_text())
    assert saved["resolved"] == r["resolved"] and saved["chains"] == A.FALLBACK_CHAINS and saved["notes"] == r["notes"]


def test_probe_chains_never_resolve_judge_and_draft_to_the_same_model(ws, monkeypatch):
    monkeypatch.setattr("swesim.roles.probe_id", _probe_failing(set()))
    r = A.probe_models({"judge_model": SONNET, "draft_model": SONNET, "models": [HAIKU]})
    assert r["resolved"]["judge_model"] == SONNET and r["resolved"]["draft_model"] == SOL and r["resolved"]["stakeholder_model"] == OPUS
    assert r["resolved"]["judge_model"] != r["resolved"]["draft_model"]
    assert any("advanced the draft chain to " + SOL in n for n in r["notes"]) and r["needs_human"] == [] and r["solver_unavailable"] == []
    # draft chain exhausted: the judge chain advances instead (configured both to sonnet; gpt-5.6-sol unavailable)
    monkeypatch.setattr("swesim.roles.probe_id", _probe_failing({SOL}))
    r2 = A.probe_models({"judge_model": SONNET, "draft_model": SONNET, "models": [HAIKU]})
    assert r2["resolved"]["draft_model"] == SONNET and r2["resolved"]["judge_model"] == OPUS and any("advanced the judge chain to " + OPUS in n for n in r2["notes"])
    # nothing distinct left: recorded for a human, never silently equal-and-proceed
    monkeypatch.setattr("swesim.roles.probe_id", _probe_failing({SOL, OPUS, GPT}))
    r3 = A.probe_models({"judge_model": SONNET, "draft_model": SONNET, "models": [HAIKU]})
    assert any("no distinct model is available" in n for n in r3["needs_human"])


def test_plan_trials_carries_resolved_stakeholder_model_into_run_config(ws):
    spec = frozen_scenario(ws)
    base = {"scenario_id": spec.id, "models": [HAIKU], "conditions": ["interactive", "hidden"], "k": 2, "stakeholder_url": "https://s"}
    plain = A.plan_trials(base)["trials"]
    with_sh = A.plan_trials({**base, "stakeholder_model": SONNET})["trials"]
    assert [t["run_config"]["stakeholder"]["model_id"] for t in plain] == [RunConfig.default("full").stakeholder.model_id] * 4
    for t in with_sh:
        rc = RunConfig.model_validate(t["run_config"])
        assert rc.stakeholder.model_id == SONNET and t["stakeholder_model"] == SONNET
        assert t["run_config_hash"] == rc.run_config_hash and t["trial_id"] == trial_id(spec.spec_hash, t["condition"], rc.run_config_hash, t["attempt_index"])
    assert {t["run_config_hash"] for t in with_sh}.isdisjoint({t["run_config_hash"] for t in plain})   # the stakeholder model is part of the hash
    assert {t["trial_id"] for t in with_sh}.isdisjoint({t["trial_id"] for t in plain})


def test_resolved_models_reach_judge_draft_and_plan_trials(monkeypatch):
    async def scenario():
        seen = {}
        async def act(name, p, minutes=10, **kw):
            if name == "probe_models_activity":
                return {"resolved": {"judge_model": OPUS, "draft_model": SONNET, "stakeholder_model": SONNET}, "substitutions": {"judge_model": OPUS, "stakeholder_model": SONNET}, "needs_human": []}
            if name in ("judge_activity", "draft_activity", "plan_trials_activity"): seen[name] = {k: p.get(k) for k in ("judge_model", "draft_model", "stakeholder_model")}
            if name == "auto_approve_activity": return {"approved": True, "spec_hash": "h", "reasons": []}
            return dict(BIG)
        monkeypatch.setattr(W, "act", act)
        r = await W.PipelineWorkflow().run({"judge_model": GPT, "draft_model": SONNET, "models": [HAIKU]})
        expected = {"judge_model": OPUS, "draft_model": SONNET, "stakeholder_model": SONNET}
        assert seen["judge_activity"] == expected and seen["draft_activity"] == expected and seen["plan_trials_activity"] == expected
        assert r["status"] == "complete"
    asyncio.run(scenario())


def test_execute_trial_request_writes_runner_error_on_spec_mismatch(ws, monkeypatch):
    spec = frozen_scenario(ws); p = trial_request(ws, spec); d = ws.root / "trials" / p["trial_id"]
    atomic_json(d / "request.json", {**p, "spec_hash": "0" * 64})
    monkeypatch.setenv("SWESIM_WORKSPACE", str(ws.root)); monkeypatch.setenv("SWESIM_ENV_FILE", str(ws.root.parent / "missing.env"))
    monkeypatch.setitem(sys.modules, "swesim.stages.run", fake_module("swesim.stages.run", run_trial=lambda *a, **k: pytest.fail("must not run")))
    with pytest.raises(ValueError, match="frozen spec changed"): A.execute_trial_request(d / "request.json")
    assert "frozen spec changed" in json.loads((d / "runner-error.json").read_text())["error"]


def test_execute_trial_request_runs_run_trial_with_full_run_config(ws, monkeypatch):
    spec = frozen_scenario(ws); p = trial_request(ws, spec, condition="full"); d = ws.root / "trials" / p["trial_id"]
    atomic_json(d / "request.json", p); seen = {}
    class Rec:
        def __init__(s, tid): s.trial_id = tid; s.status = "completed"; s.reward = {"reward": 1.0, "success": 1}
    def run_trial(ws_, spec_, rc, attempt_index, stakeholder_url, worker_secret):
        seen.update(rc=rc, attempt=attempt_index, url=stakeholder_url, secret=worker_secret); return Rec(p["trial_id"])
    monkeypatch.setitem(sys.modules, "swesim.stages.run", fake_module("swesim.stages.run", run_trial=run_trial))
    monkeypatch.setenv("SWESIM_WORKSPACE", str(ws.root)); monkeypatch.setenv("SWESIM_ENV_FILE", str(ws.root.parent / "missing.env")); monkeypatch.setenv("SWESIM_WORKER_SECRET", "s")
    out = A.execute_trial_request(d / "request.json")
    assert out["status"] == "completed" and seen["rc"] == RunConfig.model_validate(p["run_config"]) and seen["secret"] == "s" and seen["attempt"] == 0


# ---------------------------------------------------------------- real Temporal test environment (fakes for activities)

def _run_in_env(scenario):
    """Run `scenario(env)` in a Temporal test environment; skip explicitly when none can start offline."""
    from temporalio.testing import WorkflowEnvironment
    async def go():
        env = None; errors = []
        from swesim.workflows import search_attributes as SA
        for name, start in (("local dev server", lambda: WorkflowEnvironment.start_local(dev_server_existing_path=shutil.which("temporal"),
                                                                                       search_attributes=[SA.key(n) for n in SA.KEYS], dev_server_log_level="error")),):
            try: env = await start(); break
            except Exception as e: errors.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
        if env is None: pytest.skip("no Temporal test server available offline: " + "; ".join(errors))
        try: await scenario(env)
        finally: await env.shutdown()
    asyncio.run(go())


def test_env_built_workers_carry_the_heartbeat_throttle_and_concurrency():
    """`build_workers` hands every production Worker the five-second heartbeat throttle and the per-queue concurrency."""
    from datetime import timedelta
    from swesim.settings import Settings
    from swesim.workflows import worker as WK
    async def scenario(env):
        s = Settings.from_env({"SWESIM_ENV_FILE": "/nonexistent/.env"})
        built = WK.build_workers(env.client, s)
        assert [w.task_queue for w in built] == list(s.task_queues) and len(built) == len(QUEUES)
        for w in built:
            assert w.config()["max_heartbeat_throttle_interval"] == timedelta(seconds=5)
            assert w.config()["max_concurrent_activities"] == WK.concurrency(s, w.task_queue)
    _run_in_env(scenario)


def test_env_trial_workflow_rejects_duplicate_id_and_launches_once():
    from temporalio import activity
    from temporalio.exceptions import WorkflowAlreadyStartedError
    launches = []
    @activity.defn(name="launch_trial_activity")
    async def launch(p): launches.append(p["trial_id"]); return {"trial_id": p["trial_id"], "unit": "swesim-trial-" + p["trial_id"], "decision": "launch", "launched": True}
    @activity.defn(name="await_trial_activity")
    async def collect(p): return {"trial_id": p["trial_id"], "status": "completed", "reward": {"reward": 1.0, "success": 1}}
    async def scenario(env):
        async with workers(env, workflows=[W.TrialWorkflow], activities=[launch, collect]):
            tid = trial_id("s", "interactive", "r", 0)
            r = await env.client.execute_workflow(W.TrialWorkflow.run, {"trial_id": tid}, id=tid, task_queue=Q.AUTHORING, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
            assert r["status"] == "completed" and launches == [tid]
            with pytest.raises(WorkflowAlreadyStartedError):
                await env.client.start_workflow(W.TrialWorkflow.run, {"trial_id": tid}, id=tid, task_queue=Q.AUTHORING, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
            assert launches == [tid]
    _run_in_env(scenario)


def test_env_pipeline_signal_mode_waits_then_verifies_signalled_hash():
    from temporalio import activity
    calls = []
    def fake(name, result):
        @activity.defn(name=name)
        async def f(p): calls.append((name, p)); return result(p) if callable(result) else result
        return f
    acts = [fake("probe_models_activity", {"substitutions": {}, "models": {}, "needs_human": []}),
            fake("mine_activity", {"candidates_path": "c"}), fake("judge_activity", {"judged_path": "j", "instance_ids": ["a__1"]}),
            fake("draft_activity", {"scenario_id": "a--1", "draft_path": "d"}),
            fake("verify_approval_activity", lambda p: {"scenario_id": p["scenario_id"], "spec_hash": p["spec_hash"]}),
            fake("compile_activity", {"compile_path": "x"}), fake("validate_activity", {"status": "valid", "failures": []}),
            fake("register_activity", {"registry_entry": {}}), fake("plan_trials_activity", {"scenario_id": "a--1", "trials": []}),
            fake("score_activity", {"scores_path": "s"}), fake("export_activity", {"manifest_paths": []})]
    async def scenario(env):
        async with workers(env, workflows=[W.PipelineWorkflow, W.TrialWorkflow], activities=acts):
            h = await env.client.start_workflow(W.PipelineWorkflow.run, {"approval_mode": "signal", "judge_model": "m", "draft_model": "m", "models": ["m"], "stakeholder_url": "https://s"},
                                                id="pipeline-test", task_queue=Q.AUTHORING, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
            for _ in range(200):
                state = await h.query(W.PipelineWorkflow.progress)
                if state.get("stage") == "awaiting-approval": break
                await asyncio.sleep(0.05)
            assert state["stage"] == "awaiting-approval" and state["scenario_id"] == "a--1"
            await h.signal(W.PipelineWorkflow.approve_spec, {"scenario_id": "a--1", "spec_hash": "sig-hash"})
            result = await h.result()
            assert result["status"] == "complete" and [(s["scenario_id"], s["status"]) for s in result["scenarios"]] == [("a--1", "complete")]
            verify = next(p for n, p in calls if n == "verify_approval_activity"); assert verify["spec_hash"] == "sig-hash"
            names = [n for n, _ in calls]
            assert names.index("verify_approval_activity") < names.index("compile_activity") < names.index("validate_activity") < names.index("register_activity")
    _run_in_env(scenario)


def test_env_matrix_runs_children_serially_with_trial_ids_then_scores():
    from temporalio import activity
    order = []
    @activity.defn(name="launch_trial_activity")
    async def launch(p): order.append(("launch", p["trial_id"])); return {"launched": True}
    @activity.defn(name="await_trial_activity")
    async def collect(p): order.append(("await", p["trial_id"])); return {"trial_id": p["trial_id"], "status": "completed", "reward": {"reward": 0.0, "success": 0}}
    @activity.defn(name="score_activity")
    async def score(p): order.append(("score", p["scenario_id"])); return {"scores_path": "s"}
    @activity.defn(name="export_activity")
    async def export(p): order.append(("export", p["scenario_id"])); return {"manifest_paths": []}
    trials = [{"trial_id": trial_id("s", "hidden", "r", i), "scenario_id": "a--1"} for i in range(2)]
    async def scenario(env):
        async with workers(env, workflows=[W.RunMatrixWorkflow, W.TrialWorkflow], activities=[launch, collect, score, export]):
            r = await env.client.execute_workflow(W.RunMatrixWorkflow.run, {"scenario_id": "a--1", "trials": trials}, id="matrix-test", task_queue=Q.AUTHORING)
            assert [t["status"] for t in r["trials"]] == ["completed", "completed"]
            assert order == [("launch", trials[0]["trial_id"]), ("await", trials[0]["trial_id"]), ("launch", trials[1]["trial_id"]), ("await", trials[1]["trial_id"]), ("score", "a--1"), ("export", "a--1")]
            # Re-running the matrix cannot relaunch: the child ids are already used.
            r2 = await env.client.execute_workflow(W.RunMatrixWorkflow.run, {"scenario_id": "a--1", "trials": trials}, id="matrix-test-2", task_queue=Q.AUTHORING)
            assert [t["status"] for t in r2["trials"]] == ["duplicate-rejected", "duplicate-rejected"] and len([o for o in order if o[0] == "launch"]) == 2
    _run_in_env(scenario)


# ---------------------------------------------------------------- worker and client surface

def test_worker_registers_all_workflows_and_partitions_activities_by_queue(monkeypatch):
    from temporalio import activity
    from swesim.settings import Settings
    from swesim.workflows import worker as WK
    own = {WK.activity_name(a) for a in A.ALL}
    assert {"mine_activity", "judge_activity", "draft_activity", "auto_approve_activity", "verify_approval_activity", "compile_activity", "validate_activity",
            "register_activity", "plan_trials_activity", "launch_trial_activity", "await_trial_activity", "score_activity", "export_activity", "probe_models_activity",
            "list_candidates_activity", "judge_one_activity", "probe_candidate_activity", "precheck_draft_activity", "human_approve_activity"} <= own
    assert set(WK.WORKFLOWS) == {WK.OperatorActionWorkflow, W.TrialWorkflow, W.RunMatrixWorkflow, W.PipelineWorkflow, W.CandidateWorkflow, W.CreateTasksWorkflow, W.RunTasksWorkflow}
    assert A.LAUNCH_SLOTS._value == 1                        # one launch at a time per worker
    # everything registered (own + whichever optional stream modules import) is partitioned by queue: union == registered, parts disjoint
    registered = {WK.activity_name(a) for a in WK.registered_activities()}
    assert own <= registered and {WK.activity_name(a) for a in WK.optional_activities()} == registered - own
    parts = {q: {WK.activity_name(a) for a in WK.activities_for_queue(q)} for q in QUEUES}
    assert set().union(*parts.values()) == registered and sum(len(v) for v in parts.values()) == len(registered)
    assert {"launch_trial_activity", "await_trial_activity"} <= parts[Q.TRIALS] and {"compile_activity", "probe_candidate_activity"} <= parts[Q.BUILD]
    assert "judge_one_activity" in parts[Q.AUTHORING] and {"record_cost_activity", "spend_check_activity"} <= parts[Q.CONTROL]
    assert (registered - own) <= parts[Q.CONTROL]            # the optional streams' activities are all control-queue bookkeeping
    # an optional module registers when importable and is skipped when not (independent of which ones main has today)
    @activity.defn(name="index_trial_activity")
    async def fake_index(p): return {}
    monkeypatch.setitem(sys.modules, "swesim.optional_fake.activities", fake_module("swesim.optional_fake.activities", ALL=[fake_index]))
    assert [WK.activity_name(a) for a in WK.optional_activities(("swesim.optional_fake.activities", "swesim.not_merged_yet.activities"))] == ["index_trial_activity"]
    assert WK.optional_activities(()) == []
    # concurrency: the per-queue default unless the pool overrides it; the startup line carries queues and digest, no secret
    s = Settings.from_env({"SWESIM_TASK_QUEUE": "swesim-build,swesim-control", "SWESIM_WORKER_IMAGE_DIGEST": "sha256:abc", "TEMPORAL_API_KEY": "tmprl_x",
                           "SWESIM_ENV_FILE": "/nonexistent/.env"})
    assert WK.concurrency(s, Q.BUILD) == 8 and WK.concurrency(s.model_copy(update={"max_concurrent_activities": 3}), Q.BUILD) == 3
    # heartbeats are throttled to five seconds so an operator's cancel reaches a running stage within seconds, not the SDK's 60 s
    from datetime import timedelta
    assert WK.HEARTBEAT_THROTTLE == timedelta(seconds=5)
    assert WK.worker_options(s, Q.BUILD) == {"max_concurrent_activities": 8, "max_heartbeat_throttle_interval": timedelta(seconds=5)}
    assert WK.worker_options(s.model_copy(update={"max_concurrent_activities": 3}), Q.CONTROL)["max_concurrent_activities"] == 3
    line = WK.startup_line(s)
    assert "queues=swesim-build,swesim-control" in line and "worker_image_digest=sha256:abc" in line and "tmprl_x" not in line
    # the control pool migrates the index at startup when it is enabled; other pools and a disabled index skip; a missing entry point is reported
    calls = []
    fake_index = fake_module("swesim.index", ensure_migrated=lambda settings: calls.append(settings.temporal_namespace))
    monkeypatch.setitem(sys.modules, "swesim.index", fake_index)
    with_db = s.model_copy(update={"database_url": __import__("pydantic").SecretStr("postgresql+psycopg://u:pw@/db")})
    assert WK.migrate_index_if_enabled(s) == {"migrated": False, "reason": "index disabled"} and calls == []
    assert WK.migrate_index_if_enabled(with_db.model_copy(update={"task_queues": ("swesim-build",)})) == {"migrated": False, "reason": "not the control pool"}
    assert WK.migrate_index_if_enabled(with_db) == {"migrated": True} and calls == ["default"]
    monkeypatch.setitem(sys.modules, "swesim.index", fake_module("swesim.index"))
    assert WK.migrate_index_if_enabled(with_db)["migrated"] is False and "ensure_migrated" in WK.migrate_index_if_enabled(with_db)["reason"]


def test_client_ids_and_signal_payload(monkeypatch):
    from swesim.workflows import client as C
    class Handle:
        def __init__(s): s.signals = []; s.cancelled = 0; s.queries = []
        async def signal(s, fn, arg): s.signals.append((fn, arg))
        async def cancel(s): s.cancelled += 1
        async def query(s, name): s.queries.append(name); return {"stage": "x"}
    class FakeClient:
        def __init__(s): s.started = []; s.handle = Handle(); s.wids = []
        async def start_workflow(s, fn, arg, **kw): s.started.append((fn, arg, kw))
        def get_workflow_handle(s, wid): s.wids.append(wid); return s.handle
    fc = FakeClient()
    async def connect(): return fc
    monkeypatch.setattr(C, "connect", connect)
    wid = asyncio.run(C.start_pipeline({"source": "github-repo"}))
    assert wid.startswith("pipeline-") and fc.started[0][2]["id"] == wid and fc.started[0][2]["task_queue"] == Q.AUTHORING
    assert fc.started[0][2]["id_reuse_policy"] == WorkflowIDReusePolicy.REJECT_DUPLICATE
    asyncio.run(C.approve(wid, "a--1", "h"))
    assert fc.wids[-1] == wid and fc.handle.signals == [(W.PipelineWorkflow.approve_spec, {"scenario_id": "a--1", "spec_hash": "h"})]
    mid = asyncio.run(C.start_matrix({"scenario_id": "a--1", "trials": []}))
    assert mid.startswith("matrix-") and fc.started[1][0] == W.RunMatrixWorkflow.run
    # create / run starters carry a secret-free summary memo
    cid = asyncio.run(C.start_create({"sources": [{"kind": "github-repo", "repo": "https://github.com/x/y"}, {"kind": "swebench-annotated", "pool": "A"}],
                                      "judge_model": "j", "draft_model": "d", "stakeholder_url": "https://s", "approval_mode": "hold"}))
    assert cid.startswith("create-") and fc.started[2][0] == W.CreateTasksWorkflow.run
    assert fc.started[2][2]["memo"] == {"summary": {"sources": ["https://github.com/x/y", "swebench-annotated:A"], "judge_model": "j", "draft_model": "d", "approval_mode": "hold"}}
    rid = asyncio.run(C.start_run({"scenario_ids": ["a--1"], "models": ["m"], "conditions": ["hidden"], "k": 2, "stakeholder_url": "https://s"}))
    assert rid.startswith("run-") and fc.started[3][0] == W.RunTasksWorkflow.run and fc.started[3][2]["memo"]["summary"]["scenario_ids"] == ["a--1"]
    assert "stakeholder_url" not in json.dumps(fc.started[3][2]["memo"])
    # hold-mode decisions always reach the candidate workflow, whether given the parent id or the candidate id
    assert asyncio.run(C.approve_candidate(cid, "a--1", "h", "approve", "looks right")) == "create:a--1"
    assert asyncio.run(C.approve_candidate("create:a--1", "a--1", "h", "reject", "no")) == "create:a--1"
    assert fc.wids[-2:] == ["create:a--1", "create:a--1"]
    assert fc.handle.signals[-2:] == [(W.CandidateWorkflow.approve, {"scenario_id": "a--1", "spec_hash": "h", "decision": "approve", "note": "looks right"}),
                                      (W.CandidateWorkflow.approve, {"scenario_id": "a--1", "spec_hash": "h", "decision": "reject", "note": "no"})]
    # a rebuilt candidate runs as create:<sid>@v<n>: given the parent id, the client reads the child's id from the parent's progress
    async def query_with_candidates(name): fc.handle.queries.append(name); return {"stage": "creating", "candidates": {"a--1": {"candidate_workflow_id": "create:a--1@v2"}}}
    fc.handle.query = query_with_candidates
    assert asyncio.run(C.approve_candidate(cid, "a--1", "h2", "approve", "v2 reviewed")) == "create:a--1@v2" and fc.wids[-1] == "create:a--1@v2"
    assert asyncio.run(C.approve_candidate("create:a--1@v2", "a--1", "h2", "reject", "no")) == "create:a--1@v2"
    with pytest.raises(ValueError): asyncio.run(C.approve_candidate(cid, "a--1", "h", "maybe"))
    assert asyncio.run(C.progress(cid))["stage"] == "creating" and fc.handle.queries[-1] == "progress"
    asyncio.run(C.cancel(cid)); assert fc.handle.cancelled == 1


def test_create_cli_rebuild_flag_sets_the_param(monkeypatch):
    from typer.testing import CliRunner
    from swesim import cli as CLI
    from swesim.workflows import client as C
    captured = []
    async def start_create(params): captured.append(params); return "create-fake"
    monkeypatch.setattr(C, "start_create", start_create)
    runner = CliRunner()
    r = runner.invoke(CLI.app, ["create", "--source", "https://github.com/x/y", "--rebuild", "--stakeholder-url", "https://s"])
    assert r.exit_code == 0, r.output
    assert "create-fake" in r.output and captured[-1]["rebuild"] is True and captured[-1]["sources"][0]["repo"] == "https://github.com/x/y"
    r2 = runner.invoke(CLI.app, ["create", "--source", "swebench:A:psf__requests-1944", "--stakeholder-url", "https://s"])
    assert r2.exit_code == 0, r2.output
    assert "rebuild" not in captured[-1] and captured[-1]["sources"][0]["instance_ids"] == ["psf__requests-1944"]   # default: today's behavior


def test_console_create_form_rebuild_checkbox_maps_to_the_param(monkeypatch):
    from swesim.dashboard import console, render_console
    body = {"source": "swebench-annotated", "instance_ids": "psf__requests-1944", "judge_model": "openai/gpt-5.5", "draft_model": "anthropic/claude-sonnet-5",
            "approval_mode": "auto", "max_parallel": "2", "limit": "5"}
    params, errors = console.validate_create({**body, "rebuild": ["1"]})          # the console JS posts a checked box as ["1"]
    assert errors == [] and params["rebuild"] is True and console.cli_for_create(params).endswith("--rebuild")
    plain, errors = console.validate_create({**body, "rebuild": []})              # unchecked: []
    assert errors == [] and plain["rebuild"] is False and "--rebuild" not in console.cli_for_create(plain)
    assert console.validate_create({**body, "rebuild": "true"})[0]["rebuild"] is True and console.validate_create(body)[0]["rebuild"] is False
    calls = []
    async def start_create(wf_params): calls.append(wf_params); return "create-fake"
    monkeypatch.setattr(console, "client_fn", lambda name: start_create if name == "start_create" else None)
    monkeypatch.setattr(console, "stakeholder_url", lambda settings=None: "https://s")
    out = asyncio.run(console.start_create(params))
    assert out["workflow_ids"] == ["create-fake"] and calls[-1]["rebuild"] is True and calls[-1]["sources"][0]["kind"] == "swebench-annotated"
    asyncio.run(console.start_create(plain))
    assert "rebuild" not in calls[-1]
    page = render_console.create_page(console.defaults())
    assert "name='rebuild'" in page and "Rebuild existing scenarios as a new version" in page and "type='checkbox'" in page


def test_client_list_runs_filters_by_type_and_summarizes(monkeypatch):
    from datetime import datetime, timezone
    from temporalio.client import WorkflowExecutionStatus
    from swesim.workflows import client as C
    class Exec:
        def __init__(s, wid, wtype, status, summary):
            s.id = wid; s.run_id = "r"; s.workflow_type = wtype; s.status = status; s._summary = summary
            s.start_time = datetime(2026, 9, 13, tzinfo=timezone.utc); s.close_time = None
        async def memo(s): return {"summary": s._summary}
    class FakeClient:
        def __init__(s): s.queries = []
        async def list_workflows(s, query, limit=None):
            s.queries.append((query, limit))
            for e in (Exec("create-1", "CreateTasksWorkflow", WorkflowExecutionStatus.RUNNING, {"sources": ["r"]}), Exec("run-1", "RunTasksWorkflow", WorkflowExecutionStatus.COMPLETED, {"models": ["m"]})):
                if e.workflow_type in query: yield e
    fc = FakeClient()
    async def connect(): return fc
    monkeypatch.setattr(C, "connect", connect)
    runs = asyncio.run(C.list_runs("create", 10))
    assert fc.queries[-1] == ("WorkflowType = 'CreateTasksWorkflow'", 10)
    assert runs == [{"workflow_id": "create-1", "run_id": "r", "type": "CreateTasksWorkflow", "status": "RUNNING", "start_time": "2026-09-13T00:00:00+00:00", "close_time": None, "summary": {"sources": ["r"]}}]
    allruns = asyncio.run(C.list_runs("all", 5))
    assert [r["type"] for r in allruns] == ["CreateTasksWorkflow", "RunTasksWorkflow"] and " OR " in fc.queries[-1][0]
    with pytest.raises(ValueError): asyncio.run(C.list_runs("bogus"))
    # open executions per type (the deploy path's open-executions check)
    class OpenClient:
        def __init__(s): s.queries = []
        async def list_workflows(s, query, limit=None):
            s.queries.append(query)
            for wid, wtype in (("t1", "TrialWorkflow"), ("t2", "TrialWorkflow"), ("run-1", "RunTasksWorkflow")):
                yield Exec(wid, wtype, WorkflowExecutionStatus.RUNNING, {})
    oc = OpenClient()
    async def connect_open(): return oc
    monkeypatch.setattr(C, "connect", connect_open)
    assert asyncio.run(C.open_types()) == [{"type": "RunTasksWorkflow", "open_count": 1}, {"type": "TrialWorkflow", "open_count": 2}]
    assert oc.queries == ["ExecutionStatus = 'Running'"]


# ---------------------------------------------------------------- task-creation activities

def test_list_candidates_and_judge_one_per_candidate_files(ws, monkeypatch):
    calls = []
    def judge_candidate(row, model_id):
        calls.append(row["instance_id"]); return {"gaps": [], "verified_rubric_score": 2, "keep": row["instance_id"] == "a__1", "model_id": model_id}
    monkeypatch.setitem(sys.modules, "swesim.stages.judge", fake_module("swesim.stages.judge", judge_candidate=judge_candidate))
    cand = ws.candidates_dir / "github-repo--x__y.jsonl"; cand.parent.mkdir(parents=True)
    cand.write_text("".join(json.dumps({"instance_id": i, "repo": "x/y"}) + "\n" for i in ("a__1", "b__2", "c__3")))
    listed = asyncio.run(A.list_candidates_activity({"candidates_path": str(cand), "max_candidates": 2}))
    assert listed == stamped({"candidates": [{"instance_id": "a__1", "scenario_id": "a--1"}, {"instance_id": "b__2", "scenario_id": "b--2"}]})
    j = asyncio.run(A.judge_one_activity({"candidates_path": str(cand), "instance_id": "a__1", "judge_model": "jm"}))
    assert j["keep"] is True and j["scenario_id"] == "a--1" and j["judge_model"] == "jm" and Path(j["judged_path"]) == cand.parent / "judged" / "a--1.judged.jsonl"
    assert json.loads(Path(j["judged_path"]).read_text())["judge"]["keep"] is True
    assert asyncio.run(A.judge_one_activity({"candidates_path": str(cand), "instance_id": "a__1", "judge_model": "jm"})) == j and calls == ["a__1"]   # retained
    j2 = asyncio.run(A.judge_one_activity({"candidates_path": str(cand), "instance_id": "b__2", "judge_model": "jm"}))
    assert j2["keep"] is False and j2["reasons"] == ["judge did not keep the candidate"]
    with pytest.raises(Exception, match="not in"): asyncio.run(A.judge_one_activity({"candidates_path": str(cand), "instance_id": "zz__9", "judge_model": "jm"}))
    # the one-row judged file feeds draft_activity unchanged
    drafted = []
    def draft(row, judged, model_id, out_dir, **kw):
        drafted.append(row["instance_id"]); out_dir.mkdir(parents=True, exist_ok=True); (out_dir / "scenario.draft.json").write_text("{}"); return out_dir / "scenario.draft.json"
    monkeypatch.setitem(sys.modules, "swesim.stages.draft_spec", fake_module("swesim.stages.draft_spec", draft=draft))
    d = asyncio.run(A.draft_activity({"judged_path": j["judged_path"], "instance_id": "a__1", "draft_model": "dm"}))
    assert d["scenario_id"] == "a--1" and drafted == ["a__1"]


def test_scenario_version_helper(ws):
    sid = "example-scenario"
    assert A.scenario_version(sid) == {"scenario_id": sid, "exists": False, "version": None, "next": 1}
    sdir = ws.scenario_dir(sid); sdir.mkdir(parents=True)
    atomic_json(sdir / "scenario.json", {**minimal_spec_dict(), "version": 1})
    assert A.scenario_version(sid) == {"scenario_id": sid, "exists": True, "version": 1, "next": 2}
    atomic_json(ws.registry_path, {"scenarios": {sid: {"id": sid, "version": 3, "spec_hash": "x"}}})     # registry ahead of the file: max wins
    assert A.scenario_version(sid) == {"scenario_id": sid, "exists": True, "version": 3, "next": 4}
    (sdir / "scenario.json").unlink()
    assert A.scenario_version(sid)["exists"] is True and A.scenario_version(sid)["next"] == 4          # registry alone counts as existing
    atomic_json(ws.registry_path, {"scenarios": {"other": {"id": "other", "version": 9}}})
    assert A.scenario_version(sid) == {"scenario_id": sid, "exists": False, "version": None, "next": 1}
    assert asyncio.run(A.scenario_version_activity({"scenario_id": sid}))["next"] == 1
    # A rebuild to v2 that was rejected at a gate (or ended invalid) consumed `create:<sid>@v2`: the registry still says v1 and
    # scenario.json is archived, but the v2 draft on disk (or its rejected copy, or the archive dir) must push `next` to 3.
    atomic_json(ws.registry_path, {"scenarios": {sid: {"id": sid, "version": 1, "spec_hash": "x"}}})
    (sdir / "archive" / "v1").mkdir(parents=True)
    assert A.scenario_version(sid)["next"] == 2
    atomic_json(sdir / "scenario.draft.rejected-1.json", {**minimal_spec_dict(), "version": 2})
    assert A.scenario_version(sid) == {"scenario_id": sid, "exists": True, "version": 2, "next": 3}
    (sdir / "scenario.draft.rejected-1.json").unlink(); (sdir / "archive" / "v2").mkdir()
    assert A.scenario_version(sid)["next"] == 3


def test_archive_scenario_moves_exactly_the_listed_files_and_is_idempotent(ws):
    sid = "example-scenario"; sdir = ws.scenario_dir(sid); (sdir / "assets").mkdir(parents=True)
    for name in A.ARCHIVE_FILES + ("scenario.draft.rejected-1.json", "scenario.draft.rejected-2.json", "scores.json", "approval.note.json"):
        (sdir / name).write_text(json.dumps({"name": name, "version": 1}))
    (sdir / "assets" / "oracle.diff").write_text("diff"); (sdir / "assets" / "test.patch").write_text("patch")
    r = asyncio.run(A.archive_scenario_activity({"scenario_id": sid, "previous_version": 1}))
    archive = sdir / "archive" / "v1"
    assert r["archive_dir"] == str(archive) and r["previous_version"] == 1 and "already_archived" not in r
    assert sorted(r["archived"]) == sorted(list(A.ARCHIVE_FILES) + ["scenario.draft.rejected-1.json", "scenario.draft.rejected-2.json"])
    for name in r["archived"]:
        assert (archive / name).is_file() and not (sdir / name).exists()
    assert (sdir / "assets" / "oracle.diff").read_text() == "diff" and (sdir / "scores.json").exists() and (sdir / "approval.note.json").exists()   # kept in place
    assert json.loads((archive / "archived.json").read_text())["archived"] == r["archived"]
    # a rerun after a partial rebuild must not archive the half-built newer version as the old one
    atomic_json(sdir / "scenario.draft.json", {"version": 2})
    again = A.archive_scenario(sid, 1)
    assert again["already_archived"] is True and (sdir / "scenario.draft.json").exists() and json.loads((archive / "scenario.draft.json").read_text())["version"] == 1
    # nothing to archive is fine (a registry-only scenario): empty list, marker written
    r2 = A.archive_scenario("other-one", 2)
    assert r2["archived"] == [] and (ws.scenario_dir("other-one") / "archive" / "v2" / "archived.json").exists()


def test_draft_activity_passes_version_and_draft_accepts_it(ws, monkeypatch):
    import inspect
    from swesim.stages import draft_spec
    assert inspect.signature(draft_spec.draft).parameters["version"].default == 1                     # the one drafter kwarg this task adds
    drafted = []
    def draft(row, judged, model_id, out_dir, pipeline_version="0.1.0", require_probe=True, version=1):
        drafted.append(version); out_dir.mkdir(parents=True, exist_ok=True); (out_dir / "scenario.draft.json").write_text(json.dumps({"version": version})); return out_dir / "scenario.draft.json"
    monkeypatch.setitem(sys.modules, "swesim.stages.draft_spec", fake_module("swesim.stages.draft_spec", draft=draft))
    judged = ws.candidates_dir / "judged" / "a--1.judged.jsonl"; judged.parent.mkdir(parents=True)
    judged.write_text(json.dumps({"instance_id": "a__1", "judge": {"keep": True}}) + "\n")
    r = asyncio.run(A.draft_activity({"judged_path": str(judged), "instance_id": "a__1", "draft_model": "d", "version": 2}))
    assert drafted == [2] and r["version"] == 2 and json.loads(Path(r["draft_path"]).read_text())["version"] == 2
    (ws.scenario_dir("a--1") / "scenario.draft.json").unlink()
    r1 = asyncio.run(A.draft_activity({"judged_path": str(judged), "instance_id": "a__1", "draft_model": "d"}))
    assert drafted == [2, 1] and r1["version"] == 1


def test_candidate_child_id_formation():
    assert W.candidate_child_id("a--1") == "create:a--1" == W.candidate_child_id("a--1", None) == W.candidate_child_id("a--1", 1)
    assert W.candidate_child_id("a--1", 2) == "create:a--1@v2" and W.candidate_child_id("a--1", 10) == "create:a--1@v10"
    from swesim.dashboard import console
    assert console.WORKFLOW_ID_RE.fullmatch("create:a--1@v2") and console.WORKFLOW_ID_RE.fullmatch("create-abc123")   # the console's /jobs route accepts both


def test_precheck_and_human_approve_activities(ws, monkeypatch):
    spec = ScenarioSpec.model_validate(minimal_spec_dict()); sdir = ws.scenario_dir(spec.id); sdir.mkdir(parents=True)
    atomic_json(sdir / "scenario.draft.json", spec.model_dump(mode="json"))
    gates = {"reasons": ["judge did not accept", "withheld brief still contains excerpt of f01"]}; approvals = []
    def check_assets(sdir_, spec_): return []
    def auto_gates(spec_, sdir_): return list(gates["reasons"])
    def approve_scenario(ws_, sid, auto):
        approvals.append(auto); atomic_json(ws_.scenario_dir(sid) / "approval.json", {"spec_hash": spec.spec_hash, "approved_by": "human-command", "review_needed": False})
        return {"approved": True, "spec_hash": spec.spec_hash, "mode": "human"}
    monkeypatch.setitem(sys.modules, "swesim.stages.approve", fake_module("swesim.stages.approve", check_assets=check_assets, auto_gates=auto_gates, approve_scenario=approve_scenario))
    pre = asyncio.run(A.precheck_draft_activity({"scenario_id": spec.id}))
    assert pre["ok"] is False and pre["reasons"] == ["withheld brief still contains excerpt of f01"] and pre["spec_hash"] == spec.spec_hash   # only leak gates pre-check
    gates["reasons"] = []
    assert asyncio.run(A.precheck_draft_activity({"scenario_id": spec.id})) == stamped({"ok": True, "reasons": [], "scenario_id": spec.id, "spec_hash": spec.spec_hash})
    assert not (sdir / "approval.json").exists()                                     # pre-check writes nothing
    with pytest.raises(Exception, match="differs from the draft"): asyncio.run(A.human_approve_activity({"scenario_id": spec.id, "spec_hash": "0" * 64}))
    assert approvals == [] and not (sdir / "approval.json").exists()                 # a mismatched hash never approves
    r = asyncio.run(A.human_approve_activity({"scenario_id": spec.id, "spec_hash": spec.spec_hash, "note": "reviewed"}))
    assert r == stamped({"approved": True, "scenario_id": spec.id, "spec_hash": spec.spec_hash, "approved_by": "human-command"}) and approvals == [False]
    assert json.loads((sdir / "approval.note.json").read_text())["note"] == "reviewed"
    (sdir / "scenario.draft.json").write_text("{not json")
    bad = asyncio.run(A.precheck_draft_activity({"scenario_id": spec.id}))
    assert bad["ok"] is False and bad["reasons"][0].startswith("schema:")


# ---------------------------------------------------------------- CreateTasksWorkflow / RunTasksWorkflow (real Temporal test environment)

def _create_fakes(conc, judge_calls, validate_invalid=("a--5",), judge_error=("a__3",), judge_drop=("a__4",), lists=None, hold=None, not_reproducible=("a__2",)):
    from temporalio import activity
    from temporalio.exceptions import ApplicationError
    lists = lists or {"/c/https://github.com/x/a.jsonl": ["a__1", "a__2", "a__3", "a__4", "a__5"], "/c/A.jsonl": ["a__1", "b__1"]}
    sid = lambda i: i.replace("__", "--")
    @activity.defn(name="probe_candidate_activity")
    async def probe_c(p):
        ok = p["instance_id"] not in not_reproducible
        return {"judged_path": p["judged_path"], "instance_id": p["instance_id"], "scenario_id": p["scenario_id"], "fail_to_pass": ["t1"] if ok else [],
                "pass_to_pass": ["t0"], "reproducible": ok, "reason": None if ok else "measured FAIL_TO_PASS is empty in the offline probe"}
    @activity.defn(name="probe_models_activity")
    async def probe(p): return {"resolved": {"judge_model": p.get("judge_model"), "draft_model": p.get("draft_model"), "stakeholder_model": "openai/gpt-5.5"}, "substitutions": {}, "needs_human": []}
    @activity.defn(name="mine_activity")
    async def mine(p): return {"candidates_path": f"/c/{p.get('repo') or p.get('pool')}.jsonl"}
    @activity.defn(name="discover_activity")
    async def discover(p): return {"candidates_path": f"/c/{p['repo']}.jsonl"}
    @activity.defn(name="list_candidates_activity")
    async def listc(p): return {"candidates": [{"instance_id": i, "scenario_id": sid(i)} for i in lists[p["candidates_path"]]]}
    @activity.defn(name="judge_one_activity")
    async def judge(p):
        judge_calls.append(p["instance_id"]); conc["cur"] += 1; conc["max"] = max(conc["max"], conc["cur"])
        await asyncio.sleep(0.05); conc["cur"] -= 1
        if p["instance_id"] in judge_error: raise ApplicationError("judge exploded", non_retryable=True)
        return {"judged_path": "j", "instance_id": p["instance_id"], "scenario_id": sid(p["instance_id"]), "keep": p["instance_id"] not in judge_drop, "reasons": []}
    @activity.defn(name="draft_activity")
    async def draft(p): return {"scenario_id": sid(p["instance_id"]), "draft_path": "d"}
    @activity.defn(name="auto_approve_activity")
    async def approve(p): return {"approved": True, "spec_hash": "h-" + p["scenario_id"], "reasons": []}
    @activity.defn(name="precheck_draft_activity")
    async def precheck(p): return {"ok": True, "reasons": [], "scenario_id": p["scenario_id"], "spec_hash": "draft-" + p["scenario_id"]}
    @activity.defn(name="human_approve_activity")
    async def human(p):
        if hold is not None: hold.setdefault("calls", []).append(p)
        if p["spec_hash"] != "draft-" + p["scenario_id"]: raise ApplicationError("signalled spec_hash differs from the draft on disk", non_retryable=True)
        return {"approved": True, "scenario_id": p["scenario_id"], "spec_hash": p["spec_hash"], "approved_by": "human-command"}
    @activity.defn(name="verify_approval_activity")
    async def verify(p): return {"scenario_id": p["scenario_id"], "spec_hash": p["spec_hash"]}
    @activity.defn(name="compile_activity")
    async def compile_(p): return {"compile_path": "c"}
    @activity.defn(name="validate_activity")
    async def validate(p): return {"status": "invalid" if p["scenario_id"] in validate_invalid else "valid", "failures": ["oracle earned 0"]}
    @activity.defn(name="register_activity")
    async def register(p): return {"registry_entry": {}}
    return [probe, mine, discover, listc, judge, probe_c, draft, approve, precheck, human, verify, compile_, validate, register]


def test_env_create_tasks_fanout_bound_duplicates_failing_sibling_and_progress():
    conc = {"cur": 0, "max": 0}; judge_calls = []
    params = {"sources": [{"kind": "github-repo", "repo": "https://github.com/x/a", "since": "2026-06-01"}, {"kind": "swebench-annotated", "pool": "A"}],
              "judge_model": "j", "draft_model": "d", "stakeholder_url": "https://s", "max_parallel": 2}
    async def scenario(env):
        async with workers(env, workflows=[W.CreateTasksWorkflow, W.CandidateWorkflow], activities=_create_fakes(conc, judge_calls)):
            h = await env.client.start_workflow(W.CreateTasksWorkflow.run, params, id="create-test-1", task_queue=Q.AUTHORING)
            r = await h.result()
            assert r["counts"] == {"candidates": 6, "valid": 2, "invalid": 1, "rejected": 1, "error": 1, "duplicate-rejected": 0, "duplicate-in-batch": 1, "not-reproducible-offline": 1}
            assert r["status"] == "complete" and {s for s, c in r["candidates"].items() if c["status"] == "valid"} == {"a--1", "b--1"}
            assert r["candidates"]["a--2"]["status"] == "not-reproducible-offline" and "FAIL_TO_PASS is empty" in r["candidates"]["a--2"]["reasons"][0]
            assert r["candidates"]["a--5"]["status"] == "invalid" and r["candidates"]["a--5"]["reasons"] == ["oracle earned 0"]
            assert r["candidates"]["a--3"]["status"] == "error" and "judge exploded" in r["candidates"]["a--3"]["reasons"][0]
            assert r["candidates"]["a--4"]["status"] == "rejected" and r["candidates"]["a--1"]["spec_hash"] == "h-a--1"
            assert conc["max"] <= 2 and sorted(judge_calls) == ["a__1", "a__2", "a__3", "a__4", "a__5", "b__1"]   # bound respected; duplicate judged once
            prog = await h.query(W.CreateTasksWorkflow.progress)
            assert prog["stage"] == "complete" and set(prog["candidates"]) == {"a--1", "a--2", "a--3", "a--4", "a--5", "b--1"} and prog["awaiting_approval"] == []
            c1 = prog["candidates"]["a--1"]
            assert c1["status"] == "valid" and c1["stage"] == "done" and c1["candidate_workflow_id"] == "create:a--1" and c1["source"] in ("github-repo--x__a--feature", "A", "a")   # normalized keys
            assert ["judging", "probing", "auto-approving", "compiling", "validating", "registering"] == [s for s in c1["stages"] if s in ("judging", "probing", "auto-approving", "compiling", "validating", "registering")]
            assert "registering" not in prog["candidates"]["a--5"]["stages"] and prog["candidates"]["a--3"]["stages"] == ["judging"]
            assert prog["candidates"]["a--2"]["stages"] == ["judging", "probing"]
            assert {s["status"] for s in prog["sources"].values()} == {"mined"} and prog["probe"]["resolved"]["judge_model"] == "j"
            # the same candidates again: terminal outcomes are duplicate-rejected and never redone; the errored (failed)
            # candidate is the only one that runs again (ALLOW_DUPLICATE_FAILED_ONLY) and still errors here
            r2 = await env.client.execute_workflow(W.CreateTasksWorkflow.run, params, id="create-test-2", task_queue=Q.AUTHORING)
            assert r2["counts"]["duplicate-rejected"] == 5 and r2["counts"]["error"] == 1 and r2["counts"]["valid"] == 0 and r2["status"] == "complete-with-failures"
            assert r2["candidates"]["a--3"]["status"] == "error" and judge_calls.count("a__3") == 2 and len(judge_calls) == 7
    _run_in_env(scenario)


def test_env_create_tasks_rebuild_uses_versioned_child_ids_and_archives_only_existing_scenarios():
    from temporalio import activity
    conc = {"cur": 0, "max": 0}; judge_calls = []; seen_ids = {}; archived = []; draft_versions = {}; existing = {}
    lists = {"/c/R.jsonl": ["r__1", "r__2"]}
    @activity.defn(name="scenario_version_activity")
    async def version(p):
        v = existing.get(p["scenario_id"]); return {"scenario_id": p["scenario_id"], "exists": v is not None, "version": v, "next": (v or 0) + 1}
    @activity.defn(name="archive_scenario_activity")
    async def archive(p): archived.append((p["scenario_id"], p["previous_version"], activity.info().workflow_id)); return {"archived": ["scenario.json"]}
    base = _create_fakes(conc, judge_calls, validate_invalid=(), judge_error=(), judge_drop=(), lists=lists, not_reproducible=())
    @activity.defn(name="judge_one_activity")
    async def judge(p):
        judge_calls.append(p["instance_id"]); seen_ids.setdefault(p["instance_id"], []).append(activity.info().workflow_id)
        return {"judged_path": "j", "instance_id": p["instance_id"], "scenario_id": p["instance_id"].replace("__", "--"), "keep": True, "reasons": []}
    @activity.defn(name="draft_activity")
    async def draft(p): draft_versions[p["instance_id"]] = p.get("version"); return {"scenario_id": p["instance_id"].replace("__", "--"), "draft_path": "d", "version": p.get("version") or 1}
    acts = [a for a in base if a.__name__ not in ("judge", "draft")] + [version, archive, judge, draft]
    params = {"sources": [{"kind": "swebench-annotated", "pool": "R"}], "judge_model": "j", "draft_model": "d", "stakeholder_url": "https://s"}
    async def scenario(env):
        async with workers(env, workflows=[W.CreateTasksWorkflow, W.CandidateWorkflow], activities=acts):
            first = await env.client.execute_workflow(W.CreateTasksWorkflow.run, params, id="create-first", task_queue=Q.AUTHORING)
            assert first["counts"]["valid"] == 2 and seen_ids == {"r__1": ["create:r--1"], "r__2": ["create:r--2"]} and archived == [] and draft_versions == {"r__1": 1, "r__2": 1}
            # r--1 now exists as version 1 (scenario.json / registry); r--2 does not (say its registration was rolled back)
            existing["r--1"] = 1
            h = await env.client.start_workflow(W.CreateTasksWorkflow.run, {**params, "rebuild": True}, id="create-rebuild", task_queue=Q.AUTHORING)
            r = await h.result()
            assert r["candidates"]["r--1"]["status"] == "valid" and r["candidates"]["r--2"]["status"] == "duplicate-rejected"   # unchanged id for a non-existing scenario
            assert seen_ids["r__1"] == ["create:r--1", "create:r--1@v2"] and seen_ids["r__2"] == ["create:r--2"]
            assert archived == [("r--1", 1, "create:r--1@v2")] and draft_versions["r__1"] == 2
            prog = await h.query(W.CreateTasksWorkflow.progress)
            assert prog["rebuild"] is True and prog["candidates"]["r--1"]["candidate_workflow_id"] == "create:r--1@v2"
            assert prog["candidates"]["r--1"]["version"] == 2 and prog["candidates"]["r--1"]["previous_version"] == 1 and "archiving" in prog["candidates"]["r--1"]["stages"]
            assert prog["candidates"]["r--2"]["candidate_workflow_id"] == "create:r--2" and "version" not in prog["candidates"]["r--2"]
            child = await env.client.get_workflow_handle("create:r--1@v2").query(W.CandidateWorkflow.progress)
            assert child["status"] == "valid"
            # a non-rebuild run keeps today's behavior: ids unchanged, everything duplicate-rejected
            plain = await env.client.execute_workflow(W.CreateTasksWorkflow.run, params, id="create-plain-again", task_queue=Q.AUTHORING)
            assert plain["counts"]["duplicate-rejected"] == 2 and plain["candidates"]["r--1"]["status"] == "duplicate-rejected" and len(archived) == 1
    _run_in_env(scenario)


def test_env_create_tasks_hold_mode_review_queue_approve_reject_and_refused_hash(monkeypatch):
    from swesim.workflows import client as C
    conc = {"cur": 0, "max": 0}; judge_calls = []; hold = {}
    lists = {"/c/H.jsonl": ["h__1", "h__2", "h__3"]}
    params = {"sources": [{"kind": "swebench-annotated", "pool": "H"}], "judge_model": "j", "draft_model": "d", "stakeholder_url": "https://s", "approval_mode": "hold"}
    async def scenario(env):
        async def connect(): return env.client
        monkeypatch.setattr(C, "connect", connect)
        async with workers(env, workflows=[W.CreateTasksWorkflow, W.CandidateWorkflow],
                          activities=_create_fakes(conc, judge_calls, validate_invalid=(), judge_error=(), judge_drop=(), lists=lists, hold=hold, not_reproducible=())):
            h = await env.client.start_workflow(W.CreateTasksWorkflow.run, params, id="create-hold", task_queue=Q.AUTHORING)
            for _ in range(400):
                prog = await h.query(W.CreateTasksWorkflow.progress)
                if len(prog["awaiting_approval"]) == 3: break
                await asyncio.sleep(0.05)
            queue = sorted(prog["awaiting_approval"], key=lambda x: x["scenario_id"])
            assert queue == [{"scenario_id": s, "spec_hash": "draft-" + s, "candidate_workflow_id": "create:" + s} for s in ("h--1", "h--2", "h--3")]
            assert all(prog["candidates"][s]["status"] == "awaiting-approval" for s in ("h--1", "h--2", "h--3"))
            # a mismatched hash is refused and the candidate keeps waiting
            await C.approve_candidate("create-hold", "h--1", "stale-hash", "approve", "old queue")
            for _ in range(400):
                prog = await h.query(W.CreateTasksWorkflow.progress)
                if prog["candidates"]["h--1"].get("refusals"): break
                await asyncio.sleep(0.05)
            assert "differs from the draft" in prog["candidates"]["h--1"]["refusals"][0] and prog["candidates"]["h--1"]["status"] == "awaiting-approval"
            child = await env.client.get_workflow_handle("create:h--1").query(W.CandidateWorkflow.progress)
            assert child["stage"] == "awaiting-approval" and child["spec_hash"] == "draft-h--1" and child["refusals"]
            # a reject with a stale hash and an unknown decision are refused too; both decisions are bound to the draft hash
            await C.approve_candidate("create:h--2", "h--2", "stale-hash", "reject", "wrong queue")
            await env.client.get_workflow_handle("create:h--2").signal(W.CandidateWorkflow.approve, {"scenario_id": "h--2", "spec_hash": "draft-h--2", "decision": "maybe"})
            for _ in range(400):
                prog = await h.query(W.CreateTasksWorkflow.progress)
                if len(prog["candidates"]["h--2"].get("refusals", [])) >= 2: break
                await asyncio.sleep(0.05)
            assert [r[:6] for r in prog["candidates"]["h--2"]["refusals"]] == ["reject", "unknow"] and prog["candidates"]["h--2"]["status"] == "awaiting-approval"
            await C.approve_candidate("create-hold", "h--1", "draft-h--1", "approve", "reviewed")
            await C.approve_candidate("create:h--2", "h--2", "draft-h--2", "reject", "not ready")
            await C.approve_candidate("create-hold", "h--3", "draft-h--3")
            r = await h.result()
            assert r["counts"]["valid"] == 2 and r["counts"]["rejected-by-human"] == 1 and r["status"] == "complete"
            assert r["candidates"]["h--2"] == {"scenario_id": "h--2", "instance_id": "h__2", "status": "rejected-by-human", "reasons": ["not ready"], "spec_hash": "draft-h--2"}
            assert r["candidates"]["h--1"]["status"] == "valid" and r["candidates"]["h--3"]["status"] == "valid"
            # a stale hash is refused inside the workflow (no activity round trip); the activity re-checks the hash on disk anyway
            # Independent candidate activities can complete in either order; each decision stays bound to its hash.
            by_hash = {call["spec_hash"]: call for call in hold["calls"]}
            assert len(hold["calls"]) == 2 and set(by_hash) == {"draft-h--1", "draft-h--3"} and by_hash["draft-h--1"]["note"] == "reviewed"
            prog = await h.query(W.CreateTasksWorkflow.progress)
            assert prog["awaiting_approval"] == [] and "awaiting-approval" in prog["candidates"]["h--1"]["stages"] and "compiling" in prog["candidates"]["h--1"]["stages"]
            assert prog["candidates"]["h--1"]["stages"].index("probing") < prog["candidates"]["h--1"]["stages"].index("pre-checking")
    _run_in_env(scenario)


def test_create_tasks_progress_status_follows_the_child_stage():
    # Fix round (split review, Important 1): a candidate leaves the review queue as soon as it moves on.
    w = W.CreateTasksWorkflow()
    w.candidates["a--1"] = {"instance_id": "a__1", "source": "s", "status": "running", "stage": "starting", "stages": [], "candidate_workflow_id": "create:a--1", "spec_hash": None}
    w.candidate_stage({"instance_id": "a__1", "scenario_id": "a--1", "stage": "awaiting-approval", "spec_hash": "d1"})
    assert w.candidates["a--1"]["status"] == "awaiting-approval"
    assert w.progress()["awaiting_approval"] == [{"scenario_id": "a--1", "spec_hash": "d1", "candidate_workflow_id": "create:a--1"}]
    w.candidate_stage({"instance_id": "a__1", "scenario_id": "a--1", "stage": "compiling"})
    assert w.candidates["a--1"]["status"] == "running" and w.progress()["awaiting_approval"] == [] and w.candidates["a--1"]["stages"] == ["awaiting-approval", "compiling"]
    w.candidate_stage({"instance_id": "a__1", "scenario_id": None, "stage": "judging"})          # before judge: lookup by instance id
    assert w.candidates["a--1"]["stage"] == "judging" and w.candidates["a--1"]["spec_hash"] == "d1"
    w.candidate_stage({"instance_id": "zz__9", "scenario_id": "zz--9", "stage": "judging"})      # unknown candidate: ignored
    assert set(w.candidates) == {"a--1"}


def test_create_tasks_rejects_duplicate_source_keys_before_any_activity(monkeypatch):
    # Fix round (split review, Important 2): mine writes one file per pool/repo, so a batch may not list a key twice.
    from temporalio.exceptions import ApplicationError
    async def scenario():
        async def act(*a, **k): raise AssertionError("no activity may run for an invalid batch")
        monkeypatch.setattr(W, "act", act)
        with pytest.raises(ApplicationError, match="duplicate sources.*swebench-annotated:a"):
            await W.CreateTasksWorkflow().run({"sources": [{"kind": "swebench-annotated", "pool": "A", "limit": 3}, {"kind": "swebench-annotated", "pool": "A", "instance_ids": ["x__1"]}]})
        with pytest.raises(ApplicationError, match="duplicate sources"):
            await W.CreateTasksWorkflow().run({"sources": [{"kind": "github-repo", "repo": "https://github.com/x/y", "since": "2026-06-01"}, {"kind": "github-repo", "repo": "https://github.com/x/y", "since": "2026-07-01"}]})
        with pytest.raises(ApplicationError, match="empty"):
            await W.CreateTasksWorkflow().run({"sources": []})
        # spellings mine merges into one output file are one key (casefold, trailing slash, .git, URL forms)
        with pytest.raises(ApplicationError, match="duplicate sources.*github-repo--x__a--feature"):
            await W.CreateTasksWorkflow().run({"sources": [{"kind": "github-repo", "repo": "https://github.com/X/A/"}, {"kind": "github-repo", "repo": "x/a.git"}]})
        with pytest.raises(ApplicationError, match="duplicate sources"):
            await W.CreateTasksWorkflow().run({"sources": [{"kind": "swebench-annotated", "pool": "A"}, {"kind": "swebench-annotated", "pool": "a "}]})
    asyncio.run(scenario())
    assert W._source_key({"kind": "github-repo", "repo": "git@github.com:X/A.git"}) == "github-repo:x/a" == W._source_key({"kind": "github-repo", "repo": "github.com/x/a/"})


def test_candidate_failure_message_names_the_failing_stage(monkeypatch):
    # Minor 4: the stage is captured before _done marks the candidate done.
    from types import SimpleNamespace
    from temporalio.exceptions import ApplicationError
    async def scenario():
        monkeypatch.setattr(W.workflow, "info", lambda: SimpleNamespace(parent=None))
        async def act(name, p, minutes=10, **kw): raise RuntimeError("judge provider down")
        monkeypatch.setattr(W, "act", act)
        w = W.CandidateWorkflow()
        with pytest.raises(ApplicationError, match="failed at stage judging: RuntimeError: judge provider down"):
            await w.run({"instance_id": "x__1", "candidates_path": "c", "judge_model": "j", "draft_model": "d"})
        assert w.progress()["status"] == "error" and w.progress()["reasons"][0].startswith("judging: RuntimeError")
    asyncio.run(scenario())


def _fake_probe_candidates(outcome: dict, calls: list):
    """Mimics swesim.stages.mine.probe_candidates (sources branch c30994c): writes <judged>.probed.jsonl and .probed.summary.json,
    measured lists replace the source lists (originals under swebench_*), probe metadata with `statuses`, reject_reason on failure."""
    def probe_candidates(ws_, judged_path, instance_ids, max_builds):
        calls.append((str(judged_path), list(instance_ids), max_builds)); out = Path(judged_path).with_suffix(".probed.jsonl"); summary = {"rows": 1, "probed": 1}
        rows = [json.loads(line) for line in Path(judged_path).read_text().splitlines() if line.strip()]
        with open(out, "w") as f:
            for r in rows:
                if r["instance_id"] not in instance_ids: continue
                r["swebench_fail_to_pass"] = list(r.get("fail_to_pass") or []); r["swebench_pass_to_pass"] = list(r.get("pass_to_pass") or [])
                if "error" in outcome:
                    r["probe"] = {"error": outcome["error"]}; r["reject_reason"] = "probe-failed"; r.setdefault("judge", {})["keep"] = False; summary["probe-failed"] = 1
                else:
                    r["fail_to_pass"], r["pass_to_pass"] = outcome["f2p"], outcome["p2p"]; r["test_python"] = "/opt/py/bin/python"
                    r["probe"] = {"kind": "swebench", "statuses": {"base": {"t1": "failed"}, "gold": {"t1": "passed"}}, "probe_dir": "/jobs/probe/x", "dropped": {"missing-on-gold": 1}}
                    if not r["fail_to_pass"]: r["reject_reason"] = "not-reproducible-offline"; r.setdefault("judge", {})["keep"] = False; summary["not-reproducible-offline"] = 1
                    else: summary["reproducible"] = 1
                f.write(json.dumps(r) + "\n")
        out.with_suffix(".summary.json").write_text(json.dumps(summary)); return out
    return probe_candidates


def test_probe_candidate_activity_follows_probe_candidates_contract_and_is_retained(ws, monkeypatch):
    judged = ws.candidates_dir / "judged" / "a--1.judged.jsonl"; judged.parent.mkdir(parents=True)
    judged.write_text(json.dumps({"instance_id": "a__1", "fail_to_pass": ["published::t1"], "pass_to_pass": [], "judge": {"keep": True}}) + "\n")
    calls = []
    monkeypatch.setitem(sys.modules, "swesim.stages.mine", fake_module("swesim.stages.mine", probe_candidates=_fake_probe_candidates({"f2p": ["t1"], "p2p": ["t0"]}, calls)))
    r = asyncio.run(A.probe_candidate_activity({"judged_path": str(judged), "instance_id": "a__1", "scenario_id": "a--1", "max_builds": 2}))
    assert r["reproducible"] is True and r["fail_to_pass"] == ["t1"] and r["pass_to_pass"] == ["t0"] and r["probe_dir"] == "/jobs/probe/x"
    assert r["dropped"] == {"reproducible": 1} and calls == [(str(judged), ["a__1"], 2)]
    row = json.loads(judged.read_text().splitlines()[0])
    assert row["fail_to_pass"] == ["t1"] and row["swebench_fail_to_pass"] == ["published::t1"] and row["probe"]["statuses"] and row["judge"]["keep"] is True   # draft's require_probe reads probe.statuses
    again = asyncio.run(A.probe_candidate_activity({"judged_path": str(judged), "instance_id": "a__1"}))
    assert again["fail_to_pass"] == ["t1"] and len(calls) == 1                                                        # retained: no second Modal run
    # nothing fails on base and passes with gold -> terminal not-reproducible-offline, with the probe's accounting
    judged2 = judged.with_name("b--2.judged.jsonl"); judged2.write_text(json.dumps({"instance_id": "b__2", "fail_to_pass": ["published::t9"], "judge": {"keep": True}}) + "\n")
    monkeypatch.setitem(sys.modules, "swesim.stages.mine", fake_module("swesim.stages.mine", probe_candidates=_fake_probe_candidates({"f2p": [], "p2p": ["t0"]}, calls)))
    r2 = asyncio.run(A.probe_candidate_activity({"judged_path": str(judged2), "instance_id": "b__2"}))
    assert r2["reproducible"] is False and r2["fail_to_pass"] == [] and r2["reason"] == "not-reproducible-offline" and r2["dropped"] == {"not-reproducible-offline": 1}
    assert json.loads(judged2.read_text())["reject_reason"] == "not-reproducible-offline"
    # probe_tests' ValueError recorded by probe_candidates is measured-but-unusable -> terminal, not an infrastructure error
    judged3 = judged.with_name("c--3.judged.jsonl"); judged3.write_text(json.dumps({"instance_id": "c__3", "judge": {"keep": True}}) + "\n")
    monkeypatch.setitem(sys.modules, "swesim.stages.mine", fake_module("swesim.stages.mine", probe_candidates=_fake_probe_candidates({"error": "ValueError: probe needs target failure and regression coverage"}, calls)))
    r3 = asyncio.run(A.probe_candidate_activity({"judged_path": str(judged3), "instance_id": "c__3"}))
    assert r3["reproducible"] is False and "probe needs target failure" in r3["reason"] and r3["dropped"] == {"probe-failed": 1}
    # a Harbor/build failure recorded by probe_candidates is infrastructure: the activity fails (the child fails, rerunnable)
    judged4 = judged.with_name("d--4.judged.jsonl"); judged4.write_text(json.dumps({"instance_id": "d__4", "judge": {"keep": True}}) + "\n")
    monkeypatch.setitem(sys.modules, "swesim.stages.mine", fake_module("swesim.stages.mine", probe_candidates=_fake_probe_candidates({"error": "HarborRunError: no trial dir; harbor stdout tail: ..."}, calls)))
    with pytest.raises(RuntimeError, match="probe infrastructure failure.*HarborRunError"): asyncio.run(A.probe_candidate_activity({"judged_path": str(judged4), "instance_id": "d__4"}))
    assert "probe" not in json.loads(judged4.read_text())                                                              # nothing recorded as measured; rerun probes again
    # rows mine already probed (probe.statuses / probe_dir) are not re-probed
    judged5 = judged.with_name("e--5.judged.jsonl"); judged5.write_text(json.dumps({"instance_id": "e__5", "fail_to_pass": ["t1"], "pass_to_pass": [], "probe_dir": "/jobs/probe/e", "judge": {"keep": True}}) + "\n")
    n = len(calls); r5 = asyncio.run(A.probe_candidate_activity({"judged_path": str(judged5), "instance_id": "e__5"}))
    assert r5["reproducible"] is True and r5["probe_dir"] == "/jobs/probe/e" and len(calls) == n
    with pytest.raises(Exception, match="not in"): asyncio.run(A.probe_candidate_activity({"judged_path": str(judged5), "instance_id": "zz__1"}))


def test_probe_candidate_activity_fallback_probe_tests_maps_value_error(ws, monkeypatch):
    # Until probe_candidates lands on main, github_repo.probe_tests(row, ws) is the fallback for every source kind.
    judged = ws.candidates_dir / "judged" / "f--6.judged.jsonl"; judged.parent.mkdir(parents=True)
    judged.write_text(json.dumps({"instance_id": "f__6", "fail_to_pass": ["published::t1"], "judge": {"keep": True}}) + "\n")
    monkeypatch.setitem(sys.modules, "swesim.stages.mine", fake_module("swesim.stages.mine"))
    ok = {"fail_to_pass": ["t1"], "pass_to_pass": ["t0"], "dropped": {"missing-on-gold": 2}, "statuses": {"base": {}, "gold": {}}, "dockerfile": "FROM x",
          "probe_dir": "/jobs/probe/f", "test_python": "/usr/bin/python3", "probe": {"kind": "github", "statuses": {"base": {}, "gold": {}}, "probe_dir": "/jobs/probe/f"}}
    monkeypatch.setitem(sys.modules, "swesim.sources.github_repo", fake_module("swesim.sources.github_repo", probe_tests=lambda row, ws_: ok))
    r = asyncio.run(A.probe_candidate_activity({"judged_path": str(judged), "instance_id": "f__6"}))
    row = json.loads(judged.read_text())
    assert r["reproducible"] is True and r["dropped"] == {"missing-on-gold": 2} and row["probe"]["statuses"] is not None and row["dockerfile"] == "FROM x" and row["swebench_fail_to_pass"] == ["published::t1"]
    # the real fallback raises ValueError when nothing fails on base and passes with gold: terminal, not infrastructure
    judged2 = judged.with_name("g--7.judged.jsonl"); judged2.write_text(json.dumps({"instance_id": "g__7", "fail_to_pass": ["published::t1"], "judge": {"keep": True}}) + "\n")
    def unusable(row, ws_): raise ValueError("probe needs target failure and regression coverage")
    monkeypatch.setitem(sys.modules, "swesim.sources.github_repo", fake_module("swesim.sources.github_repo", probe_tests=unusable))
    r2 = asyncio.run(A.probe_candidate_activity({"judged_path": str(judged2), "instance_id": "g__7"}))
    assert r2["reproducible"] is False and r2["fail_to_pass"] == [] and "probe needs target failure" in r2["reason"] and r2["dropped"] == {"probe-error": "probe needs target failure and regression coverage"}
    assert json.loads(judged2.read_text())["reject_reason"] == "not-reproducible-offline"
    # HarborRunError-like failures propagate (the child fails and stays rerunnable)
    judged3 = judged.with_name("h--8.judged.jsonl"); judged3.write_text(json.dumps({"instance_id": "h__8", "judge": {"keep": True}}) + "\n")
    class HarborRunError(RuntimeError): pass
    def broken(row, ws_): raise HarborRunError("no trial dir")
    monkeypatch.setitem(sys.modules, "swesim.sources.github_repo", fake_module("swesim.sources.github_repo", probe_tests=broken))
    with pytest.raises(HarborRunError): asyncio.run(A.probe_candidate_activity({"judged_path": str(judged3), "instance_id": "h__8"}))
    # neither probe importable: the error names the missing function
    monkeypatch.setitem(sys.modules, "swesim.sources.github_repo", fake_module("swesim.sources.github_repo"))
    with pytest.raises(Exception, match="swesim.stages.mine.probe_candidates"): asyncio.run(A.probe_candidate_activity({"judged_path": str(judged3), "instance_id": "h__8"}))


def test_env_run_tasks_two_scenarios_bounded_and_progress():
    from temporalio import activity
    from temporalio.exceptions import ApplicationError
    conc = {"cur": 0, "max": 0}; scored = []; exported = []; planned = []
    @activity.defn(name="plan_trials_activity")
    async def plan(p):
        planned.append(p)
        if p["scenario_id"] == "bad": raise ApplicationError("scenario/build not validated", non_retryable=True)
        return {"scenario_id": p["scenario_id"], "trials": [{"trial_id": f"{p['scenario_id']}-t{i}", "scenario_id": p["scenario_id"], "model": p["models"][0], "condition": "hidden", "attempt_index": i} for i in range(2)]}
    run_ids = set()
    @activity.defn(name="launch_trial_activity")
    async def launch(p):
        conc["cur"] += 1; conc["max"] = max(conc["max"], conc["cur"]); await asyncio.sleep(0.05); conc["cur"] -= 1
        run_ids.add(p.get("run_workflow_id"))
        return {"trial_id": p["trial_id"], "launched": True}
    @activity.defn(name="await_trial_activity")
    async def collect(p): return {"trial_id": p["trial_id"], "status": "completed", "reward": {"reward": 1.0, "success": 1}}
    @activity.defn(name="score_activity")
    async def score(p): scored.append(p["scenario_id"]); return {"scores_path": "s"}
    @activity.defn(name="export_activity")
    async def export(p): exported.append(p["scenario_id"]); return {"manifest_paths": []}
    params = {"scenario_ids": ["s1", "s2", "bad"], "models": ["m"], "conditions": ["hidden"], "k": 2, "purpose": "comparison", "stakeholder_model": "anthropic/claude-sonnet-5",
              "attempt_offset": 0, "stakeholder_url": "https://s", "max_parallel_trials": 1}
    async def scenario(env):
        async with workers(env, workflows=[W.RunTasksWorkflow, W.TrialWorkflow], activities=[plan, launch, collect, score, export]):
            h = await env.client.start_workflow(W.RunTasksWorkflow.run, params, id="run-test-1", task_queue=Q.AUTHORING)
            r = await h.result()
            assert r["status"] == "complete-with-failures" and r["scenarios"]["bad"]["status"] == "error" and "not validated" in r["scenarios"]["bad"]["error"]
            for sid in ("s1", "s2"):
                assert r["scenarios"][sid]["status"] == "complete" and [t["status"] for t in r["scenarios"][sid]["trials"]] == ["completed", "completed"]
            assert conc["max"] == 1 and sorted(scored) == ["s1", "s2"] == sorted(exported)              # trials one at a time; score+export per scenario
            assert run_ids == {"run-test-1"}                                                              # the parent's id reaches every trial request (Job label)
            assert all(p["stakeholder_model"] == "anthropic/claude-sonnet-5" and p["purpose"] == "comparison" for p in planned)
            prog = await h.query(W.RunTasksWorkflow.progress)
            assert prog["stage"] == "complete" and set(prog["trials"]) == {"s1-t0", "s1-t1", "s2-t0", "s2-t1"}
            assert prog["trials"]["s1-t0"] == {"scenario_id": "s1", "model": "m", "condition": "hidden", "attempt_index": 0, "status": "completed"}
            # index_score_activity is not registered on these workers (the storage stream's module): recorded as indexed False, never a failure
            assert prog["scenarios"] == {"s1": {"status": "complete", "trials": 2, "indexed": False}, "s2": {"status": "complete", "trials": 2, "indexed": False},
                                         "bad": {"status": "error", "error": prog["scenarios"]["bad"]["error"], "trials": 0}}
            assert prog["spend"]["exceeded"] is False and prog["stopped_reason"] is None
            done = await env.client.get_workflow_handle("s1-t0").query(W.TrialWorkflow.progress)
            assert done["status"] == "completed" and done["indexed"] is False
            # running the same selection again cannot relaunch: every trial id is already used
            r2 = await env.client.execute_workflow(W.RunTasksWorkflow.run, {**params, "scenario_ids": ["s1"]}, id="run-test-2", task_queue=Q.AUTHORING)
            assert [t["status"] for t in r2["scenarios"]["s1"]["trials"]] == ["duplicate-rejected", "duplicate-rejected"] and r2["status"] == "complete"
    _run_in_env(scenario)


# ---------------------------------------------------------------- Task 13 deployment files (syntax and contracts)

@pytest.mark.parametrize("script", ["create.sh", "setup.sh", "tunnel.sh", "update.sh"])
def test_vm_scripts_parse(script):
    path = ROOT / "scripts" / "vm" / script
    assert path.is_file() and os.access(path, os.X_OK), script
    subprocess.run(["bash", "-n", str(path)], check=True)
    text = path.read_text()
    assert "set -euo pipefail" in text and "export $(" not in text and "eval " not in text and "source " not in text and ". \"$" not in text


def _unit(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None); cp.optionxform = str  # systemd repeats Environment=
    cp.read_string((ROOT / "scripts" / "vm" / name).read_text()); return cp


def test_temporal_unit_binds_loopback_and_persists_db():
    u = _unit("swesim-temporal.service"); exec_ = u["Service"]["ExecStart"]
    assert "server start-dev" in exec_ and "--db-filename /var/lib/swesim/temporal.db" in exec_ and "--ip 127.0.0.1" in exec_
    assert "--port 7233" in exec_ and "--ui-port 8233" in exec_ and u["Service"]["Restart"] == "always" and u["Install"]["WantedBy"] == "default.target"


def test_worker_unit_uses_env_file_and_repo_checkout():
    u = _unit("swesim-worker.service"); s = u["Service"]
    assert s["EnvironmentFile"] == "/etc/swesim/env" and s["WorkingDirectory"] == "/opt/swesim" and s["Restart"] == "always"
    assert "swesim.workflows.worker" in s["ExecStart"] or "swesim worker" in s["ExecStart"]
    assert "swesim-temporal.service" in u["Unit"]["After"]
    assert "KillMode=" not in (ROOT / "scripts/vm/swesim-worker.service").read_text() or s.get("KillMode") == "process"


def test_dashboard_unit_binds_loopback_8080():
    u = _unit("swesim-dashboard.service"); s = u["Service"]
    assert "uvicorn swesim.dashboard.app:app" in s["ExecStart"] and "--host 127.0.0.1" in s["ExecStart"] and "--port 8080" in s["ExecStart"]
    assert s["EnvironmentFile"] == "/etc/swesim/env" and s["WorkingDirectory"] == "/opt/swesim"


def test_create_script_contract():
    text = (ROOT / "scripts/vm/create.sh").read_text()
    assert "gcloud compute instances create --quiet" in text and "e2-standard-4" in text and "100GB" in text
    assert ("debian-12" in text) or ("ubuntu-2404-lts" in text)
    assert "GCP_PROJECT" in text and "GCP_ZONE" in text and "SWESIM_ENV_FILE" in text


def test_tunnel_forwards_all_three_ports():
    text = (ROOT / "scripts/vm/tunnel.sh").read_text()
    for port in ("8233", "7233", "8080"): assert f"{port}:127.0.0.1:{port}" in text or f"{port}:localhost:{port}" in text
    assert "gcloud compute ssh" in text


def test_setup_script_contract():
    text = (ROOT / "scripts/vm/setup.sh").read_text()
    for needle in ("astral.sh/uv/install.sh", "uv python install 3.12", "temporal.download/cli/archive/v1.8.3", "/etc/swesim/env", "chmod 600",
                   "loginctl enable-linger", "systemd-analyze --user verify", "systemctl --user enable --now", "swesim-temporal", "swesim-worker", "swesim-dashboard", "/opt/swesim", "/var/lib/swesim"):
        assert needle in text, needle
    assert "docker" not in text.lower() or "not required" in text.lower()


def test_update_script_contract():
    text = (ROOT / "scripts/vm/update.sh").read_text(); setup = (ROOT / "scripts/vm/setup.sh").read_text()
    for needle in ("git fetch --quiet origin main", "git rev-list --count origin/main..HEAD", "refusing to update", "gcloud compute ssh --quiet", "cd /opt/swesim",
                   "pull --ff-only", "uv sync --extra dev --frozen", "systemctl --user restart swesim-worker swesim-dashboard", "is-active", "git rev-parse --short HEAD",
                   "is not origin/main", "FAILED_UNITS", "GCP_PROJECT", "GCP_ZONE", "SWESIM_ENV_FILE"):
        assert needle in text, needle
    assert "[ahead" not in text                                                                                # guard compares against origin/main, not the branch upstream
    assert "swesim-temporal" in text and "restart swesim-temporal" not in text and "swesim-trial" not in text   # never restarts the server or trial units
    assert "checkout --quiet -B main" in setup and "--set-upstream-to=origin/main" in setup                  # pull --ff-only needs a tracking branch
    # guard logic: refuses when HEAD carries commits origin/main lacks, passes when it does not
    refuse = 'AHEAD=2; if [ "$AHEAD" != "0" ]; then echo refusing; exit 1; fi; echo ok'
    cp = subprocess.run(["bash", "-c", refuse], capture_output=True, text=True); assert cp.returncode == 1 and "refusing" in cp.stdout
    # the VM-side unit check exits non-zero when a unit is not active
    units = 'FAILED_UNITS=0; for state in active failed active; do [ "$state" = "active" ] || FAILED_UNITS=1; done; [ "$FAILED_UNITS" = "0" ] || exit 1'
    assert subprocess.run(["bash", "-c", units]).returncode == 1
    # the clone stores no credentials: both scripts hand GH_TOKEN to git through a transient helper, never .git/config
    helper = "credential.helper=!f() { echo username=x-access-token; echo password=$GH_TOKEN; }; f"
    assert helper in text and "-c credential.helper= -c" in text and "env_get /etc/swesim/env GH_TOKEN" in text
    assert helper in setup and "gh auth setup-git" not in setup and "gh repo clone" not in setup and 'GH_TOKEN="$GH_TOKEN_VALUE" git' in setup
    # the helper emits the token from the environment only (no token appears in any git argument)
    cp = subprocess.run(["bash", "-c", "f() { echo username=x-access-token; echo password=$GH_TOKEN; }; f"], capture_output=True, text=True, env={**os.environ, "GH_TOKEN": "fake-token-value"})
    assert cp.stdout == "username=x-access-token\npassword=fake-token-value\n"
    # The deploy procedure the VM script implemented is documented in the cloud runbook: guarded by open executions, pinned to a
    # commit, and the VM backend stays a labelled fallback rather than the documented path.
    runbook = (ROOT / "docs/runbook.md").read_text()
    assert "## Deploy a commit" in runbook and "swesim workflows open-types" in runbook and "make deploy" in runbook and "SHA=COMMIT_SHA" in runbook
    assert "OVERRIDE_OPEN_EXECUTIONS" in runbook and "workflow.patched" in runbook
    assert "## Historical VM fallback" in runbook and "scripts/vm/" in runbook and "origin=vm-systemd" in runbook


def test_cloud_docs_have_required_sections():
    """setup/runbook/ops replaced the VM-era ops.md (f53c3c5). What the old check protected, in cloud terms: a complete operator
    path (setup 1-7), a runbook with deploy / inspect / stuck-trial / capacity+restart / rotation / import / restore / access /
    fallback / acceptance, an evidence ledger with model availability, restart and failure history, and the literals operators
    grep for: the trial runtime named by trial id, evidence paths, the never-relaunch rule, the spend cap and the frontier gate."""
    setup = (ROOT / "docs/setup.md").read_text(); runbook = (ROOT / "docs/runbook.md").read_text(); ops = (ROOT / "docs/ops.md").read_text()
    for section in ("## 1. Accounts and tools", "## 2. Clone and configure", "## 3. Check the checkout", "## 4. Configure Modal",
                    "## 5. Provision and deploy GCP", "## 6. First use", "## 7. Teardown"):
        assert section in setup, section
    for section in ("## Deploy a commit", "## Inspect work and spend", "## Diagnose a missing or stuck trial", "## Change capacity and test restart recovery",
                    "## Rotate credentials", "## Import and rebuild the record", "## Restore from bucket versions", "## Revoke or grant console access",
                    "## Historical VM fallback", "## Acceptance checklist"):
        assert section in runbook, section
    for section in ("## Hosted takeover", "### Model availability", "## Preserved record inventory", "## Outstanding hosted acceptance",
                    "## Historical VM evidence", "### Worker restart", "## Historical failures and resulting fixes", "## Retry and evidence rules"):
        assert section in ops, section
    assert "swesim-trial-TRIAL_ID" in runbook and "Do not issue another execute call" in runbook and "workspace/evidence/cloudrun/" in runbook
    assert "SWESIM_SPEND_CAP_USD" in runbook and "Frontier tiers require the brief's gate" in runbook and "no requested model is silently substituted" in runbook
    assert "workspace/evidence/" in ops and "swesim-trial-" in ops and "never" in ops.lower()
    # hosted evidence is dated and labelled; a VM demonstration is never presented as Cloud Run acceptance
    assert "Historical VM demonstrations remain separate from Cloud Run acceptance" in ops


def _bash_functions(text, names):
    parts = []
    for name in names:
        m = re.search(rf"^{re.escape(name)}\(\) \{{$.*?^\}}$", text, re.S | re.M)
        assert m, name
        parts.append(m.group(0))
    return "\n".join(parts)


def test_worker_env_is_rendered_with_only_the_five_keys(tmp_path):
    # Fix round 1, Important 3: the laptop dotenv file is never shipped; a filtered KEY='value' file becomes /etc/swesim/env.
    from dotenv import dotenv_values
    src = tmp_path / "laptop.env"
    src.write_text("# laptop env\nexport ANTHROPIC_API_KEY=\"fake-anthropic-123\"\nOPENAI_API_KEY=fake-openai-456 \n"
                   "SWESIM_WORKER_SECRET='fake-worker-secret'\nSWESIM_STAKEHOLDER_URL=https://stake.example\nGH_TOKEN=fake-gh-token\n"
                   "GCP_PROJECT=my-proj\nGCP_ZONE=us-central1-a\nAWS_SECRET_ACCESS_KEY=must-not-ship\nHF_TOKEN=must-not-ship-either\n"
                   "TEMPORAL_API_KEY=fake-temporal-key\n")   # Temporal Cloud keys ship when present; absent ones only warn
    create = (ROOT / "scripts/vm/create.sh").read_text(); setup = (ROOT / "scripts/vm/setup.sh").read_text()
    funcs = _bash_functions(create, ["env_get", "render_worker_env"])
    assert funcs == _bash_functions(setup, ["env_get", "render_worker_env"])          # identical copies in both scripts
    dst = tmp_path / "worker.env"
    subprocess.run(["bash", "-c", funcs + '\nrender_worker_env "$1" "$2"', "bash", str(src), str(dst)], check=True, capture_output=True, text=True)
    assert oct(dst.stat().st_mode & 0o777) == "0o600"
    text = dst.read_text()
    assert "GCP_" not in text and "AWS_" not in text and "HF_TOKEN" not in text and "export" not in text and "#" not in text
    assert all(re.fullmatch(r"[A-Z_]+='[^'\n]*'", line) for line in text.splitlines())   # systemd EnvironmentFile= safe
    assert dotenv_values(dst) == {"ANTHROPIC_API_KEY": "fake-anthropic-123", "OPENAI_API_KEY": "fake-openai-456 ", "SWESIM_WORKER_SECRET": "fake-worker-secret",
                                  "SWESIM_STAKEHOLDER_URL": "https://stake.example", "GH_TOKEN": "fake-gh-token", "TEMPORAL_API_KEY": "fake-temporal-key"}
    # create.sh ships the rendered temp file, and setup.sh renders again into /etc/swesim/env
    assert '"${SCP[@]}" "$WORKER_ENV" "$VM_NAME":~/swesim-bootstrap/env' in create and 'render_worker_env "$ENV_FILE" "$WORKER_ENV"' in create
    assert 'render_worker_env "$BOOT/env" /etc/swesim/env' in setup and 'install -m 600 "$BOOT/env"' not in setup
    # a value with a single quote is refused rather than written malformed
    bad = tmp_path / "bad.env"; bad.write_text("ANTHROPIC_API_KEY=it's-bad\nSWESIM_WORKER_SECRET=x\nGH_TOKEN=y\n")
    cp = subprocess.run(["bash", "-c", funcs + '\nrender_worker_env "$1" "$2"', "bash", str(bad), str(tmp_path / "bad.out")], capture_output=True, text=True)
    assert cp.returncode != 0 and "single quote" in cp.stderr


def test_connect_kwargs_local_and_api_key(tmp_path):
    """Local dev server by default; a Temporal Cloud API key turns TLS on and sets the routing header; the key never enters the log line."""
    from swesim.workflows import client as C
    none = {"SWESIM_ENV_FILE": str(tmp_path / "absent.env")}
    assert C.connect_kwargs(none) == {"target_host": "localhost:7233", "namespace": "default"}
    kw = C.connect_kwargs({**none, "TEMPORAL_ADDRESS": "us-central1.gcp.api.temporal.io:7233", "TEMPORAL_NAMESPACE": "swesim.abcde", "TEMPORAL_API_KEY": "tmprl_secret"})
    assert kw == {"target_host": "us-central1.gcp.api.temporal.io:7233", "namespace": "swesim.abcde", "api_key": "tmprl_secret", "tls": True,
                  "rpc_metadata": {"temporal-namespace": "swesim.abcde"}}   # the routing header Temporal Cloud requires with API keys
    line = C.describe_connection({**none, "TEMPORAL_ADDRESS": "x:7233", "TEMPORAL_NAMESPACE": "n", "TEMPORAL_API_KEY": "tmprl_secret"})
    assert line == "temporal=x:7233 namespace=n auth=api-key" and "tmprl_secret" not in line
    assert C.describe_connection(none).endswith("auth=plain")
    assert C.TASK_QUEUE == Q.AUTHORING


def test_plan_trials_uses_the_same_scaffold_identity_as_run_trial(ws):
    """Interactive mini-swe-agent trials carry the compiled agent_config.yaml digest in RunConfig.agent_config. The planner and
    run_trial must derive it identically, or the planned trial id never matches the recorded one (every interactive trial
    ended "unresolved" on 2026-09-13 before this test)."""
    import hashlib
    from swesim.stages.run import scaffold_identity
    spec = frozen_scenario(ws)
    task = ws.task_dir(spec.id, "interactive"); task.mkdir(parents=True, exist_ok=True)
    (task / "agent_config.yaml").write_text("agent:\n  instance_template: scaffold\n")
    digest = hashlib.sha256((task / "agent_config.yaml").read_bytes()).hexdigest()[:16]
    trials = A.plan_trials({"scenario_id": spec.id, "models": [HAIKU], "conditions": ["interactive", "hidden"], "k": 1, "stakeholder_url": "https://s"})["trials"]
    by_cond = {t["condition"]: t for t in trials}
    assert by_cond["interactive"]["run_config"]["agent_config"] == f"mini-swe-agent.interactive:{digest}"
    assert by_cond["hidden"]["run_config"]["agent_config"] == ""
    for t in trials:
        rc = RunConfig.model_validate(t["run_config"])
        assert rc.agent_config == scaffold_identity(ws.task_dir(spec.id, t["condition"]), rc)
        assert t["trial_id"] == trial_id(spec.spec_hash, t["condition"], rc.run_config_hash, t["attempt_index"])
    # without a scaffold file the identity is empty and the id is the pre-scaffold one
    (task / "agent_config.yaml").unlink()
    plain = A.plan_trials({"scenario_id": spec.id, "models": [HAIKU], "conditions": ["interactive"], "k": 1, "stakeholder_url": "https://s"})["trials"][0]
    assert plain["run_config"]["agent_config"] == "" and plain["trial_id"] != by_cond["interactive"]["trial_id"]


def test_create_pr_kind_reaches_the_mine_activity_params(monkeypatch):
    """The GitHub source has two discovery bands (github_repo.discover: bugfix 20-600 diff lines, feature 200-1500 + label). The
    Temporal path used to drop the band and fall back to `feature`, which rejected the PR that produced pypdf-3826 as
    diff-too-small on a rebuild (2026-09-13). CLI and console carry `pr_kind`; _mine_params maps it to the activity's `kind`."""
    from typer.testing import CliRunner
    from swesim import cli as CLI
    from swesim.workflows import workflows as W
    from swesim.dashboard import console
    captured = []
    async def fake_start(params): captured.append(params); return "create-fake"
    monkeypatch.setattr("swesim.workflows.client.start_create", fake_start)
    runner = CliRunner()
    r = runner.invoke(CLI.app, ["create", "--source", "https://github.com/x/y", "--stakeholder-url", "https://s"])
    assert r.exit_code == 0 and captured[-1]["sources"][0]["pr_kind"] == "bugfix"                       # default: the proven band
    r = runner.invoke(CLI.app, ["create", "--source", "https://github.com/x/y", "--pr-kind", "feature", "--stakeholder-url", "https://s"])
    assert r.exit_code == 0 and captured[-1]["sources"][0]["pr_kind"] == "feature"
    assert runner.invoke(CLI.app, ["create", "--source", "https://github.com/x/y", "--pr-kind", "docs", "--stakeholder-url", "https://s"]).exit_code == 2
    assert W._mine_params(captured[-1]["sources"][0])["kind"] == "feature"
    assert "kind" not in W._mine_params({"kind": "github-repo", "repo": "https://github.com/x/y"})     # absent -> discover's own default
    body = {"source": "github-repo", "repos": "https://github.com/x/y", "judge_model": "openai/gpt-5.5", "draft_model": "anthropic/claude-sonnet-5"}
    params, errors = console.validate_create(body)
    assert errors == [] and params["pr_kind"] == "bugfix" and console.create_sources(params)[0]["pr_kind"] == "bugfix"
    assert "--pr-kind bugfix" in console.cli_for_create(params)
    params, errors = console.validate_create({**body, "pr_kind": "feature"})
    assert errors == [] and console.create_sources(params)[0]["pr_kind"] == "feature"
    assert console.validate_create({**body, "pr_kind": "docs"})[1] == ["pr_kind must be bugfix or feature"]


def test_connect_kwargs_falls_back_to_the_env_file_for_temporal_keys(tmp_path, monkeypatch):
    """The CLI submits from a shell that has no TEMPORAL_* exported; the values come from the dotenv file (never exported)."""
    from swesim.workflows import client as C
    from swesim.settings import reset_settings
    envf = tmp_path / ".env"; envf.write_text("TEMPORAL_ADDRESS=x.gcp.api.temporal.io:7233\nTEMPORAL_NAMESPACE=ns.acct\nTEMPORAL_API_KEY=tmprl_from_file\nOTHER=ignored\n")
    for k in C.TEMPORAL_KEYS: monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SWESIM_ENV_FILE", str(envf)); reset_settings()
    try:
        kw = C.connect_kwargs()
        assert kw["target_host"] == "x.gcp.api.temporal.io:7233" and kw["namespace"] == "ns.acct" and kw["api_key"] == "tmprl_from_file" and kw["tls"] is True
        monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233"); monkeypatch.setenv("TEMPORAL_NAMESPACE", "default"); reset_settings()
        envf.write_text("OTHER=ignored\n")
        assert C.connect_kwargs() == {"target_host": "localhost:7233", "namespace": "default"}   # process env wins; no key -> plain
    finally:
        reset_settings()
