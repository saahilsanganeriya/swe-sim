"""Inline SVG charts, generated server-side, no library.

pass@k per model per condition, faceted so that every score group is drawn: one small chart per
(purpose, brief profile) facet, conditions on the x axis, one bar per group (colour = model; two groups that share a
condition/model slot inside a facet - a rebuild - get adjacent bars and say so). Groups are never pooled or silently
dropped. Marks follow the dataviz rules: <= 24px bars with a 4px rounded cap and a 2px surface gap, hairline
gridlines, one y axis (0..1), a legend whenever two or more models are shown, per-bar <title> tooltips with keyboard
focus, and the numbers always available in the table rendered beside the chart. Series colours come from the
validated reference palette (slots 1-3, all-pairs safe in light and dark); past three models the chart folds the
rest into the table only and says so. The k in each facet title is the k of the groups in that facet.
"""
from __future__ import annotations

from html import escape

CONDITIONS = ("full", "hidden", "interactive")
PURPOSE_ORDER = ("calibration", "comparison", "demonstration")
MAX_SERIES = 3


def _h(x) -> str:
    return escape("" if x is None else str(x), quote=True)


def _k_label(groups: list[dict]) -> str:
    ks = sorted({g.get("requested_k") for g in groups if g.get("requested_k") is not None})
    if not ks:
        return "k unknown"
    return f"k={ks[0]}" if len(ks) == 1 else "k in {" + ", ".join(str(k) for k in ks) + "}"


def _facet_svg(groups: list[dict], shown: list[str], metric: str, title: str) -> str:
    conditions = [c for c in CONDITIONS if any(g.get("condition") == c for g in groups)]
    conditions += sorted({g.get("condition") for g in groups if g.get("condition") and g.get("condition") not in CONDITIONS})
    # slots: for each condition, for each model, the list of groups (usually one; several = different builds)
    slots = {(c, m): [g for g in groups if g.get("condition") == c and g.get("model") == m] for c in conditions for m in shown}
    bars_per_condition = {c: sum(max(1, len(slots[(c, m)])) for m in shown) for c in conditions}
    pad_l, pad_r, pad_t, pad_b = 36, 12, 14, 30
    bar_w, gap, slot_gap = 18, 2, 22
    plot_h = 120
    widths = {c: bars_per_condition[c] * (bar_w + gap) + slot_gap for c in conditions}
    width = pad_l + sum(widths.values()) + pad_r
    height = pad_t + plot_h + pad_b
    y = lambda v: pad_t + plot_h * (1 - v)
    parts = [f"<svg class='chart' viewBox='0 0 {width} {height}' width='{width}' height='{height}'>",
             f"<title>{_h(title)}</title>"]
    for tick in (0.0, 0.5, 1.0):
        parts.append(f"<line class='grid' x1='{pad_l}' x2='{width - pad_r}' y1='{y(tick):.1f}' y2='{y(tick):.1f}'/>")
        parts.append(f"<text class='tick' x='{pad_l - 6}' y='{y(tick) + 3:.1f}' text-anchor='end'>{tick:g}</text>")
    x = pad_l
    shared = 0
    for cond in conditions:
        x0 = x + slot_gap / 2
        parts.append(f"<text class='cat' x='{x + widths[cond] / 2:.1f}' y='{height - 10}' text-anchor='middle'>{_h(cond)}</text>")
        bx = x0
        for mi, model in enumerate(shown):
            matching = slots[(cond, model)]
            if len(matching) > 1:
                shared += 1
            for g in matching or [None]:
                if g is not None:
                    value = g.get(metric)
                    label = (f"{cond} · {model} · {g.get('purpose')} · {g.get('brief_profile')} brief · {metric}@{g.get('requested_k')}: "
                             + (f"{value:.2f}" if isinstance(value, (int, float)) else "n/a (fewer than k valid attempts)")
                             + f" · valid {g.get('n_valid')}/{g.get('n_total')}"
                             + (f" · build {str(g.get('run_config_hash') or '')[:8]}" if len(matching) > 1 else ""))
                    if isinstance(value, (int, float)):
                        top, bottom = y(max(0.0, min(1.0, float(value)))), y(0.0)
                        hgt = max(0.0, bottom - top)
                        r = min(4.0, hgt / 2)
                        d = (f"M{bx:.1f},{bottom} V{top + r:.1f} Q{bx:.1f},{top:.1f} {bx + r:.1f},{top:.1f} H{bx + bar_w - r:.1f} "
                             f"Q{bx + bar_w:.1f},{top:.1f} {bx + bar_w:.1f},{top + r:.1f} V{bottom} Z")
                        parts.append(f"<g class='bar s{mi + 1}' tabindex='0'><title>{_h(label)}</title><path d='{d}'/></g>")
                    else:
                        parts.append(f"<g class='bar na' tabindex='0'><title>{_h(label)}</title>"
                                     f"<rect x='{bx:.1f}' y='{y(0.0) - 1}' width='{bar_w}' height='1'/>"
                                     f"<text class='na' x='{bx + bar_w / 2:.1f}' y='{y(0.0) - 5}' text-anchor='middle'>n/a</text></g>")
                bx += bar_w + gap
        x += widths[cond]
    parts.append("</svg>")
    note = (f"<p class='muted small'>{shared} condition/model slot(s) hold more than one group (different builds); each group is its own bar.</p>"
            if shared else "")
    return "".join(parts) + note


def pass_at_k_svg(groups: list[dict], metric: str = "pass_at_k") -> str:
    """``groups`` are score groups (dicts with condition, model, purpose, brief_profile, requested_k, pass_at_k, ...)."""
    groups = [g for g in groups if isinstance(g, dict) and g.get("condition") and g.get("model")]
    models = sorted({g["model"] for g in groups})
    shown, folded = models[:MAX_SERIES], models[MAX_SERIES:]
    if not groups or not shown:
        return ("<p class='empty'>No scored groups yet. <code>swesim score &lt;id&gt;</code> writes scores.json; the chart appears once "
                "a group has k valid attempts.</p>")
    facets: dict[tuple[str, str], list[dict]] = {}
    for g in groups:
        facets.setdefault((str(g.get("purpose") or "unknown purpose"), str(g.get("brief_profile") or "unknown profile")), []).append(g)

    def facet_order(key):
        purpose, profile = key
        return (PURPOSE_ORDER.index(purpose) if purpose in PURPOSE_ORDER else len(PURPOSE_ORDER), purpose, profile)

    drawn = sum(1 for g in groups if g["model"] in shown)
    out = [f"<figure class='chart-wrap' role='group' aria-label='{_h(metric)} per model per condition, one facet per purpose and brief profile'>"]
    for key in sorted(facets, key=facet_order):
        fg = facets[key]
        title = f"{key[0]} · {key[1]} brief · {_k_label(fg)}"
        out.append(f"<div class='facet'><h4>{_h(title)}</h4>{_facet_svg(fg, shown, metric, f'{metric} · {title}')}</div>")
    if len(shown) >= 2:
        out.append("<ul class='legend'>" + "".join(f"<li><i class='swatch s{i + 1}'></i>{_h(m)}</li>" for i, m in enumerate(shown)) + "</ul>")
    out.append(f"<figcaption class='muted small'>{drawn} of {len(groups)} group(s) drawn, one bar per group (condition · model within a purpose/profile "
               f"facet); {_h(_k_label(groups))}; groups are never pooled."
               + (f" {len(folded)} more model(s) in the table only: {_h(', '.join(folded))}." if folded else "") + "</figcaption></figure>")
    return "".join(out)
