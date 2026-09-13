"""Task queues: the four Temporal queues and the total map from activity name to queue (interfaces §4).

Contract
- Every activity a swesim worker registers appears in `QUEUE_OF`, including the ones other streams register
  (`swesim.index.activities`, `swesim.audit.activities`); `tests/test_queues.py` asserts it.
- `queue_for(name)` is total over that map and raises `UnroutedActivity` (a ValueError) for anything else: an unrouted
  activity is a configuration error caught at the worker's start, never a silent fallback to a random queue.
- `CONCURRENCY` is the per-queue default for `max_concurrent_activities`; `Settings.max_concurrent_activities`
  overrides it for one pool.
"""
from __future__ import annotations

AUTHORING, BUILD, TRIALS, CONTROL = "swesim-authoring", "swesim-build", "swesim-trials", "swesim-control"

QUEUE_OF: dict[str, str] = {
    # authoring: LLM-bound and light on CPU
    "probe_models_activity": AUTHORING, "discover_activity": AUTHORING, "mine_activity": AUTHORING, "judge_one_activity": AUTHORING,
    "judge_activity": AUTHORING, "list_candidates_activity": AUTHORING, "draft_activity": AUTHORING, "precheck_draft_activity": AUTHORING,
    "auto_approve_activity": AUTHORING, "human_approve_activity": AUTHORING, "verify_approval_activity": AUTHORING,
    "scenario_version_activity": AUTHORING, "archive_scenario_activity": AUTHORING, "register_activity": AUTHORING,
    "plan_trials_activity": AUTHORING, "score_activity": AUTHORING, "export_activity": AUTHORING, "difficulty_label_activity": AUTHORING,
    # build: Harbor / Modal bound, capped so the registry and Modal are not flooded
    "probe_candidate_activity": BUILD, "compile_activity": BUILD, "validate_control_activity": BUILD,
    "evaluate_controls_activity": BUILD, "validate_activity": BUILD,
    # trials: launch, await, reconcile, cancel and clean up trial runtimes (Jobs run elsewhere)
    "launch_trial_activity": TRIALS, "await_trial_activity": TRIALS, "cancel_trial_activity": TRIALS, "cleanup_trial_activity": TRIALS,
    # control: workflow-side bookkeeping (index writes, cost accounting, audits)
    "index_scenario_activity": CONTROL, "index_candidate_activity": CONTROL, "index_trial_activity": CONTROL,
    "index_score_activity": CONTROL, "index_workflow_activity": CONTROL, "index_rebuild_activity": CONTROL,
    "index_discovery_activity": CONTROL,
    "read_candidate_source_activity": CONTROL,
    "prepare_operator_action_activity": CONTROL, "commit_operator_action_activity": CONTROL,
    "record_cost_activity": CONTROL, "spend_check_activity": CONTROL,
    "frontier_gate_activity": CONTROL,
    "prepare_trial_plan_activity": CONTROL, "prepare_candidate_plan_activity": CONTROL,
    "read_plan_page_activity": CONTROL, "checkpoint_workflow_activity": CONTROL,
    "audit_transcript_activity": CONTROL, "audit_scenario_activity": CONTROL,
}

CONCURRENCY: dict[str, int] = {AUTHORING: 32, BUILD: 8, TRIALS: 64, CONTROL: 16}


class UnroutedActivity(ValueError):
    """An activity name with no queue in `QUEUE_OF`."""


def queue_for(activity_name: str) -> str:
    """The queue an activity is scheduled on; raises `UnroutedActivity` for a name outside the map."""
    try:
        return QUEUE_OF[activity_name]
    except KeyError:
        raise UnroutedActivity(f"activity {activity_name!r} has no task queue in swesim.workflows.queues.QUEUE_OF") from None


def activities_for(queue: str) -> list[str]:
    """Sorted activity names routed to `queue`; raises `UnroutedActivity` for a name that is not one of the four queues."""
    if queue not in CONCURRENCY:
        raise UnroutedActivity(f"{queue!r} is not a swesim task queue; expected one of {sorted(CONCURRENCY)}")
    return sorted(name for name, q in QUEUE_OF.items() if q == queue)
