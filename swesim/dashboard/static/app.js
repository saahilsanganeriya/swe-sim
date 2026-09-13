/* swesim console client. Vanilla JS, no build step. Every untrusted value is inserted with textContent.
   Responsibilities: theme toggle; copy-on-click ids; declarative tables (sort, per-column filter, CSV export, live
   re-render from data-cols + data-source); nav counts and stat tiles polling; the two-step action flow
   (POST /api/<action>/preview -> confirm dialog with the exact CLI -> POST /api/<action> with the signed token);
   episode transcript polling; chart table-view toggles; evaluator toggle. */
(function () {
  "use strict";
  const body = document.body;
  const main = document.getElementById("main");

  /* ------------------------------------------------------------ helpers */
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function shortId(s, n) { s = String(s); return s.length <= n ? s : s.slice(0, n) + "…"; }
  function dayClock(v) {
    if (!v) return "—";
    const d = new Date(v); if (isNaN(d)) return String(v);
    const p = (x) => String(x).padStart(2, "0");
    return p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate()) + " " + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes());
  }
  const STATUS = { valid: "good", completed: "good", finished: "good", done: "good", registered: "good", kept: "good", approved: "good", accepted: "good", informative: "good",
    running: "run", launched: "run", pending: "run", requested: "run", drafting: "run", compiling: "run", validating: "run", planned: "run", "awaiting-approval": "run", open: "run",
    failed: "critical", invalid: "critical", "launch-failed": "critical", "runner-error": "critical", error: "critical", "unit-failed": "critical", rejected: "critical", terminated: "critical", timed_out: "critical", broken: "critical",
    unresolved: "serious", cancelled: "serious", review_needed: "serious",
    archived: "neutral", saturated: "neutral", continued_as_new: "neutral", unknown: "neutral", mined: "neutral" };
  const ICON = { good: "●", run: "◐", critical: "✕", serious: "◇", neutral: "○", "": "○" };
  function chip(text, kind) {
    if (text === null || text === undefined || text === "") return el("span", "muted", "—");
    const k = kind === undefined ? (STATUS[String(text).toLowerCase()] || "") : kind;
    const c = el("span", "chip " + k);
    const i = el("i", null, ICON[k] || "○"); i.setAttribute("aria-hidden", "true");
    c.appendChild(i); c.appendChild(document.createTextNode(String(text)));
    return c;
  }
  const FAMILIES = [["anthropic", ["anthropic/", "openrouter/anthropic/"]], ["openai", ["openai/"]], ["deepseek", ["openrouter/deepseek/", "deepseek/"]],
    ["glm", ["openrouter/z-ai/", "z-ai/", "zhipu/"]], ["qwen", ["openrouter/qwen/", "qwen/"]], ["kimi", ["openrouter/moonshotai/", "moonshotai/"]],
    ["minimax", ["openrouter/minimax/", "minimax/"]], ["gpt-oss", ["openrouter/openai/gpt-oss"]]];
  function familySlot(model) {
    const m = String(model || "").toLowerCase();
    if (m.startsWith("openrouter/openai/gpt-oss")) return 8;
    for (let i = 0; i < FAMILIES.length; i++) if (FAMILIES[i][1].some((p) => m.startsWith(p))) return i + 1;
    return 0;
  }
  /* brief §3.2 tiers, verbatim ids (mirrors render.TIERS; a test keeps them in sync) */
  const TIERS = { A: ["openrouter/deepseek/deepseek-chat-v3-0324", "openrouter/z-ai/glm-4.5", "openrouter/qwen/qwen3-coder", "openrouter/moonshotai/kimi-k2", "openrouter/minimax/minimax-m2", "openrouter/openai/gpt-oss-120b"],
    B: ["openrouter/deepseek/deepseek-v3.2", "openrouter/deepseek/deepseek-v4-flash", "openrouter/deepseek/deepseek-v4-pro-0813", "openrouter/z-ai/glm-5.2", "openrouter/z-ai/glm-5.3", "openrouter/z-ai/glm-5.3-flash", "openrouter/qwen/qwen3.6-plus", "openrouter/moonshotai/kimi-k2.7-code", "openrouter/minimax/minimax-m3"],
    C: ["openai/gpt-5-mini", "openai/gpt-5.6-luna", "anthropic/claude-haiku-4-5-20251001"], D: ["anthropic/claude-sonnet-5", "openai/gpt-5.6-sol"],
    E: ["openai/gpt-6-astra", "anthropic/claude-fable-5-1", "anthropic/claude-opus-5", "openai/gpt-5.5"] };
  const TIER_OF = {}; Object.keys(TIERS).forEach((t) => TIERS[t].forEach((m) => { TIER_OF[m] = t; }));
  function modelChip(model) {
    if (!model) return el("span", null, "—");
    const s = el("span", "model");
    const sw = el("i", "swatch fam-" + familySlot(model)); sw.setAttribute("aria-hidden", "true");
    s.appendChild(sw); s.appendChild(el("span", "mono", model));
    if (TIER_OF[model]) s.appendChild(el("span", "tier", TIER_OF[model]));
    return s;
  }
  function ident(value, n, href) {
    if (value === null || value === undefined || value === "") return el("span", null, "—");
    const s = String(value), wrap = el("span", "id"); wrap.title = s;
    let t;
    if (href) { t = el("a", "idlink mono", shortId(s, n)); t.href = href; } else t = el("span", "mono", shortId(s, n));
    const b = el("button", "copy", "⧉"); b.type = "button"; b.dataset.copy = s; b.setAttribute("aria-label", "copy " + s); b.title = "copy full value";
    wrap.appendChild(t); wrap.appendChild(b);
    return wrap;
  }
  function fmtHref(tpl, row) {
    if (!tpl) return null;
    let missing = false;
    const out = tpl.replace(/\{([a-zA-Z0-9_]+)\}/g, (_, k) => { if (!(k in row)) missing = true; const v = row[k]; return v === null || v === undefined ? "" : String(v); });
    return missing ? null : out;
  }
  function metric(v) { if (v === null || v === undefined) { const s = el("span", "muted", "n/a"); s.title = "not computed: n below k or no valid attempts"; return s; } return document.createTextNode(Number(v).toFixed(3)); }
  function usd(v) { if (v === null || v === undefined) return el("span", "muted", "—"); const x = Number(v); return document.createTextNode(x < 1 ? "$" + x.toFixed(4) : "$" + x.toLocaleString(undefined, { maximumFractionDigits: 2 })); }
  function renderCell(col, row) {
    const td = document.createElement("td"); td.dataset.f = col.key;
    const v = row[col.key], href = fmtHref(col.href, row);
    if (col.title && row[col.title] !== null && row[col.title] !== undefined && row[col.title] !== "") td.title = String(row[col.title]);
    if (col.kind === "num" || col.kind === "metric" || col.kind === "usd") td.className = "num"; else if (col.kind === "mono") td.className = "mono";
    let inner;
    if (col.kind === "id") inner = ident(v, col.n || 12, href);
    else if (col.kind === "chip") inner = chip(v);
    else if (col.kind === "model") inner = modelChip(v);
    else if (col.kind === "time") { if (v) { inner = el("time", null, dayClock(v)); inner.dateTime = v; inner.title = v; } else inner = document.createTextNode("—"); }
    else if (col.kind === "metric") inner = metric(v);
    else if (col.kind === "usd") inner = usd(v);
    else if (col.kind === "bool") inner = v === null || v === undefined ? document.createTextNode("—") : chip(v ? "yes" : "no", v ? "good" : "neutral");
    else if (col.kind === "list") inner = document.createTextNode(Array.isArray(v) ? v.join(", ") : (v === null || v === undefined || v === "" ? "—" : String(v)));
    else inner = document.createTextNode(v === null || v === undefined || v === "" ? "—" : String(v));
    if (href && col.kind !== "id") { const a = el("a"); a.href = href; a.appendChild(inner); inner = a; }
    td.appendChild(inner);
    return td;
  }

  /* ------------------------------------------------------------ theme */
  const themeBtn = document.querySelector("[data-theme-toggle]");
  function currentTheme() {
    const forced = document.documentElement.getAttribute("data-theme");
    if (forced) return forced;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  if (themeBtn) {
    const paint = () => { const t = currentTheme(); themeBtn.textContent = t === "dark" ? "Light theme" : "Dark theme"; themeBtn.setAttribute("aria-pressed", t === "dark" ? "true" : "false"); };
    paint();
    themeBtn.addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("swesim-theme", next); } catch (e) { /* ignore */ }
      paint();
    });
  }

  /* ------------------------------------------------------------ copy ids */
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-copy]");
    if (!b) return;
    ev.preventDefault();
    const v = b.dataset.copy;
    const done = () => { b.classList.add("done"); b.textContent = "✓"; setTimeout(() => { b.classList.remove("done"); b.textContent = "⧉"; }, 1200); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(v).then(done, done); else done();
  });

  /* ------------------------------------------------------------ tables */
  function tableState(t) {
    if (!t._state) t._state = { sort: null, dir: "asc", filters: {}, rows: null, cols: JSON.parse(t.dataset.cols || "[]"), key: t.dataset.key };
    return t._state;
  }
  function rowData(t) {
    const st = tableState(t);
    if (st.rows) return st.rows;
    /* initial server-rendered rows: read their text so sorting/filtering works before the first poll */
    st.rows = Array.from(t.querySelectorAll("tbody tr")).map((tr) => {
      const r = { __tr: tr };
      st.cols.forEach((c) => { const td = tr.querySelector('[data-f="' + c.key + '"]'); r[c.key] = td ? (td.title || td.textContent.trim()) : ""; });
      return r;
    });
    return st.rows;
  }
  function cmp(a, b) {
    const na = parseFloat(a), nb = parseFloat(b);
    if (!isNaN(na) && !isNaN(nb) && String(a).trim() !== "" && String(b).trim() !== "") return na - nb;
    return String(a === null || a === undefined ? "" : a).localeCompare(String(b === null || b === undefined ? "" : b));
  }
  function applyTable(t) {
    const st = tableState(t), tbody = t.querySelector("tbody"), table = t.querySelector("table"), empty = t.querySelector("[data-empty]");
    let rows = rowData(t).slice();
    Object.keys(st.filters).forEach((k) => { const q = st.filters[k].toLowerCase(); if (q) rows = rows.filter((r) => String(r[k] === null || r[k] === undefined ? "" : r[k]).toLowerCase().includes(q)); });
    if (st.sort) rows.sort((a, b) => (st.dir === "asc" ? 1 : -1) * cmp(a[st.sort], b[st.sort]));
    tbody.replaceChildren();
    rows.forEach((r) => {
      if (r.__tr) { tbody.appendChild(r.__tr); return; }
      const tr = document.createElement("tr"); tr.dataset.key = r[st.key];
      st.cols.forEach((c) => tr.appendChild(renderCell(c, r)));
      tbody.appendChild(tr);
    });
    const count = t.querySelector("[data-count]");
    if (count) count.textContent = rows.length + " row" + (rows.length === 1 ? "" : "s") + (rows.length !== rowData(t).length ? " (filtered from " + rowData(t).length + ")" : "");
    if (table) table.classList.toggle("hidden", rows.length === 0);
    if (empty) empty.classList.toggle("hidden", rows.length !== 0);
    t.querySelectorAll("th .sort").forEach((b) => { if (b.dataset.sort === st.sort) b.dataset.dir = st.dir; else delete b.dataset.dir; });
  }
  function csv(t) {
    const st = tableState(t);
    const esc = (v) => { const s = v === null || v === undefined ? "" : String(v); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
    const lines = [st.cols.map((c) => esc(c.label)).join(",")].concat(rowData(t).map((r) => st.cols.map((c) => esc(r[c.key])).join(",")));
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const a = el("a"); a.href = URL.createObjectURL(blob); a.download = (t.dataset.table || "table") + ".csv"; a.click(); URL.revokeObjectURL(a.href);
  }
  document.querySelectorAll("[data-table]").forEach((t) => {
    t.addEventListener("click", (ev) => {
      const s = ev.target.closest("th .sort");
      if (s) { const st = tableState(t); if (st.sort === s.dataset.sort) st.dir = st.dir === "asc" ? "desc" : "asc"; else { st.sort = s.dataset.sort; st.dir = "asc"; } applyTable(t); }
      if (ev.target.closest("[data-csv]")) csv(t);
    });
    t.querySelectorAll("[data-filter]").forEach((inp) => inp.addEventListener("input", () => { tableState(t).filters[inp.dataset.filter] = inp.value; applyTable(t); }));
  });
  function setRows(t, items) {
    const st = tableState(t);
    st.rows = items.map((r) => Object.assign({}, r));
    applyTable(t);
  }

  /* ------------------------------------------------------------ polling */
  let lastUpdate = null;
  function liveIndicator(state, text) {
    const n = document.getElementById("live"); if (!n) return;
    n.className = "live " + state; n.querySelector(".txt").textContent = text;
  }
  function tickAgo() {
    const n = document.getElementById("live");
    if (!n || lastUpdate === null || n.classList.contains("err")) return;
    n.querySelector(".txt").textContent = "updated " + Math.max(0, Math.round((Date.now() - lastUpdate) / 1000)) + "s ago";
  }
  setInterval(tickAgo, 1000);
  function poll(baseMs, fn) {
    let delay = baseMs, failures = 0;
    function run() {
      if (document.hidden) { setTimeout(run, delay); return; }
      Promise.resolve().then(fn).then(() => { failures = 0; delay = baseMs; }, (e) => {
        failures += 1; delay = Math.min(baseMs * Math.pow(2, failures), 60000);
        liveIndicator("err", "refresh failed (" + failures + "x, retry in " + Math.round(delay / 1000) + "s): " + e);
      }).then(() => setTimeout(run, delay));
    }
    setTimeout(run, delay);
  }
  async function getJSON(url) {
    const r = await fetch(url, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }
  function patchTiles(data) {
    document.querySelectorAll("[data-tile]").forEach((n) => { const k = n.dataset.tile; if (k in data) n.textContent = data[k] === null || data[k] === undefined ? "—" : data[k]; });
  }
  function patchCounts(counts) {
    document.querySelectorAll("[data-count]").forEach((n) => { const k = n.dataset.count; if (k && k in counts) n.textContent = counts[k] === null || counts[k] === undefined ? "" : counts[k]; });
  }
  /* nav counts: every page, every 10 s */
  poll(10000, async () => { const c = await getJSON("/api/counts"); patchCounts(c); });
  /* page data: body[data-source] is a JSON endpoint whose keys match [data-tile] keys and [data-table] ids */
  if (body.dataset.source) {
    const ms = parseInt(body.dataset.pollMs || "5000", 10);
    poll(ms, async () => {
      main.classList.add("refetching");
      try {
        const data = await getJSON(body.dataset.source);
        if (data.tiles) patchTiles(data.tiles);
        document.querySelectorAll("[data-table]").forEach((t) => { const items = data[t.dataset.table]; if (Array.isArray(items)) setRows(t, items); else if (items && Array.isArray(items.items)) setRows(t, items.items); });
        lastUpdate = Date.now(); liveIndicator("ok", "updated 0s ago");
      } finally { main.classList.remove("refetching"); }
    });
  }
  /* tables with their own source (no page source) */
  document.querySelectorAll("[data-table][data-source]").forEach((t) => {
    poll(parseInt(body.dataset.pollMs || "8000", 10), async () => {
      const data = await getJSON(t.dataset.source);
      const items = Array.isArray(data) ? data : (data.items || data[t.dataset.table] || []);
      setRows(t, items); lastUpdate = Date.now(); liveIndicator("ok", "updated 0s ago");
    });
  });

  /* ------------------------------------------------------------ actions: preview -> confirm -> execute */
  const dialog = document.getElementById("confirm");
  function serialize(form) {
    const out = {};
    new FormData(form).forEach((v, k) => { if (k in out) { if (!Array.isArray(out[k])) out[k] = [out[k]]; out[k].push(v); } else out[k] = v; });
    form.querySelectorAll("input[type=checkbox]").forEach((c) => { if (!(c.name in out)) out[c.name] = []; else if (c.type === "checkbox" && !Array.isArray(out[c.name])) out[c.name] = [out[c.name]]; });
    form.querySelectorAll("select[multiple]").forEach((s) => { out[s.name] = Array.from(s.selectedOptions).map((o) => o.value); });
    return out;
  }
  async function postJSON(url, payload) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(payload) });
    let data; try { data = await r.json(); } catch (e) { data = { ok: false, error: "HTTP " + r.status }; }
    data.__status = r.status; return data;
  }
  function showBanner(target, kind, text, cli, links) {
    const slot = target || document.getElementById("banner"); if (!slot) return;
    slot.className = "banner " + kind; slot.replaceChildren();
    slot.appendChild(el("div", null, text));
    if (links && links.length) { const p = el("div"); links.forEach((l) => { const a = el("a", "mono", l.text); a.href = l.href; p.appendChild(a); p.appendChild(document.createTextNode(" ")); }); slot.appendChild(p); }
    if (cli) { slot.appendChild(el("div", "muted small", "Equivalent command:")); slot.appendChild(el("pre", "code", cli)); }
    slot.classList.remove("hidden");
  }
  function confirmWith(preview) {
    return new Promise((resolve) => {
      if (!dialog || typeof dialog.showModal !== "function") { resolve(window.confirm("Run?\n\n" + preview.cli)); return; }
      dialog.querySelector('[data-confirm="what"]').textContent = preview.what || "";
      dialog.querySelector('[data-confirm="cli"]').textContent = preview.cli || "";
      const dl = dialog.querySelector('[data-confirm="params"]'); dl.replaceChildren();
      Object.keys(preview.params || {}).forEach((k) => { dl.appendChild(el("dt", null, k)); const v = preview.params[k]; dl.appendChild(el("dd", "mono", Array.isArray(v) ? v.join(", ") : (typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)))); });
      dialog.querySelector('[data-confirm="expiry"]').textContent = preview.expires_at ? "This confirmation token expires at " + preview.expires_at + " (UTC)." : "";
      const ok = dialog.querySelector('[data-confirm="ok"]'), cancel = dialog.querySelector('[data-confirm="cancel"]');
      const done = (v) => { ok.onclick = cancel.onclick = null; dialog.onclose = null; dialog.close(); resolve(v); };
      ok.onclick = () => done(true); cancel.onclick = () => done(false); dialog.onclose = () => resolve(false);
      dialog.showModal(); ok.focus();
    });
  }
  async function runAction(action, params, statusEl, bannerEl) {
    const setStatus = (t) => { if (statusEl) statusEl.textContent = t; };
    setStatus("previewing…");
    const preview = await postJSON("/api/" + action + "/preview", params);
    if (!preview.ok) { setStatus(""); showBanner(bannerEl, "error", (preview.errors || [preview.error || "rejected"]).join("; "), preview.cli); return preview; }
    setStatus("awaiting confirmation");
    const yes = await confirmWith(preview);
    if (!yes) { setStatus("cancelled"); return { ok: false, cancelled: true }; }
    setStatus("running…");
    const result = await postJSON("/api/" + action, { token: preview.token, params: preview.params });
    setStatus("");
    if (result.ok) {
      const links = (result.workflow_ids || []).map((w) => ({ text: w, href: "/workflows/" + encodeURIComponent(w) }));
      if (result.workflow_id && !links.length) links.push({ text: result.workflow_id, href: "/workflows/" + encodeURIComponent(result.workflow_id) });
      showBanner(bannerEl, "ok", result.message || ("Accepted" + (result.via ? " via " + result.via : "") + "."), result.cli, links);
    } else showBanner(bannerEl, result.__status === 502 ? "warn" : "error", result.error || (result.errors || []).join("; ") || "failed", result.cli);
    return result;
  }
  document.querySelectorAll("form[data-action]").forEach((form) => {
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const params = serialize(form);
      if (ev.submitter && ev.submitter.name) params[ev.submitter.name] = ev.submitter.value;
      runAction(form.dataset.action, params, form.querySelector("[data-status]"), document.getElementById("banner"));
    });
  });
  document.querySelectorAll("button[data-action]").forEach((btn) => {
    btn.addEventListener("click", () => {
      let params = {}; try { params = JSON.parse(btn.dataset.params || "{}"); } catch (e) { params = {}; }
      const reasonSel = btn.dataset.reasonFrom; if (reasonSel) { const r = document.querySelector(reasonSel); if (r) params.reason = r.value; }
      runAction(btn.dataset.action, params, document.querySelector(btn.dataset.status || "#action-status"), document.getElementById("banner"));
    });
  });
  /* create form: source radio shows one block */
  document.querySelectorAll("input[name=source]").forEach((r) => r.addEventListener("change", () => {
    document.querySelectorAll("[data-source-block]").forEach((b) => b.classList.toggle("hidden", b.dataset.sourceBlock !== r.value));
  }));

  /* ------------------------------------------------------------ chart table-view toggle, tabs, evaluator toggle */
  document.querySelectorAll("[data-chart-tabs]").forEach((tabs) => {
    tabs.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      tabs.querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", x === b ? "true" : "false"));
      const fig = tabs.parentElement;
      fig.querySelectorAll("[data-view]").forEach((v) => v.classList.toggle("hidden", v.dataset.view !== b.dataset.view));
    }));
  });
  document.querySelectorAll("[role=tablist] [role=tab]").forEach((tab) => tab.addEventListener("click", () => {
    const list = tab.closest("[role=tablist]");
    list.querySelectorAll("[role=tab]").forEach((t) => t.setAttribute("aria-selected", t === tab ? "true" : "false"));
    document.querySelectorAll("[data-tab-panel]").forEach((p) => p.classList.toggle("hidden", p.dataset.tabPanel !== tab.dataset.tab));
  }));
  document.querySelectorAll("[data-evaluator-toggle]").forEach((sw) => sw.addEventListener("click", () => {
    const on = sw.getAttribute("aria-checked") !== "true";
    sw.setAttribute("aria-checked", on ? "true" : "false"); sw.classList.toggle("on", on);
    document.querySelectorAll("[data-evaluator-only]").forEach((n) => n.classList.toggle("hidden", !on));
    sw.textContent = ""; const i = el("i"); sw.appendChild(i); sw.appendChild(document.createTextNode("evaluator-only: " + (sw.dataset.label || "material") + (on ? " shown" : " hidden")));
  }));

  /* ------------------------------------------------------------ episode: live transcript */
  if (body.dataset.page === "episode" && body.dataset.trial && body.dataset.live === "true") {
    const url = "/api/trial/" + encodeURIComponent(body.dataset.trial) + "/timeline";
    poll(parseInt(body.dataset.pollMs || "3000", 10), async () => {
      const t = await getJSON(url);
      const chat = document.getElementById("chat");
      if (chat && Array.isArray(t.timeline)) {
        chat.replaceChildren();
        t.timeline.filter((r) => r.actor === "agent" || r.actor === "stakeholder").filter((r) => r.kind !== "agent-step" && r.kind !== "tool_call").forEach((r) => {
          const b = el("div", "bubble " + r.actor + " " + (r.kind || ""));
          const who = el("div", "who"); who.appendChild(el("span", "badge " + r.actor, r.actor)); who.appendChild(el("span", "xtype", r.kind));
          if (r.ts_iso) { const tm = el("time", null, r.ts_iso.slice(11, 19)); tm.dateTime = r.ts_iso; who.appendChild(tm); } else who.appendChild(el("span", "flag", "no timestamp"));
          if (r.meta && r.meta.patience_remaining !== undefined && r.meta.patience_remaining !== null) who.appendChild(el("span", "muted", "patience " + r.meta.patience_remaining));
          b.appendChild(who); b.appendChild(el("div", "text", r.text || "")); chat.appendChild(b);
        });
        if (t.live) { const w = el("div", "working"); const d = el("span", "dots"); d.appendChild(el("i")); d.appendChild(el("i")); d.appendChild(el("i")); w.appendChild(d); w.appendChild(el("span", null, "agent is working…")); chat.appendChild(w); }
        const emptyNote = document.getElementById("chat-empty"); if (emptyNote) emptyNote.classList.toggle("hidden", chat.children.length > 0);
      }
      const pat = document.getElementById("patience-live");
      if (pat && t.patience) pat.textContent = (t.patience.end === null || t.patience.end === undefined ? "—" : t.patience.end) + " / " + (t.patience.start === null || t.patience.start === undefined ? "—" : t.patience.start) + " remaining";
      lastUpdate = Date.now(); liveIndicator("ok", "updated 0s ago");
      if (!t.live) window.location.reload();
    });
  }
})();
