"""Cross-module contract test: every name the plan's Interfaces blocks promise must import with the promised callable shape.

Run after Phase 2 integration. A failure here means a stream drifted from the frozen interface, not that a feature is broken.
"""
from __future__ import annotations
import importlib, inspect
import pytest

CONTRACTS: dict[str, dict[str, list[str] | None]] = {
    # module: {name: [required parameter names in order] or None for classes/constants}
    "swesim.models": {"ScenarioSpec": None, "RunConfig": None, "Fact": None, "Requirement": None, "Contract": None,
                      "Controls": None, "Group": None, "Check": None, "Asset": None, "Brief": None, "Persona": None,
                      "Operation": None, "Environment": None, "Difficulty": None, "PatienceProfile": None,
                      "StakeholderConfig": None, "canonical_hash": ["model"],
                      "trial_id": ["spec_hash", "condition", "run_config_hash", "attempt_index"], "minimal_spec_dict": None},
    "swesim.paths": {"Workspace": None, "atomic_json": None},
    "swesim.llm": {"chat_json": ["model_id", "system", "messages", "schema_hint"], "LLMError": None},
    "swesim.harbor_cli": {"run_task": ["task_dir", "agent", "jobs_dir", "job_name"], "read_reward": ["trial_dir"],
                          "read_report": ["trial_dir"], "HarborRunError": None},
    "swesim.verifier.policy": {"is_protected": ["path", "globs"], "safe_extract": ["tar_path", "dest"],
                               "change_set": ["pristine", "tree"], "change_hash": ["base", "tree", "cs"],
                               "apply_change_set": ["target", "tree", "cs", "protected"]},
    "swesim.verifier.parsers": {"parse_pytest_ra": ["stdout", "expected"], "parse_junit": ["xml_path", "expected"],
                                "reconcile": ["stdout", "xml_path", "expected", "rc"]},
    "swesim.verifier.grader": {"GradeConfig": None, "GradeResult": None, "grade": ["cfg"]},
    "swesim.stages.compile": {"compile_scenario": ["ws", "spec", "run_config", "stakeholder_url"], "CompileResult": None},
    "swesim.stages.validate": {"validate_scenario": ["ws", "spec", "run_config", "stakeholder_url"]},
    "swesim.stages.approve": {"approve_scenario": ["ws", "scenario_id", "auto"], "auto_gates": ["spec", "sdir"]},
    "swesim.agents.patch_agent": {"PatchAgent": None},
    "swesim.sources.swebench_annotated": {"pool_a": ["csv_path"], "candidates": ["instance_ids", "out_path"]},
    "swesim.sources.threads": {"fetch_pr_thread": ["repo", "pr"], "fetch_issue_thread": ["repo", "number"]},
    "swesim.sources.github_repo": {"discover": ["repo", "since"], "build_recipe": ["repo", "base_commit"],
                                   "candidate_to_row": ["c"], "probe_tests": ["row", "ws"]},
    "swesim.stages.mine": {"mine_swebench": ["ws", "pool"], "mine_github_repo": ["ws", "repo"]},
    "swesim.stages.judge": {"issue_contract_delta": ["row"], "judge_candidate": ["row", "model_id"], "accept": None,
                            "calibrate": ["csv_path", "rows_by_id", "model_id", "n"]},
    "swesim.stages.draft_spec": {"draft": ["row", "judged", "model_id", "out_dir"]},
    "swesim.tamper": {"root_conftest_noop": [], "skip_marker": ["base_files"], "sitecustomize_junit_forge": []},
    "swesim.stakeholder.prompts": {"system_prompt": ["view", "persona", "patience", "review_mode"]},
    "swesim.stakeholder.view": {"visible_view": ["spec"]},
    "swesim.stakeholder.service": {"app": None},
    "swesim.stages.run": {"run_trial": ["ws", "spec", "rc", "attempt_index", "stakeholder_url", "worker_secret"], "TrialRecord": None},
    "swesim.stages.register": {"register_scenario": ["ws", "spec"]},
    "swesim.stages.score": {"pass_at_k": ["n", "c", "k"], "pass_pow_k": ["n", "c", "k"], "score_scenario": ["ws", "scenario_id"]},
    "swesim.stages.export": {"export_scenario": ["ws", "scenario_id"]},
    "swesim.dashboard.app": {"app": None, "merge_timeline": None},
    "swesim.workflows.workflows": {"PipelineWorkflow": None, "RunMatrixWorkflow": None, "TrialWorkflow": None},
    "swesim.workflows.activities": {"auto_approve_activity": ["p"], "plan_trials": ["p"]},
    "swesim.stakeholder.session": {"Session": None, "Reply": None, "REFUSAL": None},
    "swesim.stakeholder.tools": {"read_diff": None, "read_changed_file": None},
}

CASES = [(m, n, p) for m, names in CONTRACTS.items() for n, p in names.items()]

@pytest.mark.parametrize("module,name,params", CASES, ids=[f"{m}.{n}" for m, n, _ in CASES])
def test_contract_name(module, name, params):
    mod = importlib.import_module(module)
    obj = getattr(mod, name)
    if params is None:
        return
    sig = inspect.signature(obj)
    actual = [p for p in sig.parameters]
    assert actual[: len(params)] == params, f"{module}.{name} params {actual} do not start with {params}"


def test_trial_dir_convention_and_record_keys():
    """Cross-stream file convention: run.py writes workspace/trials/<trial_id>/{record,reward,report,transcript,status}.json
    and the orchestration/scoring streams read exactly those names."""
    import pathlib
    run_src = pathlib.Path("swesim/stages/run.py").read_text()
    for name in ("record.json", "reward.json", "report.json", "transcript.json", "status.json", "run_config.json", "spec.json"):
        assert name in run_src, name
    for consumer in ("swesim/workflows/activities.py", "swesim/stages/score.py", "swesim/dashboard/app.py"):
        src = pathlib.Path(consumer).read_text()
        assert "record.json" in src, consumer
    assert '"trials"' in run_src or "'trials'" in run_src


def test_receipt_and_grader_config_keys_agree():
    """collect.py receipt keys == what the grader reads; compile's grader_config fields == GradeConfig fields."""
    import pathlib, re, inspect
    from swesim.verifier import grader
    collect_src = pathlib.Path("swesim/templates/collect.py").read_text()
    m = re.search(r"RECEIPT_KEYS = \((.*?)\)", collect_src, re.S)
    receipt_keys = set(re.findall(r"'([a-z_0-9]+)'", m.group(1)))
    assert {"status", "rc", "user", "sha256", "bytes", "files", "freeze_status", "transcript_hash"} <= receipt_keys
    fields = set(inspect.signature(grader.GradeConfig).parameters)
    compile_src = pathlib.Path("swesim/stages/compile.py").read_text()
    for f in ("artifacts_dir", "pristine_dir", "work_dir", "expected_tests", "groups", "protected", "runner", "test_user", "verifier_out", "test_timeout_sec", "target_dir", "contract_patch"):
        assert f in fields, f
        assert f'"{f}"' in compile_src, f
