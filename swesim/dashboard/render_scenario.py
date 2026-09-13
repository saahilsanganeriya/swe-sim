"""Scenarios list and scenario detail pages ("what tasks exist and how hard are they").

The list comes from ``reader.scenarios``; the detail page joins the reader row (versions, difficulty label, build hashes,
task-sense labels) with the one scenario's artifacts (``data.scenario_detail``: briefs with withheld spans, ledger by
discovery, control table vs validation, assets). Evaluator-only sections (author-only facts, hidden test assets,
judge/draft metadata, task-sense reasons) are rendered only when ``identity.evaluator`` and sit behind the toggle.
"""
from __future__ import annotations

from swesim.dashboard import origin
from swesim.dashboard.public import project

from swesim.dashboard.render import (
    Column, chip, dash, difficulty_chip, evaluator_switch, h, ident, kv, outcome_chip, short, table, when,
)

SCENARIO_COLUMNS = (
    Column("scenario_id", "scenario", "id", href="/scenario/{scenario_id}", n=28, filterable=True),
    Column("origin_kind", "task origin", "chip", title="source", filterable=True),
    Column("version", "v", "num"),
    Column("source", "source", "text", filterable=True),
    Column("source_ref", "ref", "mono"),
    Column("validation_status", "status", "chip", filterable=True),
    Column("difficulty_tier", "difficulty", "chip", filterable=True),
    Column("task_sense_label", "task sense", "chip"),
    Column("spec_hash", "spec", "id", href="/artifacts?path=scenarios/{scenario_id}/scenario.json"),
    Column("compiled_hash", "build", "id", href="/artifacts?path=scenarios/{scenario_id}/compile.json"),
    Column("image_digest", "image", "id", n=19),
    Column("updated_at", "updated", "time"),
)


def scenario_row_view(r) -> dict:
    d = r.model_dump(mode="json")
    ts = d.get("task_sense") or {}
    d["task_sense_label"] = None if not ts else ("coherent" if ts.get("coherent") and ts.get("fair_tests") else "review_needed")
    d["difficulty_tier"] = d.get("difficulty_tier") or None
    d["origin_kind"] = origin.badge_label(r.source)
    return project(d)


def scenarios_table(rows, total: int, offset: int, limit: int, keep=None) -> str:
    return table("scenarios", SCENARIO_COLUMNS, (scenario_row_view(r) for r in rows), key="scenario_id", source="/api/scenarios",
                 total=total, offset=offset, limit=limit, keep=keep,
                 empty="<strong>No scenarios yet.</strong> Start a creation job on <a href='/create'>Create</a>; rows appear as drafts are written.")


def scenarios_body(page, counts: dict, facet: str | None = None, query=()) -> str:
    """``facet`` is the task-origin key (``origin.PARAM``) and ``query`` the request's (key, value) pairs, carried by the facet bar
    and the paging links so the selection is sticky."""
    query = list(query)
    n_valid = sum(1 for r in page.items if r.validation_status == "valid")
    labelled = sum(1 for r in page.items if r.difficulty_tier)
    lead = (f"<p class='lead' data-count-line>{page.total} scenario version(s) · showing {h(origin.subset_text(facet))} · {n_valid} valid · {labelled} with a difficulty label. "
            "Difficulty is measured by the screen run (brief §5), never asserted from diff size.</p>")
    return ("<h1>Scenarios</h1>" + origin.facet_bar(facet, query, "/scenarios") + lead
            + scenarios_table(page.items, page.total, page.offset, page.limit, keep=origin.keep_query(query)))


# ----------------------------------------------------------------------------- detail

def _fact(f: dict) -> str:
    tests = ", ".join(h(t) for t in f.get("tests") or [])
    src = f.get("source_url")
    prov = (f"<a href='{h(src)}' target='_blank' rel='noopener'>{h(f.get('provenance'))}</a>" if src and str(src).startswith("http") else h(f.get("provenance")))
    excerpt = f"<div class='muted small'>excerpt: “{h(short(f.get('source_excerpt'), 160))}”</div>" if f.get("source_excerpt") else ""
    return (f"<div class='fact'><span class='fid'>{h(f.get('id'))}</span>{chip(f.get('kind'), 'info')} <span class='muted small'>{prov}"
            f"{' · tests ' + tests if tests else ''}</span><div>{h(f.get('text'))}</div>{excerpt}</div>")


def scenario_sections(d: dict, row, versions: list, trials_html: str, evaluator: bool) -> dict:
    """Pieces of the scenario page; the review page reuses briefs/ledger/controls."""
    sid = d["scenario_id"]
    art = f"/artifacts?path=scenarios/{sid}"
    src = d.get("source_ref") or ""
    src_link = f" · <a href='{h(src)}' target='_blank' rel='noopener'>source ↗</a>" if str(src).startswith("http") else ""
    env = d.get("environment") or {}
    head = [f"<h1>{h(sid)} {origin.badge(d.get('source') or (row.source if row else None))} <span class='mono'>v{h(d.get('version'))}</span></h1>",
            f"<p class='lead'>{chip(row.validation_status) if row else ''} {difficulty_chip(row.difficulty_tier if row else None)} "
            f"{dash(d.get('source'))} · <span class='mono'>{dash(src)}</span>{src_link}" + ("" if d["frozen"] else " · <span class='flag'>draft (not approved yet)</span>") + "</p>"]
    if versions and len(versions) > 1:
        vs = " ".join(f"<a class='chip{' info' if v.version == (row.version if row else None) else ' neutral'}' href='/scenario/{h(sid)}?version={v.version}'>v{v.version} · {h(v.validation_status)}</a>"
                      for v in versions)
        head.append(f"<p class='small'>Versions: {vs} <span class='muted'>(archived versions are read from <code>archive/v&lt;n&gt;/</code>)</span></p>")
    build = kv([
        ("spec hash", ident(d.get("spec_hash"), 16, f"{art}/scenario.json")),
        ("compiled hash", ident(row.compiled_hash if row else d.get("compile", {}).get("compiled_hash"), 16, f"{art}/compile.json")),
        ("image digest", ident(row.image_digest if row else None, 23, f"{art}/validation.json")),
        ("repository", f"<span class='mono'>{dash(env.get('repo_url'))}</span> @ {ident(env.get('base_commit'), 12)}"),
        ("contract", f"{d['contract']['fail_to_pass']} fail-to-pass · {d['contract']['pass_to_pass']} pass-to-pass · {d['contract']['protected_paths']} protected path(s)"),
        ("requirements / groups / checks", f"{len(d['requirements'])} / {len(d['groups'])} / {len(d['checks'])}"),
        ("created", when(d.get("created_at"))),
    ])
    # difficulty label (brief §5)
    diff = d.get("difficulty") or {}
    if diff:
        hid, full = diff.get("hidden") or {}, diff.get("full") or {}
        difficulty = (f"<h2>Difficulty</h2><p>{difficulty_chip(diff.get('tier'))} screen model <span class='mono'>{dash(diff.get('model'))}</span> · hidden "
                      f"{dash(hid.get('successes'))}/{dash(hid.get('n'))} · full {dash(full.get('successes'))}/{dash(full.get('n'))} · labelled {when(diff.get('labelled_at'))} · "
                      f"<a href='{art}/difficulty.json'>difficulty.json</a></p>")
    else:
        difficulty = ("<h2>Difficulty</h2><p class='empty'>Not screened yet. Run <em>purpose = screen</em> on <a href='/run'>Run</a> (hidden k=2, full k=1 with the screen model); "
                      "the label lands in <code>difficulty.json</code>.</p>")
    ts = row.task_sense if row and isinstance(row.task_sense, dict) else None
    if ts:
        sense = (f"<h2>Task sense</h2><p>{chip('coherent' if ts.get('coherent') else 'not coherent', 'good' if ts.get('coherent') else 'critical')} "
                 f"{chip('fair tests' if ts.get('fair_tests') else 'unfair tests', 'good' if ts.get('fair_tests') else 'critical')} "
                 f"{chip('plausible withholding' if ts.get('plausible_withholding') else 'implausible withholding', 'good' if ts.get('plausible_withholding') else 'serious')} "
                 f"<span class='muted small'>audited {when(ts.get('audited_at'))} by <span class='mono'>{dash(ts.get('model'))}</span></span></p>")
        if evaluator and isinstance(ts.get("reasons"), dict):
            sense += "<div data-evaluator-only><dl class='kv'>" + "".join(f"<dt>{h(k)}</dt><dd>{h(v)}</dd>" for k, v in ts["reasons"].items()) + "</dl></div>"
        if ts.get("usage_complete") is not True or ts.get("cost_recorded") is not True:
            sense += "<p class='notice'>Audit cost accounting is incomplete; missing usage remains unknown.</p>"
    else:
        sense = "<h2>Task sense</h2><p class='empty'>No audit labels yet; the task-sense audit writes <code>audit.json</code> after validation.</p>"
    # briefs
    natural = "".join(f"<mark class='removed' title='withheld from the withheld brief'>{h(seg)}</mark>" if removed else h(seg) for seg, removed in d["natural_segments"])
    withheld = f"<pre>{h(d['withheld'])}</pre>" if d.get("withheld") else "<p class='empty'>No withheld brief: hidden and interactive runs use the natural brief.</p>"
    briefs = (f"<h2>Briefs</h2><p class='legend-inline'><mark class='removed'>highlighted</mark> spans are the original-issue excerpts removed from the withheld brief "
              f"({len(d['removed_excerpts'])} withholding operation(s)).</p><div class='briefs'>"
              f"<div class='brief'><h3>natural <span class='muted small'>original issue</span></h3><pre>{natural}</pre></div>"
              f"<div class='brief'><h3>withheld <span class='muted small'>what hidden/interactive agents see</span></h3>{withheld}</div></div>")
    ops = d.get("operations") or []
    if ops:
        briefs += (f"<details><summary>{len(ops)} operation(s)</summary><ul class='small'>"
                   + "".join(f"<li>{chip(o.get('type'), 'info')} target <span class='mono'>{h(o.get('target'))}</span> — {h(o.get('rationale'))}</li>" for o in ops) + "</ul></details>")
    # ledger
    order = ("brief", "on-request", "volunteered-at-review", "repo-derivable", "author-only")
    groups = d["ledger_groups"]
    toggle = evaluator_switch(False, "author-only facts, hidden assets, judge and draft metadata", evaluator)
    ledger = [f"<h2>Ledger</h2><p class='muted small'>{sum(len(v) for v in groups.values())} fact(s) visible · grouped by discovery. {toggle}</p>"]
    if not groups:
        ledger.append("<p class='empty'>No ledger facts in this spec.</p>")
    for disc in [x for x in order if x in groups] + [x for x in groups if x not in order]:
        facts = groups[disc]
        gated = " data-evaluator-only class='hidden'" if disc == "author-only" else ""
        flag = " <span class='flag small'>evaluator-only</span>" if disc == "author-only" else ""
        ledger.append(f"<div{gated}><h3>{h(disc)} <span class='muted small'>{len(facts)}</span>{flag}</h3><div class='card'>{''.join(_fact(f) for f in facts)}</div></div>")
    if not evaluator and d.get("hidden_author_only"):
        ledger.append(f"<p class='muted small'>{d['hidden_author_only']} author-only fact(s) are evaluator-only and not rendered for this identity.</p>")
    # controls
    v = d.get("validation") or {}
    vstatus = v.get("status")
    vline = (f" · build {ident(v.get('compiled_hash'), 12, f'{art}/validation.json')} · image {ident(v.get('image_digest'), 19)}" if v
             else " · validation runs nop, oracle, every control and the reachability probe on Modal")
    controls = [f"<h2>Controls and validation</h2><p class='lead'>{chip(vstatus or 'not validated', None if vstatus else 'neutral')}{vline}</p>"]
    if d.get("failures"):
        controls.append("<ul class='small'>" + "".join(f"<li class='flag'>{h(f)}</li>" for f in d["failures"]) + "</ul>")
    rows = []
    for c in d["controls"]:
        exp = ", ".join(f"{h(k.replace('expected_', ''))}={h(val)}" for k, val in (c.get("expected") or {}).items()) or "—"
        result = "<span class='muted'>not run</span>" if not c["ran"] else f"{outcome_chip(c.get('outcome'), c.get('success'))} reward {h(c.get('reward'))} · success {h(c.get('success'))}"
        verdict = chip("failed", "critical", "; ".join(c["failed"])) if c["failed"] else (chip("as expected", "good") if c["ran"] else chip("pending", "run"))
        asset = f"<a class='mono small' href='{art}/assets/{h(c.get('asset'))}'>{h(c.get('asset'))}</a>" if c.get("asset") and evaluator else ("<span class='muted small'>evaluator-only</span>" if c.get("asset") else "—")
        rows.append(f"<tr><td class='mono'>{h(c['name'])}</td><td>{chip(c['cls'], 'info')}</td><td>{asset}</td><td class='small'>{exp}</td><td>{result}</td><td>{verdict}</td>"
                    f"<td class='small muted'>{dash(c.get('rationale'))}</td></tr>")
    controls.append("<div class='table-wrap'><table><thead><tr><th>control</th><th>class</th><th>asset</th><th>expected</th><th>result</th><th>verdict</th><th>rationale</th></tr></thead>"
                    f"<tbody>{''.join(rows)}</tbody></table></div>")
    # evaluator-only: judge, draft meta, hidden assets
    judge = d.get("judge") or {}
    jud = ""
    if evaluator and judge:
        gaps = judge.get("gaps") or []
        jud = (f"<div data-evaluator-only class='hidden'><h2>Judge <span class='flag small'>evaluator-only</span></h2><p class='lead'>"
               f"{chip('keep' if judge.get('keep') else 'reject', 'good' if judge.get('keep') else 'critical')} rubric {h(judge.get('verified_rubric_score'))}/3 · complexity "
               f"{h(judge.get('complexity'))} · behavioralness {h(judge.get('test_behavioralness'))} · self-contained {h(judge.get('self_contained'))} · "
               f"<span class='mono'>{h(judge.get('model_id'))}</span></p>" + (f"<p class='muted small'>{h(judge.get('rationale'))}</p>" if judge.get("rationale") else "")
               + (f"<details><summary>{len(gaps)} gap(s)</summary><ul class='small'>" + "".join(
                   f"<li>{chip(g.get('category'), 'info')} {h(g.get('text'))} <span class='muted'>· consequential {h(g.get('consequential'))} · resolved by {h(g.get('resolved_by'))}</span></li>" for g in gaps)
                   + "</ul></details>" if gaps else "") + "</div>")
    assets = d.get("assets") or {}
    asset_rows = "".join(f"<tr><td class='mono'>{h(n)}</td><td class='mono small'>{h(a.get('path'))}</td><td class='num'>{h(a.get('bytes'))}</td><td>{ident(a.get('sha256'), 16)}</td></tr>"
                         for n, a in sorted(assets.items()) if isinstance(a, dict))
    assets_html = ("<h2>Assets</h2>" + (f"<div class='table-wrap'><table><thead><tr><th>asset</th><th>path</th><th class='num'>bytes</th><th>sha256</th></tr></thead><tbody>{asset_rows}</tbody></table></div>"
                                        if asset_rows else "<p class='empty'>No public assets in the manifest.</p>")
                   + (f"<p class='muted small'>{d['hidden_assets']} hidden test/control asset(s) are evaluator-only.</p>" if d.get("hidden_assets") else ""))
    files = "<p class='small'>" + " · ".join(f"<a href='{art}/{h(n)}'>{h(n)}</a>" for n in d.get("files") or []) + f" · <a href='{art}'>browse directory</a></p>"
    return {"head": "".join(head), "build": build, "difficulty": difficulty, "sense": sense, "briefs": briefs, "ledger": "".join(ledger),
            "controls": "".join(controls), "judge": jud, "assets": assets_html, "files": files, "trials": trials_html}


def scenario_body(d: dict, row, versions: list, trials_html: str, evaluator: bool) -> str:
    s = scenario_sections(d, row, versions, trials_html, evaluator)
    return (s["head"] + s["build"] + s["difficulty"] + s["sense"] + "<h2>Trials on this scenario</h2>" + s["trials"] + s["briefs"] + s["ledger"] + s["controls"]
            + s["judge"] + s["assets"] + s["files"])
