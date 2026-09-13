/* swesim dashboard: live polling, filters, tabs, label form. Vanilla JS; every value is inserted with textContent. */
(function () {
  "use strict";
  const body = document.body;
  const page = body.dataset.page;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function fmtElapsed(s) {
    if (s === null || s === undefined) return "—";
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? h + "h " + String(m).padStart(2, "0") + "m" : m ? m + "m " + String(sec).padStart(2, "0") + "s" : sec + "s";
  }
  function statusClass(s) {
    s = (s || "").toLowerCase();
    if (s === "running" || s === "pending" || s === "launched" || s === "requested") return "warn pulse";
    if (s === "finished" || s === "completed" || s === "done") return "good";
    if (s === "failed" || s === "launch-failed" || s === "runner-error" || s === "error" || s === "unit-failed") return "critical";
    return "";
  }
  function setChip(node, text, cls) {
    if (!node) return;
    node.textContent = text || "—";
    node.className = "chip " + (cls || "");
  }
  function setText(row, field, text) {
    const n = row.querySelector('[data-f="' + field + '"]');
    if (n) n.textContent = (text === null || text === undefined || text === "") ? "—" : String(text);
  }
  function liveIndicator(state, text) {
    const n = document.getElementById("live");
    if (!n) return;
    n.className = "live " + state;
    n.querySelector(".txt").textContent = text;
  }
  let lastUpdate = null;
  /* One poller per page: the next request is scheduled only after the previous one settles (no overlap), and the
     interval doubles on each consecutive error (capped at 60 s) until a request succeeds. */
  function poll(baseMs, fn) {
    let delay = baseMs, failures = 0, stopped = false;
    function schedule() { if (!stopped) setTimeout(run, delay); }
    function run() {
      if (document.hidden) { schedule(); return; }
      Promise.resolve().then(fn).then(function () { failures = 0; delay = baseMs; }, function (e) {
        failures += 1; delay = Math.min(baseMs * Math.pow(2, failures), 60000);
        liveIndicator("err", "refresh failed (" + failures + "x, retry in " + Math.round(delay / 1000) + "s): " + e);
      }).then(schedule);
    }
    schedule();
    return { stop: function () { stopped = true; } };
  }
  function tickAgo() {
    const n = document.getElementById("live");
    if (!n || lastUpdate === null || n.classList.contains("err")) return;  // keep the backoff message readable
    const ago = Math.max(0, Math.round((Date.now() - lastUpdate) / 1000));
    n.querySelector(".txt").textContent = "updated " + ago + "s ago";
  }
  setInterval(tickAgo, 1000);

  /* ---------------------------------------------------------------- overview */
  function fillTrialRow(row, t) {
    setText(row, "scenario", t.scenario_id);
    setText(row, "condition", t.condition);
    setText(row, "model", t.model);
    setText(row, "profile", t.brief_profile);
    setText(row, "purpose", t.purpose);
    setChip(row.querySelector('[data-f="status"]'), t.status, statusClass(t.status));
    const flag = row.querySelector('[data-f="unresolved"]');
    if (flag) { flag.textContent = t.unresolved && !t.running ? "unresolved" : ""; flag.className = t.unresolved && !t.running ? "flag" : ""; }
    setText(row, "elapsed", t.elapsed_s === null || t.elapsed_s === undefined ? null : fmtElapsed(t.elapsed_s) + (t.running ? " …" : ""));
    const elapsed = row.querySelector('[data-f="elapsed"]');
    if (elapsed) elapsed.title = "started " + (t.started_at || "—") + " · finished " + (t.finished_at || "—");
    const pat = row.querySelector('[data-f="patience"]');
    if (pat) {
      pat.replaceChildren();
      if (t.patience_start !== null && t.patience_start !== undefined) {
        const end = t.patience_end === null || t.patience_end === undefined ? t.patience_start : t.patience_end;
        pat.appendChild(document.createTextNode(end + " / " + t.patience_start + " "));
        const m = el("span", "meter" + (end <= 0 ? " out" : end * 3 <= t.patience_start ? " low" : ""));
        const i = el("i"); i.style.width = Math.max(0, Math.min(100, Math.round(100 * end / t.patience_start))) + "%";
        m.appendChild(i); pat.appendChild(m);
      } else { pat.textContent = t.live_error ? "live: " + t.live_error : "—"; }
    }
    const out = row.querySelector('[data-f="outcome"]');
    if (out) {
      out.replaceChildren();
      if (t.outcome) { out.appendChild(el("span", "chip " + (t.success === 1 ? "good" : (t.outcome_code >= 5 ? "warn" : "critical")), t.outcome)); }
      else if (t.unresolved && !t.running) { out.appendChild(el("span", "flag", t.error_type ? "error " + t.error_type : "no verifier record")); out.appendChild(document.createTextNode(" ")); out.appendChild(el("span", "muted small", "(" + (t.source_file || "no files") + ")")); }
      else out.textContent = "—";
    }
    setText(row, "reward", t.reward === null || t.reward === undefined ? null : t.reward);
    setText(row, "label", t.label);
    row.dataset.status = t.status || "";
  }
  function fillScenarioRow(row, s) {
    setText(row, "source", s.source_label);
    const st = row.querySelector('[data-f="stages"]');
    if (st) {
      st.replaceChildren();
      (s.stages || []).forEach(function (x) {
        const c = el("span", "chip " + (x.state === "done" ? "good" : x.state === "failed" ? "critical" : "pending"), x.stage);
        c.title = x.detail || ""; st.appendChild(c);
      });
    }
    setText(row, "judge", s.judge ? ("keep=" + s.judge.keep + " · rubric " + s.judge.verified_rubric_score + "/3 · complexity " + s.judge.complexity) : null);
    setText(row, "facts", s.facts);
    setText(row, "controls", (s.controls.plausible_wrong || 0) + " wrong · " + (s.controls.alternative_correct || 0) + " alt · " + (s.controls.tamper || 0) + " tamper");
    const v = row.querySelector('[data-f="validation"] .chip:not([data-f])');
    if (v) setChip(v, s.validation_status || "not validated", s.validation_status === "valid" ? "good" : s.validation_status === "invalid" ? "critical" : "pending");
    const rv = row.querySelector('[data-f="review"]');
    if (rv) {
      const needed = s.approval && s.approval.review_needed, awaiting = !s.approval && s.awaiting_approval;
      rv.classList.toggle("hidden", !(needed || awaiting));
      rv.className = "chip warn" + (awaiting ? " pulse" : "") + ((needed || awaiting) ? "" : " hidden");
      rv.textContent = needed ? "review needed" : awaiting ? "awaiting approval" : "";
      rv.href = "/review/" + encodeURIComponent(s.id);
    }
    setText(row, "runs", (s.runs.calibration || 0) + " cal · " + (s.runs.comparison || 0) + " cmp · " + (s.runs.demonstration || 0) + " demo" + (s.runs.unresolved ? " · " + s.runs.unresolved + " unresolved" : ""));
    const sc = row.querySelector('[data-f="scores"]');
    if (sc) {
      sc.replaceChildren();
      const scores = s.scores || {};
      if (scores.unreadable) sc.appendChild(el("span", "flag", "unreadable scores.json"));
      else if (!(scores.groups || []).length) sc.textContent = "—";
      else (scores.groups || []).forEach(function (g, i) {
        if (i) sc.appendChild(document.createElement("br"));
        sc.appendChild(document.createTextNode((g.condition || g.key) + " · " + (g.model || "—") + ": pass@" + g.requested_k + " "));
        sc.appendChild(g.pass_at_k === null || g.pass_at_k === undefined ? el("span", "muted", "n/a") : document.createTextNode(Number(g.pass_at_k).toFixed(2)));
        sc.appendChild(el("span", "muted", " (" + g.n_valid + "/" + g.n_total + ")"));
      });
    }
  }
  function patchTable(tbodyId, tplId, items, key, fill, hrefBase) {
    const tbody = document.getElementById(tbodyId), tpl = document.getElementById(tplId);
    if (!tbody || !tpl) return;
    const seen = new Set();
    items.forEach(function (it) {
      const id = it[key]; seen.add(id);
      let row = tbody.querySelector('tr[data-id="' + (window.CSS && CSS.escape ? CSS.escape(String(id)) : String(id).replace(/["\\]/g, "\\$&")) + '"]');
      if (!row) {
        row = tpl.content.firstElementChild.cloneNode(true);
        row.dataset.id = id;
        const a = row.querySelector('[data-f="id"] a');
        if (a) { a.textContent = id; a.href = hrefBase + encodeURIComponent(id); }
        tbody.appendChild(row);
      }
      fill(row, it);
    });
    Array.from(tbody.querySelectorAll("tr[data-id]")).forEach(function (r) { if (!seen.has(r.dataset.id)) r.remove(); });
    const empty = document.getElementById(tbodyId + "-empty");
    if (empty) empty.classList.toggle("hidden", items.length > 0);
    const table = tbody.closest("table");
    if (table) table.classList.toggle("hidden", items.length === 0);
  }
  function refreshOverview() {
    return fetch("/api/overview", { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }).then(function (ov) {
      patchTable("trials-body", "trial-row", ov.trials || [], "trial_id", fillTrialRow, "/trial/");
      patchTable("scenarios-body", "scenario-row", ov.scenarios || [], "id", fillScenarioRow, "/scenario/");
      const s = ov.summary || {};
      Object.keys(s).forEach(function (k) { const n = document.querySelector('[data-tile="' + k + '"]'); if (n) n.textContent = s[k]; });
      lastUpdate = Date.now(); liveIndicator("ok", "updated 0s ago");
    });
  }
  if (page === "overview") {
    lastUpdate = Date.now();
    poll(parseInt(body.dataset.pollMs || "5000", 10), refreshOverview);
  }

  /* ---------------------------------------------------------------- console: forms, jobs, review */
  function showBanner(kind, title, lines, cli, links) {
    const b = document.getElementById("banner");
    if (!b) return;
    b.replaceChildren();
    b.className = "banner " + kind;
    b.appendChild(el("strong", "", title));
    (lines || []).forEach(function (t) { b.appendChild(el("div", "", t)); });
    (links || []).forEach(function (l) { const a = el("a", "mono", l.text); a.href = l.href; const d = el("div"); d.appendChild(a); b.appendChild(d); });
    if (cli) { b.appendChild(el("div", "muted small", "Equivalent command:")); b.appendChild(el("pre", "", cli)); }
  }
  function formJSON(form) {
    const data = {};
    new FormData(form).forEach(function (v, k) {
      if (k in data) { if (!Array.isArray(data[k])) data[k] = [data[k]]; data[k].push(v); } else data[k] = v;
    });
    Array.from(form.querySelectorAll("select[multiple]")).forEach(function (s) {
      data[s.name] = Array.from(s.selectedOptions).map(function (o) { return o.value; });
    });
    Array.from(form.querySelectorAll("input[type=checkbox]")).forEach(function (c) {
      if (!(c.name in data)) data[c.name] = [];
      else if (!Array.isArray(data[c.name])) data[c.name] = [data[c.name]];
    });
    return data;
  }
  function wireConsoleForm(form, onSuccess) {
    const status = form.querySelector("#form-status") || document.getElementById("form-status");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      const data = formJSON(form);
      if (ev.submitter && ev.submitter.name) data[ev.submitter.name] = ev.submitter.value;
      if (status) status.textContent = "starting…";
      fetch(form.dataset.endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) })
        .then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); })
        .then(function (res) {
          const j = res.body || {};
          if (status) status.textContent = "";
          if (res.status === 200 && j.ok) { onSuccess(j, form); return; }
          if (res.status === 400) showBanner("warn", j.error || "Please fix the form", j.errors || (j.reasons ? ["gates: " + j.reasons.join("; ")] : []), j.cli);
          else if (res.status === 409) showBanner("warn", j.error || "Refused", j.current_spec_hash ? ["spec hash on disk: " + j.current_spec_hash] : [], null);
          else showBanner("error", j.error || ("request failed (" + res.status + ")"), [], j.cli);
        })
        .catch(function (e) { if (status) status.textContent = ""; showBanner("error", "request failed: " + e, [], null); });
    });
  }
  if (page === "console") {
    const form = document.querySelector("form.console");
    if (form) {
      Array.from(form.querySelectorAll("input[name=source]")).forEach(function (r) {
        r.addEventListener("change", function () {
          Array.from(form.querySelectorAll("[data-source]")).forEach(function (d) { d.classList.toggle("hidden", d.dataset.source !== r.value); });
        });
      });
      wireConsoleForm(form, function (j) {
        const ids = j.workflow_ids || [];
        showBanner("ok", "Started " + ids.length + " workflow" + (ids.length === 1 ? "" : "s") + " via " + (j.via || "Temporal"), [],
          j.cli, ids.map(function (id) { return { text: id, href: (form.dataset.jobs || "/jobs/") + encodeURIComponent(id) }; }));
      });
    }
  }
  if (page === "jobs") {
    function fillJob(row, j) {
      setText(row, "type", j.type);
      setChip(row.querySelector('[data-f="status"]'), j.status, statusClass(j.status));
      setText(row, "started", j.started_at);
      setText(row, "summary", j.summary);
    }
    function refreshJobs() {
      return fetch("/api/jobs", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (j) {
        if (!j.ok) throw new Error("jobs unavailable: " + (j.error || ""));
        patchTable("jobs-body", "job-row", j.jobs || [], "workflow_id", fillJob, "/jobs/");
        lastUpdate = Date.now(); liveIndicator("ok", "updated 0s ago");
      });
    }
    if (document.getElementById("live")) { lastUpdate = Date.now(); poll(parseInt(body.dataset.pollMs || "5000", 10), refreshJobs); }
  }
  if (page === "job") {
    const btn = document.getElementById("cancel-job");
    if (btn) btn.addEventListener("click", function () {
      const wid = body.dataset.job;
      if (!window.confirm("Cancel workflow " + wid + "? Running Harbor units are not killed; the workflow stops after its current step.")) return;
      const out = document.getElementById("cancel-status");
      fetch(btn.dataset.endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true }) })
        .then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); })
        .then(function (res) {
          if (res.status === 200 && res.body.ok) { out.textContent = "cancelled"; setTimeout(function () { location.reload(); }, 800); }
          else showBanner("error", res.body.error || "cancel failed", [], res.body.cli);
        }).catch(function (e) { showBanner("error", "cancel failed: " + e, [], null); });
    });
  }
  if (page === "review-item") {
    const form = document.getElementById("review-form");
    if (form) wireConsoleForm(form, function (j) {
      showBanner("ok", (j.decision === "approve" ? "Approved" : "Rejected") + " " + j.scenario_id + " via " + (j.via || ""), [], null);
      setTimeout(function () { location.reload(); }, 800);
    });
  }

  /* ---------------------------------------------------------------- runs filters */
  if (page === "runs") {
    const selects = Array.from(document.querySelectorAll(".toolbar select[data-filter]"));
    const rows = Array.from(document.querySelectorAll("#trials-body tr[data-id]"));
    const count = document.getElementById("filter-count");
    function apply() {
      let shown = 0;
      rows.forEach(function (r) {
        const ok = selects.every(function (s) { return !s.value || (r.dataset[s.dataset.filter] || "") === s.value; });
        r.classList.toggle("hidden", !ok); if (ok) shown++;
      });
      if (count) count.textContent = shown + " of " + rows.length + " attempts";
    }
    selects.forEach(function (s) { s.addEventListener("change", apply); });
    const reset = document.getElementById("filter-reset");
    if (reset) reset.addEventListener("click", function () { selects.forEach(function (s) { s.value = ""; }); apply(); });
    apply();
  }

  /* ---------------------------------------------------------------- episode */
  if (page === "episode") {
    const tid = body.dataset.trial;
    Array.from(document.querySelectorAll(".tabs button")).forEach(function (b) {
      b.addEventListener("click", function () {
        Array.from(document.querySelectorAll(".tabs button")).forEach(function (x) { x.setAttribute("aria-selected", x === b ? "true" : "false"); });
        Array.from(document.querySelectorAll("[data-tab-panel]")).forEach(function (p) { p.classList.toggle("hidden", p.dataset.tabPanel !== b.dataset.tab); });
      });
    });
    const form = document.getElementById("labelform");
    if (form) {
      form.addEventListener("submit", function (ev) {
        ev.preventDefault();
        const out = document.getElementById("labelresult");
        const data = { label: form.label.value, note: form.note.value };
        if (!data.label) { out.textContent = "choose a label"; return; }
        fetch("/trial/" + encodeURIComponent(tid) + "/label", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) })
          .then(function (r) { out.textContent = r.ok ? "saved" : "error " + r.status; })
          .catch(function (e) { out.textContent = "error " + e; });
      });
    }
    function bubble(r) {
      const m = r.meta || {};
      const isReview = r.kind === "review";
      const refusal = r.actor === "stakeholder" && (m.tier === "refused" || /^I have to step away now/.test(r.text || ""));
      const b = el("div", "bubble " + r.actor + (isReview ? " review" : "") + (refusal ? " refusal" : ""));
      const who = el("div", "who");
      who.appendChild(el("span", "badge " + r.actor, r.actor === "agent" ? "agent" : "stakeholder"));
      who.appendChild(el("span", "", refusal ? "refusal" : isReview ? (r.actor === "agent" ? "review request" : "review") : (r.actor === "agent" ? "question" : "reply")));
      const t = el("time", "", r.ts_iso ? r.ts_iso.slice(11, 19) : "no timestamp"); if (r.ts_iso) t.dateTime = r.ts_iso; who.appendChild(t);
      b.appendChild(who);
      b.appendChild(el("div", "text", r.text));
      if (body.dataset.evaluator === "1" && r.actor === "stakeholder" && (m.disclosed_fact_ids || m.tier !== undefined)) {
        b.appendChild(el("div", "audit", "audit (evaluator-only): tier " + (m.tier || "—") + " · patience remaining " + (m.patience_remaining === undefined ? "—" : m.patience_remaining) + " · disclosed fact ids: " + ((m.disclosed_fact_ids || []).join(", ") || "none")));
      }
      return b;
    }
    function updateMeter(p) {
      const m = document.getElementById("patience");
      if (!m || !p || p.start === null || p.start === undefined) return;
      const end = p.end === null || p.end === undefined ? p.start : p.end;
      m.querySelector(".pt").textContent = end + " / " + p.start + " remaining";
      const bar = m.querySelector(".meter"); if (bar) { bar.className = "meter" + (end <= 0 ? " out" : end * 3 <= p.start ? " low" : ""); bar.querySelector("i").style.width = Math.max(0, Math.min(100, Math.round(100 * end / p.start))) + "%"; }
      m.querySelector(".pq").textContent = "questions " + (p.questions ?? 0) + " · reviews " + (p.reviews ?? 0);
    }
    function refreshEpisode() {
      const url = "/api/trial/" + encodeURIComponent(tid) + "/timeline" + (body.dataset.evaluator === "1" ? "?evaluator=1" : "");
      return fetch(url, { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }).then(function (data) {
        if (data.status && data.status.status !== "running") { location.reload(); return; }
        const chat = document.getElementById("chat");
        if (chat) {
          const rows = (data.timeline || []).filter(function (r) { return (r.actor === "agent" || r.actor === "stakeholder") && r.kind !== "agent-step" && r.kind !== "tool_call"; });
          const nodes = rows.map(bubble);
          const working = el("div", "working"); working.appendChild(el("span", "dots")); working.querySelector(".dots").append(el("i"), el("i"), el("i")); working.appendChild(el("span", "", "agent is working…"));
          nodes.push(working);
          chat.replaceChildren.apply(chat, nodes);
          const empty = document.getElementById("chat-empty"); if (empty) empty.classList.toggle("hidden", rows.length > 0);
        }
        updateMeter(data.patience);
        lastUpdate = Date.now(); liveIndicator("ok", "updated 0s ago" + (data.live_transcript_error ? " · transcript unavailable: " + data.live_transcript_error : ""));
      });
    }
    if (body.dataset.live === "true") {
      lastUpdate = Date.now();
      poll(parseInt(body.dataset.pollMs || "3000", 10), refreshEpisode);
    }
  }
})();
