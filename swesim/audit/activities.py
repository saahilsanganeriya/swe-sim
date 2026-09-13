"""Temporal activities of the task-sense audit, routed to the control queue (interfaces §4): ``audit_scenario_activity``
with payload ``{"scenario_id"}`` and ``audit_transcript_activity`` with payload ``{"trial_id"}``.

Contract
- Payloads carry ids only. The record root comes from ``Settings`` (``SWESIM_WORKSPACE``), through
  ``swesim.storage.layout.Layout`` when it is importable and ``swesim.paths.Workspace`` otherwise.
- Idempotent: an activity that runs twice returns the same labels and writes nothing the second time (``written`` false).
- Errors are classified: a bad id or a record missing its files is the caller's problem (``ApplicationError`` non-retryable,
  type ``AuditInputError``); an exhausted model chain propagates as ``AuditLLMError`` for the LLM retry policy.
- The return value is a small dict for the workflow (ids, path, the gate labels, the number of model calls); the labels
  themselves live in ``audit.json`` and are projected by the index activities.
"""
from __future__ import annotations

import asyncio

from temporalio import activity
from temporalio.exceptions import ApplicationError

from swesim.audit import run as RUN
from swesim.audit.llm_audit import Judge
from swesim.audit.record import AuditInputError
from swesim.paths import Workspace
from swesim.settings import get_settings


def layout_from_settings():
    """The record layout for this worker: ``Layout`` once the storage stream's module is merged, ``Workspace`` until then."""
    s = get_settings()
    try:
        from swesim.storage.layout import Layout
    except ImportError:
        return Workspace(s.workspace, s.registry_path)
    return Layout(root=s.workspace)


def _required_id(p, key: str) -> str:
    value = p.get(key) if isinstance(p, dict) else None
    if not isinstance(value, str) or not value:
        raise ApplicationError(f"payload needs a non-empty {key!r}", type="AuditInputError", non_retryable=True)
    return value


@activity.defn
async def audit_scenario_activity(p: dict) -> dict:
    sid = _required_id(p, "scenario_id")
    layout = layout_from_settings()
    judge = Judge()
    try:
        audit, written = await asyncio.to_thread(RUN.audit_scenario_result, layout, sid, judge=judge)
    except AuditInputError as e:
        raise ApplicationError(str(e), type="AuditInputError", non_retryable=True) from e
    return {"scenario_id": sid, "path": str(RUN.scenario_audit_path(layout, sid)), "written": written, "coherent": audit.coherent,
            "fair_tests": audit.fair_tests, "plausible_withholding": audit.plausible_withholding, "model": audit.model,
            "rubric_version": audit.rubric_version, "llm_calls": judge.usage()["calls"]}


@activity.defn
async def audit_transcript_activity(p: dict) -> dict:
    tid = _required_id(p, "trial_id")
    layout = layout_from_settings()
    judge = Judge()
    try:
        audit, written = await asyncio.to_thread(RUN.audit_trial_result, layout, tid, judge=judge)
    except AuditInputError as e:
        raise ApplicationError(str(e), type="AuditInputError", non_retryable=True) from e
    return {"trial_id": tid, "path": str(RUN.trial_audit_path(layout, tid)), "written": written, "legacy": audit.legacy,
            "stakeholder_version": audit.stakeholder_version, "asked_before_implementing": audit.asked_before_implementing,
            "useful_question_rate": audit.useful_question_rate, "questions": len(audit.questions), "replies": len(audit.replies),
            "model": audit.model, "rubric_version": audit.rubric_version, "llm_calls": judge.usage()["calls"]}


ALL = [audit_scenario_activity, audit_transcript_activity]
