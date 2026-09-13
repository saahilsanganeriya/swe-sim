"""Episode page ("what happened to this attempt"): a two-party transcript with timestamps, patience and exchange type,
question and confirmation highlighted, the submitted patch and test results beside it, and evaluator-only material
(fact annotations, redactions, tool audit, task-sense labels, raw record/transcript/spec) behind an explicit toggle that
is rendered only for evaluator identities. Non-evaluator output never contains the evaluator-only keys (rows.EVALUATOR_ONLY_KEYS).
"""
from __future__ import annotations

import json
import re

from swesim.dashboard.data import REFUSAL_PREFIX, clock, EVALUATOR_TRIAL_FILES
from swesim.dashboard.render import chip, code_block, dash, evaluator_switch, h, ident, kv, meter, model_chip, outcome_chip, short
from swesim.stages.score import OUTCOME_NAMES, outcome_code
from swesim.dashboard.public import project

from swesim.workflows.operator_actions import LABELS
# Evaluator-only meta (Codex B-P2-6). ``patience_remaining`` stays public: it is the availability signal the design intends (Codex A-P1-3 ruling).
AUDIT_META = ("disclosed_fact_ids", "tier", "tool_calls", "delivery", "redacted_terms", "regenerated", "audit", "forbidden_terms", "withheld_sentences")


def public_rows(rows: list[dict]) -> list[dict]:
    """Timeline rows without evaluator-only material: no tool-call rows, no audit fields in any meta."""
    out = []
    for r in rows:
        if r.get("kind") == "tool_call":
            continue
        meta = r.get("meta")
        if isinstance(meta, dict):
            r = {**r, "meta": {k: v for k, v in meta.items() if k not in AUDIT_META}}
        out.append(project(r))
    return out


def _files_from_diff(text: str) -> list[str]:
    files = []
    for m in re.finditer(r"^\+\+\+ (?:b/)?(\S+)", text or "", re.MULTILINE):
        if m.group(1) != "/dev/null" and m.group(1) not in files:
            files.append(m.group(1))
    return files


def chat_bubbles(rows: list[dict], live: bool, evaluator: bool, audit_by_seq: dict | None = None) -> str:
    convo = [r for r in rows if r.get("actor") in ("agent", "stakeholder") and r.get("kind") not in ("agent-step", "tool_call")]
    parts = []
    for r in convo:
        m = r.get("meta") or {}
        kind = r.get("kind") or ""
        refusal = r["actor"] == "stakeholder" and (m.get("tier") == "refused" or str(r.get("text") or "").startswith(REFUSAL_PREFIX))
        role = ("refusal" if refusal else "review request" if kind == "review" and r["actor"] == "agent" else "review" if kind == "review"
                else "describes behaviour" if kind == "confirm" and r["actor"] == "agent" else "confirms / denies" if kind == "confirm"
                else "question" if r["actor"] == "agent" else "reply")
        when = (f"<time datetime='{h(r['ts_iso'])}' title='{h(r['ts_iso'])}'>{h(clock(r['ts']))}</time>" if r.get("ts") is not None
                else "<span class='flag' title='no timestamp'>no timestamp</span>")
        patience = f"<span class='muted' title='patience remaining after this reply'>patience {h(m.get('patience_remaining'))}</span>" if m.get("patience_remaining") is not None else ""
        head = f"<div class='who'><span class='badge {h(r['actor'])}'>{h(r['actor'])}</span><span class='xtype'>{h(kind)}</span><span>{h(role)}</span>{when}{patience}</div>"
        text = str(r.get("text") or "")
        if kind == "review" and r["actor"] == "agent":
            files = _files_from_diff(text)
            first, _, diff = text.partition("\n")
            body = (f"<div class='text'>{h(first)}</div><div class='muted small'>{len(files)} file(s) · diff-only, unverified</div>"
                    + (f"<ul class='files'>{''.join(f'<li>{h(f)}</li>' for f in files)}</ul>" if files else "")
                    + (f"<details class='diff'><summary>diff</summary>{code_block(diff, 'diff', diff=True, max_lines=400)}</details>" if diff.strip() else ""))
        else:
            body = f"<div class='text'>{h(text)}</div>"
        if evaluator and r["actor"] == "stakeholder":
            bits = []
            if m.get("tier") is not None:
                bits.append(f"tier {h(m.get('tier'))}")
            if m.get("disclosed_fact_ids") is not None:
                bits.append("disclosed fact ids: " + (", ".join(h(x) for x in m["disclosed_fact_ids"]) or "none"))
            if m.get("redacted_terms"):
                bits.append("redacted terms: " + ", ".join(h(x) for x in m["redacted_terms"]))
            if m.get("regenerated") is not None:
                bits.append(f"regenerated {h(m.get('regenerated'))}")
            if m.get("tool_calls"):
                bits.append("tools: " + ", ".join(h(c.get("tool") if isinstance(c, dict) else c) for c in m["tool_calls"]))
            if m.get("delivery"):
                bits.append(f"delivery {h(m['delivery'])} (a generated reply is not proof the agent received it)")
            a = (audit_by_seq or {}).get(m.get("seq"))
            if a:
                bits.append("task-sense: " + ", ".join(f"{h(k)}={h(v)}" for k, v in a.items() if k not in ("seq", "reason")) + (f" — {h(a.get('reason'))}" if a.get("reason") else ""))
            if bits:
                body += f"<div class='audit' data-evaluator-only><strong>audit (evaluator-only):</strong> {' · '.join(bits)}</div>"
        elif evaluator and r["actor"] == "agent" and (audit_by_seq or {}).get(m.get("seq")):
            a = audit_by_seq[m.get("seq")]
            body += "<div class='audit' data-evaluator-only><strong>question audit (evaluator-only):</strong> " + ", ".join(f"{h(k)}={h(v)}" for k, v in a.items() if k not in ("seq", "reason")) + (f" — {h(a.get('reason'))}" if a.get("reason") else "") + "</div>"
        if m.get("request_id"):
            body += f"<div class='muted small'>request {ident(m['request_id'], 10)}</div>"
        parts.append(f"<div class='bubble {h(r['actor'])} {h(kind)}{' refusal' if refusal else ''}'>{head}{body}</div>")
    if live:
        parts.append("<div class='working'><span class='dots'><i></i><i></i><i></i></span><span>agent is working…</span></div>")
    empty = ("<p class='empty' id='chat-empty'><strong>No exchanges yet.</strong> The agent asks with <code>ask-user</code> and describes its resulting behaviour "
             "with <code>confirm-behavior</code>; agent messages appear on the left, stakeholder replies on the right.</p>")
    return f"<div class='chat' id='chat'>{''.join(parts)}</div>" + (empty if not convo else empty.replace("<p class='empty'", "<p class='empty hidden'"))


def _call_text(call) -> str:
    if not isinstance(call, dict):
        return str(call)
    args = call.get("arguments")
    if isinstance(args, dict) and isinstance(args.get("command"), str):
        return args["command"]
    return args if isinstance(args, str) else json.dumps(args, indent=1)


def timeline_row(r: dict, evaluator: bool) -> str:
    actor, kind, meta = r["actor"], r["kind"], r.get("meta") or {}
    when = (f"<time datetime='{h(r['ts_iso'])}' title='{h(r['ts_iso'])}'>{h(clock(r['ts']))}</time>" if r["ts"] is not None
            else "<span class='flag' title='This row has no timestamp; its position is not chronological evidence.'>no timestamp</span>")
    head = f"<div class='hdr'>{when} <span class='badge {h(actor)}'>{h(actor)}</span><span class='xtype'>{h(kind)}</span></div>"
    if kind == "agent-step":
        body = f"<div class='txt'>{h(r['text'])}</div>"
        calls = meta.get("tool_calls") or []
        obs = (meta.get("observation") or {}).get("results") or []
        if calls or obs:
            inner = "".join(f"<pre class='code'>$ {h(_call_text(c))}</pre>" for c in calls)
            inner += "".join(f"<pre class='code'>{h(short(o.get('content') if isinstance(o, dict) else o, 1500))}</pre>" for o in obs)
            body += f"<details><summary>{len(calls)} tool call(s), {len(obs)} observation(s)</summary>{inner}</details>"
    elif kind in ("prompt", "system"):
        body = f"<details><summary>{h(kind)} from harness ({len(r['text'])} chars)</summary><pre class='code'>{h(r['text'])}</pre></details>"
    elif kind == "tool_call":
        body = f"<code>{h(r['text'])}</code> <span class='muted'>stakeholder tool call; diff-only, unverified</span>"
    elif kind == "outcome":
        body = f"<div class='txt'><strong>{h(r['text'])}</strong></div>"
    else:
        body = f"<div class='txt'>{h(r['text'])}</div>"
        if meta.get("request_id"):
            body += f"<div class='muted small'>request {ident(meta['request_id'], 10)}</div>"
    return f"<div class='row {h(actor)}'>{head}{body}</div>"


def verifier_section(t: dict, tid: str) -> str:
    rep, rw = t["report"], t["reward"]
    if not rep and not rw:
        return "<p class='empty'>No verifier result yet. Outcome, reward and per-group results appear when the trial completes.</p>"
    code = outcome_code({"reward": rw}) if rw else None
    name = rep.get("outcome") or (OUTCOME_NAMES[code] if code is not None else None)
    res = rep.get("reviewed_equals_submitted")
    res_text = "null — diff-only pilot: the reviewed diff is agent-supplied and unverified against the graded submission" if res is None else str(res)
    pairs = [("outcome", f"{outcome_chip(name, rw.get('success'), code)} <span class='muted'>code {h(rw.get('outcome_code'))}</span> · <a href='/trial/{h(tid)}/file/reward.json'>reward.json</a>"),
             ("success", h(rw.get("success"))), ("reward", f"{h(rw.get('reward'))} <span class='muted'>partial credit by requirement group; any regression sets 0</span>"),
             ("simulator validity", h(rep.get("simulator_validity") or ("unadjudicated" if t["meta"].get("condition") == "interactive" else "n/a"))),
             ("transcript binding", h(rep.get("transcript_binding_valid"))), ("reviewed_equals_submitted", h(res_text))]
    if rep.get("integrity_signals"):
        pairs.append(("integrity signals", f"<span class='flag'>{h(json.dumps(rep['integrity_signals']))}</span>"))
    if rep.get("harbor_exception"):
        pairs.append(("harbor exception", f"<span class='flag'>{h(json.dumps(rep['harbor_exception']))}</span>"))
    if rep.get("error"):
        pairs.append(("error", f"<span class='flag'>{h(rep['error'])}</span>"))
    parts = [kv(pairs)]
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


def label_form(t: dict, tid: str) -> str:
    cur = (t["label"] or {}).get("label")
    opts = "".join(f"<option value='{h(lab)}'{' selected' if lab == cur else ''}>{h(lab)}</option>" for lab in LABELS)
    note = h((t["label"] or {}).get("note", ""))
    current = f" Current: <strong>{h(cur)}</strong>" if cur else " No label yet."
    return (f"<form class='label' id='labelform' data-action='label'><input type='hidden' name='trial_id' value='{h(tid)}'>"
            f"<p class='muted small'>Reviewer label for this episode (evaluator-only annotation; it never changes reward or training eligibility).{current}</p>"
            f"<label for='label'>label</label><select id='label' name='label'><option value=''{'' if cur else ' selected'}>— choose —</option>{opts}</select>"
            f"<label for='note'>note</label><textarea id='note' name='note' maxlength='2000'>{note}</textarea>"
            "<div class='actions'><button type='submit' class='primary'>Save label…</button> <span data-status class='muted small'></span></div></form>")


def files_section(t: dict, tid: str, resolve, evaluator: bool) -> str:
    items, gated = [], 0
    for name in t["files"]:
        p = resolve(t, name)
        if p is None or not p.exists():
            continue
        if name in EVALUATOR_TRIAL_FILES and not evaluator:
            gated += 1
            continue
        flag = " <span class='flag small'>evaluator-only</span>" if name in EVALUATOR_TRIAL_FILES else ""
        items.append(f"<li><a href='/trial/{h(tid)}/file/{h(name)}'>{h(name)}</a>{flag}</li>")
    if gated:
        items.append(f"<li class='muted'>{gated} evaluator-only file(s) (record, transcript, spec, audit) are not listed for this identity.</li>")
    items.append(f"<li><a href='/artifacts?path=trials/{h(tid)}'>browse trials/{h(tid)}/</a> <span class='muted small'>(the per-trial task copy is never listed)</span></li>")
    return "<ul class='small'>" + "".join(items) + "</ul>"


def episode_body(t: dict, row, rows: list[dict], resolve, evaluator: bool, extras: dict | None = None) -> str:
    m, tid = t["meta"], t["trial_id"]
    rows = rows if evaluator else public_rows(rows)
    st = (row.status if row else None) or t["status"].get("status") or ("completed" if t["record"] else "unknown")
    live = bool(t["live"])
    p = t["patience"]
    audit = t.get("audit") if evaluator else {}
    by_seq = {}
    for k in ("questions", "replies"):
        for item in (audit.get(k) or []) if isinstance(audit, dict) else []:
            if isinstance(item, dict) and "seq" in item:
                by_seq[item["seq"]] = item
    left = [f"<h1>Episode {ident(tid, 24)}</h1>",
            (f"<p class='lead'>{chip(st)} <a href='/scenario/{h(m.get('scenario_id'))}'>{dash(m.get('scenario_id'))}</a> · {dash(m.get('condition'))} · brief {dash(m.get('brief_profile'))} · "
             f"{model_chip(m.get('model'))} · {dash(m.get('purpose'))} · attempt {dash(m.get('attempt_index'))}"
             + (f" · stakeholder <span class='mono'>{h(m.get('stakeholder_model'))}</span>" if m.get("stakeholder_model") else "") + "</p>")]
    if t["unresolved"] and not live:
        u = t["unresolved"]
        left.append(f"<p class='flag'>Unresolved attempt: no record.json was written. status {h(u['status'])}"
                    f"{' · error ' + h(u['error_type']) if u.get('error_type') else ''} · from {h(u['source_file'])}. Not counted in n, success or pass@k.</p>")
    pat = meter(p["end"], p["start"]) if p["start"] is not None else "<span class='muted'>no stakeholder session for this condition</span>"
    tier = f" · <span data-evaluator-only class='hidden'>last tier {h(p['tier'])}</span>" if p.get("tier") and evaluator else ""
    left.append(f"<div class='card small'><strong>Patience</strong> <span id='patience-live'>{pat}</span> <span class='muted'>questions {h(p['questions'])} · confirmations {h(p['confirmations'])} · "
                f"reviews {h(p['reviews'])}{tier}</span></div>")
    left.append(f"<p class='small'>{evaluator_switch(False, 'fact annotations, redactions, tool audit, task-sense labels, raw files', evaluator)}</p>")
    left.append("<div class='tabs' role='tablist'><button type='button' role='tab' id='tab-chat' aria-controls='panel-chat' data-tab='chat' aria-selected='true'>Conversation</button>"
                "<button type='button' role='tab' id='tab-timeline' aria-controls='panel-timeline' data-tab='timeline' aria-selected='false'>Timeline</button></div>")
    if live:
        left.append("<p class='note'>Trial is running: the stakeholder transcript is polled every 3 s. The agent's tool calls appear in the Timeline when the trial completes.</p>")
        if t["live_transcript_error"]:
            left.append(f"<p class='flag'>Live transcript unavailable: {h(t['live_transcript_error'])}</p>")
    left.append(f"<section data-tab-panel='chat' id='panel-chat' role='tabpanel' aria-labelledby='tab-chat'>{chat_bubbles(rows, live, evaluator, by_seq)}</section>")
    tl = "".join(timeline_row(r, evaluator) for r in rows) or "<p class='empty'>No timeline rows yet.</p>"
    if any(r.get("no_timestamp") for r in rows):
        tl += "<p class='muted small'>Rows marked <span class='flag'>no timestamp</span> are listed last; their position is not chronological evidence.</p>"
    left.append(f"<section data-tab-panel='timeline' id='panel-timeline' role='tabpanel' aria-labelledby='tab-timeline' class='timeline hidden'>{tl}</section>")
    brief = (f"<details open><summary class='muted small'>instruction.md as the agent saw it</summary><pre class='code'>{h(t['instruction'])}</pre></details>"
             if t["instruction"] else "<p class='empty'>Instruction not materialised yet; it appears when the trial task is prepared.</p>")
    patch_file = t.get("patch_file") or "verifier/patch.diff"
    legacy = " · <span class='flag small' title='pre-2026-09-13 artifact name; the grader now writes verifier/submission.diff'>legacy patch.diff</span>" if patch_file.endswith("patch.diff") else ""
    patch = (f"<details><summary class='muted small'>submitted patch ({len(_files_from_diff(t['patch']))} file(s)) · <code>{h(patch_file)}</code>{legacy}</summary>"
             f"{code_block(t['patch'], 'diff', diff=True, max_lines=600)}</details>"
             if t.get("patch") else "<p class='muted small'>No patch artifact collected for this trial (no <code>verifier/submission.diff</code>).</p>")
    cost = ""
    costs = t.get("costs") or []
    if isinstance(costs, list) and costs:
        cost = "<h2>Cost</h2>" + kv([(f"{c.get('role')} · {c.get('model')}", f"${float(c.get('usd') or 0):.4f} · {c.get('input_tokens')} in / {c.get('output_tokens')} out · {h(c.get('price_source'))}")
                                   for c in costs if isinstance(c, dict)]) + f"<p class='small'><a href='/trial/{h(tid)}/file/costs.json'>costs.json</a></p>"
    lineage = kv([("spec hash", ident(m.get("spec_hash"), 16, f"/scenario/{h(m.get('scenario_id'))}")), ("run config hash", ident(m.get("run_config_hash"), 16, f"/trial/{h(tid)}/file/run_config.json")),
                  ("compiled hash", ident(m.get("compiled_hash"), 16)), ("image digest", ident(m.get("image_digest"), 23)),
                  ("started", f"<span class='mono'>{dash(m.get('started_at'))}</span>"), ("finished", f"<span class='mono'>{dash(m.get('finished_at'))}</span>"),
                  ("origin / launcher", f"{dash(row.origin if row else None)} / {dash(row.launcher if row else None)}" + (f" · <span class='mono'>{h(row.job_name)}</span>" if row and row.job_name else "")),
                  ("workflow", f"<a class='mono' href='/workflows/{h(row.workflow_id)}'>{h(row.workflow_id)}</a>" if row and row.workflow_id else "—")])
    right = ["<div class='panel card'><h2>Brief</h2>", brief, "<h2>Patch</h2><div id='submitted-patch'>", patch, "</div><h2>Verifier</h2><div id='verifier-result'>", verifier_section(t, tid), "</div>", cost, "<h2>Lineage</h2>", lineage,
             "<h2>Reviewer label</h2>", label_form(t, tid), "<h2>Files</h2>", files_section(t, tid, resolve, evaluator), "</div>"]
    return f"<div class='two-col'><div>{''.join(left)}</div>{''.join(right)}</div>"
