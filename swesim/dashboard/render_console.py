"""Operator console pages: Create, Run, Review queue and the review page.

Every form posts through the two-step flow in ``static/app.js`` (``data-action``): a preview returns the exact CLI
equivalent and a signed token, the confirm dialog shows it, the second POST carries the token. The forms keep the
validation of ``console.py``; the review page reuses the scenario sections and its Approve/Reject buttons go to the
existing ``approve_candidate`` signal path.
"""
from __future__ import annotations

import json

from swesim.dashboard import console
from swesim.dashboard.render import TIERS, chip, h, ident, model_chip, table, Column
from swesim.dashboard.render_scenario import scenario_sections


def banner_slot() -> str:
    return "<div id='banner' class='banner hidden' role='status'></div>"


def unavailable_banner(message: str, cli: str, kind: str = "unreachable") -> str:
    heading = {"missing": "This worker's client does not support that yet.", "no-query": "No progress query for this workflow type."}.get(kind, "Temporal is not reachable from the console.")
    return (f"<div class='banner {'warn' if kind in ('missing', 'no-query') else 'error'}'><strong>{h(heading)}</strong> {h(message)}"
            f"<div class='muted small'>Equivalent command:</div><pre class='code'>{h(cli)}</pre></div>")


def _opts(values, selected=None) -> str:
    pairs = values if values and isinstance(values[0], tuple) else [(x, x) for x in values]
    return "".join(f"<option value='{h(v)}'{' selected' if v == selected else ''}>{h(label)}</option>" for v, label in pairs)


def create_body(d: dict, repos: list[str], spend_cap) -> str:
    repo_opts = "".join(f"<label class='inline'><input type='checkbox' name='repos' value='{h(r)}'> <span class='mono'>{h(r)}</span></label>" for r in repos)
    cap = f"${spend_cap:,.2f}" if spend_cap is not None else "none (SWESIM_SPEND_CAP_USD unset)"
    body = ["<h1>Create tasks</h1>",
            ("<p class='lead'>Start a creation job (CreateTasksWorkflow): mine candidates from every source, then one CandidateWorkflow per candidate — judge, draft, approve "
             "(auto or held for you), compile, validate the controls on Modal, register. You confirm the exact command before it runs. The stakeholder URL and worker secret "
             "come from the server settings and are never form fields.</p>"), banner_slot(),
            "<form id='create-form' class='console' data-action='create'>",
            "<fieldset><legend>Source</legend>",
            "<label class='inline'><input type='radio' name='source' value='github-repo' checked> GitHub repositories</label>",
            "<label class='inline'><input type='radio' name='source' value='swebench-annotated'> SWE-bench annotated pool A</label>",
            "<div data-source-block='github-repo'>",
            (f"<div class='muted small'>Repositories already in the record:</div>{repo_opts}" if repos else ""),
            "<label for='repos_extra'>More repository URLs or owner/repo, one per line</label><textarea id='repos_extra' name='repos' rows='3' placeholder='https://github.com/py-pdf/pypdf'></textarea>",
            f"<div class='row'><label for='since'>merged since<input id='since' name='since' type='date' value='{h(d['since'])}'></label>",
            f"<label for='max_candidates'>max candidates per repo<input id='max_candidates' name='max_candidates' type='number' min='1' max='50' value='{h(d['max_candidates'])}'></label>",
            "<label for='pr_kind'>PR band (--pr-kind)<select id='pr_kind' name='pr_kind'><option value='bugfix' selected>bugfix (20–600 diff lines, ≥4 comments)</option>"
            "<option value='feature'>feature (200–1500 lines, ≥8 comments, feature label)</option></select></label></div></div>",
            "<div data-source-block='swebench-annotated' class='hidden'><label for='instance_ids'>Instance ids (optional, one per line; empty = whole pool)</label>"
            "<textarea id='instance_ids' name='instance_ids' rows='3' placeholder='psf__requests-1944'></textarea>"
            f"<label for='limit'>limit<input id='limit' name='limit' type='number' min='1' max='200' value='{h(d['limit'])}'></label></div>",
            "</fieldset><fieldset><legend>Models</legend><div class='row'>",
            f"<label for='judge_model'>judge model<input id='judge_model' name='judge_model' value='{h(d['judge_model'])}'></label>",
            f"<label for='draft_model'>draft model<input id='draft_model' name='draft_model' value='{h(d['draft_model'])}'></label></div>",
            f"<p class='muted small'>Judge and draft must differ (the judge never grades its own drafts). Solver for demonstration trials: {model_chip(d['solver_model'])}.</p>",
            "</fieldset><fieldset><legend>Approval, concurrency, rebuild</legend>",
            f"<label for='approval_mode'>approval mode<select id='approval_mode' name='approval_mode'>{_opts(list(console.APPROVAL_MODES), 'auto')}</select></label>",
            ("<p class='muted small'>Auto: the code-owned gates decide and record <code>approved_by: auto-validators, review_needed: true</code>. Hold: each draft waits in the "
             "<a href='/review'>review queue</a> until you approve or reject it.</p>"),
            f"<label for='max_parallel'>max parallel candidates<input id='max_parallel' name='max_parallel' type='number' min='1' max='{console.MAX_PARALLEL}' value='{h(d['max_parallel'])}'></label>",
            "<label class='inline'><input type='checkbox' id='rebuild' name='rebuild' value='1'> Rebuild existing scenarios as a new version</label>",
            ("<p class='muted small'>Existing scenarios are re-created as version n+1; the previous version's files move to <code>archive/v&lt;n&gt;/</code>. Judged rows and probe results are kept.</p>"),
            f"<p class='muted small'>Spend cap: <strong>{h(cap)}</strong>. Pipeline-role costs are recorded per candidate.</p>",
            "</fieldset><div class='actions'><button type='submit' class='button primary'>Preview and confirm…</button> <span data-status class='muted small'></span></div></form>"]
    return "".join(body)


def create_page(d: dict) -> str:
    """The create form body with no repository list and no cap (the shape older callers and tests/test_workflows.py use)."""
    return create_body(d, [], None)


def run_body(d: dict, scenarios: list, spend_cap) -> str:
    """``scenarios``: ScenarioRows (valid, current) with difficulty labels for the picker."""
    opts = "".join(f"<option value='{h(s.scenario_id)}' data-tier='{h(s.difficulty_tier or 'unlabelled')}' data-source='{h(s.source)}'>"
                   f"{h(s.scenario_id)} · v{h(s.version)} · {h(s.difficulty_tier or 'unlabelled')} · {h(s.source)}</option>" for s in scenarios)
    tiers = "".join(f"<details{' open' if t in ('A', 'B', 'C') else ''}><summary>Tier {h(t)} <span class='muted small'>{len(ms)} model(s)"
                    + (" — gated on Tier A–C trends (brief §3.2)" if t in ("D", "E") else "") + "</span></summary>"
                    + "".join(f"<label class='inline'><input type='checkbox' name='models' value='{h(m)}'> {model_chip(m, tier=False)}</label>" for m in ms) + "</details>"
                    for t, ms in TIERS.items())
    conds = "".join(f"<label class='inline'><input type='checkbox' name='conditions' value='{c}'{' checked' if c == 'interactive' else ''}> {c}</label>" for c in console.CONDITIONS)
    cap = f"${spend_cap:,.2f}" if spend_cap is not None else "none (SWESIM_SPEND_CAP_USD unset)"
    body = ["<h1>Run trials</h1>",
            ("<p class='lead'>Start a run (RunTasksWorkflow) over registered, valid scenarios: k attempts per model per condition, grouped by frozen run configuration. "
             "Calibration, comparison, demonstration and screen runs are never pooled. A screen uses hidden k=2 and full k=1, and defaults to the configured screen model when none is selected. You confirm the exact command before it runs.</p>"), banner_slot(),
            "<form id='run-form' class='console' data-action='run'><fieldset><legend>Scenarios</legend>"]
    if scenarios:
        body.append("<p class='muted small'>Difficulty labels: informative = on the leaderboard; saturated = excluded by default from frontier runs; unlabelled = run the screen first (purpose = screen).</p>")
        body.append(f"<label for='scenario_ids'>registered valid scenarios (multi-select)</label><select id='scenario_ids' name='scenario_ids' multiple size='{min(10, max(3, len(scenarios)))}'>{opts}</select>")
    else:
        body.append("<p class='empty'><strong>No registered valid scenarios.</strong> A scenario appears here when validation reports <code>valid</code> and it is registered. <a href='/create'>Create tasks</a> first.</p>")
    body += ["</fieldset><fieldset><legend>Solver models (by tier)</legend>", tiers,
             "<label for='models_extra'>other model ids (comma or newline separated)<input id='models_extra' name='models_extra' placeholder='provider/model-id'></label>",
             "</fieldset><fieldset><legend>Matrix</legend>", conds, "<div class='row'>",
             f"<label for='k'>k<input id='k' name='k' type='number' min='1' max='10' value='{h(d['k'])}'></label>",
             f"<label for='purpose'>purpose<select id='purpose' name='purpose'>{_opts(list(console.PURPOSES), 'comparison')}</select></label>",
             f"<label for='brief_profile'>brief profile<select id='brief_profile' name='brief_profile'>{_opts(list(console.PROFILES), 'withheld')}</select></label>",
             f"<label for='stakeholder_model'>stakeholder model<input id='stakeholder_model' name='stakeholder_model' value='{h(d['stakeholder_model'])}'></label>",
             f"<label for='max_parallel'>max parallel trials<input id='max_parallel' name='max_parallel' type='number' min='1' max='{console.MAX_PARALLEL}' value='{h(d['max_parallel'])}'></label>",
             "<label for='attempt_offset'>attempt offset<input id='attempt_offset' name='attempt_offset' type='number' min='0' max='1000' value='0'></label></div>",
             f"<p class='muted small'>Spend cap: <strong>{h(cap)}</strong>; RunTasksWorkflow stops planning new trials when recorded spend exceeds it. A retry of the same matrix needs a fresh attempt offset: trial ids are never reused.</p>",
             "</fieldset><div class='actions'><button type='submit' class='button primary'" + (" disabled" if not scenarios else "") + ">Preview and confirm…</button> <span data-status class='muted small'></span></div></form>"]
    return "".join(body)


# ----------------------------------------------------------------------------- review

def review_queue_body(queue: dict) -> str:
    items = queue.get("items") or []
    body = ["<h1>Review queue</h1><p class='lead'>What needs a decision: drafts held by creation jobs (approval mode <em>hold</em>) and local drafts without <code>approval.json</code>. "
            "Approve signals the holding CandidateWorkflow; reject records the reason.</p>"]
    for err in queue.get("errors") or []:
        body.append(f"<div class='banner warn'>{h(err)}</div>")
    views = [{"scenario_id": i["scenario_id"], "source": i.get("source"), "workflow_id": i.get("workflow_id"), "spec_hash": i.get("disk_hash") or i.get("spec_hash"),
              "hash_state": "differs from job" if i.get("spec_hash") and i.get("disk_hash") and i["spec_hash"] != i["disk_hash"] else "matches",
              "judge": f"{i.get('judge_keep')} / {i.get('rubric')}", "drafted_at": i.get("drafted_at"), "on_disk": "on disk" if i.get("on_disk") else "not on disk"} for i in items]
    cols = (Column("scenario_id", "scenario", "text", href="/review/{scenario_id}"), Column("source", "source", "text"), Column("workflow_id", "held by", "id", href="/workflows/{workflow_id}", n=24),
            Column("spec_hash", "spec hash", "id"), Column("hash_state", "hash", "text"), Column("judge", "judge keep / rubric", "text"), Column("drafted_at", "drafted", "time"), Column("on_disk", "files", "chip"))
    body.append(table("review", cols, views, key="scenario_id", empty="<strong>Nothing to review.</strong> Drafts appear while a creation job with approval mode <em>hold</em> waits, "
                                                                     "or when a draft exists that nobody approved yet. Auto-approved tasks carry <em>review needed</em> on their scenario page.", export=False))
    return "".join(body)


def review_body(d: dict, row, versions: list, spec_hash: str | None, workflow_id: str | None, approval: dict, review: dict, evaluator: bool) -> str:
    sid = d["scenario_id"]
    read_only = bool(approval) or review.get("decision") == "reject"
    head = [f"<h1>Review <span class='mono'>{h(sid)}</span></h1>", banner_slot()]
    if approval:
        head.append(f"<p class='lead'>{chip('approved', 'good')} by {h(approval.get('approved_by'))}" + (" · " + chip("review needed", "serious") if approval.get("review_needed") else "")
                    + f" · spec hash {ident(approval.get('spec_hash'), 16)}. Read-only: this draft is frozen as scenario.json.</p>")
    elif review.get("decision") == "reject":
        head.append(f"<p class='lead'>{chip('rejected', 'critical')} by {h(review.get('decided_by'))} at {h(review.get('decided_at'))}" + (f" — {h(review.get('note'))}" if review.get("note") else "") + "</p>")
    else:
        head.append(f"<p class='lead'>{chip('awaiting approval', 'run')} spec hash on disk {ident(spec_hash, 16)}"
                    + (f" · held by job <a class='mono' href='/workflows/{h(workflow_id)}'>{h(workflow_id)}</a>" if workflow_id else " · local draft") + "</p>")
    if not spec_hash and not approval:
        head.append("<p class='flag'>The draft on disk does not validate against the current schema; approval is disabled.</p>")
    sections = scenario_sections(d, row, versions, "", evaluator)
    disabled = " disabled" if read_only or not spec_hash else ""
    params = {"scenario_id": sid, "spec_hash": spec_hash or "", "workflow_id": workflow_id or ""}
    form = (f"<div class='card' id='review-form'><label for='note'>reason (recorded with the decision)</label><textarea id='note' name='note' rows='3' maxlength='2000'></textarea>"
            f"<div class='actions'><button type='button' class='button primary' data-action='review' data-params='{h(json.dumps({**params, 'decision': 'approve'}))}' data-reason-from='#note' data-status='#action-status'{disabled}>Approve…</button>"
            f"<button type='button' class='button danger' data-action='review' data-params='{h(json.dumps({**params, 'decision': 'reject'}))}' data-reason-from='#note' data-status='#action-status'{disabled}>Reject…</button>"
            "<span id='action-status' class='muted small'></span></div>"
            "<p class='muted small'>Approve freezes the draft as scenario.json (local) or signals the holding CandidateWorkflow; the server re-reads the spec hash and refuses if the draft changed since "
            "this page was rendered. Never hand-edit a draft to pass a gate: re-draft or take the next candidate.</p></div>")
    return "".join(head) + sections["briefs"] + sections["ledger"] + sections["controls"] + sections["assets"] + sections["judge"] + form
