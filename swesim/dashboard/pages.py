"""Record pages: Home, Trials, Candidates, Workflows, Leaderboard, Database browser, Artifact browser, Spend, Search.

Every list, count and score on these pages comes from the IndexReader rows handed in by ``app.py``; the artifact
browser is the one page that walks the filesystem (``Layout`` + ``os.scandir``, paged). Every number links to the
artifact it came from. Charts follow ``charts.py`` (one axis, legend + direct labels, table twin).
"""
from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

from swesim.dashboard import charts
from swesim.dashboard.render import (
    Column, chip, code_block, dash, difficulty_chip, fmt_usd, h, hero, ident, kv, model_chip, table, tier_of, tile, tiles,
)

# ----------------------------------------------------------------------------- columns

TRIAL_COLUMNS = (
    Column("trial_id", "trial", "id", href="/trial/{trial_id}"),
    Column("scenario_id", "scenario", "text", href="/scenario/{scenario_id}", filterable=True),
    Column("version", "v", "num"),
    Column("model", "model", "model", filterable=True),
    Column("condition", "condition", "text", filterable=True),
    Column("brief_profile", "brief", "text", filterable=True),
    Column("purpose", "purpose", "text", filterable=True),
    Column("status", "status", "chip", filterable=True),
    Column("outcome", "outcome", "chip", title="outcome_detail"),
    Column("reward", "reward", "metric"),
    Column("questions_asked", "asked", "num"),
    Column("confirmations", "confirmed", "num"),
    Column("cost_usd", "cost", "usd"),
    Column("origin", "origin", "text", filterable=True),
    Column("started_at", "started", "time"),
    Column("attempt_index", "#", "num"),
)
WORKFLOW_COLUMNS = (
    Column("workflow_id", "workflow", "id", href="/workflows/{workflow_id}", n=28, filterable=True),
    Column("type", "type", "text", filterable=True),
    Column("status", "status", "chip", filterable=True),
    Column("summary", "params", "text"),
    Column("failure", "failure", "text"),
    Column("started_at", "started", "time"),
    Column("closed_at", "closed", "time"),
    Column("cost_usd", "cost", "usd"),
    Column("temporal", "Temporal", "link", href="{temporal_url}"),
)
CANDIDATE_COLUMNS = (
    Column("instance_id", "candidate", "mono", filterable=True),
    Column("source_key", "source key", "text", filterable=True),
    Column("repo", "repo", "text"),
    Column("pr_kind", "band", "text"),
    Column("stage", "stage", "chip", filterable=True),
    Column("status", "status", "chip", filterable=True),
    Column("gate", "gate", "text", filterable=True),
    Column("reasons", "reason", "list"),
    Column("scenario_id", "scenario", "text", href="/scenario/{scenario_id}"),
    Column("updated_at", "updated", "time"),
    Column("retry", "", "link", href="#"),
)


def trial_view(r, extras: dict | None = None) -> dict:
    d = r.model_dump(mode="json")
    ex = extras or {}
    d["outcome"] = ex.get("outcome") or (None if r.outcome_code is None else str(r.outcome_code))
    d["outcome_detail"] = (f"error {ex['error_type']} · from {ex.get('source_file')}" if ex.get("error_type") else (f"from {ex['source_file']}" if ex.get("source_file") else None))
    if not d["outcome"] and r.status not in ("completed", "running", "launched", "planned"):
        d["outcome"] = "unresolved"
    return d


def workflow_view(w: dict, settings) -> dict:
    d = dict(w)
    d["temporal_url"] = settings.workflow_url(w.get("workflow_id", ""), w.get("run_id")) or ""
    d["temporal"] = "open ↗" if d["temporal_url"] else None
    if isinstance(w.get("params_summary"), dict) and not w.get("summary"):
        d["summary"] = ", ".join(f"{k}={','.join(map(str, v)) if isinstance(v, (list, tuple)) else v}" for k, v in w["params_summary"].items())
    return d


# ----------------------------------------------------------------------------- home

def home_body(data: dict, settings) -> str:
    """``data``: what app.py assembles for /api/home (tiles, running, recent, workflows, funnel, spend)."""
    t = data["tiles"]
    cap = settings.spend_cap_usd
    spend_note = f"cap ${cap:,.2f}" if cap is not None else "no cap configured (SWESIM_SPEND_CAP_USD)"
    body = ["<h1>Home</h1><p class='lead'>What is running and what changed. Counts come from the index; every number links to its list.</p>",
            tiles([tile("running trials", t["trials_running"], href="/trials?status=running", key="trials_running", kind="run"),
                   tile("completed trials", t["trials_completed"], href="/trials?status=completed", key="trials_completed"),
                   tile("unresolved / failed", t["trials_unresolved"], href="/trials?status=unresolved", key="trials_unresolved", kind="critical" if t["trials_unresolved"] else ""),
                   tile("valid scenarios", t["scenarios_valid"], href="/scenarios?validation_status=valid", key="scenarios_valid"),
                   tile("awaiting review", t["review_pending"], href="/review", key="review_pending", kind="run" if t["review_pending"] else ""),
                   tile("candidates pending", t["candidates_pending"], href="/candidates?status=pending", key="candidates_pending"),
                   tile("spend (recorded)", f"${t['spend_usd']:,.2f}", href="/spend", key="spend_text", note=spend_note),
                   tile("workflows running", t["workflows_running"], href="/workflows?status=running", key="workflows_running")])]
    body.append("<h2>Running now</h2>")
    body.append(table("running", TRIAL_COLUMNS, data["running"], key="trial_id",
                      empty="<strong>Nothing is running.</strong> Start a run on <a href='/run'>Run</a>; rows appear here within 5 s of launch.", export=False))
    body.append("<div class='side-by-side'><div><h2>Recent workflows</h2>" + table("workflows", WORKFLOW_COLUMNS[:7], data["workflows"], key="workflow_id",
                                                                                  empty="<strong>No workflows listed.</strong> " + h(data.get("workflow_error") or "Start one on Create or Run."), export=False)
                + "</div><div><h2>Creation funnel</h2>" + charts.funnel(data["funnel"], "chart-home-funnel") + "</div></div>")
    body.append("<h2>Recently finished</h2>")
    body.append(table("recent", TRIAL_COLUMNS, data["recent"], key="trial_id", empty="<strong>No finished trials yet.</strong>", export=False))
    return "".join(body)


# ----------------------------------------------------------------------------- trials

def _filter_select(name: str, values: list, current: list | None, label: str) -> str:
    opts = "".join(f"<option value='{h(v)}'{' selected' if current and str(v) in current else ''}>{h(v)}</option>" for v in values)
    return f"<label>{h(label)}<select name='{h(name)}'><option value=''>all</option>{opts}</select></label>"


def trials_body(page, views: list[dict], facets: dict, current: dict, unprojected: list[dict]) -> str:
    form = ["<form class='toolbar' method='get' action='/trials'>",
            _filter_select("scenario_id", facets["scenario_id"], current.get("scenario_id"), "scenario"),
            _filter_select("version", facets["version"], current.get("version"), "version"),
            _filter_select("model", facets["model"], current.get("model"), "model"),
            _filter_select("condition", ["full", "hidden", "interactive"], current.get("condition"), "condition"),
            _filter_select("brief_profile", ["natural", "withheld"], current.get("brief_profile"), "brief profile"),
            _filter_select("status", facets["status"], current.get("status"), "status"),
            _filter_select("outcome_code", facets["outcome_code"], current.get("outcome_code"), "outcome code"),
            _filter_select("purpose", ["calibration", "comparison", "demonstration", "screen"], current.get("purpose"), "purpose"),
            _filter_select("origin", facets["origin"], current.get("origin"), "origin"),
            f"<label>since<input type='date' name='since' value='{h(current.get('since') or '')}'></label>",
            f"<label>until<input type='date' name='until' value='{h(current.get('until') or '')}'></label>",
            "<button type='submit' class='button'>Filter</button> <a class='button' href='/trials'>Reset</a></form>"]
    body = ["<h1>Trials</h1><p class='lead'>Every trial record and every planned, launched, unresolved or launch-failed attempt. Unresolved attempts never enter n or pass@k.</p>",
            "".join(form),
            table("trials", TRIAL_COLUMNS, views, key="trial_id", total=page.total, offset=page.offset, limit=page.limit,
                  empty="<strong>No trials match.</strong> Reset the filters, or start a run on <a href='/run'>Run</a>.")]
    if unprojected:
        items = "".join(f"<li><a class='mono' href='/trial/{h(u['trial_id'])}'>{h(u['trial_id'])}</a> · status {h(u['status'])} · missing {h(', '.join(u['missing']))} · "
                        f"<a href='/artifacts?path=trials/{h(u['trial_id'])}'>files</a></li>" for u in unprojected)
        body.append(f"<h2>Attempt directories without lineage</h2><p class='muted small'>{len(unprojected)} directory(ies) exist under <code>trials/</code> but carry no condition/model in any file, "
                    f"so they cannot be projected into the trials table. They are listed here rather than dropped.</p><ul class='small'>{items}</ul>")
    return "".join(body)


# ----------------------------------------------------------------------------- candidates

def candidates_body(page, views: list[dict], funnel_steps: list[dict], hist: dict[str, int], source_keys: list[str], current_key: str | None) -> str:
    sel = "".join(f"<option value='{h(k)}'{' selected' if k == current_key else ''}>{h(k)}</option>" for k in source_keys)
    body = ["<h1>Candidates</h1><p class='lead'>Where the creation funnel loses candidates, and why. Mining-gate rejects (no linked issue, diff size, thread depth) never became rows; "
            "they are counted at the <em>mined</em> stage from <code>&lt;key&gt;.rejects.json</code>.</p>",
            f"<form class='toolbar' method='get' action='/candidates'><label>source key<select name='source_key'><option value=''>all sources</option>{sel}</select></label>"
            "<button type='submit' class='button'>Filter</button></form>",
            "<div class='side-by-side'><div>", charts.funnel(funnel_steps, "chart-funnel"), "</div><div>",
            charts.hbars(list(hist.items()), "chart-rejects", "rejects by gate"), "</div></div>",
            "<h2>Every candidate</h2>",
            table("candidates", CANDIDATE_COLUMNS, views, key="instance_id", total=page.total, offset=page.offset, limit=page.limit,
                  empty="<strong>No candidate rows.</strong> A creation job's mining step writes <code>candidates/&lt;key&gt;.jsonl</code>; judged and probed rows follow.")]
    return "".join(body)


def candidate_view(r) -> dict:
    d = r.model_dump(mode="json")
    d["retry"] = "retry…" if r.status in ("rejected", "error") else None
    return d


# ----------------------------------------------------------------------------- workflows

def workflows_body(views: list[dict], error: str | None, total: int, settings) -> str:
    body = ["<h1>Workflows</h1><p class='lead'>What Temporal did: every execution with type, status (including failed, cancelled, terminated and their failure text), "
            "params summary, cost and a deep link to the Temporal UI for the event history. This page never duplicates the history itself.</p>"]
    if error:
        body.append(f"<div class='banner warn' role='status'><strong>Live listing unavailable.</strong> {h(error)} Rows below come from the index only.</div>")
    body.append(table("workflows", WORKFLOW_COLUMNS, views, key="workflow_id", total=total, source="/api/workflows",
                      empty="<strong>No workflows.</strong> Start one on <a href='/create'>Create</a> or <a href='/run'>Run</a>."))
    return "".join(body)


def workflow_body(wid: str, row, job: dict | None, progress: dict | None, error: str | None, children: list[dict], trials: list[dict], settings, redact) -> str:
    st = (row.status if row else (job or {}).get("status")) or "unknown"
    url = settings.workflow_url(wid, (row.run_id if row else (job or {}).get("run_id")))
    body = [f"<h1>Workflow {ident(wid, 40)}</h1>",
            f"<p class='lead'>{chip(st)} {dash((row.type if row else (job or {}).get('type')))} · started {dash(row.started_at.isoformat() if row and row.started_at else (job or {}).get('started_at'))}"
            + (f" · closed {dash(row.closed_at.isoformat() if row and row.closed_at else (job or {}).get('closed_at'))}" if (row and row.closed_at) or (job or {}).get("closed_at") else "")
            + (f" · <a class='ext' href='{h(url)}' target='_blank' rel='noopener'>event history in Temporal ↗</a>" if url else " · <span class='muted'>no Temporal UI configured</span>") + "</p>",
            "<div id='banner' class='banner hidden' role='status'></div>"]
    failure = (row.failure if row else None) or (job or {}).get("failure")
    if failure:
        body.append(f"<div class='banner error'><strong>Failure</strong><pre class='code'>{h(redact(failure))}</pre></div>")
    params = (row.params_summary if row else None) or (job or {}).get("params_summary") or {}
    if params:
        body.append("<h2>Parameters</h2>" + kv([(k, f"<span class='mono'>{h(', '.join(map(str, v)) if isinstance(v, (list, tuple)) else v)}</span>") for k, v in params.items()]))
    if row and row.cost_usd is not None:
        body.append(f"<p>Cost recorded against this workflow: {fmt_usd(row.cost_usd)} · <a href='/spend?workflow_id={h(wid)}'>ledger</a></p>")
    if error:
        body.append(f"<div class='banner warn'><strong>Progress query unavailable.</strong> {h(error)}</div>")
    if progress:
        stage = progress.get("stage") or progress.get("status")
        body.append(f"<h2>Progress</h2><p>Stage {chip(stage)}" + (f" · scenario <a href='/scenario/{h(progress['scenario_id'])}'>{h(progress['scenario_id'])}</a>" if progress.get("scenario_id") else "") + "</p>")
        if progress.get("needs_human"):
            body.append(f"<p class='flag'>needs a person: {h(json.dumps(progress['needs_human']))}</p>")
        counts = progress.get("counts") if isinstance(progress.get("counts"), dict) else {k: progress[k] for k in ("tried", "completed", "rejected", "invalid") if k in progress}
        if counts:
            body.append("<p class='muted small'>" + " · ".join(f"{h(k)} {h(v)}" for k, v in counts.items()) + "</p>")
        body.append(f"<details><summary>raw progress (redacted)</summary><pre class='code'>{h(redact(json.dumps(progress, indent=2, default=str))[:20000])}</pre></details>")
    if children:
        body.append("<h2>Children</h2>" + table("children", WORKFLOW_COLUMNS[:7], children, key="workflow_id", empty="none", export=False))
    if trials:
        body.append("<h2>Trials launched by this workflow</h2>" + table("wf-trials", TRIAL_COLUMNS, trials, key="trial_id", empty="none", export=False))
    if st in ("running", "open", "unknown", "continued_as_new"):
        body.append(f"<div class='card'><strong>Cancel</strong> <span class='muted small'>Requests cancellation; children are cancelled with the parent. A running trial finishes on its own and is never collected afterwards.</span>"
                    f"<div class='actions'><button type='button' class='button danger' data-action='cancel' data-params='{h(json.dumps({'workflow_id': wid}))}' data-status='#action-status'>Cancel workflow…</button>"
                    f"<span id='action-status' class='muted small'></span></div></div>")
    return "".join(body)


# ----------------------------------------------------------------------------- leaderboard

LB_COLUMNS = (
    Column("model", "model", "model"),
    Column("condition", "condition", "text"),
    Column("brief_profile", "brief", "text"),
    Column("n", "n", "num"),
    Column("scenarios", "scenarios", "num"),
    Column("success_count", "solved", "num"),
    Column("pass_rate", "pass rate", "metric"),
    Column("pass_at_k", "pass@k", "metric"),
    Column("pass_pow_k", "pass^k", "metric"),
    Column("mean_reward", "mean reward", "metric"),
    Column("cost_per_success", "cost / solved", "usd"),
    Column("questions_asked", "questions", "num"),
    Column("confirmations", "confirmations", "num"),
    Column("asked_first_rate", "asked-first rate", "metric"),
    Column("confidence_note", "confidence", "text"),
)


def leaderboard_body(views: list[dict], gate: list[dict], current: dict, matrix_html: str, chart_html: str, saturated_views: list[dict]) -> str:
    eligible = [g for g in gate if g["eligible"]]
    checks = "".join(f"<tr><td><a href='/scenario/{h(g['scenario_id'])}'>{h(g['scenario_id'])}</a> <span class='mono muted'>v{h(g['version'])}</span></td><td>{chip(g['validation_status'])}</td>"
                     f"<td>{difficulty_chip(g['difficulty_tier'])}</td>"
                     f"<td>{chip('coherent + fair', 'good') if g['checks']['task_sense'] else chip('not audited' if not g['task_sense'] else 'fails audit', 'neutral' if not g['task_sense'] else 'critical')}</td>"
                     f"<td>{chip('on board', 'good') if g['eligible'] else chip('excluded', 'neutral')}</td></tr>" for g in gate)
    form = ("<form class='toolbar' method='get' action='/leaderboard'>"
            + _filter_select("purpose", ["comparison", "calibration", "demonstration", "screen"], [current.get("purpose") or "comparison"], "purpose (never pooled)")
            + _filter_select("brief_profile", ["natural", "withheld"], current.get("brief_profile"), "brief profile")
            + _filter_select("source", ["github-repo", "swebench-annotated", "authored"], current.get("source"), "scenario source")
            + _filter_select("tier", ["A", "B", "C", "D", "E"], current.get("tier"), "model tier")
            + _filter_select("condition", ["full", "hidden", "interactive"], current.get("condition"), "condition")
            + f"<label class='inline'><input type='checkbox' name='include_saturated' value='1'{' checked' if current.get('include_saturated') else ''}> include saturated scenarios</label>"
            "<button type='submit' class='button'>Apply</button> <a class='button' href='/leaderboard'>Reset</a></form>")
    body = ["<h1>Leaderboard</h1><p class='lead'>How do models compare on informative tasks? Only scenarios that are valid, labelled <em>informative</em> by the difficulty screen and "
            "pass the task-sense audit (coherent, fair tests) enter the board; saturated scenarios are excluded by default; broken never appear. Nothing is shown without n; "
            "purposes are never pooled.</p>",
            hero(f"{len(eligible)} / {len(gate)}", "scenarios on the board (eligible / current versions)", "/scenarios"), form,
            table("leaderboard", LB_COLUMNS, views, key="key", total=len(views), empty="<strong>No cells with n ≥ 1 on eligible scenarios.</strong> The gate table below says why each scenario is excluded; "
                                                                                       "run the difficulty screen (<a href='/run'>Run</a>, purpose = screen) and the task-sense audit to admit scenarios.", export=True),
            "<h2>Per-scenario matrix</h2>", matrix_html, "<h2>Pass rate by condition</h2>", chart_html]
    if saturated_views:
        body.append("<h2>Saturated scenarios (shown separately)</h2>" + table("saturated", LB_COLUMNS, saturated_views, key="key", empty="none", export=False))
    body.append("<h2>Gate</h2><p class='muted small'>Interfaces §9.1: valid · difficulty informative (saturated only with the toggle; broken never) · task-sense coherent and fair_tests.</p>"
                f"<div class='table-wrap'><table><thead><tr><th>scenario</th><th>validation</th><th>difficulty</th><th>task sense</th><th>board</th></tr></thead><tbody>{checks}</tbody></table></div>")
    return "".join(body)


# ----------------------------------------------------------------------------- database browser

def db_index_body(tables: list[str], counts: dict[str, int], rebuilds: list, index_enabled: bool) -> str:
    rows = "".join(f"<tr><td><a href='/db/{h(t)}'>{h(t)}</a></td><td class='num'>{h(counts.get(t, '—'))}</td><td><a class='button small' href='/api/db/{h(t)}.csv'>CSV</a></td></tr>" for t in tables)
    last = rebuilds[-1] if rebuilds else None
    reb = (f"<p>Last index rebuild <span class='mono'>{h(last.rebuild_id)}</span> started {h(last.started_at.isoformat())}"
           + (f", finished {h(last.finished_at.isoformat())}" if last.finished_at else " (running)") + f" · counts {h(json.dumps(last.counts))}"
           + (f"<details><summary>diff against the files</summary><pre class='code'>{h(json.dumps(last.diff, indent=2))}</pre></details>" if last.diff else "") + "</p>") if last else \
        ("<p class='muted'>No rebuild recorded" + (" (the index is disabled: rows are projected from the files on each request)." if not index_enabled else ".") + "</p>")
    action = ("<div class='card'><strong>Rebuild the index from the files</strong> <span class='muted small'>Projects every record file into rows and reports the diff against the previous rows.</span>"
              "<div class='actions'><button type='button' class='button' data-action='rebuild' data-params='{}' data-status='#action-status'>Rebuild…</button><span id='action-status' class='muted small'></span></div></div>"
              "<div id='banner' class='banner hidden' role='status'></div>")
    return ("<h1>Database</h1><p class='lead'>Every table of the index (interfaces §2), browsable with filters, sorting, paging and CSV export. "
            + ("Source: Postgres." if index_enabled else "Source: the files, projected on demand (index disabled).") + "</p>" + reb
            + f"<div class='table-wrap'><table><thead><tr><th>table</th><th class='num'>rows</th><th>export</th></tr></thead><tbody>{rows}</tbody></table></div>" + action)


def db_table_body(name: str, page, columns: list[str], where: dict, q: str | None, order_by: str | None) -> str:
    cols = tuple(Column(c, c, "num" if page.items and isinstance(page.items[0].get(c), (int, float)) and not isinstance(page.items[0].get(c), bool) else "text", filterable=False) for c in columns)
    rows = []
    for r in page.items:
        rows.append({c: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for c, v in r.items()})
    wh = "".join(f"<input type='hidden' name='where.{h(k)}' value='{h(v)}'>" for k, v in where.items())
    form = (f"<form class='toolbar' method='get' action='/db/{h(name)}'>{wh}<label>free text<input type='search' name='q' value='{h(q or '')}'></label>"
            f"<label>order by<input name='order_by' value='{h(order_by or '')}' placeholder='-updated_at'></label>"
            f"<label>where column<input name='where_col' placeholder='status'></label><label>equals<input name='where_val' placeholder='valid'></label>"
            "<button type='submit' class='button'>Apply</button> <a class='button' href='/db/" + h(name) + "'>Reset</a></form>")
    active = (" · where " + ", ".join(f"<code>{h(k)}={h(v)}</code>" for k, v in where.items())) if where else ""
    return (f"<h1>Table <span class='mono'>{h(name)}</span></h1><p class='lead'>{page.total} row(s){active}. <a class='button small' href='/api/db/{h(name)}.csv'>CSV (filtered)</a></p>" + form
            + table(f"db-{name}", cols, rows, key=columns[0] if columns else "id", total=page.total, offset=page.offset, limit=page.limit,
                    empty="<strong>No rows.</strong>", dense=True))


# ----------------------------------------------------------------------------- artifact browser

TEXT_PREVIEW = (".json", ".diff", ".patch", ".log", ".toml", ".md", ".txt", ".jsonl", ".status", ".cfg", ".ini", ".yaml", ".yml")
TOP_LEVEL = ("scenarios", "tasks", "trials", "exports", "evidence", "candidates", "calibration", "jobs", "probes")
PAGE_SIZE = 200


def _human(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def is_listable(rel: Path, evaluator: bool) -> tuple[bool, str]:
    """Never list a per-trial task copy (session token); evaluator-only paths need an evaluator identity."""
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == "trials" and parts[2] == "task":
        return False, "the per-trial task copy holds the session token and is never listed"
    if not evaluator:
        if len(parts) >= 3 and parts[0] == "scenarios" and (parts[2] == "assets" or parts[-1] in ("scenario.json", "scenario.draft.json", "judge.json", "draft.meta.json", "audit.json")):
            return False, "evaluator-only (author-only facts, hidden test assets, judge/draft metadata)"
        if len(parts) >= 5 and parts[0] == "scenarios" and parts[2] == "archive" and (parts[4] == "assets" or parts[-1] in ("scenario.json", "scenario.draft.json", "judge.json", "draft.meta.json", "audit.json")):
            return False, "evaluator-only (archived spec and metadata)"
        if len(parts) >= 3 and parts[0] == "trials" and parts[-1] in ("record.json", "transcript.json", "spec.json", "audit.json"):
            return False, "evaluator-only (raw record, transcript, frozen spec, audit)"
        if len(parts) >= 2 and parts[0] == "tasks":
            return False, "evaluator-only (compiled task directories carry hidden tests)"
        if len(parts) >= 2 and parts[0] == "exports":
            return False, "evaluator-only (episode exports carry the frozen spec)"
    return True, ""


def sha256_from_manifests(root: Path, rel: Path) -> str | None:
    """sha256 recorded by a manifest for this path, when one exists (scenario assets from scenario.json; compile manifests)."""
    parts = rel.parts
    if len(parts) >= 4 and parts[0] == "scenarios" and parts[2] == "assets":
        try:
            spec = json.loads((root / parts[0] / parts[1] / "scenario.json").read_text())
        except (OSError, ValueError):
            return None
        for a in (spec.get("assets") or {}).values():
            if isinstance(a, dict) and a.get("path") and Path(str(a["path"])).name == parts[-1]:
                return a.get("sha256")
    return None


def list_dir(root: Path, rel: Path, evaluator: bool, offset: int = 0) -> dict:
    """One directory level via os.scandir, paged; entries carry size, sha256 (from manifests), listability."""
    target = (root / rel).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise ValueError("path escapes the workspace")
    entries = []
    try:
        with os.scandir(target) as it:
            for e in it:
                r = rel / e.name
                ok, why = is_listable(r, evaluator)
                try:
                    st = e.stat(follow_symlinks=False)
                    size = None if e.is_dir(follow_symlinks=False) else st.st_size
                    mtime = st.st_mtime
                except OSError:
                    size, mtime = None, None
                entries.append({"name": e.name, "rel": r.as_posix(), "dir": e.is_dir(follow_symlinks=False), "size": size, "mtime": mtime, "listable": ok, "why": why,
                                "sha256": sha256_from_manifests(root, r) if ok and not e.is_dir(follow_symlinks=False) else None})
    except FileNotFoundError:
        return {"rel": rel.as_posix(), "entries": [], "total": 0, "offset": 0, "missing": True}
    entries.sort(key=lambda x: (not x["dir"], x["name"]))
    return {"rel": rel.as_posix(), "entries": entries[offset:offset + PAGE_SIZE], "total": len(entries), "offset": offset, "missing": False}


def artifacts_body(root: Path, listing: dict, preview: dict | None, rel: Path) -> str:
    crumbs = [f"<a href='/artifacts'>workspace</a>"]
    acc = Path()
    for part in rel.parts:
        acc = acc / part
        crumbs.append(f"<a href='/artifacts?path={h(acc.as_posix())}'>{h(part)}</a>")
    body = ["<h1>Artifacts</h1><p class='lead'>A tree over the record (scenarios, tasks, trials, exports, evidence, candidates). Sizes from the filesystem; sha256 from manifests where one exists; "
            "text preview for JSON, diff, log and TOML files. Evaluator-only paths are gated by identity; the per-trial task copy is never listed.</p>",
            "<p class='mono small'>" + " / ".join(crumbs) + "</p>"]
    if not rel.parts:
        items = "".join(f"<div class='file'><span class='name'><a href='/artifacts?path={h(t)}'>{h(t)}/</a></span><span class='size'></span><span></span><span></span></div>" for t in TOP_LEVEL if (root / t).is_dir())
        body.append(f"<div class='tree'>{items or '<p class=empty>Empty workspace.</p>'}</div>")
        return "".join(body)
    if listing.get("missing"):
        body.append("<p class='empty'>No such directory.</p>")
        return "".join(body)
    rows = []
    for e in listing["entries"]:
        if e["dir"]:
            link = f"<a href='/artifacts?path={h(e['rel'])}'>{h(e['name'])}/</a>" if e["listable"] else f"<span class='muted' title='{h(e['why'])}'>{h(e['name'])}/ (not listed)</span>"
            rows.append(f"<div class='file'><span class='name'>{link}</span><span class='size'></span><span></span><span></span></div>")
        else:
            if e["listable"]:
                link = f"<a href='/artifacts?path={h(e['rel'])}'>{h(e['name'])}</a>"
                dl = f"<a class='button small' href='/artifacts/download?path={h(e['rel'])}'>download</a>"
            else:
                link, dl = f"<span class='muted' title='{h(e['why'])}'>{h(e['name'])} (evaluator-only)</span>", ""
            sha = ident(e["sha256"], 16) if e["sha256"] else "<span class='muted small'>no manifest sha</span>"
            rows.append(f"<div class='file'><span class='name'>{link}</span><span class='size'>{h(_human(e['size']))}</span><span>{sha}</span><span>{dl}</span></div>")
    body.append(f"<div class='tree'>{''.join(rows)}</div>")
    if listing["total"] > PAGE_SIZE:
        off = listing["offset"]
        body.append(f"<nav class='paging'><a class='button small' href='/artifacts?path={h(rel.as_posix())}&offset={max(0, off - PAGE_SIZE)}'>‹ prev</a><span class='muted small'>{off + 1}–{min(listing['total'], off + PAGE_SIZE)} of {listing['total']}</span>"
                    f"<a class='button small' href='/artifacts?path={h(rel.as_posix())}&offset={off + PAGE_SIZE}'>next ›</a></nav>")
    if preview:
        body.append(f"<h2>Preview <span class='mono'>{h(preview['name'])}</span> <span class='muted small'>{h(_human(preview['size']))} · sha256 {ident(preview['sha256'], 16)}</span></h2>"
                    f"<p class='small'><a class='button small' href='/artifacts/download?path={h(preview['rel'])}'>download</a></p>"
                    f"<div class='preview'>{code_block(preview['text'], preview['lang'], diff=preview['lang'] == 'diff', max_lines=preview.get('max_lines'))}</div>")
    return "".join(body)


def preview_file(root: Path, rel: Path, max_bytes: int = 400_000) -> dict | None:
    p = (root / rel).resolve()
    if not str(p).startswith(str(root.resolve())) or not p.is_file():
        return None
    size = p.stat().st_size
    lang = "diff" if p.suffix in (".diff", ".patch") else p.suffix.lstrip(".")
    text = None
    if p.suffix.lower() in TEXT_PREVIEW:
        data = p.read_bytes()[:max_bytes]
        text = data.decode("utf-8", errors="replace")
        if p.suffix == ".json":
            try:
                text = json.dumps(json.loads(text), indent=2, sort_keys=True)
            except ValueError:
                pass
        if size > max_bytes:
            text += f"\n… truncated at {max_bytes} bytes; download for the full file"
    digest = hashlib.sha256(p.read_bytes()).hexdigest() if size <= 50_000_000 else None
    return {"name": p.name, "rel": rel.as_posix(), "size": size, "sha256": digest, "text": text if text is not None else "(binary or non-text file: download to inspect)", "lang": lang, "max_lines": 2000}


# ----------------------------------------------------------------------------- spend

SPEND_COLUMNS = (Column("provider", "provider", "text"), Column("model", "model", "model"), Column("usd", "USD", "usd"), Column("share_of_cap", "share of cap", "metric"),
                 Column("input_tokens", "input tokens", "num"), Column("output_tokens", "output tokens", "num"), Column("records", "records", "num"))
COST_COLUMNS = (Column("at", "at", "time"), Column("trial_id", "trial", "id", href="/trial/{trial_id}"), Column("workflow_id", "workflow", "id", href="/workflows/{workflow_id}", n=20),
                Column("role", "role", "text"), Column("model", "model", "model"), Column("input_tokens", "in", "num"), Column("output_tokens", "out", "num"),
                Column("usd", "USD", "usd"), Column("price_source", "price source", "text"))


def spend_body(spend_rows: list, costs: list, cap: Decimal | None, by_run: list[dict], pools: list[dict] | None) -> str:
    total = sum((r.usd for r in spend_rows), Decimal(0))
    pct = f"{(float(total) / float(cap) * 100):.1f}% of cap ${cap:,.2f}" if cap else "no cap configured"
    views = [r.model_dump(mode="json") for r in spend_rows]
    body = ["<h1>Spend</h1><p class='lead'>Recorded provider spend per provider and model (the live ledger from <code>costs</code>); per run below. "
            "Unknown prices are recorded with <code>price_source = unknown</code> and never guessed.</p>",
            hero(f"${total:,.2f}", f"recorded spend · {pct}", "/db/costs"),
            table("spend", SPEND_COLUMNS, views, key="model", empty="<strong>No costs recorded yet.</strong> Costs land in <code>trials/&lt;tid&gt;/costs.json</code> as trials complete.", export=True),
            "<h2>Per run (workflow)</h2>",
            table("spend-runs", (Column("workflow_id", "workflow", "id", href="/workflows/{workflow_id}", n=28), Column("usd", "USD", "usd"), Column("records", "records", "num")), by_run,
                  key="workflow_id", empty="No workflow-attributed costs.", export=False),
            "<h2>Ledger (latest 500)</h2>",
            table("costs", COST_COLUMNS, [c.model_dump(mode="json") for c in costs[:500]], key="cost_id", empty="No cost records.", export=True)]
    if pools is not None:
        body.append("<h2>Worker pool health</h2>" + (table("pools", (Column("queue", "queue", "text"), Column("pool", "pool", "text"), Column("state", "state", "chip"), Column("instances", "instances", "num")), pools,
                                                          key="queue", empty="No pool health in the index.", export=False)))
    return "".join(body)


# ----------------------------------------------------------------------------- search

def search_body(q: str, hits: dict) -> str:
    body = [f"<h1>Search <span class='mono'>{h(q)}</span></h1>"]
    if not q:
        body.append("<p class='empty'>Type a scenario id, trial id, workflow id or model id in the search box.</p>")
        return "".join(body)
    sections = (("Scenarios", hits.get("scenarios") or [], lambda s: f"<a href='/scenario/{h(s.scenario_id)}'>{h(s.scenario_id)}</a> v{h(s.version)} {chip(s.validation_status)}"),
                ("Trials", hits.get("trials") or [], lambda t: f"<a class='mono' href='/trial/{h(t.trial_id)}'>{h(t.trial_id)}</a> · {h(t.scenario_id)} · {h(t.condition)} · {model_chip(t.model)} {chip(t.status)}"),
                ("Workflows", hits.get("workflows") or [], lambda w: f"<a class='mono' href='/workflows/{h(w['workflow_id'])}'>{h(w['workflow_id'])}</a> · {h(w.get('type'))} {chip(w.get('status'))}"),
                ("Models", hits.get("models") or [], lambda m: f"{model_chip(m)} · <a href='/trials?model={h(m)}'>trials</a> · <a href='/leaderboard'>leaderboard</a>"))
    any_hit = False
    for name, items, fmt in sections:
        if items:
            any_hit = True
            body.append(f"<h2>{name} <span class='muted small'>{len(items)}</span></h2><ul>" + "".join(f"<li>{fmt(i)}</li>" for i in items[:50]) + "</ul>")
    if not any_hit:
        body.append("<p class='empty'>No match in scenarios, trials, workflows or model ids.</p>")
    return "".join(body)


def tier_facets(models: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in models:
        out.setdefault(tier_of(m) or "other", []).append(m)
    return out
