"""Base layout and the component library of the console (server-rendered HTML, no build step).

Contract
- Every dynamic value passes through ``h()`` (``html.escape``); attributes are single-quoted throughout.
- No inline scripts and no page-specific colour literals: behaviour lives in ``static/app.js`` (driven by ``data-*``
  attributes) and colour in ``static/tokens.css`` (design spec §2–§3).
- ``layout()`` renders the persistent left nav with live counts, the global search box, breadcrumbs and the theme toggle;
  pages pass their body and the nav key. Counts come from the reader (``/api/counts`` keeps them live).
- ``table()`` renders a declarative table from ``Column`` specs and emits the same specs as JSON (``data-cols``) so the
  client re-renders rows from a polled JSON endpoint without a second template. Ids are shortened with copy-on-click and
  the full value on hover (``ident()``); statuses are chips with an icon and a label (``chip()``), never colour alone;
  model ids carry a family swatch in the fixed categorical order (``model_chip()``).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from html import escape
from typing import Iterable, Sequence

# ----------------------------------------------------------------------------- primitives

def h(x) -> str:
    return escape("" if x is None else str(x), quote=True)


def dash(x) -> str:
    return h(x) if x not in (None, "") else "—"


def short(x, n: int = 12) -> str:
    s = "" if x is None else str(x)
    return s if len(s) <= n else s[:n] + "…"


def clock(value) -> str:
    """HH:MM:SS UTC from an ISO string or a datetime; the full value belongs in ``title``."""
    dt = _dt(value)
    return dt.strftime("%H:%M:%S") if dt else "no timestamp"


def day_clock(value) -> str:
    dt = _dt(value)
    return dt.strftime("%m-%d %H:%M") if dt else "—"


def _dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), UTC)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def fmt_metric(v, digits: int = 3) -> str:
    if v is None:
        return "<span class='muted' title='not computed: n below k or no valid attempts'>n/a</span>"
    return h(f"{float(v):.{digits}f}")


def fmt_usd(v) -> str:
    if v is None:
        return "<span class='muted' title='no priced usage recorded'>—</span>"
    return h(f"${float(v):,.4f}" if float(v) < 1 else f"${float(v):,.2f}")


def fmt_elapsed(s: float | None, running: bool = False) -> str:
    if s is None:
        return "—"
    s = round(s)
    hh, mm, ss = s // 3600, (s % 3600) // 60, s % 60
    text = f"{hh}h {mm:02d}m" if hh else (f"{mm}m {ss:02d}s" if mm else f"{ss}s")
    return h(text + (" …" if running else ""))


# ----------------------------------------------------------------------------- status, ids, models

STATUS_KIND = {
    # good
    "valid": "good", "completed": "good", "finished": "good", "done": "good", "registered": "good", "kept": "good",
    "approved": "good", "accepted": "good", "informative": "good", "retained": "good",
    # running / pending (pulsing)
    "running": "run", "launched": "run", "pending": "run", "requested": "run", "drafting": "run", "compiling": "run",
    "validating": "run", "planned": "run", "awaiting-approval": "run", "open": "run",
    # critical
    "failed": "critical", "invalid": "critical", "launch-failed": "critical", "runner-error": "critical", "error": "critical",
    "unit-failed": "critical", "rejected": "critical", "terminated": "critical", "timed_out": "critical", "broken": "critical",
    # serious (needs a look, not a failure)
    "unresolved": "serious", "cancelled": "serious", "review_needed": "serious",
    # neutral
    "archived": "neutral", "saturated": "neutral", "continued_as_new": "neutral", "unknown": "neutral", "mined": "neutral",
}
STATUS_ICON = {"good": "●", "run": "◐", "critical": "✕", "serious": "◇", "neutral": "○", "": "○"}


def status_kind(s: str | None) -> str:
    return STATUS_KIND.get((s or "").lower(), "")


def chip(text, kind: str | None = None, title: str | None = None, icon: bool = True) -> str:
    """Status chip: icon + label (never colour alone). ``kind`` defaults to the status table."""
    if text in (None, ""):
        return "<span class='muted'>—</span>"
    k = status_kind(text) if kind is None else kind
    t = f" title='{h(title)}'" if title else ""
    ic = f"<i aria-hidden='true'>{STATUS_ICON.get(k, '○')}</i>" if icon else ""
    return f"<span class='chip {h(k)}'{t}>{ic}{h(text)}</span>"


def outcome_chip(name: str | None, success, code: int | None = None) -> str:
    if not name:
        return "<span class='muted'>—</span>"
    kind = "good" if success in (1, True) else ("serious" if (code or 0) >= 5 else "critical")
    return chip(name, kind)


def ident(value, n: int = 12, href: str | None = None) -> str:
    """Shortened id/hash: full value on hover, copy on click (app.js), optional link."""
    if value in (None, ""):
        return "—"
    s = str(value)
    label = h(short(s, n))
    text = f"<a class='idlink mono' href='{h(href)}'>{label}</a>" if href else f"<span class='mono'>{label}</span>"
    return (f"<span class='id' title='{h(s)}'>{text}<button type='button' class='copy' data-copy='{h(s)}' "
            f"aria-label='copy {h(s)}' title='copy full value'>⧉</button></span>")


FAMILIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # fixed categorical order (design spec §3); prefixes are matched against the model id
    ("anthropic", "Anthropic", ("anthropic/", "openrouter/anthropic/")),
    ("openai", "OpenAI", ("openai/",)),
    ("deepseek", "DeepSeek", ("openrouter/deepseek/", "deepseek/")),
    ("glm", "GLM/Z.ai", ("openrouter/z-ai/", "z-ai/", "zhipu/")),
    ("qwen", "Qwen", ("openrouter/qwen/", "qwen/")),
    ("kimi", "Kimi", ("openrouter/moonshotai/", "moonshotai/")),
    ("minimax", "MiniMax", ("openrouter/minimax/", "minimax/")),
    ("gpt-oss", "gpt-oss", ("openrouter/openai/gpt-oss",)),
)
FAMILY_SLOT = {key: i + 1 for i, (key, _label, _p) in enumerate(FAMILIES)}
TIERS: dict[str, tuple[str, ...]] = {  # brief §3.2, verbatim ids
    "A": ("openrouter/deepseek/deepseek-chat-v3-0324", "openrouter/z-ai/glm-4.5", "openrouter/qwen/qwen3-coder",
          "openrouter/moonshotai/kimi-k2", "openrouter/minimax/minimax-m2", "openrouter/openai/gpt-oss-120b"),
    "B": ("openrouter/deepseek/deepseek-v3.2", "openrouter/deepseek/deepseek-v4-flash", "openrouter/deepseek/deepseek-v4-pro-0813",
          "openrouter/z-ai/glm-5.2", "openrouter/z-ai/glm-5.3", "openrouter/z-ai/glm-5.3-flash", "openrouter/qwen/qwen3.6-plus",
          "openrouter/moonshotai/kimi-k2.7-code", "openrouter/minimax/minimax-m3"),
    "C": ("openai/gpt-5-mini", "openai/gpt-5.6-luna", "anthropic/claude-haiku-4-5-20251001"),
    "D": ("anthropic/claude-sonnet-5", "openai/gpt-5.6-sol"),
    "E": ("openai/gpt-6-astra", "anthropic/claude-fable-5-1", "anthropic/claude-opus-5", "openai/gpt-5.5"),
}
TIER_OF = {m: t for t, ms in TIERS.items() for m in ms}


def family_of(model_id: str | None) -> tuple[str, str, int]:
    """(key, label, slot) for a model id; unknown families are ('other', 'other', 0)."""
    m = (model_id or "").lower()
    for key, label, prefixes in FAMILIES:
        if any(m.startswith(p) for p in prefixes):
            if key == "openai" and m.startswith("openrouter/openai/gpt-oss"):
                continue
            return key, label, FAMILY_SLOT[key]
    return "other", "other", 0


def tier_of(model_id: str | None) -> str | None:
    return TIER_OF.get(model_id or "")


def model_chip(model_id: str | None, tier: bool = True) -> str:
    """Model id with its family swatch (fixed slot) and tier label; text stays in ink tokens."""
    if not model_id:
        return "—"
    key, label, slot = family_of(model_id)
    t = tier_of(model_id)
    tier_html = f" <span class='tier' title='solver tier (brief §3.2)'>{h(t)}</span>" if tier and t else ""
    return (f"<span class='model' title='{h(label)} family'><i class='swatch fam-{slot}' aria-hidden='true'></i>"
            f"<span class='mono'>{h(model_id)}</span>{tier_html}</span>")


def difficulty_chip(tier: str | None) -> str:
    if not tier:
        return chip("unlabelled", "neutral", "no difficulty screen has run for this version")
    return chip(tier, {"informative": "good", "saturated": "neutral", "broken": "critical"}.get(tier, ""),
                {"informative": "hidden arm ≤ 1/2 on the screen model: the task discriminates",
                 "saturated": "screen model passes hidden 2/2 and full 1/1: excluded from the leaderboard by default",
                 "broken": "full arm 0/1 on the screen model: suspect the task"}.get(tier))


# ----------------------------------------------------------------------------- tables

@dataclass(frozen=True)
class Column:
    """One declarative column. ``kind``: text | num | mono | id | chip | model | time | metric | usd | bool | link | list.
    ``href`` is a format string over the row (``/trial/{trial_id}``); ``title`` a row key shown on hover."""
    key: str
    label: str
    kind: str = "text"
    href: str | None = None
    title: str | None = None
    filterable: bool = False
    n: int = 12

    def spec(self) -> dict:
        return asdict(self)


def _fmt_href(col: Column, row: dict) -> str | None:
    if not col.href:
        return None
    try:
        return col.href.format(**{k: ("" if v is None else v) for k, v in row.items()})
    except (KeyError, IndexError):
        return None


def cell(col: Column, row: dict) -> str:
    v = row.get(col.key)
    href = _fmt_href(col, row)
    title = f" title='{h(row.get(col.title))}'" if col.title and row.get(col.title) not in (None, "") else ""
    cls = {"num": " class='num'", "metric": " class='num'", "usd": " class='num'", "mono": " class='mono'"}.get(col.kind, "")
    if col.kind == "id":
        inner = ident(v, col.n, href)
    elif col.kind == "chip":
        inner = chip(v)
    elif col.kind == "model":
        inner = model_chip(v)
    elif col.kind == "time":
        inner = f"<time datetime='{h(v)}' title='{h(v)}'>{h(day_clock(v))}</time>" if v else "—"
    elif col.kind == "metric":
        inner = fmt_metric(v)
    elif col.kind == "usd":
        inner = fmt_usd(v)
    elif col.kind == "bool":
        inner = "—" if v is None else chip("yes" if v else "no", "good" if v else "neutral")
    elif col.kind == "list":
        inner = h(", ".join(str(x) for x in v)) if isinstance(v, (list, tuple)) else dash(v)
    elif col.kind == "num":
        inner = dash(v)
    else:
        inner = dash(v)
    if href and col.kind not in ("id",):
        inner = f"<a href='{h(href)}'>{inner}</a>"
    return f"<td data-f='{h(col.key)}'{cls}{title}>{inner}</td>"


def table(table_id: str, columns: Sequence[Column], rows: Iterable[dict], *, key: str, empty: str, source: str | None = None,
          total: int | None = None, offset: int = 0, limit: int | None = None, caption: str | None = None,
          export: bool = True, dense: bool = True) -> str:
    """Declarative table: sticky header, sortable columns, per-column filters, paging footer, CSV export, empty state.
    With ``source`` the client polls that JSON endpoint and re-renders the rows from ``data-cols``."""
    rows = list(rows)
    cols = list(columns)
    filt = "".join(f"<label class='colf'>{h(c.label)}<input type='search' data-filter='{h(c.key)}' placeholder='filter'></label>"
                   for c in cols if c.filterable)
    head = "".join(f"<th scope='col'{' class=num' if c.kind in ('num', 'metric', 'usd') else ''}><button type='button' class='sort' data-sort='{h(c.key)}' "
                   f"aria-label='sort by {h(c.label)}'>{h(c.label)}<span class='dir' aria-hidden='true'></span></button></th>" for c in cols)
    body = "".join(f"<tr data-key='{h(r.get(key))}'>" + "".join(cell(c, r) for c in cols) + "</tr>" for r in rows)
    n = len(rows)
    total = n if total is None else total
    paging = ""
    if limit and total > limit:
        paging = (f"<nav class='paging' aria-label='pages'><a class='button small' data-page='prev' href='?offset={max(0, offset - limit)}&limit={limit}'"
                  f"{' aria-disabled=true' if offset == 0 else ''}>‹ prev</a><span class='muted small'>{offset + 1}–{min(total, offset + n)} of {total}</span>"
                  f"<a class='button small' data-page='next' href='?offset={offset + limit}&limit={limit}'{' aria-disabled=true' if offset + n >= total else ''}>next ›</a></nav>")
    attrs = f" data-source='{h(source)}'" if source else ""
    cap = f"<caption class='sr-only'>{h(caption)}</caption>" if caption else ""
    tools = (f"<div class='tbl-tools'>{filt}<span class='spacer'></span><span class='muted small' data-count>{n} row{'s' if n != 1 else ''}"
             + (f" of {total}" if total != n else "") + "</span>"
             + ("<button type='button' class='button small' data-csv>CSV</button>" if export else "") + "</div>")
    return (f"<div class='tbl{' dense' if dense else ''}' id='{h(table_id)}' data-table='{h(table_id)}' data-key='{h(key)}' "
            f"data-cols='{h(json.dumps([c.spec() for c in cols]))}'{attrs}>{tools}"
            f"<div class='table-wrap'><table{' class=hidden' if not rows else ''}>{cap}<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
            f"<p class='empty{'' if not rows else ' hidden'}' data-empty>{empty}</p>{paging}</div>")


# ----------------------------------------------------------------------------- tiles, figures, blocks

def tile(label: str, value, *, href: str | None = None, note: str | None = None, key: str | None = None, kind: str = "") -> str:
    """Stat tile: label, value (proportional figures), optional source link and note. ``key`` lets app.js patch it live."""
    v = h(value) if value not in (None, "") else "—"
    data = f" data-tile='{h(key)}'" if key else ""
    val = f"<a href='{h(href)}' class='value{' ' + kind if kind else ''}'{data}>{v}</a>" if href else f"<div class='value{' ' + kind if kind else ''}'{data}>{v}</div>"
    return f"<div class='tile'><div class='label'>{h(label)}</div>{val}" + (f"<div class='note muted small'>{note}</div>" if note else "") + "</div>"


def tiles(items: list[str]) -> str:
    return "<div class='tiles'>" + "".join(items) + "</div>"


def hero(value, label: str, href: str | None = None) -> str:
    inner = f"<div class='hero-value'>{h(value)}</div><div class='hero-label'>{h(label)}</div>"
    return f"<a class='hero' href='{h(href)}'>{inner}</a>" if href else f"<div class='hero'>{inner}</div>"


def code_block(text: str | None, lang: str = "", diff: bool = False, max_lines: int | None = None) -> str:
    """Monospace block; ``diff=True`` washes +/- lines. Escaped per line."""
    if not text:
        return "<p class='empty'>empty</p>"
    lines = str(text).splitlines()
    more = ""
    if max_lines and len(lines) > max_lines:
        more = f"<div class='muted small'>… {len(lines) - max_lines} more line(s); download the artifact for the full text</div>"
        lines = lines[:max_lines]
    if diff:
        out = []
        for ln in lines:
            cls = "add" if ln.startswith("+") and not ln.startswith("+++") else "del" if ln.startswith("-") and not ln.startswith("---") else \
                "hunk" if ln.startswith("@@") else "meta" if ln.startswith(("+++", "---", "diff ", "index ")) else ""
            out.append(f"<span class='ln {cls}'>{h(ln)}</span>")
        return f"<pre class='code diff' data-lang='{h(lang)}'>" + "\n".join(out) + "</pre>" + more
    return f"<pre class='code' data-lang='{h(lang)}'>{h(chr(10).join(lines))}</pre>" + more


def kv(pairs: Iterable[tuple[str, str]]) -> str:
    """Definition list; values are pre-rendered HTML."""
    return "<dl class='kv'>" + "".join(f"<dt>{h(k)}</dt><dd>{v}</dd>" for k, v in pairs) + "</dl>"


def banner(text: str, kind: str = "warn", cli: str | None = None, heading: str | None = None) -> str:
    head = f"<strong>{h(heading)}</strong> " if heading else ""
    tail = f"<div class='muted small'>Equivalent command:</div><pre class='code'>{h(cli)}</pre>" if cli else ""
    return f"<div class='banner {h(kind)}' role='status'>{head}{h(text)}{tail}</div>"


def meter(end, start, label: str = "remaining") -> str:
    if start is None:
        return "<span class='muted'>—</span>"
    end = start if end is None else end
    pct = max(0, min(100, round(100 * end / start))) if start else 0
    kind = " out" if end <= 0 else (" low" if end * 3 <= start else "")
    return (f"<span class='pt'>{h(end)} / {h(start)} {h(label)}</span> <span class='meter{kind}' role='img' "
            f"aria-label='patience {h(end)} of {h(start)}'><i style='width:{pct}%'></i></span>")


def confirm_dialog() -> str:
    """One <dialog> per page; app.js fills it from the preview response (CLI equivalent, params, token expiry)."""
    return ("<dialog id='confirm' class='confirm' aria-labelledby='confirm-title'><form method='dialog'>"
            "<h2 id='confirm-title'>Confirm action</h2><p class='muted small' data-confirm='what'></p>"
            "<div class='muted small'>Exactly this will run:</div><pre class='code' data-confirm='cli'></pre>"
            "<dl class='kv' data-confirm='params'></dl><p class='muted small' data-confirm='expiry'></p>"
            "<div class='actions'><button type='button' class='button' value='cancel' data-confirm='cancel'>Cancel</button>"
            "<button type='button' class='button primary' data-confirm='ok'>Confirm and run</button></div></form></dialog>")


def evaluator_switch(on: bool, label: str, allowed: bool) -> str:
    """Evaluator-only toggle: rendered only for evaluator identities; others see a note (never a ?evaluator=1 link)."""
    if not allowed:
        return f"<span class='muted small'>evaluator-only material ({h(label)}) is hidden for this identity</span>"
    return (f"<button type='button' class='switch{' on' if on else ''}' data-evaluator-toggle role='switch' aria-checked='{'true' if on else 'false'}'>"
            f"<i></i>evaluator-only: {h(label)} {'shown' if on else 'hidden'}</button>")


# ----------------------------------------------------------------------------- layout

@dataclass(frozen=True)
class NavItem:
    label: str
    href: str
    key: str
    count: str | None = None  # key into counts


NAV: tuple[tuple[str, tuple[NavItem, ...]], ...] = (
    ("", (NavItem("Home", "/", "home"), NavItem("Explainer", "/explainer", "explainer"))),
    ("Record", (NavItem("Scenarios", "/scenarios", "scenarios", "scenarios_valid"), NavItem("Trials", "/trials", "trials", "trials_running"),
                NavItem("Candidates", "/candidates", "candidates", "candidates_pending"), NavItem("Workflows", "/workflows", "workflows", "workflows_running"),
                NavItem("Leaderboard", "/leaderboard", "leaderboard"), NavItem("Trends", "/trends", "trends"))),
    ("Operate", (NavItem("Review", "/review", "review", "review_pending"), NavItem("Create", "/create", "create"), NavItem("Run", "/run", "run"),
                 NavItem("Spend", "/spend", "spend"), NavItem("Worker health", "/health", "health"))),
    ("Browse", (NavItem("Database", "/db", "db"), NavItem("Artifacts", "/artifacts", "artifacts"))),
)
COUNT_TITLES = {"scenarios_valid": "valid scenarios", "trials_running": "running trials", "candidates_pending": "candidates pending",
                "workflows_running": "running workflows", "review_pending": "awaiting a decision"}


@dataclass(frozen=True)
class Page:
    """Everything the chrome needs beyond the body."""
    title: str
    active: str
    key: str
    crumbs: tuple[tuple[str, str | None], ...] = ()
    attrs: dict = field(default_factory=dict)
    live: str | None = None


def nav_html(active: str, counts: dict | None) -> str:
    counts = counts or {}
    groups = []
    for name, items in NAV:
        links = []
        for it in items:
            cur = " aria-current='page'" if it.key == active else ""
            cnt = ""
            if it.count:
                v = counts.get(it.count)
                cnt = (f"<span class='count' data-count='{h(it.count)}' title='{h(COUNT_TITLES.get(it.count, it.count))}'>"
                       f"{h(v) if v not in (None, '') else ''}</span>")
            links.append(f"<a class='navlink' href='{h(it.href)}'{cur}>{h(it.label)}{cnt}</a>")
        groups.append((f"<div class='navgroup'><div class='navhead'>{h(name)}</div>" if name else "<div class='navgroup'>") + "".join(links) + "</div>")
    return "".join(groups)


def crumbs_html(crumbs: Sequence[tuple[str, str | None]]) -> str:
    if not crumbs:
        return ""
    items = ["<li><a href='/'>Home</a></li>"]
    for label, href in crumbs:
        items.append(f"<li><a href='{h(href)}'>{h(label)}</a></li>" if href else f"<li aria-current='page'>{h(label)}</li>")
    return f"<nav class='crumbs' aria-label='breadcrumb'><ol>{''.join(items)}</ol></nav>"


def layout(page: Page, body: str, *, counts: dict | None = None, identity=None, temporal_url: str | None = None) -> str:
    data = " ".join(f"data-{h(k)}='{h(v)}'" for k, v in page.attrs.items())
    live = (f"<span id='live' class='live'><span class='dot' aria-hidden='true'></span><span class='txt'>{h(page.live)}</span></span>" if page.live else "")
    who = "anonymous"
    ev = False
    if identity is not None:
        who = getattr(identity, "email", None) or "anonymous"
        ev = bool(getattr(identity, "evaluator", False))
    ident_html = (f"<span class='who' title='{h(who)}{' · evaluator' if ev else ''}'>{h(short(who, 24))}"
                  + (" <span class='chip good'><i aria-hidden='true'>●</i>evaluator</span>" if ev else "") + "</span>")
    temporal = (f"<a class='navlink ext' href='{h(temporal_url)}' target='_blank' rel='noopener'>Temporal UI</a>" if temporal_url else "")
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<title>{h(page.title)} · swesim</title><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<link rel='stylesheet' href='/static/tokens.css'><link rel='stylesheet' href='/static/app.css'>"
            "<script src='/static/theme.js'></script><script src='/static/app.js' defer></script></head>"
            f"<body data-page='{h(page.key)}' data-evaluator='{'1' if ev else '0'}' {data}>"
            "<a class='skip' href='#main'>Skip to content</a>"
            "<div class='shell'><aside class='nav' aria-label='primary'>"
            "<a class='brand' href='/'>swesim<span class='muted small'>console</span></a>"
            "<form class='search' role='search' action='/search' method='get'><input type='search' name='q' placeholder='scenario, trial, workflow, model' aria-label='global search'></form>"
            f"{nav_html(page.active, counts)}<div class='navfoot'>{temporal}"
            "<button type='button' class='button small' data-theme-toggle aria-pressed='false' title='toggle light/dark'>Theme</button>"
            f"{ident_html}</div></aside>"
            f"<div class='content'><header class='topbar'>{crumbs_html(page.crumbs)}<span class='spacer'></span>{live}</header>"
            f"<main id='main' class='wrap' tabindex='-1'>{body}</main></div></div>{confirm_dialog()}</body></html>")
