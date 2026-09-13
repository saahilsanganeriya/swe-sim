# Grading and measurement

This document describes the implemented grading contract and the experimental record shown by the website. The first hosted graded results — 29 Cloud Run trials on one SWE-bench-sourced scenario on 2026-09-13 — are in [First hosted results](#first-hosted-results-2026-09-13) with their sample sizes; they are a smoke of the pipeline on one task. No task-yield, treatment-effect or cross-task model claim is made from them ([ops.md](ops.md#hosted-runs-2026-09-13-afternoon) has the operational record).

## Reward and outcomes

`reward.json` contains finite numbers only. `success` means the whole contract passed. `reward` is partial credit over fixed positive-weight requirement groups. A group earns its weight only if every linked FAIL_TO_PASS test passes; any PASS_TO_PASS regression forces reward to zero. Asking the stakeholder changes available information, never tests or reward weights.

| Code | Outcome | Interpretation |
|---|---|---|
| 1 | valid-correct | Full contract passed |
| 2 | valid-incorrect | Valid submission did not satisfy the full contract |
| 3 | agent-invalid-submission | Invalid or empty allowed change; base tests still run where applicable |
| 4 | agent-runtime-failure | Submission-time failure or inconsistent test evidence |
| 5 | collection-failure | Missing/invalid trusted collection evidence |
| 6 | simulator-failure | Stakeholder/simulator failure by adjudication |
| 7 | evaluator-failure | Harness, configuration, image or verifier failure |

Outcome, success and reward are distinct. For example, a source change plus a protected-test edit is graded on the allowed source change and also records the rejected edit. An infrastructure error is not evidence that an exploit was defeated. If the verifier cannot complete, its fallback outcome is evaluator-failure with zero reward/success.

## Build, collector and verifier

The compiler verifies declared asset bytes and SHA256, pins its inputs, and hashes canonical compile configuration plus the complete rendered-file manifest. That includes endpoint, hosts, timeouts, review configuration and rendered grader files. A run checks the manifest before creating a session or using the task. Legacy builds without it must be compiled and validated again.

Both images record a base file manifest. The root collector runs with a cleared environment and absolute executables, kills agent processes, bounds the submitted tree, archives it and freezes the stakeholder in `finally`. Its receipt includes `base_manifest_sha256` alongside archive hash, size/member counts, status, user and transcript hash. The verifier requires its own base manifest to match before processing the submission. Missing collector evidence is outcome 5; unequal bases or a missing verifier manifest are outcome 7.

The verifier is a separate offline container. It diffs against the untouched base, applies allowed source changes to the task's `/testbed` or `/app`, then overlays the hidden assertions. Keeping those runtime paths preserves editable imports. It never treats an agent-writable log directory as the collector's trust anchor.

Protected paths include contract tests, pytest configuration, conftest/startup hooks, package metadata, lockfiles and CI/git metadata. Symlinks, hard links, device files and escaping archive members are refused. Gold-touched source paths cannot be protected merely because a model suggested them.

## Test evidence and diagnostic budget

The main contract run uses the task's interpreter with `-I` and a trusted launcher that imports pytest before adding submitted roots. It runs as `tester`, with a compiled default budget of 600 seconds. Four signals must agree:

1. Collected node ids exactly equal the contract.
2. JUnit contains those ids once each.
3. Terminal status/category summaries agree with JUnit.
4. Exit status agrees with whether any test failed or errored.

Disagreement yields unknown observations, outcome 4 and zero reward. Reruns are diagnostic only and never restore credit. They have a **separate budget of another `test_timeout_sec`**, normally 600 seconds total across at most ten FAIL_TO_PASS ids. This is not merely the unused remainder of the first 600 seconds. The compiled verifier timeout is 1,800 seconds. Output-cap failures skip diagnostics rather than repeatedly running a flood.

These signals detect disagreement; they do not independently authenticate the behavior of arbitrary submitted code. The submission shares pytest's process and can attempt coordinated forgery of collection, JUnit, summaries and exit status, including late handlers. A forged summary is caught when it disagrees with the other evidence, not guaranteed caught in every attack.

## Controls and authoring gates

Before registration, validation executes:

| Control | Required evidence |
|---|---|
| No-op | Zero reward; measured base FAIL_TO_PASS fails and PASS_TO_PASS holds |
| Oracle | Success 1 and reward 1 |
| Plausible-wrong | Applies, fails correctness and stays below its declared reward ceiling |
| Alternative-correct | Passes when required for structural tests |
| Incorrect-exploit tamper | Applies and earns exactly zero without an infrastructure failure |
| Reachability | Gold objects and distinctive post-fix material are unavailable to the agent |

The reachability probe pairs distinctive added lines with their hunk's removed lines so a stale base copy is not automatically classified as a post-fix leak. This heuristic is not a complete proof that no useful oracle information exists.

Probe jobs record the binding of the measured inputs and the grading identity. Cached rows and drafting require a matching binding and `test_user=tester`. An old row-level binding alone cannot validate root-run or unbound evidence.

A fallback plausible-wrong patch must come from fetched Python source, apply, parse, and differ from gold in executable AST after docstrings are excluded. Lexical state carries through diff-hunk gaps and continued strings. This prevents a docstring edit from masquerading as an incorrect control; executed validation still decides whether the behavior is wrong.

The nine automatic approval gates cover strict schema, verified assets, judge acceptance, a load-bearing on-request fact, grounded requirements, withholding leaks, exact reconstruction, controls and — the ninth, added on 2026-09-13 after the pytest-7283 result below — fact scope: a stakeholder-visible fact may not assert behaviour that only a PASS_TO_PASS test carries (`fact-scope`, commit `86bc2e6`, `.superpowers/sdd/2026-09-13-hosted-pipeline/reports/fact-scope-2.md`). Every identical removed occurrence needs its own ledger entry. Every requirement links to a visible fact or a verbatim human statement. Author-only facts remain unavailable to the stakeholder.

## Metrics and grouping

With `n` agent-attributable attempts (outcomes 1–4) and `c` successes in one frozen configuration:

```text
pass@k = 1 - C(n-c, k) / C(n, k)
pass^k = C(c, k) / C(n, k)
```

Both use draws without replacement and remain null when there are fewer than `k` attempts. `k` is not silently reduced. Infrastructure failures and unresolved attempts are reported separately; mean reward uses the same eligible denominator.

Groups preserve spec hash, condition, profile, model, purpose, compiled build and effective scaffold identity. Legacy runs whose configuration lacks `agent_config` derive that identity from `report.agent_config_sha256`. Calibration, screen, comparison and demonstration records never pool. Old documentation that pooled scaffold and default-agent runs does not establish a valid pass@k estimate for the new grouping.

**Difficulty is model-dependent** (user ruling, 2026-09-13 14:50 PDT; ledger `.superpowers/sdd/2026-09-13-hosted-pipeline/progress.md`). The single-model screen (`purpose=screen`, `openrouter/deepseek/deepseek-v3.2`, hidden k=2 and full k=1 → `saturated` | `informative` | `broken`; `swesim/stages/difficulty.py`) is retired as a leaderboard gate; its label stays in `difficulty.json` and on the scenario row as an informational chip recording what that screen run measured, and as a filter the operator may apply. Difficulty is read on the console's Trends page as the **capability gradient** (`swesim/dashboard/gradient.py`): accuracy by solver tier A→E per condition and brief profile, with a per-scenario chip — `suspicious` when a Tier A/B model beats the best Tier D/E model by more than one trial's worth at the available n, `saturated` when every model with n ≥ 2 is at 100%, `holds` when the best accuracy per tier is non-decreasing A→E, `not-monotone` otherwise. The leaderboard admits every scenario that is `valid` and passes the task-sense audit (`coherent` and `fair_tests`), whatever its label. Diff size remains a discovery filter.

Motivating case: `pytest-dev--pytest-7283` was labelled `broken` by the screen (`deepseek-v3.2` hidden 1/2, full 0/1) while `deepseek-v4.1-flash` (7/7) and `glm-5.3` (6/6) solved every condition and `gpt-5-mini` (interactive 2/2, full 0/2) and `deepseek-v3.2` (interactive 2/2, full 0/3) solved interactive but not full — the label described one model, not the task ([First hosted results](#first-hosted-results-2026-09-13); `workspace/evidence/sweep/pytest-7283-comparison-2026-09-13.json`). Cost if the ruling is wrong: an easy task inflates every model's score until the gradient chips flag it. A saturated toy task or an unfair hidden test is still not evidence of model separation. The frontier admission gate (`swesim/stages/frontier.py`) still exists, but the operator can proceed past a refusal with a recorded reason (`swesim run-tasks --frontier-override "<reason>"`; [runbook.md](runbook.md#frontier-gate-tier-de-solvers)) and its "15 informative scenarios" clause no longer decides frontier runs. Until 14:50 the leaderboard additionally required an `informative` screen; that rule is preserved in the brief's §5 below its supersession note.

## First hosted results (2026-09-13)

One scenario, **`pytest-dev--pytest-7283`** (source `swebench-annotated (pool A)`), registered on the hosted stack by `create-b788cb22d7064a4d8bcbe63bb943c709` through seven executed controls (judge `openai/gpt-5.5`, drafter `openai/gpt-5.6-sol` — the pre-roll fallbacks, [ops.md](ops.md#model-role-fallbacks-in-force)). Contract: 1 FAIL_TO_PASS, 51 PASS_TO_PASS (the first trial's report: `f2p 0/1, p2p 51/51`). Three `RunTasksWorkflow`s produced 29 trials, every one a Cloud Run Job with one execution ([ops.md](ops.md#hosted-runs-2026-09-13-afternoon)):

| Run | Purpose | Cells |
|---|---|---|
| `run-ef32bb35557448578cd757b236a44805` | demonstration | `openrouter/deepseek/deepseek-v3.2` hidden k=1 (trial `50d797789546d178d8add487`: valid-incorrect, reward 0.0, $0.43879) |
| `run-5c7c7c788a394238ae7ae69ab5a24cc7` | screen | `deepseek-v3.2` hidden k=2, full k=1 → `difficulty_label` tier `broken` (hidden 1/2, full 0/1), labelled 20:02:53Z |
| `run-2528e1bd073e4953a170f797a848822e` | comparison | `deepseek-v3.2`, `deepseek-v4.1-flash`, `glm-5.3`, `gpt-5-mini` × full/hidden/interactive × k=2 = 24 trials; stakeholder `anthropic/claude-opus-5` |

Evidence: `workspace/evidence/sweep/pytest-7283-comparison-2026-09-13.json` (generated 20:28:21Z), whose cells aggregate the three runs by model × condition, and `workspace/evidence/cloudrun/executions-50d797789546d178d8add487.json` for the first trial. Cost per trial below is the cell's `cost_usd` divided by its `n`, as recorded in each trial's `costs.json` (price source per record; OpenRouter generations name the served provider).

| Model (JSON name) | Condition | Solved / n | Cost per trial (cell total) | Integrity flags |
|---|---|---:|---|---:|
| `deepseek-v3.2` | full | 0 / 3 | $0.281 ($0.8435) | 0 |
| `deepseek-v3.2` | hidden | 3 / 5 | $0.235 ($1.1759) | 4 |
| `deepseek-v3.2` | interactive | 2 / 2 | $0.233 ($0.4651) | 0 |
| `deepseek-v4.1-flash` | full | 3 / 3 | $0.0040 ($0.0120) | 1 |
| `deepseek-v4.1-flash` | hidden | 2 / 2 | $0.0016 ($0.0032) | 0 |
| `deepseek-v4.1-flash` | interactive | 2 / 2 | $0.0023 ($0.0045) | 2 |
| `glm-5.3` | full | 2 / 2 | $0.045 ($0.0897) | 2 |
| `glm-5.3` | hidden | 2 / 2 | $0.054 ($0.1078) | 2 |
| `glm-5.3` | interactive | 2 / 2 | $0.048 ($0.0962) | 2 |
| `gpt-5-mini` | full | 0 / 2 | $0.035 ($0.0692) | 0 |
| `gpt-5-mini` | hidden | 1 / 2 | $0.039 ($0.0785) | 0 |
| `gpt-5-mini` | interactive | 2 / 2 | $0.033 ($0.0667) | 0 |

`total_trials: 29`, `total_cost_usd: 3.0124`. The `deepseek-v3.2` hidden and full cells pool three purposes (demonstration 0/1 hidden; screen 1/2 hidden, 0/1 full; comparison the rest), which the grouping rule above forbids for pass@k: the comparison-only `deepseek-v3.2` cells are therefore **full 0/2, hidden 2/2, interactive 2/2** (derived: 3−0−1 hidden successes over 5−1−2 trials; 0 full successes over 3−1 trials). `deepseek-v4.1-flash` full has n=3 against a k=2 request; the JSON does not attribute trials to runs, so the third attempt's run is not identified. No pass@k is reported: no single-purpose cell has three attempts.

**Inverted ordering, full < hidden < interactive, for two models.** `deepseek-v3.2` 0/3 < 3/5 < 2/2 and `gpt-5-mini` 0/2 < 1/2 < 2/2. The full brief is the natural issue plus every stakeholder-visible fact; hidden is the selected brief alone. Fact `f05` of this scenario ("same skipped lifecycle behaviour applies to … pytest's skip marker") asserts behaviour beyond the single FAIL_TO_PASS test (`testing/test_unittest.py::test_pdb_teardown_skipped[@unittest.skip]`): the sibling parametrisation `[@pytest.mark.skip]` is a PASS_TO_PASS test that penalises implementing it. One `gpt-5-mini` full attempt did exactly that — 50/51 PASS_TO_PASS, and the regression rule set its reward to 0 (ledger entries labelled 12:58 and 13:20; the JSON's first note). The extra information in full was wrong for the contract, so full underperformed hidden; the interactive cells (the hidden brief plus the channel) solved 2/2 for both models — whether and what they asked is not yet audited on the hosted stack. This is a task-authoring defect, not a property of asking: `fair_tests` in the saved `audit.json` (`openai/gpt-5.6-sol`, 19:06Z) checks tests ⇐ facts and passed the scenario, because nothing checked facts ⇒ contract. The `facts_within_contract` label and the `fact-scope` gate (above) exist because of this trial.

**Integrity flags.** Every flag in the table is a `protected-path-edit` of `testing/test_unittest.py`: the agent edited the contract's test file; the verifier dropped the edit and graded the allowed source change (see "Build, collector and verifier"). Reward is unaffected by the dropped edit; the trial is flagged. Counts: `deepseek-v3.2` 4 (all hidden), `deepseek-v4.1-flash` 3, `glm-5.3` 6 (every one of its trials), `gpt-5-mini` 0. The flag says the model touched tests, not that it gamed anything; a dropped edit that changes the source's behaviour is graded on that source.

**Difficulty is model-relative.** `deepseek-v4.1-flash` (7/7) and `glm-5.3` (6/6) saturate this task in every condition; `deepseek-v3.2` and `gpt-5-mini` separate the conditions. The screen labelled the scenario `broken` because the screen model (`deepseek-v3.2`) scored full 0/1 — one failure at k=1 — so under the rule then in force it was off the leaderboard, while for two other models it is saturated and for two it is informative. The label was not hand-edited; it became the motivating case for the 14:50 PDT ruling that demoted the label to an informational chip ([Metrics and grouping](#metrics-and-grouping)); the multi-model screen follow-up in [ops.md](ops.md#deferred-from-the-2026-09-13-finishing-run) is no longer needed as a gate.

**Cost.** Per trial: `deepseek-v3.2` $0.23–0.28 (the first trial $0.43879 on 3,141,695 input / 31,655 output tokens, served by StreamLake and SiliconFlow); `deepseek-v4.1-flash` $0.0016–0.0040; `glm-5.3` $0.045–0.054; `gpt-5-mini` $0.033–0.039. 29 trials cost $3.0124 in total. Cost is not a difficulty signal; it varies two orders of magnitude across models at the same success.

**Caveat.** n is 2–5 per cell, all on one scenario, one stakeholder prompt version, one screen model, and the interactive cells' asks and replies have not been audited on the hosted stack. Nothing here supports a cross-task claim about conditions or models; the numbers are what the pipeline measured on its first task end to end.

## Preserved inventory — 2026-09-13

The read-only source is `registry.json`, `workspace/scenarios/*/scenario.json` and `workspace/trials/*/{record,run_config}.json`. There are **50 trial records**; **19 match the current registered version-2 spec hashes**. The other 31 belong to earlier specs. These are laptop/VM-era results, not new Cloud Run experiments.

| Current registered spec | F2P / P2P | Current-spec records | Correct / incorrect |
|---|---:|---:|---:|
| `mwaskom--seaborn-3190` v2, `812be6e0…` | 1 / 84 | 6 | 6 / 0 |
| `scikit-learn--scikit-learn-13584` v2, `4e3ecb62…` | 6 / 3 | 7 | 7 / 0 |
| `py-pdf--pypdf-3826` v2, `89338bfe…` | 2 / 95 | 6 | 0 / 6 |

The 19 records contain 13 Haiku 4.5 trials and 6 Sonnet 5 trials. The exact historical cells are:

| Scenario | Model | Condition/profile | n | Successes | Recorded config hash |
|---|---|---|---:|---:|---|
| seaborn | Haiku | full/natural | 1 | 1 | `50783ca4…` |
| seaborn | Haiku | hidden/natural | 1 | 1 | `47a3098c…` |
| seaborn | Haiku | interactive/natural | 2 | 2 | `ed408a96…` |
| seaborn | Sonnet | interactive/natural | 2 | 2 | `d45e87b6…` |
| sklearn | Haiku | full/natural | 1 | 1 | `7d316eb1…` |
| sklearn | Haiku | hidden/natural | 1 | 1 | `e6048dec…` |
| sklearn | Haiku | hidden/withheld | 1 | 1 | `914ff147…` |
| sklearn | Haiku | interactive/withheld | 2 | 2 | `36ca35b4…` |
| sklearn | Sonnet | interactive/withheld | 2 | 2 | `eef1a538…` |
| pypdf | Haiku | full/natural | 1 | 0 | `5a5256b6…` |
| pypdf | Haiku | hidden/withheld | 1 | 0 | `a8e6771b…` |
| pypdf | Haiku | interactive/withheld | 2 | 0 | `c1e834f8…` |
| pypdf | Sonnet | interactive/withheld | 2 | 0 | `c4b3355c…` |

These counts describe stored outcomes, not a treatment-effect claim. None of these cells supplies three attempts for pass@3. The pypdf failures also do not establish informative difficulty: the saved task-sense audit flagged an unfair hidden-test dependency.

Scenario version and stakeholder prompt version are different fields. These older run configs still contain `prompt_version=v1`, while transcript annotations identify v2 reporter/filter behavior for ten interactive audited trials. Two no-exchange trials were reported as unknown by the historical audit. Preserve that provenance discrepancy; do not silently rewrite the source records or pool them with current v3 configurations.

A minimal inventory check after pulling the same record is:

```python
import json
from swesim.models import ScenarioSpec
from swesim.settings import get_settings
s = get_settings()
catalog = json.loads(s.registry_path.read_text())["scenarios"]
hashes = {
    sid: ScenarioSpec.model_validate_json(
        (s.workspace / "scenarios" / sid / "scenario.json").read_text()
    ).spec_hash
    for sid in catalog
}
records = [
    json.loads(path.read_text())
    for path in (s.workspace / "trials").glob("*/record.json")
]
current = [r for r in records if hashes.get(r.get("scenario_id")) == r.get("spec_hash")]
print({"records": len(records), "current_spec_records": len(current)})
```

## Historical task-sense and disclosure audit

Artifact `workspace/evidence/audit/2026-09-13-task-sense.json`, generated at 09:07:36 UTC with rubric `2026-09-13.1`, covers the preserved record. Its model-reading labels are historical observations; the stronger current drafter/filter has not been validated by a new hosted experiment.

| Scenario | Coherent | Fair tests | Plausible withholding |
|---|---|---|---|
| seaborn v2 | true | true | unknown/not applicable: natural-only brief |
| sklearn v2 | true | true | false: withheld its only error description |
| pypdf v2 | true | false | true |

The pypdf audit found a hidden test calling an oracle-introduced private helper/class constant and an exact warning-string requirement. The legacy stakeholder could also disclose ungrounded requirements that remained public after their facts became author-only. Current requirement-grounding gates address that path; they do not retroactively make the preserved task fair.

Across the audit's 12 current-spec interactive trials, ten asked at least once. The report contains 22 replies: eight labelled as volunteering information and six as disclosing test/identifier or ungrounded requirements. Five trials edited protected test files; those edits were dropped before grading. These are audit labels, not independently adjudicated training labels.

Historical v1 diff review also exposed test construction, including expected hidden-test behavior. Current default interaction is behavioral confirmation. The service filters hidden-test identifiers and recognized option markers, rejects code-shaped confirmation input, and keeps patience tier and fact/redaction annotations evaluator-only. Semantic reporter stance and unmarked option choices remain partly prompt-enforced and require delivered-reply audits.

The historical audit estimated usage from characters; its `llm_usage` field is null. Do not present that estimate as measured provider billing. Current code captures structured usage when the provider supplies it.

**Fact scope (added 2026-09-13).** The first hosted scenario, `pytest-dev--pytest-7283` (SWE-bench pool A), passed the audit with `coherent=true`, `fair_tests=true`, `plausible_withholding=true` and still failed in the `full` condition: its ledger carried the on-request fact f05, "The same skipped lifecycle behavior applies to unittest.TestCase methods skipped with unittest.skip and with pytest's skip marker", while the contract's only FAIL_TO_PASS test is `testing/test_unittest.py::test_pdb_teardown_skipped[@unittest.skip]` and the `@pytest.mark.skip` parametrisation sits in PASS_TO_PASS. Models given every stakeholder-visible fact acted on f05, changed more than the contract asks, and one gpt-5-mini attempt regressed a pass-to-pass test (50/51, reward 0); `full` scored 0/3 and 0/2 for the two models while `interactive` scored 2/2 for both. `fair_tests` checks that the tests follow from the facts, not that every fact stays within what the tests require. Rubric `2026-09-13.2` adds the scenario label `facts_within_contract` with per-fact detail `fact_scope` (`fact_id`, `within_contract`, `reason`, `offending_tests`, `markers`): a mechanical rule flags a stakeholder-visible fact that names a parametrisation id or code identifier found only in PASS_TO_PASS tests (judged against the FAIL_TO_PASS ids and test bodies from the test assets; polarity ignored), and the reading pass (scenario prompt v2, Opus 5 with the gpt-5.6-sol fallback) judges every visible fact against the FAIL_TO_PASS and PASS_TO_PASS bodies; the mechanical verdict wins where it fires. The label is projected to the scenario row's `task_sense` and counted in the audit report; the leaderboard gate stays `coherent and fair_tests`. The same mechanical rule is now the code-owned approval gate `fact-scope`: a draft whose visible fact names a PASS_TO_PASS-only marker is rejected with the fact id and the offending test id (author-only facts are exempt), and per the standing rule the draft is never hand-edited: the workflow re-drafts once or takes the next candidate.

## Exports and training eligibility

Exports separate `policy_visible` observations from `evaluator_only` annotations and carry frozen lineage. A service-generated reply is insufficient evidence of agent receipt; the policy export must match an actual tool observation.

The 50 preserved exports contain **17 `training_eligible=true` flags**: six full and eleven hidden episodes. The other 33 are false. The earlier claim that every export was false was incorrect. Interactive episodes remain unadjudicated unless a later explicit adjudication says otherwise; their transcript binding must also hold.

A legacy non-interactive flag reflects the exporter rules that wrote it. It is not an adjudication of fairness, disclosure quality or suitability for training, and it does not override the later task-sense findings. The website should expose the underlying provenance rather than advertise all flagged episodes as training-ready.

## Judge calibration

The metric defines a positive prediction as **`verified_rubric_score >= 2`** (threshold 2), compared with the human `underspecified >= 2` label on the 150-row annotated SWE-bench pool. It does not use the narrower creation `keep` decision as the positive label. A calibration artifact records per-instance scores, booleans, confusion class, errors, seed, model/prompt identity, usage and cost.

Two judges have been calibrated on the same 145 judged rows (150 requested, 5 oversized inputs errored) with prompt `judge-v3`:

| Judge | Agreement | Precision | Recall | tp / fp / tn / fn | Artifact |
|---|---:|---:|---:|---|---|
| `anthropic/claude-opus-5` | 0.634 | 0.604 | 0.460 | 29 / 19 / 63 / 34 | `workspace/calibration/anthropic--claude-opus-5/2026-09-13.json` (per-instance rows; started 09:00:15Z, finished 09:08:03Z; usage complete: 1,392,326 input / 154,082 output tokens over 145 calls, $10.81 from the Anthropic pricing page, cost record `01M2D0C41D03P1NTYPQPPXWB81`) |
| `openai/gpt-5.5` | 0.655 | 0.644 | 0.460 | 29 / 16 / 66 / 34 | `workspace/judge_calibration.json` (summary only, laptop run 2026-09-12/13; carried as `reference` inside the Opus 5 artifact) |

Recall is identical; Opus 5 accepts three more false positives. The gpt-5.5 summary has **no per-instance predictions**, so it cannot be re-scored under a changed threshold; the Opus 5 artifact can.

Ruling (ledger, 2026-09-13): **the judge stays `anthropic/claude-opus-5`** by the user's alignment directive; both numbers are reported here; cost if wrong is about two points of agreement on this calibration set. The creations of 2026-09-13 that started before the `400c288` roll (verified 19:12:15Z) — including the one that registered `pytest-dev--pytest-7283` — nevertheless used `openai/gpt-5.5` as judge because the Anthropic workspace API limit made Opus 5 and Sonnet 5 unreachable (fallback chain `swesim/roles.py` `ROLE_CHAINS["judge_model"]`, resolution recorded in `workspace/evidence/model-probe-takeover.json`); workflows started after that roll, on key version 2, resolve to `anthropic/claude-opus-5` again (see [ops.md](ops.md#model-role-fallbacks-in-force)). Every candidate records the judge that actually scored it in `difficulty.judged.model_id`; do not pool candidates judged by different models when comparing yields.

## Deferred experimental evidence

The hosted stack has run one scenario end to end ([First hosted results](#first-hosted-results-2026-09-13); [ops.md](ops.md#hosted-runs-2026-09-13-afternoon)). Not yet run on it: a screen on a novel (repository-sourced) scenario, Tier A–C comparisons behind the frontier gate, and a disclosure/integrity audit of the hosted interactive trials; the since-2023 both-band creation sweep is running. As each runs, record actual commands, ids, sample sizes, model substitutions, cost/unknown usage and failures. Keep old builds, origins and configuration groups separate, and leave unsupported metrics null.

[Operations evidence](ops.md) tracks website verification, import parity and the deferred checks.
