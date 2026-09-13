"""Artifact readers for one trial or one scenario, and the live-transcript client.

Contract
- Lists, filters, funnels and scores come from the ``IndexReader`` (``swesim.dashboard.access``); this module never
  globs the workspace for lists. It reads the artifacts one page shows: the files of one trial (record, transcript,
  trajectory, report, reward, instruction) or one scenario (spec, validation, approval, compile, judge, difficulty,
  audit), through the ``Layout`` paths.
- Every reader is defensive: a missing or malformed file is an empty state, never a 500.
- Recorded paths (``record.json``'s ``trial_dir`` / ``task_dir``) are absolute on the machine that produced them (the VM
  writes ``/opt/swesim/workspace/...``); ``local_path`` re-roots them onto this workspace by the rule of
  ``scripts/verify.sh local()`` (the part after the first ``/workspace/``), and every artifact lookup goes through it.
- The per-trial task copy (``trials/<tid>/task/``) carries the session token and is not mirrored to the bucket; when it
  is absent the episode page shows the compiled task's ``instruction.md`` (``Layout.task_dir``), labelled as such.
- ``fetch_live_transcript`` contacts the stakeholder service for a running trial with ``Settings.stakeholder_url`` and the
  worker secret read at the point of use (``swesim.settings.secret``); errors degrade to a type name, never a URL or secret.
"""
from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from swesim.settings import MissingSecret, Settings, secret
from swesim.stages.score import OUTCOME_NAMES, normalize_record, outcome_code, unresolved_attempt
from swesim.storage.layout import LayoutError

TRIAL_RE = re.compile(r"^[a-f0-9]{24}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
LIVE_TIMEOUT = 10.0
LIVE_CACHE_TTL = 6.0
REFUSAL_PREFIX = "I have to step away now"
_live_cache: dict[str, tuple[float, dict, str | None]] = {}
# Files a reader may open from the episode page. Fixed names only; nothing else under the trial dir is served.
TRIAL_FILES: dict[str, tuple[str, str]] = {
    "record.json": ("trial", "record.json"), "reward.json": ("trial", "reward.json"), "report.json": ("trial", "report.json"),
    "transcript.json": ("trial", "transcript.json"), "run_config.json": ("trial", "run_config.json"), "spec.json": ("trial", "spec.json"),
    "status.json": ("trial", "status.json"), "label.json": ("trial", "label.json"), "request.json": ("trial", "request.json"),
    "runner-error.json": ("trial", "runner-error.json"), "launch.json": ("trial", "launch.json"), "costs.json": ("trial", "costs.json"),
    "audit.json": ("trial", "audit.json"), "launcher.log": ("trial", "launcher.log"),
    "harbor-result.json": ("harbor", "result.json"), "trajectory.json": ("harbor", "agent/trajectory.json"),
    "verifier-reward.json": ("harbor", "verifier/reward.json"), "verifier-report.json": ("harbor", "verifier/report.json"),
    "submission.diff": ("harbor", "verifier/submission.diff"), "patch.diff": ("harbor", "verifier/patch.diff"),
}
# The grader writes the submission diff (taken against the untouched base before the hidden-test overlay) as
# verifier/submission.diff; verifier/patch.diff is the pre-2026-09-13 name kept for the recorded trials that have it.
PATCH_FILES: tuple[str, ...] = ("submission.diff", "patch.diff")
# Evaluator-only artifacts (Codex B-P2-6): raw record/transcript/spec carry facts, redactions and hidden test ids; the
# audit carries labels; scenario.json/draft carry author-only facts and hidden assets; judge/draft metadata is internal.
EVALUATOR_TRIAL_FILES = frozenset(TRIAL_FILES) - frozenset({"reward.json", "report.json", "status.json", "costs.json", "label.json", "submission.diff", "patch.diff"})
SCENARIO_FILES = ("scenario.json", "scenario.draft.json", "draft.meta.json", "judge.json", "approval.json", "compile.json", "validation.json",
                  "difficulty.json", "audit.json", "scores.json")
EVALUATOR_SCENARIO_FILES = frozenset({"scenario.json", "scenario.draft.json", "draft.meta.json", "judge.json", "audit.json"})


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
            dt = datetime.fromisoformat(s)
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


# ----------------------------------------------------------------------------- live transcript

def fetch_live_transcript(tid: str, settings: Settings, timeout: float | None = None) -> tuple[dict, str | None]:
    """``GET {stakeholder_url}/transcript/{tid}`` with the worker secret. Returns (transcript, error-type-or-None)."""
    base = (settings.stakeholder_url or "").rstrip("/")
    if not base:
        return {}, "SWESIM_STAKEHOLDER_URL not configured"
    try:
        token = secret("SWESIM_WORKER_SECRET", settings)
    except MissingSecret:
        return {}, "SWESIM_WORKER_SECRET not configured"
    try:
        r = httpx.get(f"{base}/transcript/{tid}", headers={"X-Worker-Secret": token}, timeout=LIVE_TIMEOUT if timeout is None else timeout)
        r.raise_for_status()
        data = r.json()
        return (data if isinstance(data, dict) else {}), None
    except Exception as e:  # noqa: BLE001 - any failure degrades to "no live transcript", reported by type only
        return {}, type(e).__name__


def live_transcript_cached(tid: str, settings: Settings, now: float | None = None) -> tuple[dict, str | None]:
    now = time.time() if now is None else now
    hit = _live_cache.get(tid)
    if hit and now - hit[0] < LIVE_CACHE_TTL:
        return hit[1], hit[2]
    transcript, error = fetch_live_transcript(tid, settings)
    _live_cache[tid] = (now, transcript, error)
    return transcript, error


def patience_of(transcript: dict, run_config: dict | None = None) -> dict:
    """{start, end, tier, questions, confirmations, reviews} from a transcript; start falls back to the run config."""
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
            "confirmations": sum(1 for e in events if e.get("role") == "user" and e.get("exchange") == "confirm"),
            "reviews": sum(1 for e in events if e.get("role") == "user" and e.get("exchange") == "review")}


# ----------------------------------------------------------------------------- one trial

def _contained(layout, path: Path) -> bool:
    """Artifact references in stored records may be stale or hostile; accept only regular workspace paths."""
    root = layout.root.resolve()
    if not path.resolve().is_relative_to(root):
        return False
    current = path
    while current != root and current != current.parent:
        if current.is_symlink():
            return False
        current = current.parent
    return True

WORKSPACE_MARKER = "/workspace/"


def local_path(layout, recorded) -> Path | None:
    """A recorded artifact path as it exists in this workspace (the rule of ``scripts/verify.sh local()``): the recorded path
    itself when it lies inside the workspace and exists; otherwise the part after the first ``/workspace/`` joined onto
    ``layout.root``. None when the value is empty, carries no ``/workspace/`` segment, or would escape the workspace."""
    if not recorded:
        return None
    p = Path(str(recorded))
    if p.is_absolute() and _contained(layout, p) and p.exists():
        return p
    s = p.as_posix()
    i = s.find(WORKSPACE_MARKER)
    if i < 0:
        return None
    rel = s[i + len(WORKSPACE_MARKER):]
    if not rel:
        return None
    q = layout.root / rel
    return q if _contained(layout, q) else None


def compiled_instruction(layout, scenario_id, condition) -> Path | None:
    """The compiled task's ``instruction.md`` (mirrored to the bucket; the per-trial copy is not): identical to what the agent saw minus the session token."""
    if not scenario_id or not condition:
        return None
    try:
        p = layout.task_dir(str(scenario_id), str(condition)) / "instruction.md"
    except (LayoutError, ValueError):
        return None
    return p if _contained(layout, p) and p.is_file() else None


def _row(ts, seq, actor, kind, text, meta) -> dict:
    return {"ts": ts, "ts_iso": iso(ts), "no_timestamp": ts is None, "seq": seq, "actor": actor, "kind": kind, "text": text, "meta": meta}


def merge_timeline(traj: dict, transcript: dict, report: dict | None = None, reward: dict | None = None, status: dict | None = None) -> list[dict]:
    """Interleave ATIF agent steps, stakeholder transcript messages (each stakeholder ``tool_calls`` entry its own row) and
    one verifier outcome row, sorted by time; rows without a timestamp sort last and carry ``no_timestamp: True``."""
    rows: list[dict] = []
    for i, s in enumerate((traj or {}).get("steps") or []):
        if not isinstance(s, dict):
            continue
        source = s.get("source", "agent")
        actor = "agent" if source == "agent" else "harness"
        kind = "agent-step" if source == "agent" else ("prompt" if source == "user" else "system")
        rows.append(_row(to_ts(s.get("timestamp")), i, actor, kind, str(s.get("message") or ""), s))
    last_exchange = None
    for i, e in enumerate((transcript or {}).get("events") or []):
        if not isinstance(e, dict) or e.get("kind") != "message":
            continue
        ts = to_ts(e.get("ts"))
        if e.get("role") == "user":
            actor, last_exchange = "agent", e.get("exchange") or last_exchange or "ask"
            kind = last_exchange
        else:
            actor, kind = "stakeholder", e.get("exchange") or last_exchange or "reply"
        rows.append(_row(ts, i, actor, kind, str(e.get("content") or ""), e))
        for tool in e.get("tool_calls") or []:
            rows.append(_row(ts, i, "stakeholder", "tool_call", json.dumps(tool), tool))
    report, reward, status = report or {}, reward or {}, status or {}
    if report.get("outcome") or reward.get("outcome_code") is not None:
        name = report.get("outcome") or OUTCOME_NAMES[outcome_code({"reward": reward})]
        text = name if not reward else f"{name}: reward={reward.get('reward')} success={reward.get('success')}"
        ts = next((to_ts(status.get(k)) for k in ("finished_at", "completed_at", "ended_at", "updated_at") if status.get(k)), None)
        ts = ts if ts is not None else to_ts(report.get("finished_at"))
        rows.append(_row(ts, 1 << 30, "verifier", "outcome", text, {"report": report, "reward": reward, "status": status}))
    return sorted(rows, key=lambda r: (r["ts"] is None, r["ts"] or 0.0, r["seq"]))


def _trajectory(layout, tid: str, record: dict) -> dict:
    trial_dir = local_path(layout, record.get("trial_dir"))
    if trial_dir is not None:
        traj = read_json(trial_dir / "agent" / "trajectory.json")
        if traj:
            return traj
    found = sorted((layout.jobs_dir("runs") / tid).glob("*/agent/trajectory.json"))
    return read_json(found[0]) if len(found) == 1 else {}


def load_trial(layout, tid: str, settings: Settings) -> dict | None:
    """The artifact bundle of one trial (None when the directory does not exist)."""
    if not TRIAL_RE.fullmatch(tid):
        return None
    d = layout.trial_dir(tid)
    if not _contained(layout, d) or not d.is_dir():
        return None
    status = read_json(d / "status.json")
    raw = read_json(d / "record.json") if (d / "record.json").exists() else {}
    has_record = bool(raw)
    record = normalize_record(raw, d) if has_record else {}
    unresolved = None if has_record else unresolved_attempt(d)
    request = read_json(d / "request.json")
    rc = record.get("run_config") or request.get("run_config") or (unresolved or {}).get("run_config") or {}
    live = status.get("status") == "running" and not has_record
    live_error = None
    if live:
        transcript, live_error = live_transcript_cached(tid, settings)
    else:
        transcript = record.get("transcript") or read_json(d / "transcript.json")
    report = record.get("report") or read_json(d / "report.json")
    reward = record.get("reward") or read_json(d / "reward.json")
    traj = _trajectory(layout, tid, record)
    u = unresolved or {}
    task_dir = local_path(layout, record.get("task_dir"))
    ins, ins_source = next(((p, "trial") for p in (d / "task" / "instruction.md", task_dir / "instruction.md" if task_dir is not None else None)
                            if p is not None and _contained(layout, p) and p.is_file()), (None, None))
    if ins is None:  # the per-trial task copy is not mirrored (session token): the compiled task's instruction is the same text minus the token
        ins = compiled_instruction(layout, record.get("scenario_id") or request.get("scenario_id") or u.get("scenario_id"),
                                   record.get("condition") or request.get("condition") or u.get("condition"))
        ins_source = "compiled-task" if ins is not None else None
    meta = {
        "scenario_id": record.get("scenario_id") or request.get("scenario_id") or u.get("scenario_id"),
        "condition": record.get("condition") or request.get("condition") or u.get("condition"),
        "brief_profile": record.get("brief_profile") or rc.get("brief_profile") or u.get("brief_profile"),
        "model": record.get("model") or request.get("model") or rc.get("model") or u.get("model"),
        "purpose": record.get("purpose") or rc.get("purpose") or u.get("purpose"),
        "attempt_index": record.get("attempt_index", request.get("attempt_index")),
        "spec_hash": record.get("spec_hash") or request.get("spec_hash"),
        "run_config_hash": record.get("run_config_hash") or request.get("run_config_hash") or u.get("run_config_hash"),
        "compiled_hash": record.get("compiled_hash") or rc.get("compiled_hash") or u.get("compiled_hash"),
        "image_digest": (report or {}).get("image_digest"), "trial_dir": record.get("trial_dir"),
        "stakeholder_model": (rc.get("stakeholder") or {}).get("model_id") or request.get("stakeholder_model"),
        "started_at": status.get("started_at") or record.get("started_at"), "finished_at": status.get("finished_at") or record.get("finished_at"),
    }
    patch, patch_file = None, None
    trial_dir = local_path(layout, record.get("trial_dir"))
    if trial_dir is not None:
        for name in PATCH_FILES:  # the grader's submission.diff first; the legacy patch.diff only when no submission.diff exists
            p = trial_dir / TRIAL_FILES[name][1]
            if _contained(layout, p) and p.is_file():
                try:
                    patch, patch_file = p.read_text(errors="replace"), TRIAL_FILES[name][1]
                except OSError:
                    continue
                break
    return {"trial_id": tid, "dir": d, "status": {**{k: v for k, v in meta.items() if k in ("started_at", "finished_at") and v}, **status},
            "record": record, "request": request, "live": live, "unresolved": unresolved, "live_transcript_error": live_error,
            "transcript": transcript or {}, "report": report or {}, "reward": reward or {}, "trajectory": traj,
            "instruction": ins.read_text(errors="replace") if ins else None, "instruction_source": ins_source,
            "label": read_json(d / "label.json", default=None) or None, "meta": meta,
            "patience": patience_of(transcript or {}, rc), "files": list(TRIAL_FILES), "patch": patch, "patch_file": patch_file,
            "costs": read_json(d / "costs.json", default=[]), "audit": read_json(d / "audit.json")}


def resolve_trial_file(layout, t: dict, name: str) -> Path | None:
    base, rel = TRIAL_FILES[name]
    if base == "trial":
        path = t["dir"] / rel
        return path if _contained(layout, path) else None
    trial_dir = local_path(layout, t["meta"].get("trial_dir"))
    if trial_dir is not None:
        path = trial_dir / rel
        return path if _contained(layout, path) else None
    found = sorted((layout.jobs_dir("runs") / t["trial_id"]).glob(f"*/{rel}"))
    return found[0] if len(found) == 1 and _contained(layout, found[0]) else None


# ----------------------------------------------------------------------------- one scenario

def highlight_spans(text: str, excerpts: list[str]) -> list[tuple[str, bool]]:
    """Split ``text`` into (segment, removed) pairs where removed segments are the withheld excerpts (case-insensitive,
    longest first, non-overlapping). Renderers escape each segment separately."""
    spans: list[tuple[int, int]] = []
    lower = text.lower()
    for ex in sorted({e for e in excerpts if e and e.strip()}, key=len, reverse=True):
        start, needle = 0, ex.lower()
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


def scenario_detail(layout, sid: str, evaluator: bool = False, version: int | None = None) -> dict | None:
    """Everything the scenario/review page needs from one scenario directory (or an archived version).
    ``evaluator=False`` drops author-only facts, hidden test assets, judge/draft metadata and the withheld sentences (the
    natural brief shows a masked placeholder where a span was removed; facts keep their text but not the excerpt)."""
    if not ID_RE.fullmatch(sid):
        return None
    sdir = layout.scenario_dir(sid) if version is None else layout.scenario_archive_dir(sid, version)
    if not _contained(layout, sdir):
        return None
    if not sdir.is_dir():
        return None
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
    if not evaluator:  # the withheld sentences themselves are evaluator-only (Codex B-P2-6): facts keep their text, not the excerpt
        visible = [{k: v for k, v in f.items() if k != "source_excerpt"} for f in visible]
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
                "reward": res.get("reward"), "success": res.get("success"),
                "outcome": res.get("outcome") or (OUTCOME_NAMES.get(res.get("outcome_code")) if isinstance(res.get("outcome_code"), int) else None),
                "ran": bool(res), "failed": failed}

    controls = [control_row("nop", "no-op", None), control_row("oracle", "oracle", None)]
    for family, cls in (("plausible_wrong", "plausible-wrong"), ("alternative_correct", "alternative-correct"), ("tamper", "tamper")):
        for item in controls_spec.get(family) or []:
            if isinstance(item, dict) and item.get("name"):
                controls.append(control_row(item["name"], item.get("cls") or cls, item))
    approval = read_json(sdir / "approval.json")
    compiled = read_json(sdir / "compile.json")
    assets = spec.get("assets") if isinstance(spec.get("assets"), dict) else {}
    contract = spec.get("contract") or {}
    public_assets = {k: v for k, v in assets.items() if evaluator or not _hidden_asset(k, v)}
    files = [n for n in SCENARIO_FILES if (sdir / n).exists() and (evaluator or n not in EVALUATOR_SCENARIO_FILES)]
    out = {
        "scenario_id": sid, "version": spec.get("version") or approval.get("version"), "frozen": (sdir / "scenario.json").exists(),
        "spec_hash": approval.get("spec_hash") or compiled.get("spec_hash") or validation.get("spec_hash"),
        "source": spec.get("source"), "source_ref": spec.get("source_ref"), "created_at": spec.get("created_at"),
        "natural_segments": [(seg if evaluator or not is_removed else f"[withheld span · {len(seg)} chars]", is_removed) for seg, is_removed in highlight_spans(natural, removed)],
        "withheld": withheld, "removed_excerpts": removed if evaluator else [None] * len(removed), "operations": ops,
        "ledger_groups": grouped, "hidden_author_only": 0 if evaluator else hidden_author_only, "evaluator": evaluator,
        "requirements": spec.get("requirements") or [], "groups": spec.get("groups") or [], "checks": spec.get("checks") or [],
        "persona": spec.get("persona") or {}, "environment": spec.get("environment") or {},
        "contract": {"fail_to_pass": len(contract.get("fail_to_pass") or []), "pass_to_pass": len(contract.get("pass_to_pass") or []),
                     "protected_paths": len(contract.get("protected_paths") or []),
                     **({"fail_to_pass_ids": contract.get("fail_to_pass") or [], "pass_to_pass_ids": contract.get("pass_to_pass") or []} if evaluator else {})},
        "controls": controls, "validation": validation, "failures": failures, "probe": validation.get("probe") or {},
        "approval": approval, "compile": compiled, "assets": public_assets, "hidden_assets": 0 if evaluator else len(assets) - len(public_assets),
        "difficulty": read_json(sdir / "difficulty.json"), "audit": read_json(sdir / "audit.json") if evaluator else {},
        "judge": (read_json(sdir / "judge.json") or (spec.get("difficulty") or {}).get("judged") or {}) if evaluator else {},
        "draft_meta": read_json(sdir / "draft.meta.json") if evaluator else {}, "review": read_json(sdir / "review.json"), "files": files, "dir": sdir,
    }
    return out


def _hidden_asset(name: str, item) -> bool:
    """Hidden test assets and control diffs are evaluator-only; the base Dockerfile is not."""
    n = name.lower()
    return not (n.startswith("dockerfile") or n.endswith((".dockerfile", ".toml")))
