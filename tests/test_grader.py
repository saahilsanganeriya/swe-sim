import json, os, tarfile, shutil, subprocess, sys
from pathlib import Path
import pytest
from swesim.verifier import grader

def _tar_tree(tree: Path, out: Path):
    with tarfile.open(out, "w") as t: t.add(tree, arcname=".")

def _pristine(tmp_path):
    p = tmp_path / "pristine"; (p / "pkg").mkdir(parents=True); (p / "tests").mkdir()
    (p / "pkg" / "__init__.py").write_text("")
    (p / "pkg" / "mod.py").write_text("def f(x):\n    return x\n")
    (p / "tests" / "test_mod.py").write_text("from pkg.mod import f\ndef test_new():\n    assert f(2) == 4\ndef test_old():\n    assert f(0) == 0\n")
    return p

def _base_manifest(tmp_path):
    path = tmp_path / "verifier-base-manifest.txt"
    path.write_text("trusted base file hashes\n")
    return path


def _cfg(tmp_path, pristine, artifacts):
    out = tmp_path / "verifier_out"; out.mkdir(exist_ok=True)
    return grader.GradeConfig(artifacts_dir=artifacts, pristine_dir=pristine, work_dir=tmp_path / "work",
        expected_tests={"tests/test_mod.py::test_new": "f2p", "tests/test_mod.py::test_old": "p2p"},
        groups=[{"id": "g01", "weight": 1.0, "tests": ["tests/test_mod.py::test_new"]}],
        protected=["tests/**", "**/conftest.py", "**/sitecustomize.py", "**/usercustomize.py", "**/*.pth"],
        runner={"kind": "pytest-ra", "command": sys.executable + " -I " + str(Path(grader.__file__).with_name("run_pytest.py")) + " -rA -p no:cacheprovider --junitxml={junit_path} tests",
                "single_command": sys.executable + " -I " + str(Path(grader.__file__).with_name("run_pytest.py")) + " -rA -p no:cacheprovider --junitxml={junit_path} {nodeid}"},
        test_user=None, verifier_out=out, base_manifest=_base_manifest(tmp_path))

def _artifacts(tmp_path, tree):
    a = tmp_path / "artifacts"; a.mkdir(exist_ok=True); _tar_tree(tree, a / "app.tar")
    (a / "collect.status").write_text(json.dumps({"status":"ok", "rc":0, "sha256":grader.sha256_file(a / "app.tar"), "user":"root",
                                                "base_manifest_sha256": grader.sha256_file(_base_manifest(tmp_path))})); return a

@pytest.mark.parametrize("evidence", ["different", "missing", "missing-verifier"])
def test_base_manifests_must_match_before_submission_tests(tmp_path, monkeypatch, evidence):
    pristine = _pristine(tmp_path)
    artifacts = _artifacts(tmp_path, pristine)
    cfg = _cfg(tmp_path, pristine, artifacts)
    manifest = tmp_path / "base-manifest.txt"
    manifest.write_text("base image file hashes\n")
    cfg.base_manifest = manifest
    receipt = json.loads((artifacts / "collect.status").read_text())
    receipt["base_manifest_sha256"] = grader.sha256_file(manifest) if evidence == "missing-verifier" else "0" * 64
    if evidence == "missing": receipt.pop("base_manifest_sha256")
    if evidence == "missing-verifier": manifest.unlink()
    (artifacts / "collect.status").write_text(json.dumps(receipt))

    def never_run(*args, **kwargs):
        raise AssertionError("submission tests ran before base parity was established")

    monkeypatch.setattr(grader, "_run_tests", never_run)
    result = grader.grade(cfg)
    assert result.reward["success"] == 0 and result.reward["reward"] == 0
    assert "base manifest" in result.report["error"]
    assert result.report["outcome"] == ("collection-failure" if evidence == "missing" else "evaluator-failure")


def test_correct_patch_scores_success(tmp_path):
    pristine = _pristine(tmp_path); tree = tmp_path / "tree"; shutil.copytree(pristine, tree)
    (tree / "pkg" / "mod.py").write_text("def f(x):\n    return x * 2\n")
    grader.grade(_cfg(tmp_path, pristine, _artifacts(tmp_path, tree)))
    r = json.loads((tmp_path / "verifier_out" / "reward.json").read_text())
    assert r["success"] == 1 and r["reward"] == 1.0 and r["outcome_code"] == 1

def test_editing_protected_test_is_dropped_and_signaled(tmp_path):
    pristine = _pristine(tmp_path); tree = tmp_path / "tree"; shutil.copytree(pristine, tree)
    (tree / "tests" / "test_mod.py").write_text("def test_new(): pass\ndef test_old(): pass\n")
    grader.grade(_cfg(tmp_path, pristine, _artifacts(tmp_path, tree)))
    r = json.loads((tmp_path / "verifier_out" / "reward.json").read_text()); rep = json.loads((tmp_path / "verifier_out" / "report.json").read_text())
    assert r["success"] == 0 and r["reward"] == 0.0 and rep["dropped_paths"] == ["tests/test_mod.py"] and r["outcome_code"] == 3

def test_regression_zeroes_partial_reward(tmp_path):
    pristine = _pristine(tmp_path); tree = tmp_path / "tree"; shutil.copytree(pristine, tree)
    (tree / "pkg" / "mod.py").write_text("def f(x):\n    return 4\n")   # new passes, old regresses
    grader.grade(_cfg(tmp_path, pristine, _artifacts(tmp_path, tree)))
    r = json.loads((tmp_path / "verifier_out" / "reward.json").read_text())
    assert r["f2p_passed"] == 1 and r["p2p_passed"] == 0 and r["reward"] == 0.0 and r["success"] == 0 and r["outcome_code"] == 2

def test_missing_receipt_is_collection_failure(tmp_path):
    pristine = _pristine(tmp_path); a = tmp_path / "artifacts"; a.mkdir()
    grader.grade(_cfg(tmp_path, pristine, a))
    r = json.loads((tmp_path / "verifier_out" / "reward.json").read_text()); assert r["outcome_code"] == 5 and r["reward"] == 0

def test_reward_json_is_numeric_only(tmp_path):
    pristine = _pristine(tmp_path); tree = tmp_path / "tree"; shutil.copytree(pristine, tree)
    grader.grade(_cfg(tmp_path, pristine, _artifacts(tmp_path, tree)))
    r = json.loads((tmp_path / "verifier_out" / "reward.json").read_text())
    assert all(isinstance(v, (int, float)) for v in r.values())

def test_nop_records_real_base_statuses(tmp_path):
    p=_pristine(tmp_path); result=grader.grade(_cfg(tmp_path,p,_artifacts(tmp_path,p)))
    assert {x["id"]:x["observed"] for x in result.report["per_test"]} == {
        "tests/test_mod.py::test_new":"failed", "tests/test_mod.py::test_old":"passed"}

def test_receipt_nonzero_is_not_validated(tmp_path):
    p=_pristine(tmp_path); a=_artifacts(tmp_path,p)
    receipt=json.loads((a/"collect.status").read_text()); receipt["rc"]=2
    (a/"collect.status").write_text(json.dumps(receipt))
    assert grader.grade(_cfg(tmp_path,p,a)).reward["outcome_code"] == 5

def test_hash_includes_contents(tmp_path):
    from swesim.verifier import policy
    p=_pristine(tmp_path); t=tmp_path/"tree"; shutil.copytree(p,t)
    f=t/"pkg/mod.py"; f.write_text("x=1"); cs=policy.change_set(p,t); a=policy.change_hash(p,t,cs)
    f.write_text("x=2"); assert a!=policy.change_hash(p,t,cs)

# ---- additional coverage beyond the brief ----

REQUIRED_REPORT_KEYS = {"outcome", "outcome_code", "per_test", "per_group", "patch_sha256", "transcript_hash",
                        "reviewed_diff_sha256", "dropped_paths", "integrity_signals", "rejected_members"}

def _reward(tmp_path): return json.loads((tmp_path / "verifier_out" / "reward.json").read_text())
def _report(tmp_path): return json.loads((tmp_path / "verifier_out" / "report.json").read_text())

def _fixed_tree(tmp_path, pristine):
    tree = tmp_path / "tree"; shutil.copytree(pristine, tree)
    (tree / "pkg" / "mod.py").write_text("def f(x):\n    return x * 2\n"); return tree

def test_sha_mismatch_is_collection_failure(tmp_path):
    p = _pristine(tmp_path); a = _artifacts(tmp_path, _fixed_tree(tmp_path, p))
    receipt = json.loads((a / "collect.status").read_text()); receipt["sha256"] = "0" * 64
    (a / "collect.status").write_text(json.dumps(receipt))
    res = grader.grade(_cfg(tmp_path, p, a))
    assert res.reward["outcome_code"] == 5 and res.report["outcome"] == "collection-failure" and res.report["per_test"] == []

def test_non_root_collector_is_collection_failure(tmp_path):
    p = _pristine(tmp_path); a = _artifacts(tmp_path, _fixed_tree(tmp_path, p))
    receipt = json.loads((a / "collect.status").read_text()); receipt["user"] = "agent"
    (a / "collect.status").write_text(json.dumps(receipt))
    assert grader.grade(_cfg(tmp_path, p, a)).reward["outcome_code"] == 5

def test_oversized_and_invalid_tree_receipts_are_agent_invalid_submission(tmp_path):
    p = _pristine(tmp_path); a = tmp_path / "artifacts"; a.mkdir()
    for status in ("oversized", "invalid-tree"):
        (a / "collect.status").write_text(json.dumps({"status": status, "rc": 1, "user": "root", "transcript_hash": "ab" * 32}))
        res = grader.grade(_cfg(tmp_path, p, a))
        assert res.reward["outcome_code"] == 3 and res.reward["reward"] == 0 and res.report["transcript_hash"] == "ab" * 32

def test_report_always_has_required_keys(tmp_path):
    p = _pristine(tmp_path)
    a = tmp_path / "artifacts"; a.mkdir()
    grader.grade(_cfg(tmp_path, p, a)); assert REQUIRED_REPORT_KEYS <= set(_report(tmp_path))            # collection failure
    (a / "collect.status").write_text(json.dumps({"status": "oversized", "rc": 1, "user": "root"}))
    grader.grade(_cfg(tmp_path, p, a)); assert REQUIRED_REPORT_KEYS <= set(_report(tmp_path))            # invalid submission
    grader.grade(_cfg(tmp_path, p, _artifacts(tmp_path, _fixed_tree(tmp_path, p))))
    rep = _report(tmp_path); assert REQUIRED_REPORT_KEYS <= set(rep) and rep["outcome"] == "valid-correct" and rep["outcome_code"] == 1
    assert rep["per_group"] == [{"id": "g01", "weight": 1.0, "passed": True}]
    assert rep["reviewed_diff_sha256"] is None and len(rep["patch_sha256"]) == 64

def test_symlink_member_is_rejected_and_signaled_but_source_still_graded(tmp_path):
    p = _pristine(tmp_path); tree = _fixed_tree(tmp_path, p)
    a = tmp_path / "artifacts"; a.mkdir(); tar = a / "app.tar"
    with tarfile.open(tar, "w") as t:
        t.add(tree, arcname=".")
        ti = tarfile.TarInfo("./pkg/secret"); ti.type = tarfile.SYMTYPE; ti.linkname = "/tests/test.patch"; t.addfile(ti)
    (a / "collect.status").write_text(json.dumps({"status": "ok", "rc": 0, "sha256": grader.sha256_file(tar), "user": "root",
                                                "base_manifest_sha256": grader.sha256_file(_base_manifest(tmp_path))}))
    res = grader.grade(_cfg(tmp_path, p, a))
    assert res.reward["outcome_code"] == 1 and res.reward["integrity_signal_count"] == 2
    assert res.report["rejected_members"] == [["./pkg/secret", "non-regular"]]
    assert [s["kind"] for s in res.report["integrity_signals"]] == ["rejected-archive-member", "tests-ran-as-grader"]
    assert res.report["integrity_signals"][0] == {"kind": "rejected-archive-member", "path": "./pkg/secret", "reason": "non-regular"}
    assert not (tmp_path / "work" / "repo" / "pkg" / "secret").exists()

def test_tests_ran_as_is_recorded_and_signaled_without_test_user(tmp_path):
    import pwd
    p = _pristine(tmp_path); res = grader.grade(_cfg(tmp_path, p, _artifacts(tmp_path, _fixed_tree(tmp_path, p))))
    me = pwd.getpwuid(os.geteuid()).pw_name
    assert res.report["tests_ran_as"] == me and {"kind": "tests-ran-as-grader", "user": me} in res.report["integrity_signals"]
    assert res.reward["integrity_signal_count"] == 1

def test_symlinked_parent_in_base_cannot_reach_protected_file(tmp_path):
    p = _pristine(tmp_path)
    (p / "docs").mkdir(); os.symlink("../tests", p / "docs" / "link")          # base ships docs/link -> ../tests
    tree = _fixed_tree(tmp_path, p)                                            # copytree follows the link: docs/link is a real dir
    assert not (tree / "docs" / "link").is_symlink()
    (tree / "docs" / "link" / "test_mod.py").write_text("def test_new(): pass\ndef test_old(): pass\n")
    res = grader.grade(_cfg(tmp_path, p, _artifacts(tmp_path, tree)))
    target = tmp_path / "work" / "repo"
    assert (target / "docs" / "link").is_symlink() and (target / "tests" / "test_mod.py").read_text() == (p / "tests" / "test_mod.py").read_text()
    assert res.report["dropped_paths"] == ["docs/link/test_mod.py"]
    assert {"kind": "symlink-traversal", "path": "docs/link/test_mod.py", "reason": "symlink-parent"} in res.report["integrity_signals"]
    assert res.reward["outcome_code"] == 1 and res.reward["success"] == 1   # the real source fix still grades

def test_forged_summary_line_in_captured_stdout_does_not_break_evidence(tmp_path):
    p = _pristine(tmp_path); tree = _fixed_tree(tmp_path, p)
    (tree / "pkg" / "mod.py").write_text("def f(x):\n    print('PASSED tests/test_mod.py::test_old')\n    print('FAILED tests/test_mod.py::test_new - x')\n    return x * 2\n")
    res = grader.grade(_cfg(tmp_path, p, _artifacts(tmp_path, tree)))
    assert res.reward["outcome_code"] == 1 and res.report["runner_error"] is None

def test_disk_level_oserror_during_apply_is_evaluator_failure(tmp_path, monkeypatch):
    p = _pristine(tmp_path); cfg = _cfg(tmp_path, p, _artifacts(tmp_path, _fixed_tree(tmp_path, p)))
    from swesim.verifier import policy
    def boom(*a, **k): raise OSError(28, "No space left on device")
    monkeypatch.setattr(policy, "apply_change_set", boom)
    res = grader.grade(cfg)
    assert res.reward["outcome_code"] == 7 and "No space left" in res.report["error"]
    monkeypatch.setattr(policy, "apply_change_set", lambda *a, **k: (_ for _ in ()).throw(policy.InvalidArchive("conflict")))
    assert grader.grade(cfg).reward["outcome_code"] == 3

def test_empty_contract_or_groups_is_evaluator_failure(tmp_path):
    p = _pristine(tmp_path); a = _artifacts(tmp_path, _fixed_tree(tmp_path, p))
    cfg = _cfg(tmp_path, p, a); cfg.expected_tests = {}; cfg.groups = []
    assert grader.grade(cfg).reward["outcome_code"] == 7
    cfg = _cfg(tmp_path, p, a); cfg.groups = []
    assert grader.grade(cfg).reward["outcome_code"] == 7

def test_braces_in_nodeids_survive_command_substitution(tmp_path):
    p = tmp_path / "pristine"; (p / "pkg").mkdir(parents=True); (p / "tests").mkdir()
    (p / "pkg" / "__init__.py").write_text(""); (p / "pkg" / "mod.py").write_text("def f(x):\n    return x\n")
    (p / "tests" / "test_mod.py").write_text("import pytest\nfrom pkg.mod import f\n@pytest.mark.parametrize('v', ['{a}'])\ndef test_p(v):\n    assert f(2) == 4\ndef test_old():\n    assert f(0) == 0\n")
    tree = tmp_path / "tree"; shutil.copytree(p, tree); (tree / "pkg" / "mod.py").write_text("def f(x):\n    return x * 2\n")
    cfg = _cfg(tmp_path, p, _artifacts(tmp_path, tree))
    cfg.expected_tests = {"tests/test_mod.py::test_p[{a}]": "f2p", "tests/test_mod.py::test_old": "p2p"}
    cfg.groups = [{"id": "g01", "weight": 1.0, "tests": ["tests/test_mod.py::test_p[{a}]"]}]
    assert grader.grade(cfg).reward["outcome_code"] == 1
    # force the single_command path: the full run collects one id while two are expected
    cfg.runner["command"] = cfg.runner["command"].replace(" tests", " tests/test_mod.py::test_old")
    res = grader.grade(cfg)
    assert res.reward["outcome_code"] == 4 and res.report["reruns"][0]["statuses"] == {"tests/test_mod.py::test_p[{a}]": "passed"}

def test_unbounded_output_is_capped_killed_and_coded_runtime_failure(tmp_path, monkeypatch):
    import time
    p = _pristine(tmp_path); cfg = _cfg(tmp_path, p, _artifacts(tmp_path, _fixed_tree(tmp_path, p)))
    monkeypatch.setattr(grader, "MAX_OUTPUT_BYTES", 256 * 1024)
    (tmp_path / "flood.py").write_text("import os, sys\nopen(sys.argv[1] + '.pid', 'w').write(str(os.getpid()))\n"
                                       "while True:\n    sys.stdout.write('x' * 65536); sys.stdout.flush()\n")
    cfg.runner["command"] = sys.executable + " " + str(tmp_path / "flood.py") + " {junit_path}"; cfg.runner["single_command"] = ""
    cfg.test_timeout_sec = 60
    t0 = time.monotonic(); res = grader.grade(cfg); elapsed = time.monotonic() - t0
    assert res.reward["outcome_code"] == 4 and res.reward["reward"] == 0 and res.reward["success"] == 0
    assert res.report["runner_error"].startswith("output-cap-exceeded")
    sig = [s for s in res.report["integrity_signals"] if s["kind"] == "output-cap-exceeded"]
    assert len(sig) == 1 and sig[0]["bytes"] > 256 * 1024 and sig[0]["cap"] == 256 * 1024
    assert res.report["runner_output_bytes"] == sig[0]["bytes"] and res.reward["integrity_signal_count"] == 2
    assert len(res.report["runner_tail"]) <= 4000
    assert (tmp_path / "verifier_out" / "runner_stdout.txt").stat().st_size <= 256 * 1024 + 65536   # nothing past the cap is kept
    assert elapsed < 30, elapsed                                                                      # killed, not run to the timeout
    pid = int(next((tmp_path / "work").glob("results-*/junit.xml.pid")).read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("flooding test process %d still alive" % pid)

def test_normal_run_under_cap_is_unchanged(tmp_path):
    p = _pristine(tmp_path); res = grader.grade(_cfg(tmp_path, p, _artifacts(tmp_path, _fixed_tree(tmp_path, p))))
    assert res.reward["outcome_code"] == 1 and res.reward["success"] == 1 and res.report["runner_error"] is None
    assert 0 < res.report["runner_output_bytes"] < grader.MAX_OUTPUT_BYTES
    assert not [s for s in res.report["integrity_signals"] if s["kind"] == "output-cap-exceeded"]
    assert "short test summary info" in (tmp_path / "verifier_out" / "runner_stdout.txt").read_text()

def test_patch_paths_handles_git_quoted_names(tmp_path):
    patch = tmp_path / "t.patch"
    patch.write_text('diff --git "a/tests/da ta.txt" "b/tests/da ta.txt"\n--- "a/tests/da ta.txt"\n+++ "b/tests/da ta.txt"\n@@ -1 +1 @@\n-x\n+y\n'
                     'diff --git a/tests/plain.py b/tests/plain.py\n--- a/tests/plain.py\t2020\n+++ b/tests/plain.py\n'
                     '--- /dev/null\n+++ "b/tests/caf\\303\\251.py"\nrename from old/name.py\nrename to new/name.py\n')
    assert grader._patch_paths(patch) == ["new/name.py", "old/name.py", "tests/café.py", "tests/da ta.txt", "tests/plain.py"]

def test_submission_diff_marks_missing_trailing_newline(tmp_path):
    p = _pristine(tmp_path); tree = tmp_path / "tree"; shutil.copytree(p, tree)
    (tree / "pkg" / "mod.py").write_text("def f(x):\n    return x * 2")   # no trailing newline
    grader.grade(_cfg(tmp_path, p, _artifacts(tmp_path, tree)))
    diff = (tmp_path / "verifier_out" / "submission.diff").read_text()
    assert "+    return x * 2\n\\ No newline at end of file\n" in diff and diff.count("\\ No newline") == 1

def test_corrupt_archive_is_agent_invalid_submission(tmp_path):
    p = _pristine(tmp_path); a = tmp_path / "artifacts"; a.mkdir()
    (a / "app.tar").write_bytes(b"not a tar archive at all" * 40)
    (a / "collect.status").write_text(json.dumps({"status": "ok", "rc": 0, "sha256": grader.sha256_file(a / "app.tar"), "user": "root",
                                                "base_manifest_sha256": grader.sha256_file(_base_manifest(tmp_path))}))
    res = grader.grade(_cfg(tmp_path, p, a))
    assert res.reward["outcome_code"] == 3 and "invalid_tree" in res.report

def test_evidence_mismatch_is_runtime_failure_with_diagnostic_reruns(tmp_path):
    p = _pristine(tmp_path); cfg = _cfg(tmp_path, p, _artifacts(tmp_path, _fixed_tree(tmp_path, p)))
    # runner selects one test while the manifest expects two: collection differs -> no credit, reruns as diagnostics only
    cfg.runner["command"] = cfg.runner["command"].replace(" tests", " tests/test_mod.py::test_new")
    res = grader.grade(cfg)
    assert res.reward["outcome_code"] == 4 and res.reward["reward"] == 0 and res.reward["success"] == 0
    assert "collection differs" in res.report["runner_error"]
    assert {x["id"]: x["observed"] for x in res.report["per_test"]} == {"tests/test_mod.py::test_new": "unknown", "tests/test_mod.py::test_old": "unknown"}
    assert res.report["per_group"] == [{"id": "g01", "weight": 1.0, "passed": False}]
    assert len(res.report["reruns"]) == 1 and res.report["reruns"][0]["id"] == "tests/test_mod.py::test_new"
    assert res.report["reruns"][0]["statuses"] == {"tests/test_mod.py::test_new": "passed"} and res.report["reruns"][0]["error"] is None

def test_timeout_is_runtime_failure(tmp_path):
    p = _pristine(tmp_path); cfg = _cfg(tmp_path, p, _artifacts(tmp_path, _fixed_tree(tmp_path, p)))
    cfg.runner["command"] = sys.executable + " -c 'import time; time.sleep(30)' {junit_path}"; cfg.runner["single_command"] = ""
    cfg.test_timeout_sec = 1
    res = grader.grade(cfg)
    assert res.reward["outcome_code"] == 4 and res.report["runner_error"] == "timeout" and res.report["reruns"] == []

def test_nodeid_map_translates_historical_ids(tmp_path):
    p = _pristine(tmp_path); cfg = _cfg(tmp_path, p, _artifacts(tmp_path, _fixed_tree(tmp_path, p)))
    cfg.expected_tests = {"hist::new": "f2p", "hist::old": "p2p"}
    cfg.groups = [{"id": "g01", "weight": 1.0, "tests": ["hist::new"]}]
    cfg.runner["nodeid_map"] = {"hist::new": "tests/test_mod.py::test_new", "hist::old": "tests/test_mod.py::test_old"}
    res = grader.grade(cfg)
    assert res.reward["outcome_code"] == 1 and [x["id"] for x in res.report["per_test"]] == ["hist::new", "hist::old"]
    assert all(x["observed"] == "passed" for x in res.report["per_test"])

def test_non_bijective_nodeid_map_is_evaluator_failure(tmp_path):
    p = _pristine(tmp_path); cfg = _cfg(tmp_path, p, _artifacts(tmp_path, _fixed_tree(tmp_path, p)))
    cfg.runner["nodeid_map"] = {"tests/test_mod.py::test_new": "tests/test_mod.py::test_old"}
    res = grader.grade(cfg)
    assert res.reward["outcome_code"] == 7 and res.report["outcome"] == "evaluator-failure" and "runner_exit_code" not in res.report

def test_unsupported_contract_id_or_group_is_evaluator_failure(tmp_path):
    p = _pristine(tmp_path); a = _artifacts(tmp_path, _fixed_tree(tmp_path, p)); cfg = _cfg(tmp_path, p, a)
    cfg.expected_tests = {"README.rst::README.rst": "f2p"}
    assert grader.grade(cfg).reward["outcome_code"] == 7
    cfg = _cfg(tmp_path, p, a)
    cfg.groups = [{"id": "g01", "weight": 1.0, "tests": ["tests/test_mod.py::missing"]}]
    assert grader.grade(cfg).reward["outcome_code"] == 7
    cfg = _cfg(tmp_path, p, a)
    cfg.runner["command"] = cfg.runner["command"].replace("{junit_path}", "junit.xml")
    assert grader.grade(cfg).reward["outcome_code"] == 7
    cfg = _cfg(tmp_path, p, a); cfg.target_dir = p     # would let apply modify the untouched base
    assert grader.grade(cfg).reward["outcome_code"] == 7 and (p / "pkg" / "mod.py").read_text() == "def f(x):\n    return x\n"

def _pristine_two_features(tmp_path):
    p = tmp_path / "pristine"; (p / "pkg").mkdir(parents=True); (p / "tests").mkdir()
    (p / "pkg" / "__init__.py").write_text("")
    (p / "pkg" / "mod.py").write_text("def f(x):\n    return x\n\ndef g(x):\n    return x\n")
    (p / "tests" / "test_mod.py").write_text("from pkg.mod import f, g\ndef test_new():\n    assert f(2) == 4\ndef test_new2():\n    assert g(1) == 'one'\ndef test_old():\n    assert f(0) == 0\n")
    return p

def test_partial_reward_by_groups_without_regression(tmp_path):
    p = _pristine_two_features(tmp_path); tree = tmp_path / "tree"; shutil.copytree(p, tree)
    (tree / "pkg" / "mod.py").write_text("def f(x):\n    return x * 2\n\ndef g(x):\n    return x\n")
    cfg = _cfg(tmp_path, p, _artifacts(tmp_path, tree))
    cfg.expected_tests = {"tests/test_mod.py::test_new": "f2p", "tests/test_mod.py::test_new2": "f2p", "tests/test_mod.py::test_old": "p2p"}
    cfg.groups = [{"id": "g01", "weight": 0.6, "tests": ["tests/test_mod.py::test_new"]}, {"id": "g02", "weight": 0.4, "tests": ["tests/test_mod.py::test_new2"]}]
    res = grader.grade(cfg)
    r = res.reward
    assert r["outcome_code"] == 2 and r["success"] == 0 and r["reward"] == 0.6 and r["f2p_passed"] == 1 and r["f2p_expected"] == 2
    assert r["p2p_passed"] == 1 and r["groups_passed"] == 1 and r["groups_total"] == 2 and r["patch_applied"] == 1
    assert [g["passed"] for g in res.report["per_group"]] == [True, False]

def _unified(pristine, tree, rel):
    import difflib
    old = (pristine / rel).read_text().splitlines(keepends=True) if (pristine / rel).exists() else []
    new = (tree / rel).read_text().splitlines(keepends=True)
    return "".join(difflib.unified_diff(old, new, fromfile="a/" + rel if old else "/dev/null", tofile="b/" + rel))

@pytest.mark.skipif(not shutil.which("git"), reason="git required for contract patch overlay")
def test_contract_patch_overlays_hidden_tests_after_source_changes(tmp_path):
    p = _pristine(tmp_path)
    (p / "tests" / "test_mod.py").write_text("from pkg.mod import f\ndef test_old():\n    assert f(0) == 0\n")   # base lacks test_new
    hidden = tmp_path / "hidden"; shutil.copytree(p, hidden)
    (hidden / "tests" / "test_mod.py").write_text("from pkg.mod import f\ndef test_new():\n    assert f(2) == 4\ndef test_old():\n    assert f(0) == 0\n")
    (hidden / "tests" / "data.txt").write_text("fixture\n")
    patch = tmp_path / "test.patch"; patch.write_text(_unified(p, hidden, "tests/test_mod.py") + _unified(p, hidden, "tests/data.txt"))
    tree = _fixed_tree(tmp_path, p)
    (tree / "tests" / "data.txt").write_text("agent wrote this first\n")   # touched by the contract patch -> protected even without a glob
    cfg = _cfg(tmp_path, p, _artifacts(tmp_path, tree)); cfg.contract_patch = patch; cfg.protected = ["**/conftest.py"]
    res = grader.grade(cfg)
    assert res.reward["outcome_code"] == 1 and res.reward["success"] == 1
    assert res.report["dropped_paths"] == ["tests/data.txt"] and res.report["contract_patch_applied"] == 1
    assert (tmp_path / "work" / "repo" / "tests" / "data.txt").read_text() == "fixture\n"
    # the submission diff was taken against the untouched base before the overlay
    diff = (tmp_path / "verifier_out" / "submission.diff").read_text()
    assert "pkg/mod.py" in diff and "test_new" not in diff

def test_empty_contract_patch_is_skipped(tmp_path):
    p = _pristine(tmp_path); cfg = _cfg(tmp_path, p, _artifacts(tmp_path, _fixed_tree(tmp_path, p)))
    cfg.contract_patch = tmp_path / "empty.patch"; cfg.contract_patch.write_text("\n")
    res = grader.grade(cfg)
    assert res.reward["outcome_code"] == 1 and res.report["contract_patch_applied"] == 0

def test_missing_contract_patch_is_evaluator_failure(tmp_path):
    p = _pristine(tmp_path); cfg = _cfg(tmp_path, p, _artifacts(tmp_path, _fixed_tree(tmp_path, p)))
    cfg.contract_patch = tmp_path / "does-not-exist.patch"
    assert grader.grade(cfg).reward["outcome_code"] == 7

def test_target_dir_is_used_in_place(tmp_path):
    p = _pristine(tmp_path); target = tmp_path / "testbed"; shutil.copytree(p, target)
    (target / ".git").mkdir(); (target / ".git" / "HEAD").write_text("ref: refs/heads/main\n")   # runtime tree extras survive
    cfg = _cfg(tmp_path, p, _artifacts(tmp_path, _fixed_tree(tmp_path, p))); cfg.target_dir = target
    res = grader.grade(cfg)
    assert res.reward["outcome_code"] == 1 and (target / "pkg" / "mod.py").read_text() == "def f(x):\n    return x * 2\n"
    assert (target / ".git" / "HEAD").exists() and not (tmp_path / "work" / "repo").exists()

def test_freeze_failure_is_signaled_without_changing_outcome(tmp_path):
    p = _pristine(tmp_path); a = _artifacts(tmp_path, _fixed_tree(tmp_path, p))
    receipt = json.loads((a / "collect.status").read_text()); receipt.update(freeze_status="failed", transcript_hash=None)
    (a / "collect.status").write_text(json.dumps(receipt))
    res = grader.grade(_cfg(tmp_path, p, a))
    assert res.reward["outcome_code"] == 1 and res.report["transcript_hash"] is None
    assert {"kind": "freeze-failed"} in res.report["integrity_signals"] and res.reward["integrity_signal_count"] == 2

def test_submission_diff_and_hashes_recorded(tmp_path):
    p = _pristine(tmp_path); tree = _fixed_tree(tmp_path, p)
    (tree / "tests" / "test_mod.py").write_text("def test_new(): pass\ndef test_old(): pass\n")   # dropped, but part of the submission hash
    res = grader.grade(_cfg(tmp_path, p, _artifacts(tmp_path, tree)))
    diff = (tmp_path / "verifier_out" / "submission.diff").read_text()
    assert "+++ b/pkg/mod.py" in diff and "test_mod" not in diff
    assert res.report["patch_sha256"] != res.report["submission_sha256"] and len(res.report["submission_sha256"]) == 64

def test_main_script_mode_with_config_under_isolated_interpreter(tmp_path):
    p = _pristine(tmp_path); a = _artifacts(tmp_path, _fixed_tree(tmp_path, p)); cfg = _cfg(tmp_path, p, a)
    from dataclasses import asdict
    d = {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(cfg).items()}
    (tmp_path / "grader_config.json").write_text(json.dumps(d))
    proc = subprocess.run([sys.executable, "-I", grader.__file__, "--config", str(tmp_path / "grader_config.json")],
                          cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _reward(tmp_path)["outcome_code"] == 1 and _report(tmp_path)["outcome"] == "valid-correct"

def test_main_writes_evaluator_failure_on_unexpected_error(tmp_path):
    p = _pristine(tmp_path); a = _artifacts(tmp_path, _fixed_tree(tmp_path, p)); cfg = _cfg(tmp_path, p, a)
    from dataclasses import asdict
    d = {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(cfg).items()}
    d["groups"] = "not-a-list"
    (tmp_path / "grader_config.json").write_text(json.dumps(d))
    proc = subprocess.run([sys.executable, "-I", grader.__file__, "--config", str(tmp_path / "grader_config.json")],
                          cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    r = _reward(tmp_path); rep = _report(tmp_path)
    assert r["outcome_code"] == 7 and r["reward"] == 0 and rep["outcome"] == "evaluator-failure" and REQUIRED_REPORT_KEYS <= set(rep)

def test_launcher_writes_manifest_and_junit(tmp_path):
    p = _pristine(tmp_path); ids = tmp_path / "ids.json"; junit = tmp_path / "j.xml"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "SWESIM_NODEIDS": str(ids)}
    proc = subprocess.run([sys.executable, "-I", str(Path(grader.__file__).with_name("run_pytest.py")), "-rA", "-p", "no:cacheprovider",
                           "--junitxml=" + str(junit), "tests"], cwd=p, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1
    assert json.loads(ids.read_text()) == ["tests/test_mod.py::test_new", "tests/test_mod.py::test_old"]
    from swesim.verifier import parsers
    assert parsers.reconcile(proc.stdout, junit, json.loads(ids.read_text()), proc.returncode) == {
        "tests/test_mod.py::test_new": "failed", "tests/test_mod.py::test_old": "passed"}
