"""Authoritative discovery counters project into history and the same SQL/file console histogram."""
import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from temporalio.exceptions import ApplicationError

from swesim.dashboard.pages import reject_histogram
from swesim.index import activities as IA, models as M, project as P
from swesim.index.files import FileIndexReader
from swesim.paths import atomic_json
from swesim.settings import Settings
from swesim.storage.layout import Layout

KEY = "github-repo--py-pdf__pypdf--bugfix"


@pytest.fixture
def context(tmp_path):
    settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_WORKSPACE": str(tmp_path / "workspace"),
                                  "SWESIM_DATABASE_URL": f"sqlite:///{tmp_path / 'index.db'}", "SWESIM_ORIGIN": "cloudrun",
                                  "SWESIM_WORKER_IMAGE_DIGEST": "sha256:" + "a" * 64})
    IA.reset_store()
    store = IA.store_for(settings); store.migrate()
    layout = Layout(root=settings.workspace)
    yield layout, settings, store, FileIndexReader(layout, settings=settings)
    IA.reset_store()


def write_rejects(layout, counts, key=KEY):
    path = layout.candidates_file(key, "rejects")
    atomic_json(path, counts)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover(context, counts, *, run="r1", key=KEY, at="2026-09-13T10:00:00Z"):
    layout, settings, _, _ = context
    digest = write_rejects(layout, counts, key)
    payload = {"source_key": key, "artifact_sha256": digest, "workflow_id": "create:repo", "run_id": run, "discovered_at": at}
    return IA.index_discovery(payload, settings), payload


def test_discovery_history_is_idempotent_and_latest_counts_reach_console(context):
    layout, settings, store, files = context
    first, payload = discover(context, {"no-linked-issue": 3, "thin-thread": 2})
    assert first["indexed"] and first["rows"] == 1
    assert IA.index_discovery(payload, settings) == first
    snapshot = layout.discovery_snapshot(KEY, first["discovery_id"])
    original = snapshot.read_bytes()
    row = M.DiscoveryRow.model_validate_json(original)
    assert row.workflow_id == "create:repo" and row.run_id == "r1" and row.origin == "cloudrun"
    assert row.worker_image_digest == "sha256:" + "a" * 64 and row.identity_kind == "workflow"
    assert row.repo == "py-pdf/pypdf" and row.band == "bugfix"
    assert row.artifact_sha256 == payload["artifact_sha256"] and row.discovered_at == datetime(2026, 9, 13, 10, tzinfo=UTC)

    second, _ = discover(context, {"no-linked-issue": 1}, run="r2", at="2026-09-13T11:00:00Z")
    assert second["discovery_id"] != first["discovery_id"] and snapshot.read_bytes() == original
    assert store.table("discoveries", M.TableFilter()).total == files.table("discoveries", M.TableFilter()).total == 2
    # Reindex an old snapshot: updated_at must never make it the latest discovery.
    store.upsert_discovery(row)
    assert reject_histogram(store, [], None) == reject_histogram(files, [], None) == {"discovery:no-linked-issue": 1}
    assert store.candidates(M.CandidateFilter()).total == 0  # counts are never fake candidates
    store.rebuild_from_layout(layout, settings.registry_path)
    assert store.table("discoveries", M.TableFilter()).total == 2
    assert store.reject_histogram(KEY) == files.reject_histogram(KEY) == {"discovery:no-linked-issue": 1}
    discover(context, {}, run="r3", at="2026-09-13T12:00:00Z")
    assert store.reject_histogram(KEY) == files.reject_histogram(KEY) == {}
    assert store.table("discoveries", M.TableFilter()).total == 3


def test_legacy_reject_files_and_candidate_gates_are_distinct_and_not_duplicated(context):
    layout, settings, store, files = context
    legacy = "github-repo--legacy__repo"
    write_rejects(layout, {"judge": 7, "no-linked-issue": 2}, legacy)
    source = {"instance_id": "legacy__repo-1", "repo": "legacy/repo", "source": "github-repo"}
    layout.candidates_file(legacy, "mined").write_text(json.dumps(source) + "\n")
    judged = {**source, "judge": {"keep": False}, "reject_reason": "ambiguous"}
    for stage in ("judged", "probed"):
        layout.candidates_file(legacy, stage).write_text(json.dumps(judged) + "\n")
    discover(context, {"thin-thread": 4})
    store.rebuild_from_layout(layout, settings.registry_path)
    expected = {"discovery:judge": 7, "discovery:thin-thread": 4, "discovery:no-linked-issue": 2, "candidate:judge": 1}
    assert store.reject_histogram() == files.reject_histogram() == expected
    assert store.reject_histogram(legacy) == {k: v for k, v in expected.items() if k != "discovery:thin-thread"}
    row = next(row for row in P.project_discoveries(layout) if row.source_key == legacy)
    assert row.identity_kind == "legacy" and row.workflow_id is None and row.run_id is None and row.band is None
    assert row.repo == "legacy/repo"


@pytest.mark.parametrize("counts", [{"bad": -1}, {"bad": True}, {"bad": "3"}, {"bad": 1.5}, {"": 1}, [1, 2]])
def test_invalid_counts_fail_projection_instead_of_becoming_zero(context, counts):
    layout, _, _, _ = context
    write_rejects(layout, counts)
    with pytest.raises((P.ProjectionError, ValidationError)):
        P.project_discoveries(layout, KEY)


def test_projection_cannot_attribute_changed_artifact_to_previous_workflow(context):
    layout, settings, store, _ = context
    digest = write_rejects(layout, {"old": 2})
    write_rejects(layout, {"new": 3})
    with pytest.raises(ApplicationError, match="changed") as exc:
        IA.index_discovery({"source_key": KEY, "artifact_sha256": digest, "workflow_id": "create:old", "run_id": "r1"}, settings)
    assert exc.value.non_retryable and store.table("discoveries", M.TableFilter()).total == 0


def test_discovery_snapshot_is_preserved_when_sql_is_disabled(tmp_path):
    settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_WORKSPACE": str(tmp_path / "ws")})
    layout = Layout(root=settings.workspace)
    digest = write_rejects(layout, {"no-linked-issue": 4})
    result = IA.index_discovery({"source_key": KEY, "artifact_sha256": digest, "workflow_id": "create:x", "run_id": "r1"}, settings)
    assert result["indexed"] is False and result["reason"] == "index disabled"
    assert layout.discovery_snapshot(KEY, result["discovery_id"]).is_file()
    assert FileIndexReader(layout, settings=settings).reject_histogram() == {"discovery:no-linked-issue": 4}


def test_console_candidates_page_reads_the_indexed_discovery_histogram(context):
    from fastapi.testclient import TestClient
    from swesim.dashboard.app import app, reader_dep, settings_dep
    _, settings, store, _ = context
    discover(context, {"no-linked-issue": 4})
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[reader_dep] = lambda: store
    app.dependency_overrides[settings_dep] = lambda: settings
    try:
        response = TestClient(app).get("/candidates")
    finally:
        app.dependency_overrides.clear(); app.dependency_overrides.update(previous)
    assert response.status_code == 200
    assert "discovery:no-linked-issue" in response.text
    assert "Discovery-time reject counts are unavailable" not in response.text


def test_replay_uses_frozen_snapshot_after_flat_artifact_changes(context):
    layout, settings, store, _ = context
    result, payload = discover(context, {"old": 2})
    write_rejects(layout, {"new": 3})
    assert IA.index_discovery(payload, settings) == result
    assert store.table("discoveries", M.TableFilter()).total == 1


def test_immutable_reject_artifact_survives_another_discovery_before_indexing(context):
    layout, settings, store, _ = context
    digest = write_rejects(layout, {"first-run": 2})
    frozen = layout.evidence_dir("workflows") / "first" / "discovery.rejects.json"
    frozen.parent.mkdir(parents=True)
    frozen.write_bytes(layout.candidates_file(KEY, "rejects").read_bytes())
    write_rejects(layout, {"second-run": 3})
    result = IA.index_discovery({"source_key": KEY, "artifact_path": str(frozen), "artifact_sha256": digest,
                                 "workflow_id": "create:first", "run_id": "r1"}, settings)
    row = P.read_discovery_snapshot(layout, KEY, result["discovery_id"])
    assert row.rejects == {"first-run": 2} and row.artifact_path == layout.relative(frozen)
    assert store.reject_histogram(KEY) == {"discovery:first-run": 2}


def test_discovery_artifact_references_cannot_escape_workspace(context, tmp_path):
    layout, settings, _, _ = context
    outside = tmp_path / "outside.rejects.json"; outside.write_text('{"bad":1}')
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    link = layout.candidates_file(KEY, "rejects"); link.parent.mkdir(parents=True); link.symlink_to(outside)
    for path in (outside, link):
        with pytest.raises(ApplicationError, match="outside the workspace"):
            IA.index_discovery({"source_key": KEY, "artifact_path": str(path), "artifact_sha256": digest,
                                "workflow_id": "create:x", "run_id": "r1"}, settings)
