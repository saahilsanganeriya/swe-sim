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

import hashlib
import json

from temporalio import activity
from temporalio.exceptions import ApplicationError

from swesim.audit import run as RUN
from swesim.audit import rubric as R
from swesim.audit.usage import Ledger
from swesim.audit.llm_audit import Judge
from swesim.audit.record import AuditInputError
from swesim.paths import Workspace, atomic_json
from swesim.roles import ROLE_CHAINS
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


def _judge(p: dict) -> Judge:
    chain = tuple(ROLE_CHAINS["audit_model"])
    configured = p.get("audit_model") or chain[0]
    return Judge(models=(configured, *(model for model in chain if model != configured)))


def _pending(layout, identifier: str, judge: Judge, trial: bool, reason: str):
    if trial:
        b = RUN.load_trial(layout, identifier)
        return R.TrialAudit(trial_id=identifier, scenario_id=b.scenario_id, spec_hash=b.spec_hash, stakeholder_version=b.stakeholder_version,
                            legacy=b.legacy, questions=[], replies=[], asked_before_implementing=None, useful_question_rate=None,
                            patience=None, trajectory_source=b.trajectory_source, model=None, audited_at=RUN._now(),
                            rubric_version=R.RUBRIC_VERSION, inputs_hash=RUN.trial_inputs_hash(b, judge.models), status="pending")
    b = RUN.load_scenario(layout, identifier)
    return R.ScenarioAudit(scenario_id=identifier, spec_hash=b.spec_hash, coherent=None, fair_tests=None, plausible_withholding=None,
                           reasons={"audit": reason}, model=None, audited_at=RUN._now(), rubric_version=R.RUBRIC_VERSION,
                           inputs_hash=RUN.scenario_inputs_hash(b, judge.models), status="pending")


async def _execute(layout, identifier: str, judge: Judge, *, trial: bool):
    from swesim.workflows.activities import blocking
    path = RUN.trial_audit_path(layout, identifier) if trial else RUN.scenario_audit_path(layout, identifier)
    ledger = None
    if activity.in_activity():
        info = activity.info()
        ledger = Ledger(layout, path.parent, info.workflow_id, info.workflow_run_id, info.activity_id, info.attempt, identifier if trial else None)
        judge.on_call = ledger.capture
        await blocking(ledger.replay)  # repair a previous post-result ledger failure before another paid request
    fn = RUN.audit_trial_result if trial else RUN.audit_scenario_result
    try:
        audit, written = await blocking(lambda: fn(layout, identifier, judge=judge))
    except AuditInputError:
        raise
    except Exception as error:  # noqa: BLE001 - all measured failed attempts belong in the ledger
        if ledger is not None:
            for ordinal, call in enumerate(judge.calls):
                await blocking(lambda ordinal=ordinal, call=call: ledger.capture(call, ordinal))
            pending = _pending(layout, identifier, judge, trial, type(error).__name__)
            if path.exists():
                previous = json.loads(path.read_text())
                if previous.get("status", "completed") == "completed":
                    digest = hashlib.sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()
                    atomic_json(path.parent / "audit-history" / f"{digest}.json", previous)
            pending = pending.model_copy(update={**ledger.summary(), "cost_recorded": False})
            atomic_json(path, pending.model_dump(mode="json"))
            await blocking(ledger.replay)
            atomic_json(path, pending.model_copy(update={"cost_recorded": True}).model_dump(mode="json"))
        raise
    if ledger is not None:
        summary = ledger.summary(retained_without_receipt=not written and audit.model is not None and not ledger.receipts())
        audit = audit.model_copy(update={**summary, "cost_recorded": False})
        atomic_json(path, audit.model_dump(mode="json"))
        await blocking(ledger.replay)
        audit = audit.model_copy(update={"cost_recorded": True})
        atomic_json(path, audit.model_dump(mode="json"))
    return audit, written


@activity.defn
async def audit_scenario_activity(p: dict) -> dict:
    sid = _required_id(p, "scenario_id")
    layout = layout_from_settings()
    judge = _judge(p)
    try:
        audit, written = await _execute(layout, sid, judge, trial=False)
    except AuditInputError as e:
        raise ApplicationError(str(e), type="AuditInputError", non_retryable=True) from e
    return {"scenario_id": sid, "path": str(RUN.scenario_audit_path(layout, sid)), "written": written, "coherent": audit.coherent,
            "fair_tests": audit.fair_tests, "plausible_withholding": audit.plausible_withholding, "model": audit.model,
            "rubric_version": audit.rubric_version, "llm_calls": judge.usage()["calls"],
            "unknown_usage_count": audit.unknown_usage_count, "usage_complete": audit.usage_complete, "cost_recorded": audit.cost_recorded}


@activity.defn
async def audit_transcript_activity(p: dict) -> dict:
    tid = _required_id(p, "trial_id")
    layout = layout_from_settings()
    judge = _judge(p)
    try:
        audit, written = await _execute(layout, tid, judge, trial=True)
    except AuditInputError as e:
        raise ApplicationError(str(e), type="AuditInputError", non_retryable=True) from e
    return {"trial_id": tid, "path": str(RUN.trial_audit_path(layout, tid)), "written": written, "legacy": audit.legacy,
            "stakeholder_version": audit.stakeholder_version, "asked_before_implementing": audit.asked_before_implementing,
            "useful_question_rate": audit.useful_question_rate, "questions": len(audit.questions), "replies": len(audit.replies),
            "model": audit.model, "rubric_version": audit.rubric_version, "llm_calls": judge.usage()["calls"],
            "unknown_usage_count": audit.unknown_usage_count, "usage_complete": audit.usage_complete, "cost_recorded": audit.cost_recorded}


ALL = [audit_scenario_activity, audit_transcript_activity]
