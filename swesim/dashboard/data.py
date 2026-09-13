"""Workspace readers for the dashboard.

Everything here reads files the pipeline already writes (``workspace/scenarios/<id>/*``, ``workspace/trials/<id>/*``,
``registry.json``) and returns plain dicts for the renderer and the JSON APIs. Every reader is defensive: a missing
or malformed file is an empty state, never a 500. The stakeholder service is contacted only for the live transcript
of a running trial (``fetch_live_transcript``), with a short cache for the overview poll.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from swesim.stages.score import (
    OUTCOME_NAMES,
    UNRESOLVED_FILES,
    count_exchanges,
    normalize_record,
    outcome_code,
    unresolved_attempt,
)

TRIAL_RE = re.compile(r"[a-f0-9]{24}")
ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")
LIVE_TIMEOUT = 10.0
LIVE_CACHE_TTL = 4.0
REFUSAL_PREFIX = "I have to step away now"
STAGES = ("candidate", "judged", "drafted", "approved", "compiled", "validated", "registered")
SCENARIO_FILES = ("scenario.json", "scenario.draft.json", "draft.meta.json", "judge.json", "approval.json", "compile.json",
                  "validation.json", "scores.json")
_live_cache: dict[str, tuple[float, dict, str | None]] = {}


# ----------------------------------------------------------------------------- primitives

def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def to_ts(value) -> float | None:
    """Epoch seconds from a float, an int, or an ISO-8601 string (naive strings are taken as UTC). None otherwise."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        s = value.strip()
        try:
            dt = datetime.fromisoformat(s)  # Python 3.11+ accepts a trailing 'Z'
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
    return None


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds") if ts is not None else None


def clock(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M:%S") if ts is not None else "no timestamp"


def mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def short(x, n: int = 12) -> str:
    s = "" if x is None else str(x)
    return s if len(s) <= n else s[:n] + "…"


# ----------------------------------------------------------------------------- live transcript

def fetch_live_transcript(tid: str) -> tuple[dict, str | None]:
    """``GET {SWESIM_STAKEHOLDER_URL}/transcript/{tid}`` with the worker secret. Returns (transcript, error).
    Reads only the two keys it needs from the environment (python-dotenv, SWESIM_ENV_FILE); never logs them."""
    from dotenv import load_dotenv
    load_dotenv(os.environ.get("SWESIM_ENV_FILE", str(Path.home() / "Documents" / ".env")), override=False)
    base = os.environ.get("SWESIM_STAKEHOLDER_URL", "").rstrip("/")
    secret = os.environ.get("SWESIM_WORKER_SECRET", "")
    if not base:
        return {}, "SWESIM_STAKEHOLDER_URL not configured"
    if not secret:
        return {}, "SWESIM_WORKER_SECRET not configured"
    try:
        r = httpx.get(f"{base}/transcript/{tid}", headers={"X-Worker-Secret": secret}, timeout=LIVE_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return (data if isinstance(data, dict) else {}), None
    # Any failure degrades to "no live transcript", reported by exception type only (never URL or secret).
    except Exception as e:  # noqa: BLE001
        return {}, type(e).__name__


def live_transcript_cached(tid: str, now: float | None = None) -> tuple[dict, str | None]:
    """The overview polls every few seconds for every running trial; bound the calls to one per trial per TTL."""
    now = time.time() if now is None else now
    hit = _live_cache.get(tid)
    if hit and now - hit[0] < LIVE_CACHE_TTL:
        return hit[1], hit[2]
    transcript, error = fetch_live_transcript(tid)
    _live_cache[tid] = (now, transcript, error)
    return transcript, error


def patience_of(transcript: dict, run_config: dict | None = None) -> dict:
    """{start, end, tier, questions, reviews} from a transcript (live or frozen), start falling back to the run config."""
    events = [e for e in (transcript or {}).get("events") or [] if isinstance(e, dict) and e.get("kind") == "message"]
    last_reply = next((e for e in reversed(events) if e.get("role") == "assistant"), None)
    start = (transcript or {}).get("patience_start")
    if start is None:
        start = (((run_config or {}).get("stakeholder") or {}).get("patience_profile") or {}).get("start")
    end = (transcript or {}).get("patience_end")
    if end is None and last_reply is not None:
        end = last_reply.get("patience_remaining")
    return {"start": start, "end": end, "tier": (last_reply or {}).get("tier"),
            "questions": sum(1 for e in events if e.get("role") == "user" and e.get("exchange") == "ask"),
            "reviews": sum(1 for e in events if e.get("role") == "user" and e.get("exchange") == "review")}


# ----------------------------------------------------------------------------- trials

def trial_dirs(root: Path) -> list[Path]:
    trials = root / "trials"
    if not trials.is_dir():
        return []
    return sorted(p for p in trials.iterdir() if p.is_dir() and TRIAL_RE.fullmatch(p.name)
                  and any((p / name).exists() for name in ("record.json", *UNRESOLVED_FILES)))


def trial_row(d: Path, now: float | None = None, live: bool = False) -> dict:
    """One board row from status.json plus record.json, or from whatever an unresolved attempt left behind.
    With ``live=True`` a running trial's patience is fetched from the stakeholder service (cached)."""
    now = time.time() if now is None else now
    status = read_json(d / "status.json")
    raw = read_json(d / "record.json") if (d / "record.json").exists() else {}
    has_record = bool(raw)
    rec = normalize_record(raw, d) if has_record else {}
    u = ({} if has_record else unresolved_attempt(d)) or {}
    request = read_json(d / "request.json")
    rc = rec.get("run_config") or request.get("run_config") or u.get("run_config") or {}
    label = read_json(d / "label.json", default=None) or {}
    code = outcome_code(rec) if rec.get("reward") else None
    state = status.get("status") or u.get("status") or ("finished" if has_record else "unknown")
    running = state == "running" and not has_record
    started_at = status.get("started_at") or rec.get("started_at") or u.get("started_at")
    finished_at = status.get("finished_at") or rec.get("finished_at") or u.get("finished_at")
    started_ts, finished_ts = to_ts(started_at), to_ts(finished_at)
    elapsed = None
    if started_ts is not None:
        end = finished_ts if finished_ts is not None else (now if running else None)
        elapsed = max(0.0, end - started_ts) if end is not None else None
    patience = patience_of(rec.get("transcript") or {}, rc) if has_record else {"start": None, "end": None, "tier": None,
                                                                                     "questions": None, "reviews": None}
    live_error = None
    if running and live:
        transcript, live_error = live_transcript_cached(d.name, now)
        if transcript:
            patience = patience_of(transcript, rc)
        elif patience["start"] is None:
            patience = patience_of({}, rc)
    elif running and patience["start"] is None:
        patience = patience_of({}, rc)
    report = rec.get("report") or {}
    return {
        "trial_id": d.name, "status": state, "running": running, "unresolved": not has_record,
        "error_type": u.get("error_type"), "source_file": u.get("source_file"),
        "scenario_id": rec.get("scenario_id") or request.get("scenario_id") or u.get("scenario_id"),
        "condition": rec.get("condition") or request.get("condition") or u.get("condition"),
        "brief_profile": rec.get("brief_profile") or rc.get("brief_profile") or u.get("brief_profile"),
        "model": rec.get("model") or request.get("model") or rc.get("model") or u.get("model"),
        "purpose": rec.get("purpose") or rc.get("purpose") or u.get("purpose"),
        "attempt_index": rec.get("attempt_index", request.get("attempt_index")),
        "outcome": OUTCOME_NAMES[code] if code is not None else None, "outcome_code": code,
        "success": (rec.get("reward") or {}).get("success"), "reward": (rec.get("reward") or {}).get("reward"),
        "questions": count_exchanges(rec, "ask") if has_record else patience.get("questions"),
        "reviews": count_exchanges(rec, "review") if has_record else patience.get("reviews"),
        "patience_start": patience.get("start"), "patience_end": patience.get("end"), "patience_tier": patience.get("tier"),
        "label": label.get("label"), "started_at": started_at, "finished_at": finished_at,
        "started_ts": started_ts, "finished_ts": finished_ts, "elapsed_s": elapsed,
        "simulator_validity": report.get("simulator_validity"), "transcript_binding_valid": report.get("transcript_binding_valid"),
        "live_error": live_error, "spec_hash": rec.get("spec_hash") or request.get("spec_hash"),
        "run_config_hash": rec.get("run_config_hash") or request.get("run_config_hash") or u.get("run_config_hash"),
    }


def trial_board(root: Path, now: float | None = None, live: bool = False) -> list[dict]:
    now = time.time() if now is None else now
    rows = [trial_row(d, now, live) for d in trial_dirs(root)]
    rows.sort(key=lambda r: (r["started_ts"] or 0.0, r["trial_id"]), reverse=True)
    return rows


# ----------------------------------------------------------------------------- scenarios

def source_label(source: str | None, source_ref: str | None) -> tuple[str, str | None]:
    """Human label and an optional URL for a scenario source."""
    ref = source_ref or ""
    if source == "swebench-annotated":
        m = re.fullmatch(r"([A-Za-z0-9_.-]+)__([A-Za-z0-9_.-]+)-(\d+)", ref)
        url = f"https://github.com/{m.group(1)}/{m.group(2)}/pull/{m.group(3)}" if m else None
        return f"SWE-bench · {ref}", url
    if source == "github-repo":
        return f"GitHub · {ref}", ref if ref.startswith("http") else None
    return f"{source or 'unknown source'} · {ref}".strip(" ·"), ref if ref.startswith("http") else None


def _candidate_refs(root: Path) -> set[str]:
    refs: set[str] = set()
    cdir = root / "candidates"
    if not cdir.is_dir():
        return refs
    for p in cdir.glob("*.jsonl"):
        try:
            for line in p.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if isinstance(row, dict):
                    for key in ("instance_id", "source_ref", "pr_url", "url"):
                        if row.get(key):
                            refs.add(str(row[key]))
        except (OSError, ValueError):
            continue
    return refs


def scores_summary(sdir: Path) -> dict:
    """{groups: [...], unreadable: bool, exists: bool} from scores.json."""
    p = sdir / "scores.json"
    if not p.exists():
        return {"exists": False, "unreadable": False, "groups": []}
    groups = read_json(p, default="unreadable")
    if not isinstance(groups, dict):
        return {"exists": True, "unreadable": True, "groups": []}
    out = []
    for key, g in sorted(groups.items()):
        if not isinstance(g, dict):
            continue
        out.append({"key": key, "condition": g.get("condition"), "brief_profile": g.get("brief_profile"), "model": g.get("model"),
                    "purpose": g.get("purpose"), "requested_k": g.get("requested_k"), "n_valid": g.get("n_valid"),
                    "n_total": g.get("n_total"), "attempts_total": g.get("attempts_total"),
                    "attempts_unresolved": g.get("attempts_unresolved"), "success_count": g.get("success_count"),
                    "pass_at_k": g.get("pass_at_k"), "pass_pow_k": g.get("pass_pow_k"), "mean_reward": g.get("mean_reward"),
                    "outcomes": g.get("outcomes") or {}, "labels": g.get("labels") or {}, "trial_ids": g.get("trial_ids") or [],
                    "unresolved": g.get("unresolved") or [], "unresolved_by_status": g.get("unresolved_by_status") or {},
                    "inconsistent_reward": g.get("inconsistent_reward") or []})
    return {"exists": True, "unreadable": False, "groups": out}


def scenario_stages(sdir: Path, registry_entry: dict | None, candidate_refs: set[str], spec: dict, judge: dict,
                    approval: dict, compiled: dict, validation: dict) -> list[dict]:
    """Stage chips derived from the files that exist: candidate -> judged -> drafted -> approved -> compiled -> validated -> registered."""
    stages: list[dict] = []
    ref = spec.get("source_ref")
    has_draft = (sdir / "scenario.draft.json").exists() or (sdir / "draft.meta.json").exists()
    stages.append({"stage": "candidate", "state": "done" if (ref in candidate_refs or has_draft or spec) else "pending",
                   "detail": "candidate row present" if ref in candidate_refs else ("consumed by drafting" if has_draft else "")})
    if judge:
        keep = judge.get("keep")
        stages.append({"stage": "judged", "state": "done" if keep else ("failed" if keep is False else "done"),
                       "detail": f"keep={keep} · rubric {judge.get('verified_rubric_score')}/3 · complexity {judge.get('complexity')}"})
    else:
        stages.append({"stage": "judged", "state": "pending", "detail": "swesim judge writes judge.json"})
    stages.append({"stage": "drafted", "state": "done" if has_draft else "pending",
                   "detail": "scenario.draft.json" if has_draft else "swesim draft writes scenario.draft.json + assets/"})
    if approval:
        stages.append({"stage": "approved", "state": "done", "detail": f"{approval.get('approved_by')}"
                       + (" · review needed" if approval.get("review_needed") else "") + f" · v{approval.get('version')}"})
    else:
        stages.append({"stage": "approved", "state": "pending", "detail": "swesim approve --auto <id> writes approval.json + scenario.json"})
    if compiled:
        stages.append({"stage": "compiled", "state": "done", "detail": f"image {short(compiled.get('image_digest'), 19)} · "
                                                                      f"build {short(compiled.get('compiled_hash'), 8)}"})
    else:
        stages.append({"stage": "compiled", "state": "pending", "detail": "swesim compile <id> writes compile.json and the task dirs"})
    if validation:
        failures = validation.get("failures") or []
        state = "done" if validation.get("status") == "valid" else "failed"
        detail = "valid" if state == "done" else f"invalid · {len(failures)} failure(s)" + (f" · {failures[0]}" if failures else "")
        stages.append({"stage": "validated", "state": state, "detail": detail})
    else:
        stages.append({"stage": "validated", "state": "pending", "detail": "swesim validate <id> runs the controls on Modal"})
    stages.append({"stage": "registered", "state": "done" if registry_entry else "pending",
                   "detail": "in registry.json" if registry_entry else "swesim register <id> after a valid validation"})
    return stages


def scenario_entry(sdir: Path, registry: dict, candidate_refs: set[str]) -> dict:
    sid = sdir.name
    spec = read_json(sdir / "scenario.json") or read_json(sdir / "scenario.draft.json") or {}
    if not isinstance(spec, dict):
        spec = {}
    judge = read_json(sdir / "judge.json") or (spec.get("difficulty") or {}).get("judged") or {}
    approval = read_json(sdir / "approval.json")
    compiled = read_json(sdir / "compile.json")
    validation = read_json(sdir / "validation.json")
    meta = read_json(sdir / "draft.meta.json")
    entry = registry.get(sid)
    ledger = spec.get("ledger") or []
    by_discovery: dict[str, int] = {}
    for f in ledger:
        if isinstance(f, dict):
            by_discovery[str(f.get("discovery"))] = by_discovery.get(str(f.get("discovery")), 0) + 1
    controls = spec.get("controls") or {}
    label, url = source_label(spec.get("source"), spec.get("source_ref"))
    files = [name for name in SCENARIO_FILES if (sdir / name).exists()]
    stamps = [t for t in (mtime(sdir / n) for n in files) if t is not None]
    spec_hash = approval.get("spec_hash") or compiled.get("spec_hash") or validation.get("spec_hash") or meta.get("spec_hash") \
        or (entry or {}).get("spec_hash")
    stages = scenario_stages(sdir, entry, candidate_refs, spec, judge if isinstance(judge, dict) else {}, approval, compiled, validation)
    return {
        "id": sid, "version": spec.get("version") or (entry or {}).get("version"), "frozen": (sdir / "scenario.json").exists(),
        "source": spec.get("source"), "source_ref": spec.get("source_ref"), "source_label": label, "source_url": url,
        "repo_url": (spec.get("environment") or {}).get("repo_url"), "spec_hash": spec_hash,
        "created_at": spec.get("created_at") or meta.get("created_at"),
        "judge": {"keep": judge.get("keep"), "verified_rubric_score": judge.get("verified_rubric_score"),
                  "complexity": judge.get("complexity"), "test_behavioralness": judge.get("test_behavioralness"),
                  "self_contained": judge.get("self_contained"), "alternatives_accepted": judge.get("alternatives_accepted"),
                  "model_id": judge.get("model_id")} if isinstance(judge, dict) and judge else None,
        "facts": len(ledger), "facts_by_discovery": by_discovery, "requirements": len(spec.get("requirements") or []),
        "groups": len(spec.get("groups") or []), "checks": len(spec.get("checks") or []),
        "controls": {"oracle": controls.get("oracle"), "plausible_wrong": len(controls.get("plausible_wrong") or []),
                     "alternative_correct": len(controls.get("alternative_correct") or []), "tamper": len(controls.get("tamper") or [])},
        "briefs": [b.get("profile") for b in spec.get("briefs") or [] if isinstance(b, dict)],
        "stages": stages, "validation_status": validation.get("status"), "validation_failures": validation.get("failures") or [],
        "image_digest": compiled.get("image_digest"), "compiled_hash": compiled.get("compiled_hash"),
        "task_dirs": compiled.get("task_dirs") or {}, "registered": entry is not None,
        "approval": {"approved_by": approval.get("approved_by"), "review_needed": bool(approval.get("review_needed")),
                     "approved_at": approval.get("approved_at"), "spec_hash": approval.get("spec_hash")} if approval else None,
        "awaiting_approval": bool((sdir / "scenario.draft.json").exists() and not approval
                                  and read_json(sdir / "review.json").get("decision") != "reject"),
        "runs": {k: len((entry or {}).get(f"{k}_runs") or []) for k in ("calibration", "comparison", "demonstration", "unresolved")},
        "scores": scores_summary(sdir), "files": files, "updated_ts": max(stamps) if stamps else None,
    }


def scenario_board(root: Path, registry_path: Path) -> list[dict]:
    registry = (read_json(registry_path).get("scenarios") or {}) if registry_path.exists() else {}
    if not isinstance(registry, dict):
        registry = {}
    refs = _candidate_refs(root)
    out = []
    sdirs = root / "scenarios"
    seen = set()
    if sdirs.is_dir():
        for sdir in sorted(p for p in sdirs.iterdir() if p.is_dir() and ID_RE.fullmatch(p.name)):
            seen.add(sdir.name)
            out.append(scenario_entry(sdir, registry, refs))
    for sid in sorted(set(registry) - seen):  # registered but no scenario dir in this workspace copy
        if ID_RE.fullmatch(sid):
            out.append(scenario_entry(sdirs / sid, registry, refs))
    return out


def overview(root: Path, registry_path: Path, live: bool = False, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    scenarios = scenario_board(root, registry_path)
    trials = trial_board(root, now, live)
    summary = {"scenarios": len(scenarios), "valid": sum(1 for s in scenarios if s["validation_status"] == "valid"),
               "trials": len(trials), "running": sum(1 for t in trials if t["running"]),
               "finished": sum(1 for t in trials if not t["unresolved"]),
               "unresolved": sum(1 for t in trials if t["unresolved"] and not t["running"]),
               "succeeded": sum(1 for t in trials if t["success"] == 1)}
    return {"generated_at": iso(now), "now": now, "summary": summary, "scenarios": scenarios, "trials": trials}


# ----------------------------------------------------------------------------- scenario detail

def highlight_spans(text: str, excerpts: list[str]) -> list[tuple[str, bool]]:
    """Split ``text`` into (segment, removed) pairs where removed segments are the withheld excerpts (case-insensitive,
    longest first, non-overlapping). Renderers escape each segment separately."""
    spans: list[tuple[int, int]] = []
    lower = text.lower()
    for ex in sorted({e for e in excerpts if e and e.strip()}, key=len, reverse=True):
        start = 0
        needle = ex.lower()
        while True:
            i = lower.find(needle, start)
            if i < 0:
                break
            j = i + len(needle)
            if not any(a < j and i < b for a, b in spans):
                spans.append((i, j))
            start = j
    spans.sort()
    out: list[tuple[str, bool]] = []
    pos = 0
    for a, b in spans:
        if a > pos:
            out.append((text[pos:a], False))
        out.append((text[a:b], True))
        pos = b
    if pos < len(text):
        out.append((text[pos:], False))
    return out or [(text, False)]


def scenario_detail(root: Path, registry_path: Path, sid: str, evaluator: bool = False) -> dict | None:
    """Everything the scenario page needs. ``evaluator=False`` drops author-only facts entirely (they never reach the HTML)."""
    if not ID_RE.fullmatch(sid):
        return None
    sdir = root / "scenarios" / sid
    registry = (read_json(registry_path).get("scenarios") or {}) if registry_path.exists() else {}
    entry = registry.get(sid) if isinstance(registry, dict) else None
    if not sdir.is_dir() and not entry:
        return None
    board = scenario_entry(sdir, registry if isinstance(registry, dict) else {}, _candidate_refs(root))
    spec = read_json(sdir / "scenario.json") or read_json(sdir / "scenario.draft.json") or {}
    if not isinstance(spec, dict):
        spec = {}
    briefs = {b.get("profile"): b for b in spec.get("briefs") or [] if isinstance(b, dict)}
    ledger = [f for f in spec.get("ledger") or [] if isinstance(f, dict)]
    by_id = {f.get("id"): f for f in ledger}
    ops = [o for o in spec.get("operations") or [] if isinstance(o, dict)]
    removed = [by_id[o["target"]].get("source_excerpt") for o in ops
               if o.get("type") == "withholding" and o.get("target") in by_id and by_id[o["target"]].get("source_excerpt")]
    natural = (briefs.get("natural") or {}).get("text") or ""
    withheld = (briefs.get("withheld") or {}).get("text")
    hidden_author_only = sum(1 for f in ledger if f.get("discovery") == "author-only")
    visible = [f for f in ledger if evaluator or f.get("discovery") != "author-only"]
    grouped: dict[str, list[dict]] = {}
    for f in visible:
        grouped.setdefault(str(f.get("discovery")), []).append(f)
    validation = read_json(sdir / "validation.json")
    vcontrols = validation.get("controls") or {}
    failures = validation.get("failures") or []
    controls_spec = spec.get("controls") or {}

    def control_row(name: str, cls: str, item: dict | None) -> dict:
        res = vcontrols.get(name) or {}
        failed = [f for f in failures if f.startswith(f"{name}:") or f":{name}:" in f]
        return {"name": name, "cls": cls, "asset": (item or {}).get("asset") or (controls_spec.get("oracle") if name == "oracle" else None),
                "rationale": (item or {}).get("rationale"),
                "expected": {k: v for k, v in (item or {}).items() if k.startswith("expected_")} if item else
                ({"reward": 0} if name == "nop" else {"success": 1, "reward": 1} if name == "oracle" else {}),
                "reward": res.get("reward"), "success": res.get("success"), "outcome": res.get("outcome") or
                (OUTCOME_NAMES.get(res.get("outcome_code")) if isinstance(res.get("outcome_code"), int) else None),
                "ran": bool(res), "failed": failed}

    controls = [control_row("nop", "no-op", None), control_row("oracle", "oracle", None)]
    for family, cls in (("plausible_wrong", "plausible-wrong"), ("alternative_correct", "alternative-correct"), ("tamper", "tamper")):
        for item in controls_spec.get(family) or []:
            if isinstance(item, dict) and item.get("name"):
                controls.append(control_row(item["name"], item.get("cls") or cls, item))
    return {
        "board": board, "spec": spec, "exists": sdir.is_dir(), "frozen": (sdir / "scenario.json").exists(),
        "natural_segments": highlight_spans(natural, removed), "withheld": withheld, "removed_excerpts": removed,
        "operations": ops, "ledger_groups": grouped, "hidden_author_only": 0 if evaluator else hidden_author_only,
        "evaluator": evaluator, "requirements": spec.get("requirements") or [], "groups": spec.get("groups") or [],
        "checks": spec.get("checks") or [], "persona": spec.get("persona") or {}, "environment": spec.get("environment") or {},
        "contract": spec.get("contract") or {}, "controls": controls, "validation": validation, "failures": failures,
        "probe": validation.get("probe") or {}, "trial_dirs": validation.get("trial_dirs") or {},
        "approval": read_json(sdir / "approval.json"), "compile": read_json(sdir / "compile.json"),
        "judge": read_json(sdir / "judge.json") or (spec.get("difficulty") or {}).get("judged") or {},
        "difficulty": spec.get("difficulty") or {}, "registry_entry": entry,
    }
