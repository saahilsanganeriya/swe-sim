"""Server-rendered HTML for the dashboard: layout, components and the four pages.

Every dynamic value passes through ``h()`` (``html.escape``). No inline scripts: behaviour lives in
``static/app.js``; the page tells it what to do through ``data-*`` attributes on ``<body>``. Monospace is used only
for ids, hashes, paths and code. Rows that the JavaScript patches live carry ``data-id`` and ``data-f`` cells and
have a matching ``<template>`` so the client never builds HTML from strings. HTML attributes are single-quoted
throughout; ``h()`` escapes both quote characters.
"""
from __future__ import annotations

import json
import re
from html import escape

from swesim.dashboard.charts import pass_at_k_svg
from swesim.dashboard.data import REFUSAL_PREFIX, clock, short
from swesim.stages.score import OUTCOME_NAMES, outcome_code

TEMPORAL_UI = "http://localhost:8233"
SCENARIO_EVALUATOR_FILES = ("scenario.json", "scenario.draft.json")
LABELS = ("unresolved", "no-question", "disclosed-not-used", "simulator-omission", "derived-without-asking")
NAV = (("Overview", "/", "overview"), ("Runs", "/runs", "runs"), ("Scenarios", "/scenarios", "scenarios"),
       ("Jobs", "/jobs", "jobs"), ("Review", "/review", "review"), ("Create", "/create", "create"), ("Run", "/run", "run"))


def h(x) -> str:
    return escape("" if x is None else str(x), quote=True)


def dash(x) -> str:
    return h(x) if x not in (None, "") else "—"


def status_kind(s: str | None) -> str:
    s = (s or "").lower()
    if s in ("running", "pending", "launched", "requested"):
        return "warn pulse"
    if s in ("finished", "completed", "done", "valid"):
        return "good"
    if s in ("failed", "launch-failed", "runner-error", "error", "unit-failed", "invalid", "unreadable-record"):
        return "critical"
    return ""


def chip(text, kind: str = "", title: str | None = None) -> str:
    t = f" title='{h(title)}'" if title else ""
    return f"<span class='chip {h(kind)}'{t}>{dash(text)}</span>"


def outcome_chip(name: str | None, success, code: int | None = None) -> str:
    if not name:
        return "<span class='muted'>—</span>"
    kind = "good" if success == 1 else ("warn" if (code or 0) >= 5 else "critical")
    return chip(name, kind)


def fmt_metric(v, digits: int = 3) -> str:
    if v is None:
        return "<span class='muted' title='insufficient samples: n &lt; k'>n/a</span>"
    return h(f"{float(v):.{digits}f}")


def fmt_elapsed(s: float | None, running: bool = False) -> str:
    if s is None:
        return "—"
    s = round(s)
    hh, mm, ss = s // 3600, (s % 3600) // 60, s % 60
    text = f"{hh}h {mm:02d}m" if hh else (f"{mm}m {ss:02d}s" if mm else f"{ss}s")
    return h(text + (" …" if running else ""))


def meter(end, start, label: str = "remaining") -> str:
    if start is None:
        return "<span class='muted'>—</span>"
    end = start if end is None else end
    pct = max(0, min(100, round(100 * end / start))) if start else 0
    kind = " out" if end <= 0 else (" low" if end * 3 <= start else "")
    return (f"<span class='pt'>{h(end)} / {h(start)} {h(label)}</span> <span class='meter{kind}' role='img' "
            f"aria-label='patience {h(end)} of {h(start)}'><i style='width:{pct}%'></i></span>")


def layout(title: str, body: str, active: str, page: str, attrs: dict | None = None, live_label: str | None = None) -> str:
    data = " ".join(f"data-{h(k)}='{h(v)}'" for k, v in (attrs or {}).items())
    links = "".join(f"<a class='link' href='{href}'{' aria-current=\"page\"' if key == active else ''}>{name}</a>" for name, href, key in NAV)
    live = (f"<span id='live' class='live'><span class='dot'></span><span class='txt'>{h(live_label)}</span></span>" if live_label else "")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{h(title)}</title>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<link rel='stylesheet' href='/static/app.css'><script src='/static/app.js' defer></script></head>"
            f"<body data-page='{h(page)}' {data}><nav class='nav'><a class='brand' href='/'>swesim</a>{links}<span class='spacer'></span>"
            f"{live}<a class='link ext' href='{TEMPORAL_UI}' target='_blank' rel='noopener'>Temporal UI</a></nav>"
            f"<main class='wrap'>{body}</main></body></html>")


# ----------------------------------------------------------------------------- shared row renderers

def stage_chips(stages: list[dict]) -> str:
    return "<span class='stages'>" + "".join(
        chip(s.get("stage"), "good" if s.get("state") == "done" else "critical" if s.get("state") == "failed" else "pending", s.get("detail"))
        for s in stages) + "</span>"


def scores_line(scores: dict) -> str:
    if scores.get("unreadable"):
        return "<span class='flag'>unreadable scores.json</span>"
    groups = scores.get("groups") or []
    if not groups:
        return "<span class='muted'>—</span>"
    return "<br>".join(f"{h(g.get('condition') or g.get('key'))} · {h(g.get('model') or '—')}: pass@{h(g.get('requested_k'))} "
                       f"{fmt_metric(g.get('pass_at_k'), 2)} <span class='muted'>({h(g.get('n_valid'))}/{h(g.get('n_total'))})</span>"
                       for g in groups)


def review_chip(s: dict) -> str:
    """Auto-approved tasks carry a 'review needed' chip; held drafts an 'awaiting approval' chip; both open the review page."""
    ap = s.get("approval") or {}
    if ap.get("review_needed"):
        return (f"<a data-f='review' class='chip warn' href='/review/{h(s['id'])}' "
                f"title='approved by {h(ap.get('approved_by'))}; review the frozen spec'>review needed</a>")
    if s.get("awaiting_approval"):
        return f"<a data-f='review' class='chip warn pulse' href='/review/{h(s['id'])}'>awaiting approval</a>"
    return "<a data-f='review' class='chip warn hidden' href='#'></a>"


def scenario_row(s: dict) -> str:
    judge = s.get("judge")
    judge_text = (f"keep={judge.get('keep')} · rubric {judge.get('verified_rubric_score')}/3 · complexity {judge.get('complexity')}"
                  if judge else None)
    c = s.get("controls") or {}
    runs = s.get("runs") or {}
    runs_text = f"{runs.get('calibration', 0)} cal · {runs.get('comparison', 0)} cmp · {runs.get('demonstration', 0)} demo" + \
        (f" · {runs['unresolved']} unresolved" if runs.get("unresolved") else "")
    vs = s.get("validation_status")
    return (f"<tr data-id='{h(s['id'])}'><td data-f='id'><a href='/scenario/{h(s['id'])}'>{h(s['id'])}</a>"
            f"<div class='muted small mono'>{short(s.get('spec_hash'), 12) if s.get('spec_hash') else 'no spec hash yet'}</div></td>"
            f"<td data-f='source'>{dash(s.get('source_label'))}</td><td data-f='stages'>{stage_chips(s.get('stages') or [])}</td>"
            f"<td data-f='judge'>{dash(judge_text)}</td><td data-f='facts' class='num'>{dash(s.get('facts'))}</td>"
            f"<td data-f='controls'>{c.get('plausible_wrong', 0)} wrong · {c.get('alternative_correct', 0)} alt · {c.get('tamper', 0)} tamper</td>"
            f"<td data-f='validation'>{chip(vs or 'not validated', 'good' if vs == 'valid' else 'critical' if vs == 'invalid' else 'pending', '; '.join(s.get('validation_failures') or []) or None)}"
            f" {review_chip(s)}</td>"
            f"<td data-f='runs'>{h(runs_text)}</td><td data-f='scores'>{scores_line(s.get('scores') or {})}</td></tr>")


SCENARIO_TEMPLATE = ("<template id='scenario-row'><tr data-id=''><td data-f='id'><a href='#'></a></td><td data-f='source'></td>"
                     "<td data-f='stages'></td><td data-f='judge'></td><td data-f='facts' class='num'></td><td data-f='controls'></td>"
                     "<td data-f='validation'><span class='chip'></span> <a data-f='review' class='chip warn hidden' href='#'></a></td>"
                     "<td data-f='runs'></td><td data-f='scores'></td></tr></template>")
SCENARIO_HEAD = ("<tr><th>scenario</th><th>source</th><th>stages</th><th>judge</th><th class='num'>facts</th><th>controls</th>"
                 "<th>validation</th><th>runs</th><th>scores</th></tr>")


def trial_row(t: dict) -> str:
    unresolved = t.get("unresolved") and not t.get("running")
    if t.get("outcome"):
        outcome = outcome_chip(t["outcome"], t.get("success"), t.get("outcome_code"))
    elif unresolved:
        outcome = (f"<span class='flag'>{'error ' + h(t['error_type']) if t.get('error_type') else 'no verifier record'}</span> "
                   f"<span class='muted small'>({h(t.get('source_file') or 'no files')})</span>")
    else:
        outcome = "<span class='muted'>—</span>"
    if t.get("patience_start") is not None:
        patience = meter(t.get("patience_end"), t.get("patience_start"), "")
    else:
        patience = f"<span class='muted'>{'live: ' + h(t['live_error']) if t.get('live_error') else '—'}</span>"
    return (f"<tr data-id='{h(t['trial_id'])}' data-scenario='{h(t.get('scenario_id'))}' data-condition='{h(t.get('condition'))}' "
            f"data-model='{h(t.get('model'))}' data-status='{h(t.get('status'))}'>"
            f"<td data-f='id'><a class='trial-link' href='/trial/{h(t['trial_id'])}'>{h(t['trial_id'])}</a></td>"
            f"<td data-f='scenario'>{dash(t.get('scenario_id'))}</td><td data-f='condition'>{dash(t.get('condition'))}</td>"
            f"<td data-f='profile'>{dash(t.get('brief_profile'))}</td><td data-f='model'>{dash(t.get('model'))}</td>"
            f"<td data-f='purpose'>{dash(t.get('purpose'))}</td>"
            f"<td><span data-f='status' class='chip {status_kind(t.get('status'))}'>{dash(t.get('status'))}</span> "
            f"<span data-f='unresolved' class='{'flag' if unresolved else ''}'>{'unresolved' if unresolved else ''}</span></td>"
            f"<td data-f='elapsed' class='num' title='started {h(t.get('started_at') or '—')} · finished {h(t.get('finished_at') or '—')}'>"
            f"{fmt_elapsed(t.get('elapsed_s'), bool(t.get('running')))}</td>"
            f"<td data-f='patience'>{patience}</td><td data-f='outcome'>{outcome}</td>"
            f"<td data-f='reward' class='num'>{dash(t.get('reward'))}</td><td data-f='label'>{dash(t.get('label'))}</td></tr>")


TRIAL_TEMPLATE = ("<template id='trial-row'><tr data-id=''><td data-f='id'><a class='trial-link' href='#'></a></td><td data-f='scenario'></td>"
                  "<td data-f='condition'></td><td data-f='profile'></td><td data-f='model'></td><td data-f='purpose'></td>"
                  "<td><span data-f='status' class='chip'></span> <span data-f='unresolved'></span></td><td data-f='elapsed' class='num'></td>"
                  "<td data-f='patience'></td><td data-f='outcome'></td><td data-f='reward' class='num'></td><td data-f='label'></td></tr></template>")
TRIAL_HEAD = ("<tr><th>trial</th><th>scenario</th><th>condition</th><th>profile</th><th>model</th><th>purpose</th><th>status</th>"
              "<th class='num'>elapsed</th><th>patience</th><th>outcome</th><th class='num'>reward</th><th>label</th></tr>")

EMPTY_SCENARIOS = ("<p class='empty' id='scenarios-body-empty'><strong>No scenarios yet.</strong> A scenario directory appears under "
                   "<code>workspace/scenarios/&lt;id&gt;/</code> once <code>swesim draft</code> writes a draft; the chips then fill in as "
                   "<code>judge</code>, <code>approve</code>, <code>compile</code>, <code>validate</code> and <code>register</code> run "
                   "(or as the Temporal PipelineWorkflow advances). This board refreshes every 5 s.</p>")
EMPTY_TRIALS = ("<p class='empty' id='trials-body-empty'><strong>No trials yet.</strong> Attempts appear here within 5 s of "
                "<code>swesim run &lt;scenario&gt; --condition interactive</code> or a Temporal RunMatrixWorkflow launch: status, elapsed "
                "time and live patience while running; outcome and reward once the verifier finishes.</p>")


def tiles(summary: dict) -> str:
    items = (("scenarios", "scenarios"), ("valid", "validated"), ("running", "running"), ("finished", "finished"),
             ("succeeded", "succeeded"), ("unresolved", "unresolved"))
    return "<div class='grid tiles'>" + "".join(
        f"<div class='tile'><div class='label'>{label}</div><div class='value' data-tile='{key}'>{h(summary.get(key, 0))}</div></div>"
        for key, label in items) + "</div>"


def _hidden_unless(items, empty_html: str) -> str:
    return empty_html if not items else empty_html.replace("<p class='empty'", "<p class='empty hidden'")


def scenarios_table(scenarios: list[dict]) -> str:
    cls = "hidden" if not scenarios else ""
    return (f"<div class='table-wrap'><table class='{cls}'><thead>{SCENARIO_HEAD}</thead><tbody id='scenarios-body'>"
            + "".join(scenario_row(s) for s in scenarios) + "</tbody></table></div>" + _hidden_unless(scenarios, EMPTY_SCENARIOS)
            + SCENARIO_TEMPLATE)


def trials_table(trials: list[dict]) -> str:
    cls = "hidden" if not trials else ""
    return (f"<div class='table-wrap'><table class='{cls}'><thead>{TRIAL_HEAD}</thead><tbody id='trials-body'>"
            + "".join(trial_row(t) for t in trials) + "</tbody></table></div>" + _hidden_unless(trials, EMPTY_TRIALS) + TRIAL_TEMPLATE)


# ----------------------------------------------------------------------------- pages

def start_cta(ov: dict) -> str:
    """Call to action when the board is empty: the console can start creation and run jobs."""
    if ov.get("scenarios") or ov.get("trials"):
        return ""
    return ("<div class='card cta'><strong>Nothing here yet.</strong> Start a creation job to mine, judge, draft, validate and register tasks, "
            "then run a matrix of attempts over them. <div class='row'><a class='button primary' href='/create'>Create tasks</a> "
            "<a class='button' href='/run'>Run trials</a> <a class='button' href='/jobs'>Jobs</a></div></div>")


def overview_page(ov: dict, root, registry) -> str:
    body = ["<h1>Overview</h1>",
            ("<p class='lead'>Scenarios in creation and runs in flight, from the workspace files. "
             f"Workspace <code>{h(root)}</code> · registry <code>{h(registry)}</code>.</p>"),
            tiles(ov.get("summary") or {}),
            start_cta(ov),
            "<h2>Scenarios</h2>", scenarios_table(ov.get("scenarios") or []),
            ("<h2>Trials</h2><p class='muted small'>Every attempt directory is listed. <span class='flag'>unresolved</span> marks an attempt "
             "that ended without a verifier record; it never enters n or pass@k. Patience is live for running trials.</p>"),
            trials_table(ov.get("trials") or [])]
    return layout("swesim · overview", "".join(body), "overview", "overview", {"poll-ms": "5000"}, "updated 0s ago")


def scenarios_page(scenarios: list[dict]) -> str:
    body = ["<h1>Scenarios</h1><p class='lead'>Every scenario directory in the workspace with its pipeline stage, judge verdict and scores.</p>",
            scenarios_table(scenarios)]
    return layout("swesim · scenarios", "".join(body), "scenarios", "scenarios")


def _select(name: str, values: list, label: str) -> str:
    opts = "".join(f"<option value='{h(v)}'>{h(v)}</option>" for v in values)
    return f"<label>{label}<select data-filter='{name}' name='{name}'><option value=''>all</option>{opts}</select></label>"


def scores_table(groups: list[dict]) -> str:
    if not groups:
        return "<p class='empty'>No scored groups yet. <code>swesim score &lt;id&gt;</code> writes scores.json after attempts finish.</p>"
    rows = []
    for g in groups:
        k = g.get("requested_k")
        n_unres = g.get("attempts_unresolved") or 0
        by_status = ", ".join(f"{h(s)} {h(c)}" for s, c in sorted((g.get("unresolved_by_status") or {}).items()))
        attempts = (f"{h(g.get('attempts_total'))} (<span class='{'flag' if n_unres else 'muted'}' title='{by_status or 'none'}'>{h(n_unres)}</span>)"
                    if g.get("attempts_total") is not None else "—")
        outcomes = ", ".join(f"{h(n)} {h(c)}" for n, c in sorted((g.get("outcomes") or {}).items())) or "—"
        labels = ", ".join(f"{h(n)} {h(c)}" for n, c in sorted((g.get("labels") or {}).items())) or "—"
        trials = " ".join(f"<a class='trial-link' href='/trial/{h(t)}'>{h(short(t, 8))}</a>" for t in g.get("trial_ids") or [] if t)
        trials += "".join(f" <a class='trial-link flag' title='unresolved attempt: {h(x.get('status'))}' href='/trial/{h(x.get('trial_id'))}'>{h(short(x.get('trial_id'), 8))}</a>"
                          for x in g.get("unresolved") or [] if x.get("trial_id"))
        rows.append(f"<tr><td>{dash(g.get('condition') or g.get('key'))}</td><td>{dash(g.get('brief_profile'))}</td><td>{dash(g.get('model'))}</td>"
                    f"<td>{dash(g.get('purpose'))}</td><td class='num'>{h(g.get('n_valid'))} / {h(g.get('n_total'))}</td><td class='num'>{attempts}</td>"
                    f"<td class='num'>{h(g.get('success_count'))}</td><td class='num'>{fmt_metric(g.get('pass_at_k'))} <span class='muted'>@{h(k)}</span></td>"
                    f"<td class='num'>{fmt_metric(g.get('pass_pow_k'))} <span class='muted'>^{h(k)}</span></td><td class='num'>{fmt_metric(g.get('mean_reward'))}</td>"
                    f"<td>{outcomes}</td><td>{labels}</td><td>{trials or '—'}</td></tr>")
    return ("<div class='table-wrap'><table><thead><tr><th>condition</th><th>profile</th><th>model</th><th>purpose</th><th class='num'>valid / total</th>"
            "<th class='num' title='attempt directories in this group; unresolved = ended without a verifier record'>attempts (unresolved)</th>"
            "<th class='num'>success</th><th class='num'>pass@k</th><th class='num'>pass^k</th><th class='num'>mean reward</th><th>outcomes</th>"
            "<th>labels</th><th>trials</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


def runs_page(trials: list[dict], scenarios: list[dict]) -> str:
    def distinct(key):
        return sorted({str(t.get(key)) for t in trials if t.get(key)})
    toolbar = ("<div class='toolbar'>" + _select("scenario", distinct("scenario_id"), "scenario") + _select("condition", distinct("condition"), "condition")
               + _select("model", distinct("model"), "model") + _select("status", distinct("status"), "status")
               + "<button type='button' id='filter-reset'>reset</button><span class='muted small' id='filter-count'></span></div>")
    body = [("<h1>Runs</h1><p class='lead'>Every attempt, filterable; pass@k and pass^k per frozen run configuration. Groups are condition · brief "
             "profile · run config (model, purpose, build); calibration and comparison are never pooled; metrics are n/a when fewer than k valid attempts exist.</p>"),
            toolbar, trials_table(trials), "<h2>Scores by scenario</h2>"]
    scored = [s for s in scenarios if (s.get("scores") or {}).get("exists")]
    if not scored:
        body.append("<p class='empty'><strong>No scores yet.</strong> <code>swesim score &lt;id&gt;</code> (or the Temporal score activity) writes "
                    "<code>workspace/scenarios/&lt;id&gt;/scores.json</code>; the pass@k chart and table appear here.</p>")
    for s in scored:
        sc = s["scores"]
        body.append(f"<h3 id='scores-{h(s['id'])}'><a href='/scenario/{h(s['id'])}'>{h(s['id'])}</a></h3>")
        if sc.get("unreadable"):
            body.append("<p class='empty'>unreadable scores.json (not a JSON object of groups); re-run <code>swesim score</code>.</p>")
            continue
        groups = sc.get("groups") or []
        body.append("<div class='side-by-side'>" + pass_at_k_svg(groups) + scores_table(groups) + "</div>")
    return layout("swesim · runs", "".join(body), "runs", "runs")


# ----------------------------------------------------------------------------- episode

def _files_from_diff(text: str) -> list[str]:
    files = []
    for m in re.finditer(r"^\+\+\+ (?:b/)?(\S+)", text or "", re.MULTILINE):
        if m.group(1) != "/dev/null" and m.group(1) not in files:
            files.append(m.group(1))
    return files


def chat_bubbles(rows: list[dict], live: bool, evaluator: bool = False) -> str:
    """Stakeholder fact annotations, tier/patience internals, tool audit and delivery flags are evaluator-only:
    they render only when the page was opened with ?evaluator=1 (same gate as author-only facts on the scenario page)."""
    convo = [r for r in rows if r.get("actor") in ("agent", "stakeholder") and r.get("kind") not in ("agent-step", "tool_call")]
    sha_by_request = {(r.get("meta") or {}).get("request_id"): (r.get("meta") or {}).get("reviewed_diff_sha256")
                      for r in rows if r.get("actor") == "stakeholder" and (r.get("meta") or {}).get("reviewed_diff_sha256")}
    parts = []
    for r in convo:
        m = r.get("meta") or {}
        review = r.get("kind") == "review"
        refusal = r["actor"] == "stakeholder" and (m.get("tier") == "refused" or str(r.get("text") or "").startswith(REFUSAL_PREFIX))
        role = ("refusal" if refusal else "review request" if review and r["actor"] == "agent" else "review" if review
                else "question" if r["actor"] == "agent" else "reply")
        when = (f"<time datetime='{h(r['ts_iso'])}'>{h(clock(r['ts']))}</time>" if r.get("ts") is not None
                else "<span class='flag' title='no timestamp'>no timestamp</span>")
        head = f"<div class='who'><span class='badge {h(r['actor'])}'>{h(r['actor'])}</span><span>{h(role)}</span>{when}</div>"
        text = str(r.get("text") or "")
        if review and r["actor"] == "agent":
            sha = sha_by_request.get(m.get("request_id"))
            files = _files_from_diff(text)
            first, _, diff = text.partition("\n")
            body = (f"<div class='text'>{h(first)}</div><div class='muted small'>diff sha256 <span class='mono'>{h(short(sha, 16)) if sha else 'unknown'}</span>"
                    f" · {len(files)} file(s) · diff-only, unverified</div>"
                    + (f"<ul class='files'>{''.join(f'<li>{h(f)}</li>' for f in files)}</ul>" if files else "")
                    + (f"<details class='diff'><summary>diff</summary><pre>{h(diff)}</pre></details>" if diff.strip() else ""))
        else:
            body = f"<div class='text'>{h(text)}</div>"
        if r["actor"] == "stakeholder" and evaluator:
            bits = []
            if m.get("tier") is not None or m.get("patience_remaining") is not None:
                bits.append(f"tier {h(m.get('tier'))} · patience remaining {h(m.get('patience_remaining'))}")
            if m.get("disclosed_fact_ids") is not None:
                bits.append("disclosed fact ids: " + (", ".join(h(x) for x in m["disclosed_fact_ids"]) or "none"))
            if m.get("tool_calls"):
                bits.append("tools: " + ", ".join(h(c.get("tool") if isinstance(c, dict) else c) for c in m["tool_calls"]))
            if m.get("delivery"):
                bits.append(f"delivery {h(m['delivery'])} (a generated reply is not proof the agent received it)")
            if bits:
                body += f"<div class='audit'><strong>audit (evaluator-only):</strong> {' · '.join(bits)}</div>"
        elif m.get("request_id"):
            body += f"<div class='muted small mono'>request_id {h(m['request_id'])}</div>"
        parts.append(f"<div class='bubble {h(r['actor'])}{' review' if review else ''}{' refusal' if refusal else ''}'>{head}{body}</div>")
    if live:
        parts.append("<div class='working'><span class='dots'><i></i><i></i><i></i></span><span>agent is working…</span></div>")
    empty = ("<p class='empty' id='chat-empty'><strong>No exchanges yet.</strong> The agent asks with <code>ask-user</code> and requests a "
             "diff review with <code>request-review</code>; questions appear on the left, stakeholder replies on the right.</p>")
    return f"<div class='chat' id='chat'>{''.join(parts)}</div>" + _hidden_unless(convo, empty)


def _call_text(call) -> str:
    if not isinstance(call, dict):
        return str(call)
    args = call.get("arguments")
    if isinstance(args, dict) and isinstance(args.get("command"), str):
        return args["command"]
    return args if isinstance(args, str) else json.dumps(args, indent=1)


def timeline_row(r: dict, evaluator: bool = False) -> str:
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
            inner += "".join(f"<pre>{h(short(o.get('content') if isinstance(o, dict) else o, 1500))}</pre>" for o in obs)
            body += f"<details><summary>{len(calls)} tool call(s), {len(obs)} observation(s)</summary>{inner}</details>"
    elif kind in ("prompt", "system"):
        body = f"<details><summary>{h(kind)} from harness ({len(r['text'])} chars)</summary><pre>{h(r['text'])}</pre></details>"
    elif kind == "tool_call":
        body = f"<code>{h(r['text'])}</code> <span class='muted'>stakeholder tool call; diff-only, unverified</span>"
    elif kind == "outcome":
        body = f"<div class='txt'><strong>{h(r['text'])}</strong></div>"
    else:
        body = f"<div class='txt'>{h(r['text'])}</div>"
        if actor == "stakeholder" and evaluator:
            bits = []
            if meta.get("tier") is not None or meta.get("patience_remaining") is not None:
                bits.append(f"tier {h(meta.get('tier'))}, patience remaining {h(meta.get('patience_remaining'))}")
            if meta.get("disclosed_fact_ids") is not None:
                bits.append("disclosed fact ids: " + (", ".join(h(x) for x in meta["disclosed_fact_ids"]) or "none"))
            if bits:
                body += f"<div class='audit'><strong>audit (evaluator-only):</strong> {' · '.join(bits)}</div>"
        elif meta.get("request_id"):
            body += f"<div class='muted small mono'>request_id {h(meta['request_id'])}</div>"
    return f"<div class='row {h(actor)}'>{head}{body}</div>"


def verifier_section(t: dict) -> str:
    rep, rw = t["report"], t["reward"]
    if not rep and not rw:
        return "<p class='empty'>No verifier result yet. Outcome, reward and per-group results appear when the trial completes.</p>"
    code = outcome_code({"reward": rw}) if rw else None
    name = rep.get("outcome") or (OUTCOME_NAMES[code] if code is not None else None)
    res = rep.get("reviewed_equals_submitted")
    res_text = ("null — diff-only pilot: the reviewed diff is agent-supplied and unverified against the graded submission" if res is None else str(res))
    parts = ["<dl class='kv'>",
             f"<dt>outcome</dt><dd>{outcome_chip(name, rw.get('success'), code)} <span class='muted'>code {h(rw.get('outcome_code'))}</span></dd>",
             f"<dt>success</dt><dd>{h(rw.get('success'))}</dd><dt>reward</dt><dd>{h(rw.get('reward'))} <span class='muted'>partial credit by requirement group; any regression sets 0</span></dd>",
             f"<dt>simulator validity</dt><dd>{h(rep.get('simulator_validity') or ('unadjudicated' if t['meta'].get('condition') == 'interactive' else 'n/a'))}</dd>",
             f"<dt>transcript binding</dt><dd>{h(rep.get('transcript_binding_valid'))}</dd>",
             f"<dt>reviewed_equals_submitted</dt><dd>{h(res_text)}</dd>"]
    if rep.get("integrity_signals"):
        parts.append(f"<dt>integrity signals</dt><dd class='flag'>{h(json.dumps(rep['integrity_signals']))}</dd>")
    if rep.get("harbor_exception"):
        parts.append(f"<dt>harbor exception</dt><dd class='flag'>{h(json.dumps(rep['harbor_exception']))}</dd>")
    if rep.get("error"):
        parts.append(f"<dt>error</dt><dd class='flag'>{h(rep['error'])}</dd>")
    parts.append("</dl>")
    groups = rep.get("per_group") or []
    if groups:
        parts.append("<h3>Requirement groups</h3><table><thead><tr><th>group</th><th class='num'>weight</th><th>passed</th></tr></thead><tbody>")
        for g in groups:
            ok = g.get("passed")
            parts.append(f"<tr><td>{h(g.get('id'))}</td><td class='num'>{h(g.get('weight'))}</td><td>{chip('pass' if ok else 'fail', 'good' if ok else 'critical')}</td></tr>")
        parts.append("</tbody></table>")
    tests = rep.get("per_test") or []
    if tests:
        rows = "".join(f"<tr><td class='mono'>{h(x.get('id'))}</td><td>{h(x.get('kind'))}</td><td>{h(x.get('expected'))}</td>"
                       f"<td>{chip(x.get('observed'), 'good' if x.get('observed') == x.get('expected') else 'critical')}</td></tr>" for x in tests)
        parts.append(f"<details><summary>{len(tests)} contract test(s)</summary><table><thead><tr><th>test</th><th>kind</th><th>expected</th><th>observed</th></tr></thead><tbody>{rows}</tbody></table></details>")
    return "".join(parts)


def label_form(t: dict) -> str:
    cur = (t["label"] or {}).get("label")
    opts = "".join(f"<option value='{h(lab)}'{' selected' if lab == cur else ''}>{h(lab)}</option>" for lab in LABELS)
    note = h((t["label"] or {}).get("note", ""))
    current = f" Current: <strong>{h(cur)}</strong>" if cur else " No label yet."
    return ("<form class='label' id='labelform'><p class='muted small'>Reviewer label for this episode (evaluator-only annotation; it does not change "
            f"reward or training eligibility).{current}</p>"
            f'<label for="label">label</label><select id="label" name="label"><option value=""{"" if cur else " selected"}>— choose —</option>{opts}</select>'
            f'<label for="note">note</label><textarea id="note" name="note" maxlength="2000">{note}</textarea>'
            "<div><button type='submit' class='primary'>Save label</button> <span id='labelresult' class='muted small'></span></div></form>")


def files_section(t: dict, resolve, evaluator: bool = False) -> str:
    items = []
    gated = 0
    for name in t["files"]:
        p = resolve(t, name)
        if p is None or not p.exists():
            continue
        if name in EVALUATOR_FILES and not evaluator:
            gated += 1
            continue
        q = "?evaluator=1" if name in EVALUATOR_FILES else ""
        items.append(f"<li><a href='/trial/{h(t['trial_id'])}/file/{h(name)}{q}'>{h(name)}</a> <span class='muted mono small'>{h(p)}</span></li>")
    if gated:
        items.append(f"<li class='muted'>{gated} evaluator-only file(s) (record, transcript, spec) hidden; turn the evaluator switch on to open them.</li>")
    if t["meta"].get("trial_dir"):
        items.append(f"<li class='muted'>Harbor trial dir <code>{h(t['meta']['trial_dir'])}</code> (inspect with <code>harbor view</code>)</li>")
    return "<ul class='small'>" + "".join(items) + "</ul>" if items else "<p class='empty'>No files yet.</p>"


EVALUATOR_FILES = ("record.json", "transcript.json", "spec.json")


def evaluator_switch(base: str, on: bool, label: str) -> str:
    return (f"<a class='switch{' on' if on else ''}' href='{h(base)}{'' if on else '?evaluator=1'}' role='switch' aria-checked='{'true' if on else 'false'}'>"
            f"<i></i>evaluator-only: {h(label)} {'shown' if on else 'hidden'}</a>")


def episode_page(t: dict, rows: list[dict], resolve, evaluator: bool = False) -> str:
    m, tid = t["meta"], t["trial_id"]
    rows = [r for r in rows if evaluator or r.get("kind") != "tool_call"]  # stakeholder tool audit is evaluator-only
    st = t["status"].get("status") or ("finished" if t["record"] else "unknown")
    live = bool(t["live"])
    p = t["patience"]
    left = [f"<h1>Episode <span class='mono'>{h(tid)}</span></h1>",
            (f"<p class='lead'>{chip(st, status_kind(st))} {dash(m.get('scenario_id'))} · {dash(m.get('condition'))} · brief {dash(m.get('brief_profile'))} · "
             f"{dash(m.get('model'))} · {dash(m.get('purpose'))} · attempt {dash(m.get('attempt_index'))}</p>")]
    if t["unresolved"] and not live:
        u = t["unresolved"]
        left.append(f"<p class='flag'>Unresolved attempt: no record.json was written. status {h(u['status'])}"
                    f"{' · error ' + h(u['error_type']) if u.get('error_type') else ''} · from {h(u['source_file'])}. "
                    "Not counted in n, success or pass@k; reported as attempts_unresolved. Files that exist are listed on the right.</p>")
    pat = meter(p["end"], p["start"]) if p["start"] is not None else "<span class='muted'>no stakeholder session for this condition</span>"
    tier = f" · last tier {h(p['tier'])}" if p.get("tier") and evaluator else ""  # the tier is a stakeholder internal
    left.append(f"<div id='patience' class='card small'><strong>Patience</strong> {pat} "
                f"<span class='pq muted'>questions {h(p['questions'])} · reviews {h(p['reviews'])}{tier}</span></div>")
    left.append(f"<p class='small'>{evaluator_switch(f'/trial/{tid}', evaluator, 'fact annotations, tool audit, raw transcript/spec files')}</p>")
    left.append("<div class='tabs' role='tablist'><button type='button' role='tab' id='tab-chat' aria-controls='panel-chat' data-tab='chat' aria-selected='true'>Conversation</button>"
                "<button type='button' role='tab' id='tab-timeline' aria-controls='panel-timeline' data-tab='timeline' aria-selected='false'>Timeline</button></div>")
    if live:
        left.append("<p class='note'>Trial is running: the stakeholder transcript is polled every 3 s. The agent's own tool calls appear in the "
                    "Timeline tab when the trial completes; Harbor writes the trajectory at the end.</p>")
        if t["live_transcript_error"]:
            left.append(f"<p class='flag'>Live transcript unavailable: {h(t['live_transcript_error'])}</p>")
    else:
        left.append("<p class='note'>Conversation shows the stakeholder transcript. The agent's tool calls, interleaved by time with the "
                    "conversation and the verifier outcome, are in the Timeline tab.</p>")
    left.append(f"<section data-tab-panel='chat' id='panel-chat' role='tabpanel' aria-labelledby='tab-chat'>{chat_bubbles(rows, live, evaluator)}</section>")
    tl = "".join(timeline_row(r, evaluator) for r in rows) or "<p class='empty'>No timeline rows yet.</p>"
    if any(r.get("no_timestamp") for r in rows):
        tl += "<p class='muted small'>Rows marked <span class='flag'>no timestamp</span> are listed last; their position is not chronological evidence.</p>"
    left.append(f"<section data-tab-panel='timeline' id='panel-timeline' role='tabpanel' aria-labelledby='tab-timeline' class='timeline hidden'>{tl}</section>")
    brief = (f"<details open><summary class='muted small'>instruction.md as the agent saw it</summary><pre>{h(t['instruction'])}</pre></details>"
             if t["instruction"] else "<p class='empty'>Instruction not materialized yet; it appears when the trial task is prepared.</p>")
    right = ["<div class='panel card'><h2>Brief</h2>", brief, "<h2>Verifier</h2>", verifier_section(t),
             "<h2>Lineage</h2><dl class='kv'>",
             f"<dt>spec hash</dt><dd class='mono'>{dash(m.get('spec_hash'))}</dd><dt>run config hash</dt><dd class='mono'>{dash(m.get('run_config_hash'))}</dd>",
             f"<dt>compiled hash</dt><dd class='mono'>{dash(m.get('compiled_hash'))}</dd><dt>image digest</dt><dd class='mono'>{dash(m.get('image_digest'))}</dd>",
             f"<dt>started</dt><dd class='mono'>{dash(m.get('started_at'))}</dd><dt>finished</dt><dd class='mono'>{dash(m.get('finished_at'))}</dd></dl>",
             "<h2>Reviewer label</h2>", label_form(t), "<h2>Files</h2>", files_section(t, resolve, evaluator), "</div>"]
    body = f"<div class='two-col'><div>{''.join(left)}</div>{''.join(right)}</div>"
    attrs = {"trial": tid, "live": "true" if live else "false", "poll-ms": "3000", "evaluator": "1" if evaluator else "0"}
    return layout(f"Episode {tid}", body, "runs", "episode", attrs, "live" if live else None)


# ----------------------------------------------------------------------------- scenario

def _fact(f: dict) -> str:
    tests = ", ".join(h(t) for t in f.get("tests") or [])
    src = f.get("source_url")
    prov = (f"<a href='{h(src)}' target='_blank' rel='noopener'>{h(f.get('provenance'))}</a>" if src and str(src).startswith("http")
            else h(f.get("provenance")))
    excerpt = f"<div class='muted small'>excerpt: “{h(short(f.get('source_excerpt'), 160))}”</div>" if f.get("source_excerpt") else ""
    return (f"<div class='fact'><span class='id'>{h(f.get('id'))}</span>{chip(f.get('kind'), 'info')} <span class='muted small'>{prov}"
            f"{' · tests ' + tests if tests else ''}</span><div>{h(f.get('text'))}</div>{excerpt}</div>")


def scenario_page_sections(d: dict, toggle_base: str | None = None) -> dict:
    """The scenario page pieces (head, briefs, ledger, controls, judge, links) so the review page can reuse them."""
    b = d["board"]
    sid = b["id"]
    judge = d.get("judge") or {}
    source_link = f" · <a href='{h(b['source_url'])}' target='_blank' rel='noopener'>source ↗</a>" if b.get("source_url") else ""
    repo_ok = str(b.get("repo_url") or "").startswith(("http://", "https://"))
    repo_link = f" · <a href='{h(b['repo_url'])}' target='_blank' rel='noopener'>repository ↗</a>" if repo_ok else ""
    draft_flag = "" if d["frozen"] else " · <span class='flag'>draft (not approved yet)</span>"
    head = [f"<h1>{h(sid)} <span class='mono'>v{h(b.get('version'))}</span></h1>",
            f"<p class='lead'>{dash(b.get('source_label'))}{source_link}{repo_link} · spec hash <span class='mono'>{dash(b.get('spec_hash'))}</span>{draft_flag}</p>",
            stage_chips(b.get("stages") or [])]
    if not d["exists"]:
        head.append("<p class='empty'><strong>Registered, but no scenario directory in this workspace copy.</strong> Copy "
                    f"<code>workspace/scenarios/{h(sid)}/</code> here to see briefs, ledger and controls.</p>")
        return {"head": "".join(head), "briefs": "", "ledger": "", "controls": "", "judge": "", "links": "", "exists": False}
    natural = "".join(f"<mark class='removed' title='withheld from the withheld brief'>{h(seg)}</mark>" if removed else h(seg)
                      for seg, removed in d["natural_segments"])
    withheld = (f"<pre>{h(d['withheld'])}</pre>" if d.get("withheld") else
                "<p class='empty'>No withheld brief: hidden and interactive runs use the natural brief for this scenario.</p>")
    briefs = (f"<h2>Briefs</h2><p class='legend-inline'><mark class='removed'>highlighted</mark> spans are the original-issue excerpts removed from the withheld "
              f"brief ({len(d['removed_excerpts'])} withholding operation(s)).</p><div class='briefs'>"
              f"<div class='brief'><h3>natural <span class='muted small'>original issue</span></h3><pre>{natural}</pre></div>"
              f"<div class='brief'><h3>withheld <span class='muted small'>what hidden/interactive agents see</span></h3>{withheld}</div></div>")
    ops = d.get("operations") or []
    if ops:
        items = "".join(f"<li>{chip(o.get('type'), 'info')} target <span class='mono'>{h(o.get('target'))}</span> — {h(o.get('rationale'))}</li>" for o in ops)
        briefs += f"<details><summary>{len(ops)} operation(s)</summary><ul class='small'>{items}</ul></details>"
    order = ("brief", "on-request", "volunteered-at-review", "repo-derivable", "author-only")
    groups = d["ledger_groups"]
    on = d["evaluator"]
    base = toggle_base or f"/scenario/{sid}"
    toggle = evaluator_switch(base, on, "author-only facts" + ("" if on else f" ({d['hidden_author_only']})"))
    ledger = [f"<h2>Ledger</h2><p class='muted small'>{sum(len(v) for v in groups.values())} fact(s) visible · grouped by discovery. {toggle}</p>"]
    if not groups:
        ledger.append("<p class='empty'>No ledger facts in this spec.</p>")
    for disc in [x for x in order if x in groups] + [x for x in groups if x not in order]:
        facts = groups[disc]
        eval_flag = " <span class='flag small'>evaluator-only</span>" if disc == "author-only" else ""
        ledger.append(f"<h3>{h(disc)} <span class='muted small'>{len(facts)}</span>{eval_flag}</h3><div class='card'>{''.join(_fact(f) for f in facts)}</div>")
    v = d.get("validation") or {}
    vstatus = v.get("status")
    vline = (f" · build <span class='mono'>{h(short(v.get('compiled_hash'), 12))}</span> · image <span class='mono'>{h(short(v.get('image_digest'), 19))}</span>" if v
             else " · <code>swesim validate &lt;id&gt;</code> runs nop, oracle, every control and the reachability probe on Modal")
    controls = [f"<h2>Controls and validation</h2><p class='lead'>{chip(vstatus or 'not validated', 'good' if vstatus == 'valid' else 'critical' if vstatus == 'invalid' else 'pending')}{vline}</p>"]
    if d.get("failures"):
        controls.append("<ul class='small'>" + "".join(f"<li class='flag'>{h(f)}</li>" for f in d["failures"]) + "</ul>")
    rows = []
    for c in d["controls"]:
        exp = ", ".join(f"{h(k.replace('expected_', ''))}={h(val)}" for k, val in (c.get("expected") or {}).items()) or "—"
        result = ("<span class='muted'>not run</span>" if not c["ran"] else
                  f"{outcome_chip(c.get('outcome'), c.get('success'))} reward {h(c.get('reward'))} · success {h(c.get('success'))}")
        verdict = chip("failed", "critical", "; ".join(c["failed"])) if c["failed"] else (chip("as expected", "good") if c["ran"] else chip("pending", "pending"))
        rows.append(f"<tr><td class='mono'>{h(c['name'])}</td><td>{chip(c['cls'], 'info')}</td><td class='mono small'>{dash(c.get('asset'))}</td>"
                    f"<td class='small'>{exp}</td><td>{result}</td><td>{verdict}</td><td class='small muted'>{dash(c.get('rationale'))}</td></tr>")
    controls.append("<div class='table-wrap'><table><thead><tr><th>control</th><th>class</th><th>asset</th><th>expected</th><th>result</th><th>verdict</th><th>rationale</th></tr></thead>"
                    f"<tbody>{''.join(rows)}</tbody></table></div>")
    jud = ""
    if judge:
        gaps = judge.get("gaps") or []
        gap_items = "".join(f"<li>{chip(g.get('category'), 'info')} {h(g.get('text'))} <span class='muted'>· consequential {h(g.get('consequential'))} · "
                            f"derivable {h(g.get('derivable_from_repo'))} · resolved by {h(g.get('resolved_by'))}</span></li>" for g in gaps)
        jud = (f"<h2>Judge</h2><p class='lead'>{chip('keep' if judge.get('keep') else 'reject', 'good' if judge.get('keep') else 'critical')} rubric "
               f"{h(judge.get('verified_rubric_score'))}/3 · complexity {h(judge.get('complexity'))} · test behavioralness {h(judge.get('test_behavioralness'))} · "
               f"self-contained {h(judge.get('self_contained'))} · alternatives accepted {h(judge.get('alternatives_accepted'))}"
               + (f" · <span class='muted'>{h(judge.get('model_id'))}</span>" if judge.get("model_id") else "") + "</p>"
               + (f"<p class='muted small'>{h(judge.get('rationale'))}</p>" if judge.get("rationale") else "")
               + (f"<details><summary>{len(gaps)} gap(s)</summary><ul class='small'>{gap_items}</ul></details>" if gaps else ""))
    env = d.get("environment") or {}
    contract = d.get("contract") or {}
    tdirs = b.get("task_dirs") or {}
    groups_text = ", ".join(f"{h(g.get('id'))} ({h(g.get('weight'))})" for g in d.get("groups") or []) or "—"
    links = ["<h2>Environment and links</h2><dl class='kv'>",
             f"<dt>image</dt><dd class='mono'>{dash(env.get('image'))}</dd><dt>repo</dt><dd class='mono'>{dash(env.get('repo_url'))} @ {h(short(env.get('base_commit'), 12))}</dd>",
             f"<dt>fail_to_pass</dt><dd>{len(contract.get('fail_to_pass') or [])} test(s)</dd><dt>pass_to_pass</dt><dd>{len(contract.get('pass_to_pass') or [])} test(s)</dd>",
             f"<dt>requirements</dt><dd>{len(d.get('requirements') or [])}</dd><dt>groups</dt><dd>{groups_text}</dd>",
             f"<dt>persona</dt><dd>{dash((d.get('persona') or {}).get('summary'))}</dd>"]
    for cond, path in sorted(tdirs.items()):
        links.append(f"<dt>task dir · {h(cond)}</dt><dd class='mono'>{h(path)}</dd>")
    links.append("</dl><p class='small'>" + " · ".join(
        f"<a href='/scenario/{h(sid)}/file/{h(n)}{'?evaluator=1' if n in SCENARIO_EVALUATOR_FILES else ''}'>{h(n)}</a>"
        + (" <span class='muted'>(evaluator-only)</span>" if n in SCENARIO_EVALUATOR_FILES else "")
        for n in b.get("files") or [] if on or n not in SCENARIO_EVALUATOR_FILES) + "</p>")
    return {"head": "".join(head), "briefs": briefs, "ledger": "".join(ledger), "controls": "".join(controls), "judge": jud,
            "links": "".join(links), "exists": True}


def scenario_page(d: dict) -> str:
    sid = d["board"]["id"]
    sec = scenario_page_sections(d)
    body = sec["head"] + sec["briefs"] + sec["ledger"] + sec["controls"] + sec["judge"] + sec["links"]
    return layout(f"swesim · {sid}", body, "scenarios", "scenario")
