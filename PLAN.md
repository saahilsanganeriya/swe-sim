# swesim build plan: milestones and acceptance checks

This file is the checklist a `/goal` run works through. Each milestone maps to
tasks in `docs/superpowers/plans/2026-09-12-swesim-implementation.md` and is
accepted only when `bash scripts/verify.sh <n>` exits 0. Check a box only after
the verify script passes for that milestone. Commit and push after each one.

Spec: `docs/superpowers/specs/2026-09-12-swe-simulator-design.md` (v2.1).
Verified recipe for every compiled task: `references/modal-smoke-task/`.

- [ ] **M1 Models** (Task 1): `swesim/models.py` v2 with strict validators; `uv run pytest tests/test_models.py` passes.
- [ ] **M2 Verifier** (Tasks 2, 3): `swesim/verifier/{policy,parsers,grader}.py`, stdlib only; grader tests pass including protected-path drop, regression-to-zero, receipt handling, numeric-only reward.
- [ ] **M3 Compile + first Modal run** (Tasks 4, 5): templates render three conditions; `swesim compile` works on a scenario; `patch-agent` loads under Harbor; oracle and nop run on Modal against one compiled SWE-bench task.
- [ ] **M4 Validate** (Tasks 6, 7): SWE-bench pool A mining with threads; judge; draft; human approve; `swesim validate <id>` writes `validation.json` with status `valid` (oracle success 1, nop 0, all incorrect-exploit tamper controls reward 0, plausible-wrong within bound, FAIL_TO_PASS failing on base, no gold lines reachable).
- [ ] **M5 Stakeholder + first episode** (Tasks 8, 9): service deployed on Modal (`.stakeholder_url` present, `/health` ok); session tests pass; one `interactive` trial record exists with at least one ask exchange, a numeric `reward.json`, and matching transcript hashes.
- [ ] **M6 Repository-URL source** (Task 10): `swesim mine --source github-repo --repo py-pdf/pypdf` produces candidates with a reject histogram; one pypdf scenario drafted, approved, validated (`valid`).
- [ ] **M7 Temporal** (Task 11): workflow test passes; a `RunMatrixWorkflow` completed against the dev server; worker restart check documented in `docs/ops.md` with exactly one trial record for the interrupted trial id.
- [ ] **M8 Score/export/register** (Task 12): `scores.json` for both scenarios with pass@k, pass^k, outcome counts; `workspace/exports/*.episode.json` exist; `registry.json` lists both scenarios as `valid`.
- [ ] **M9 VM + dashboard** (Tasks 13, 14): systemd units and scripts present; dashboard tests pass; `docs/ops.md` records the laptop-off acceptance test result.
- [ ] **M10 Example + docs** (Tasks 15, 16): `examples/<scenario>/` with validation, three task dirs, one sanitized run; `README.md`, `docs/grading.md`, `docs/architecture.md` complete; `bash scripts/verify.sh 10` exits 0.

Order is the critical path. Do not start M6 before M4 is `valid`. Do not start
M9 before M7. The second model tier and a third scenario come after M10.
