"""scripts/cloud/import_vm.sh with a fake gcloud: what is uploaded, what is excluded, and that the index is migrated
and rebuilt with origin vm-systemd afterwards (against a SQLite file). Offline; bash and the venv's python only."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from swesim.index import models as M
from swesim.index.store import IndexStore

try:
    from test_index_project import Record, build_record
except ImportError:  # pragma: no cover
    from tests.test_index_project import Record, build_record

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "cloud" / "import_vm.sh"
FAKE_GCLOUD = """#!/bin/bash
printf '%s\\n' "$*" >> "$GCLOUD_LOG"
"""


@pytest.fixture(scope="module")
def record(tmp_path_factory) -> Record:
    return build_record(tmp_path_factory.mktemp("record"))


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_import_uploads_the_record_without_task_copies_and_rebuilds_the_index(tmp_path, record: Record):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(FAKE_GCLOUD)
    (bin_dir / "gcloud").chmod(0o755)
    log = tmp_path / "gcloud.log"
    env_file = tmp_path / "test.env"
    db = tmp_path / "index.db"
    env_file.write_text(f"SWESIM_WORKSPACE={record.layout.root}\nSWESIM_REGISTRY={record.registry_path}\nSWESIM_BUCKET=swesim-test-bucket\n"
                        f"SWESIM_DATABASE_URL=sqlite:///{db}\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWESIM_")}
    env.update({"PATH": f"{bin_dir}:{env['PATH']}", "GCLOUD_LOG": str(log), "SWESIM_ENV_FILE": str(env_file), "PYTHON": sys.executable})
    proc = subprocess.run(["bash", str(SCRIPT)], cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = [line.split() for line in log.read_text().splitlines()]  # argv after `gcloud`: --quiet storage <verb> ... <src> <dst>
    root = str(record.layout.root.resolve())
    rsyncs = {c[-2]: c for c in calls if c[1:3] == ["storage", "rsync"]}
    for prefix in ("scenarios", "candidates", "evidence", "costs", "trials", "jobs"):
        assert f"{root}/{prefix}" in rsyncs and rsyncs[f"{root}/{prefix}"][-1] == f"gs://swesim-test-bucket/{prefix}", prefix
    assert f"{root}/tasks" not in rsyncs  # the synthetic record has no compiled task dirs: a missing prefix is skipped, not an error
    assert "--exclude=^[^/]+/task/.*|.*\\.lock$" in rsyncs[f"{root}/trials"]
    assert any(a.startswith("--exclude=") and "config" in a and "task" in a for a in rsyncs[f"{root}/jobs"])
    assert not any("--delete-unmatched-destination-objects" in c for c in calls)
    cps = [c for c in calls if c[1:3] == ["storage", "cp"]]
    assert [c[-1] for c in cps] == ["gs://swesim-test-bucket/registry.json", "gs://swesim-test-bucket/dataset.json"]
    assert "index rebuilt with origin vm-systemd" in proc.stdout
    store = IndexStore(f"sqlite:///{db}")
    assert store.trials(M.TrialFilter()).total == len(record.tids) - 1 and store.rebuilds()[0].origin == "vm-systemd"
    assert store.trial(record.tids["done"]).origin == "vm-systemd"
    # a second run is a no-op for the index (same files) and never deletes bucket objects
    proc2 = subprocess.run(["bash", str(SCRIPT)], cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    assert proc2.returncode == 0 and store.rebuilds()[0].diff["trials"]["delta"] == 0 and len(store.rebuilds()) == 2
