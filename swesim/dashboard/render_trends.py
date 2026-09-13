"""Trends: the capability-gradient view, plus the historical curves; every value comes from public measurement rows.

The page answers "is this task set hard in the right way?": accuracy by model ordered by solver tier A→E per arm
(condition × brief profile), a per-scenario matrix with the gradient chip (holds · suspicious · saturated · not
monotone), the three condition deltas per model, cost per solved task and per trial, integrity flags and asking
behaviour. Every chart has a table twin and per-mark tooltips; colour = model family (fixed slot), sequential blue for
accuracy cells (``static/tokens.css``). Fragments carry stable ids so ``app.js`` preserves each chart/table choice on
refresh, and use only the element vocabulary ``patchCharts`` accepts.
"""
from __future__ import annotations

from swesim.dashboard import charts, gradient, origin, pages
from swesim.dashboard.render import TIER_LABELS, TIERS, chip, difficulty_chip, h, model_chip

METRICS = (
    ("pass_rate", "Pass rate as scenarios accrue", "n", "", 1.0),
    ("questions_per_trial", "Questions per trial", "questions_n", "", None),
    ("confirmations_per_trial", "Confirmations per trial", "confirmations_n", "", None),
    ("cost_per_success", "USD per solved attempt", "cost_known_n", " USD", None),
)
GRADIENT_CHIPS: dict[str, tuple[str, str]] = {"holds": ("gradient holds", "good"), "suspicious": ("suspicious", "serious"),
                                              "saturated": ("saturated", "neutral"), "not-monotone": ("not monotone", "neutral")}


def gradient_chip(state: str | None, reason: str | None = None) -> str:
    """The informational gradient chip of one scenario row (never a gate)."""
    if not state:
        return f"<span class='muted small' title='{h(reason or '')}'>one tier</span>"
    text, kind = GRADIENT_CHIPS.get(state, (state, "neutral"))
    return chip(text, kind, reason)


def _usd(v: float) -> str:
    return f"${v:,.2f}" if v >= 0.01 or v == 0 else f"${v:.4f}"


def _arm(condition: str, profile: str) -> str:
    return f"{condition} · {profile}"


def cohort_fragments(c: dict) -> dict[str, str]:
    """The six blocks of one evaluation cohort, keyed by stable fragment id."""
    key = c["evaluation_cohort"]
    frags: dict[str, str] = {}
    frags[f"gradient-{key}-head"] = (f"<h2>{h(c['cohort_label'])}</h2><p class='muted small'>{c['trial_count']} admitted trials · {len(c['models'])} model(s): "
                                     + ", ".join(f"{h(charts.short_model(m))} ({h(gradient.tier_label(m))})" for m in c["models"]) + "</p>")
    # (a) accuracy by model, tier A→E, one chart per arm
    arms = sorted({(x["condition"], x["brief_profile"]) for x in c["accuracy"]}, key=gradient._arm_order)
    for condition, profile in arms:
        cid = f"gradient-{key}-{condition}-{profile}-accuracy"
        cells = [x for x in c["accuracy"] if x["condition"] == condition and x["brief_profile"] == profile]
        frags[cid] = f"<h3>{h(_arm(condition, profile))}: accuracy by model, tier A→E</h3>" + charts.tier_bars(
            cells, chart_id=cid, title=f"{_arm(condition, profile)}: accuracy by model and tier")
    # (b) per-scenario matrix with the gradient chip
    for m in c["matrices"]:
        condition, profile = m["condition"], m["brief_profile"]
        cid = f"gradient-{key}-{condition}-{profile}-matrix"
        rows = m["scenarios"]
        by_sid = {r["scenario_id"]: r for r in rows}
        flags = " ".join(f"{gradient_chip(state)} <span class='small muted'>{m['flags'][state]}</span>" for state in gradient.GRADIENT_STATES if m["flags"].get(state))
        summary = (f"<p class='small'>{len(rows)} scenario(s) · {len(m['models'])} model(s) · " + (flags or "<span class='muted'>no scenario has two tiers with data yet</span>") + "</p>")

        def cell_of(sid, model, _by=by_sid):
            cell = _by[sid]["cells"].get(model)
            if not cell:
                return None, 0, f"{sid} · {model}: no admitted trials"
            return cell["accuracy"], cell["n"], f"{sid} · {model} (tier {gradient.tier_label(model)}): {cell['success_count']}/{cell['n']} solved"

        def row_label(sid, _by=by_sid):
            state = _by[sid]["gradient"]["state"]
            return f"{sid} · {GRADIENT_CHIPS[state][0] if state else 'one tier'}"

        def row_html(sid, _by=by_sid):
            r = _by[sid]
            return f"<a href='/scenario/{h(sid)}'>{h(sid)}</a> {origin.badge(r.get('source'))} {difficulty_chip(r.get('difficulty_tier'))}"

        frags[cid] = f"<h3>{h(_arm(condition, profile))}: per-scenario matrix</h3>" + summary + charts.matrix(
            [r["scenario_id"] for r in rows], m["models"], cell_of, chart_id=cid, title=f"{_arm(condition, profile)}: solved/n per scenario × model (tier order)",
            col_label=lambda model: f"{charts.short_model(model, 16)} · {gradient.tier_label(model)}", row_label=row_label, row_html=row_html,
            cell_text=lambda sid, model, _v, n, _by=by_sid: f"{_by[sid]['cells'][model]['success_count']}/{n}",
            extra=("gradient", lambda sid, _by=by_sid: gradient_chip(_by[sid]["gradient"]["state"], _by[sid]["gradient"]["reason"])),
            caption="Sequential single hue: darker is a higher accuracy; every cell prints solved/n, so n is visible everywhere. Columns run tier A→E; "
                    "expected: cells darken left to right. The gradient chip is informational: suspicious = a tier A/B model beats the best tier D/E model by more "
                    "than one trial's worth at these n; saturated = every model with n ≥ 2 is at 100%.")
    # (c) the three deltas per model
    cid = f"gradient-{key}-deltas"
    frags[cid] = "<h3>Condition deltas per model: full − hidden, interactive − hidden, full − interactive</h3>" + charts.delta_bars(
        c["deltas"], chart_id=cid, title="condition deltas per model (percentage points)")
    # (d) cost per solved task and per trial
    cost_columns = [("model", "model", "text"), ("tier", "tier", "text"), ("n", "n", "link"), ("cost_known_n", "with cost", "num"), ("success_count", "solved", "num"),
                    ("cost_usd", "USD total", "num"), ("cost_per_trial", "USD per trial", "num"), ("cost_per_success", "USD per solved task", "num"),
                    ("cost_per_success_reason", "why unknown", "text")]
    cid = f"gradient-{key}-cost-solved"
    frags[cid] = "<h3>Cost per solved task by model</h3>" + charts.model_hbars(
        c["cost"]["by_model"], metric="cost_per_success", chart_id=cid, title="USD per solved task", fmt=_usd, reason_key="cost_per_success_reason", columns=cost_columns,
        caption="All conditions of the cohort pooled per model (cost is a property of the trial); known only when every trial has a recorded cost and at least one succeeded. "
                "The table below the next chart splits the same figures by condition.")
    cid = f"gradient-{key}-cost-trial"
    frags[cid] = "<h3>Cost per trial by model</h3>" + charts.model_hbars(
        c["cost"]["by_model"], metric="cost_per_trial", chart_id=cid, title="USD per trial", fmt=_usd, reason_key="cost_per_success_reason", columns=cost_columns,
        caption="Mean over the trials with a recorded cost (coverage in the table).")
    by_mc = c["cost"]["by_model_condition"]
    if by_mc:
        rows = "".join(f"<tr><td>{model_chip(r['model'])}</td><td>{h(r['condition'])}</td><td class='num'><a href='{h(r['evidence_url'])}'>{r['n']}</a></td>"
                       f"<td class='num'>{r['cost_known_n']}</td><td class='num'>{r['success_count']}</td>"
                       f"<td class='num'>{_usd(r['cost_usd']) if r['cost_usd'] is not None else '—'}</td>"
                       f"<td class='num'>{_usd(r['cost_per_trial']) if r['cost_per_trial'] is not None else '—'}</td>"
                       f"<td class='num'>{_usd(r['cost_per_success']) if r['cost_per_success'] is not None else '—'}</td><td class='muted'>{h(r['cost_per_success_reason'] or '')}</td></tr>"
                       for r in by_mc)
        frags[f"gradient-{key}-cost-table"] = ("<div class='table-wrap'><table><caption>Cost by model and condition (tier order)</caption><thead><tr><th>model</th><th>condition</th>"
                                               "<th class='num'>n</th><th class='num'>with cost</th><th class='num'>solved</th><th class='num'>USD total</th><th class='num'>USD per trial</th>"
                                               f"<th class='num'>USD per solved task</th><th>why unknown</th></tr></thead><tbody>{rows}</tbody></table></div>")
    # (e) integrity flags per model
    cid = f"gradient-{key}-integrity"
    rows = "".join(f"<tr><td>{model_chip(r['model'])}</td><td class='num'>{r['n']}</td><td class='num'>{r['known_n']}</td>"
                   f"<td class='num'>{(chip(str(r['flagged_n']), 'serious') if r['flagged_n'] else '0')}</td><td class='num'>{r['signals']}</td>"
                   f"<td class='num'>{f'{r[chr(114) + chr(97) + chr(116) + chr(101)]:.2f}' if r['rate'] is not None else '—'}</td>"
                   f"<td>{('<a href=' + chr(39) + h(r['evidence_url']) + chr(39) + '>flagged trials</a>') if r['flagged_n'] else '<span class=' + chr(39) + 'muted' + chr(39) + '>none</span>'}</td></tr>"
                   for r in c["integrity"])
    frags[cid] = ("<h3>Integrity flags per model</h3><p class='muted small'>Trials whose grader report carries at least one integrity signal (forged reports, protected-path "
                  "touches, config edits). Count and rate over trials with a known signal count; unknown stays unknown.</p>"
                  "<div class='table-wrap'><table><thead><tr><th>model</th><th class='num'>trials</th><th class='num'>with known count</th><th class='num'>flagged</th>"
                  f"<th class='num'>signals</th><th class='num'>flag rate</th><th>evidence</th></tr></thead><tbody>{rows}</tbody></table></div>")
    # (f) asking behaviour on the interactive arms
    cid = f"gradient-{key}-asking"
    if c["asking"]:
        frags[cid] = "<h3>Questions per interactive trial and asked-before-implementing rate</h3>" + charts.model_hbars(
            c["asking"], metric="questions_per_trial", chart_id=cid, title="questions per interactive trial", fmt=lambda v: f"{v:.2f}",
            label=lambda r: f"{charts.short_model(r['model'], 18)} · {r['tier']} · {r['brief_profile']}",
            columns=[("model", "model", "text"), ("tier", "tier", "text"), ("brief_profile", "brief", "text"), ("n", "n", "link"), ("questions_known_n", "questions known", "num"),
                     ("questions_per_trial", "questions per trial", "num"), ("asked_any_rate", "asked-at-all rate", "num"), ("asked_first_labelled_n", "audit labels", "num"),
                     ("asked_first_rate", "asked-before-implementing rate", "num")],
            caption="Questions use known counts only; the asked-before-implementing rate exists only where the task-sense audit labelled the transcript (labels column).")
    else:
        frags[cid] = "<h3>Questions per interactive trial</h3><p class='empty'>No interactive trials in this cohort yet.</p>"
    return frags


def history_fragments(data: dict) -> dict[str, str]:
    """The cumulative-by-day curves (pass rate, questions, confirmations, cost per solved attempt, asked vs did not ask) per arm and cohort."""
    fragments: dict[str, str] = {}
    pairs = sorted({(row["condition"], row["brief_profile"], row["evaluation_cohort"], row["cohort_label"]) for row in data["history"]})
    for condition, profile, evaluation, cohort_label in pairs:
        selected = [row for row in data["history"] if row["condition"] == condition and row["brief_profile"] == profile and row["evaluation_cohort"] == evaluation]
        cohort = [row for row in data["interaction"] if row["condition"] == condition and row["brief_profile"] == profile and row["evaluation_cohort"] == evaluation]
        for metric, metric_label, nkey, unit, maximum in METRICS:
            cid = f"trend-{condition}-{profile}-{evaluation}-{metric}"
            title = f"{condition} · {profile}: {metric_label} · {cohort_label}"
            fragments[cid] = f"<h3>{h(title)}</h3>" + charts.history(selected, metric=metric, chart_id=cid, title=title, sample_key=nkey, unit=unit, ymax=maximum)
        cid = f"trend-{condition}-{profile}-{evaluation}-asking"
        title = f"{condition} · {profile}: asked versus did not ask · {cohort_label}"
        fragments[cid] = f"<h3>{h(title)}</h3>" + charts.history(cohort, metric="pass_rate", chart_id=cid, title=title, ymax=1.0)
    return fragments


def chart_fragments(data: dict) -> dict[str, str]:
    """Stable chart IDs let the browser preserve each chart/table choice during refresh. Order: the gradient per cohort,
    then the history curves, then the screen-label distribution (informational)."""
    fragments: dict[str, str] = {}
    cohorts = data.get("gradient", {}).get("cohorts", [])
    if not cohorts:
        fragments["gradient-empty"] = ("<p class='empty'><strong>No admitted trials yet.</strong> The gradient appears once completed trials exist on valid, audited scenarios "
                                       "for this purpose and facet. Start a run on <a href='/run'>Run</a>.</p>")
    for c in cohorts:
        fragments.update(cohort_fragments(c))
    history = history_fragments(data)
    if history:
        fragments["history-head"] = "<h2>As scenarios accrued</h2><p class='muted small'>Cumulative through each UTC completion day; one curve per model, split by arm and cohort.</p>"
        fragments.update(history)
    difficulty = charts.hbars([(row["label"], row["n"]) for row in data["difficulty"]], "trend-difficulty", "Screen labels on current valid scenarios")
    links = "<p class='small'>" + " · ".join(f"<a href='{h(row['evidence_url'])}'>{h(row['label'])}: {row['n']}</a>" for row in data["difficulty"]) + "</p>"
    fragments["trend-difficulty"] = ("<h2>Screen labels (informational)</h2><p class='muted small'>What the one-model difficulty screen said about each current valid scenario. "
                                     "The label filters the board and this page when applied; it never excludes by default. The gradient above is the difficulty evidence.</p>"
                                     + difficulty + links)
    return fragments


def body(data: dict, current: dict) -> str:
    select = pages._filter_select
    facet = current.get("pool")
    query = [(k, v) for k, vs in data.get("query", {}).items() for v in (vs if isinstance(vs, list) else [vs])]
    filters = ("<form class='toolbar' action='/trends' method='get'>" + origin.hidden_input(facet)
               + pages.cohort_select(current)
               + select("purpose", ["comparison", "calibration", "demonstration", "screen"], [current["purpose"]], "purpose")
               + select("condition", ["full", "hidden", "interactive"], current.get("condition"), "condition")
               + select("brief_profile", ["natural", "withheld"], current.get("brief_profile"), "brief profile")
               + select("source", ["github-repo", "swebench-annotated", "authored"], current.get("source"), "source")
               + select("tier", ["A", "B", "C", "D", "E"], current.get("tier"), "model tier")
               + pages.difficulty_checkboxes(list(current.get("difficulty_tier") or []))
               + "<button class='button' type='submit'>Apply</button><a class='button' href='/trends'>Reset</a></form>")
    frontier = ", ".join(charts.short_model(m) for m in TIERS["E"])
    tiers = " · ".join(f"<strong>{t}</strong> {h(TIER_LABELS[t])} ({len(TIERS[t])})" for t in TIERS)
    return (f"<h1>Trends <span class='muted' data-subset>· {h(origin.subset_text(facet))}</span></h1>"
            + origin.facet_bar(facet, query, "/trends")
            + "<p class='lead'><strong>Capability gradient.</strong> Is this task set hard in the right way? The generation axis is the solver tier of the brief: "
            f"{tiers}. Frontier models ({h(frontier)}) and the mid-frontier tier should score high — the target is 80–90% — while last-generation open models "
            "(tier A) should not match them. Accuracy is expected to be <em>non-decreasing with tier</em>; a scenario where a tier A/B model beats the best tier D/E model "
            "by more than one trial's worth is chipped <em>suspicious</em>, and one where every model with n ≥ 2 scores 100% is chipped <em>saturated</em>. "
            "Chips are informational; nothing here is excluded by a difficulty label unless you apply the filter. Every cell carries its n and nothing is shown below n = 1; "
            "purposes, conditions, brief profiles and evaluation cohorts are never pooled.</p>" + filters
            + f"<p class='note' id='trend-coverage'>{coverage(data)}</p>"
            + "<p class='muted small'>Rates are descriptive, not causal. Deltas are computed on the scenarios two arms share for a model. Cost per solved task needs a recorded cost on every "
            "trial of the cell and at least one success. Integrity counts use the grader's signals; unknown stays unknown. Select any n or bar for the supporting trials.</p>"
            + "<div id='trend-charts' data-live-charts>" + "".join(data["charts"].values()) + "</div>")


def coverage(data: dict) -> str:
    g = data.get("gradient", {})
    tiers_present = sorted({gradient.tier_label(m) for m in g.get("models", [])}, key=lambda t: (t == "other", t))
    return (f"{data['trial_count']} admitted attempts on {g.get('scenario_count', 0)} scenario(s) · {len(g.get('models', []))} model(s) across tier(s) "
            f"{', '.join(tiers_present) or '—'} · {len(g.get('cohorts', []))} evaluation cohort(s), shown separately · "
            f"{data['undated_trials']} without a completion timestamp are excluded from the history curves; {data['unknown_questions']} have unknown question counts. "
            "Charts and tables refresh together.")
