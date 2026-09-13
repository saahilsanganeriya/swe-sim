"""Layout (interfaces §1): every record path is derived here and nowhere else. Tests pin every path string relative to
`root`, the id validation, and the `Workspace` adapter that keeps today's callers working."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from swesim.paths import Workspace, atomic_json
from swesim.storage.layout import Layout, LayoutError

SID = "py-pdf--pypdf-3826"
TID = "0123456789abcdef0123abcd"


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(root=tmp_path / "ws")


def test_scenario_paths(layout: Layout):
    root = layout.root
    assert layout.scenario_dir(SID) == root / "scenarios" / SID
    for name in ("scenario.json", "scenario.draft.json", "draft.meta.json", "judge.json", "approval.json", "compile.json",
                 "validation.json", "difficulty.json", "audit.json", "scores.json"):
        assert layout.scenario_file(SID, name) == root / "scenarios" / SID / name
    assert layout.scenario_assets_dir(SID) == root / "scenarios" / SID / "assets"
    assert layout.scenario_archive_dir(SID, 2) == root / "scenarios" / SID / "archive" / "v2"
    with pytest.raises(LayoutError):
        layout.scenario_file(SID, "review.json")  # not a record file
    with pytest.raises(LayoutError):
        layout.scenario_archive_dir(SID, 0)  # versions start at 1


def test_task_and_trial_paths(layout: Layout):
    root = layout.root
    assert layout.task_dir(SID, "interactive") == root / "tasks" / f"{SID}__interactive"
    with pytest.raises(LayoutError):
        layout.task_dir(SID, "partial")
    assert layout.trial_dir(TID) == root / "trials" / TID
    for name in ("request.json", "launch.json", "status.json", "record.json", "report.json", "reward.json", "run_config.json",
                 "spec.json", "transcript.json", "runner-error.json", "launcher.log", "costs.json", "audit.json"):
        assert layout.trial_file(TID, name) == root / "trials" / TID / name
    with pytest.raises(LayoutError):
        layout.trial_file(TID, "label.json")
    assert layout.trial_task_dir(TID) == root / "trials" / TID / "task"
    assert layout.export_path(TID) == root / "exports" / f"{TID}.episode.json"


def test_candidate_probe_evidence_and_job_paths(layout: Layout):
    root = layout.root
    key = "github-repo--py-pdf__pypdf"
    assert layout.candidates_dir() == root / "candidates"
    assert layout.candidates_file(key, "mined") == root / "candidates" / f"{key}.jsonl"
    assert layout.candidates_file(key, "judged") == root / "candidates" / f"{key}.judged.jsonl"
    assert layout.candidates_file(key, "probed") == root / "candidates" / f"{key}.judged.probed.jsonl"
    assert layout.candidates_file(key, "probe_summary") == root / "candidates" / f"{key}.judged.probed.summary.json"
    assert layout.candidates_file(key, "rejects") == root / "candidates" / f"{key}.rejects.json"
    assert layout.candidates_file(key, "status") == root / "candidates" / f"{key}.status"
    with pytest.raises(LayoutError):
        layout.candidates_file(key, "drafted")
    with pytest.raises(LayoutError):
        layout.candidates_file("../escape", "mined")
    assert layout.probe_dir("py-pdf__pypdf-3826") == root / "probes" / "py-pdf__pypdf-3826"
    assert layout.evidence_dir("cloudrun") == root / "evidence" / "cloudrun"
    with pytest.raises(LayoutError):
        layout.evidence_dir("../x")
    assert layout.model_probe_path() == root / "evidence" / "model-probe.json"
    from swesim.storage.layout import EVIDENCE_KINDS
    assert "model-probe" not in EVIDENCE_KINDS and {"cloudrun", "cloud", "sweep", "console", "audit"} <= set(EVIDENCE_KINDS)
    assert layout.calibration_path("openai/gpt-5.5", date(2026, 9, 13)) == root / "calibration" / "openai--gpt-5.5" / "2026-09-13.json"
    for kind in ("probe", "runs", "validate"):
        assert layout.jobs_dir(kind) == root / "jobs" / kind
    with pytest.raises(LayoutError):
        layout.jobs_dir("smoke")
    assert layout.registry_path() == root / "registry.json"
    assert layout.dataset_path() == root / "dataset.json"
    assert layout.costs_dir() == root / "costs"
    assert layout.workflow_costs_path("create:py-pdf--pypdf-3826") == root / "costs" / "create:py-pdf--pypdf-3826.json"
    with pytest.raises(LayoutError):
        layout.workflow_costs_path("../x")


def test_ids_are_validated(layout: Layout):
    for bad in ("", "A", "Upper-Case", "../x", "a/b", "x" * 65):
        with pytest.raises(LayoutError):
            layout.scenario_dir(bad)
    for bad in ("", TID[:-1], TID + "0", TID.upper(), "g" * 24, "../" + TID[3:]):
        with pytest.raises(LayoutError):
            layout.trial_dir(bad)
    assert issubclass(LayoutError, ValueError)


def test_relative_is_the_posix_object_name(layout: Layout):
    assert layout.relative(layout.trial_file(TID, "record.json")) == f"trials/{TID}/record.json"
    assert layout.relative(layout.scenario_archive_dir(SID, 1) / "scenario.json") == f"scenarios/{SID}/archive/v1/scenario.json"
    assert layout.relative(layout.root) == "."
    with pytest.raises(LayoutError):
        layout.relative(layout.root.parent / "elsewhere.json")


def test_layout_is_strict(tmp_path: Path):
    with pytest.raises(ValueError):
        Layout(root=tmp_path, bucket="x")  # type: ignore[call-arg]
    assert Layout(root=str(tmp_path)).root == tmp_path


def test_workspace_adapter_keeps_the_public_names(tmp_path: Path):
    ws = Workspace(tmp_path / "workspace", tmp_path / "registry.json")
    root = (tmp_path / "workspace").resolve()
    assert ws.root == root and ws.registry_path == (tmp_path / "registry.json").resolve()
    assert isinstance(ws.layout, Layout) and ws.layout.root == root
    assert ws.scenario_dir(SID) == ws.layout.scenario_dir(SID) == root / "scenarios" / SID
    assert ws.task_dir(SID, "full") == root / "tasks" / f"{SID}__full"
    assert ws.jobs_dir == root / "jobs" and ws.candidates_dir == root / "candidates"
    with pytest.raises(ValueError):
        ws.scenario_dir("../x")
    with pytest.raises(ValueError):
        ws.task_dir(SID, "partial")


def test_workspace_defaults_and_registry_override(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws = Workspace()
    assert ws.root == (tmp_path / "workspace").resolve() and ws.registry_path == (tmp_path / "registry.json").resolve()
    # Settings.registry_path may live outside the root on laptops: the adapter keeps the override; the layout keeps the record's.
    ws2 = Workspace(tmp_path / "w", tmp_path / "elsewhere" / "registry.json")
    assert ws2.registry_path == (tmp_path / "elsewhere" / "registry.json").resolve()
    assert ws2.layout.registry_path() == (tmp_path / "w").resolve() / "registry.json"


def test_atomic_json_writes_then_replaces(tmp_path: Path):
    target = tmp_path / "deep" / "x.json"
    atomic_json(target, {"b": 1, "a": [1, 2]})
    assert target.read_text() == '{\n  "a": [\n    1,\n    2\n  ],\n  "b": 1\n}'
    assert [p.name for p in target.parent.iterdir()] == ["x.json"]  # no temp file left behind
    with pytest.raises(ValueError):
        atomic_json(target, {"nan": float("nan")})
    assert target.read_text().startswith("{")  # the failed write never touched the file
