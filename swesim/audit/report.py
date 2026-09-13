"""Aggregates of the task-sense audit over the record, rendered as JSON and Markdown.

Contract
- ``build_report(layout, registry_path)`` reads every ``scenarios/*/audit.json`` and ``trials/*/audit.json`` plus the trial's
  ``record.json``/``run_config.json``/``report.json``/``transcript.json`` and the registry, and returns one JSON-able dict:
  per-scenario labels with the leaderboard gate (``coherent and fair_tests``), per-model interaction metrics (trials,
  asked-before-implementing rate, questions, useful-question rate, replies and their disclosure labels, reply-filter
  regenerations/redactions, integrity signals per trial), per-scenario question/reply counts, the list of disclosure
  violations (trial id + seq + evidence) and the list of excluded trials with the reason.
- Pooling rules: only interactive trials on the registry's current spec hash with stakeholder v2 (or undeterminable
  version, listed separately) enter the model rows; legacy (v1) and archived-spec trials are listed under ``excluded``;
  non-interactive trials are counted and skipped. Rates have explicit denominators; an empty denominator yields ``None``.
- Numbers in the Markdown come from the same dict, so the two renderings never disagree.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from swesim.audit import rubric as R
from swesim.audit.record import RecordPaths
from swesim.paths import atomic_json


def _read(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _registry(paths: RecordPaths, registry_path: Path | None) -> dict[str, dict]:
    raw = _read(Path(registry_path) if registry_path else paths.registry_path())
    entries = (raw or {}).get("scenarios") if isinstance(raw, dict) else None
    if isinstance(entries, dict):
        return {sid: e for sid, e in entries.items() if isinstance(e, dict)}
    if isinstance(entries, list):
        return {e.get("id"): e for e in entries if isinstance(e, dict) and e.get("id")}
    return {}


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _scenario_rows(paths: RecordPaths, registry: dict[str, dict]) -> tuple[list[dict], list[str]]:
    rows, unaudited = [], []
    root = paths.scenarios_root()
    for sdir in sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []:
        raw = _read(sdir / "audit.json")
        reg = registry.get(sdir.name, {})
        if raw is None:
            unaudited.append(sdir.name)
            continue
        try:
            a = R.ScenarioAudit.model_validate(raw)
        except ValidationError:
            unaudited.append(sdir.name)
            continue
        validation = _read(sdir / "validation.json")
        rows.append({
            "scenario_id": a.scenario_id, "version": reg.get("version"), "spec_hash": a.spec_hash, "registered": bool(reg),
            "current": bool(reg) and reg.get("spec_hash") == a.spec_hash,
            "validation_status": reg.get("validation_status") or (validation.get("status") if isinstance(validation, dict) else None),
            "coherent": a.coherent, "fair_tests": a.fair_tests, "plausible_withholding": a.plausible_withholding, "reasons": a.reasons,
            "unfair_tests": a.unfair_tests, "model": a.model, "audited_at": a.audited_at, "rubric_version": a.rubric_version,
            "leaderboard_eligible": bool(a.coherent and a.fair_tests),
        })
    return rows, unaudited


def _row_key(model: str, version: str | None) -> str:
    return f"{model} @ {version or 'unknown'}"


def _model_row(model: str, version: str | None) -> dict:
    return {"model": model, "stakeholder_version": version, "trials": 0, "trial_ids": [], "unknown_version_trials": [], "asked_trials": 0,
            "asked_before_implementing": {"true": 0, "false": 0, "unknown": 0, "rate": None},
            "questions": 0, "useful_questions": 0, "unlabelled_questions": 0, "useful_question_rate": None, "questions_per_trial": None,
            "questions_before_implementation": 0, "questions_after_implementation": 0,
            "confirmations": 0, "replies": 0, "targeted": 0, "not_targeted": 0, "targeted_unknown": 0, "volunteered_replies": 0,
            "test_or_identifier_disclosed": 0, "behaviour_level_false": 0, "behaviour_level_unknown": 0, "undeclared_replies": 0,
            "ungrounded_replies": 0,
            "regenerated_replies": 0, "redacted_replies": 0, "patience_spent": 0, "patience_refusals": 0,
            "integrity_signals": 0, "flagged_trials": []}


def _finish_model_row(m: dict) -> dict:
    abi = m["asked_before_implementing"]
    abi["rate"] = _rate(abi["true"], abi["true"] + abi["false"])
    labelled = m["questions"] - m["unlabelled_questions"]
    m["useful_question_rate"] = _rate(m["useful_questions"], labelled)
    m["questions_per_trial"] = _rate(m["questions"], m["trials"])
    return m


def build_report(layout, registry_path: Path | None = None, *, llm_usage: dict | None = None) -> dict:
    """See the module contract."""
    paths = RecordPaths(layout)
    registry = _registry(paths, registry_path)
    scenarios, unaudited = _scenario_rows(paths, registry)
    models: dict[str, dict] = OrderedDict()
    per_scenario: dict[str, dict] = OrderedDict()
    violations: list[dict] = []
    excluded: list[dict] = []
    counts = {"trial_dirs": 0, "audited": 0, "non_interactive": 0, "included": 0, "legacy": 0, "archived_spec": 0,
              "unregistered_scenario": 0, "unknown_version": 0, "invalid_audit": 0}
    troot = paths.trials_root()
    for tdir in sorted(p for p in troot.iterdir() if p.is_dir()) if troot.exists() else []:
        counts["trial_dirs"] += 1
        raw = _read(tdir / "audit.json")
        if raw is None:
            continue
        try:
            a = R.TrialAudit.model_validate(raw)
        except ValidationError:
            counts["invalid_audit"] += 1
            continue
        counts["audited"] += 1
        record = _read(tdir / "record.json") or {}
        rc = _read(tdir / "run_config.json") or record.get("run_config") or {}
        report = _read(tdir / "report.json") or record.get("report") or {}
        transcript = _read(tdir / "transcript.json") or record.get("transcript") or {}
        condition = rc.get("condition") or record.get("condition")
        model = rc.get("model") or record.get("model") or "unknown"
        sid = a.scenario_id or record.get("scenario_id") or "unknown"
        if condition != "interactive":
            counts["non_interactive"] += 1
            continue
        reg = registry.get(sid)
        base = {"trial_id": a.trial_id, "model": model, "scenario_id": sid, "spec_hash": a.spec_hash}
        if reg is None:
            counts["unregistered_scenario"] += 1
            excluded.append({**base, "reason": "scenario not in the registry"})
            continue
        reasons = []
        if reg.get("spec_hash") != a.spec_hash:
            counts["archived_spec"] += 1
            reasons.append(f"archived spec (registry has version {reg.get('version')}, spec {str(reg.get('spec_hash'))[:8]})")
        if a.legacy:
            counts["legacy"] += 1
            reasons.append("legacy stakeholder v1")
        if reasons:
            excluded.append({**base, "reason": "; ".join(reasons)})
            continue
        counts["included"] += 1
        key = _row_key(model, a.stakeholder_version)      # stakeholder versions never pool (models.py: v3 = v2 prompt on Opus 5)
        m = models.setdefault(key, _model_row(model, a.stakeholder_version))
        s = per_scenario.setdefault(sid, {"trials": 0, "models": {}, "questions": 0, "replies": 0, "volunteered_replies": 0,
                                          "test_or_identifier_disclosed": 0, "asked_trials": 0})
        m["trials"] += 1
        m["trial_ids"].append(a.trial_id)
        s["trials"] += 1
        s["models"][key] = s["models"].get(key, 0) + 1
        if a.stakeholder_version is None:
            counts["unknown_version"] += 1
            m["unknown_version_trials"].append(a.trial_id)
        if a.questions:
            m["asked_trials"] += 1
            s["asked_trials"] += 1
        key = {True: "true", False: "false", None: "unknown"}[a.asked_before_implementing]
        m["asked_before_implementing"][key] += 1
        for q in a.questions:
            m["questions"] += 1
            s["questions"] += 1
            u = R.useful(q)
            if u is None:
                m["unlabelled_questions"] += 1
            elif u:
                m["useful_questions"] += 1
            if q.before_implementation is True:
                m["questions_before_implementation"] += 1
            elif q.before_implementation is False:
                m["questions_after_implementation"] += 1
        events = {e.get("seq"): e for e in (transcript.get("events") or []) if isinstance(e, dict) and e.get("kind") == "message"}
        for r in a.replies:
            m["replies"] += 1
            s["replies"] += 1
            ev = events.get(r.seq, {})
            if ev.get("exchange") == "confirm":
                m["confirmations"] += 1
            if r.targeted is True:
                m["targeted"] += 1
            elif r.targeted is False:
                m["not_targeted"] += 1
            else:
                m["targeted_unknown"] += 1
            if r.volunteered:
                m["volunteered_replies"] += 1
                s["volunteered_replies"] += 1
            if r.test_or_identifier_disclosed:
                m["test_or_identifier_disclosed"] += 1
                s["test_or_identifier_disclosed"] += 1
            if r.behaviour_level is False:
                m["behaviour_level_false"] += 1
            elif r.behaviour_level is None:
                m["behaviour_level_unknown"] += 1
            if r.undeclared:
                m["undeclared_replies"] += 1
            if r.ungrounded:
                m["ungrounded_replies"] += 1
            if ev.get("regenerated"):
                m["regenerated_replies"] += 1
            if ev.get("redacted_terms"):
                m["redacted_replies"] += 1
            kinds = []
            if r.test_or_identifier_disclosed:
                kinds.append("test_or_identifier_disclosed")
            if r.ungrounded:
                kinds.append("ungrounded_requirement")
            if r.volunteered:
                kinds.append("volunteered")
            if r.behaviour_level is False:
                kinds.append("not_behaviour_level")
            if kinds:
                violations.append({**base, "seq": r.seq, "exchange": ev.get("exchange"), "kinds": kinds, "hits": r.hits,
                                   "ungrounded": r.ungrounded, "volunteered": r.volunteered, "reason": r.reason})
        if a.patience:
            m["patience_spent"] += a.patience.spent
            m["patience_refusals"] += a.patience.refusals
        signals = report.get("integrity_signals") or []
        if signals:
            m["integrity_signals"] += len(signals)
            m["flagged_trials"].append({"trial_id": a.trial_id, "scenario_id": sid,
                                        "signals": [sg.get("kind") if isinstance(sg, dict) else str(sg) for sg in signals],
                                        "dropped_paths": report.get("dropped_paths") or []})
    for m in models.values():
        _finish_model_row(m)
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rubric_version": R.RUBRIC_VERSION,
            "root": str(paths.root), "scenarios": scenarios, "unaudited_scenarios": unaudited, "models": models,
            "per_scenario": per_scenario, "disclosure_violations": violations, "excluded": excluded, "trials": counts,
            "llm_usage": llm_usage}


# ---------------------------------------------------------------- markdown

def _yn(v) -> str:
    return {True: "yes", False: "no", None: "unknown"}[v]


def _pct(v) -> str:
    return "n/a" if v is None else f"{100 * v:.0f}%"


def render_markdown(report: dict) -> str:
    """Markdown tables over ``build_report``'s dict."""
    out = [f"# Task-sense audit report ({report['generated_at']}, rubric {report['rubric_version']})", "",
           f"Record root: `{report['root']}`.", "", "## Scenarios", "",
           "| scenario | version | current | validation | coherent | fair tests | plausible withholding | leaderboard | model |",
           "|---|---|---|---|---|---|---|---|---|"]
    for s in report["scenarios"]:
        out.append(f"| {s['scenario_id']} | {s['version']} | {_yn(s['current'])} | {s['validation_status']} | {_yn(s['coherent'])} | "
                   f"{_yn(s['fair_tests'])} | {_yn(s['plausible_withholding'])} | {'eligible' if s['leaderboard_eligible'] else 'gated out'} | {s['model']} |")
    for s in report["scenarios"]:
        out += ["", f"### {s['scenario_id']}", ""]
        for k in ("coherent", "fair_tests", "plausible_withholding"):
            out.append(f"- **{k}** = {_yn(s[k])}: {s['reasons'].get(k, '')}")
        if s["unfair_tests"]:
            out.append(f"- unfair tests: {', '.join(f'`{t}`' for t in s['unfair_tests'])}")
    if report["unaudited_scenarios"]:
        out += ["", f"Scenarios without an audit: {', '.join(report['unaudited_scenarios'])}."]
    c = report["trials"]
    out += ["", "## Trials", "",
            f"{c['trial_dirs']} trial directories, {c['audited']} with an audit.json; {c['non_interactive']} non-interactive (not audited for "
            f"interaction), {c['included']} interactive trials pooled below; excluded: {c['legacy']} legacy (stakeholder v1), "
            f"{c['archived_spec']} on an archived spec, {c['unregistered_scenario']} on unregistered scenarios; {c['unknown_version']} pooled "
            f"trials have an undeterminable stakeholder version (no exchange).", "",
            "| model | stakeholder | trials | asked | asked before implementing (true/false/unknown) | rate | questions | useful | useful rate | "
            "confirmations | replies | targeted / not / unknown | volunteered | test or identifier disclosed | ungrounded requirement | "
            "not behaviour-level | undeclared facts | regenerated | redacted | patience spent | integrity signals |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for m in report["models"].values():
        abi = m["asked_before_implementing"]
        out.append(f"| {m['model']} | {m['stakeholder_version'] or 'unknown'} | {m['trials']} | {m['asked_trials']} | "
                   f"{abi['true']}/{abi['false']}/{abi['unknown']} | {_pct(abi['rate'])} | "
                   f"{m['questions']} | {m['useful_questions']} | {_pct(m['useful_question_rate'])} | {m['confirmations']} | {m['replies']} | "
                   f"{m['targeted']} / {m['not_targeted']} / {m['targeted_unknown']} | {m['volunteered_replies']} | {m['test_or_identifier_disclosed']} | "
                   f"{m['ungrounded_replies']} | {m['behaviour_level_false']} | {m['undeclared_replies']} | {m['regenerated_replies']} | {m['redacted_replies']} | "
                   f"{m['patience_spent']} | {m['integrity_signals']} |")
    for key, m in report["models"].items():
        if m["flagged_trials"]:
            out += ["", f"Integrity signals under {key}:"]
            out += [f"- `{f['trial_id']}` ({f['scenario_id']}): {', '.join(f['signals'])}; dropped paths {f['dropped_paths']}" for f in m["flagged_trials"]]
    out += ["", "## Per scenario (pooled interactive trials)", "", "| scenario | trials | models | asked | questions | replies | volunteered | test or identifier disclosed |",
            "|---|---|---|---|---|---|---|---|"]
    for sid, s in report["per_scenario"].items():
        models = ", ".join(f"{k} x{v}" for k, v in s["models"].items())
        out.append(f"| {sid} | {s['trials']} | {models} | {s['asked_trials']} | {s['questions']} | {s['replies']} | {s['volunteered_replies']} | {s['test_or_identifier_disclosed']} |")
    out += ["", "## Disclosure violations", ""]
    if not report["disclosure_violations"]:
        out.append("None.")
    for v in report["disclosure_violations"]:
        out.append(f"- `{v['trial_id']}` seq {v['seq']} ({v['exchange']}, {v['model']}, {v['scenario_id']}): {', '.join(v['kinds'])}; "
                   f"terms {v['hits']}; ungrounded {v['ungrounded']}; volunteered {v['volunteered']}. {v['reason']}")
    out += ["", "## Excluded trials", ""]
    if not report["excluded"]:
        out.append("None.")
    out += [f"- `{e['trial_id']}` ({e['model']}, {e['scenario_id']}): {e['reason']}" for e in report["excluded"]]
    u = report.get("llm_usage")
    if u:
        out += ["", "## Model usage", "",
                f"{u['calls']} successful calls ({u['failed_calls']} failed), ~{u['estimated_input_tokens']} input and ~{u['estimated_output_tokens']} "
                f"output tokens, estimated ${u['estimated_cost_usd']:.2f}. {u['price_source']}."]
        for model, m in u["by_model"].items():
            out.append(f"- {model}: {m['calls']} calls, ~{m['estimated_input_tokens']} in / ~{m['estimated_output_tokens']} out tokens, ~${m['estimated_cost_usd']:.2f}")
    return "\n".join(out) + "\n"


def write_report(report: dict, out_dir: Path, stem: str) -> tuple[Path, Path]:
    """Write ``<stem>.json`` and ``<stem>.md`` under ``out_dir`` atomically; returns (md_path, json_path)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    atomic_json(json_path, report)
    tmp = md_path.with_name(f".write-{md_path.name}")
    tmp.write_text(render_markdown(report))
    tmp.replace(md_path)
    return md_path, json_path
