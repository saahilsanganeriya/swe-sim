"""Audit one scenario or one trial and write its ``audit.json`` (interfaces §9.1) idempotently.

Contract
- ``audit_scenario(layout, sid)`` writes ``scenarios/<sid>/audit.json``; ``audit_trial(layout, tid)`` writes
  ``trials/<tid>/audit.json``. Writes are atomic (write-then-rename). An existing file with the same ``rubric_version`` and
  ``inputs_hash`` is returned unchanged and no model is called, unless ``force``; a stale rubric, a changed transcript,
  trajectory, spec or hidden asset changes the hash and re-audits.
- Labels combine the mechanical rules of ``swesim.audit.rubric`` with the model's reading (``swesim.audit.llm_audit``):
  unambiguous mechanical evidence wins where it exists (a restated brief sentence is ``redundant``; a reply-filter term in a
  reply is ``test_or_identifier_disclosed``; ``before_implementation`` is trajectory order), the model decides the reading
  labels (specific, decision_oriented, fishing, targeted, volunteered, behaviour_level) and fills the rest; a label nobody
  could decide stays ``None``. Every reason names its evidence.
- Stakeholder v1 trials are labelled ``legacy`` and get mechanical labels only (no model call); trials without exchanges
  and non-interactive trials get an empty audit with no model call.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from swesim.audit import rubric as R
from swesim.audit.llm_audit import SCENARIO_PROMPT_VERSION, TRIAL_PROMPT_VERSION, Judge
from swesim.audit.record import RecordPaths, ScenarioBundle, TrialBundle, load_scenario, load_trial
from swesim.paths import atomic_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def scenario_inputs_hash(b: ScenarioBundle, models: tuple[str, ...]) -> str:
    return _hash({"rubric": R.RUBRIC_VERSION, "prompt": SCENARIO_PROMPT_VERSION, "models": list(models), "spec_hash": b.spec_hash,
                  "assets": b.asset_hashes})


def trial_inputs_hash(b: TrialBundle, models: tuple[str, ...]) -> str:
    return _hash({"rubric": R.RUBRIC_VERSION, "prompt": TRIAL_PROMPT_VERSION, "models": list(models), "spec_hash": b.spec_hash,
                  "condition": b.condition, "brief_profile": b.brief_profile, "events": b.messages,
                  "patience": [b.transcript.get("patience_start"), b.transcript.get("patience_end")],
                  "trajectory": _hash(b.trajectory) if b.trajectory else None, "submission_paths": b.submission_paths,
                  "forbidden_terms": b.forbidden_terms, "stakeholder_version": b.stakeholder_version})


def scenario_audit_path(layout, scenario_id: str) -> Path:
    return RecordPaths(layout).scenario_dir(scenario_id) / "audit.json"


def trial_audit_path(layout, trial_id: str) -> Path:
    return RecordPaths(layout).trial_dir(trial_id) / "audit.json"


def _existing(path: Path, model_cls):
    try:
        return model_cls.model_validate(json.loads(path.read_text()))
    except (OSError, ValueError, ValidationError):  # absent, unreadable or from an older shape: re-audit
        return None


def read_scenario_audit(layout, scenario_id: str) -> R.ScenarioAudit | None:
    return _existing(scenario_audit_path(layout, scenario_id), R.ScenarioAudit)


def read_trial_audit(layout, trial_id: str) -> R.TrialAudit | None:
    return _existing(trial_audit_path(layout, trial_id), R.TrialAudit)


def _fresh(existing, inputs_hash: str, force: bool) -> bool:
    return (existing is not None and existing.status == "completed" and not force and existing.rubric_version == R.RUBRIC_VERSION and existing.inputs_hash == inputs_hash)


# ---------------------------------------------------------------- scenarios

def audit_scenario_result(layout, scenario_id: str, *, force: bool = False, judge: Judge | None = None) -> tuple[R.ScenarioAudit, bool]:
    """(audit, written): written is False when the existing audit.json was current."""
    b = load_scenario(layout, scenario_id)
    judge = judge or Judge()
    h = scenario_inputs_hash(b, judge.models)
    path = scenario_audit_path(layout, scenario_id)
    existing = _existing(path, R.ScenarioAudit)
    if _fresh(existing, h, force):
        return existing, False
    out, model = judge.judge_scenario(b)
    reasons = dict(out["reasons"])
    plausible = out["plausible_withholding"]
    if b.withheld_brief is None:
        plausible = None
        reasons["plausible_withholding"] = "no withheld brief in this spec (natural profile only); nothing to judge"
    audit = R.ScenarioAudit(scenario_id=scenario_id, spec_hash=b.spec_hash, coherent=out["coherent"], fair_tests=out["fair_tests"],
                            plausible_withholding=plausible, reasons=reasons, unfair_tests=out["unfair_tests"], model=model,
                            audited_at=_now(), rubric_version=R.RUBRIC_VERSION, inputs_hash=h)
    atomic_json(path, audit.model_dump(mode="json"))
    return audit, True


def audit_scenario(layout, scenario_id: str, *, force: bool = False, judge: Judge | None = None) -> R.ScenarioAudit:
    return audit_scenario_result(layout, scenario_id, force=force, judge=judge)[0]


# ---------------------------------------------------------------- trials

def _join(*parts: str | None) -> str:
    return "; ".join(p for p in parts if p)


def _collapse_prefix_hits(hits: list[str]) -> list[str]:
    """``test_`` is subsumed by ``test_interval_with_bools``: keep the identifier, drop the prefix term."""
    return [h for h in hits if not (h.endswith("_") and any(o != h and o.startswith(h) for o in hits))]


def _question_labels(b: TrialBundle, llm: dict[int, dict], before: dict[int, bool | None], edit: tuple[int, str] | None) -> list[R.QuestionLabel]:
    labels = []
    for q in b.questions:
        seq = int(q.get("seq", 0))
        lq = llm.get(seq, {})
        restated = R.redundant_with_brief(q.get("content", ""), b.brief_seen_by_agent)
        if before.get(seq) is None:
            timing = "trajectory unavailable, order unknown" if b.trajectory is None else "question not placed in the trajectory"
        elif before[seq]:
            timing = "asked before the first implementation edit" + (f" (step {edit[0]}: {edit[1]})" if edit else " (no edit found)")
        else:
            timing = f"asked after the first implementation edit (step {edit[0]}: {edit[1]})" if edit else "asked after implementation"
        labels.append(R.QuestionLabel(
            seq=seq, specific=lq.get("specific"), decision_oriented=lq.get("decision_oriented"), before_implementation=before.get(seq),
            redundant=True if restated else lq.get("redundant"), fishing=lq.get("fishing"),
            reason=_join(timing, "restates a sentence of the brief" if restated else None, lq.get("reason") or None,
                         None if lq else ("not read by the model (legacy stakeholder v1)" if b.legacy else "no model label for this question"))))
    return labels


def _reply_labels(b: TrialBundle, llm: dict[int, dict], model_ran: bool) -> list[R.ReplyLabel]:
    labels = []
    for r in b.replies:
        seq = int(r.get("seq", 0))
        lr = llm.get(seq, {})
        prompt = b.prompt_for(r)
        mech = R.disclosure_targeted(r.get("content", ""), prompt, list(r.get("disclosed_fact_ids") or []), b.visible_facts)
        hits = _collapse_prefix_hits(R.test_or_identifier_disclosed(r.get("content", ""), b.forbidden_terms))
        code_hits = R.code_like_hits(hits)
        ungrounded = R.conveyed_requirements(r.get("content", ""), b.requirements, ungrounded_only=True)
        if code_hits or ungrounded:                   # unambiguous: an identifier, test vocabulary, or a test-only requirement
            disclosed: bool | None = True
        elif lr.get("test_or_identifier_disclosed") is not None:
            disclosed = lr["test_or_identifier_disclosed"]
        else:
            disclosed = False if model_ran else None
        targeted = lr["targeted"] if "targeted" in lr else mech.targeted
        volunteered = lr["volunteered"] if lr else mech.volunteered
        mech_note = None
        if lr and (mech.volunteered != volunteered or mech.targeted != targeted):
            mech_note = f"mechanical overlap view: targeted={mech.targeted}, volunteered={mech.volunteered}"
        labels.append(R.ReplyLabel(
            seq=seq, targeted=targeted, volunteered=volunteered, test_or_identifier_disclosed=disclosed, behaviour_level=lr.get("behaviour_level"),
            hits=hits, undeclared=mech.undeclared, ungrounded=ungrounded,
            reason=_join(f"reply-filter identifiers present: {', '.join(code_hits)}" if code_hits else None,
                         f"reply-filter words present (left to the reading pass): {', '.join(h for h in hits if h not in code_hits)}"
                         if len(hits) > len(code_hits) else None,
                         f"conveys ungrounded (test-only) requirement {', '.join(ungrounded)}" if ungrounded else None,
                         f"conveys undeclared facts {', '.join(mech.undeclared)}" if mech.undeclared else None,
                         f"regenerated by the filter, redacted terms {r.get('redacted_terms')}" if r.get("regenerated") else None,
                         lr.get("reason") or None, mech_note,
                         None if lr else ("not read by the model (legacy stakeholder v1)" if b.legacy else "no model label for this reply"))))
    return labels


def audit_trial_result(layout, trial_id: str, *, force: bool = False, judge: Judge | None = None) -> tuple[R.TrialAudit, bool]:
    """(audit, written): written is False when the existing audit.json was current."""
    b = load_trial(layout, trial_id)
    judge = judge or Judge()
    h = trial_inputs_hash(b, judge.models)
    path = trial_audit_path(layout, trial_id)
    existing = _existing(path, R.TrialAudit)
    if _fresh(existing, h, force):
        return existing, False
    base = dict(trial_id=trial_id, scenario_id=b.scenario_id, spec_hash=b.spec_hash, trajectory_source=b.trajectory_source,
                audited_at=_now(), rubric_version=R.RUBRIC_VERSION, inputs_hash=h)
    if not b.interactive:
        audit = R.TrialAudit(**base, stakeholder_version=None, legacy=False, questions=[], replies=[], asked_before_implementing=None,
                             useful_question_rate=None, patience=None, model=None)
        atomic_json(path, audit.model_dump(mode="json"))
        return audit, True
    paths = b.submission_paths
    before = R.questions_before_implementation(b.transcript, b.trajectory, paths)
    edit = R.first_edit(b.trajectory, paths)
    model_ran = not b.legacy and bool(b.questions or b.replies)
    llm_q: dict[int, dict] = {}
    llm_r: dict[int, dict] = {}
    model = None
    if model_ran:
        out, model = judge.judge_trial(b)
        llm_q, llm_r = out["questions"], out["replies"]
    questions = _question_labels(b, llm_q, before, edit)
    replies = _reply_labels(b, llm_r, model_ran)
    audit = R.TrialAudit(**base, stakeholder_version=b.stakeholder_version, legacy=b.legacy, questions=questions, replies=replies,
                         asked_before_implementing=R.asked_before_implementing(b.transcript, b.trajectory, paths),
                         useful_question_rate=R.useful_question_rate(questions), patience=R.patience_spent(b.transcript), model=model)
    atomic_json(path, audit.model_dump(mode="json"))
    return audit, True


def audit_trial(layout, trial_id: str, *, force: bool = False, judge: Judge | None = None) -> R.TrialAudit:
    return audit_trial_result(layout, trial_id, force=force, judge=judge)[0]
