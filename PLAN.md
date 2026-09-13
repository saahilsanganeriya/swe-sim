# swesim milestones and acceptance checks

Plan: [implementation plan](docs/superpowers/plans/2026-09-12-swesim-implementation.md).
Spec is read-only during this plan review. A box is checked only after
`bash scripts/verify.sh N` succeeds. Checks are cumulative and offline; the
cloud execution required to produce evidence is an explicit implementation step.

- [x] **M1 Models** — Task 1: dependencies installed; strict model, asset, group and hash round-trip tests pass.
- [x] **M2 Grader** — Tasks 2–3: safe extraction, source import, exact-ID/exit cross-checks, genuine base outcomes and content hashes pass tests.
- [x] **M3 Compiler** — Tasks 4–5: three task variants render, assets are verified, root collector is packaged, PatchAgent implements Harbor's abstract API. This milestone alone claims no cloud run.
- [x] **M4 Authored SWE-bench task** — Tasks 6–7: mine/judge/draft; unattended approval by code-owned gates (recorded as auto-validators, review_needed=true) bound to spec hash; executed Modal nop/oracle/wrong/tamper controls with complete base/gold test evidence; valid validation file.
- [x] **M5 Interaction** — Tasks 8–9: durable stakeholder sessions, serialized freeze, bounded replies; a real mini-swe-agent interactive episode with a paired question/reply, trajectory and transcript hash matching collection.
- [x] **M6 Repo URL** — Task 10: mine a GitHub repository URL without a PR override; retain filter/build rejections; automatically select a feature candidate, auto-approve it through the gates, validate and run it. A failed search is recorded honestly and leaves M6 incomplete.
- [x] **M7 Pipeline and dataset** — Task 12 then Task 11: both scenarios registered with frozen evidence; actual attempts in full/hidden/interactive and hidden runs on both natural/withheld profiles for one scenario; reproducible score tables and exports; Temporal approval/hash, model-specific trial IDs and launch/poll logic tested; explicit integration test included.
- [ ] **M8 Cloud independence** — Task 13: GCE persistent Temporal server/worker and detached trial units; saved evidence of pipeline progress with laptop closed, plus worker restart yielding one launch and one terminal trial record.
- [ ] **M9 Viewing** — Task 14: runs page, episode timeline, near-live stakeholder chat and completed agent trajectory; dashboard tests pass.
- [ ] **M10 Reviewable delivery** — Tasks 15–16: tracked complete example and sanitized run with submitted patch; README, grading, architecture and operations docs; complete offline suite and `verify.sh 10` pass; commit pushed.

Execution order when sequential: 1–10, **12 before 11**, then 14, 13, 15–16. See the working model below for the parallel form. Task 12 is needed before
Temporal calls register/score/export. Run one tier first; additional repeats
and a frontier tier follow the complete end-to-end path. Insufficient repeats
produce null pass@k/pass^k at the requested k, never a silently reduced k.

Preserve the floor early: with six hours remaining, snapshot the first valid
simulation and write the initial README/grading/architecture docs even if M6
is blocked. This does not waive M6 or mark dependent milestones complete.
Keep optional sandbox review, third tasks and extensive UI work deferred.

Approval is unattended by default: after drafting, the agent runs
`swesim approve --auto <id>`; the code-owned gates decide and record
`approved_by: auto-validators, review_needed: true`. The human reviews the
frozen `scenario.json` and `approval.json` afterwards. Supervised mode
(`swesim approve <id>` by a person, or `--approval-mode human` in Temporal)
remains available but is not used for the overnight run. Never hand-edit a
draft to satisfy a gate; re-draft once or move to the next candidate.

## Execution working model (parallel streams, frozen interfaces)

Model rule (CLAUDE.md): code, design and review on `fable`; recon and simple mechanical work on `opus`/`sonnet`; never `haiku`. Phase 0 (integrator): Task 1 models + `tests/test_contracts.py` (imports every
cross-module name the plan promises). `swesim/models.py` is then frozen; streams
request schema changes from the integrator instead of editing it. `swesim/cli.py`
is wired only by the integrator.

Phase 1 (six parallel streams, each in its own git worktree and branch, owning
only the files listed, tests first, done when its tests pass and are committed):

| stream | tasks | owns | cloud |
|---|---|---|---|
| verifier | 2, 3 | `swesim/verifier/`, tests policy/parsers/grader | no |
| compile | 4, 5, 7 code | `swesim/templates/`, `stages/compile.py`, `agents/`, `harbor_cli.py`, `stages/validate.py`, `stages/approve.py`, `llm.py` | no |
| sources | 6, 10 code | `swesim/sources/`, `stages/mine.py`, `judge.py`, `draft_spec.py`, `tamper.py` | GitHub API, HF datasets only |
| stakeholder | 8 | `swesim/stakeholder/` | no (deploy in phase 3) |
| scoring+ui | 12, 14 | `stages/register.py`, `score.py`, `export.py`, `swesim/dashboard/` | no |
| orchestration | 11, 13 | `swesim/workflows/`, `scripts/vm/` | no |

Phase 2 (integrator): merge streams, run the full suite and the contract test,
fix naming drift, wire the CLI, `verify.sh 3`.

Phase 3 (cloud, wall-clock bound; parallel where independent): deploy the
stakeholder; SWE-bench path (mine, judge, draft, auto-approve, compile,
validate on Modal, interactive episode); pypdf path (discovery, probe, draft,
validate, run); VM + Temporal; docs written as artifacts appear.

Phase 4 (integrator): scores, exports, example snapshot, docs, `verify.sh 10`.
If subagents are unavailable, execute the same streams sequentially in the
order 1-10, 12, 11, 14, 13, 15-16.
