# Console design spec (2026-09-13, console stream)

Binding on the look and structure of `swesim/dashboard/**`. Requirements come from the hosted-pipeline brief §2.5 and
§2.5.1; data names from `2026-09-13-interfaces.md` §2 (row models, `IndexReader`). Colour decisions follow the dataviz
skill; the validator output is pasted below and saved at `workspace/evidence/console/palette-validation.txt`.

## 1. Pages and the one question each answers first

| Route | Page | First-screen question | Reader calls |
|---|---|---|---|
| `/` | Home | What is running and what changed? | `trials(status=running)`, `workflows(limit)`, `spend()`, `funnel()`, counts |
| `/scenarios`, `/scenario/{id}` | Scenarios | What tasks exist and how hard are they? | `scenarios`, `scenario_versions`, `scenario` |
| `/trials`, `/trial/{id}` | Trials / Episode | What happened to each attempt? | `trials`, `trial`, `exchanges(evaluator)` + per-trial artifacts |
| `/candidates` | Candidates | Where does the creation funnel lose candidates, and why? | `funnel`, `candidates` |
| `/workflows`, `/workflows/{id}` | Workflows | What did Temporal do? (status, params, failure, cost; deep link for history) | `workflows`, `workflow` + `console.job_progress` |
| `/review`, `/review/{id}` | Review queue | What needs a decision? | `console.review_queue` + scenario artifacts |
| `/leaderboard` | Leaderboard | How do models compare on informative tasks? | `leaderboard`, `scores` |
| `/db`, `/db/{table}` | Database browser | What is in the index, and is it fresh? | `table`, `rebuilds` |
| `/artifacts` | Artifact browser | Which file did this number come from? | `Layout` + `os.scandir` (the one non-reader page) |
| `/create`, `/run` | Create / Run | What exactly will run if I press this? | `console.validate_*`, `cli_for_*` |
| `/spend` | Spend | Who is spending what, against which cap? | `spend`, `costs` |

Every number links to its artifact (`/artifacts?path=...` or a file route). Ids and hashes render as `<span class=id
title=full>` shortened to 12 chars; click copies the full value. Temporal event histories are never duplicated: every
workflow row carries `Settings.workflow_url(workflow_id, run_id)`.

## 2. Type scale, grid, surfaces

- Fonts: UI `--font-sans` (system humanist sans stack), ids/hashes/diffs/logs `--font-mono`. Scale (px): 12 / 13 / 14 /
  16 / 20 / 26 / 40 (hero). Line height 1.5 body, 1.2 headings/values. Tabular figures only in table columns and axis
  ticks; proportional on stat tiles and the hero figure.
- Spacing: 8-px grid (`--s-1..--s-7` = 4, 8, 12, 16, 24, 32, 48). Radii 4 / 8 / 14. Borders 1 px hairline `--line`.
- Surfaces (light / dark): page `#f9f9f7` / `#0d0d0d`; card `#fcfcfb` / `#1a1a19`; inset `#f3f2ee` / `#2c2c2a`;
  overlay `#ffffff` / `#232322`. Ink primary `#0b0b0b` / `#ffffff`, secondary `#52514e` / `#c3c2b7`, muted `#6f6e69` /
  `#9d9c93` (both ≥ 4.5:1 on every surface — measured 4.56–9.72, see evidence).
- Accent (interactive only): `#1c5cab` light text / `#86b6ef` dark text; fill `#2a78d6` / `#3987e5`. Focus ring 2 px accent.
- Theme: `prefers-color-scheme` default, overridden by `html[data-theme]` from the nav toggle (persisted in `localStorage`).

## 3. Palettes

**Categorical — model families, fixed order, never cycled** (slot → family; light / dark):
1 Anthropic `#2a78d6`/`#3987e5` · 2 OpenAI `#eb6834`/`#d95926` · 3 DeepSeek `#1baf7a`/`#199e70` · 4 GLM/Z.ai
`#eda100`/`#c98500` · 5 Qwen `#e87ba4`/`#d55181` · 6 Kimi `#008300`/`#008300` · 7 MiniMax `#4a3aa7`/`#9085e9` ·
8 gpt-oss `#e34948`/`#e66767` · other `#898781` (de-emphasis, not a slot). Family is derived from the model id
(`anthropic/`, `openai/`, `openrouter/deepseek/`, `openrouter/z-ai/`, `openrouter/qwen/`, `openrouter/moonshotai/`,
`openrouter/minimax/`, `openrouter/openai/gpt-oss*`). Text never wears a series colour; a swatch beside the text does.
Charts with more than three families on one axis fold to "other" plus the table, or facet.

Validator output (`node scripts/validate_palette.js ... --mode light --surface "#fcfcfb"`):
```
Palette (light, surface #fcfcfb, categorical): 8 slots
  [PASS] Lightness band         all 8 inside L 0.43–0.77
  [PASS] Chroma floor           all 8 >= 0.1
  [PASS] CVD separation         worst adjacent #eda100↔#1baf7a ΔE 9.1 (protan) · tritan 5.8
  [PASS] Normal-vision floor    worst adjacent #e87ba4↔#eda100 ΔE 19.6 (normal)
  [WARN] Contrast vs surface    below 3:1 — relief required (visible labels or table view): [["#1baf7a",2.74],["#eda100",2.11],["#e87ba4",2.62]]
  → ALL CHECKS PASS   exit=0
Palette (dark, surface #1a1a19, categorical): 8 slots
  [PASS] Lightness band         all 8 inside L 0.48–0.67
  [PASS] Chroma floor           all 8 >= 0.1
  [PASS] CVD separation         worst adjacent #c98500↔#199e70 ΔE 8.4 (protan) · tritan 8.7
  [PASS] Normal-vision floor    worst adjacent #d55181↔#c98500 ΔE 19.3 (normal)
  [PASS] Contrast vs surface    all 8 >= 3:1
  → ALL CHECKS PASS   exit=0
```
The light-mode contrast WARN is relieved as the skill requires: every chart ships direct labels and a table view.

**Sequential (magnitudes: pass rate, cost, cells of the per-scenario matrix)** — blue 100→700 (`#cde2fb … #0d366b`),
lightest = near zero; dark mode anchors the ramp from 600 downwards. Validated monotone (`--ordinal`: "Lightness
monotone PASS"); adjacent-ΔL and CVD gates are categorical/ordinal-only and not applied to a continuous ramp.

**Status (fixed, never themed; always icon + label, never colour alone)**: valid/good `#0ca30c` ●, running/pending
`#fab219` ◐ (pulses), failed/invalid/critical `#d03b3b` ✕, unresolved/serious `#ec835a` ◇, neutral/archived gray ○.
Text variants (`--good-ink` etc.) are the ≥ 4.5:1 steps listed in the evidence file. Difficulty labels use status tokens:
informative = good, saturated = neutral gray, broken = critical.

**Funnel decision.** An 8-step ordinal ramp cannot clear the ordinal gate (adjacent ΔL ≥ 0.06) with the documented
13-step ramp (measured 0.046–0.049 per step), so the creation funnel is drawn as horizontal stacked bars per stage in
status tokens (kept = good, rejected = critical, error = serious, pending = gray) with 2-px surface gaps, direct labels
and a table twin. Cost if wrong: one chart re-skinned.

## 4. Component inventory

nav (persistent left rail, live counts from `/api/counts`: running trials, awaiting review, funnel pending; global search
box → `/search?q=`; theme toggle; Temporal link) · breadcrumbs · stat tile (label, value, delta/source link) · hero
figure (one per page, ≥ 40 px) · table (sticky header, sortable, column filters, paging, right-aligned numerics, mono
ids, empty state that says what to do next, CSV export; server declares columns once and the client re-renders from the
same spec on poll) · chip (status icon + label) · id (shortened, copy-on-click, full on hover) · model chip (family
swatch + tier label) · meter (patience; same-ramp track) · funnel (stacked bars) · bar chart (one axis, ≤ 24-px bars,
4-px caps, legend + selective direct labels, `<title>` tooltips with keyboard focus, table twin) · heat matrix
(sequential cells, value in cell) · timeline / transcript (two-party bubbles: agent left, stakeholder right; question and
confirmation highlighted; patience after each reply; exchange type badge) · code/diff block (mono, `+`/`-` washes) ·
confirm dialog (`<dialog>`: exact CLI equivalent, params table, token expiry, Confirm/Cancel; focus trapped) · banner
(ok/warn/error with the CLI fallback) · tree (artifact browser, `<details>` per directory, sizes, sha256, preview).

## 5. Evaluator gate and mutations

`Identity(email, evaluator)` from IAP headers (`X-Goog-Authenticated-User-Email` + JWT `X-Goog-IAP-JWT-Assertion`
verified with `google.auth`'s `id_token.verify_token(..., certs_url="https://www.gstatic.com/iap/verify/public_key",
audience=settings.iap_audience)`), `evaluator = email in Settings.evaluators`; no headers → anonymous, `evaluator=False`.
`?evaluator=1` is gone. Evaluator-only material (`redacted_terms`, `withheld_sentences`, `disclosed_fact_ids`, `tier`,
`regenerated`, `audit`, tool audit, author-only facts, hidden test assets, oracle, judge/draft metadata, raw
record/transcript/spec) renders only for evaluator identities, behind an explicit toggle. A test enumerates every
non-evaluator route and asserts none of those keys or sentinel values appears.

Mutations (create, run, cancel, approve/reject, label, retry candidate, index rebuild) are two-step: `POST
/api/<action>/preview` returns the exact CLI equivalent and an HMAC-signed token bound to the canonical params, the actor
and a 10-minute expiry; the second `POST /api/<action>` carries the token. Both check `Origin` scheme+host+port against
`Settings.console_url` (or the request's own origin). Every attempt writes an `OperatorActionRow` (`add_operator_action`
on the store, or `evidence/console/actions.jsonl` when the index is disabled).

## 6. Accessibility and performance

Keyboard: every control is a native element or has `tabindex`/`role`; skip link; focus-visible ring; tables sort with
Enter/Space on headers; dialogs trap focus and close on Escape. Contrast ≥ 4.5:1 for all text tokens (measured, evidence
file). No information by colour alone (icon + label on every status; swatch + name on every series). Pages render from
one reader call set with `limit ≤ 500`; live updates poll JSON endpoints (`/api/...`) and patch rows in place, holding the
previous render at reduced opacity while a refetch is in flight; no full reloads.
