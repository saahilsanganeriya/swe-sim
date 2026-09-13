# Grading and measurement

This document describes the implemented grading contract and the preserved experimental record shown by the website. The hosted smoke started on 2026-09-13 and has produced **no graded hosted outcome yet** ([ops.md](ops.md#hosted-smoke-2026-09-13)); no new task yield, treatment effect or hosted model comparison is claimed.

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

The eight automatic approval gates cover strict schema, verified assets, judge acceptance, a load-bearing on-request fact, grounded requirements, withholding leaks, exact reconstruction and controls. Every identical removed occurrence needs its own ledger entry. Every requirement links to a visible fact or a verbatim human statement. Author-only facts remain unavailable to the stakeholder.

## Metrics and grouping

With `n` agent-attributable attempts (outcomes 1–4) and `c` successes in one frozen configuration:

```text
pass@k = 1 - C(n-c, k) / C(n, k)
pass^k = C(c, k) / C(n, k)
```

Both use draws without replacement and remain null when there are fewer than `k` attempts. `k` is not silently reduced. Infrastructure failures and unresolved attempts are reported separately; mean reward uses the same eligible denominator.

Groups preserve spec hash, condition, profile, model, purpose, compiled build and effective scaffold identity. Legacy runs whose configuration lacks `agent_config` derive that identity from `report.agent_config_sha256`. Calibration, screen, comparison and demonstration records never pool. Old documentation that pooled scaffold and default-agent runs does not establish a valid pass@k estimate for the new grouping.

Difficulty is measured by hidden/full screening with a specified solver and attempt budget. Diff size is a discovery filter. The leaderboard additionally requires current validation, an informative screen and coherent/fair task-sense labels. A saturated toy task or unfair hidden test is not evidence of model separation.

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

Ruling (ledger, 2026-09-13): **the judge stays `anthropic/claude-opus-5`** by the user's alignment directive; both numbers are reported here; cost if wrong is about two points of agreement on this calibration set. The runs of 2026-09-13 nevertheless used `openai/gpt-5.5` as judge because the Anthropic workspace API limit made Opus 5 and Sonnet 5 unreachable (fallback chain `swesim/roles.py` `ROLE_CHAINS["judge_model"]`, resolution recorded in `workspace/evidence/model-probe-takeover.json`; see [ops.md](ops.md#model-role-fallbacks-in-force)). Every candidate records the judge that actually scored it in `difficulty.judged.model_id`; do not pool candidates judged by different models when comparing yields.

## Deferred experimental evidence

The hosted creation smoke has started and no hosted trial has run ([ops.md](ops.md#hosted-smoke-2026-09-13)). Difficulty screening, Tier A–C comparisons, frontier-gated runs and a new disclosure/integrity sweep have not been executed on the hosted stack. As each runs, record actual commands, ids, sample sizes, model substitutions, cost/unknown usage and failures. Keep old builds, origins and configuration groups separate, and leave unsupported metrics null.

[Operations evidence](ops.md) tracks website verification, import parity and the deferred checks.
