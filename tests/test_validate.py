"""Validate stage expectation logic and harbor_cli wrapper. Offline: no Harbor process is started."""
import json, os, shutil, subprocess, sys
from pathlib import Path

import pytest

from swesim import harbor_cli
from swesim.models import ScenarioSpec, RunConfig, minimal_spec_dict
from swesim.paths import Workspace
from swesim.stages import validate
from swesim.stages.compile import CompileResult
from swesim.stages.validate import evaluate_controls, gold_lines, gold_probes, is_distinctive


@pytest.fixture(autouse=True)
def stub_compiled_manifest(monkeypatch):
    # Validation orchestration uses placeholder compile results; compiler integrity has its own behavioral tests.
    monkeypatch.setattr(validate, "verify_compiled_artifacts", lambda task, record, condition: None)


def test_validate_rejects_unbound_compilation_before_harbor(tmp_path, monkeypatch):
    from swesim.stages.compile import verify_compiled_artifacts
    ws, spec = _spec_ws(tmp_path)
    monkeypatch.setattr(validate, "compile_scenario", _fake_compile(tmp_path, spec))
    monkeypatch.setattr(validate, "verify_compiled_artifacts", verify_compiled_artifacts)
    calls = []
    monkeypatch.setattr(harbor_cli, "run_task", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="recompile"):
        validate.validate_scenario(ws, spec, RunConfig.default("full"), "")
    assert calls == []


def test_expectations():
    results = {"nop": {"reward": 0, "success": 0, "outcome_code": 3}, "oracle": {"reward": 1.0, "success": 1, "outcome_code": 1},
               "plausible-wrong:keyerror": {"reward": 0.0, "success": 0, "outcome_code": 2},
               "tamper:root-conftest": {"reward": 0.0, "success": 0, "outcome_code": 2}}
    spec_controls = {"plausible_wrong": [{"name": "keyerror", "expected_success": 0, "expected_reward_max": 0.5}],
                     "alternative_correct": [], "tamper": [{"name": "root-conftest", "cls": "incorrect-exploit", "expected_reward": 0.0}]}
    ok, failures = evaluate_controls(results, spec_controls)
    assert ok and failures == []
    results["tamper:root-conftest"]["reward"] = 1.0; results["tamper:root-conftest"]["success"] = 1
    ok, failures = evaluate_controls(results, spec_controls)
    assert not ok and "tamper:root-conftest" in failures[0]


def test_expectations_edge_cases():
    controls = {"plausible_wrong": [{"name": "k", "expected_success": 0, "expected_reward_max": 0.0}], "alternative_correct": [{"name": "alt"}],
                "tamper": [{"name": "t", "cls": "incorrect-exploit"}, {"name": "benign", "cls": "benign-edit"}]}
    ok, failures = evaluate_controls({"nop": {}, "oracle": {}}, controls)
    assert not ok and failures[0].startswith("missing control results") and "alt-correct:alt" in failures[0]
    good = {"nop": {"reward": 0, "success": 0, "outcome_code": 3}, "oracle": {"reward": 1, "success": 1, "outcome_code": 1},
            "plausible-wrong:k": {"reward": 0, "success": 0, "outcome_code": 2}, "alt-correct:alt": {"reward": 1, "success": 1, "outcome_code": 1},
            "tamper:t": {"reward": 0, "success": 0, "outcome_code": 2}, "tamper:benign": {"reward": 1, "success": 1, "outcome_code": 1}}
    assert evaluate_controls(good, controls) == (True, [])
    infra = json.loads(json.dumps(good)); infra["tamper:t"] = {"reward": 0, "success": 0, "outcome_code": 5}
    ok, failures = evaluate_controls(infra, controls)
    assert not ok and any("infrastructure" in f and "tamper:t" in f for f in failures)  # a collection failure is not a defeated attack
    bad = json.loads(json.dumps(good)); bad["plausible-wrong:k"]["reward"] = 0.3; bad["tamper:benign"]["success"] = 0; bad["alt-correct:alt"]["success"] = 0
    ok, failures = evaluate_controls(bad, controls)
    assert not ok and len(failures) == 3
    nonnum = json.loads(json.dumps(good)); nonnum["oracle"]["reward"] = "1"
    ok, failures = evaluate_controls(nonnum, controls)
    assert not ok and any("numeric" in f for f in failures)
    partial_alt = json.loads(json.dumps(good)); partial_alt["alt-correct:alt"]["reward"] = 0.5
    ok, failures = evaluate_controls(partial_alt, controls)
    assert not ok and failures == ["alt-correct:alt: reward 0.5 != 1"]  # an alternative correct fix must earn full credit
    declared = json.loads(json.dumps(controls)); declared["tamper"][1]["expected_reward"] = 1.0; declared["plausible_wrong"][0]["expected_reward"] = 0.0
    assert evaluate_controls(good, declared) == (True, [])
    off = json.loads(json.dumps(good)); off["tamper:benign"]["reward"] = 0.5; off["plausible-wrong:k"]["reward"] = 0.0
    ok, failures = evaluate_controls(off, declared)
    assert not ok and failures == ["tamper:benign: reward 0.5 != expected 1.0"]


def test_gold_probes_select_distinctive_lines_and_pair_removed():
    diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,4 +1,5 @@\n import os\n-from .exceptions import TooManyRedirects, InvalidSchema\n+from .exceptions import (\n"
            "+    TooManyRedirects, InvalidSchema, ChunkedEncodingError, ContentDecodingError)\n+# a comment that is certainly longer than thirty characters\n"
            "+    return self.build_response_for_request(request, resp)\n+    third_distinctive_added_line_in_same_hunk = compute(request)\n"
            "@@ -20,3 +21,5 @@\n context\n+short = 1\n+    if not stream and response.content is not None:\n \n"
            "--- a/y.py\n+++ b/y.py\n@@ -1 +1,2 @@\n-    old_value = compute(a)\n-)\n-\n+    new_value = compute_with_dimension(a, dim)\n")
    probes = gold_probes(diff)
    assert [p["line"] for p in probes] == ["TooManyRedirects, InvalidSchema, ChunkedEncodingError, ContentDecodingError)",
                                            "return self.build_response_for_request(request, resp)",  # at most two probes per hunk
                                            "if not stream and response.content is not None:", "new_value = compute_with_dimension(a, dim)"]
    assert probes[0] == {"line": probes[0]["line"], "removed": ["from .exceptions import TooManyRedirects, InvalidSchema"], "path": "x.py", "new": False}
    assert probes[2]["removed"] == [] and probes[2]["path"] == "x.py"  # pure-addition hunk
    assert probes[3]["removed"] == ["old_value = compute(a)"] and probes[3]["path"] == "y.py"  # blank / punctuation-only removed lines carry no signal
    assert gold_lines(diff) == [p["line"] for p in probes] and gold_probes("") == []
    # removed lines need an identifier and >= 8 chars (else `else:`-style lines suppress real hits); none left -> pure-addition rule
    short = gold_probes("--- a/z.py\n+++ b/z.py\n@@ -1,3 +1,3 @@\n-else:\n-x = 1\n-    return r\n+    handled = dispatch_to_dimension_handler(value)\n")
    assert short == [{"line": "handled = dispatch_to_dimension_handler(value)", "removed": ["return r"], "path": "z.py", "new": False}]
    # a removed line that is a substring of the added line is kept: it distinguishes base copies under whole-line matching
    sub = gold_probes("--- a/s.py\n+++ b/s.py\n@@ -1 +1 @@\n-    result = compute_value(payload)\n+    result = compute_value(payload) + dimension_offset(dim)\n")
    assert sub[0]["removed"] == ["result = compute_value(payload)"]
    # new file: pure addition at a path that cannot exist at base
    new = gold_probes("diff --git a/pkg/newmod.py b/pkg/newmod.py\n--- /dev/null\n+++ b/pkg/newmod.py\n@@ -0,0 +1,2 @@\n+    def brand_new_helper_for_dimensions(value, dim):\n+        return value\n")
    assert new == [{"line": "def brand_new_helper_for_dimensions(value, dim):", "removed": [], "path": "pkg/newmod.py", "new": True}]
    # `--- ` / `+++ ` inside a hunk are content unless they form a header pair
    sql = gold_probes("--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,2 @@\n--- legacy comment about the dimension column\n+++ plus\n+    select dimension_value from dimension_table;\n")
    assert sql == [{"line": "select dimension_value from dimension_table;", "removed": ["-- legacy comment about the dimension column"], "path": "q.sql", "new": False}]
    for bad in ("from .exceptions import (", "    return r", "    return None", "        pass", "# " + "x" * 40, '"""' + "d" * 40, "import os, sys, json, re, pathlib, shutil",
                "    ) + ((((((((((((((((((((((((((((((((", "", "   "):
        assert not is_distinctive(bad), bad
    assert is_distinctive("    raise ValueError('dimension mismatch for input')") and is_distinctive("    return self.build_response(request, resp)")


@pytest.mark.skipif(not all(shutil.which(x) for x in ("sh", "grep", "head")), reason="needs sh and grep")
def test_probe_filters_base_copies_on_fake_tree(tmp_path):
    from swesim.agents.patch_agent import build_probe_command, parse_probe_output
    diff = ("--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1,3 +1,3 @@\n import os\n-    result = compute_legacy_value(payload, strict=True)\n+    result = compute_modern_value(payload, strict=True, dim=dim)\n"
            "--- a/pkg/other.py\n+++ b/pkg/other.py\n@@ -1,2 +1,3 @@\n import os\n+    registry.register_default_handler(handler_for_dimension)\n"
            "--- a/pkg/third.py\n+++ b/pkg/third.py\n@@ -1,2 +1,3 @@\n import os\n+    registry.register_fallback_handler(handler_for_dimension)\n"
            "--- a/pkg/sub.py\n+++ b/pkg/sub.py\n@@ -1 +1 @@\n-    result = compute_value(payload)\n+    result = compute_value(payload) + dimension_offset(dim)\n"
            "--- /dev/null\n+++ b/pkg/newmod.py\n@@ -0,0 +1 @@\n+    def brand_new_helper_for_dimensions(value, dim):\n")
    probes = gold_probes(diff)
    assert [p["path"] for p in probes] == ["pkg/mod.py", "pkg/other.py", "pkg/third.py", "pkg/sub.py", "pkg/newmod.py"] and probes[4]["new"] is True
    added, removed, added1, added2, added3, removed3, added4 = (probes[0]["line"], probes[0]["removed"][0], probes[1]["line"], probes[2]["line"],
                                                              probes[3]["line"], probes[3]["removed"][0], probes[4]["line"])
    tree = tmp_path / "repo"
    files = {"pkg/mod.py": f"import os\n    {added}\n    {removed}\n",        # base path holding both: a base copy (re-added line), not a hit
             "build/lib/pkg/mod.py": f"    {added}\n    {removed}\n",          # base copy among build products: not a hit
             "stale/pkg/mod.py": f"    {added}\nother\n",                     # post-fix copy (removed lines absent): a hit
             "pkg/other.py": "import os\n",                                    # pure-addition hunk; the line is genuinely new at base
             "dist/pkg/other.py": f"    {added1}\n",                           # stale copy carrying the new line: a hit
             "pkg/third.py": f"    {added2}\n",                                # pure-addition hunk whose line already exists at base...
             "build/lib/pkg/third.py": f"    {added2}\n",                      # ...so no copy of it is evidence (PREEXISTS)
             "pkg/sub.py": f"    {removed3}\n",                                # base copy holds only the removed line: not even RAW
             "stale/pkg/sub.py": f"    {added3}\n",                            # added line contains the removed line as a substring: still a hit
             "pkg/newmod.py": f"    {added4}\n",                               # new file at its own path cannot be a base copy: a hit
             "build/lib/pkg/newmod.py": f"    {added4}\n",                     # copy of the new file elsewhere: a hit
             ".git/objects/leak.py": f"    {added}\n"}                         # never in scope
    for rel, content in files.items():
        (tree / rel).parent.mkdir(parents=True, exist_ok=True); (tree / rel).write_text(content)
    r = subprocess.run(["sh", "-c", build_probe_command(str(tree), probes)], capture_output=True, text=True)
    parsed = parse_probe_output(r.stdout, probes)
    assert parsed["hits_block_found"], r.stdout + r.stderr
    assert {(h["probe"], h["file"]) for h in parsed["raw_hits"]} == {(0, "./pkg/mod.py"), (0, "./build/lib/pkg/mod.py"), (0, "./stale/pkg/mod.py"),
                                                                     (1, "./dist/pkg/other.py"), (2, "./pkg/third.py"), (2, "./build/lib/pkg/third.py"),
                                                                     (3, "./stale/pkg/sub.py"), (4, "./pkg/newmod.py"), (4, "./build/lib/pkg/newmod.py")}
    assert {(h["probe"], h["file"]) for h in parsed["filtered_hits"]} == {(0, "./stale/pkg/mod.py"), (1, "./dist/pkg/other.py"), (3, "./stale/pkg/sub.py"),
                                                                          (4, "./pkg/newmod.py"), (4, "./build/lib/pkg/newmod.py")}
    assert parsed["preexisting_probes"] == [2]
    assert parsed["gold_line_hits"] == sorted(["./stale/pkg/mod.py", "./dist/pkg/other.py", "./stale/pkg/sub.py", "./pkg/newmod.py", "./build/lib/pkg/newmod.py"])
    # whole-line matching: a removed line present only as part of a longer line does not mark a base copy
    (tree / "stale/pkg/mod.py").write_text(f"    {added}\n    x{removed}x\n")
    parsed = parse_probe_output(subprocess.run(["sh", "-c", build_probe_command(str(tree), probes[:1])], capture_output=True, text=True).stdout, probes[:1])
    assert {h["file"] for h in parsed["filtered_hits"]} == {"./stale/pkg/mod.py"}


# ---- validate_scenario end to end with fake compile + fake harbor ------------------------------

def _spec_ws(tmp_path):
    ws = Workspace(tmp_path / "ws"); d = minimal_spec_dict(); spec = ScenarioSpec.model_validate(d)
    ws.scenario_dir(spec.id).mkdir(parents=True); return ws, spec


ORACLE_DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n import os\n-    def new_helper(value):\n+    def new_helper_for_dimension(value, dim):\n"


def _fake_compile(tmp_path, spec, oracle_diff=ORACLE_DIFF):
    task = tmp_path / "task-full"; (task / "controls").mkdir(parents=True); (task / "solution").mkdir()
    for c in spec.controls.plausible_wrong + spec.controls.alternative_correct + spec.controls.tamper: (task / "controls" / f"{c.name}.diff").write_text("diff")
    (task / "solution" / "oracle.diff").write_text(oracle_diff)
    cj = tmp_path / "compile.json"; cj.write_text(json.dumps({"compiled_hash": "c" * 64}))
    return lambda ws, s, rc, url: CompileResult({"full": task, "hidden": task, "interactive": task}, "sha256:" + "d" * 64, cj)


def _fake_harbor(jobs_root: Path, rewards: dict, probe_txt: str | None, exception_for=()):
    launched = []

    def run_task(task_dir, agent, jobs_dir, job_name, model=None, agent_kwargs=None, env_provider="modal", timeout_sec=10800, **kw):
        name = job_name.split("--", 1)[1]; launched.append((name, agent, agent_kwargs))
        td = jobs_dir / job_name / "trial"; (td / "verifier").mkdir(parents=True); (td / "agent").mkdir()
        (td / "result.json").write_text(json.dumps({"exception_info": {"exception_type": "X"} if name in exception_for else None}))
        if name == "probe":
            if probe_txt is not None: (td / "agent" / "probe.txt").write_text(probe_txt)
            return td
        key = {"nop": "nop", "oracle": "oracle"}.get(name, name)
        r = rewards[key]; (td / "verifier" / "reward.json").write_text(json.dumps({k: v for k, v in r.items() if k != "per_test"}))
        (td / "verifier" / "report.json").write_text(json.dumps({"per_test": r.get("per_test", [])}))
        return td
    return run_task, launched


GOOD = {"nop": {"reward": 0, "success": 0, "outcome_code": 3, "per_test": [{"id": "t1", "observed": "failed"}, {"id": "t0", "observed": "passed"}]},
        "oracle": {"reward": 1.0, "success": 1, "outcome_code": 1, "per_test": [{"id": "t1", "observed": "passed"}, {"id": "t0", "observed": "passed"}]},
        "plausible-wrong-keyerror": {"reward": 0.0, "success": 0, "outcome_code": 2},
        "tamper-root-conftest": {"reward": 0.0, "success": 0, "outcome_code": 2}}
CLEAN_PROBE = "REFS=1\nREFLOG=1\nGOLD_SHA_CHECKED=none\nHITS_BEGIN\nHITS_END\n"


def test_validate_scenario_valid(tmp_path, monkeypatch):
    ws, spec = _spec_ws(tmp_path)
    monkeypatch.setattr(validate, "compile_scenario", _fake_compile(tmp_path, spec))
    run_task, launched = _fake_harbor(ws.jobs_dir, GOOD, CLEAN_PROBE)
    monkeypatch.setattr(harbor_cli, "run_task", run_task)
    out = validate.validate_scenario(ws, spec, RunConfig.default("full"), "https://x.modal.run")
    assert out["status"] == "valid" and out["failures"] == []
    assert out["expected_base"] == {"t1": "failed", "t0": "passed"} and out["expected_gold"] == {"t1": "passed", "t0": "passed"}
    ctl = out["controls"]["tamper:root-conftest"]
    assert ctl["reward"] == 0.0 and ctl["success"] == 0 and ctl["outcome"] == "valid-incorrect" and ctl["expected"]["cls"] == "incorrect-exploit" and "report" not in ctl
    assert out["controls"]["nop"]["outcome"] == "agent-invalid-submission"
    assert out["probe"]["gold_hits"] == [] and out["probe"]["all_refs"] == 1 and out["probe"]["raw_hits"] == [] and out["probe"]["filtered_hits"] == []
    assert out["probe"]["probes"] == [{"line": "def new_helper_for_dimension(value, dim):", "removed": ["def new_helper(value):"], "path": "x.py", "new": False}]
    assert "note" not in out["probe"]
    assert set(out["trial_dirs"]) == {"nop", "oracle", "plausible-wrong:keyerror", "tamper:root-conftest", "probe"}
    assert out["compiled_hash"] == "c" * 64 and out["image_digest"] == "sha256:" + "d" * 64 and out["spec_hash"] == spec.spec_hash
    assert json.loads((ws.scenario_dir(spec.id) / "validation.json").read_text())["status"] == "valid"
    names = [n for n, _, _ in launched]
    assert names == ["nop", "oracle", "plausible-wrong-keyerror", "tamper-root-conftest", "probe"]
    assert launched[0][1] == "nop" and launched[1][1] == "oracle"
    assert launched[2][1] == validate.PATCH_AGENT and launched[2][2]["patch"].endswith("controls/keyerror.diff")
    assert launched[4][2]["probe"] == "true" and json.loads(launched[4][2]["gold_lines"]) == out["probe"]["probes"]
    assert all(jd.parent.parent.name == "c" * 16 for jd in [Path(p) for p in out["trial_dirs"].values()])  # jobs keyed by compiled hash


def test_validate_scenario_invalid_reasons(tmp_path, monkeypatch):
    ws, spec = _spec_ws(tmp_path)
    monkeypatch.setattr(validate, "compile_scenario", _fake_compile(tmp_path, spec))
    bad = json.loads(json.dumps(GOOD)); bad["nop"]["per_test"] = [{"id": "t1", "observed": "passed"}, {"id": "t0", "observed": "passed"}]
    probe_txt = ("REFS=1\nREFLOG=1\nGOLD_SHA_CHECKED=abc\nHITS_BEGIN\nGOLD_OBJECT_REACHABLE:abc\n"
                 "RAW 0\t./build/lib/x.py\nRAW 0\t./x.py\nRAW 0\t./stale/x.py\nHIT 0\t./stale/x.py\nHITS_END\n")  # base copies raw only; one post-fix copy
    run_task, _ = _fake_harbor(ws.jobs_dir, bad, probe_txt, exception_for={"tamper-root-conftest"})
    monkeypatch.setattr(harbor_cli, "run_task", run_task)
    out = validate.validate_scenario(ws, spec, RunConfig.default("full"), "https://x.modal.run")
    assert out["status"] == "invalid"
    assert any("base must execute" in f for f in out["failures"])
    assert any("gold patch lines reachable" in f and "./stale/x.py" in f and "build/lib" not in f and "GOLD_OBJECT" not in f for f in out["failures"])
    assert any("gold commit object reachable" in f for f in out["failures"]) and out["probe"]["gold_object_reachable"] is True
    assert len(out["probe"]["raw_hits"]) == 3 and out["probe"]["filtered_hits"] == [{"probe": 0, "file": "./stale/x.py"}] and out["probe"]["gold_hits"] == ["./stale/x.py"]
    assert any(f.startswith("tamper:root-conftest") and "control agent failed" in f for f in out["failures"])
    assert json.loads((ws.scenario_dir(spec.id) / "validation.json").read_text())["status"] == "invalid"
    # base copies alone never invalidate: same probe output without the post-fix copy and without the gold object
    ws3, spec3 = _spec_ws(tmp_path / "c"); monkeypatch.setattr(validate, "compile_scenario", _fake_compile(tmp_path / "c", spec3))
    run_task, _ = _fake_harbor(ws3.jobs_dir, GOOD, "REFS=1\nREFLOG=1\nGOLD_SHA_CHECKED=abc\nHITS_BEGIN\nRAW 0\t./build/lib/x.py\nRAW 0\t./x.py\nHITS_END\n")
    monkeypatch.setattr(harbor_cli, "run_task", run_task)
    out = validate.validate_scenario(ws3, spec3, RunConfig.default("full"), "https://x.modal.run")
    assert out["status"] == "valid" and len(out["probe"]["raw_hits"]) == 2 and out["probe"]["filtered_hits"] == []


def test_validate_notes_skipped_line_probe(tmp_path, monkeypatch):
    ws, spec = _spec_ws(tmp_path)
    monkeypatch.setattr(validate, "compile_scenario", _fake_compile(tmp_path, spec, oracle_diff="--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"))
    run_task, launched = _fake_harbor(ws.jobs_dir, GOOD, "REFS=1\nREFLOG=0\nGOLD_SHA_CHECKED=none\nHITS_BEGIN\nHITS_END\n")
    monkeypatch.setattr(harbor_cli, "run_task", run_task)
    out = validate.validate_scenario(ws, spec, RunConfig.default("full"), "https://x.modal.run")
    assert out["status"] == "valid" and out["probe"]["probes"] == [] and json.loads(launched[4][2]["gold_lines"]) == []
    assert out["probe"]["note"] == "line probe skipped: no distinctive added lines in the oracle diff"  # a clean scan was not performed


def test_validate_scenario_missing_probe_and_launch_failure(tmp_path, monkeypatch):
    ws, spec = _spec_ws(tmp_path)
    monkeypatch.setattr(validate, "compile_scenario", _fake_compile(tmp_path, spec))
    run_task, _ = _fake_harbor(ws.jobs_dir, GOOD, None)
    monkeypatch.setattr(harbor_cli, "run_task", run_task)
    out = validate.validate_scenario(ws, spec, RunConfig.default("full"), "https://x.modal.run")
    assert out["status"] == "invalid" and any("reachability evidence" in f for f in out["failures"])

    ws2, spec2 = _spec_ws(tmp_path / "b")
    monkeypatch.setattr(validate, "compile_scenario", _fake_compile(tmp_path / "b", spec2))
    calls = []
    def failing(task_dir, agent, jobs_dir, job_name, **kw):
        calls.append(job_name); raise harbor_cli.HarborRunError("no trial dir")
    monkeypatch.setattr(harbor_cli, "run_task", failing)
    out = validate.validate_scenario(ws2, spec2, RunConfig.default("full"), "https://x.modal.run")
    assert out["status"] == "invalid" and any("nop" in f and "no trial dir" in f for f in out["failures"])
    assert len(calls) == 1  # a launch failure stops the sweep instead of burning five more launches
    assert (ws2.scenario_dir(spec2.id) / "validation.json").exists()


# ---- harbor_cli -------------------------------------------------------------------------------

def _fake_run_factory(jobs_dir: Path, trials=1, rc=0, result=None):
    seen = {}
    def fake_run(cmd, capture_output, text, timeout, env):
        seen.update(cmd=cmd, env=env, timeout=timeout)
        job = jobs_dir / cmd[cmd.index("--job-name") + 1]
        for i in range(trials):
            (job / f"trial{i}").mkdir(parents=True); (job / f"trial{i}" / "result.json").write_text(json.dumps(result if result is not None else {"id": i}))
        return subprocess.CompletedProcess(cmd, rc, stdout="out", stderr="err")
    return fake_run, seen


def test_run_task_builds_command_and_returns_trial(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"; fake_run, seen = _fake_run_factory(jobs)
    monkeypatch.setattr(harbor_cli.subprocess, "run", fake_run)
    fake_bin = tmp_path / "bin"; fake_bin.mkdir(); (fake_bin / "harbor").write_text("#!/bin/sh\n"); (fake_bin / "harbor").chmod(0o755)
    monkeypatch.setattr(harbor_cli.sys, "executable", str(fake_bin / "python"))
    monkeypatch.setenv("GH_TOKEN", "gh-secret"); monkeypatch.setenv("SWESIM_WORKER_SECRET", "w"); monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    td = harbor_cli.run_task(tmp_path / "task", validate.PATCH_AGENT, jobs, "job-1", model="anthropic/m", agent_kwargs={"patch": "/p.diff", "probe": "true"}, timeout_sec=123)
    assert td == jobs / "job-1" / "trial0"
    cmd = seen["cmd"]
    assert cmd[0] == str(fake_bin / "harbor") and cmd[1:3] == ["run", "--max-retries"] and cmd[3] == "0"
    for flag, val in (("-p", str(tmp_path / "task")), ("--agent", validate.PATCH_AGENT), ("-e", "modal"), ("--jobs-dir", str(jobs)), ("--job-name", "job-1"), ("-m", "anthropic/m")):
        assert cmd[cmd.index(flag) + 1] == val, flag
    assert "--yes" in cmd and cmd.count("--ak") == 2 and "patch=/p.diff" in cmd and "probe=true" in cmd
    assert seen["timeout"] == 123
    assert "GH_TOKEN" not in seen["env"] and "SWESIM_WORKER_SECRET" not in seen["env"] and seen["env"]["ANTHROPIC_API_KEY"] == "k"
    assert seen["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(Path(harbor_cli.__file__).resolve().parent.parent)


def test_run_task_resolves_harbor_from_path_or_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(harbor_cli.sys, "executable", str(tmp_path / "nowhere" / "python"))
    monkeypatch.setattr(harbor_cli.shutil, "which", lambda name: "/opt/bin/harbor" if name == "harbor" else None)
    assert harbor_cli.harbor_executable() == "/opt/bin/harbor"
    monkeypatch.setattr(harbor_cli.shutil, "which", lambda name: None)
    with pytest.raises(harbor_cli.HarborRunError, match="harbor executable"):
        harbor_cli.run_task(tmp_path / "task", "nop", tmp_path / "jobs", "j")
    real = Path(sys.executable).with_name("harbor")
    if real.exists():
        monkeypatch.setattr(harbor_cli.sys, "executable", sys.executable)
        assert harbor_cli.harbor_executable() == str(real)


def test_run_task_reconciles_and_refuses_ambiguity(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"; monkeypatch.setattr(harbor_cli, "harbor_executable", lambda: "/bin/harbor")
    launched = []
    monkeypatch.setattr(harbor_cli.subprocess, "run", lambda *a, **k: launched.append(1))
    existing = jobs / "done" / "trialA"; existing.mkdir(parents=True); (existing / "result.json").write_text("{}")
    assert harbor_cli.run_task(tmp_path / "task", "nop", jobs, "done") == existing and not launched
    (jobs / "pending").mkdir()
    with pytest.raises(harbor_cli.HarborRunError, match="reconcile"):
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "pending")
    assert not launched
    fake_run, _ = _fake_run_factory(jobs, trials=2); monkeypatch.setattr(harbor_cli.subprocess, "run", fake_run)
    with pytest.raises(harbor_cli.HarborRunError, match="ambiguous"):
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "two")
    fake_run, _ = _fake_run_factory(jobs, trials=0, rc=1); monkeypatch.setattr(harbor_cli.subprocess, "run", fake_run)
    with pytest.raises(harbor_cli.HarborRunError, match="no trial dir"):
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "none")
    fake_run, _ = _fake_run_factory(jobs, trials=1, rc=1, result={"exception_info": {"exception_type": "AgentError"}}); monkeypatch.setattr(harbor_cli.subprocess, "run", fake_run)
    assert harbor_cli.run_task(tmp_path / "task", "nop", jobs, "agent-error").name == "trial0"  # agent-error trials are returned for attribution

    def timeout(*a, **k): raise subprocess.TimeoutExpired(cmd="harbor", timeout=1)
    monkeypatch.setattr(harbor_cli.subprocess, "run", timeout)
    with pytest.raises(harbor_cli.HarborRunError, match="deadline"):
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "slow")


def test_run_task_malformed_result_is_a_harbor_error(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"; monkeypatch.setattr(harbor_cli, "harbor_executable", lambda: "/bin/harbor")
    def fake_run(cmd, capture_output, text, timeout, env):
        job = jobs / cmd[cmd.index("--job-name") + 1]; (job / "t").mkdir(parents=True); (job / "t" / "result.json").write_text("{not json")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(harbor_cli.subprocess, "run", fake_run)
    with pytest.raises(harbor_cli.HarborRunError, match="malformed"):
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "bad")
    with pytest.raises(harbor_cli.HarborRunError, match="malformed"):  # reconciling an existing job reads the same file
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "bad")


def test_read_reward_and_report(tmp_path):
    td = tmp_path / "trial"; (td / "verifier").mkdir(parents=True)
    assert harbor_cli.read_reward(td) == {} and harbor_cli.read_report(td) == {}
    (td / "verifier" / "reward.json").write_text('{"reward": 1, "success": 1}'); (td / "verifier" / "report.json").write_text('{"per_test": []}')
    assert harbor_cli.read_reward(td) == {"reward": 1, "success": 1} and harbor_cli.read_report(td) == {"per_test": []}


# ---- harbor_cli: bounded fresh attempts after an abandoned job dir (P2-17) ----------------------

def _abandon(job_dir: Path) -> Path:
    """What a worker-pool roll leaves behind: Harbor created the job and trial dirs, nobody wrote result.json."""
    (job_dir / "trial-interrupted" / "agent").mkdir(parents=True); (job_dir / "trial-interrupted" / "agent" / "log.txt").write_text("killed mid-run")
    return job_dir


def test_run_task_starts_a_fresh_attempt_after_an_abandoned_job_and_records_lineage(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"; monkeypatch.setattr(harbor_cli, "harbor_executable", lambda: "/bin/harbor")
    fake_run, seen = _fake_run_factory(jobs); monkeypatch.setattr(harbor_cli.subprocess, "run", fake_run)
    original = _abandon(jobs / "probe-x"); before = sorted(str(p.relative_to(original)) for p in original.rglob("*"))
    td = harbor_cli.run_task(tmp_path / "task", "nop", jobs, "probe-x", max_fresh_attempts=3, image_digest="sha256:" + "a" * 64)
    assert td == jobs / "probe-x--attempt-1" / "trial0"
    assert seen["cmd"][seen["cmd"].index("--job-name") + 1] == "probe-x--attempt-1" and seen["cmd"][seen["cmd"].index("--jobs-dir") + 1] == str(jobs)
    # the abandoned directory is evidence: never deleted, never overwritten
    assert sorted(str(p.relative_to(original)) for p in original.rglob("*")) == before and not harbor_cli._terminal_trials(original)
    lineage = json.loads(harbor_cli.lineage_path(jobs, "probe-x").read_text())
    assert harbor_cli.lineage_path(jobs, "probe-x").parent == jobs and lineage["job_name"] == "probe-x"
    [entry] = lineage["attempts"]
    assert entry["attempt"] == 1 and entry["job_dir"] == str(jobs / "probe-x--attempt-1") and entry["previous_dir"] == str(original)
    assert "abandoned" in entry["reason"] and entry["at"].endswith("+00:00") and entry["worker_image_digest"] == "sha256:" + "a" * 64
    # a later call reuses the attempt's terminal result instead of launching again; the lineage is unchanged
    monkeypatch.setattr(harbor_cli.subprocess, "run", lambda *a, **k: pytest.fail("a terminal result is reused, never relaunched"))
    assert harbor_cli.run_task(tmp_path / "task", "nop", jobs, "probe-x", max_fresh_attempts=3) == td
    assert json.loads(harbor_cli.lineage_path(jobs, "probe-x").read_text()) == lineage


def test_run_task_fresh_attempts_are_opt_in_and_terminal_results_win(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"; monkeypatch.setattr(harbor_cli, "harbor_executable", lambda: "/bin/harbor")
    monkeypatch.setattr(harbor_cli.subprocess, "run", lambda *a, **k: pytest.fail("launch-once callers never relaunch"))
    _abandon(jobs / "graded")
    with pytest.raises(harbor_cli.HarborRunError, match="reconcile, do not relaunch"):  # default: unknown state only reconciles
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "graded")
    assert not harbor_cli.lineage_path(jobs, "graded").exists() and not (jobs / "graded--attempt-1").exists()
    # the original has a unique terminal result: it is reused even when attempts are allowed and a stale attempt dir exists
    done = jobs / "done" / "trialA"; done.mkdir(parents=True); (done / "result.json").write_text("{}"); _abandon(jobs / "done--attempt-1")
    assert harbor_cli.run_task(tmp_path / "task", "nop", jobs, "done", max_fresh_attempts=3) == done
    # two terminal results is an anomaly, not an abandonment: reconcile by hand
    two = jobs / "two"; (two / "a").mkdir(parents=True); (two / "b").mkdir(); (two / "a" / "result.json").write_text("{}"); (two / "b" / "result.json").write_text("{}")
    with pytest.raises(harbor_cli.HarborRunError, match="reconcile, do not relaunch"):
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "two", max_fresh_attempts=3)
    assert not (jobs / "two--attempt-1").exists()


def test_run_task_fourth_abandonment_raises_with_the_lineage(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"; monkeypatch.setattr(harbor_cli, "harbor_executable", lambda: "/bin/harbor")
    launched = []
    def interrupted(cmd, capture_output, text, timeout, env):  # every launch dies before result.json, like a pool roll
        launched.append(cmd[cmd.index("--job-name") + 1]); _abandon(jobs / launched[-1])
        raise subprocess.TimeoutExpired(cmd="harbor", timeout=1)
    monkeypatch.setattr(harbor_cli.subprocess, "run", interrupted)
    _abandon(jobs / "flaky")
    for n in (1, 2, 3):
        with pytest.raises(harbor_cli.HarborRunError, match="deadline"):
            harbor_cli.run_task(tmp_path / "task", "nop", jobs, "flaky", max_fresh_attempts=3, image_digest=f"sha256:{n}")
        assert launched[-1] == f"flaky--attempt-{n}"
    lineage = json.loads(harbor_cli.lineage_path(jobs, "flaky").read_text())
    assert [e["attempt"] for e in lineage["attempts"]] == [1, 2, 3]
    assert [e["previous_dir"] for e in lineage["attempts"]] == [str(jobs / "flaky"), str(jobs / "flaky--attempt-1"), str(jobs / "flaky--attempt-2")]
    assert [e["worker_image_digest"] for e in lineage["attempts"]] == ["sha256:1", "sha256:2", "sha256:3"]
    with pytest.raises(harbor_cli.HarborRunError, match="reconcile, do not relaunch") as info:  # the 4th occurrence: bounded
        harbor_cli.run_task(tmp_path / "task", "nop", jobs, "flaky", max_fresh_attempts=3)
    msg = str(info.value)
    assert len(launched) == 3 and not (jobs / "flaky--attempt-4").exists()
    assert str(jobs / "flaky") in msg and str(jobs / "flaky--attempt-3") in msg and str(harbor_cli.lineage_path(jobs, "flaky")) in msg and "sha256:3" in msg
    assert json.loads(harbor_cli.lineage_path(jobs, "flaky").read_text()) == lineage  # nothing new is recorded when nothing new runs
    for d in ("flaky", "flaky--attempt-1", "flaky--attempt-2", "flaky--attempt-3"):
        assert (jobs / d / "trial-interrupted" / "agent" / "log.txt").read_text() == "killed mid-run"


def test_run_task_never_treats_a_job_live_in_this_worker_as_abandoned(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"; monkeypatch.setattr(harbor_cli, "harbor_executable", lambda: "/bin/harbor")
    inner = {}
    def fake_run(cmd, capture_output, text, timeout, env):
        job = jobs / cmd[cmd.index("--job-name") + 1]; _abandon(job)  # Harbor has created the dir; the result is pending
        with pytest.raises(harbor_cli.HarborRunError, match="running in this worker") as info:  # a concurrent caller in the same process
            harbor_cli.run_task(tmp_path / "task", "nop", jobs, "live", max_fresh_attempts=3)
        inner["msg"] = str(info.value); (job / "trial0").mkdir(); (job / "trial0" / "result.json").write_text("{}")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(harbor_cli.subprocess, "run", fake_run)
    assert harbor_cli.run_task(tmp_path / "task", "nop", jobs, "live", max_fresh_attempts=3) == jobs / "live" / "trial0"
    assert "reconcile" in inner["msg"] and not (jobs / "live--attempt-1").exists() and not harbor_cli.lineage_path(jobs, "live").exists()
    assert not harbor_cli._LIVE  # the registry is released after the launch, success or not


def test_job_dirs_lists_the_original_and_every_attempt_for_ownership_checks(tmp_path):
    dirs = harbor_cli.job_dirs(tmp_path, "sid--nop")
    assert dirs == [tmp_path / "sid--nop", *(tmp_path / f"sid--nop--attempt-{n}" for n in (1, 2, 3))]
    assert harbor_cli.MAX_FRESH_ATTEMPTS == 3
