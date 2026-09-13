"""Contracts for the queue map, the retry classes and the search-attribute helpers (interfaces §4-5).

Offline: no Temporal server. Asserted behaviour: every registered activity is routed to one of the four queues, the
names other streams register are routed too, the retry classes have the shape the brief fixes, ApplicationErrors built
by `retries` carry the type and retryability they claim, and the search-attribute helpers produce typed Keyword
attributes under the six frozen names.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.common import SearchAttributeIndexedValueType, TypedSearchAttributes
from temporalio.exceptions import ApplicationError

from swesim.settings import QUEUES
from swesim.workflows import activities as A
from swesim.workflows import queues as Q
from swesim.workflows import retries as R
from swesim.workflows import search_attributes as SA

# Activities other streams register on the worker (interfaces §2, §9.1); the map must route them before they exist here.
OTHER_STREAMS = ["index_scenario_activity", "index_candidate_activity", "index_trial_activity", "index_score_activity",
                 "index_workflow_activity", "index_rebuild_activity", "audit_transcript_activity", "audit_scenario_activity"]


def _name(fn) -> str:
    return activity._Definition.from_callable(fn).name  # the name Temporal dispatches on, not the Python symbol


def test_queue_names_are_the_settings_queues():
    assert (Q.AUTHORING, Q.BUILD, Q.TRIALS, Q.CONTROL) == QUEUES
    assert set(Q.CONCURRENCY) == set(QUEUES)
    assert Q.CONCURRENCY == {Q.AUTHORING: 32, Q.BUILD: 8, Q.TRIALS: 64, Q.CONTROL: 16}


def test_every_registered_activity_has_a_queue():
    for fn in A.ALL:
        assert Q.queue_for(_name(fn)) in QUEUES, _name(fn)
    for name in OTHER_STREAMS:
        assert Q.queue_for(name) == Q.CONTROL, name


def test_routing_pins_the_brief():
    assert Q.queue_for("probe_candidate_activity") == Q.BUILD == Q.queue_for("compile_activity") == Q.queue_for("validate_activity")
    assert Q.queue_for("validate_control_activity") == Q.BUILD == Q.queue_for("evaluate_controls_activity")
    assert Q.queue_for("launch_trial_activity") == Q.TRIALS == Q.queue_for("await_trial_activity")
    assert Q.queue_for("cancel_trial_activity") == Q.TRIALS == Q.queue_for("cleanup_trial_activity")
    assert Q.queue_for("judge_one_activity") == Q.AUTHORING == Q.queue_for("discover_activity") == Q.queue_for("difficulty_label_activity")
    assert Q.queue_for("mine_activity") == Q.AUTHORING  # discovery is GitHub/CSV bound, not Modal bound
    assert Q.queue_for("record_cost_activity") == Q.CONTROL == Q.queue_for("spend_check_activity")


def test_unrouted_activity_is_a_value_error():
    assert issubclass(Q.UnroutedActivity, ValueError)
    with pytest.raises(Q.UnroutedActivity, match="nope_activity"):
        Q.queue_for("nope_activity")


def test_activities_for_partitions_the_map():
    seen: list[str] = []
    for queue in QUEUES:
        names = Q.activities_for(queue)
        assert names == sorted(names) and all(Q.queue_for(n) == queue for n in names)
        seen += names
    assert sorted(seen) == sorted(Q.QUEUE_OF) and len(seen) == len(set(seen))
    with pytest.raises(Q.UnroutedActivity):
        Q.activities_for("swesim")  # the retired single queue is not a queue


@pytest.mark.parametrize("policy,initial,maximum,attempts", [
    (R.LLM, timedelta(seconds=10), timedelta(minutes=2), 4),
    (R.BUILD, timedelta(seconds=30), timedelta(minutes=5), 3),
    (R.READ, timedelta(seconds=5), timedelta(minutes=1), 5),
])
def test_retry_classes_have_the_fixed_shape(policy, initial, maximum, attempts):
    assert policy.initial_interval == initial and policy.maximum_interval == maximum
    assert policy.backoff_coefficient == 2.0 and policy.maximum_attempts == attempts


def test_launch_is_once():
    assert R.ONCE.maximum_attempts == 1


def test_error_constructors_classify():
    e = R.non_retryable("gate rejected", kind="gate")
    assert isinstance(e, ApplicationError) and e.non_retryable is True and e.type == "gate" and "gate rejected" in str(e)
    t = R.transient("provider 503", kind="provider")
    assert isinstance(t, ApplicationError) and t.non_retryable is False and t.type == "provider"


def test_search_attribute_keys_are_the_six_keywords():
    assert SA.KEYS == (SA.SCENARIO_ID, SA.MODEL, SA.CONDITION, SA.STATUS, SA.VERSION, SA.PURPOSE)
    assert SA.KEYS == ("swesim_scenario_id", "swesim_model", "swesim_condition", "swesim_status", "swesim_version", "swesim_purpose")
    for name in SA.KEYS:
        assert SA.key(name).indexed_value_type == SearchAttributeIndexedValueType.KEYWORD


def test_attributes_builds_typed_pairs_and_skips_unset_values():
    attrs = SA.attributes(scenario_id="a--1", version=2, purpose="screen", status="running")
    assert isinstance(attrs, TypedSearchAttributes)
    pairs = {p.key.name: p.value for p in attrs}
    assert pairs == {"swesim_scenario_id": "a--1", "swesim_version": "2", "swesim_purpose": "screen", "swesim_status": "running"}
    assert len(SA.attributes()) == 0
    assert {p.key.name for p in SA.attributes(model="m", condition="hidden")} == {"swesim_model", "swesim_condition"}


def test_upsert_status_outside_a_workflow_is_a_noop():
    SA.upsert_status("completed")  # direct tests run workflow bodies as plain coroutines; nothing to upsert into


def test_worker_executor_is_sized_to_the_activity_concurrency(tmp_path):
    """A 1-vCPU instance gets cpu_count+4 executor threads by default; with 32 concurrent activities a bare to_thread
    queues behind them and misses its heartbeat window (measured 2026-09-13). The pool must cover every activity."""
    from swesim import settings as S
    from swesim.workflows import worker as W
    s = S.Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_TASK_QUEUE": "swesim-authoring"})
    assert W.executor_size(s) >= W.concurrency(s, "swesim-authoring") + 8
    s_all = S.Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env")})
    assert W.executor_size(s_all) >= sum(W.concurrency(s_all, q) for q in s_all.task_queues)


def test_blocking_without_drain_releases_immediately_on_cancel_and_slot_wait_heartbeats():
    """Discovery must release its slot the moment it is cancelled (the thread finishes on its own) and a later attempt
    waiting for the slot must heartbeat instead of sitting silent (measured 2026-09-13: five attempts timed out in a row)."""
    import asyncio, threading, time
    from swesim.workflows import activities as A

    async def scenario():
        gate = threading.Event()
        def slow():
            gate.wait(5); return "done"
        t = asyncio.ensure_future(A.blocking(slow, every=0.05, drain_on_cancel=False))
        await asyncio.sleep(0.15); t.cancel()
        started = time.monotonic()
        try:
            await t
        except asyncio.CancelledError:
            pass
        assert time.monotonic() - started < 1.0, "cancel must not wait for the thread when drain_on_cancel is False"
        gate.set()
        # slot wait heartbeats: hold the slot, then acquire from another coroutine with a tiny poll interval
        beats = []
        orig = A._heartbeat
        A._heartbeat = lambda *d: beats.append(d)
        try:
            await A.DISCOVERY_SLOTS.acquire()
            waiter = asyncio.ensure_future(A.acquire_discovery_slot(every=0.05))
            await asyncio.sleep(0.2)
            assert beats and beats[0][0]["waiting_for"] == "discovery-slot"
            A.DISCOVERY_SLOTS.release()
            await asyncio.wait_for(waiter, 2)
            A.DISCOVERY_SLOTS.release()
        finally:
            A._heartbeat = orig
    asyncio.run(scenario())
