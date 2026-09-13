"""HTML for the operator console: create / run forms, jobs list and progress, review queue and review page."""
from __future__ import annotations

import json

from swesim.dashboard import console
from swesim.dashboard.render import (
    chip,
    dash,
    h,
    layout,
    outcome_chip,
    scenario_page_sections,
    stage_chips,
    status_kind,
)


def redact_dump(progress: dict) -> str:
    """Raw progress for the details pane, with URLs and secret-like values masked (the redactor caps at 300 chars per call)."""
    text = json.dumps(progress, indent=2, default=str)
    return "\n".join(console.redact(line) for line in text.splitlines()[:400])


def banner_slot() -> str:
    return "<div id='banner' class='banner hidden' role='status'></div>"


def unavailable_banner(message: str, cli: str, kind: str = "unreachable") -> str:
    heading = {"missing": "This worker's client does not support that yet.",
               "no-query": "No progress query for this workflow type."}.get(kind, "Temporal is not reachable from the dashboard.")
    return (f"<div class='banner {'warn' if kind in ('missing', 'no-query') else 'error'}'><strong>{h(heading)}</strong> {h(message)}"
            f"<div class='muted small'>Equivalent command:</div><pre>{h(cli)}</pre></div>")


def _opts(values, selected=None) -> str:
    return "".join(f"<option value='{h(v)}'{' selected' if v == selected else ''}>{h(label)}</option>"
                   for v, label in (values if isinstance(values[0], tuple) else [(x, x) for x in values]))


def create_page(d: dict) -> str:
    body = ["<h1>Create tasks</h1>",
            ("<p class='lead'>Start a creation job: mine candidates, judge them, draft the scenario, approve (auto or held for you), compile, "
            "validate the controls on Modal and register. The stakeholder URL and worker secret come from the server environment.</p>"),
            banner_slot(),
            "<form id='create-form' class='console' data-endpoint='/api/create' data-jobs='/jobs/'>",
            "<fieldset><legend>Source</legend>",
            "<label class='inline'><input type='radio' name='source' value='github-repo' checked> GitHub repositories</label>",
            "<label class='inline'><input type='radio' name='source' value='swebench-annotated'> SWE-bench annotated pool A</label>",
            ("<div data-source='github-repo'><label for='repos'>Repository URLs, one per line</label>"
            "<textarea id='repos' name='repos' rows='4' placeholder='https://github.com/py-pdf/pypdf'></textarea>"
            f"<div class='row'><label for='since'>merged since<input id='since' name='since' type='date' value='{h(d['since'])}'></label>"
            f"<label for='max_candidates'>max candidates per repo<input id='max_candidates' name='max_candidates' type='number' min='1' max='50' value='{h(d['max_candidates'])}'></label></div></div>"),
            ("<div data-source='swebench-annotated' class='hidden'><label for='instance_ids'>Instance ids (optional, one per line; empty = whole pool)</label>"
            "<textarea id='instance_ids' name='instance_ids' rows='3' placeholder='psf__requests-1944'></textarea>"
            f"<label for='limit'>limit<input id='limit' name='limit' type='number' min='1' max='200' value='{h(d['limit'])}'></label></div>"),
            "</fieldset><fieldset><legend>Models</legend><div class='row'>",
            f"<label for='judge_model'>judge model<input id='judge_model' name='judge_model' value='{h(d['judge_model'])}'></label>",
            (f"<label for='draft_model'>draft model<input id='draft_model' name='draft_model' value='{h(d['draft_model'])}'></label></div>"
            f"<p class='muted small'>Solver for the pipeline's single demonstration trial: <span class='mono'>{h(d['solver_model'])}</span> (never substituted).</p>"),
            "</fieldset><fieldset><legend>Approval and concurrency</legend>",
            f"<label for='approval_mode'>approval mode<select id='approval_mode' name='approval_mode'>{_opts(list(console.APPROVAL_MODES), 'auto')}</select></label>",
            ("<p class='muted small'>Auto: the code-owned gates decide and record <code>approved_by: auto-validators, review_needed: true</code>; "
            "you review in the morning. Hold: each draft waits in the <a href='/review'>review queue</a> until you approve or reject it.</p>"),
            f"<label for='max_parallel'>max parallel<input id='max_parallel' name='max_parallel' type='number' min='1' max='{console.MAX_PARALLEL}' value='{h(d['max_parallel'])}'></label>",
            "</fieldset><div><button type='submit' class='primary'>Start creation job</button> <span class='muted small' id='form-status'></span></div></form>"]
    return layout("swesim · create", "".join(body), "create", "console")


def run_page(d: dict, scenarios: list[dict]) -> str:
    opts = "".join(f"<option value='{h(s['id'])}'>{h(s['id'])} · v{h(s.get('version'))} · {h(s.get('source_ref'))}</option>" for s in scenarios)
    tiers = "".join(f"<label class='inline'><input type='checkbox' name='models' value='{h(m)}'{' checked' if m == d['solver_model'] else ''}> <span class='mono'>{h(m)}</span></label>"
                    for m in console.SOLVER_TIERS)
    conds = "".join(f"<label class='inline'><input type='checkbox' name='conditions' value='{c}'{' checked' if c == 'interactive' else ''}> {c}</label>"
                    for c in console.CONDITIONS)
    body = ["<h1>Run trials</h1>",
            ("<p class='lead'>Start a run matrix over registered, valid scenarios: k attempts per model per condition, grouped by frozen run "
            "configuration. Calibration and comparison runs are never pooled.</p>"), banner_slot(),
            "<form id='run-form' class='console' data-endpoint='/api/run' data-jobs='/jobs/'>",
            "<fieldset><legend>Scenarios</legend>"]
    if scenarios:
        body.append(f"<label for='scenario_ids'>registered valid scenarios (multi-select)</label><select id='scenario_ids' name='scenario_ids' multiple size='{min(8, max(3, len(scenarios)))}'>{opts}</select>")
    else:
        body.append("<p class='empty'><strong>No registered valid scenarios.</strong> A scenario appears here after <code>swesim validate</code> reports "
                    "<code>valid</code> and <code>swesim register</code> adds it to registry.json (or a creation job completes). <a href='/create'>Create tasks</a> first.</p>")
    body += ["</fieldset><fieldset><legend>Solver models</legend>", tiers,
             "<label for='models_extra'>other model ids (comma or newline separated)<input id='models_extra' name='models_extra' placeholder='provider/model-id'></label>",
             "</fieldset><fieldset><legend>Matrix</legend>", conds, "<div class='row'>",
             f"<label for='k'>k<input id='k' name='k' type='number' min='1' max='10' value='{h(d['k'])}'></label>",
             f"<label for='purpose'>purpose<select id='purpose' name='purpose'>{_opts(list(console.PURPOSES), 'comparison')}</select></label>",
             f"<label for='brief_profile'>brief profile<select id='brief_profile' name='brief_profile'>{_opts(list(console.PROFILES), 'withheld')}</select></label>",
             f"<label for='stakeholder_model'>stakeholder model<input id='stakeholder_model' name='stakeholder_model' value='{h(d['stakeholder_model'])}'></label>",
             f"<label for='max_parallel'>max parallel trials<input id='max_parallel' name='max_parallel' type='number' min='1' max='{console.MAX_PARALLEL}' value='{h(d['max_parallel'])}'></label>",
             "</div></fieldset><div><button type='submit' class='primary'"
             + (" disabled" if not scenarios else "") + ">Start run</button> <span class='muted small' id='form-status'></span></div></form>"]
    return layout("swesim · run", "".join(body), "run", "console")


JOB_HEAD = "<tr><th>type</th><th>workflow id</th><th>status</th><th>started</th><th>summary</th></tr>"
JOB_TEMPLATE = ("<template id='job-row'><tr data-id=''><td data-f='type'></td><td data-f='id'><a class='mono' href='#'></a></td>"
                "<td><span data-f='status' class='chip'></span></td><td data-f='started' class='mono small'></td><td data-f='summary'></td></tr></template>")


def job_row(j: dict) -> str:
    return (f"<tr data-id='{h(j['workflow_id'])}'><td data-f='type'>{dash(j.get('type'))}</td>"
            f"<td data-f='id'><a class='mono' href='/jobs/{h(j['workflow_id'])}'>{h(j['workflow_id'])}</a></td>"
            f"<td><span data-f='status' class='chip {status_kind(j.get('status'))}'>{dash(j.get('status'))}</span></td>"
            f"<td data-f='started' class='mono small'>{dash(j.get('started_at'))}</td><td data-f='summary'>{dash(j.get('summary'))}</td></tr>")


def jobs_page(jobs: list[dict] | None, error: console.ConsoleUnavailable | None) -> str:
    body = [("<h1>Jobs</h1><p class='lead'>Temporal workflows started from the console or the CLI: creation jobs (PipelineWorkflow) and run "
            "matrices. Refreshes every 5 s. Workflow state lives in Temporal; episodes and outcomes live on the <a href='/runs'>Runs</a> page.</p>")]
    if error is not None:
        body.append(unavailable_banner(str(error), error.cli, error.kind))
    rows = "".join(job_row(j) for j in jobs or [])
    body.append(f"<div class='table-wrap'><table class='{'hidden' if not jobs else ''}'><thead>{JOB_HEAD}</thead><tbody id='jobs-body'>{rows}</tbody></table></div>")
    empty = ("<p class='empty' id='jobs-body-empty'><strong>No workflows yet.</strong> Start one from <a href='/create'>Create</a> or <a href='/run'>Run</a>, or "
             "with <code>uv run swesim pipeline …</code> / <code>uv run swesim matrix …</code>; rows appear here within 5 s.</p>")
    body.append(empty if not jobs and error is None else empty.replace("<p class='empty'", "<p class='empty hidden'"))
    body.append(JOB_TEMPLATE)
    return layout("swesim · jobs", "".join(body), "jobs", "jobs", {"poll-ms": "5000"}, "updated 0s ago" if error is None else None)


def _progress_candidates(cands: list[dict]) -> str:
    rows = []
    for c in cands:
        if not isinstance(c, dict):
            continue
        stages = c.get("stages") if isinstance(c.get("stages"), list) else None
        chips = stage_chips(stages) if stages else chip(c.get("stage"), "warn pulse" if c.get("stage") == "awaiting-approval" else "info")
        sid = c.get("scenario_id")
        link = f"<a href='/scenario/{h(sid)}'>{h(sid)}</a>" if sid else "—"
        review = f" <a href='/review/{h(sid)}'>review</a>" if sid and c.get("stage") == "awaiting-approval" else ""
        detail = c.get("detail") or c.get("reasons")
        rows.append(f"<tr><td class='mono'>{dash(c.get('instance_id') or c.get('candidate'))}</td><td>{link}{review}</td><td>{chips}</td>"
                    f"<td class='small muted'>{dash(console.redact(str(detail))) if detail else '—'}</td></tr>")
    return ("<h2>Candidates</h2><div class='table-wrap'><table><thead><tr><th>candidate</th><th>scenario</th><th>stage</th><th>detail</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def _progress_trials(trials: list[dict]) -> str:
    rows = []
    for t in trials:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("trial_id") or "")
        link = f"<a class='trial-link' href='/trial/{h(tid)}'>{h(tid)}</a>" if tid else "—"
        rows.append(f"<tr><td>{link}</td><td>{dash(t.get('condition'))}</td><td>{dash(t.get('model'))}</td>"
                    f"<td>{chip(t.get('status'), status_kind(t.get('status')))}</td><td>{outcome_chip(t.get('outcome'), t.get('success'))}</td>"
                    f"<td class='num'>{dash(t.get('reward'))}</td></tr>")
    return ("<h2>Trials</h2><div class='table-wrap'><table><thead><tr><th>trial</th><th>condition</th><th>model</th><th>status</th><th>outcome</th>"
            f"<th class='num'>reward</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>")


def job_page(wid: str, progress: dict | None, error: console.ConsoleUnavailable | None, job: dict | None = None) -> str:
    body = [f"<h1>Job <span class='mono'>{h(wid)}</span></h1>"]
    if job:
        body.append(f"<p class='lead'>{dash(job.get('type'))} {chip(job.get('status'), status_kind(job.get('status')))} started {dash(job.get('started_at'))}</p>")
    body.append(banner_slot())
    if error is not None:
        body.append(unavailable_banner(str(error), error.cli, error.kind))
    if progress:
        stage = progress.get("stage") or progress.get("status")
        body.append(f"<p>Stage {chip(stage, 'warn pulse' if stage in ('awaiting-approval', 'trial', 'validating', 'compiling', 'mining', 'judging') else 'info')}"
                    + (f" · scenario <a href='/scenario/{h(progress['scenario_id'])}'>{h(progress['scenario_id'])}</a>" if progress.get("scenario_id") else "")
                    + (f" · <a href='/review/{h(progress['scenario_id'])}'>review this draft</a>" if stage == "awaiting-approval" and progress.get("scenario_id") else "")
                    + (f" · trial <a class='trial-link' href='/trial/{h(progress['trial_id'])}'>{h(progress['trial_id'])}</a>" if progress.get("trial_id") else "") + "</p>")
        if progress.get("needs_human"):
            body.append(f"<p class='flag'>needs a person: {h(json.dumps(progress['needs_human']))}</p>")
        if progress.get("substitutions"):
            body.append(f"<p class='muted small'>model substitutions: {h(json.dumps(progress['substitutions']))}</p>")
        if isinstance(progress.get("candidates"), list):
            body.append(_progress_candidates(progress["candidates"]))
        if isinstance(progress.get("trials"), list):
            body.append(_progress_trials(progress["trials"]))
        if isinstance(progress.get("scenarios"), list) and progress["scenarios"]:
            items = "".join(f"<li><a href='/scenario/{h(s.get('scenario_id'))}'>{h(s.get('scenario_id'))}</a> {chip(s.get('status'), 'good' if str(s.get('status', '')).startswith('complete') else 'critical')}"
                            f"{' · ' + h('; '.join(map(str, s.get('reasons') or s.get('failures') or []))) if (s.get('reasons') or s.get('failures')) else ''}</li>"
                            for s in progress["scenarios"] if isinstance(s, dict))
            body.append(f"<h2>Scenarios</h2><ul class='small'>{items}</ul>")
        counts = {k: progress[k] for k in ("tried", "completed", "rejected", "invalid") if k in progress}
        if counts:
            body.append("<p class='muted small'>" + " · ".join(f"{k} {h(v)}" for k, v in counts.items()) + "</p>")
        body.append(f"<details><summary>raw progress</summary><pre>{h(console.redact(json.dumps(progress, indent=2, default=str))[:300] if False else redact_dump(progress))}</pre></details>")
    elif error is None:
        body.append("<p class='empty'>No progress yet; the workflow has not reported a stage.</p>")
    body.append(f"<div class='card'><strong>Cancel</strong> <span class='muted small'>Stops the workflow; running Harbor units are not killed by this button.</span>"
                f"<div><button type='button' class='danger' id='cancel-job' data-endpoint='/api/jobs/{h(wid)}/cancel'>Cancel workflow…</button> "
                f"<span class='muted small' id='cancel-status'></span></div></div>")
    return layout(f"swesim · job {wid}", "".join(body), "jobs", "job", {"job": wid})


def review_queue_page(queue: dict) -> str:
    items = queue.get("items") or []
    body = [("<h1>Review queue</h1><p class='lead'>Drafts waiting for a decision: candidates held by creation jobs (approval mode "
            "<em>hold</em>) and local drafts with <code>scenario.draft.json</code> but no <code>approval.json</code>.</p>")]
    for err in queue.get("errors") or []:
        body.append(f"<div class='banner warn'>{h(err)}</div>")
    if not items:
        body.append("<p class='empty'><strong>Nothing to review.</strong> Drafts appear here while a creation job with approval mode "
                    "<em>Hold each task for my approval</em> waits, or when <code>swesim draft</code> has written a draft that nobody approved yet. "
                    "Auto-approved tasks are listed on the <a href='/'>overview</a> with a <em>review needed</em> chip.</p>")
    else:
        rows = "".join(
            f"<tr><td><a href='/review/{h(i['scenario_id'])}'>{h(i['scenario_id'])}</a></td><td>{dash(i.get('source'))}"
            + (f" <span class='mono small'>{h(i['workflow_id'])}</span>" if i.get("workflow_id") else "") + "</td>"
            f"<td class='mono small'>{dash((i.get('disk_hash') or i.get('spec_hash') or '')[:12])}"
            + (" <span class='flag'>hash differs from job</span>" if i.get("spec_hash") and i.get("disk_hash") and i["spec_hash"] != i["disk_hash"] else "") + "</td>"
            f"<td>{dash(i.get('judge_keep'))} / {dash(i.get('rubric'))}</td><td>{dash(i.get('drafted_at'))}</td>"
            f"<td>{chip('on disk', 'good') if i.get('on_disk') else chip('not on disk', 'critical', 'copy the scenario directory here to review it')}</td></tr>"
            for i in items)
        body.append("<div class='table-wrap'><table><thead><tr><th>scenario</th><th>source</th><th>spec hash</th><th>judge keep / rubric</th>"
                    f"<th>drafted</th><th>files</th></tr></thead><tbody>{rows}</tbody></table></div>")
    return layout("swesim · review", "".join(body), "review", "review")


def review_page(d: dict, spec_hash: str | None, workflow_id: str | None, approval: dict, review: dict, evaluator: bool = False) -> str:
    sid = d["board"]["id"]
    read_only = bool(approval) or review.get("decision") == "reject"
    head = [f"<h1>Review <span class='mono'>{h(sid)}</span></h1>", banner_slot()]
    if approval:
        head.append(f"<p class='lead'>{chip('approved', 'good')} by {h(approval.get('approved_by'))}"
                    + (" · " + chip("review needed", "warn") if approval.get("review_needed") else "")
                    + f" · spec hash <span class='mono'>{dash(approval.get('spec_hash'))}</span>. Read-only: this draft is already frozen as scenario.json.</p>")
    elif review.get("decision") == "reject":
        head.append(f"<p class='lead'>{chip('rejected', 'critical')} by {h(review.get('decided_by'))} at {h(review.get('decided_at'))}"
                    + (f" — {h(review.get('note'))}" if review.get("note") else "") + "</p>")
    else:
        head.append(f"<p class='lead'>{chip('awaiting approval', 'warn pulse')} spec hash on disk <span class='mono'>{dash(spec_hash)}</span>"
                    + (f" · held by job <a class='mono' href='/jobs/{h(workflow_id)}'>{h(workflow_id)}</a>" if workflow_id else " · local draft") + "</p>")
    if not spec_hash and not approval:
        head.append("<p class='flag'>The draft on disk does not validate against the current schema; approval is disabled.</p>")
    sections = scenario_page_sections(d, toggle_base=f"/review/{sid}")
    assets = d["spec"].get("assets") or {}
    asset_rows = "".join(f"<tr><td class='mono'>{h(name)}</td><td class='mono small'>{h(a.get('path'))}</td><td class='num'>{h(a.get('bytes'))}</td>"
                         f"<td class='mono small'>{h(str(a.get('sha256') or '')[:16])}…</td></tr>" for name, a in sorted(assets.items()) if isinstance(a, dict))
    assets_html = (f"<h2>Assets</h2><div class='table-wrap'><table><thead><tr><th>asset</th><th>path</th><th class='num'>bytes</th><th>sha256</th></tr></thead>"
                   f"<tbody>{asset_rows}</tbody></table></div>" if asset_rows else "<h2>Assets</h2><p class='empty'>No assets in the manifest.</p>")
    disabled = " disabled" if read_only or not spec_hash else ""
    form = (f"<form id='review-form' class='console card' data-endpoint='/api/review/{h(sid)}'>"
            f"<input type='hidden' name='spec_hash' value='{h(spec_hash or '')}'><input type='hidden' name='workflow_id' value='{h(workflow_id or '')}'>"
            "<label for='note'>note (recorded with the decision)</label><textarea id='note' name='note' rows='3' maxlength='2000'></textarea>"
            f"<div><button type='submit' class='primary' name='decision' value='approve'{disabled}>Approve</button> "
            f"<button type='submit' class='danger' name='decision' value='reject'{disabled}>Reject</button> <span class='muted small' id='form-status'></span></div>"
            "<p class='muted small'>Approve freezes the draft as scenario.json (local) or signals the holding job; the server re-reads the spec hash and refuses "
            "if the draft changed since this page was rendered. Never hand-edit a draft to pass a gate: re-draft or take the next candidate.</p></form>")
    body = "".join(head) + sections["briefs"] + sections["ledger"] + sections["controls"] + assets_html + sections["judge"] + form
    return layout(f"swesim · review {sid}", body, "review", "review-item")
