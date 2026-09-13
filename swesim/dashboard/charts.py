"""Inline SVG charts, generated server-side, no library. Rules from the dataviz skill (design spec §3–§4):

- Form follows the data's job: grouped bars for identity (model families, fixed categorical slot per family, never
  cycled), horizontal stacked bars in status tokens for the creation funnel, a sequential heat matrix for magnitudes,
  single-hue bars for a histogram (nominal categories: one colour, order carried by position).
- One axis per chart; ≤ 24-px bars with a 4-px rounded cap square at the baseline; 2-px surface gaps; hairline solid
  grid; legend whenever ≥ 2 series plus selective direct labels; per-mark ``<title>`` tooltips with keyboard focus.
- Every figure ships a table twin (``figure()`` wraps chart + table behind chart/table tabs); text never wears a series
  colour (labels use ink tokens; the swatch beside them carries identity). Colours come from ``tokens.css`` classes only.
"""
from __future__ import annotations

from decimal import Decimal
from datetime import date
from html import escape

from swesim.dashboard.render import family_of

CONDITIONS = ("full", "hidden", "interactive")
MAX_FAMILIES_PER_AXIS = 3  # all-pairs cap from the palette validation (design spec §3)


def _h(x) -> str:
    return escape("" if x is None else str(x), quote=True)


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def figure(title: str, svg: str, table_html: str, caption: str, chart_id: str) -> str:
    """Chart + table twin behind tabs; the table is the WCAG-clean equivalent and is always present."""
    return (f"<figure class='chart-wrap' id='{_h(chart_id)}' role='group' aria-label='{_h(title)}'>"
            f"<div class='chart-tabs' role='tablist' data-chart-tabs><button type='button' role='tab' aria-selected='true' data-view='chart'>Chart</button>"
            f"<button type='button' role='tab' aria-selected='false' data-view='table'>Table</button></div>"
            f"<div data-view='chart'>{svg}</div><div data-view='table' class='hidden'>{table_html}</div>"
            f"<figcaption class='muted small'>{caption}</figcaption></figure>")


def _rounded_bar(x: float, top: float, bottom: float, w: float) -> str:
    hgt = max(0.0, bottom - top)
    r = min(4.0, hgt / 2, w / 2)
    return (f"M{x:.1f},{bottom:.1f} V{top + r:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} H{x + w - r:.1f} "
            f"Q{x + w:.1f},{top:.1f} {x + w:.1f},{top + r:.1f} V{bottom:.1f} Z")


# ----------------------------------------------------------------------------- grouped bars: metric per model per condition

def grouped_bars(rows: list[dict], *, metric: str = "pass_at_k", chart_id: str = "chart-pass", title: str | None = None, ymax: float = 1.0) -> str:
    """``rows``: dicts with ``model``, ``condition``, ``<metric>`` (0..1 or None), ``n`` and optional ``label``.
    One bar per (condition, model); colour = model family (fixed slot); more than three families fold to the table."""
    rows = [r for r in rows if r.get("model") and r.get("condition")]
    if not rows:
        return "<p class='empty'>No scored cells yet. Cells appear when a group has valid attempts.</p>"
    models = sorted({r["model"] for r in rows}, key=lambda m: (family_of(m)[2] or 99, m))
    fams = []
    for m in models:
        f = family_of(m)[0]
        if f not in fams:
            fams.append(f)
    shown_fams = fams[:MAX_FAMILIES_PER_AXIS]
    shown = [m for m in models if family_of(m)[0] in shown_fams]
    folded = [m for m in models if m not in shown]
    conditions = [c for c in CONDITIONS if any(r["condition"] == c for r in rows)] + sorted({r["condition"] for r in rows if r["condition"] not in CONDITIONS})
    pad_l, pad_r, pad_t, pad_b = 40, 12, 18, 30
    bar_w, gap, slot_gap, plot_h = 18, 2, 24, 140
    widths = {c: max(100, len(shown) * (bar_w + gap) + slot_gap) for c in conditions}
    width = pad_l + sum(widths.values()) + pad_r
    height = pad_t + plot_h + pad_b
    y = lambda v: pad_t + plot_h * (1 - v / ymax)  # noqa: E731
    parts = [f"<svg class='chart' viewBox='0 0 {width} {height}' width='{width}' height='{height}' role='img' aria-label='{_h(title or metric)}'>"]
    for tick in (0.0, ymax / 2, ymax):
        parts.append(f"<line class='grid' x1='{pad_l}' x2='{width - pad_r}' y1='{y(tick):.1f}' y2='{y(tick):.1f}'/>")
        parts.append(f"<text class='tick' x='{pad_l - 6}' y='{y(tick) + 3:.1f}' text-anchor='end'>{tick:g}</text>")
    parts.append(f"<line class='axis' x1='{pad_l}' x2='{width - pad_r}' y1='{y(0):.1f}' y2='{y(0):.1f}'/>")
    x = pad_l
    for cond in conditions:
        parts.append(f"<text class='cat' x='{x + widths[cond] / 2:.1f}' y='{height - 10}' text-anchor='middle'>{_h(cond)}</text>")
        bx = x + (widths[cond] - len(shown) * (bar_w + gap)) / 2
        # selective direct label: the extreme (maximum) of each condition group; the rest live in tooltip + table
        vals = [_num(r.get(metric)) for r in rows if r["condition"] == cond]
        vmax = max((v for v in vals if v is not None), default=None)
        for m in shown:
            slot = family_of(m)[2]
            cell = next((r for r in rows if r["condition"] == cond and r["model"] == m), None)
            v = _num(cell.get(metric)) if cell else None
            label = f"{cond} · {m}: {metric} " + (f"{v:.2f}" if v is not None else "n/a") + (f" · n={cell.get('n')}" if cell and cell.get("n") is not None else "")
            if v is not None:
                top, bottom = y(max(0.0, min(ymax, v))), y(0)
                parts.append(f"<g class='bar fam-{slot}' tabindex='0'><title>{_h(label)}</title><path d='{_rounded_bar(bx, top, bottom, bar_w)}'/>"
                             + (f"<text class='lbl' x='{bx + bar_w / 2:.1f}' y='{top - 3:.1f}' text-anchor='middle'>{v:.2f}</text>" if vmax is not None and v == vmax else "")
                             + "</g>")
            elif cell is not None:
                parts.append(f"<g class='bar na' tabindex='0'><title>{_h(label)}</title><rect x='{bx:.1f}' y='{y(0) - 1:.1f}' width='{bar_w}' height='1'/>"
                             f"<text x='{bx + bar_w / 2:.1f}' y='{y(0) - 5:.1f}' text-anchor='middle'>n/a</text></g>")
            bx += bar_w + gap
        x += widths[cond]
    parts.append("</svg>")
    legend = ("<ul class='legend'>" + "".join(f"<li><i class='swatch fam-{family_of(m)[2]}' aria-hidden='true'></i>{_h(m)}</li>" for m in shown) + "</ul>") if len(shown) >= 2 else ""
    table = ("<div class='table-wrap'><table><thead><tr><th>model</th><th>condition</th><th class='num'>" + _h(metric) + "</th><th class='num'>n</th></tr></thead><tbody>"
             + "".join(f"<tr><td class='mono'>{_h(r['model'])}</td><td>{_h(r['condition'])}</td><td class='num'>{_h(f'{_num(r.get(metric)):.3f}') if _num(r.get(metric)) is not None else 'n/a'}</td>"
                       f"<td class='num'>{_h(r.get('n'))}</td></tr>" for r in sorted(rows, key=lambda r: (r['model'], r['condition']))) + "</tbody></table></div>")
    cap = f"{len(rows)} cell(s); one bar per condition × model, colour = model family (fixed slot)." + (f" {len(folded)} model(s) of a fourth+ family in the table only: {_h(', '.join(folded))}." if folded else "")
    return figure(title or metric, "".join(parts) + legend, table, cap, chart_id)


# ----------------------------------------------------------------------------- funnel: stacked horizontal bars in status tokens

# The five terminal states of a candidate version (`swesim.index.reports.funnel`), their status token class and legend label.
FUNNEL_STATES: tuple[tuple[str, str, str], ...] = (
    ("kept", "good", "kept: continues to the next stage (at the last stage: registered)"),
    ("rejected", "critical", "rejected at this gate"),
    ("error", "serious", "error: the infrastructure failed here"),
    ("pending", "neutral", "pending: kept here, never advanced (not-selected, cap-cut, workflow-stopped)"),
    ("superseded", "warn", "superseded: a later version replaced it"),
)
# One line per stage: what "rejected here" means.
FUNNEL_GATES: dict[str, str] = {
    "mined": "no row is rejected here; PRs that discovery filtered (not merged in range, no linked issue, diff or thread size) are in the rejects histogram, and pending means mined but never judged.",
    "judged": "the judge did not keep it: not a behavioural bug fix, not underspecified in a way a stakeholder could resolve, or too large to reproduce.",
    "probed": "FAIL_TO_PASS did not reproduce offline (nothing fails on base and passes on gold), or the probe build failed.",
    "drafted": "the draft failed a pre-check: schema, missing assets, or hidden facts leaking into the brief.",
    "approved": "auto-approval or the human reviewer rejected the draft, or the approval hold timed out.",
    "compiled": "the Harbor task image did not build.",
    "validated": "a control run failed: oracle, nop, alt-correct or plausible-wrong did not behave as the scenario requires.",
    "registered": "nothing is rejected here: kept means the version is in registry.json; superseded means a later version replaced it.",
}


def _count(step: dict, key: str) -> int:
    v = step.get(key)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _entered(step: dict) -> int:
    return _count(step, "entered") or sum(_count(step, key) for key, _kind, _label in FUNNEL_STATES)


def funnel(steps: list[dict], chart_id: str = "chart-funnel") -> str:
    """``steps``: FunnelStep dicts (stage, entered, kept, rejected, error, pending, superseded, ids). One row per stage, scaled by
    the largest ``entered``; the segments are the five terminal states in status tokens; the label reads "kept of entered".
    Under the chart: what "rejected here" means at each stage. The caption names the registered scenarios (the last stage's
    kept ids). Only the element/attribute vocabulary ``app.js`` accepts for live chart updates is used (no ``code``/``dl``)."""
    if not steps:
        return "<p class='empty'>No candidates yet. The funnel fills as a creation job mines, judges and probes.</p>"
    total = max(_entered(s) for s in steps) or 1
    pad_l, pad_r, row_h, bar_h, gap = 92, 150, 26, 18, 2
    width = pad_l + 420 + pad_r
    height = row_h * len(steps) + 8
    scale = 420 / total
    parts = [f"<svg class='chart funnel' viewBox='0 0 {width} {height}' width='{width}' height='{height}' role='img' aria-label='creation funnel'>"]
    for i, s in enumerate(steps):
        stage, entered, kept = str(s.get("stage") or ""), _entered(s), _count(s, "kept")
        yy = 4 + i * row_h
        parts.append(f"<text class='cat' x='{pad_l - 8}' y='{yy + bar_h - 5}' text-anchor='end'>{_h(stage)}</text>")
        xx = pad_l
        for key, kind, _label in FUNNEL_STATES:
            n = _count(s, key)
            if n <= 0:
                continue
            w = max(1.0, n * scale - gap)
            tip = f"{stage} · {key}: {n} of {entered} entered" + (f" — rejected here means: {FUNNEL_GATES.get(stage, '')}" if key == "rejected" else "")
            parts.append(f"<g class='bar {kind}' tabindex='0'><title>{_h(tip)}</title><rect x='{xx:.1f}' y='{yy}' width='{w:.1f}' height='{bar_h}' rx='2'/></g>")
            xx += n * scale
        parts.append(f"<text class='lbl strong' x='{xx + 6:.1f}' y='{yy + bar_h - 5}'>{kept}<tspan class='lbl'> kept of {entered}</tspan></text>")
    parts.append("</svg>")
    legend = "<ul class='legend'>" + "".join(f"<li><i class='swatch {kind}' aria-hidden='true'></i>{_h(label)}</li>" for _key, kind, label in FUNNEL_STATES) + "</ul>"
    gates = ("<ul class='small muted funnel-gates'>"
             + "".join(f"<li><strong>{_h(s.get('stage'))}</strong> · rejected here: {_h(FUNNEL_GATES.get(str(s.get('stage')), 'a gate of this stage said no.'))}</li>" for s in steps)
             + "</ul>")
    head = "".join(f"<th class='num'>{k}</th>" for k in ("entered", "kept", "rejected", "error", "pending", "superseded"))
    body = "".join(f"<tr><td>{_h(s.get('stage'))}</td><td class='num'>{_entered(s)}</td>"
                   + "".join(f"<td class='num'>{_count(s, key)}</td>" for key, _kind, _label in FUNNEL_STATES)
                   + f"<td>{_h(FUNNEL_GATES.get(str(s.get('stage')), ''))}</td></tr>" for s in steps)
    table = f"<div class='table-wrap'><table><thead><tr><th>stage</th>{head}<th>rejected here means</th></tr></thead><tbody>{body}</tbody></table></div>"
    last = next((s for s in reversed(steps) if s.get("stage") == "registered"), steps[-1])
    ids = [str(x) for x in (last.get("ids") or []) if x]
    registered = (f"Registered ({len(ids)}): " + ", ".join(f"<span class='mono'>{_h(x)}</span>" for x in ids) + ".") if ids else "Registered: none yet."
    caption = ("Candidate versions per stage: entered at a stage equals kept at the previous one; a rebuilt scenario's earlier version is counted "
               "too and reads superseded where it stopped. " + registered)
    return figure("creation funnel", "".join(parts) + legend + gates, table, caption, chart_id)


# ----------------------------------------------------------------------------- histogram: single hue

def hbars(items: list[tuple[str, int]], chart_id: str = "chart-hist", title: str = "reject reasons") -> str:
    if not items:
        return "<p class='empty'>No rejects recorded.</p>"
    top = max(v for _k, v in items) or 1
    pad_l, row_h, bar_h = 180, 22, 14
    width, height = pad_l + 320 + 60, row_h * len(items) + 8
    parts = [f"<svg class='chart' viewBox='0 0 {width} {height}' width='{width}' height='{height}' role='img' aria-label='{_h(title)}'>"]
    for i, (k, v) in enumerate(items):
        yy = 4 + i * row_h
        w = max(1.0, 320 * v / top)
        parts.append(f"<text class='cat' x='{pad_l - 8}' y='{yy + bar_h - 3}' text-anchor='end'>{_h(k)}</text>")
        parts.append(f"<g class='bar accent' tabindex='0'><title>{_h(k)}: {v}</title><rect x='{pad_l}' y='{yy}' width='{w:.1f}' height='{bar_h}' rx='2'/></g>")
        parts.append(f"<text class='lbl' x='{pad_l + w + 6:.1f}' y='{yy + bar_h - 3}'>{v}</text>")
    parts.append("</svg>")
    table = ("<div class='table-wrap'><table><thead><tr><th>gate</th><th class='num'>count</th></tr></thead><tbody>"
             + "".join(f"<tr><td>{_h(k)}</td><td class='num'>{v}</td></tr>" for k, v in items) + "</tbody></table></div>")
    return figure(title, "".join(parts), table, "One colour: the categories are nominal; order is by count.", chart_id)


# ----------------------------------------------------------------------------- heat matrix: sequential

def _step(v: float | None) -> int:
    if v is None:
        return 0
    return 1 + min(6, int(v * 7)) if v > 0 else 1


def matrix(rows: list[str], cols: list[str], cell_of, *, chart_id: str = "chart-matrix", title: str = "per-scenario matrix", col_label=None,
           row_label=None, row_html=None) -> str:
    """``cell_of(row, col) -> (value 0..1 or None, n, tooltip)``. Sequential blue steps; value printed in the cell (ink by step).
    ``row_label(row)`` is the plain text of the SVG row label; ``row_html(row)`` the pre-escaped first cell of the table twin."""
    if not rows or not cols:
        return "<p class='empty'>No cells yet.</p>"
    pad_l, pad_t, cw, ch = 220, 70, 74, 26
    width, height = pad_l + cw * len(cols) + 100, pad_t + ch * len(rows) + 8
    parts = [f"<svg class='chart' viewBox='0 0 {width} {height}' width='{width}' height='{height}' role='img' aria-label='{_h(title)}'>"]
    for j, c in enumerate(cols):
        lab = col_label(c) if col_label else c
        parts.append(f"<text class='cat' x='{pad_l + j * cw + cw / 2:.1f}' y='{pad_t - 12}' text-anchor='start' transform='rotate(-30 {pad_l + j * cw + cw / 2:.1f},{pad_t - 12})'>{_h(lab)}</text>")
    trows = []
    for i, r in enumerate(rows):
        yy = pad_t + i * ch
        lab = str(row_label(r) if row_label else r)
        parts.append(f"<text class='cat' x='{pad_l - 8}' y='{yy + ch - 8}' text-anchor='end'>{_h(lab if len(lab) <= 34 else lab[:33] + '…')}</text>")
        cells = []
        for j, c in enumerate(cols):
            v, n, tip = cell_of(r, c)
            v = _num(v)
            step = _step(v) if n else 0
            txt = (f"{v:.2f}" if v is not None else "n/a") if n else "—"
            parts.append(f"<g class='bar' tabindex='0'><title>{_h(tip)}</title><rect class='cell s{step}' x='{pad_l + j * cw}' y='{yy}' width='{cw}' height='{ch}'/>"
                         f"<text class='cellv{' dark' if step >= 5 else ''}' x='{pad_l + j * cw + cw / 2:.1f}' y='{yy + ch - 8}' text-anchor='middle'>{_h(txt)}</text></g>")
            cells.append(f"<td class='num'>{_h(txt)}" + (f" <span class='muted'>(n={n})</span>" if n else "") + "</td>")
        trows.append(f"<tr><td>{row_html(r) if row_html else _h(lab)}</td>{''.join(cells)}</tr>")
    parts.append("</svg>")
    legend = "<ul class='legend'>" + "".join(f"<li><i class='swatch' style='background:var(--seq-{s})'></i>{lo:.2f}–{hi:.2f}</li>" for s, lo, hi in
                                              ((100, 0.0, 0.14), (200, 0.14, 0.29), (300, 0.29, 0.43), (400, 0.43, 0.57), (500, 0.57, 0.71), (600, 0.71, 0.86), (700, 0.86, 1.0))) + "</ul>"
    table = ("<div class='table-wrap'><table><thead><tr><th>scenario</th>" + "".join(f"<th class='num'>{_h(col_label(c) if col_label else c)}</th>" for c in cols)
             + "</tr></thead><tbody>" + "".join(trows) + "</tbody></table></div>")
    return figure(title, "".join(parts) + legend, table, "Sequential single hue: darker is a higher pass rate; the value is printed in every cell.", chart_id)


def dec(v) -> float | None:
    return float(v) if isinstance(v, (int, float, Decimal)) else _num(v)


def history(rows: list[dict], *, metric: str, chart_id: str, title: str, sample_key: str = "n", unit: str = "", ymax: float | None = None) -> str:
    """UTC daily cumulative lines, direct labels, sample-count tooltips and a complete evidence table.

Missing values break lines. Families keep their categorical slot; distinct models
and asking cohorts have named series and dash patterns. At most three families and
twelve series share a plot; every series remains in its table.
"""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["model"], row.get("cohort", "")), []).append(row)
    for series in grouped.values():
        series.sort(key=lambda row: row["day"])
    names = sorted(grouped, key=lambda key: (family_of(key[0])[2] or 99, key))
    families = list(dict.fromkeys(family_of(model)[0] for model, _ in names))[:MAX_FAMILIES_PER_AXIS]
    shown = [key for key in names if family_of(key[0])[0] in families][:12]
    days = sorted({row["day"] for row in rows})
    first = date.fromisoformat(days[0]).toordinal() if days else 0
    last = date.fromisoformat(days[-1]).toordinal() if days else first
    values = [_num(row.get(metric)) for row in rows]
    peak = max([v for v in values if v is not None] + [0.0])
    maximum = ymax if ymax is not None else (peak * 1.1 if peak > 0 else 1.0)
    left, top, plot_width, plot_height, right = 80, 24, 500, max(180, len(shown) * 18), 230
    width, height = left + plot_width + right, top + plot_height + 36

    def x(day):
        return left + plot_width * ((date.fromisoformat(day).toordinal() - first) / (last - first) if last != first else 0.5)

    def y(value):
        return top + plot_height * (1 - value / maximum)

    parts = [f"<svg class='chart history-chart' viewBox='0 0 {width} {height}' width='{width}' height='{height}' role='img' aria-label='{_h(title)}'>"]
    for value in (0, maximum / 2, maximum):
        parts.append(f"<line class='grid' x1='{left}' x2='{left + plot_width}' y1='{y(value):.1f}' y2='{y(value):.1f}'/>"
                     f"<text class='tick' x='{left - 8}' y='{y(value) + 4:.1f}' text-anchor='end'>{value:.2g}{_h(unit)}</text>")
    ticks = sorted({0, len(days) // 2, len(days) - 1}) if days else []
    for i in ticks:
        day = days[i]
        parts.append(f"<text class='cat' x='{x(day):.1f}' y='{height - 10}' text-anchor='middle'>{_h(day)}</text>")
    last_labels = []
    for number, key in enumerate(shown):
        model, cohort = key
        label = model.split("/")[-1] + (f" · {cohort}" if cohort else "")
        short_model = model.split("/")[-1]
        short_label = (short_model[:14] + "…" if len(short_model) > 14 else short_model) + (" · asked" if cohort == "asked" else " · no ask" if cohort else "")
        slot = family_of(model)[2]
        pattern = "6 3" if cohort == "did-not-ask" else ("", "8 3", "2 3")[number % 3]
        segments, segment = [], []
        for row in grouped[key]:
            value = _num(row.get(metric))
            if value is None:
                if segment:
                    segments.append(segment)
                    segment = []
                continue
            segment.append((x(row["day"]), y(value)))
            tooltip = f"{row['day']} · {label} · {metric}={value:.4g}{unit} · n={row.get(sample_key, 0)} · {row['scenarios']} scenarios"
            parts.append(f"<a href='{_h(row['evidence_url'])}' tabindex='0'><title>{_h(tooltip)}</title>"
                         f"<circle class='history-point fam-{slot}' cx='{x(row['day']):.1f}' cy='{y(value):.1f}' r='3'/></a>")
        if segment:
            segments.append(segment)
        for points in segments:
            parts.append(f"<polyline class='history-line fam-{slot}' stroke-dasharray='{pattern}' points='"
                         + " ".join(f"{xx:.1f},{yy:.1f}" for xx, yy in points) + "'/>")
        valid = [row for row in grouped[key] if _num(row.get(metric)) is not None]
        if valid:
            row = valid[-1]
            last_labels.append((y(float(row[metric])), label, short_label, float(row[metric]), row["evidence_url"]))
    labels = sorted(last_labels)
    positions = []
    for desired, *_ in labels:
        positions.append(max(desired, positions[-1] + 18 if positions else top))
    if positions and positions[-1] > top + plot_height:
        overflow = positions[-1] - top - plot_height
        positions = [position - overflow for position in positions]
    for placed, (_desired, label, short_label, value, href) in zip(positions, labels):
        parts.append(f"<a href='{_h(href)}'><text class='lbl' x='{left + plot_width + 12}' y='{placed + 4:.1f}'><title>{_h(label)}</title>"
                     f"{_h(short_label)} {value:.3g}{_h(unit)}</text></a>")
    if not any(v is not None for v in values):
        parts.append(f"<text class='lbl' x='{left + 20}' y='{top + plot_height / 2}'>No estimates with sufficient observations.</text>")
    parts.append("</svg>")
    legend = "<ul class='legend'>" + "".join(f"<li><i class='swatch fam-{family_of(model)[2]}'></i>{_h(model)}{_h(' · ' + cohort if cohort else '')}</li>" for model, cohort in shown) + "</ul>"
    cells = []
    for row in rows:
        value = _num(row.get(metric))
        value_text = f"{value:.4g}{unit}" if value is not None else "unknown"
        n = row.get(sample_key, row["n"])
        cells.append(f"<tr><td>{_h(row['model'])}{_h(' · ' + row['cohort'] if row.get('cohort') else '')}</td><td>{_h(row['day'])}</td>"
                     f"<td class='num'><a href='{_h(row['evidence_url'])}'>{n}</a></td><td class='num'>{row['scenarios']}</td>"
                     f"<td class='num'><a href='{_h(row['evidence_url'])}'>{_h(value_text)}</a></td></tr>")
    table = ("<div class='table-wrap'><table><thead><tr><th>model / cohort</th><th>through UTC day</th><th class='num'>n observed</th>"
             f"<th class='num'>scenarios</th><th class='num'>{_h(title)}</th></tr></thead><tbody>{''.join(cells)}</tbody></table></div>")
    caption = "Cumulative through each UTC completion day; n is the observed denominator. Select a point or value for its trials. Missing values break the line."
    if len(shown) < len(names):
        caption += f" {len(names) - len(shown)} additional series are in the table."
    return figure(title, "".join(parts) + legend, table, caption, chart_id)
