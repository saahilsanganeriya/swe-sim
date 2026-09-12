# Pending plan edits (apply after Codex's in-place review commit lands)

Do not edit `docs/superpowers/plans/2026-09-12-swesim-implementation.md` while Codex holds it.

1. Task 1 (models): add `OperationType.WITHHOLDING = "withholding"`; allow `Brief.provenance` in
   `{"original-issue", "withheld", "authored"}`; add `RunConfig.brief_profile: Literal["natural", "withheld"] = "withheld"`;
   validator: if any brief has profile `withheld`, every `withholding` operation target must be a fact id with
   `provenance == original-issue` and a `source_excerpt`, and none of those excerpts may appear in the withheld brief text
   (case-insensitive substring); tests for both.
2. Task 4 (compile): `hidden` and `interactive` use the brief selected by `run_config.brief_profile` (fall back to
   `natural` when no `withheld` brief exists); `full` always uses `natural` + visible facts. Test asserts the withheld
   brief text appears in hidden/interactive and the natural text in full.
3. Task 6 (draft_spec): add a second LLM call `withhold(natural_issue) -> {withheld_brief, removed: [{category, excerpt, kind, discovery}]}`
   with categories: reproduction, error_message, expected_output, location, version, proposed_fix; convert `removed` into
   ledger facts (`provenance: original-issue`, `source_url` = issue URL, `discovery` per item) and `operations` entries of
   type `withholding`; run the code leak check and retry once with the violating excerpts listed if it fails.
4. Task 6 tests: `test_withheld_brief_has_no_excerpts` with a fixture issue containing a traceback and a snippet.
5. Task 10 (github-repo): add `--kind feature|bugfix` bands from spec section 6 and the target repo list
   (python-statemachine, kombu, gql, narwhals, pytest, textual, pypdf); default `--max-prs 400`.
6. Task 12 (score): report `brief_profile` per group key (`condition|brief_profile|run_config_hash`); GOAL.md and
   verify.sh: add a check that at least one scenario has `hidden` runs on both `natural` and `withheld`.
7. CLAUDE.md hard rules: "hidden and interactive always share the same brief profile; full is always natural + visible facts;
   withheld removals are category-targeted and every removal is a ledger fact with the reporter's excerpt."
8. Task 6 (judge): add `issue_contract_delta(row) -> dict` (code-extracted: exception names and string literals in test_patch not in issue; new public defs in patch not in issue) fed into the judge prompt; add `swesim judge-calibrate --n 150 --model ...` that samples annotated instances, runs the judge with the Verified rubric (data: `data/annotations/ensembled_annotations_public.csv`; instructions: https://cdn.openai.com/introducing-swe-bench-verified/swe-b-annotation-instructions.pdf), and writes `workspace/judge_calibration.json` with agreement at the >=2 threshold, precision/recall, and a confusion table; docs/grading.md reports the number. Test: agreement computation on a synthetic set.
9. Task 6 (judge schema): replace the single `non_derivable_choice` field with `gaps: [{id, text, source: "test-literal|test-exception|patch-public-name|diff-branch|thread", derivable_from_repo: bool, consequential: bool, resolved_by: "thread|patch|none", category}]` plus `verified_rubric_score: 0-3`; acceptance rule in code: `keep = any(not g.derivable_from_repo and g.consequential and g.resolved_by == "thread" for g in gaps)`; default judge model `openai/gpt-5.5` (different from the draft model `anthropic/claude-sonnet-5`), both recorded in `difficulty.judged`.
