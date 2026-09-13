"""Recorded artifact paths from another machine resolve against this workspace, and the episode page falls back to the
compiled task's instruction when the per-trial task copy (session token; never mirrored) is absent."""
from __future__ import annotations

import json
import shutil

from swesim.dashboard import data
from swesim.storage.layout import Layout
from test_dashboard import site as site  # noqa: F401 - fixture

VM_ROOT = "/opt/swesim/workspace"
SUBMISSION = "diff --git a/pkg/new.py b/pkg/new.py\n--- a/pkg/new.py\n+++ b/pkg/new.py\n@@ -1 +1 @@\n-old\n+REROOTED_MARKER\n"


def test_local_path_reroots_vm_paths_onto_this_workspace(tmp_path):
    root = tmp_path / "workspace"
    layout = Layout(root=root)
    tid = "689a9433aefa9bc77622f0b4"
    here = root / "jobs" / "runs" / tid / "task__abcd"
    (here / "verifier").mkdir(parents=True)
    (here / "verifier" / "submission.diff").write_text(SUBMISSION)
    # the VM's absolute path does not exist here: the part after /workspace/ is joined onto this workspace
    assert data.local_path(layout, f"{VM_ROOT}/jobs/runs/{tid}/task__abcd") == here
    # a re-rooted path is returned even before its files exist (callers check the file), never outside the workspace
    assert data.local_path(layout, f"{VM_ROOT}/jobs/runs/{tid}/other") == root / "jobs" / "runs" / tid / "other"
    assert data.local_path(layout, f"{VM_ROOT}/../etc/passwd") is None and data.local_path(layout, f"{VM_ROOT}/jobs/../../etc") is None
    # an existing path inside this workspace is taken as recorded; one outside it is not followed
    assert data.local_path(layout, str(here)) == here
    outside = tmp_path / "elsewhere" / "workspace" / "jobs"
    outside.mkdir(parents=True)
    assert data.local_path(layout, str(outside)) == root / "jobs"  # re-rooted, not the foreign directory
    assert data.local_path(layout, "/tmp/no-marker/runs/x") is None and data.local_path(layout, None) is None and data.local_path(layout, "") is None
    assert data.local_path(layout, f"{VM_ROOT}/") is None


def _point_record_at_vm_paths(site, tid: str) -> str:
    """Rewrite the fixture record's trial_dir/task_dir to the VM's absolute paths; the artifacts stay where the fixture wrote them."""
    ws = site["ws"]
    record_path = ws.root / "trials" / tid / "record.json"
    record = json.loads(record_path.read_text())
    harbor_name = record["trial_dir"].rsplit("/", 1)[-1]
    record["trial_dir"] = f"{VM_ROOT}/jobs/runs/{tid}/{harbor_name}"
    record["task_dir"] = f"{VM_ROOT}/trials/{tid}/task"
    record_path.write_text(json.dumps(record))
    return harbor_name


def test_episode_resolves_vm_recorded_artifacts_against_this_workspace(site):
    tid = site["done"]
    harbor_name = _point_record_at_vm_paths(site, tid)
    verifier = site["ws"].root / "jobs" / "runs" / tid / harbor_name / "verifier"
    (verifier / "submission.diff").write_text(SUBMISSION)
    c = site["client"]
    html = c.get(f"/trial/{tid}").text
    assert "REROOTED_MARKER" in html and "verifier/submission.diff" in html and "No patch artifact collected" not in html
    assert c.get(f"/trial/{tid}/file/submission.diff").text == SUBMISSION  # public artifact
    ev = site["eval"]  # the harbor-side artifacts are evaluator-only; they re-root the same way
    assert c.get(f"/trial/{tid}/file/trajectory.json", headers=ev).status_code == 200
    assert c.get(f"/trial/{tid}/file/verifier-reward.json", headers=ev).json()["outcome_code"] == 1
    assert c.get(f"/trial/{tid}/file/harbor-result.json", headers=ev).status_code == 200
    assert c.get(f"/trial/{tid}/file/trajectory.json").status_code == 403  # still gated for anonymous identities
    timeline = c.get(f"/api/trial/{tid}/timeline").json()
    assert timeline["patch"] == SUBMISSION and any(r["actor"] == "agent" and r["kind"] == "agent-step" for r in timeline["timeline"])


def test_episode_falls_back_to_the_compiled_task_instruction_with_a_label(site):
    tid = site["done"]
    ws = site["ws"]
    _point_record_at_vm_paths(site, tid)
    shutil.rmtree(ws.root / "trials" / tid / "task")  # the per-trial copy is not in the bucket
    c = site["client"]
    html = c.get(f"/trial/{tid}").text
    assert "Instruction not materialised yet" in html and "compiled task" in html  # says where it looked
    compiled = ws.task_dir(site["spec"].id, "interactive")
    compiled.mkdir(parents=True, exist_ok=True)
    (compiled / "instruction.md").write_text("# Task\n\nCOMPILED_INSTRUCTION_MARKER\n")
    html = c.get(f"/trial/{tid}").text
    assert "COMPILED_INSTRUCTION_MARKER" in html and "instruction.md from the compiled task" in html and "data-instruction-source=compiled-task" in html
    assert "Instruction not materialised yet" not in html
    # the per-trial copy, when present, is still preferred and labelled as what the agent saw
    (ws.root / "trials" / tid / "task").mkdir()
    (ws.root / "trials" / tid / "task" / "instruction.md").write_text("# Task\n\nTRIAL_COPY_MARKER\n")
    html = c.get(f"/trial/{tid}").text
    assert "TRIAL_COPY_MARKER" in html and "as the agent saw it" in html and "COMPILED_INSTRUCTION_MARKER" not in html
