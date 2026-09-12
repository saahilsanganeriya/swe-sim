# swesim milestones and acceptance checks

Plan: [implementation plan](docs/superpowers/plans/2026-09-12-swesim-implementation.md).
Spec is read-only during this plan review. A box is checked only after
`bash scripts/verify.sh N` succeeds. Checks are cumulative and offline; the
cloud execution required to produce evidence is an explicit implementation step.

- [ ] **M1 Models** — Task 1: dependencies installed; strict model, asset, group and hash round-trip tests pass.
- [ ] **M2 Grader** — Tasks 2–3: safe extraction, source import, exact-ID/exit cross-checks, genuine base outcomes and content hashes pass tests.
- [ ] **M3 Compiler** — Tasks 4–5: three task variants render, assets are verified, root collector is packaged, PatchAgent implements Harbor's abstract API. This milestone alone claims no cloud run.
- [ ] **M4 Authored SWE-bench task** — Tasks 6–7: mine/judge/draft; human approval bound to spec hash; executed Modal nop/oracle/wrong/tamper controls with complete base/gold test evidence; valid validation file.
- [ ] **M5 Interaction** — Tasks 8–9: durable stakeholder sessions, serialized freeze, bounded replies; a real mini-swe-agent interactive episode with a paired question/reply, trajectory and transcript hash matching collection.
- [ ] **M6 Repo URL** — Task 10: mine a GitHub repository URL without a PR override; retain filter/build rejections; automatically select a feature candidate, get human approval, validate and run it. A failed search is recorded honestly and leaves M6 incomplete.
- [ ] **M7 Pipeline and dataset** — Task 12 then Task 11: both scenarios registered with frozen evidence; actual attempts in full/hidden/interactive and hidden runs on both natural/withheld profiles for one scenario; reproducible score tables and exports; Temporal approval/hash, model-specific trial IDs and launch/poll logic tested; explicit integration test included.
- [ ] **M8 Cloud independence** — Task 13: GCE persistent Temporal server/worker and detached trial units; saved evidence of pipeline progress with laptop closed, plus worker restart yielding one launch and one terminal trial record.
- [ ] **M9 Viewing** — Task 14: runs page, episode timeline, near-live stakeholder chat and completed agent trajectory; dashboard tests pass.
- [ ] **M10 Reviewable delivery** — Tasks 15–16: tracked complete example and sanitized run with submitted patch; README, grading, architecture and operations docs; complete offline suite and `verify.sh 10` pass; commit pushed.

Execution order: 1–10, **12 before 11**, then 14, 13, 15–16. Task 12 is needed before
Temporal calls register/score/export. Run one tier first; additional repeats
and a frontier tier follow the complete end-to-end path. Insufficient repeats
produce null pass@k/pass^k at the requested k, never a silently reduced k.

Preserve the floor early: with six hours remaining, snapshot the first valid
simulation and write the initial README/grading/architecture docs even if M6
is blocked. This does not waive M6 or mark dependent milestones complete.
Keep optional sandbox review, third tasks and extensive UI work deferred.

Approval is an actual human checkpoint: the agent prints the pending draft
path and exact approval command and continues independent work. It cannot
execute approval itself or treat silence as approval. On the VM the human
approves the VM's file and sends its hash to Temporal. A timed-out approval
remains pending work, never success.
