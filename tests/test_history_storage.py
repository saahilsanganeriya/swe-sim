"""History publication is immutable and works without a writable hosted mount."""
from datetime import UTC, datetime, timedelta

import pytest
from google.api_core.exceptions import PreconditionFailed

from swesim.index import models as M
from swesim.settings import Settings
from swesim.storage import history
from swesim.storage.layout import Layout


def workflow(**over):
    return M.WorkflowRow(**{**dict(workflow_id="create:one", run_id="run-1", type="CandidateWorkflow", parent_workflow_id="create-parent",
                                  status="running", params_summary={}, failure=None, started_at=datetime(2026, 9, 13, tzinfo=UTC),
                                  closed_at=None, task_queue="swesim-authoring", cost_usd=None, search_attributes={}, origin="cloudrun"), **over})


def test_immutable_replay_and_content_addressed_history(tmp_path):
    layout = Layout(root=tmp_path)
    first = workflow()
    path = history.persist_row(layout, first)
    original = path.read_bytes()
    later = first.model_copy(update={"updated_at": first.updated_at + timedelta(seconds=30)})
    assert history.persist_row(layout, later) == path and path.read_bytes() == original
    assert history.read_rows(layout, "workflows") == [first]
    path.write_bytes(b'{}')
    with pytest.raises(ValueError):
        history.persist_row(layout, first)


class Bucket:
    def __init__(self): self.objects = {}
    def blob(self, name): return Blob(self, name)
    def get_blob(self, name): return self.blob(name) if name in self.objects else None
    def list_blobs(self, prefix): return [self.blob(name) for name in self.objects if name.startswith(prefix)]


class Blob:
    generation = 1
    def __init__(self, bucket, name): self.bucket, self.name = bucket, name
    def upload_from_string(self, data, *, if_generation_match, **kwargs):
        assert if_generation_match == 0
        if self.name in self.bucket.objects: raise PreconditionFailed("already published")
        self.bucket.objects[self.name] = data
    def download_as_bytes(self, *, if_generation_match):
        assert if_generation_match == 1
        return self.bucket.objects[self.name]


def test_hosted_history_uses_object_api_without_writing_mount(tmp_path, monkeypatch):
    layout = Layout(root=tmp_path / "read-only-mount")
    settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_BUCKET": "record", "SWESIM_ORIGIN": "cloudrun"})
    bucket = Bucket()
    monkeypatch.setattr(history, "bucket_for", lambda settings: bucket)
    row = workflow()
    path = history.persist_row(layout, row, settings)
    assert not layout.root.exists() and not path.exists()
    assert history.persist_row(layout, row, settings) == path
    assert history.read_rows(layout, "workflows", settings) == [row]


def test_a_laptop_import_with_a_bucket_name_still_uses_the_files(tmp_path):
    """`scripts/cloud/import_vm.sh` runs `swesim index rebuild` with SWESIM_BUCKET set and origin laptop: files, not the API."""
    layout = Layout(root=tmp_path / "record")
    settings = Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"), "SWESIM_BUCKET": "record", "SWESIM_WORKSPACE": str(layout.root)})
    assert not history.hosted(settings)
    row = workflow()
    path = history.persist_row(layout, row, settings)
    assert path.is_file() and history.read_rows(layout, "workflows", settings) == [row]
