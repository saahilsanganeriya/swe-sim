"""Settings: one typed object from the environment plus the env file; secrets on demand; .env.example generated."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from swesim import settings as S


def test_defaults_are_the_cloud_only_neutral_values(tmp_path):
    s = S.Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env")})
    assert s.env_file == tmp_path / "none.env" and s.workspace == Path("./workspace") and s.registry_path == Path("./registry.json")
    assert s.task_queues == S.QUEUES and s.trial_launcher == "systemd" and s.origin == "laptop" and not s.index_enabled
    assert s.temporal_address == "localhost:7233" and not s.temporal_cloud and s.workflow_url("w") is None
    assert not any("Documents" in str(v) for v in s.model_dump().values() if isinstance(v, (str, Path)))


def test_trial_launcher_configuration_survives_environment_loading(tmp_path):
    s = S.Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "none.env"),
                            "SWESIM_WORKER_IMAGE": "registry/worker@sha256:" + "a" * 64,
                            "SWESIM_WORKER_SERVICE_ACCOUNT": "worker@example.iam.gserviceaccount.com",
                            "SWESIM_SQL_CONNECTION_NAME": "project:region:index"})
    assert s.worker_image == "registry/worker@sha256:" + "a" * 64
    assert s.worker_service_account == "worker@example.iam.gserviceaccount.com"
    assert s.sql_connection_name == "project:region:index"


def test_env_file_supplies_non_secret_keys_but_process_env_wins(tmp_path):
    f = tmp_path / ".env"
    f.write_text("TEMPORAL_ADDRESS=us-central1.gcp.api.temporal.io:7233\nTEMPORAL_NAMESPACE=swesim.abcde\nANTHROPIC_API_KEY=sk-file\n"
                 "SWESIM_EVALUATORS=A@example.com, b@example.com\nSWESIM_SPEND_CAP_USD=1500\nSWESIM_TASK_QUEUE=swesim-build\n")
    s = S.Settings.from_env({"SWESIM_ENV_FILE": str(f), "TEMPORAL_NAMESPACE": "override.ns"})
    assert s.temporal_namespace == "override.ns" and s.temporal_cloud
    assert s.workflow_url("run-1", "r9") == "https://cloud.temporal.io/namespaces/override.ns/workflows/run-1/r9"
    assert s.evaluators == frozenset({"a@example.com", "b@example.com"}) and s.spend_cap_usd == Decimal("1500")
    assert s.task_queues == ("swesim-build",)
    assert "ANTHROPIC_API_KEY" not in s.model_dump()  # secrets are never fields
    assert S.secret("ANTHROPIC_API_KEY", s, environ={}) == "sk-file"
    assert S.secret("ANTHROPIC_API_KEY", s, environ={"ANTHROPIC_API_KEY": "sk-env"}) == "sk-env"
    with pytest.raises(S.MissingSecret) as e:
        S.secret("OPENAI_API_KEY", s, environ={})
    assert "sk-" not in str(e.value) and "OPENAI_API_KEY" in str(e.value)


def test_database_url_is_a_secret_str_and_never_reprs(tmp_path):
    s = S.Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "x"), "SWESIM_DATABASE_URL": "postgresql://u:pw@h/db"})
    assert s.index_enabled and "pw" not in repr(s) and "pw" not in str(s.model_dump())
    assert s.database_url.get_secret_value().endswith("@h/db")


def test_invalid_launcher_is_rejected(tmp_path):
    with pytest.raises(Exception):
        S.Settings.from_env({"SWESIM_ENV_FILE": str(tmp_path / "x"), "SWESIM_TRIAL_LAUNCHER": "docker"})


def test_env_example_is_generated_from_the_variable_table():
    text = S.render_env_example()
    for v in S.ENV_VARS:
        assert f"\n{v.name}=" in text
    for name in S.SECRET_NAMES:
        assert f"\n{name}=\n" in text  # secrets are blank in the example
    tracked = Path(__file__).resolve().parents[1] / ".env.example"
    assert tracked.read_text() == text, "run: uv run python -c 'from swesim.settings import render_env_example as r; open(\".env.example\",\"w\").write(r())'"
