"""swesim dashboard: runs table and episode viewer (FastAPI, server-rendered HTML, no build step).

Temporal shows workflow state; this UI shows the conversation and the outcomes. It reads workspace files
only: ``registry.json``, ``workspace/scenarios/*/scores.json``, ``workspace/trials/<id>/{status,record,
transcript,label}.json`` and the Harbor trial directory (``agent/trajectory.json``, ``verifier/*``). The
stakeholder service is contacted for one thing: the live transcript of a trial whose status is ``running``.

Honesty rules baked in: rows without timestamps are shown last and flagged, never ordered as facts; the
stakeholder's fact annotations and tool audit are marked evaluator-only; null metrics (n < k) read "n/a",
never a silently reduced k; ``reviewed_equals_submitted`` null is explained as the diff-only pilot.
This is a reviewer tool; it is reached over an SSH forward and must never be exposed to agents.
"""
from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from html import escape
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

from swesim.paths import atomic_json
from swesim.stages.score import OUTCOME_NAMES, count_exchanges, normalize_record, outcome_code

app = FastAPI(title="swesim dashboard", docs_url=None, redoc_url=None)
ROOT = Path(os.environ.get("SWESIM_WORKSPACE", "workspace")).resolve()
REGISTRY = Path(os.environ.get("SWESIM_REGISTRY", "registry.json")).resolve()
LABELS = ("unresolved", "no-question", "disclosed-not-used", "simulator-omission", "derived-without-asking")
TRIAL_RE = re.compile(r"[a-f0-9]{24}")
LIVE_TIMEOUT = 10.0
POLL_MS = 5000
# Files a reviewer may open from the episode page. Fixed names only; nothing else under the trial dir is served.
TRIAL_FILES = {
    "record.json": ("trial", "record.json"), "reward.json": ("trial", "reward.json"), "report.json": ("trial", "report.json"),
    "transcript.json": ("trial", "transcript.json"), "run_config.json": ("trial", "run_config.json"),
    "spec.json": ("trial", "spec.json"), "status.json": ("trial", "status.json"), "label.json": ("trial", "label.json"),
    "request.json": ("trial", "request.json"), "instruction.md": ("trial", "task/instruction.md"),
    "harbor-result.json": ("harbor", "result.json"), "trajectory.json": ("harbor", "agent/trajectory.json"),
    "verifier-reward.json": ("harbor", "verifier/reward.json"), "verifier-report.json": ("harbor", "verifier/report.json"),
}


# ----------------------------------------------------------------------------- files and time

def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {} if default is None else default


def trial_path(tid: str) -> Path:
    if not TRIAL_RE.fullmatch(tid):
        raise HTTPException(404)
    return ROOT / "trials" / tid


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


# ----------------------------------------------------------------------------- timeline

def _row(ts, seq, actor, kind, text, meta) -> dict:
    return {"ts": ts, "ts_iso": iso(ts), "no_timestamp": ts is None, "seq": seq, "actor": actor, "kind": kind,
            "text": text, "meta": meta}


def merge_timeline(traj: dict, transcript: dict, report: dict | None = None, reward: dict | None = None,
                   status: dict | None = None) -> list[dict]:
    """Interleave ATIF agent steps (``timestamp``), stakeholder transcript messages (``ts``; each stakeholder
    ``tool_calls`` entry is its own row) and, when a report exists, one verifier outcome row. Sorted by time;
    rows without a timestamp sort last and carry ``no_timestamp: True``."""
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


# ----------------------------------------------------------------------------- trial loading

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


def _trajectory(tid: str, record: dict) -> dict:
    if record.get("trial_dir"):
        traj = read_json(Path(record["trial_dir"]) / "agent" / "trajectory.json")
        if traj:
            return traj
    found = sorted((ROOT / "jobs" / "runs" / tid).glob("*/agent/trajectory.json"))
    return read_json(found[0]) if len(found) == 1 else {}


def load_trial(tid: str) -> dict:
    d = trial_path(tid)
    if not d.is_dir():
        raise HTTPException(404)
    status = read_json(d / "status.json")
    has_record = (d / "record.json").exists()
    record = normalize_record(read_json(d / "record.json"), d) if has_record else {}
    request = read_json(d / "request.json")
    rc = record.get("run_config") or request.get("run_config") or {}
    live = status.get("status") == "running" and not has_record
    live_error = None
    if live:
        transcript, live_error = fetch_live_transcript(tid)
    else:
        transcript = record.get("transcript") or read_json(d / "transcript.json")
    report = record.get("report") or read_json(d / "report.json")
    reward = record.get("reward") or read_json(d / "reward.json")
    traj = _trajectory(tid, record)
    ins = next((p for p in (d / "task" / "instruction.md",
                            Path(record["task_dir"]) / "instruction.md" if record.get("task_dir") else None)
                if p is not None and p.exists()), None)
    events = [e for e in (transcript or {}).get("events") or [] if isinstance(e, dict) and e.get("kind") == "message"]
    last_reply = next((e for e in reversed(events) if e.get("role") == "assistant"), None)
    p_start = (transcript or {}).get("patience_start")
    if p_start is None:
        p_start = ((rc.get("stakeholder") or {}).get("patience_profile") or {}).get("start")
    p_end = (transcript or {}).get("patience_end")
    if p_end is None and last_reply is not None:
        p_end = last_reply.get("patience_remaining")
    meta = {
        "scenario_id": record.get("scenario_id") or request.get("scenario_id"),
        "condition": record.get("condition") or request.get("condition"),
        "brief_profile": record.get("brief_profile") or rc.get("brief_profile"),
        "model": record.get("model") or request.get("model") or rc.get("model"),
        "purpose": record.get("purpose") or rc.get("purpose"),
        "attempt_index": record.get("attempt_index", request.get("attempt_index")),
        "spec_hash": record.get("spec_hash") or request.get("spec_hash"),
        "run_config_hash": record.get("run_config_hash") or request.get("run_config_hash"),
        "compiled_hash": record.get("compiled_hash") or rc.get("compiled_hash"),
        "image_digest": (report or {}).get("image_digest"),
        "trial_dir": record.get("trial_dir"),
    }
    # ISO-8601 timestamps: status.json first, record.json as fallback (both written by the run stage when present).
    timestamps = {k: status.get(k) or record.get(k) for k in ("started_at", "finished_at")}
    meta.update(timestamps)
    status_view = {**{k: v for k, v in timestamps.items() if v}, **status}
    return {"trial_id": tid, "dir": d, "status": status_view, "record": record, "request": request, "live": live,
            "live_transcript_error": live_error, "transcript": transcript or {}, "report": report or {},
            "reward": reward or {}, "trajectory": traj, "instruction": ins.read_text() if ins else None,
            "label": read_json(d / "label.json", default=None) or None, "meta": meta,
            "patience": {"start": p_start, "end": p_end, "tier": (last_reply or {}).get("tier"),
                         "questions": sum(1 for e in events if e.get("role") == "user" and e.get("exchange") == "ask"),
                         "reviews": sum(1 for e in events if e.get("role") == "user" and e.get("exchange") == "review")}}


def trial_summary(d: Path) -> dict:
    """One runs-table row from status.json plus record.json (or request.json while running)."""
    status = read_json(d / "status.json")
    has_record = (d / "record.json").exists()
    rec = normalize_record(read_json(d / "record.json"), d) if has_record else {}
    request = read_json(d / "request.json")
    rc = rec.get("run_config") or request.get("run_config") or {}
    label = read_json(d / "label.json", default=None) or {}
    code = outcome_code(rec) if rec.get("reward") else None
    return {"trial_id": d.name, "status": status.get("status") or ("finished" if has_record else "unknown"),
            "scenario_id": rec.get("scenario_id") or request.get("scenario_id"),
            "condition": rec.get("condition") or request.get("condition"),
            "brief_profile": rec.get("brief_profile") or rc.get("brief_profile"),
            "model": rec.get("model") or request.get("model") or rc.get("model"), "purpose": rec.get("purpose") or rc.get("purpose"),
            "attempt_index": rec.get("attempt_index", request.get("attempt_index")),
            "outcome": OUTCOME_NAMES[code] if code is not None else None,
            "success": (rec.get("reward") or {}).get("success"), "reward": (rec.get("reward") or {}).get("reward"),
            "questions": count_exchanges(rec, "ask") if rec else None, "reviews": count_exchanges(rec, "review") if rec else None,
            "label": label.get("label"), "started_at": status.get("started_at") or rec.get("started_at"),
            "finished_at": status.get("finished_at") or rec.get("finished_at")}


# ----------------------------------------------------------------------------- HTML

CSS = """
:root{--surface:#fcfcfb;--panel:#ffffff;--line:#d9d8d3;--ink:#0b0b0b;--ink-2:#52514e;--ink-3:#8a8984;
--agent:#2a78d6;--stakeholder:#eb6834;--verifier:#1baf7a;--harness:#8a8984;--good:#0ca30c;--critical:#d03b3b;--warning:#fab219;--audit:#fff4e8}
*{box-sizing:border-box}body{margin:0;padding:1rem 1.5rem 3rem;background:var(--surface);color:var(--ink);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
a{color:var(--agent)}h1{font-size:1.4rem;margin:.2rem 0 .8rem}h2{font-size:1.1rem;margin:1.4rem 0 .4rem}h3{font-size:1rem;margin:1rem 0 .3rem}
nav{color:var(--ink-2);margin-bottom:.8rem}
table{border-collapse:collapse;width:100%;background:var(--panel);margin:.3rem 0 .6rem}
th,td{border:1px solid var(--line);padding:.3rem .5rem;text-align:left;vertical-align:top;font-size:13px}
th{background:#f3f2ee;color:var(--ink-2);font-weight:600}td.num{text-align:right;font-variant-numeric:tabular-nums}
code,pre,.mono{font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
pre{white-space:pre-wrap;word-break:break-word;background:var(--panel);border:1px solid var(--line);padding:.6rem;margin:.3rem 0;max-height:26rem;overflow:auto}
dl.meta{display:grid;grid-template-columns:max-content 1fr;gap:.15rem 1rem;background:var(--panel);border:1px solid var(--line);padding:.6rem .8rem;margin:0}
dl.meta dt{color:var(--ink-2);font-weight:600}dl.meta dd{margin:0;word-break:break-all}
.row{background:var(--panel);border:1px solid var(--line);border-left:5px solid var(--harness);margin:.45rem 0;padding:.45rem .7rem;border-radius:0 4px 4px 0}
.row.agent{border-left-color:var(--agent)}.row.stakeholder{border-left-color:var(--stakeholder)}.row.verifier{border-left-color:var(--verifier)}
.hdr{color:var(--ink-2);font-size:12px;margin-bottom:.25rem}.hdr time{font-variant-numeric:tabular-nums}
.txt{white-space:pre-wrap;word-break:break-word}
.badge{display:inline-block;padding:0 .45rem;border-radius:3px;font-size:11px;font-weight:700;color:#fff;background:var(--harness);margin-right:.35rem;letter-spacing:.02em}
.badge.agent{background:var(--agent)}.badge.stakeholder{background:var(--stakeholder)}.badge.verifier{background:var(--verifier)}
.audit{font-size:12px;color:var(--ink-2);background:var(--audit);border:1px dashed var(--stakeholder);padding:.25rem .5rem;margin-top:.35rem}
.flag{color:var(--critical);font-weight:600}.muted{color:var(--ink-3)}
.status{font-weight:700}.status.good{color:var(--good)}.status.critical{color:var(--critical)}.status.warning{color:#7a5300}
.meter{display:inline-block;width:12rem;height:.6rem;background:#eceae4;border:1px solid var(--line);vertical-align:middle;margin:0 .5rem}
.meter i{display:block;height:100%;background:var(--stakeholder)}
details>summary{cursor:pointer;color:var(--ink-2)}
form.label{background:var(--panel);border:1px solid var(--line);padding:.7rem .9rem;max-width:44rem}
form.label label{display:block;margin:.35rem 0 .15rem;color:var(--ink-2)}textarea,select{width:100%;font:inherit}textarea{min-height:4rem}
button{font:inherit;padding:.3rem .8rem;margin-top:.5rem}
#live{color:var(--ink-2);font-size:12px}
"""


def h(x) -> str:
    return escape("" if x is None else str(x), quote=True)


def short(x, n: int = 12) -> str:
    s = "" if x is None else str(x)
    return h(s if len(s) <= n else s[:n] + "…")


def fmt_metric(v, digits: int = 3) -> str:
    if v is None:
        return '<span class="muted" title="insufficient samples: n &lt; k">n/a</span>'
    return h(f"{float(v):.{digits}f}")


def status_class(s: str | None) -> str:
    s = (s or "").lower()
    if s in ("running", "pending", "launched"):
        return "warning"
    if s in ("finished", "completed", "done"):
        return "good"
    if s in ("failed", "launch-failed", "runner-error", "error"):
        return "critical"
    return ""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{h(title)}</title>"
                        f"<meta name='viewport' content='width=device-width,initial-scale=1'><style>{CSS}</style></head>"
                        f"<body><nav><a href='/'>swesim runs</a></nav>{body}</body></html>")


def _outcome_cell(name: str | None, success) -> str:
    if not name:
        return '<span class="muted">—</span>'
    cls = "good" if success == 1 else ("critical" if name.startswith(("valid", "agent")) else "warning")
    return f'<span class="status {cls}">{h(name)}</span>'


def render_runs_page() -> str:
    registry = read_json(REGISTRY).get("scenarios") or {}
    parts = ["<h1>swesim runs</h1>",
             f"<p class='muted'>workspace <code>{h(ROOT)}</code> · registry <code>{h(REGISTRY)}</code></p>"]
    # scenarios
    parts.append("<h2>Registered scenarios</h2>")
    if registry:
        parts.append("<table><tr><th>id</th><th>v</th><th>source</th><th>source_ref</th><th>validation</th><th>image digest</th>"
                     "<th>calibration</th><th>comparison</th><th>demonstration</th><th>spec hash</th></tr>")
        for sid, e in sorted(registry.items()):
            parts.append(f"<tr><td><a href='#scores-{h(sid)}'>{h(e.get('id', sid))}</a></td><td class='num'>{h(e.get('version'))}</td>"
                         f"<td>{h(e.get('source'))}</td><td>{h(e.get('source_ref'))}</td>"
                         f"<td><span class='status {status_class('finished' if e.get('validation_status') == 'valid' else 'failed')}'>{h(e.get('validation_status'))}</span></td>"
                         f"<td class='mono' title='{h(e.get('image_digest'))}'>{short(e.get('image_digest'), 24)}</td>"
                         f"<td class='num'>{len(e.get('calibration_runs') or [])}</td><td class='num'>{len(e.get('comparison_runs') or [])}</td>"
                         f"<td class='num'>{len(e.get('demonstration_runs') or [])}</td><td class='mono'>{short(e.get('spec_hash'))}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p class='muted'>No scenarios registered yet.</p>")
    # scores
    parts.append("<h2>Scores</h2><p class='muted'>Groups are condition · brief profile · frozen run config (model, purpose, build). "
                 "Calibration and comparison runs are never pooled. pass@k and pass^k are null (n/a) when fewer than k valid attempts exist; "
                 "k is never reduced. Denominators count agent-attributable outcomes (valid-correct, valid-incorrect, "
                 "agent-invalid-submission, agent-runtime-failure); infrastructure failures are excluded from n.</p>")
    score_files = sorted((ROOT / "scenarios").glob("*/scores.json")) if (ROOT / "scenarios").is_dir() else []
    if not score_files:
        parts.append("<p class='muted'>No scores.json yet (run <code>swesim score &lt;id&gt;</code>).</p>")
    for sf in score_files:
        sid = sf.parent.name
        groups = read_json(sf, default="unreadable")
        parts.append(f"<h3 id='scores-{h(sid)}'>{h(sid)} <span class='muted mono'>{h(sf)}</span></h3>")
        if not isinstance(groups, dict):
            parts.append("<p class='muted'>unreadable scores.json (not a JSON object of groups); re-run <code>swesim score</code>.</p>")
            continue
        groups = {k: g for k, g in groups.items() if isinstance(g, dict)}
        if not groups:
            parts.append("<p class='muted'>No finished trials.</p>")
            continue
        parts.append("<table><tr><th>condition</th><th>profile</th><th>model</th><th>purpose</th><th>valid / total</th><th>success</th>"
                     "<th>pass@k</th><th>pass^k</th><th>mean reward</th><th>questions</th><th>reviews</th><th>outcomes</th><th>labels</th><th>trials</th></tr>")
        for key, g in sorted(groups.items()):
            k = g.get("requested_k")
            qs, rv = g.get("questions_asked") or [], g.get("reviews") or []
            outcomes = ", ".join(f"{h(n)} {c}" for n, c in sorted((g.get("outcomes") or {}).items()))
            labels = ", ".join(f"{h(n)} {c}" for n, c in sorted((g.get("labels") or {}).items())) or "—"
            trials = " ".join(f"<a class='mono' href='/trial/{h(t)}'>{short(t, 8)}</a>" for t in g.get("trial_ids") or [] if t)
            parts.append(f"<tr><td>{h(g.get('condition'))}</td><td>{h(g.get('brief_profile'))}</td><td>{h(g.get('model'))}</td><td>{h(g.get('purpose'))}</td>"
                         f"<td class='num'>{h(g.get('n_valid'))} / {h(g.get('n_total'))}</td><td class='num'>{h(g.get('success_count'))}</td>"
                         f"<td class='num' title='k={h(k)}'>{fmt_metric(g.get('pass_at_k'))} <span class='muted'>@{h(k)}</span></td>"
                         f"<td class='num' title='k={h(k)}'>{fmt_metric(g.get('pass_pow_k'))} <span class='muted'>^{h(k)}</span></td>"
                         f"<td class='num'>{fmt_metric(g.get('mean_reward'))}</td>"
                         f"<td class='num' title='mean over valid attempts'>{fmt_metric(sum(qs) / len(qs), 2) if qs else '—'}</td>"
                         f"<td class='num' title='mean over valid attempts'>{fmt_metric(sum(rv) / len(rv), 2) if rv else '—'}</td>"
                         f"<td>{outcomes}</td><td>{labels}</td><td>{trials}</td></tr>")
        parts.append("</table>")
    # trials
    parts.append("<h2>Trials</h2>")
    dirs = sorted((ROOT / "trials").glob("*/status.json")) if (ROOT / "trials").is_dir() else []
    rows = [trial_summary(p.parent) for p in dirs if TRIAL_RE.fullmatch(p.parent.name)]
    rows.sort(key=lambda r: (r["started_at"] or "", r["trial_id"]), reverse=True)
    if not rows:
        parts.append("<p class='muted'>No trials yet.</p>")
    else:
        parts.append("<table><tr><th>trial</th><th>scenario</th><th>condition</th><th>profile</th><th>model</th><th>purpose</th><th>#</th>"
                     "<th>status</th><th>outcome</th><th>reward</th><th>questions</th><th>reviews</th><th>label</th><th>started</th></tr>")
        for r in rows:
            parts.append(f"<tr><td><a class='mono' href=\"/trial/{h(r['trial_id'])}\">{h(r['trial_id'])}</a></td><td>{h(r['scenario_id'])}</td>"
                         f"<td>{h(r['condition'])}</td><td>{h(r['brief_profile'])}</td><td>{h(r['model'])}</td><td>{h(r['purpose'])}</td>"
                         f"<td class='num'>{h(r['attempt_index'])}</td><td><span class='status {status_class(r['status'])}'>{h(r['status'])}</span></td>"
                         f"<td>{_outcome_cell(r['outcome'], r['success'])}</td><td class='num'>{h(r['reward']) if r['reward'] is not None else '—'}</td>"
                         f"<td class='num'>{h(r['questions']) if r['questions'] is not None else '—'}</td>"
                         f"<td class='num'>{h(r['reviews']) if r['reviews'] is not None else '—'}</td>"
                         f"<td>{h(r['label']) if r['label'] else '—'}</td><td class='mono'>{h(r['started_at'] or '')}</td></tr>")
        parts.append("</table>")
    return "".join(parts)


def _call_text(call) -> str:
    """Readable form of one ATIF tool call: the shell command when present, else the JSON arguments."""
    if not isinstance(call, dict):
        return str(call)
    args = call.get("arguments")
    if isinstance(args, dict) and isinstance(args.get("command"), str):
        return args["command"]
    return args if isinstance(args, str) else json.dumps(args, indent=1)


def render_row(r: dict) -> str:
    actor, kind, meta = r["actor"], r["kind"], r.get("meta") or {}
    when = (f"<time datetime='{h(r['ts_iso'])}' title='{h(r['ts_iso'])}'>{h(clock(r['ts']))}</time>" if r["ts"] is not None
            else "<span class='flag' title='This row has no timestamp; its position is not chronological evidence.'>no timestamp</span>")
    head = f"<div class='hdr'>{when} <span class='badge {h(actor)}'>{h(actor)}</span><span>{h(kind)}</span></div>"
    if kind == "agent-step":
        body = f"<div class='txt'>{h(r['text'])}</div>"
        calls = meta.get("tool_calls") or []
        obs = (meta.get("observation") or {}).get("results") or []
        if calls or obs:
            inner = "".join(f"<pre>$ {h(_call_text(c))}</pre>" for c in calls)
            inner += "".join(f"<pre>{h(o.get('content') if isinstance(o, dict) else o)}</pre>" for o in obs)
            body += f"<details><summary>{len(calls)} tool call(s), {len(obs)} observation(s)</summary>{inner}</details>"
    elif kind in ("prompt", "system"):
        body = f"<details><summary>{h(kind)} from harness ({len(r['text'])} chars)</summary><pre>{h(r['text'])}</pre></details>"
    elif kind == "tool_call":
        body = f"<code>{h(r['text'])}</code> <span class='muted'>stakeholder tool call; diff-only, unverified</span>"
    elif kind == "outcome":
        body = f"<div class='txt'><strong>{h(r['text'])}</strong></div>"
    else:
        body = f"<div class='txt'>{h(r['text'])}</div>"
        if actor == "stakeholder":
            bits = []
            if meta.get("tier") is not None or meta.get("patience_remaining") is not None:
                bits.append(f"tier {h(meta.get('tier'))}, patience remaining {h(meta.get('patience_remaining'))}")
            if meta.get("disclosed_fact_ids") is not None:
                bits.append("disclosed fact ids: " + (", ".join(h(x) for x in meta["disclosed_fact_ids"]) or "none"))
            if meta.get("delivery"):
                bits.append(f"delivery: {h(meta['delivery'])} (a generated reply is not proof the agent received it)")
            if bits:
                body += f"<div class='audit'><strong>audit (evaluator-only):</strong> {' · '.join(bits)}</div>"
        elif meta.get("request_id"):
            body += f"<div class='muted mono'>request_id {h(meta['request_id'])}</div>"
    return f"<div class='row {h(actor)}'>{head}{body}</div>"


def _verifier_section(t: dict) -> str:
    rep, rw = t["report"], t["reward"]
    if not rep and not rw:
        return "<p class='muted'>No verifier result yet.</p>"
    code = outcome_code({"reward": rw}) if rw else None
    name = rep.get("outcome") or (OUTCOME_NAMES[code] if code is not None else None)
    res = t["report"].get("reviewed_equals_submitted")
    res_text = ("null — diff-only pilot: the reviewed diff is agent-supplied and unverified against the graded submission"
                if res is None else str(res))
    parts = ["<dl class='meta'>",
             f"<dt>outcome</dt><dd>{_outcome_cell(name, rw.get('success'))} <span class='muted'>code {h(rw.get('outcome_code'))}</span></dd>",
             f"<dt>success</dt><dd>{h(rw.get('success'))}</dd><dt>reward</dt><dd>{h(rw.get('reward'))} <span class='muted'>(partial credit by requirement group; any regression sets 0)</span></dd>",
             f"<dt>simulator validity</dt><dd>{h(rep.get('simulator_validity') or ('unadjudicated' if t['meta'].get('condition') == 'interactive' else 'n/a'))}</dd>",
             f"<dt>transcript binding</dt><dd>{h(rep.get('transcript_binding_valid'))}</dd>",
             f"<dt>reviewed_equals_submitted</dt><dd>{h(res_text)}</dd>",
             f"<dt>patch sha256</dt><dd class='mono'>{h(rep.get('patch_sha256'))}</dd><dt>transcript hash</dt><dd class='mono'>{h(rep.get('transcript_hash'))}</dd>"]
    if rep.get("integrity_signals"):
        parts.append(f"<dt>integrity signals</dt><dd class='flag'>{h(json.dumps(rep['integrity_signals']))}</dd>")
    if rep.get("harbor_exception"):
        parts.append(f"<dt>harbor exception</dt><dd class='flag'>{h(json.dumps(rep['harbor_exception']))}</dd>")
    if rep.get("error"):
        parts.append(f"<dt>error</dt><dd class='flag'>{h(rep['error'])}</dd>")
    parts.append("</dl>")
    groups = rep.get("per_group") or []
    if groups:
        parts.append("<h3>Requirement groups</h3><table><tr><th>group</th><th>weight</th><th>passed</th></tr>")
        for g in groups:
            ok = g.get("passed")
            parts.append(f"<tr><td>{h(g.get('id'))}</td><td class='num'>{h(g.get('weight'))}</td>"
                         f"<td><span class='status {'good' if ok else 'critical'}'>{'pass' if ok else 'fail'}</span></td></tr>")
        parts.append("</table>")
    tests = rep.get("per_test") or []
    if tests:
        rows = "".join(f"<tr><td class='mono'>{h(x.get('id'))}</td><td>{h(x.get('kind'))}</td><td>{h(x.get('expected'))}</td>"
                       f"<td><span class='status {'good' if x.get('observed') == x.get('expected') else 'critical'}'>{h(x.get('observed'))}</span></td></tr>"
                       for x in tests)
        parts.append(f"<details><summary>{len(tests)} contract test(s)</summary><table><tr><th>test</th><th>kind</th><th>expected</th><th>observed</th></tr>{rows}</table></details>")
    return "".join(parts)


def _files_section(t: dict) -> str:
    items = []
    for name in TRIAL_FILES:
        p = _resolve_file(t, name)
        if p is not None and p.exists():
            items.append(f"<li><a href=\"/trial/{h(t['trial_id'])}/file/{h(name)}\">{h(name)}</a> <span class='muted mono'>{h(p)}</span></li>")
    if t["meta"].get("trial_dir"):
        items.append(f"<li class='muted'>Harbor trial dir <code>{h(t['meta']['trial_dir'])}</code> (inspect with <code>harbor view</code>)</li>")
    return "<ul>" + "".join(items) + "</ul>" if items else "<p class='muted'>No files yet.</p>"


def _resolve_file(t: dict, name: str) -> Path | None:
    base, rel = TRIAL_FILES[name]
    if base == "trial":
        return t["dir"] / rel
    trial_dir = t["meta"].get("trial_dir")
    if trial_dir:
        return Path(trial_dir) / rel
    found = sorted((ROOT / "jobs" / "runs" / t["trial_id"]).glob(f"*/{rel}"))
    return found[0] if len(found) == 1 else None


def _label_form(t: dict) -> str:
    cur = (t["label"] or {}).get("label")
    opts = "".join(f"<option value='{h(l)}'{' selected' if l == cur else ''}>{h(l)}</option>" for l in LABELS)
    note = h((t["label"] or {}).get("note", ""))
    tid = h(t["trial_id"])
    return (f"<form class='label' id='labelform' onsubmit='return saveLabel(event)'>"
            f"<p class='muted'>Reviewer label for this episode (evaluator-only annotation; it does not change reward or training eligibility)."
            f"{' Current: <strong>' + h(cur) + '</strong>' if cur else ' No label yet.'}</p>"
            f'<label for="label">label</label><select id="label" name="label"><option value=""{"" if cur else " selected"}>— choose —</option>{opts}</select>'
            f'<label for="note">note</label><textarea id="note" name="note" maxlength="2000">{note}</textarea>'
            f"<button type='submit'>Save label</button> <span id='labelresult' class='muted'></span></form>"
            "<script>async function saveLabel(ev){ev.preventDefault();const f=ev.target;const out=document.getElementById('labelresult');"
            "const body={label:f.label.value,note:f.note.value};if(!body.label){out.textContent='choose a label';return false}"
            f"const r=await fetch('/trial/{tid}/label',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});"
            "out.textContent=r.ok?'saved':'error '+r.status;return false}</script>")


def _poll_script(tid: str) -> str:
    return ("<script>(function(){const box=document.getElementById('conversation');const live=document.getElementById('live');"
            "function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e}"
            "function row(r){const d=el('div','row '+r.actor);const hd=el('div','hdr');"
            "hd.appendChild(el('span',r.ts_iso?'':'flag',r.ts_iso?r.ts_iso.slice(11,19):'no timestamp'));hd.appendChild(document.createTextNode(' '));"
            "hd.appendChild(el('span','badge '+r.actor,r.actor));hd.appendChild(el('span','',r.kind));d.appendChild(hd);d.appendChild(el('div','txt',r.text));return d}"
            f"async function refresh(){{try{{const res=await fetch('/api/trial/{tid}/timeline');const data=await res.json();"
            "if(data.status&&data.status.status!=='running'){location.reload();return}"
            "box.replaceChildren(...data.timeline.map(row));live.textContent='live · '+new Date().toISOString().slice(11,19)+' UTC · '+data.timeline.length+' rows'+(data.live_transcript_error?' · transcript unavailable: '+data.live_transcript_error:'')}"
            f"catch(e){{live.textContent='refresh failed: '+e}}}}refresh();setInterval(refresh,{POLL_MS});}})();</script>")


def render_episode(t: dict) -> str:
    m, tid = t["meta"], t["trial_id"]
    st = t["status"].get("status") or ("finished" if t["record"] else "unknown")
    parts = [f"<h1>Episode <span class='mono'>{h(tid)}</span></h1>", "<dl class='meta'>",
             f"<dt>status</dt><dd><span class='status {status_class(st)}'>{h(st)}</span> <span id='live'></span></dd>",
             f"<dt>started</dt><dd class='mono'>{h(m.get('started_at') or '—')}</dd>",
             f"<dt>finished</dt><dd class='mono'>{h(m.get('finished_at') or '—')}</dd>",
             f"<dt>scenario</dt><dd>{h(m.get('scenario_id'))}</dd><dt>condition</dt><dd>{h(m.get('condition'))}</dd>",
             f"<dt>brief profile</dt><dd>{h(m.get('brief_profile'))}</dd><dt>model</dt><dd>{h(m.get('model'))}</dd>",
             f"<dt>purpose</dt><dd>{h(m.get('purpose'))}</dd><dt>attempt</dt><dd>{h(m.get('attempt_index'))}</dd>",
             f"<dt>spec hash</dt><dd class='mono'>{h(m.get('spec_hash'))}</dd><dt>run config hash</dt><dd class='mono'>{h(m.get('run_config_hash'))}</dd>",
             f"<dt>compiled hash</dt><dd class='mono'>{h(m.get('compiled_hash'))}</dd><dt>image digest</dt><dd class='mono'>{h(m.get('image_digest'))}</dd>",
             "</dl>"]
    parts.append("<h2>Brief (instruction.md as the agent saw it)</h2>")
    parts.append(f"<pre>{h(t['instruction'])}</pre>" if t["instruction"] else "<p class='muted'>Preparing task; instruction not materialized yet.</p>")
    p = t["patience"]
    parts.append("<h2>Patience</h2>")
    if p["start"] is not None:
        end = p["end"] if p["end"] is not None else p["start"]
        pct = max(0, min(100, round(100 * end / p["start"]))) if p["start"] else 0
        parts.append(f"<p>{h(end)} / {h(p['start'])} remaining <span class='meter' role='img' aria-label='patience {h(end)} of {h(p['start'])}'><i style='width:{pct}%'></i></span>"
                     f"<span class='muted'>questions {h(p['questions'])} · reviews {h(p['reviews'])}{' · last tier ' + h(p['tier']) if p['tier'] else ''}</span></p>")
    else:
        parts.append("<p class='muted'>No stakeholder session for this condition.</p>")
    parts.append("<h2>Conversation and agent steps</h2>")
    if t["live"]:
        parts.append(f"<p class='muted'>Trial is running: the stakeholder transcript is polled every {POLL_MS // 1000}s; the agent trajectory appears when Harbor finishes.</p>")
        if t["live_transcript_error"]:
            parts.append(f"<p class='flag'>Live transcript unavailable: {h(t['live_transcript_error'])}</p>")
    rows = merge_timeline(t["trajectory"], t["transcript"], report=t["report"], reward=t["reward"], status=t["status"])
    parts.append("<div id='conversation'>" + ("".join(render_row(r) for r in rows) or "<p class='muted'>No messages yet.</p>") + "</div>")
    if any(r["no_timestamp"] for r in rows):
        parts.append("<p class='muted'>Rows marked <span class='flag'>no timestamp</span> are listed last; their position is not chronological evidence.</p>")
    parts.append("<h2>Verifier</h2>" + _verifier_section(t))
    parts.append("<h2>Reviewer label</h2>" + _label_form(t))
    parts.append("<h2>Files</h2>" + _files_section(t))
    if t["live"]:
        parts.append(_poll_script(h(tid)))
    return "".join(parts)


# ----------------------------------------------------------------------------- routes

@app.get("/", response_class=HTMLResponse)
def runs() -> HTMLResponse:
    return page("swesim runs", render_runs_page())


@app.get("/api/trial/{tid}/timeline")
def timeline(tid: str) -> dict:
    t = load_trial(tid)
    return {"trial_id": tid, "status": t["status"], "live": t["live"], "live_transcript_error": t["live_transcript_error"],
            "timeline": merge_timeline(t["trajectory"], t["transcript"], report=t["report"], reward=t["reward"], status=t["status"]),
            "report": t["report"], "reward": t["reward"], "label": t["label"], "patience": t["patience"], "meta": t["meta"]}


@app.get("/trial/{tid}", response_class=HTMLResponse)
def episode(tid: str) -> HTMLResponse:
    return page(f"Episode {tid}", render_episode(load_trial(tid)))


@app.post("/trial/{tid}/label")
def label(tid: str, body: dict) -> dict:
    d = trial_path(tid)
    if not d.is_dir():
        raise HTTPException(404)
    if not isinstance(body, dict) or body.get("label") not in LABELS:
        raise HTTPException(400, detail=f"label must be one of {list(LABELS)}")
    atomic_json(d / "label.json", {"label": body["label"], "note": str(body.get("note", ""))[:2000]})
    return {"saved": True}


@app.get("/trial/{tid}/file/{name}")
def trial_file(tid: str, name: str) -> PlainTextResponse:
    if name not in TRIAL_FILES:
        raise HTTPException(404)
    t = load_trial(tid)
    p = _resolve_file(t, name)
    if p is None or not p.is_file():
        raise HTTPException(404)
    media = "application/json" if name.endswith(".json") else "text/markdown; charset=utf-8"
    return PlainTextResponse(p.read_text(), media_type=media)
