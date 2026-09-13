"""Projection against the real laptop mirror of the record (read-only). Skipped when the mirror is not present.
Every scenario in registry.json must project (all versions); every trial directory with a record.json or a launch.json
must project; directories without either are ignored. Counts are printed for the report."""
from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import pytest

from swesim.index import project as P
from swesim.storage.layout import Layout

WORKSPACE = Path(os.environ.get("SWESIM_MIRROR_WORKSPACE", "/path/to/swesim/workspace"))
REGISTRY = Path(os.environ.get("SWESIM_MIRROR_REGISTRY", "/path/to/swesim/registry.json"))

pytestmark = pytest.mark.skipif(not (WORKSPACE.is_dir() and REGISTRY.is_file()), reason="laptop mirror not present")


def test_every_registered_scenario_projects():
    layout = Layout(root=WORKSPACE)
    registry = P.project_registry(layout, REGISTRY)
    assert registry, "registry.json lists no scenarios"
    rows = []
    for sid in registry:
        versions = P.project_scenario_versions(layout, sid, registry=registry, origin="laptop")
        assert versions and versions[-1].validation_status == "valid" and versions[-1].registry_json is not None, sid
        rows.extend(versions)
    all_ids = P.scenario_ids(layout, registry)
    statuses = Counter()
    skipped = []
    for sid in all_ids:
        try:
            for r in P.project_scenario_versions(layout, sid, registry=registry, origin="laptop"):
                statuses[r.validation_status] += 1
        except ValueError as e:
            skipped.append((sid, str(e)[:120]))
    print(f"\nmirror scenarios: registered={len(registry)} registered_rows={len(rows)} dirs_or_registered={len(all_ids)} "
          f"by_status={dict(statuses)} skipped={skipped}")
    assert not skipped


def test_every_trial_directory_with_evidence_projects():
    layout = Layout(root=WORKSPACE)
    statuses = Counter()
    ignored = skipped = 0
    exchanges = costs = 0
    for tid in P.trial_ids(layout):
        d = layout.trial_dir(tid)
        if not (d / "record.json").exists() and not (d / "launch.json").exists():
            ignored += 1
            continue
        try:
            row, ex, cs = P.project_trial(layout, tid, origin="laptop")
        except P.ProjectionError as e:
            skipped += 1
            print(f"skipped {tid}: {e}")
            continue
        statuses[row.status] += 1
        exchanges += len(ex)
        costs += len(cs)
        if not (d / "record.json").exists():
            assert row.status in ("unresolved", "launch-failed", "running", "runner-error"), (tid, row.status)
    registry = json.loads(REGISTRY.read_text()).get("scenarios", {})
    unresolved_in_registry = sum(len(e.get("unresolved_runs") or []) for e in registry.values())
    print(f"\nmirror trials: by_status={dict(statuses)} ignored={ignored} skipped={skipped} exchanges={exchanges} costs={costs} "
          f"registry_unresolved_runs={unresolved_in_registry}")
    assert skipped == 0 and statuses["completed"] > 0
    assert statuses["unresolved"] == unresolved_in_registry


def test_mirror_scores_and_candidates_project():
    layout = Layout(root=WORKSPACE)
    registry = P.project_registry(layout, REGISTRY)
    scores = sum(len(P.project_scores(layout, sid, origin="laptop")) for sid in P.scenario_ids(layout, registry))
    stages = Counter()
    for key in P.source_keys(layout):
        for r in P.project_candidates(layout, key, registry=registry, origin="laptop"):
            stages[(r.stage, r.status)] += 1
    jobs = P.project_workflows_from_jobs(layout, origin="laptop")
    print(f"\nmirror scores={scores} candidates_by_stage={dict(stages)} harbor_jobs={len(jobs)} source_keys={len(P.source_keys(layout))}")
    assert scores > 0 and stages
