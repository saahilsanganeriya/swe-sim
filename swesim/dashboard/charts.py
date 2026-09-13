"""Inline SVG charts, generated server-side, no library.

pass@k per model per condition as grouped columns: one y axis (0..1), <= 24px bars with a 4px rounded cap and a
2px surface gap, hairline gridlines, a legend whenever there are two or more series (models), per-bar <title>
tooltips and keyboard focus, and the numbers always available in the table rendered beside the chart. Series
colours come from the validated reference palette (slots 1-3, all-pairs safe in light and dark); past three models
the chart folds the rest into the table only and says so.
"""
from __future__ import annotations

from html import escape

CONDITIONS = ("full", "hidden", "interactive")
MAX_SERIES = 3


def _h(x) -> str:
    return escape("" if x is None else str(x), quote=True)


def pass_at_k_svg(groups: list[dict], k: int | None = None, metric: str = "pass_at_k") -> str:
    """``groups`` are score groups (dicts with condition, model, purpose, pass_at_k, n_valid, requested_k)."""
    conditions = [c for c in CONDITIONS if any(g.get("condition") == c for g in groups)]
    models = sorted({g.get("model") for g in groups if g.get("model")})
    shown = models[:MAX_SERIES]
    folded = models[MAX_SERIES:]
    if not conditions or not shown:
        return "<p class='empty'>No scored groups yet. <code>swesim score &lt;id&gt;</code> writes scores.json; the chart appears once a group has k valid attempts.</p>"
    k = k or next((g.get("requested_k") for g in groups if g.get("requested_k")), None)
    # geometry
    pad_l, pad_r, pad_t, pad_b = 36, 12, 14, 30
    bar_w, gap = 18, 2
    slot_w = len(shown) * (bar_w + gap) + 22
    plot_h = 120
    width = pad_l + len(conditions) * slot_w + pad_r
    height = pad_t + plot_h + pad_b
    y = lambda v: pad_t + plot_h * (1 - v)
    parts = [(f"<svg class='chart' viewBox='0 0 {width} {height}' width='{width}' height='{height}' role='img' "
             f"aria-label='{_h(metric)} per model per condition'>")]
    parts.append(f"<title>{_h(metric)} (k={_h(k)}) per model per condition</title>")
    for tick in (0.0, 0.5, 1.0):
        parts.append(f"<line class='grid' x1='{pad_l}' x2='{width - pad_r}' y1='{y(tick):.1f}' y2='{y(tick):.1f}'/>")
        parts.append(f"<text class='tick' x='{pad_l - 6}' y='{y(tick) + 3:.1f}' text-anchor='end'>{tick:g}</text>")
    for ci, cond in enumerate(conditions):
        x0 = pad_l + ci * slot_w + 11
        parts.append(f"<text class='cat' x='{x0 + (len(shown) * (bar_w + gap)) / 2:.1f}' y='{height - 10}' text-anchor='middle'>{_h(cond)}</text>")
        for mi, model in enumerate(shown):
            g = next((g for g in groups if g.get("condition") == cond and g.get("model") == model), None)
            if g is None:
                continue  # no attempts for this model in this condition: no mark at all (n/a is reserved for n < k)
            x = x0 + mi * (bar_w + gap)
            value = g.get(metric)
            label = f"{cond} · {model} · {metric}@{k}: " + (f"{value:.2f}" if isinstance(value, (int, float)) else "n/a (fewer than k valid attempts)")
            label += f" · valid {g.get('n_valid')}/{g.get('n_total')}"
            if isinstance(value, (int, float)):
                top = y(max(0.0, min(1.0, float(value))))
                bottom = y(0.0)
                hgt = max(0.0, bottom - top)
                r = min(4.0, hgt / 2)
                # rounded cap, square baseline
                d = (f"M{x},{bottom} V{top + r:.1f} Q{x},{top:.1f} {x + r:.1f},{top:.1f} H{x + bar_w - r:.1f} "
                     f"Q{x + bar_w},{top:.1f} {x + bar_w},{top + r:.1f} V{bottom} Z")
                parts.append(f"<g class='bar s{mi + 1}' tabindex='0'><title>{_h(label)}</title><path d='{d}'/></g>")
            else:
                parts.append(f"<g class='bar na' tabindex='0'><title>{_h(label)}</title>"
                             f"<rect x='{x}' y='{y(0.0) - 1}' width='{bar_w}' height='1'/>"
                             f"<text class='na' x='{x + bar_w / 2:.1f}' y='{y(0.0) - 5}' text-anchor='middle'>n/a</text></g>")
    parts.append("</svg>")
    legend = ""
    if len(shown) >= 2:
        legend = "<ul class='legend'>" + "".join(f"<li><i class='swatch s{i + 1}'></i>{_h(m)}</li>" for i, m in enumerate(shown)) + "</ul>"
    note = (f"<p class='muted small'>{len(folded)} more model(s) in the table only: {_h(', '.join(folded))}</p>" if folded else "")
    return f"<figure class='chart-wrap'>{parts[0]}{''.join(parts[1:])}{legend}{note}</figure>"
