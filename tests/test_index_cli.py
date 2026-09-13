"""`swesim index migrate|rebuild|status` (Typer sub-app): exit codes, printed counts, nothing secret in the output."""
from __future__ import annotations

import pytest
from contextlib import contextmanager
from typer.testing import CliRunner

from swesim.index.cli import index_app
from swesim.settings import reset_settings

try:
    from test_index_project import Record, build_record
except ImportError:  # pragma: no cover
    from tests.test_index_project import Record, build_record

runner = CliRunner()


@pytest.fixture(scope="module")
def record(tmp_path_factory) -> Record:
    return build_record(tmp_path_factory.mktemp("record"))


@pytest.fixture
def env(tmp_path, record: Record, monkeypatch):
    for key in ("SWESIM_DATABASE_URL", "SWESIM_WORKSPACE", "SWESIM_REGISTRY", "SWESIM_ENV_FILE", "SWESIM_ORIGIN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SWESIM_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("SWESIM_WORKSPACE", str(record.layout.root))
    monkeypatch.setenv("SWESIM_REGISTRY", str(record.registry_path))
    reset_settings()
    yield monkeypatch
    reset_settings()


def test_disabled_index(env):
    status = runner.invoke(index_app, ["status"])
    assert status.exit_code == 0 and "index enabled: False" in status.output
    for cmd in (["migrate"], ["rebuild"]):
        r = runner.invoke(index_app, cmd)
        assert r.exit_code == 2 and "disabled" in (r.output + str(r.stderr if hasattr(r, "stderr") else ""))


def test_migrate_uses_the_same_schema_lock_as_worker_startup(env, tmp_path):
    from swesim.index.store import IndexStore, MIGRATION_LOCK_KEY
    original = IndexStore.advisory_lock
    keys = []

    @contextmanager
    def observed(self, key):
        keys.append(key)
        with original(self, key):
            yield

    env.setenv("SWESIM_DATABASE_URL", f"sqlite:///{tmp_path / 'locked.db'}")
    env.setattr(IndexStore, "advisory_lock", observed)
    reset_settings()
    result = runner.invoke(index_app, ["migrate"])
    assert result.exit_code == 0 and keys == [MIGRATION_LOCK_KEY]


def test_migrate_rebuild_status(env, tmp_path, record: Record):
    url = f"sqlite:///{tmp_path / 'index.db'}"
    env.setenv("SWESIM_DATABASE_URL", url)
    reset_settings()
    before = runner.invoke(index_app, ["status"])
    assert before.exit_code == 0 and "not migrated" in before.output and "missing" in before.output
    migrated = runner.invoke(index_app, ["migrate"])
    assert migrated.exit_code == 0 and "0004_history" in migrated.output
    bad = runner.invoke(index_app, ["rebuild", "--origin", "moon"])
    assert bad.exit_code == 2
    rebuilt = runner.invoke(index_app, ["rebuild", "--origin", "vm-systemd"])
    assert rebuilt.exit_code == 0, rebuilt.output
    assert f"trials            {len(record.tids) - 1:>7}  (was 0, +{len(record.tids) - 1})" in rebuilt.output
    assert "skipped trials" in rebuilt.output and record.tids["stale-running"] in rebuilt.output and "origin vm-systemd" in rebuilt.output
    again = runner.invoke(index_app, ["rebuild", "--json"])
    assert again.exit_code == 0 and '"delta": 0' in again.output and '"origin": "laptop"' in again.output
    status = runner.invoke(index_app, ["status"])
    assert status.exit_code == 0 and "index enabled: True" in status.output and "revision: 0004_history" in status.output
    assert f"trials            {len(record.tids) - 1:>7}" in status.output and "last rebuild:" in status.output and "diff none" in status.output
    for out in (before.output, migrated.output, rebuilt.output, status.output):
        assert url not in out and "index.db" not in out  # the database URL is a secret; never printed
