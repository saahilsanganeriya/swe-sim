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

from swesim.dashboard.render import FAMILIES, TIER_LABELS, TIER_ORDER, family_of

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
           row_label=None, row_html=None, cell_text=None, extra: tuple[str, object] | None = None, caption: str | None = None) -> str:
    """``cell_of(row, col) -> (value 0..1 or None, n, tooltip)``. Sequential blue steps; value printed in the cell (ink by step).
    ``row_label(row)`` is the plain text of the SVG row label; ``row_html(row)`` the pre-escaped first cell of the table twin;
    ``cell_text(row, col, value, n)`` overrides the printed cell text (the gradient matrix prints ``successes/n`` so n is in
    every cell); ``extra = (header, fn)`` appends one pre-escaped column to the table twin (the gradient chip)."""
    if not rows or not cols:
        return "<p class='empty'>No cells yet.</p>"
    pad_l, pad_t, cw, ch = 220, 88, 74, 26   # pad_t holds −30° column labels up to ~20 chars
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
            if n and cell_text is not None:
                txt = cell_text(r, c, v, n)
            parts.append(f"<g class='bar' tabindex='0'><title>{_h(tip)}</title><rect class='cell s{step}' x='{pad_l + j * cw}' y='{yy}' width='{cw}' height='{ch}'/>"
                         f"<text class='cellv{' dark' if step >= 5 else ''}' x='{pad_l + j * cw + cw / 2:.1f}' y='{yy + ch - 8}' text-anchor='middle'>{_h(txt)}</text></g>")
            cells.append(f"<td class='num'>{_h(txt)}" + (f" <span class='muted'>(n={n})</span>" if n else "") + "</td>")
        trows.append(f"<tr><td>{row_html(r) if row_html else _h(lab)}</td>{''.join(cells)}" + (f"<td>{extra[1](r)}</td>" if extra else "") + "</tr>")
    parts.append("</svg>")
    legend = "<ul class='legend'>" + "".join(f"<li><i class='swatch' style='background:var(--seq-{s})'></i>{lo:.2f}–{hi:.2f}</li>" for s, lo, hi in
                                              ((100, 0.0, 0.14), (200, 0.14, 0.29), (300, 0.29, 0.43), (400, 0.43, 0.57), (500, 0.57, 0.71), (600, 0.71, 0.86), (700, 0.86, 1.0))) + "</ul>"
    table = ("<div class='table-wrap'><table><thead><tr><th>scenario</th>" + "".join(f"<th class='num'>{_h(col_label(c) if col_label else c)}</th>" for c in cols)
             + (f"<th>{_h(extra[0])}</th>" if extra else "") + "</tr></thead><tbody>" + "".join(trows) + "</tbody></table></div>")
    return figure(title, "".join(parts) + legend, table, caption or "Sequential single hue: darker is a higher pass rate; the value is printed in every cell.", chart_id)


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


# ----------------------------------------------------------------------------- capability gradient (Trends): identity = model family, axis = tier

def short_model(model: str, n: int = 22) -> str:
    name = (model or "").split("/")[-1]
    return name if len(name) <= n else name[:n - 1] + "…"


def family_legend(models: list[str]) -> str:
    """Swatch + family label for the families present, in the fixed categorical order (colour = family, never cycled)."""
    present = {family_of(m)[0] for m in models}
    items = [(label, slot) for slot, (key, label, _p) in enumerate(FAMILIES, start=1) if key in present]
    if "other" in present:
        items.append(("other", 0))
    return "<ul class='legend'>" + "".join(f"<li><i class='swatch fam-{slot}' aria-hidden='true'></i>{_h(label)}</li>" for label, slot in items) + "</ul>"


def _tier_title(tier: str) -> str:
    return f"tier {tier} · {TIER_LABELS[tier]}" if tier in TIER_LABELS else "outside the tier table"


def tier_bars(cells: list[dict], *, chart_id: str, title: str, ymax: float = 1.0) -> str:
    """Accuracy by model for one arm, grouped and ordered by solver tier A→E (then models outside the table). ``cells``:
    dicts with ``model``, ``tier``, ``accuracy``, ``n``, ``success_count``, ``scenarios`` and optional ``evidence_url``.
    One bar per model, colour = family (fixed slot), a rotated model label with its n under every bar, the maximum of
    each tier direct-labelled; the rest lives in the tooltip and the table twin. Cells without a trial do not exist."""
    cells = [c for c in cells if c.get("n") and c.get("model")]
    if not cells:
        return "<p class='empty'>No admitted trials in this arm yet.</p>"
    tiers = [t for t in (*TIER_ORDER, "other") if any(c.get("tier") == t for c in cells)]
    by_tier = {t: sorted((c for c in cells if c.get("tier") == t), key=lambda c: (family_of(c["model"])[2] or 99, c["model"])) for t in tiers}
    pad_l, pad_r, pad_t, pad_b = 40, 12, 18, 156   # pad_b: the −60° model labels (≤ 21 chars ≈ 113 px tall) plus the two-line tier title
    bar_w, gap, slot_gap, plot_h = 18, 2, 28, 150
    widths = {t: max(96, len(by_tier[t]) * (bar_w + gap) + slot_gap) for t in tiers}
    width = pad_l + sum(widths.values()) + pad_r
    height = pad_t + plot_h + pad_b
    y = lambda v: pad_t + plot_h * (1 - v / ymax)  # noqa: E731
    base = y(0)
    parts = [f"<svg class='chart' viewBox='0 0 {width} {height}' width='{width}' height='{height}' role='img' aria-label='{_h(title)}'>"]
    for tick in (0.0, ymax / 2, ymax):
        parts.append(f"<line class='grid' x1='{pad_l}' x2='{width - pad_r}' y1='{y(tick):.1f}' y2='{y(tick):.1f}'/>")
        parts.append(f"<text class='tick' x='{pad_l - 6}' y='{y(tick) + 3:.1f}' text-anchor='end'>{tick:g}</text>")
    parts.append(f"<line class='axis' x1='{pad_l}' x2='{width - pad_r}' y1='{base:.1f}' y2='{base:.1f}'/>")
    x = pad_l
    for tier in tiers:
        group = by_tier[tier]
        parts.append(f"<text class='cat strong' x='{x + widths[tier] / 2:.1f}' y='{height - 18}' text-anchor='middle'>{'tier ' + _h(tier) if tier in TIER_LABELS else 'no tier'}</text>"
                     f"<text class='tick' x='{x + widths[tier] / 2:.1f}' y='{height - 6}' text-anchor='middle'>{_h(TIER_LABELS.get(tier, 'outside the tier table'))}</text>")
        if tier != tiers[-1]:
            parts.append(f"<line class='grid' x1='{x + widths[tier]:.1f}' x2='{x + widths[tier]:.1f}' y1='{pad_t}' y2='{base + 4:.1f}'/>")
        bx = x + (widths[tier] - len(group) * (bar_w + gap)) / 2
        vmax = max((_num(c.get("accuracy")) for c in group if _num(c.get("accuracy")) is not None), default=None)
        for c in group:
            v = _num(c.get("accuracy"))
            slot = family_of(c["model"])[2]
            tip = (f"{_tier_title(tier)} · {c['model']}: accuracy " + (f"{v:.2f}" if v is not None else "n/a")
                   + f" · {c.get('success_count', 0)}/{c['n']} trials · {c.get('scenarios', '?')} scenario(s)")
            cx = bx + bar_w / 2
            href = c.get("evidence_url")
            open_tag = f"<a class='bar fam-{slot}' href='{_h(href)}' tabindex='0'>" if href else f"<g class='bar fam-{slot}' tabindex='0'>"
            close_tag = "</a>" if href else "</g>"
            body = f"<title>{_h(tip)}</title>"
            if v is not None:
                top = y(max(0.0, min(ymax, v)))
                body += f"<path d='{_rounded_bar(bx, top, base, bar_w)}'/>"
                if vmax is not None and v == vmax:
                    body += f"<text class='lbl strong' x='{cx:.1f}' y='{top - 3:.1f}' text-anchor='middle'>{v:.2f}</text>"
            else:
                body += f"<rect x='{bx:.1f}' y='{base - 1:.1f}' width='{bar_w}' height='1'/>"
            body += (f"<text class='cat' x='{cx:.1f}' y='{base + 12:.1f}' text-anchor='end' transform='rotate(-60 {cx:.1f},{base + 12:.1f})'>"
                     f"{_h(short_model(c['model'], 16))} ·n={c['n']}</text>")
            parts.append(open_tag + body + close_tag)
            bx += bar_w + gap
        x += widths[tier]
    parts.append("</svg>")
    rows = sorted(cells, key=lambda c: ((*TIER_ORDER, "other").index(c.get("tier", "other")) if c.get("tier") in (*TIER_ORDER, "other") else 99,
                                        family_of(c["model"])[2] or 99, c["model"]))
    def table_row(c: dict) -> str:
        acc = _num(c.get("accuracy"))
        n_cell = f"<a href='{_h(c['evidence_url'])}'>{c['n']}</a>" if c.get("evidence_url") else str(c["n"])
        return (f"<tr><td>{_h(c.get('tier'))}</td><td class='mono'>{_h(c['model'])}</td><td>{_h(family_of(c['model'])[1])}</td>"
                f"<td class='num'>{f'{acc:.3f}' if acc is not None else 'n/a'}</td><td class='num'>{_h(c.get('success_count', 0))}</td>"
                f"<td class='num'>{n_cell}</td><td class='num'>{_h(c.get('scenarios'))}</td></tr>")

    body_rows = "".join(table_row(c) for c in rows)
    table = ("<div class='table-wrap'><table><thead><tr><th>tier</th><th>model</th><th>family</th><th class='num'>accuracy</th><th class='num'>solved</th>"
             f"<th class='num'>n</th><th class='num'>scenarios</th></tr></thead><tbody>{body_rows}</tbody></table></div>")
    caption = (f"{len(cells)} model cell(s) across {len(tiers)} tier group(s); one bar per model, colour = family (fixed slot), the label under each bar "
               "carries its n; the highest bar of each tier is labelled. Expected: bars rise from left (tier A) to right (tier E).")
    return figure(title, "".join(parts) + family_legend([c["model"] for c in cells]), table, caption, chart_id)


DELTA_TITLES: dict[str, str] = {"full_minus_hidden": "full − hidden", "interactive_minus_hidden": "interactive − hidden", "full_minus_interactive": "full − interactive"}


def _arm_text(fig: dict | None) -> str:
    if not fig:
        return "—"
    acc = _num(fig.get("accuracy"))
    return (f"{acc:.2f}" if acc is not None else "n/a") + f" <span class='muted'>({fig.get('success_count', 0)}/{fig.get('n', 0)})</span>"


def delta_bars(rows: list[dict], *, chart_id: str, title: str) -> str:
    """The three deltas per model × brief profile (``gradient.deltas`` rows) as three small multiples that share the row
    axis and one scale (−100 … +100 pp), bars from a zero baseline, colour = family. Same measure, same scale in every
    column: this is small multiples, not a second axis. The table twin carries every arm's figure and both n."""
    rows = [r for r in rows if r.get("deltas")]
    if not rows:
        return "<p class='empty'>No model has trials in two arms on a shared scenario yet.</p>"
    drawn = [r for r in rows if any(r["deltas"].get(k) for k in DELTA_TITLES)]
    keys = list(DELTA_TITLES)
    pad_l, col_w, col_gap, row_h, bar_h, pad_t, pad_b = 236, 168, 26, 22, 12, 34, 22
    half = (col_w - 56) / 2
    width = pad_l + len(keys) * (col_w + col_gap)
    height = pad_t + max(1, len(drawn)) * row_h + pad_b
    parts = [f"<svg class='chart' viewBox='0 0 {width} {height}' width='{width}' height='{height}' role='img' aria-label='{_h(title)}'>"]
    for j, key in enumerate(keys):
        cx = pad_l + j * (col_w + col_gap) + col_w / 2
        parts.append(f"<text class='cat strong' x='{cx:.1f}' y='{pad_t - 18}' text-anchor='middle'>{_h(DELTA_TITLES[key])}</text>")
        parts.append(f"<text class='tick' x='{cx:.1f}' y='{pad_t - 6}' text-anchor='middle'>percentage points</text>")
        parts.append(f"<line class='axis' x1='{cx:.1f}' x2='{cx:.1f}' y1='{pad_t}' y2='{height - pad_b + 4}'/>")
        for tick, label in ((-1.0, "−100"), (0.0, "0"), (1.0, "+100")):
            parts.append(f"<text class='tick' x='{cx + tick * half:.1f}' y='{height - 6}' text-anchor='middle'>{label}</text>")
    for i, r in enumerate(drawn):
        yy = pad_t + i * row_h
        label = f"{short_model(r['model'], 20)} · {r.get('tier', '?')} · {r.get('brief_profile', '')}"
        parts.append(f"<text class='cat' x='{pad_l - 10}' y='{yy + bar_h + 3}' text-anchor='end'>{_h(label)}</text>")
        slot = family_of(r["model"])[2]
        for j, key in enumerate(keys):
            cx = pad_l + j * (col_w + col_gap) + col_w / 2
            d = r["deltas"].get(key)
            if not d:
                parts.append(f"<text class='tick' x='{cx:.1f}' y='{yy + bar_h + 3}' text-anchor='middle'>—</text>")
                continue
            pp = float(d["pp"])
            a, b = d["a"], d["b"]
            names = DELTA_TITLES[key].split(" − ")
            tip = (f"{r['model']} · {r.get('brief_profile', '')}: {names[0]} {a['success_count']}/{a['n']} ({_num(a['accuracy']):.2f}) − "
                   f"{names[1]} {b['success_count']}/{b['n']} ({_num(b['accuracy']):.2f}) = {pp:+.1f} pp on {d['shared_scenarios']} shared scenario(s)")
            x_end = cx + max(-1.0, min(1.0, pp / 100)) * half
            x0, x1 = (cx, x_end) if pp >= 0 else (x_end, cx)
            parts.append(f"<g class='bar fam-{slot}' tabindex='0'><title>{_h(tip)}</title>"
                         f"<rect x='{x0:.1f}' y='{yy + 4}' width='{max(1.0, x1 - x0):.1f}' height='{bar_h}' rx='2'/>"
                         f"<text class='lbl' x='{(x_end + 4) if pp >= 0 else (x_end - 4):.1f}' y='{yy + bar_h + 3}' text-anchor='{'start' if pp >= 0 else 'end'}'>{pp:+.0f}</text></g>")
    parts.append("</svg>")

    def delta_td(d: dict | None) -> str:
        if not d:
            return "<td class='num'>—</td>"
        return (f"<td class='num'><strong>{float(d['pp']):+.1f}</strong> <span class='muted'>n {d['a']['n']}/{d['b']['n']} · {d['shared_scenarios']} shared</span></td>")

    body_rows = "".join(f"<tr><td class='mono'>{_h(r['model'])}</td><td>{_h(r.get('tier'))}</td><td>{_h(r.get('brief_profile'))}</td>"
                        + "".join(f"<td class='num'>{_arm_text(r['arms'].get(arm))}</td>" for arm in ("full", "hidden", "interactive"))
                        + "".join(delta_td(r["deltas"].get(k)) for k in keys) + "</tr>" for r in rows)
    table = ("<div class='table-wrap'><table><thead><tr><th>model</th><th>tier</th><th>brief</th><th class='num'>full (solved/n)</th><th class='num'>hidden</th>"
             "<th class='num'>interactive</th>" + "".join(f"<th class='num'>Δ {_h(DELTA_TITLES[k])} (pp)</th>" for k in keys) + f"</tr></thead><tbody>{body_rows}</tbody></table></div>")
    caption = ("Percentage points; each delta is computed on the scenarios the two arms share for that model (the arm figures in the table use all trials). "
               f"{len(drawn)} of {len(rows)} model rows have a computable delta; the rest are in the table.")
    return figure(title, "".join(parts) + family_legend([r["model"] for r in rows]), table, caption, chart_id)


def model_hbars(rows: list[dict], *, metric: str, chart_id: str, title: str, fmt=None, unit: str = "", reason_key: str | None = None,
                columns: list[tuple[str, str, str]] | None = None, caption: str | None = None, label=None) -> str:
    """Horizontal bars, one row per model in tier order, colour = family; the value at the bar tip, n in the tooltip and the
    table twin. Rows whose metric is unknown are drawn as an "n/a" row (the reason in the tooltip and the table).
    ``columns``: (key, header, kind) for the table twin (kind: num | text | link); ``label(row)`` overrides the row label
    (default: short model id · tier)."""
    if not rows:
        return "<p class='empty'>No rows yet.</p>"
    fmt = fmt or (lambda v: f"{v:.2f}{unit}")
    label = label or (lambda r: f"{short_model(r['model'], 20)} · {r.get('tier', '?')}")
    values = [_num(r.get(metric)) for r in rows]
    top = max([v for v in values if v is not None] + [0.0]) or 1.0
    pad_l, plot_w, pad_r, row_h, bar_h = 230, 300, 110, 22, 14
    width, height = pad_l + plot_w + pad_r, row_h * len(rows) + 8
    parts = [f"<svg class='chart' viewBox='0 0 {width} {height}' width='{width}' height='{height}' role='img' aria-label='{_h(title)}'>"]
    for i, (r, v) in enumerate(zip(rows, values)):
        yy = 4 + i * row_h
        slot = family_of(r["model"])[2]
        parts.append(f"<text class='cat' x='{pad_l - 8}' y='{yy + bar_h - 3}' text-anchor='end'>{_h(label(r))}</text>")
        if v is None:
            reason = r.get(reason_key) if reason_key else None
            parts.append(f"<g class='bar na' tabindex='0'><title>{_h(r['model'])}: {_h(title)} unknown" + (f" — {_h(reason)}" if reason else "") + f" · n={_h(r.get('n'))}</title>"
                         f"<rect x='{pad_l}' y='{yy + bar_h - 2}' width='{plot_w}' height='1'/><text x='{pad_l + 4}' y='{yy + bar_h - 4}'>n/a" + (f" · {_h(reason)}" if reason else "") + "</text></g>")
            continue
        w = max(1.0, plot_w * v / top)
        tip = f"{r['model']} (tier {r.get('tier', '?')}): {title} {fmt(v)} · n={r.get('n')}"
        parts.append(f"<g class='bar fam-{slot}' tabindex='0'><title>{_h(tip)}</title><rect x='{pad_l}' y='{yy}' width='{w:.1f}' height='{bar_h}' rx='2'/>"
                     f"<text class='lbl' x='{pad_l + w + 6:.1f}' y='{yy + bar_h - 3}'>{_h(fmt(v))}</text></g>")
    parts.append("</svg>")
    columns = columns or [("model", "model", "text"), ("tier", "tier", "text"), (metric, title, "num"), ("n", "n", "num")]

    def td(r: dict, key: str, kind: str) -> str:
        v = r.get(key)
        if kind == "num":
            num = _num(v)
            text = fmt(num) if key == metric and num is not None else (_h(v) if v is not None else "—")
            return f"<td class='num'>{text}</td>"
        if kind == "link":
            return f"<td class='num'><a href='{_h(r.get('evidence_url'))}'>{_h(v)}</a></td>" if r.get("evidence_url") else f"<td class='num'>{_h(v)}</td>"
        cls = " class='mono'" if key == "model" else ""
        return f"<td{cls}>{_h(v) if v is not None else '—'}</td>"

    def th(label: str, kind: str) -> str:
        cls = " class='num'" if kind != "text" else ""
        return f"<th{cls}>{_h(label)}</th>"

    body_rows = "".join("<tr>" + "".join(td(r, key, kind) for key, _label, kind in columns) + "</tr>" for r in rows)
    table = ("<div class='table-wrap'><table><thead><tr>" + "".join(th(label, kind) for _k, label, kind in columns)
             + f"</tr></thead><tbody>{body_rows}</tbody></table></div>")
    return figure(title, "".join(parts) + family_legend([r["model"] for r in rows]), table,
                  caption or "One bar per model in tier order (A→E); colour = family; the value sits at the bar tip and n in the tooltip and table.", chart_id)
