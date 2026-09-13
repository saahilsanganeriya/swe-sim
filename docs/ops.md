# Operations evidence

This is the evidence ledger, not the setup procedure. Use [setup](setup.md), [the runbook](runbook.md), and [architecture](architecture.md) for current operation. Dates are UTC unless labelled otherwise. Historical VM demonstrations remain separate from Cloud Run acceptance.

On 2026-09-13 the user first reprioritized completion to **all code and a properly hosted website over the preserved record**, then (ledger entry labelled 11:55 PDT) un-deferred the hosted smoke: the goal is a running pipeline to iterate on task generation, reward and tuning; cost and infrastructure polish are not priorities. The measured smoke, the first graded Cloud Run trials and the fixes they forced are in [Hosted runs (2026-09-13 afternoon)](#hosted-runs-2026-09-13-afternoon); everything the finishing streams did not finish is in [Deferred from the 2026-09-13 finishing run](#deferred-from-the-2026-09-13-finishing-run). The website checks below remain required.

## Hosted takeover — 2026-09-13

Claude Code stopped at its workspace usage limit, missing the 08:00 PDT target. Codex resumed the original worktrees and preserved their interrupted changes. At takeover, the starting checkout was `30ef8af`, 53 commits ahead of the remote, and the authenticated Temporal Cloud namespace contained no workflows.

The first recovered offline run returned 785 passed, 3 skipped and 2 failures: an obsolete console model-default assertion and an order-dependent cost mock. A later integrated checkpoint recorded **849 passed, 3 skipped**, clean `ruff --select E9,F`, and valid Terraform. Evidence: `workspace/evidence/takeover-offline-tests.log`.

The operator renewed gcloud authentication and GitHub's workflow permission before deployment and pushing could resume. The ignored dotenv file was changed to mode 0600. No credential values belong in this ledger.

| Check | Observed result | Evidence |
|---|---|---|
| Cloud Build | Build `55086fb6` from commit `2bc7389` succeeded | `workspace/evidence/cloud/takeover-deploy-2bc7389.log`; Cloud Build execution |
| Terraform update | Added index-maintenance Job and supplied missing trial image, service account and SQL settings | `workspace/evidence/cloud/terraform-apply-20260913T164005Z.txt` |
| Hosted migration | Maintenance execution `swesim-index-maintenance-tl5t6` completed successfully | Cloud Run execution, reported by the operator |
| Temporal Cloud | API-key connection; authoring activity/workflow pollers 2/2, build activity 2, control activity 2, trials activity 1 | `workspace/evidence/cloud/verify_cloud-20260913T164426Z.txt` |
| Console access edge | Unauthenticated request returned HTTP 302 to Google sign-in | `workspace/evidence/cloud/iap-headers-20260913T164426Z.txt` |
| Runtime readiness | All four worker pools and console Ready; each used Terraform's pinned image | Same 16:44:26 verification file |
| Stakeholder | Redeployed in about 23.5 s after adding `python-ulid`; `/health` succeeded | `workspace/evidence/cloud/takeover-stakeholder-deploy.log` |
| Import pre-scan | 4,093 files, 6,837,576,392 bytes scanned; no known credential-value matches in the selected upload set | `workspace/evidence/cloud/takeover-import-scan.json` |
| New hosted graded outcomes | None established by these checks | Verification explicitly reported no trial Jobs |

The verified image digest was:

```text
sha256:c095bcb87841fad47686f3d1bdd43ea652b81175d6bcb8b2732e07a6f3c7e726
```

This is a dated deployment observation, not a claim that every later source commit is deployed.

### Correction to the earlier verification

`verify_cloud-20260913T091709Z.txt` used a plaintext localhost Temporal connection. It did not demonstrate the Cloud namespace or hosted workflow execution. Resource Ready conditions also missed a trials-pool crash loop: logs reported missing `SWESIM_WORKER_IMAGE` and `SWESIM_WORKER_SERVICE_ACCOUNT`, and the activity queue had zero pollers.

The updated verifier checks Cloud authentication and pollers as well as Ready/image state. The 16:44:26 file passes those stronger checks. It still **skips SQL status because the laptop has no Cloud SQL socket** and reports no trial Jobs. Registry presence is the only bucket-content check in that script; complete scenario/trial/index parity and post-cleanup execution evidence require separate verification.

### Model availability

`workspace/evidence/model-probe-takeover.json`, starting 16:24:23 UTC, records quota-limit errors for the configured Anthropic primaries. Tiny requests to the listed OpenAI and OpenRouter alternatives succeeded.

| Role | Resolved id in that probe |
|---|---|
| Judge | `openai/gpt-5.5` |
| Drafter | `openai/gpt-5.6-sol` |
| Stakeholder | `openai/gpt-5.6-sol` |
| Audit | `openai/gpt-5.6-sol` |
| Screen | `openrouter/deepseek/deepseek-v3.2` |

Judge and drafter initially collided on Sol. With the drafter chain exhausted, the resolver advanced the judge to GPT-5.5. The probe also reached `openrouter/z-ai/glm-5.3` and `openai/gpt-5-mini`. Reachability is not a graded result, and the solver is never silently substituted.

### Fresh-clone check

A new clone of pushed commit `29a3d41d8d4565d83a69308f3c38a7cf379d33af`, with a fresh copy of `.env.example` at mode 0600, ran `make check`. Result: **869 passed, 3 skipped, 1 failed in 70.76 s**. The failure was `test_reader_candidates_funnel_and_reject_histogram`: it expected `judge`, while the new reader correctly exposes `candidate:judge` plus `discovery:` counts.

A second fresh remote clone of `1fb1530b66066c17b832c9e4cffe73f1b0ffe262`, again with a new `.env` and virtual environment, ran the same command successfully: **895 passed, 3 skipped, 1 warning in 124.90 s**. The warning is the Starlette/AnyIO deprecation. The clone log was supplied to the integrator for the evidence record. Documentation local links/code fences passed, and 21 documented shell/Python blocks parsed without executing their commands.

No cloud bootstrap, deployment or trial was executed from that fresh clone. The operator is responsible for the separate idempotent `cloud-bootstrap` acceptance and authenticated browser checks.

## Hosted runs (2026-09-13 afternoon)

<a id="hosted-smoke-2026-09-13"></a>

The first workflows ever started in the Temporal Cloud namespace `swesim.abcde`, the deploys around them, the first graded Cloud Run trial and the first hosted screen and comparison (29 trials, one scenario). Times are UTC from the evidence file names and contents. The session ledger (`.superpowers/sdd/2026-09-13-hosted-pipeline/progress.md`, entries from "Claude resumes as integrator" onward) is cited as "ledger entry labelled hh:mm"; its PDT labels are not a clock (it corrects itself once, entry labelled 12:47) and are never used as timestamps here. Graded numbers are interpreted in [grading.md](grading.md#first-hosted-results-2026-09-13); this section records what ran, what broke and what was fixed.

### Deploys

| Commit | What happened | Evidence |
|---|---|---|
| `1976af3` | `make deploy` stopped at the open-executions guard before building: `swesim workflows open-types` tried `127.0.0.1:7233` (connection refused), so the guard could not list the Cloud namespace. The guard's report file is empty. | `workspace/evidence/cloud/deploy-1976af3.log`, `open-types-20260913T182145Z.txt` (0 bytes) |
| `609f923` (`609f923557ca0807e1e90cde0114057f77fce027`) | Guard: `# No open workflows`. Cloud Build `ae494b42-bf9e-4306-8f8b-7af7b168fb6b` SUCCESS (2M13S, started 18:22:38Z). Pinned `worker@sha256:f2b265054b9ea61d442957945d94ad89ee544e4683475ee41a85889bb8f425fc`. Terraform `Plan: 0 to add, 6 to change, 0 to destroy` (image `c095bcb8…` → `f2b26505…` on the four pools, the console and the maintenance Job); `Apply complete! Resources: 0 added, 6 changed, 0 destroyed`. Migration execution `swesim-index-maintenance-nzrvm` completed successfully. `verify_cloud` `result: OK`: Temporal api-key on `us-central1.gcp.api.temporal.io:7233` / `swesim.abcde` with pollers (`swesim-authoring` activity 2 / workflow 2, `swesim-build` activity 2, `swesim-control` activity 1, `swesim-trials` activity 1), IAP 302 to `accounts.google.com`, four pools and the console Ready on the pinned image, `registry.json` in the bucket. Notes: no `swesim-trial-*` Jobs yet; index status skipped (no Cloud SQL socket on the laptop). | `deploy-609f923.log`, `open-types-20260913T182230Z.txt`, `terraform-apply-20260913T182558Z.txt`, `verify_cloud-20260913T183003Z.txt`, `iap-headers-20260913T183003Z.txt` |
| `40c84d3` | Guard stopped the deploy: `CreateTasksWorkflow 1` open (the swebench creation below). | `deploy-40c84d3.log`, `open-types-20260913T183755Z.txt` |
| `1ca70a6` (`1ca70a66d142210bb25b4a6edf3744e204bcad30`) | Started with `OVERRIDE_OPEN_EXECUTIONS=1` (the guard's report carries the override line). Ledger ruling: every command-sequence change since the running image (frontier gate, lifecycle rows, requested-score-k, frozen retry) is behind `workflow.patched` markers and the open workflow is a `CreateTasksWorkflow` whose remaining commands are unchanged in shape; cost if wrong: one non-determinism failure on that workflow, which would be re-run. Cloud Build `31c3a7b9-91c9-49ac-ae43-a1fc4cb60509` SUCCESS (2M36S, started 18:42:28Z). Pinned `worker@sha256:e21881d64fbed1597cfd637f3f595c0082d193b7d2718d321cd45ae98dc8e271`; the pin guard recorded `forced: FORCE_PIN=1 acknowledged (mismatch)` (the file named the new digest while the workloads still ran `f2b26505…`, which is exactly the roll `make deploy` intends). Terraform `Plan: 0 to add, 6 to change, 0 to destroy`; `Apply complete! Resources: 0 added, 6 changed, 0 destroyed`. `write-pin` published the applied pin to the state bucket (first published pin; the log's `image.auto.tfvars.json` copy line). Migration execution `swesim-index-maintenance-c8tmb` completed successfully. `verify_cloud` `result: OK` at 18:52:10Z: Temporal api-key with pollers (`swesim-authoring` activity 1 / workflow 1, `swesim-build` 1, `swesim-control` 1, `swesim-trials` 1), IAP 302, four pools and the console Ready on the pinned image, `registry.json` in the bucket; the same two notes (no trial Jobs; index status skipped locally). This image is the first one carrying `gh` (commit `1555810`). | `deploy-1ca70a6.log`, `open-types-20260913T184221Z.txt`, `terraform-apply-20260913T184612Z.txt`, `verify_cloud-20260913T185210Z.txt`, `iap-headers-20260913T185210Z.txt` |
| `400c288` (`400c28899d0ed47790a271b93f59eec32686e12a`) | Receipts wiring (`1ddfd74`), docs, test fixes. Guard: `CandidateWorkflow 1`, `CreateTasksWorkflow 3` open, `OVERRIDE_OPEN_EXECUTIONS=1` recorded. Cloud Build `d854aaf2-6217-4a07-aba1-fa4313c6eac3` SUCCESS (2M43S, created 19:04:26Z). Pinned `worker@sha256:8559aabd144b6bf3756ca81d89dc862bd4fe24c7f4a374486214964188e45171` (pin guard `forced`, workloads still on `e21881d6…`). `Plan: 0 to add, 6 to change, 0 to destroy`; `Apply complete! Resources: 0 added, 6 changed, 0 destroyed`; the `swesim-trials` pool's `Modifications complete after 52s` (apply file written 19:09:16Z). Migration execution `swesim-index-maintenance-48sfp` completed. `verify_cloud` `result: OK` at 19:12:15Z: pollers `swesim-authoring` 1/1, `swesim-build` 2, `swesim-control` 2, `swesim-trials` 1; `trial jobs: 1 unique names`; `job swesim-trial-50d797789546d178d8add487: 1 execution` (the first graded trial, below). This roll also moved the pools onto version 2 of the Anthropic key in Secret Manager (pushed by `push_secrets` after the user supplied a new key, ledger entry labelled 12:05): workflows started after it resolve the configured Anthropic role heads again; workflows started earlier keep their OpenAI resolution. | `deploy-400c288.log`, `open-types-20260913T190418Z.txt`, `terraform-apply-20260913T190818Z.txt`, `verify_cloud-20260913T191215Z.txt`, `iap-headers-20260913T191215Z.txt` |
| `3c177a5` (apply only) | Not a deploy: `make apply` after the Artifact Registry fix below. Pin guard `match` on `8559aabd…`; `Apply complete! Resources: 1 added, 0 changed, 0 destroyed` — the `worker_reader` IAM binding, adopted into Terraform after it had been granted with `gcloud` (apply file written 19:13:26Z). No image change. | `apply-3c177a5.log`, `terraform-apply-20260913T191326Z.txt` |
| `350414e` (`350414e952216a944df3d49fd3a03aabb3dd4304`) | Console path fix for imported VM-era trials (`1b877b7`). Guard: `RunTasksWorkflow 1`, `TrialWorkflow 1` open (the graded trial), override recorded. Cloud Build `43cdb473-bf33-48cf-b1a7-feb10e41031f` SUCCESS (2M12S, 19:29:26Z). Pinned `worker@sha256:d44e1bc241f57d7df8688ff71cfd6ad97ddd2c5160d6b0e765c6f9718a03a830`. Plan 0/6/0; `Apply complete! Resources: 0 added, 6 changed, 0 destroyed`; `swesim-trials` `Modifications complete after 1m12s` (apply started 19:32:34Z, file written 19:34:02Z) — **this roll restarted the trials pool while trial `50d79778…` was executing** (see [First graded trial](#first-graded-trial)). Migration execution `swesim-index-maintenance-fxcct` completed. `verify_cloud` `result: OK` at 19:35:52Z: pollers 1 on every queue; `trial jobs: 1 unique names`, `1 execution`. The ledger notes the chain's exit 2 was the operator's log-name grep after HEAD moved, not the deploy (entry labelled 12:40). | `deploy-350414e.log`, `open-types-20260913T192918Z.txt`, `terraform-apply-20260913T193234Z.txt`, `verify_cloud-20260913T193552Z.txt`, `iap-headers-20260913T193552Z.txt` |
| `fe61913` (`fe619139eee709c2cb91701d40c94baa4b1d013d`) | `/explainer` (`9532c17`, `8aaca25`), novel-vs-SWE-bench facet and badges (`bf992c7`), viewer-local timestamps (`3f72946`), fixes (`1c0972b` numbered option picks, `d5bb9b3` resolved models on workflow rows, `2e732b8` 5 s heartbeat throttle), index tolerance for in-progress candidates (`e203b13`). Guard: `CreateTasksWorkflow 6` open (the repository sweep), override recorded. Cloud Build `a6b892ec-90d0-49bf-aed3-0f029a91ee25` SUCCESS (2M9S, 19:42:25Z). Pinned `worker@sha256:133bc51e1e3ae6dfb29ef8c3d6455e8317955056fc410fee0ea8fbcd218543f5`. Plan 0/6/0; apply 0 added, 6 changed, 0 destroyed. Migration execution `swesim-index-maintenance-gj8dg` completed. `verify_cloud` `result: OK` at 19:50:43Z: pollers `swesim-authoring` 2/2, `swesim-build` 2, `swesim-control` 2, `swesim-trials` 1; `trial jobs: 23 unique names`, every Job `1 execution`. | `deploy-fe61913.log`, `open-types-20260913T194218Z.txt`, `terraform-apply-20260913T194545Z.txt`, `verify_cloud-20260913T195043Z.txt`, `iap-headers-20260913T195043Z.txt` |
| `d36b898` (`d36b8985247bf574d528791e32e8781b28d76619`) | Funnel reconciliation (`ba89b9c`, `f6af907`, `9fcef5b`; new Alembic revision `0005_funnel_versions`), the console's warn token for superseded segments (`d36b898`), bounded fresh probe/control attempts (`b44052f`, merged as `b5e8a8a`). Guard: `RunTasksWorkflow 1`, `TrialWorkflow 6` open (the comparison run), override recorded. Cloud Build `d9e4e730-b08c-49b7-bfb0-0b30e7a0effe` SUCCESS (2M16S, 20:07:42Z). Pinned `worker@sha256:68769582b3029e6e19e470ec86bac4a5240e22b6269229a9a278fb5ae70d1ab1`. Plan 0/6/0; apply 0 added, 6 changed, 0 destroyed. Migration execution `swesim-index-maintenance-qhrn6` completed; the migrated head is `0005_funnel_versions` (`swesim/index/migrations/versions/0005_funnel_candidate_versions.py`, `down_revision = '0004_history'`; ledger entry labelled 13:33). `verify_cloud` `result: OK` at 20:15:43Z: pollers `swesim-authoring` 3/3, `swesim-build` 2, `swesim-control` 1, `swesim-trials` 2; `trial jobs: 18 unique names`, every Job `1 execution`. | `deploy-d36b898.log`, `open-types-20260913T200734Z.txt`, `terraform-apply-20260913T201113Z.txt`, `verify_cloud-20260913T201543Z.txt`, `iap-headers-20260913T201543Z.txt` |
| `cf471f6` (`cf471f618aad66090225f78089b3e3da4e4c0f27`) | GitHub rate-limit handling (`767b75a`, `04d72b3`; below). Guard: `# No open workflows`. Cloud Build `d8467c3e-02cc-4766-8d40-ffcee864380c` SUCCESS (1M56S, 20:19:34Z). Pinned `worker@sha256:2c539bf4b58b3a7aa0fbc017dfbf08c6dbc40da1ebc5ab0c3143407d473fb79e`. Plan 0/6/0; apply 0 added, 6 changed, 0 destroyed. Migration execution `swesim-index-maintenance-hmmls` completed. `verify_cloud` `result: OK` at 20:26:24Z: pollers `swesim-authoring` 2/2, `swesim-build` 1, `swesim-control` 1, `swesim-trials` 2; `trial jobs: 16 unique names`, every Job `1 execution`. | `deploy-cf471f6.log`, `open-types-20260913T201928Z.txt`, `terraform-apply-20260913T202241Z.txt`, `verify_cloud-20260913T202624Z.txt`, `iap-headers-20260913T202624Z.txt` |

The deploy of `cf471f6` (image `2c539bf4…`, verified 20:26:24Z) is the newest verified deploy in this ledger. Seven deploys of the afternoon verified `result: OK`; five of them (`1ca70a6`, `400c288`, `350414e`, `fe61913`, `d36b898`) rolled with `OVERRIDE_OPEN_EXECUTIONS=1` under the ruling above, each override written into its `open-types-<UTC>.txt` by the Makefile; the ledger records no workflow failure attributed to an override. Every verification that found `swesim-trial-*` Jobs found exactly one execution per Job (1, 1, 23, 18 and 16 Jobs at 19:12:15Z, 19:35:52Z, 19:50:43Z, 20:15:43Z and 20:26:24Z); the falling count is completed trials whose Jobs were cleaned up after their records were ingested ([runbook](runbook.md#diagnose-a-missing-or-stuck-trial): absence after recorded cleanup is expected). The image digest of a later deploy is not known until its log carries `pinned …@sha256:…` and `verify_cloud … result: OK`.

### Creation workflows

| Workflow id | Request | Observed | Evidence |
|---|---|---|---|
| `create-fa2489a292574fd19ba88796c0c18087` | `swebench:A:pytest-dev__pytest-7283`, auto approval | Started 18:31Z. Open at 18:37:55Z and 18:42:21Z (the guard listings). Ledger entry labelled 13:25: blocked on the missing `gh` binary too (`swebench_annotated.candidates` → `threads.fetch_pr_thread`); `mine_activity` at attempt 3 under the LLM retry class. Plan recorded there: once an image built from main ≥ `238fb33` is deployed, cancel it and resubmit. | `workspace/evidence/smoke/create-swebench-pytest-7283.log` (the id), `open-types-20260913T183755Z.txt`, `open-types-20260913T184221Z.txt` |
| `create-3b1513db47894549a189f678343774b9` | `https://github.com/encode/httpx`, band `bugfix`, max 3, auto approval | Started 18:31Z. Errored in discovery: `FileNotFoundError: 'gh'` — the worker image had no GitHub CLI. Fixed by commit `1555810` (`Dockerfile.worker` installs gh 2.100.0, checksum-verified; validated in a throwaway amd64 container per the ledger) and `6906312` (a discovery attempt fails when `gh` is unavailable instead of counting every PR as rejected). Both first attempts ended in error with their attempts exhausted (ledger entry labelled 11:52) and are kept as failed rows in the workflows history. | `workspace/evidence/smoke/create-httpx.log` (the id), `Dockerfile.worker`, commit `1555810` |
| `create-b788cb22d7064a4d8bcbe63bb943c709` | `swebench:A:pytest-dev__pytest-7283`, auto approval — resubmission on image `e21881d6…` | Submitted 18:52:42Z (file write). Reached validation with 7 controls on Modal and registered **`pytest-dev--pytest-7283`, the first hosted valid task** (ledger entry labelled 12:06): judge `openai/gpt-5.5`, drafter `openai/gpt-5.6-sol` (the pre-roll role resolution, [below](#model-role-fallbacks-in-force)); contract 1 FAIL_TO_PASS / 51 PASS_TO_PASS (the first trial's report, below). | `create-swebench-pytest-7283.log`, the scenario's `scenario.json`/`validation.json` in the bucket, console Scenarios page |
| `create-49584e8df239448c83673333030e907b` | `encode/httpx`, `bugfix`, max 3 — resubmission | Submitted 18:52:43Z; completed 18:53:06Z (its second `index_workflow_activity` row, ledger entry labelled 12:22) with **0 discovered**: encode/httpx has no PR merged since 2026-06-01 (checked with `gh` on the laptop: the recent closed PRs are all unmerged). `gh` works in the image. Scan record written: `candidates/github-repo--encode__httpx--bugfix/scans/ce2b9fab….json`. Its SQL row still read `running` afterwards — the stale-row investigation below. | `create-httpx.log`, the scan record in the bucket |
| `create-c7831cace7b44658aefe2fa3f8c58a17` | `pydantic/pydantic`, `bugfix`, max 3 | Submitted 18:55:19Z. `discover_activity` ran a second attempt after a heartbeat timeout: the `400c288` apply rolled the authoring pool mid-discovery (19:08–19:09Z) and the retry restarted the scan from scratch (idempotent by design; large repositories take long with per-PR `gh` calls). Yield: 11 discovered → 3 candidates → 2 judge-rejected, 1 not reproducible offline (ledger entry labelled 12:30). No task. | `create-pydantic.log` |
| `create-c4470d5ed8a14b1bab65e2525b0cb0cd` | `pytest-dev/pytest`, `bugfix`, max 3 | Submitted 18:55:21Z. Yield: 13 discovered → 3 candidates → 2 judge-rejected, 1 invalid: `pytest-dev--pytest-14447`, the first fully novel candidate to reach validation, failed its alternative-correct control `eager-operand-snapshots` (`success != 1` on the hidden tests), so the tests accept only one implementation and the scenario was correctly rejected as structural (ledger entries labelled 12:15 and 12:22). No task. | `create-pytest-repo.log` |

Repository sweep (brief §4 step 1; ids in `workspace/evidence/smoke/sweep-creations.log`, last written 19:56:44Z): nine creations started in parallel, `bugfix` band, max 20 candidates each — `psf/black` `create-32f7c270…`, `marshmallow-code/marshmallow` `create-a36d4edb…`, `Textualize/rich` `create-9ce80ee3…`, `python-poetry/poetry` `create-976a81b0…`, `sqlfluff/sqlfluff` `create-b19e96c8…`, `pydantic/pydantic` `create-acfbd5a1…`, `pytest-dev/pytest` `create-ea0dfec7…`, `py-pdf/pypdf` `create-b06694b3…`, `fastapi/fastapi` `create-77e6c1bb…`. Outcomes (ledger entries labelled 12:40–12:52): marshmallow and rich 0 discovered; poetry 4 candidates → 3 judge-rejected, 1 probe error (`python-poetry--poetry-10941`, the stale-directory bug below); **six errored** (black, sqlfluff, pydantic, pytest, pypdf, fastapi) with `DiscoveryUnavailable: gh api unavailable (rate-limit)`. Root cause (ledger entry labelled 13:05): GitHub's **secondary** (burst) rate limit — worker logs read "secondary rate limit … review our Terms of Service" while the deployed token's core quota read 5000/5000; nine parallel scans on one token, about five `gh` calls per PR, and the `retries.LLM` class (4 attempts, at most 2 min backoff) re-triggered it. A sequential rerun was started before the root cause was known (`sequential-sweep.log`: `pytest-dev/pytest create-fa2a88aa3232445386bf533a8512bf6a` start → `stage=complete`, then `pydantic/pydantic create-d6988a1afa194c7f9edfb48c71330992` started; the script's own `12:45`/`12:56` labels are its local clock; file written 19:56:44Z) and stopped once it was; its quota gate read 5000 before and after the pytest scan. Fix (`767b75a`, `04d72b3`, merged as `cf471f6`, verified 20:26:24Z): `RateLimited` carries the reset time; the secondary case honours `Retry-After` with 120 s → 15 min waits inside the attempt under heartbeats and resumes the same page/PR; new `retries.DISCOVERY` class (8 attempts, at most 15 min) with a 2 h activity timeout and 3 min heartbeat; a process-wide `gh` lock with 0.25 s pacing and one scan per worker instance; detail-first cheaper call order; `tests/conftest.py` fails any offline test that reaches the real `gh`. A read-only GitHub App (`swesim-discovery`) was created for a second 5,000/h bucket; installation-token minting was verified and the rotation code is in flight (`finish/gh-app`); no App id or key is recorded here.

Since-2023 both-band sweep (user directive, ledger entry labelled 13:08: target at least 20 novel valid tasks): `/tmp/swesim-sweep-2023.sh` → `workspace/evidence/smoke/sweep-2023.log` — 10 repositories × {`bugfix`, `feature`}, `--since 2023-01-01`, `--max-candidates 40`, one scan at a time, waiting while the token's remaining quota is below 2000. First line: `13:26 start pytest-dev/pytest bugfix create-06b18aec377b4697986b09e6d8dcc84d remaining_before=5000` (script-local label; file written 20:27:07Z). Running at the time of writing; its yield is not recorded here.

Lesson recorded for the image: the worker image must carry every binary the stages shell out to (`git`, `gh`); a `gh --version` check inside the image belongs in `verify_cloud` (deferred, infra list below).

### First graded trial

| Id | What happened | Evidence |
|---|---|---|
| `run-175c0580f6e44e8ea0308682c9bd2825` | `RunTasksWorkflow`: `pytest-dev--pytest-7283`, hidden, `openrouter/deepseek/deepseek-v3.2`, k=1, purpose demonstration. Its trial `d5282a7bacc1bf7fd19cd782` **launch-failed**: Cloud Run `CreateJob` returned 403 `artifactregistry.repositories.downloadArtifacts` for the caller (the worker service account). Cloud Run validates image access with the caller's identity when a Job is created; the pools pull through the service agent and never hit it. The failed attempt stays in the record as launch-failed. | `workspace/evidence/smoke/run-pytest-7283-hidden.log` (the id); ledger entry labelled 12:10 |
| `run-ef32bb35557448578cd757b236a44805` | The same request with `--attempt-offset 1` after `roles/artifactregistry.reader` was granted on the `swesim` repository (`gcloud`, then Terraform `3c177a5`). Trial **`50d797789546d178d8add487`**: Job `swesim-trial-50d797789546d178d8add487`, execution `swesim-trial-50d797789546d178d8add487-whx7p` created 19:10:06Z, `Started deployed execution in 1m48.47s` (19:11:56Z), `Execution completed successfully in 30m37.2s` (19:40:59Z), `succeededCount: 1`, `taskCount: 1`, `maxRetries: 0`, image `worker@sha256:8559aabd…`, labels `swesim-run-workflow-id`, `swesim-scenario-id`, `swesim-trial-id`. Outcome **valid-incorrect, reward 0.0** (FAIL_TO_PASS 0/1, PASS_TO_PASS 51/51, groups 0/1, integrity signals 0), cost $0.43879 (3,141,695 input / 31,655 output tokens; OpenRouter served by StreamLake and SiliconFlow), agent run 19:11:59Z→19:40:53Z (ledger entry labelled 12:43). | `workspace/evidence/cloudrun/executions-50d797789546d178d8add487.json` (one execution in the list), `verify_cloud-20260913T191215Z.txt`, `-193552Z.txt`, `-195043Z.txt` (`1 execution` each time) |

**Restart proof.** The `350414e` apply rolled the `swesim-trials` pool between 19:32:34Z and 19:34:02Z (`terraform-apply-20260913T193234Z.txt`, `pool["trials"]: Modifications complete after 1m12s`) while execution `…-whx7p` was running (19:11:56Z→19:40:59Z). The executions list captured at 19:42:02Z holds exactly one execution, and every later `verify_cloud` reports `job swesim-trial-50d797789546d178d8add487: 1 execution`: the restarted pool resumed the existing runtime and did not launch again — the condition the VM-era [worker restart](#worker-restart) demonstrated, now measured on Cloud Run. The `400c288` roll of the same pool completed at 19:09:16Z, before the Job was created (19:10:06Z), so it is not part of the proof. Gap: this trial's `record.json` has `worker_image_digest`/`origin` = None; `launch.json` carries them (deferred P2-11, below).

### Screen and comparison on `pytest-dev--pytest-7283`

Runs (`workspace/evidence/smoke/runs.log`, written 19:46:52Z): `screen` `run-b46b6810b30d4d3f807c06d0d69a1113` (v3.2 hidden k=2 / full k=1), `comparison` `run-968e63cbc5124dc38fc507e0fc76b297` (deepseek-v3.2, gpt-5-mini, glm-5.3 × full/hidden/interactive × k=2 = 18) and `comparison-v4.1-flash` `run-d33c714f68cf4d81ab194698a02f475e` (`openrouter/deepseek/deepseek-v4.1-flash` × 3 × k=2, added at the user's request) were **cancelled**: `max_parallel_trials` defaults to 1, so each run launched its trials serially (ledger entry labelled 12:50). Resubmitted with fresh attempt offsets: `screen2` `run-5c7c7c788a394238ae7ae69ab5a24cc7` (`--max-parallel-trials 6`, offset 10) and `comparison2` `run-2528e1bd073e4953a170f797a848822e` (4 models × 3 conditions × k=2 = 24 trials, `--max-parallel-trials 16`, offset 20). Ruling: the CLI/console default should be raised (deferred, below). The interactive cells' stakeholder resolved to `anthropic/claude-opus-5` (key version 2; ledger entry labelled 12:44).

Screen label (`difficulty_label` in the evidence JSON): tier **`broken`**, screen model `openrouter/deepseek/deepseek-v3.2`, hidden 1/2, full 0/1, labelled 20:02:53Z (ledger entry labelled 13:15) → the scenario is off the leaderboard by rule; results stay visible per scenario.

Comparison summary, `workspace/evidence/sweep/pytest-7283-comparison-2026-09-13.json` (generated 20:28:21Z; cells aggregate the first trial, `screen2` and `comparison2` by model × condition; `cost_usd` is the cell total):

| Model (JSON name) | full solved/n, cost, flags | hidden solved/n, cost, flags | interactive solved/n, cost, flags |
|---|---|---|---|
| `deepseek-v3.2` | 0/3, $0.8435, 0 | 3/5, $1.1759, 4 | 2/2, $0.4651, 0 |
| `deepseek-v4.1-flash` | 3/3, $0.0120, 1 | 2/2, $0.0032, 0 | 2/2, $0.0045, 2 |
| `glm-5.3` | 2/2, $0.0897, 2 | 2/2, $0.1078, 2 | 2/2, $0.0962, 2 |
| `gpt-5-mini` | 0/2, $0.0692, 0 | 1/2, $0.0785, 0 | 2/2, $0.0667, 0 |

`total_trials: 29`, `total_cost_usd: 3.0124`. The JSON's two notes: fact `f05` over-generalises beyond the FAIL_TO_PASS contract (full < hidden < interactive for deepseek-v3.2 and gpt-5-mini); integrity flags are protected-path edits of `testing/test_unittest.py`, dropped by the verifier. `deepseek-v4.1-flash` full has n=3 against a k=2 request; the JSON does not attribute trials to runs, so the third attempt's run is not identified here. What the numbers mean, and what they do not, is in [grading.md](grading.md#first-hosted-results-2026-09-13).

### Bugs found by the smoke and their fixes

| Found | Observed | Fix and where it is deployed |
|---|---|---|
| Missing `gh` in the worker image | `create-3b1513…` `FileNotFoundError: 'gh'` in discovery; `create-fa2489…` blocked in `swebench_annotated.candidates` → `threads.fetch_pr_thread` (`mine_activity` attempt 3) | `1555810` (pinned gh 2.100.0, checksum-verified), `6906312`; image `e21881d6…` (`1ca70a6`, verified 18:52:10Z). The CLAUDE.md rule that the image carries every shelled-out binary dates from this. |
| Artifact Registry read for the worker at `CreateJob` | Trial `d5282a7b…` 403 `artifactregistry.repositories.downloadArtifacts` (above) | `roles/artifactregistry.reader` on the `swesim` repository for the worker service account: `gcloud` at once, Terraform `3c177a5` (`1 added`, 19:13:26Z). Verified by trial `50d79778…` launching. |
| Stale `running` workflow row | The console showed `create-49584e8d…` as `running` after it completed at 18:53:06Z. Temporal history: both `index_workflow_activity` calls (running, completed) returned `indexed: true`; the bucket snapshots carry both rows with the same `run_id`; SQLite tests prove store precedence — so the SQL row should have read `completed`. Remaining hypotheses (ledger entry labelled 12:22): a Postgres-only commit/precedence difference in `upsert_workflow`'s locked transaction, or console caching. Root cause **not established**; the decisive check (a later workflow shows `completed` without a rebuild) was put to the user and is not recorded. | Index rebuild through the maintenance Job, rebuild id `01M2E2K7STNRJGDN145YZG1TZP`: scenarios 12, candidates 53, trials 58, exchanges 66, scores 40, workflows 5 (SQL had 7: two rows without bucket snapshots), costs 4, discoveries 2 (ledger entry labelled 12:06). Hardening merged as `c0561eb` (`9a5b62b` SQL-path precedence proved in both report orders, `e203b13` in-progress candidates are "nothing to index yet", `5e18d96` deterministic cancellation test) and `d5bb9b3` (a second `running` row with the resolved models); deployed in `350414e`/`fe61913`. `reconcile_workflows_activity` stays deferred (below). |
| Funnel accounting | Home funnel read `judged 32 kept → probed 14 of 14; validated 7 → registered 5 of 5` while the registry held 4 scenarios (a candidate present in two files was double-counted); 18 judge-kept candidates that were never probed (legacy per-instance probe files not joined; cap cuts and stopped workflows unrecorded) vanished instead of reading `pending`; superseded v1 versions vanished (ledger entry labelled 12:32) | `ba89b9c`, `f6af907`, `9fcef5b`: one row per (source, instance, version); five terminal states including `pending` and `superseded`; invariant `entered[n+1] == kept[n]`; `registered` read from `registry.json`; Alembic `0005_funnel_versions`; deployed in `d36b898` (verified 20:15:43Z), after which the Home funnel reconciles stage to stage (ledger entry labelled 13:33). |
| Probe stale directory | `python-poetry--poetry-10941` errored at probing: `HarborRunError: job /workspace/jobs/probe/<id> exists without a unique terminal result; reconcile` — a probe directory left by an attempt the 19:08Z pool roll interrupted; the Harbor wrapper refused to rerun into it (review finding P2-17) | `b44052f` (merged `b5e8a8a`): bounded fresh `--attempt-<n>` directories with preserved lineage for abandoned probe and control jobs; graded trials untouched; deployed in `d36b898`. The candidate can be re-run (`ALLOW_DUPLICATE_FAILED_ONLY`). The debug path `stages/validate.py` has no `max_fresh_attempts` (deferred). |
| GitHub secondary rate limit | Six of nine parallel discoveries errored (above) | `767b75a`, `04d72b3` (`cf471f6`, verified 20:26:24Z); the since-2023 sweep runs one scan at a time behind a quota gate. |
| Serial trial launches | `max_parallel_trials` defaults to 1; three runs cancelled and resubmitted with `--max-parallel-trials` (above) | Operator flag today; default change deferred (below). |
| `index_scenario` "no version 1" noise | Projection warnings for candidates without a registered version | Fixed in the `5edf19b` merge (`e203b13`), deployed in `fe61913`. |
| Full brief broader than the contract | `pytest-dev--pytest-7283` fact `f05` asserts behaviour for pytest's skip marker that only a PASS_TO_PASS test covers; the saved `audit.json` (`openai/gpt-5.6-sol`, 19:06Z) labelled the task `coherent`/`fair_tests`/`plausible_withholding` = true because `fair_tests` checks tests ⇐ facts, not facts ⇒ contract (ledger entry labelled 13:25) | `facts_within_contract` audit label and a ninth approval gate `fact-scope` on `finish/fact-scope` (`86bc2e6`; `.superpowers/sdd/2026-09-13-hosted-pipeline/reports/fact-scope-2.md`); not deployed at the time of writing. |

### Model-role fallbacks in force

The overnight probe (`workspace/evidence/model-probe.json`, probed 2026-09-13 08:37Z) reached all 25 ids with no substitutions. After the Anthropic workspace API usage limit ("regain access 2026-10-01" in the operator's account) the takeover probe (`workspace/evidence/model-probe-takeover.json`, 16:24Z) recorded `anthropic/claude-opus-5` and `anthropic/claude-sonnet-5` as unreachable (HTTP 400 `invalid_request_error`; the provider text is redacted in the file) and resolved:

| Role | Configured (`swesim/roles.py` `ROLE_CHAINS` head) | Resolved | Reasoning line in the file |
|---|---|---|---|
| judge | `anthropic/claude-opus-5` | `openai/gpt-5.5` | fell back to `openai/gpt-5.6-sol`, then "judge and draft both resolved to openai/gpt-5.6-sol; draft chain exhausted, advanced the judge chain to openai/gpt-5.5" |
| draft | `anthropic/claude-sonnet-5` | `openai/gpt-5.6-sol` | "anthropic/claude-sonnet-5 unavailable; fell back to openai/gpt-5.6-sol" |
| stakeholder | `anthropic/claude-opus-5` | `openai/gpt-5.6-sol` | "anthropic/claude-opus-5 unavailable; fell back to openai/gpt-5.6-sol" |
| audit | `anthropic/claude-opus-5` | `openai/gpt-5.6-sol` | same |
| screen | `openrouter/deepseek/deepseek-v3.2` | unchanged | — |

Hosted workflows re-probe in `probe_models_activity` and write their own resolution to `evidence/model-probe.json` in the bucket; the laptop files above are the operator's probes, not the workers'. The judge remains `anthropic/claude-opus-5` by the user's directive (see [grading](grading.md#judge-calibration)); the creations that ran before the `400c288` roll — including the one that registered `pytest-dev--pytest-7283` — used `openai/gpt-5.5` because of the limit. The solver is never substituted.

Afternoon change: the user supplied a new Anthropic key in the repository's `./.env`; it was pushed to Secret Manager as version 2 and an Opus 5 probe with it succeeded (ledger entry labelled 12:05). The pools picked it up at the `400c288` roll (verified 19:12:15Z); workflows started after that resolve the configured heads again (ledger entry labelled 12:30: judge/stakeholder Opus 5, drafter Sonnet 5), and the comparison run's stakeholder was `anthropic/claude-opus-5` (ledger entry labelled 12:44). Workflows started earlier kept their OpenAI resolution to completion. The laptop's default env file still carries the old key: laptop commands that need Anthropic must set `SWESIM_ENV_FILE=./.env`. Each candidate's `difficulty.judged.model_id` and each trial's `RunConfig.stakeholder.model_id` record which id actually ran; do not pool across them.

## Preserved record inventory

A read-only inventory on 2026-09-13 found **50 trial records**, including **19 whose spec hash matches the current registered version-2 spec**, and 50 episode exports. The current subset contains 13 correct and 6 incorrect outcomes. There are 3 registered version-2 scenarios; their contracts contain respectively 1/84, 6/3 and 2/95 FAIL_TO_PASS/PASS_TO_PASS tests for seaborn, sklearn and pypdf.

These are preserved laptop/VM results. Importing them does not turn them into hosted runs. At the 16:44:26 verification, the bucket had a registry seed; upload of the full preserved record and SQL parity had not been established. The import log is `workspace/evidence/cloud/takeover-import-record.log`; completion and count comparison remain pending until recorded below.

The upload pre-scan covered known credential values and excluded trial task copies, job config/task files, locks and symlinks. Its no-match result is limited to that scan and selected upload set.

New projects now seed an empty registry rather than copying a catalog whose artifacts are absent. Bootstrap also deploys the stakeholder and reads its URL output when the dotenv file has none (`29a3d41`). These code changes remove two setup gaps; the fresh-project execution of the entire sequence remains to be measured.

## Outstanding hosted acceptance

| Required evidence | Status in this ledger | Exact next check |
|---|---|---|
| Complete record import | Pending | Import command finishes; compare bucket registry/scenario/trial objects and SQL row counts; inspect skipped records |
| Current release image and schema | `cf471f6`: build `d8467c3e…`, digest `2c539bf4…`, execution `swesim-index-maintenance-hmmls`, verify OK 20:26:24Z; migrated head `0005_funnel_versions` ([Hosted runs](#hosted-runs-2026-09-13-afternoon)) | Record build id, full digest, maintenance execution and migrated head revision after each deploy |
| Hosted task creation | Measured: `create-b788cb22…` registered `pytest-dev--pytest-7283` (SWE-bench pool A) through 7 controls; six repository creations reached honest terminal results with 0 novel tasks (httpx 0 discovered; pydantic and pytest 3 candidates each, all rejected; poetry 4, all rejected or errored); the since-2023 both-band sweep is running ([Hosted runs](#hosted-runs-2026-09-13-afternoon)) | A **novel** (repository-sourced) task passes controls and registers |
| Hosted graded run | Measured: `run-ef32bb35…` → trial `50d79778…` (valid-incorrect, reward 0.0, one Job, one execution); 29 hosted trials in total on one scenario ([First graded trial](#first-graded-trial), [grading.md](grading.md#first-hosted-results-2026-09-13)) | Transcript binding and audit labels on the hosted interactive trials; `record.json` provenance stamped (P2-11) |
| Launch-once and cleanup | Measured: every `swesim-trial-*` Job listed by `verify_cloud` at 19:12:15Z, 19:35:52Z, 19:50:43Z, 20:15:43Z and 20:26:24Z had exactly one execution (1, 1, 23, 18, 16 Jobs); the count fell as completed trials were cleaned up | Persist the cleanup receipt per trial alongside the record and cite one here |
| Worker-pool restart | Measured on Cloud Run: the `350414e` apply rolled `swesim-trials` (19:32:34Z–19:34:02Z) during execution `…-whx7p` of trial `50d79778…`; one execution before and after ([First graded trial](#first-graded-trial)) | Repeat deliberately with `make` capacity changes per the runbook and record the pool revision ids |
| IAP identity enforcement | Redirect measured; browser roles pending | Allowed account enters, denied account fails, ordinary allowed account cannot see evaluator material |
| Fresh-clone developer check | Passed at `1fb1530` | 895 passed, 3 skipped; repeat after a materially changed release |
| Fresh-clone bootstrap | Pending | Operator runs `cloud-bootstrap` against the configured project; repeat plan/apply is clean; console loads |
| Screen and model sweep | Screen ran on `pytest-dev--pytest-7283` (`broken`, model-specific — see grading.md); four-model k=2 comparison ran (24 trials); frontier gate and Tier A–C not exercised | An informative screen label on a novel scenario; Tier A–C runs behind the frontier gate; spend ledger read from the console |
| Disclosure/integrity audit | Historical audit exists; new experiments deferred | Audit future scenarios and delivered replies, index labels, explain every signal |
| Full acceptance scripts | Pending for final record | `scripts/verify.sh 10` and hosted verification against the final integrated image and record |

Do not replace a pending item with a planned command or a unit-test result. Add workflow/trial ids, timestamps and artifact paths when it is observed.

## Deferred from the 2026-09-13 finishing run

Compiled from the five finishing reports (`.superpowers/sdd/2026-09-13-hosted-pipeline/reports/{frontier-2,history-2,website-2,costs-2,infra-2}.md`) and the ledger. Each line: what — why it was deferred — where it goes. "Direction" means the user's 11:55 PDT essentials-only ruling relayed by the integrator.

Frontier and launcher (`frontier-2.md`):

- Planned/launched/running trial rows (review P1-4) — cross-stream contract with storage; not required to iterate — `prepare_trial_plan_activity` upserts `planned`; `TrialWorkflow` hooks `index_trial_activity` with `status` after the launch acknowledgement; `await_trial_activity` refreshes `running`; storage's `index_trial` accepts `status` + `request`/`request_path` (payload shape in the report's concern 4).
- `reconcile_workflows_activity` + `swesim workflows reconcile` (P1-6, second half) — needs a Temporal connection to test beyond fakes; lifecycle rows already remove the common stale-`running` cases — `client.py:reconcile_workflows()` describing every `running` row and upserting the Temporal-observed status with `lifecycle_source="temporal"`; CONTROL activity; CLI command.
- Request bounds, per-run spend cap, fail-closed spend check (P2-16) — changes env-test semantics for uncapped dev runs; the frontier cap-in-force part shipped — `swesim/workflows/bounds.py` checked in `client.start_run/start_create` and at the top of both operator workflows; `spend_check_activity` reports min(run, global); `_spend_exceeded` returns `stopped_reason="spend-unknown"` on an unreadable ledger.
- `validation.run_control` transient classification (P2-17) — validation is not on the iteration path — Harbor infrastructure exceptions → `retries.transient(kind="HarborInfrastructure")` with a fresh `<job>--attempt-<n>` directory (max 3); `assemble_validation` treats an infrastructure-class directory as "nothing measured".
- Console surfaces `admission.reasons`, `include_saturated` and a per-run `spend_cap_usd` input (concern 3) — UI completeness — `dashboard/console.py:validate_run` and the Run form; progress query `stopped_reason == "frontier-gate"`.
- Timing flake `tests/test_workflow_batching.py::test_threaded_launch_finishes_before_cancellation_stops_runtime` (concern 1) — pre-existing, not in the narrowed scope — give the fake launch activity a short heartbeat timeout or assert on the cancel receipt.

History, registry and provenance (`history-2.md`):

- Storage half of the live trial rows (item 4, P1-4) — only useful together with the launcher call sites above — `swesim/index/activities.py:index_trial` builds a row from `request`/`request_path` when no terminal files exist, never regressing a terminal row; `WorkflowStatus.unknown` for reconciled, not-retained executions.
- Provenance fields `worker_image_digest, origin, workflow_id, run_id` on `TrialRecord`, `approval.json`, `compile.json`, `validation.json` (item 5, P2-11) — three of the four writers are other streams' files — `launchers/base.py:launch_once`, `stages/compile.py`, `workflows/validation.py` / `stages/validate.py`, `stages/run.py:TrialRecord`; projections already prefer file provenance when present.
- `reconcile_workflow_rows(store, temporal_listing)` writer (item 6, P1-6) — only the reconciler function and its caller remain (schema column, `choose_workflow` precedence and the activity payload field are in place) — `swesim/index/history.py` + the CONTROL activity above.
- `CandidateWorkflow` does not yet call `index_candidate_event_activity` (concern C2, exact call sites and payload in the report) — dispatched separately as the `finish/receipts` stream (ledger entry labelled 13:20); not merged when this list was written — `swesim/workflows/workflows.py`.

Website, discovery and integrity (`website-2.md`):

- SQL half of the scan record: `DiscoveryRow.funnel/decisions/scan_path/scan_sha256` + a discovery-integrity migration (item 2) — direction; the file-backed Candidates page is enough to iterate — `swesim/index/{models,schema,project}.py`, a new Alembic revision **`0006_discovery_integrity` with `down_revision="0005_funnel_versions"`** (the `0005` slot was taken by the funnel reconciliation deployed in `d36b898`); head expectations in `tests/test_index_{schema,cli,store}.py`.
- §7 integrity views on the leaderboard (item 4): per-model `integrity_signal_count` distribution with flagged-trial links, protected-config touch detector, small-patch-vs-oracle heuristic, useful-question-rate columns, interactive-audit completeness badge — direction; measurement surface — `swesim/index/reports.py`, `LeaderboardRow`, `pages.leaderboard_body`.
- Facets without the first-500 subsets (item 6) — direction; UI completeness — `app.py:/trials` facets and the `limit=500` candidate lookup.
- Authenticated-page screenshots in both modes into `workspace/evidence/console/finish/` (item 7) — direction; needs the console running locally over the file reader — command in the report.
- Discovery retry policy (concern 3): `DiscoveryUnavailable` is retried under the default `retries.LLM` policy (4 attempts, 10s→2m); an `auth` failure (401/403) is retried too — decide `retries.READ`/dedicated policy and fail-fast on `kind == "auth"` — `swesim/workflows/activities.py:discover_activity`.
- Legacy `mine_github_repo` (`swesim/stages/mine.py`, debug-only) now propagates `DiscoveryUnavailable` instead of counting `fetch-error` (concern 4) — behaviour change in an unowned file; acceptable for a debug stage — note only.
- Hosted Candidates page lists `candidates/*/scans/` on the GCS mount per request (concern 5) — fine for tens of sources — the SQL half above or a TTL cache like `data._live_cache`.

Costs (`costs-2.md`, no deferred scope; follow-ups named in its concerns):

- Separate "served by" cell for `price_source` values such as `openrouter-generation (served by DeepInfra)` — optional readability — `swesim/dashboard/pages.py` cost table via `costs.served_providers_of` / `costs.price_source_key`.
- `record_cost_activity` passes no `idempotency_key` while `stages/run.py:_solver_cost` passes `"solver-final"` — both idempotent today; aligning gives one `cost_id` per trial — `swesim/workflows/activities.py:record_cost_activity`.
- Surface `accounting_summary()['complete']` next to every spend figure — handoff item — website/history owner.
- OpenRouter `provider.order` pinning — a RunConfig-relevant request-body knob inside the agent container that cannot be verified offline — `swesim/stages/run.py` once a smoke trial can confirm the served provider changes.

Infra (`infra-2.md`):

- File/SQL parity through a read-only maintenance Job `swesim-parity` (item 3, P2-13) — direction; also needs `scripts/cloud/parity_report.py` copied into `Dockerfile.worker` and excepted in `.dockerignore` — `infra/modules/run/main.tf` next to `index_maintenance`, `scripts/cloud/verify_cloud.sh` check 8; report to `evidence/cloud/parity-<UTC>.json`.
- Cloud Build trigger on push to main (item 5a, P3-18) — direction — `google_cloudbuild_trigger` in `infra/modules/registry/main.tf` (or a `build` module) with `cloudbuild_repository` / `cloudbuild_trigger_enabled` variables; the trigger builds only, rolling stays with `make deploy`.
- Selective open-executions check (item 5b, P3-18) — direction — `scripts/cloud/open_executions.py` mapping changed files in `<running sha>..<new sha>` to `@workflow.defn` types, conservative fallback to all types.
- Alert on the reconcile metric > 1 in 10 minutes (item 5c, P3-18) — direction; a fresh user metric is not queryable for a few minutes after creation (needs a second apply) — `google_monitoring_alert_policy` in `infra/modules/logging/main.tf`.
- `verify_cloud` runs `gh --version` (and the other shelled-out binaries) inside the image — lesson from the smoke above — `scripts/cloud/verify_cloud.sh`.

Added or re-motivated by the afternoon runs ([Hosted runs](#hosted-runs-2026-09-13-afternoon); ledger entries labelled 12:03–13:45):

- **Per-model screen labels.** `pytest-dev--pytest-7283` is `broken` because the screen model (`deepseek-v3.2`) scored full 0/1, while the four-model k=2 full results read flash 3/3, glm-5.3 2/2, v3.2 0/3, gpt-5-mini 0/2: the label describes the (scenario, screen model) pair. Rule stands (never hand-edit a label); design follow-up: a multi-model screen or a re-label from k=2 full results across models, and the brief's note that full k=1 is a noisy screen — `swesim/stages/difficulty.py`, brief §5.
- **`reconcile_workflows_activity`** (frontier item above) — re-motivated by the stale-`running` row whose root cause is not established; a Temporal-observed status with `lifecycle_source="temporal"` would have settled it without a rebuild.
- **Planned/launched/running trial rows** (frontier and history items above) — still deferred; the 24-trial comparison had only terminal rows to show.
- **`TrialRecord` provenance** (history item above, P2-11) — now a measured gap: trial `50d79778…`'s `record.json` has `worker_image_digest`/`origin` = None while its `launch.json` and the Job's `SWESIM_WORKER_IMAGE_DIGEST` env carry `8559aabd…` — `stages/run.py:TrialRecord`, `launchers/base.py:launch_once`.
- **Per-run spend cap** (frontier item above, P2-16) — still deferred; the afternoon's $3.0124 over 29 trials was bounded by hand (k=2, one scenario).
- **Discovery-integrity migration is `0006`** (website item above, corrected in place).
- **`max_parallel_trials` default.** The default of 1 serialised three runs, which were cancelled and resubmitted with `--max-parallel-trials 6`/`16`; raise the CLI and console default and document the flag in the runbook — `swesim/cli.py`, `swesim/dashboard/console.py:validate_run`, `docs/runbook.md`.
- **`stages/validate.py` debug path lacks `max_fresh_attempts`** (probe-retry concern) — debug-only stage; `b44052f` covers the workflow path.
- **Re-draft on a `fact-scope` rejection.** `draft_and_auto_approve` re-drafts once only on a leak rejection; a fact-scope rejection moves to the next candidate (`reports/fact-scope-2.md` concern 1) — `swesim/workflows/workflows.py`, behind a `workflow.patched` marker while executions are open.
- **GitHub App authentication for discovery** (installation-token minting, `GH_TOKEN_n` rotation) — in flight on `finish/gh-app`; until merged the sweep is bounded by one token's 5,000/h primary quota (about 3–5 scans per hour per the ledger's estimate, entry labelled 13:08).
- **Stakeholder reliability as an experiment axis** (user note for later, ledger entry labelled 12:12): wrong answers at a controlled rate, terse replies, personalities, then experiments on agent behaviour under each. Hooks exist (`Persona(summary, tone, disclosure_style)`, `PatienceProfile.terse_below`, `StakeholderConfig.prompt_version`; distinct versions never pool). Sequence: finish setup first, then design these as explicit conditions with their own `prompt_version` and cohort.

## Historical VM evidence

The VM backend used a local Temporal development server and one systemd unit per trial. Its results predate the Cloud Run path.

### Worker restart

Trial `26f3304d9caedfe10cd75ebf`, inside workflow `run-48574f7dd72f4ecfbd853dbea588f25b`, started its trial unit at 03:25:35 UTC. The worker restarted at 03:26:04 UTC, changing PID 5096 to 5647 while the trial unit remained active. The journal contains one start; one completed record reports reward 1.0; the restarted worker reconciled and ingested it.

Evidence beneath `workspace/evidence/vm/`: `workspace/evidence/swesim-trial-26f3304d9caedfe10cd75ebf.journal.txt`, `workspace/trials/26f3304d9caedfe10cd75ebf/{launch,record,report}.json`, and `workspace/evidence/run-history.json`.

### Laptop independence

Workflow `run-42874b1b2f724a129fa75fbfeb95a03e` ran from 03:46:06 to 03:55:38 UTC and completed two seaborn trials: `bb750ab72dd0751a972931ea` interactive and `b64138e19ab4e23fd94c54de` hidden, both reward 1.0. Its history has 41 events; the corresponding systemd journals and records are under `workspace/evidence/vm/`.

The reported no-SSH window was 03:46:03–04:10:46 UTC, written in `workspace/evidence/laptop_off_window.txt`. The lid remained open. **Laptop/session absence is operator-reported**: no independent SSH authentication log was found in the inspected mirror. The workflow artifacts establish VM progress during the reported interval, not physical laptop-off or Cloud Run acceptance.

The earlier VM creation workflow `create-da33e214d30d46a7a3eecc739c16b016` reached validation and ended invalid because its plausible-wrong patch did not apply. Its history is retained as a failure, not counted as an accepted task.

## Historical failures and resulting fixes

| Trigger | Observed failure | Resulting safeguard |
|---|---|---|
| requests-1944 | Network-dependent tests did not reproduce offline | Repository excluded from the pilot pool; probe measures the offline contract |
| sphinx-9467 | Published FAIL_TO_PASS tests already passed on untouched base | Empty measured FAIL_TO_PASS rejects a candidate |
| pytest-7283 / sklearn-13584 | Model-proposed protected paths shielded the oracle's source edit | Protected paths are code-owned; gold-touched source cannot be protected |
| sklearn-13584 | Whole-file and exact-id ordering produced different results | Definition-order exact-id probe and grading |
| pypdf-3826 / pytest-7283 | Corrupt model control patches failed before grading | Draft-time apply checks and fetched-source diff construction |
| Early pypdf trials | Missing gcc/make caused agent setup to attempt blocked apt access | Build tools installed in the task image |
| matplotlib-25547 | Base-image guard rejected the environment before controls ran | Infrastructure errors invalidate the scenario; they do not defeat attacks |
| sphinx-10097 | Alternative-correct control failed | Structural tasks require a passing alternative implementation |
| Initial v2 interactive workflows | Planner and runner hashed different scaffold identities | Shared effective scaffold identity; unresolved phantom ids remain in the record |
| Repository rebuild | Feature defaults obscured the successful bugfix band | Explicit discovery band and source identity |
| Reporter disclosure | Author-only requirements leaked through requirement text | Ground every requirement and sanitize all stakeholder-visible discoveries |
| Repeated withholding | One selected phrase could explain multiple removed sentences | Record each occurrence and reconstruct one occurrence per operation |
| Cached probes | Matching row metadata could reuse root-run or stale evidence | Binding recorded by the job, grading identity required on reuse and draft |
| Fallback controls | A docstring inversion could retain gold behavior | Full-source lexical state and distinct valid executable AST |
| Build provenance | Hash omitted runtime/render settings | Canonical configuration and rendered-file manifest, verified before use |
| Base reproducibility | Two image manifests were written but never compared | Collector digest and fail-closed verifier parity check |

The code changes above have offline regression coverage. Their presence does not retroactively validate old builds. Recompilation, controls and new hosted outcomes must name the code that ran.

## Retry and evidence rules

A retry is a new attempt index after reconciling any existing runtime. Preserve request, launch, status, record, reward, report and transcript files. Record the old trial id, terminal status, why a new attempt is justified and the new id. Never remove evidence merely to bypass duplicate-launch protection.

The root collector, isolated pytest launcher and report-consistency checks are tested defenses. Submitted code still shares the pytest process, so coordinated forgery remains a residual. Semantic stakeholder stance remains partly prompt-based and needs disclosure audit evidence.
