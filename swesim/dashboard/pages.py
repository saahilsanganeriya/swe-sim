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

from swesim.dashboard import charts, origin
from swesim.index import reports
from swesim.dashboard.public import project
from urllib.parse import urlencode
from swesim.stages.score import OUTCOME_NAMES
from swesim.dashboard.render import (
    Column, chip, code_block, dash, difficulty_chip, fmt_usd, h, hero, ident, kv, model_chip, table, tier_of, tile, tiles, when,
)

# ----------------------------------------------------------------------------- columns

TRIAL_COLUMNS = (
    Column("trial_id", "trial", "id", href="/trial/{trial_id}"),
    Column("scenario_id", "scenario", "text", href="/scenario/{scenario_id}", filterable=True),
    Column("origin_kind", "task origin", "chip", title="scenario_source", filterable=True),
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
    Column("workflow_id", "workflow", "id", href="/workflows/{workflow_id}", n=16),
    Column("started_at", "started", "time"),
    Column("attempt_index", "#", "num"),
)
WORKFLOW_COLUMNS = (
    Column("workflow_id", "workflow", "id", href="{console_url}", n=28, filterable=True),
    Column("run_id", "execution", "id", n=12),
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
    Column("origin_kind", "task origin", "chip", title="source", filterable=True),
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


def trial_view(r, source: str | None = None) -> dict:
    """A TrialRow as the table/JSON row: adds the outcome name (score.OUTCOME_NAMES), a hover detail and, from the scenario's
    ``source`` (a TrialRow does not carry it; ``app.trial_views`` looks it up), the task-origin badge."""
    d = r.model_dump(mode="json")
    d["scenario_source"] = source
    d["origin_kind"] = origin.badge_label(source)
    d["outcome"] = OUTCOME_NAMES.get(r.outcome_code) if r.outcome_code is not None else None
    d["outcome_detail"] = f"outcome code {r.outcome_code}" if r.outcome_code is not None else None
    if not d["outcome"] and r.status not in ("completed", "running", "launched", "planned"):
        d["outcome"] = "unresolved"
        d["outcome_detail"] = "ended without a verifier record; never counted in n or pass@k"
    return d


def reject_histogram(reader, rows, source_key: str | None) -> dict[str, int]:
    """Index projection of discovery and candidate rejects; older readers expose candidate gates only."""
    from collections import Counter
    read = getattr(reader, "reject_histogram", None)
    if callable(read):
        return read(source_key)
    out: Counter = Counter()
    for r in rows:
        if r.status == "rejected" and r.gate and (source_key is None or r.source_key == source_key):
            out[r.gate] += 1
    return dict(out.most_common())


def workflow_view(w: dict, settings) -> dict:
    d = dict(w)
    d["temporal_url"] = settings.workflow_url(w.get("workflow_id", ""), w.get("run_id")) or ""
    d["temporal"] = "open ↗" if d["temporal_url"] else None
    d["console_url"] = "/workflows/" + w.get("workflow_id", "") + ("?" + urlencode({"run_id": w["run_id"]}) if w.get("run_id") else "")
    d["execution_key"] = f"{w.get('workflow_id')}|{w.get('run_id', '')}"
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
    body.append("<div class='side-by-side'><div><h2>Recent workflows</h2>" + table("workflows", tuple(c for c in WORKFLOW_COLUMNS if c.key in ("workflow_id", "status", "temporal")), data["workflows"], key="execution_key",
                                                                                  empty="<strong>No workflows listed.</strong> " + h(data.get("workflow_error") or "Start one on Create or Run."), export=False)
                + "</div><div><h2>Creation funnel</h2><div id='home-charts' data-live-charts>" + charts.funnel(data["funnel"], "chart-home-funnel") + "</div></div></div>")
    body.append("<h2>Recently finished</h2>")
    body.append(table("recent", TRIAL_COLUMNS, data["recent"], key="trial_id", empty="<strong>No finished trials yet.</strong>", export=False))
    body.append(health_panel())
    return "".join(body)


def health_panel() -> str:
    """The page renders immediately; a bounded JSON request fills the observation independently of the index."""
    return ("<section class='card health-panel' data-health-source='/api/health'><h2>Worker queue polling</h2>"
            "<p class='lead'>Recent Temporal pollers show that a worker has contacted a queue. They do not measure backlog, available capacity, or successful execution.</p>"
            "<p data-health-state role='status'>Awaiting a queue observation…</p><p class='muted small' data-health-time>Not checked yet.</p>"
            "<div data-health-rows></div><p class='muted small'>Cloud Run resource status: not measured. Queue polling is observed separately from resource readiness.</p>"
            "<p class='small'><a href='/api/health'>Observation JSON</a> · <a href='/health'>Worker health</a></p></section>")


# ----------------------------------------------------------------------------- trials

def _filter_select(name: str, values: list, current: list | None, label: str) -> str:
    opts = "".join(f"<option value='{h(v)}'{' selected' if current and str(v) in current else ''}>{h(v)}</option>" for v in values)
    return f"<label>{h(label)}<select name='{h(name)}'><option value=''>all</option>{opts}</select></label>"


def trials_body(page, views: list[dict], facets: dict, current: dict, query=()) -> str:
    """``current['pool']`` is the task-origin facet; ``query`` the request's (key, value) pairs, carried by the facet bar, the
    filter form and the paging links."""
    query = list(query)
    facet = current.get("pool")
    form = ["<form class='toolbar' method='get' action='/trials'>", origin.hidden_input(facet),
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
            origin.facet_bar(facet, query, "/trials"), "".join(form),
            f"<p class='muted small' data-count-line>{page.total} attempt(s) match · showing {h(origin.subset_text(facet))}</p>",
            table("trials", TRIAL_COLUMNS, views, key="trial_id", total=page.total, offset=page.offset, limit=page.limit, source="/api/trials",
                  keep=origin.keep_query(query), empty="<strong>No trials match.</strong> Reset the filters, or start a run on <a href='/run'>Run</a>.")]
    return "".join(body)


# ----------------------------------------------------------------------------- candidates

# The §4 discovery sequence (PRs listed → merged in range → linked issue → band filters → retained) continues into the
# candidate stages of `FUNNEL_ORDER`; `retained` is the PR set that became `mined` candidate rows.
DISCOVERY_STAGES: tuple[str, ...] = ("listed", "merged-in-range", "linked-issue", "band-passed", "retained")
SOURCE_FUNNEL_COLUMNS = (
    Column("source_key", "source key", "text", href="/candidates?source_key={source_key}", filterable=True),
    Column("repo", "repo", "text", filterable=True),
    Column("band", "band", "text", filterable=True),
    Column("listed", "PRs listed", "num"),
    Column("merged-in-range", "merged in range", "num"),
    Column("linked-issue", "linked issue", "num"),
    Column("band-passed", "band passed", "num"),
    Column("retained", "retained", "num"),
    *(Column(stage, stage, "num") for stage in ("judged", "probed", "drafted", "approved", "compiled", "validated", "registered")),
    Column("rejected_prs", "PR rejects", "num"),
    Column("errors", "fetch errors", "num"),
    Column("discovered_at", "last scan", "time"),
    Column("scan", "scan record", "mono", href="/artifacts?path={scan_path}", title="scan_sha256"),
)
DECISION_COLUMNS = (
    Column("pr_number", "PR", "num", href="{pr_url}"),
    Column("stage", "stage reached", "text", filterable=True),
    Column("status", "decision", "chip", filterable=True),
    Column("reason", "reason", "text", filterable=True),
    Column("error_class", "error", "text"),
    Column("linked_issues", "linked issues", "list"),
)


def source_funnels(latest: dict[str, dict], candidates, keys: list[str] | None = None) -> list[dict]:
    """One row per source: the discovery stages of its newest scan (`observations.latest_scans`) followed by how many
    candidates reached each stage.

    A source without a scan record (discovery before scans were written) shows dashes for the discovery stages; its
    rejects histogram still counts. Candidate stage counts are `kept + rejected + error + pending` at each stage."""
    rows = list(candidates)
    keys = sorted(keys) if keys is not None else sorted({*latest, *(r.source_key for r in rows)})
    out = []
    for key in keys:
        disc = latest.get(key) or {}
        mine = [r for r in rows if r.source_key == key]
        reached = {s.stage: s.kept + s.rejected + s.error + s.pending for s in reports.funnel_steps(mine)}
        funnel = disc.get("funnel") or {}
        decisions = disc.get("decisions") or []
        source, _, slug = key.partition("--")
        stem, _, suffix = slug.rpartition("--")
        view = {"source_key": key, "repo": disc.get("repo") or (mine[0].repo if mine else None), "band": disc.get("band") or (suffix if suffix in ("bugfix", "feature") else None),
                **{stage: funnel.get(stage) for stage in DISCOVERY_STAGES},
                **{stage: reached.get(stage) for stage in ("judged", "probed", "drafted", "approved", "compiled", "validated", "registered")},
                "rejected_prs": sum(1 for d in decisions if d.get("status") == "rejected") if disc.get("scan_path") else None,
                "errors": sum(1 for d in decisions if d.get("status") == "error") if disc.get("scan_path") else None,
                "discovered_at": disc.get("discovered_at"), "scan": (disc.get("scan_sha256") or "")[:12] or None, "scan_sha256": disc.get("scan_sha256")}
        if disc.get("scan_path"):
            view["scan_path"] = disc["scan_path"]  # absent otherwise, so the column renders no link
        out.append(view)
    return out


def decision_views(discovery: dict | None) -> list[dict]:
    """Every PR decision of a discovery observation, newest funnel stage first, rejects and errors before retained."""
    order = {"error": 0, "rejected": 1, "pending": 2, "retained": 3}
    rows = [dict(d) for d in (discovery or {}).get("decisions") or []]
    return sorted(rows, key=lambda d: (order.get(d.get("status"), 9), -DISCOVERY_STAGES.index(d["stage"]) if d.get("stage") in DISCOVERY_STAGES else 0, d.get("pr_number") or 0))


def candidates_body(page, views: list[dict], funnel_steps: list[dict], hist: dict[str, int], source_keys: list[str], current_key: str | None,
                    funnels: list[dict] | None = None, current: dict | None = None) -> str:
    grouped_keys = {key: [k for k in source_keys if origin.of_source_key(k).key == key] for key, _label in origin.CANDIDATE_HEADINGS}
    sel = "".join(f"<optgroup label='{h(label)}'>" + "".join(f"<option value='{h(k)}'{' selected' if k == current_key else ''}>{h(k)}</option>" for k in grouped_keys[key]) + "</optgroup>"
                  for key, label in origin.CANDIDATE_HEADINGS if grouped_keys[key])
    funnel_rows = list(funnels or [])
    funnel_tables = []
    for key, label in origin.CANDIDATE_HEADINGS:
        mine = [r for r in funnel_rows if origin.of_source_key(r.get("source_key")).key == key]
        what = "repository scan" if key == "novel" else "SWE-bench pool A scan"
        funnel_tables.append(f"<h3>{h(label)} <span class='muted small'>{len(mine)} source{'s' if len(mine) != 1 else ''}</span></h3>"
                             + table(f"source-funnels-{key}", SOURCE_FUNNEL_COLUMNS, mine, key="source_key", export=bool(mine),
                                     empty=f"<strong>No {h(what)} recorded.</strong> A creation job's discovery step writes <code>candidates/&lt;key&gt;.rejects.json</code> "
                                           "and <code>candidates/&lt;key&gt;/scans/&lt;sha256&gt;.json</code>."))
    body = ["<h1>Candidates</h1><p class='lead'>Where the creation funnel loses candidates, and why. Every listed PR of a source scan keeps its decision: "
            "discovery-time rejects (not merged, no linked issue, diff size, thread depth) and the later candidate gates are projected from the index.</p>",
            f"<form class='toolbar' method='get' action='/candidates'><label>source key<select name='source_key'><option value=''>all sources</option>{sel}</select></label>"
            "<button type='submit' class='button'>Filter</button></form>",
            "<h2>Source funnels</h2><p class='muted small'>PRs listed → merged in range → linked issue → band filters → retained (= mined) → judged → probed → drafted → approved → compiled "
            "→ validated → registered, per source, from its newest scan. Discovery counts are decisions per source scan; overlapping sources can include the same PR. "
            "Dashes in the discovery stages mean the scan predates per-PR records. Sources are grouped by task origin: repositories (novel tasks) and SWE-bench pool A.</p>",
            *funnel_tables,
            "<div class='side-by-side' id='candidate-charts' data-live-charts>", charts.funnel(funnel_steps, "chart-funnel"),
            charts.hbars(list(hist.items()), "chart-rejects", "rejects by gate"), "</div>"]
    if current_key:
        decisions = decision_views(current)
        body.append(f"<h2>Every PR decision · <code>{h(current_key)}</code></h2>")
        if current and current.get("scan_path"):
            body.append(kv([("scanned", when(current.get("discovered_at"))), ("PRs listed", h(str((current.get("funnel") or {}).get("listed", len(decisions))))),
                            ("scan record", f"<a href='/artifacts?path={h(current['scan_path'])}'><code>{h(current['scan_path'])}</code></a>"),
                            ("sha256", f"<code>{h(current.get('scan_sha256'))}</code>"),
                            ("workflow", ident(current.get("workflow_id"), 24, "/workflows/" + str(current.get("workflow_id"))) if current.get("workflow_id") else "—")]))
        elif current:
            body.append("<p class='muted'>This source's newest scan predates per-PR records; only its reject histogram is indexed.</p>")
        body.append(table("decisions", DECISION_COLUMNS, decisions, key="pr_number", dense=True,
                          empty="<strong>No PR decisions recorded for this source.</strong> Scans made after 0005_discovery_integrity list every PR here."))
    else:
        body.append("<p class='muted small'>Select a source key to list every PR decision of its newest scan.</p>")
    body += ["<h2>Every candidate</h2>",
             table("candidates", CANDIDATE_COLUMNS, views, key="instance_id", total=page.total, offset=page.offset, limit=page.limit, source="/api/candidates",
                   empty="<strong>No candidate rows.</strong> A creation job's mining step writes <code>candidates/&lt;key&gt;.jsonl</code>; judged and probed rows follow.")]
    return "".join(body)


def candidate_view(r) -> dict:
    d = r.model_dump(mode="json")
    d["retry"] = "retry…" if r.status in ("rejected", "error") else None
    d["origin_kind"] = origin.badge_label(r.source)
    return d


# ----------------------------------------------------------------------------- workflows

def workflows_body(views: list[dict], error: str | None, total: int, settings) -> str:
    body = ["<h1>Workflows</h1><p class='lead'>What Temporal did: every execution with type, status (including failed, cancelled, terminated and their failure text), "
            "params summary, cost and a deep link to the Temporal UI for the event history. This page never duplicates the history itself.</p>"]
    if error:
        body.append(f"<div class='banner warn' role='status'><strong>Live listing unavailable.</strong> {h(error)} Rows below come from the index only.</div>")
    body.append(table("workflows", WORKFLOW_COLUMNS, views, key="execution_key", total=total, source="/api/workflows",
                      empty="<strong>No workflows.</strong> Start one on <a href='/create'>Create</a> or <a href='/run'>Run</a>."))
    return "".join(body)


def workflow_body(wid: str, row, job: dict | None, progress: dict | None, error: str | None, children: list[dict], trials: list[dict], settings, redact) -> str:
    st = (row.status if row else (job or {}).get("status")) or "unknown"
    url = settings.workflow_url(wid, (row.run_id if row else (job or {}).get("run_id")))
    body = [f"<h1>Workflow {ident(wid, 40)}</h1>",
            f"<p class='lead'>{chip(st)} {dash((row.type if row else (job or {}).get('type')))} · started {when(row.started_at if row and row.started_at else (job or {}).get('started_at'))}"
            + (f" · closed {when(row.closed_at if row and row.closed_at else (job or {}).get('closed_at'))}" if (row and row.closed_at) or (job or {}).get("closed_at") else "")
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
        body.append("<h2>Children</h2>" + table("children", WORKFLOW_COLUMNS, children, key="execution_key", empty="none", export=False))
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
    Column("cohort_label", "evaluation cohort", "text"),
    Column("requested_k", "k", "num"),
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
    Column("confidence", "95% CI", "mono", title="confidence_note"),
)


def cohort_select(current: dict) -> str:
    options = "<option value=''>All cohorts, shown separately</option>" + "".join(
        f"<option value='{h(key)}'{' selected' if key == current.get('evaluation_cohort') else ''}>{h(label)}</option>"
        for key, label in current.get('cohort_options', []))
    return f"<label>Evaluation cohort<select name='evaluation_cohort'>{options}</select></label>"


def leaderboard_body(views: list[dict], gate: list[dict], current: dict, matrix_html: str, chart_html: str, saturated_views: list[dict], query=()) -> str:
    """``current['pool']`` is the task-origin facet: the header names the subset shown; ``gate`` is already restricted to it."""
    query = list(query)
    facet = current.get("pool")
    subset = origin.subset_text(facet)
    eligible = [g for g in gate if g["eligible"]]
    checks = "".join(f"<tr><td><a href='/scenario/{h(g['scenario_id'])}'>{h(g['scenario_id'])}</a> {origin.badge(g.get('source'))} <span class='mono muted'>v{h(g['version'])}</span></td><td>{chip(g['validation_status'])}</td>"
                     f"<td>{difficulty_chip(g['difficulty_tier'])}</td>"
                     f"<td>{chip('coherent + fair', 'good') if g['checks']['task_sense'] else chip('not audited' if not g['task_sense'] else 'fails audit', 'neutral' if not g['task_sense'] else 'critical')}</td>"
                     f"<td>{chip('on board', 'good') if g['eligible'] else chip('excluded', 'neutral')}</td></tr>" for g in gate)
    form = ("<form class='toolbar' method='get' action='/leaderboard'>" + origin.hidden_input(facet)
            + cohort_select(current)
            + _filter_select("metric_k", list(range(1, 11)), [current.get("metric_k")] if current.get("metric_k") else [], "estimator k (default: recorded)")
            + _filter_select("purpose", ["comparison", "calibration", "demonstration", "screen"], [current.get("purpose") or "comparison"], "purpose (never pooled)")
            + _filter_select("brief_profile", ["natural", "withheld"], current.get("brief_profile"), "brief profile")
            + _filter_select("source", ["github-repo", "swebench-annotated", "authored"], current.get("source"), "scenario source")
            + _filter_select("tier", ["A", "B", "C", "D", "E"], current.get("tier"), "model tier")
            + _filter_select("difficulty_tier", ["informative", "saturated"], current.get("difficulty_tier"), "difficulty")
            + _filter_select("condition", ["full", "hidden", "interactive"], current.get("condition"), "condition")
            + f"<label class='inline'><input type='checkbox' name='include_saturated' value='1'{' checked' if current.get('include_saturated') else ''}> include saturated scenarios</label>"
            "<button type='submit' class='button'>Apply</button> <a class='button' href='/leaderboard'>Reset</a></form>")
    body = [f"<h1>Leaderboard <span class='muted' data-subset>· {h(subset)}</span></h1>",
            origin.facet_bar(facet, query, "/leaderboard"),
            f"<p class='lead'>How do models compare on informative tasks? This board shows <strong>{h(subset)}</strong>: the per-model cells, the per-scenario matrix and the gate below "
            "are restricted to that subset. Only scenarios that are valid, labelled <em>informative</em> by the difficulty screen and "
            "pass the task-sense audit (coherent, fair tests) enter the board; saturated scenarios are excluded by default; broken never appear. Nothing is shown without n; "
            "purposes are never pooled.</p>",
            hero(f"{len(eligible)} / {len(gate)}", f"scenarios on the board (eligible / current versions · {subset})", "/scenarios" + (f"?{origin.PARAM}={facet}" if facet else "")), form,
            "<p><a class='button' href='/trends'>Historical trends…</a></p>",
            "<div id='leaderboard-comparison' data-live-fragment>", leaderboard_comparison(views), "</div><h2>Scores and interaction details</h2>",
            table("leaderboard", LB_COLUMNS, views, key="key", total=len(views), source="/api/leaderboard", empty="<strong>No cells with n ≥ 1 on eligible scenarios.</strong> The gate table below says why each scenario is excluded; "
                                                                                       "run the difficulty screen (<a href='/run'>Run</a>, purpose = screen) and the task-sense audit to admit scenarios.", export=True),
            "<div id='leaderboard-charts' data-live-charts><h2>Per-scenario matrix</h2>", matrix_html, "<h2>Pass rate by condition</h2>", chart_html, "</div>"]
    if saturated_views:
        body.append("<h2>Saturated scenarios (shown separately)</h2>" + table("saturated", LB_COLUMNS, saturated_views, key="key", empty="none", export=False))
    body.append("<h2>Gate</h2><p class='muted small'>Interfaces §9.1: valid · difficulty informative (saturated only with the toggle; broken never) · task-sense coherent and fair_tests.</p>"
                f"<div class='table-wrap'><table><thead><tr><th>scenario</th><th>validation</th><th>difficulty</th><th>task sense</th><th>board</th></tr></thead><tbody>{checks}</tbody></table></div>")
    return "".join(body)


def leaderboard_comparison(views: list[dict]) -> str:
    """Models as rows, with separate condition groups and explicit denominators for every estimate."""
    conditions = ("full", "hidden", "interactive")
    grouped = {}
    for row in views:
        grouped.setdefault((row["model"], row["brief_profile"], row.get("evaluation_cohort", "unknown")), {})[row["condition"]] = row
    if not grouped:
        return ""
    header = "".join(f"<th scope='colgroup' colspan='3'>{condition}</th>" for condition in conditions)
    subheader = "".join("<th scope='col'>n</th><th scope='col'>pass@k</th><th scope='col'>pass^k</th>" for _ in conditions)
    body = []
    for (model, profile, cohort), cells in sorted(grouped.items()):
        row = [f"<tr><th scope='row'>{model_chip(model)}<div class='small muted'>{h(next(iter(cells.values())).get('cohort_label', cohort))}</div></th><td>{h(profile)}</td>"]
        for condition in conditions:
            cell = cells.get(condition)
            if cell is None:
                row.append("<td class='num'>0</td><td class='num'>—</td><td class='num'>—</td>")
                continue
            query = urlencode({"model": model, "condition": condition, "brief_profile": profile, "purpose": cell["purpose"], "evaluation_cohort": cohort, "status": "completed"})
            row.append(f"<td class='num'><a href='/leaderboard/evidence?{h(query)}'>{h(cell['n'])}</a></td>")
            row.extend(f"<td class='num' title='{h(cell['confidence_note'])}'>{dash(cell.get(key))}</td>" for key in ("pass_at_k", "pass_pow_k"))
        body.append("".join(row) + "</tr>")
    return ("<div class='table-wrap'><table id='condition-comparison'><caption>Models by condition; a dash means no matching score or insufficient attempts.</caption>"
            f"<thead><tr><th scope='col' rowspan='2'>model</th><th scope='col' rowspan='2'>brief</th>{header}</tr><tr>{subheader}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>")


# ----------------------------------------------------------------------------- database browser

def db_index_body(tables: list[str], counts: dict[str, int], rebuilds: list, index_enabled: bool) -> str:
    rows = "".join(f"<tr><td><a href='/db/{h(t)}'>{h(t)}</a></td><td class='num'>{h(counts.get(t, '—'))}</td><td><a class='button small' href='/api/db/{h(t)}.csv'>CSV</a></td></tr>" for t in tables)
    last = rebuilds[-1] if rebuilds else None
    reb = (f"<p>Last index rebuild <span class='mono'>{h(last.rebuild_id)}</span> started {when(last.started_at)}"
           + (f", finished {when(last.finished_at)}" if last.finished_at else " (running)") + f" · counts {h(json.dumps(last.counts))}"
           + (f"<details><summary>diff against the files</summary><pre class='code'>{h(json.dumps(last.diff, indent=2))}</pre></details>" if last.diff else "") + "</p>") if last else \
        ("<p class='muted'>No rebuild recorded" + (" (the index is disabled: rows are projected from the files on each request)." if not index_enabled else ".") + "</p>")
    action = ("<div class='card'><strong>Rebuild the index from the files</strong> <span class='muted small'>Projects every record file into rows and reports the diff against the previous rows.</span>"
              "<div class='actions'><button type='button' class='button' data-action='rebuild' data-params='{}' data-status='#action-status'>Rebuild…</button><span id='action-status' class='muted small'></span></div></div>"
              "<div id='banner' class='banner hidden' role='status'></div>")
    return ("<h1>Database</h1><p class='lead'>Every table of the index (interfaces §2), browsable with filters, sorting, paging and CSV export. "
            + ("Source: Postgres." if index_enabled else "Source: the files, projected on demand (index disabled).") + "</p>" + reb
            + f"<div class='table-wrap'><table><thead><tr><th>table</th><th class='num'>rows</th><th>export</th></tr></thead><tbody>{rows}</tbody></table></div>" + action)


def _db_kind(c: str, first: dict | None) -> str:
    if c == "at" or c.endswith("_at"):  # every timestamp column of the index (interfaces §2) is named this way
        return "time"
    v = (first or {}).get(c)
    return "num" if isinstance(v, (int, float)) and not isinstance(v, bool) else "text"


def db_table_body(name: str, page, columns: list[str], where: dict, q: str | None, order_by: str | None) -> str:
    cols = tuple(Column(c, c, _db_kind(c, page.items[0] if page.items else None), filterable=False) for c in columns)
    rows = []
    for r in page.items:
        rows.append({c: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for c, v in r.items()})
    wh = "".join(f"<input type='hidden' name='where.{h(k)}' value='{h(v)}'>" for k, v in where.items())
    form = (f"<form class='toolbar' method='get' action='/db/{h(name)}'>{wh}<label>free text<input type='search' name='q' value='{h(q or '')}'></label>"
            f"<label>order by<input name='order_by' value='{h(order_by or '')}' placeholder='-updated_at'></label>"
            f"<label>where column<input name='where_col' placeholder='status'></label><label>equals<input name='where_val' placeholder='valid'></label>"
            "<button type='submit' class='button'>Apply</button> <a class='button' href='/db/" + h(name) + "'>Reset</a></form>")
    active = (" · where " + ", ".join(f"<code>{h(k)}={h(v)}</code>" for k, v in where.items())) if where else ""
    export_query = urlencode({**{f"where.{k}": v for k, v in where.items()}, **({"q": q} if q else {}), **({"order_by": order_by} if order_by else {})})
    return (f"<h1>Table <span class='mono'>{h(name)}</span></h1><p class='lead'>{page.total} row(s){active}. <a class='button small' href='/api/db/{h(name)}.csv?{h(export_query)}'>CSV (filtered)</a></p>" + form
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
        if parts and parts[0] not in TOP_LEVEL:
            return False, "evaluator-only (not a public record family)"
        if parts and parts[0] in ("candidates", "evidence", "jobs", "probes", "calibration"):
            return False, "evaluator-only (raw pipeline records and audit evidence)"
        if len(parts) >= 3 and parts[0] == "scenarios":
            tail = parts[2:]
            if tail[0] == "archive":
                tail = tail[2:] if len(tail) >= 2 else ()
            if tail and (len(tail) != 1 or tail[0] not in ("approval.json", "compile.json", "validation.json", "difficulty.json", "scores.json")):
                return False, "evaluator-only (scenario source, assets or metadata)"
        if len(parts) >= 3 and parts[0] == "scenarios" and (parts[2] == "assets" or parts[-1] in ("scenario.json", "scenario.draft.json", "judge.json", "draft.meta.json", "audit.json")):
            return False, "evaluator-only (author-only facts, hidden test assets, judge/draft metadata)"
        if len(parts) >= 5 and parts[0] == "scenarios" and parts[2] == "archive" and (parts[4] == "assets" or parts[-1] in ("scenario.json", "scenario.draft.json", "judge.json", "draft.meta.json", "audit.json")):
            return False, "evaluator-only (archived spec and metadata)"
        if len(parts) >= 3 and parts[0] == "trials" and parts[-1] in ("record.json", "transcript.json", "spec.json", "audit.json"):
            return False, "evaluator-only (raw record, transcript, frozen spec, audit)"
        if len(parts) >= 3 and parts[0] == "trials" and (len(parts) != 3 or parts[-1] not in ("reward.json", "report.json", "status.json", "costs.json", "label.json")):
            return False, "evaluator-only (raw trial configuration or trace)"
        if len(parts) >= 2 and parts[0] == "tasks":
            return False, "evaluator-only (compiled task directories carry hidden tests)"
        if len(parts) >= 2 and parts[0] == "exports":
            return False, "evaluator-only (episode exports carry the frozen spec)"
    return True, ""


def artifact_path(root: Path, rel: Path) -> Path:
    """Resolve a workspace artifact without following any symlink or accepting prefix siblings."""
    root = root.resolve()
    target = root / rel
    if not target.resolve().is_relative_to(root):
        raise PermissionError("path escapes the workspace")
    current = root
    for part in rel.parts:
        current /= part
        if current.is_symlink():
            raise PermissionError("artifact symlinks are not served")
    return target


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
    target = artifact_path(root, rel)
    entries = []
    try:
        with os.scandir(target) as it:
            for e in it:
                r = rel / e.name
                if len(r.parts) >= 3 and r.parts[0] == "trials" and r.parts[2] == "task":
                    continue
                ok, why = is_listable(r, evaluator)
                if e.is_symlink():
                    ok, why = False, "artifact symlinks are not served"
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
    crumbs = ["<a href='/artifacts'>workspace</a>"]
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


def preview_file(root: Path, rel: Path, max_bytes: int = 400_000, evaluator: bool = False) -> dict | None:
    p = artifact_path(root, rel)
    if not p.is_file():
        return None
    size = p.stat().st_size
    lang = "diff" if p.suffix in (".diff", ".patch") else p.suffix.lstrip(".")
    text = None
    if p.suffix.lower() in TEXT_PREVIEW:
        with p.open("rb") as stream:
            data = stream.read(max_bytes)
        text = data.decode("utf-8", errors="replace")
        if p.suffix == ".json":
            try:
                parsed = json.loads(text)
                text = json.dumps(parsed if evaluator else project(parsed), indent=2, sort_keys=True)
            except ValueError:
                if not evaluator:
                    text = "JSON cannot be projected safely; evaluator access required."
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


def spend_body(spend_rows: list, costs: list, cap: Decimal | None, by_run: list[dict], pools: list[dict] | None, *, accounting: dict | None = None) -> str:
    total = sum((r.usd for r in spend_rows), Decimal(0))
    pct = f"{(float(total) / float(cap) * 100):.1f}% of cap ${cap:,.2f}" if cap else "no cap configured"
    views = [r.model_dump(mode="json") for r in spend_rows]
    accounting = accounting or {}
    completeness = ""
    if accounting.get("spend_complete") is False:
        completeness = (f"<p class='notice'>Known spend only: {h(accounting.get('unpriced_records', 0))} unpriced records, "
                        f"{h(accounting.get('unknown_usage_count', 0))} calls without measured usage. "
                        "Some usage or ledger evidence is incomplete; missing amounts remain unknown.</p>")
    body = ["<h1>Spend</h1><p class='lead'>Recorded provider spend per provider and model (the live ledger from <code>costs</code>); per run below. "
            "Unknown prices are recorded with <code>price_source = unknown</code> and never guessed.</p>",
            hero(f"${total:,.2f}", f"known recorded spend · {pct}", "/db/costs"), completeness,
            table("spend", SPEND_COLUMNS, views, key="model", empty="<strong>No costs recorded yet.</strong> Costs land in <code>trials/&lt;tid&gt;/costs.json</code> as trials complete.", export=True),
            "<h2>Per run (workflow)</h2>",
            table("spend-runs", (Column("workflow_id", "workflow", "id", href="/workflows/{workflow_id}", n=28), Column("usd", "known USD", "usd"), Column("records", "records", "num"),
                                  Column("unpriced_records", "unpriced", "num")), by_run,
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
    sections = (("Scenarios", hits.get("scenarios") or [], lambda s: f"<a href='/scenario/{h(s.scenario_id)}'>{h(s.scenario_id)}</a> {origin.badge(s.source)} v{h(s.version)} {chip(s.validation_status)}"),
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
