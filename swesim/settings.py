"""Typed configuration: one `Settings` built from the environment once, passed down, never re-read mid-function.

Contract
- Non-secret configuration is a field on `Settings` (strict Pydantic, `extra="forbid"`). Every field maps to exactly one
  environment variable listed in `ENV_VARS`, which is also the single source of `.env.example`.
- Secrets are never `Settings` fields except `database_url`, which is a `SecretStr` because it embeds a password. Read any
  other secret on demand with `secret(name)`; it is never logged, never put in a payload, never exported.
- Resolution order for every variable: the process environment, then the python-dotenv file named by `SWESIM_ENV_FILE`
  (default `./.env`, git-ignored). The file is parsed with `dotenv_values`; nothing is sourced or exported.
- `get_settings()` caches one instance per process; tests call `reset_settings()` after changing the environment.
"""
from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr

Launcher = Literal["systemd", "cloudrun", "subprocess"]
Origin = Literal["laptop", "vm-systemd", "cloudrun"]
QUEUES = ("swesim-authoring", "swesim-build", "swesim-trials", "swesim-control")
TEMPORAL_CLOUD_SUFFIXES = (".tmprl.cloud:7233", ".api.temporal.io:7233")


class MissingSecret(RuntimeError):
    """A required credential is neither in the process environment nor in the env file. Never carries the value."""


class EnvVar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    purpose: str
    secret: bool = False
    default: str | None = None
    where: str = ""  # where a stranger obtains the value


ENV_VARS: tuple[EnvVar, ...] = (
    EnvVar(name="SWESIM_ENV_FILE", purpose="Path of this file; set it in the process environment, not here.", default="./.env"),
    EnvVar(name="SWESIM_WORKSPACE", purpose="Record root: local directory or the mounted bucket (/workspace on Cloud Run).", default="./workspace"),
    EnvVar(name="SWESIM_REGISTRY", purpose="Path of registry.json (the file record of valid scenarios).", default="./registry.json"),
    EnvVar(name="SWESIM_BUCKET", purpose="GCS bucket name that holds the record (no gs:// prefix). Empty = files only.", where="Terraform output `bucket`."),
    EnvVar(name="SWESIM_DATABASE_URL", purpose="SQLAlchemy URL of the Postgres index; empty disables the index.", secret=True,
           where="Terraform output `database_url_template` plus the DB_PASSWORD secret; on Cloud Run the Cloud SQL unix socket URL."),
    EnvVar(name="SWESIM_TASK_QUEUE", purpose="Comma list of Temporal task queues this worker polls; `all` for every queue.", default="all"),
    EnvVar(name="SWESIM_MAX_CONCURRENT_ACTIVITIES", purpose="Override of the per-queue activity concurrency (swesim/workflows/queues.py)."),
    EnvVar(name="SWESIM_TRIAL_LAUNCHER", purpose="systemd (VM) | cloudrun (production) | subprocess (local smoke only).", default="systemd"),
    EnvVar(name="SWESIM_ORIGIN", purpose="Label written into every record: laptop | vm-systemd | cloudrun.", default="laptop"),
    EnvVar(name="SWESIM_WORKER_IMAGE_DIGEST", purpose="Digest of the running worker image; set by the deploy, recorded in every result.", where="Set by Terraform on the worker pools and Jobs."),
    EnvVar(name="SWESIM_GCP_PROJECT", purpose="GCP project id (Cloud Run Jobs, Secret Manager).", where="`gcloud config get project`."),
    EnvVar(name="SWESIM_GCP_REGION", purpose="GCP region for Cloud Run Jobs.", default="us-central1"),
    EnvVar(name="SWESIM_WORKER_IMAGE", purpose="Full worker image reference with digest (<repo>/worker@sha256:...) used for trial Jobs.", where="Printed by `make build`; set by Terraform on the pools."),
    EnvVar(name="SWESIM_WORKER_SERVICE_ACCOUNT", purpose="Service account email the trial Jobs run as.", where="Terraform output `worker_service_account`."),
    EnvVar(name="SWESIM_SQL_CONNECTION_NAME", purpose="Cloud SQL connection mounted in trial and maintenance Jobs.", where="Terraform output `sql_connection_name`."),
    EnvVar(name="SWESIM_IAP_AUDIENCE", purpose="IAP JWT audience of the console service (/projects/<num>/locations/<region>/services/<name>); empty = no IAP.", where="Terraform output `iap_audience`."),
    EnvVar(name="SWESIM_IAP_PROGRAMMATIC_CLIENTS", purpose="Optional comma-separated OAuth client IDs for authenticated console automation; IAP still checks user access.", where="Your OAuth client ID or the audience of your Google Cloud CLI identity token."),
    EnvVar(name="SWESIM_EVALUATORS", purpose="Comma list of Google account emails allowed to see evaluator-only material in the console.", where="Your own IAP-allowlisted accounts."),
    EnvVar(name="SWESIM_SPEND_CAP_USD", purpose="RunTasksWorkflow stops planning new trials when recorded spend exceeds this (USD)."),
    EnvVar(name="SWESIM_STAKEHOLDER_URL", purpose="Base URL of the stakeholder service deployed to Modal.", where="Printed by `bash scripts/deploy_stakeholder.sh`."),
    EnvVar(name="SWESIM_CONSOLE_URL", purpose="Public URL of the console (used in links and notifications).", where="Terraform output `console_url`."),
    EnvVar(name="SWESIM_TEMPORAL_UI_URL", purpose="Base URL of the Temporal UI for deep links; derived for Temporal Cloud when empty."),
    EnvVar(name="TEMPORAL_ADDRESS", purpose="Temporal gRPC endpoint.", default="localhost:7233", where="Temporal Cloud namespace page, e.g. us-central1.gcp.api.temporal.io:7233."),
    EnvVar(name="TEMPORAL_NAMESPACE", purpose="Temporal namespace.", default="default", where="Temporal Cloud namespace page, e.g. swesim.abcde."),
    EnvVar(name="TEMPORAL_API_KEY", purpose="Temporal Cloud API key (TLS on when present).", secret=True, where="Temporal Cloud > Settings > API keys, or `temporal cloud apikey create`."),
    EnvVar(name="SWESIM_WORKER_SECRET", purpose="HMAC secret shared by the worker and the stakeholder service (any 32+ random bytes, hex).", secret=True, where="`python -c 'import secrets; print(secrets.token_hex(32))'`."),
    EnvVar(name="ANTHROPIC_API_KEY", purpose="Anthropic API key (judge, drafter, stakeholder, Anthropic solvers).", secret=True, where="console.anthropic.com."),
    EnvVar(name="OPENAI_API_KEY", purpose="OpenAI API key (fallback roles, OpenAI solvers).", secret=True, where="platform.openai.com."),
    EnvVar(name="OPENROUTER_API_KEY", purpose="OpenRouter API key (open-model solvers).", secret=True, where="openrouter.ai/keys."),
    EnvVar(name="GH_TOKEN", purpose="Read-only GitHub token for repository discovery (public_repo scope is enough). Optional extra buckets: GH_TOKEN_2, GH_TOKEN_3, ... (see SWESIM_GH_EXTRA_TOKENS).", secret=True, where="`gh auth token` or github.com/settings/tokens."),
    EnvVar(name="SWESIM_GH_EXTRA_TOKENS", purpose="How many optional GH_TOKEN_n (n = 2..) discovery looks for in this file; each is another 5,000/h quota bucket (laptop only, not in Secret Manager).", default="3"),
    EnvVar(name="GH_APP_ID", purpose="Optional GitHub App whose installation token is one more 5,000/h discovery bucket next to GH_TOKEN; empty = no App.", where="github.com/settings/apps > your app > App ID (docs/setup.md, GitHub App)."),
    EnvVar(name="GH_APP_INSTALLATION_ID", purpose="Installation id of that App on the account whose repositories are mined.", where="github.com/settings/installations > Configure: the number at the end of the URL."),
    EnvVar(name="GH_APP_PRIVATE_KEY_PATH", purpose="Path of the App's private key (.pem) on this machine; hosted workers read the content from the GH_APP_PRIVATE_KEY secret instead.", where="App settings > Private keys > Generate; store it mode 600 outside the repository."),
    EnvVar(name="GH_APP_PRIVATE_KEY", purpose="Content of the App's private key (PEM); pushed from GH_APP_PRIVATE_KEY_PATH by `make push-secrets`, read by the hosted pools.", secret=True, where="`make push-secrets` copies the file named by GH_APP_PRIVATE_KEY_PATH; leave it empty here."),
    EnvVar(name="MODAL_TOKEN_ID", purpose="Modal token id (Harbor's Modal backend, stakeholder deploy).", secret=True, where="modal.com/settings or ~/.modal.toml."),
    EnvVar(name="MODAL_TOKEN_SECRET", purpose="Modal token secret.", secret=True, where="modal.com/settings or ~/.modal.toml."),
)
SECRET_NAMES: frozenset[str] = frozenset(v.name for v in ENV_VARS if v.secret)
FILE_KEYS: frozenset[str] = frozenset(v.name for v in ENV_VARS if not v.secret) - {"SWESIM_ENV_FILE"}


class Settings(BaseModel):
    """Non-secret configuration of one process. Built once by `Settings.from_env`; immutable afterwards."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    env_file: Path
    workspace: Path
    registry_path: Path
    bucket: str | None = None
    database_url: SecretStr | None = None
    task_queues: tuple[str, ...] = QUEUES
    max_concurrent_activities: int | None = Field(default=None, ge=1)
    trial_launcher: Launcher = "systemd"
    origin: Origin = "laptop"
    worker_image_digest: str | None = None
    gcp_project: str | None = None
    gcp_region: str = "us-central1"
    worker_image: str | None = None
    worker_service_account: str | None = None
    sql_connection_name: str | None = None
    iap_audience: str | None = None
    iap_programmatic_clients: tuple[str, ...] = ()
    evaluators: frozenset[str] = frozenset()
    spend_cap_usd: Decimal | None = Field(default=None, ge=0)
    stakeholder_url: str = ""
    console_url: str | None = None
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_ui_url: str | None = None
    direct_stages: bool = False
    gh_app_id: int | None = None
    gh_app_installation_id: int | None = None
    gh_app_private_key_path: Path | None = None
    gh_extra_tokens: int = Field(default=3, ge=0)

    @property
    def index_enabled(self) -> bool:
        return self.database_url is not None

    @property
    def temporal_cloud(self) -> bool:
        return self.temporal_address.endswith(TEMPORAL_CLOUD_SUFFIXES)

    def workflow_url(self, workflow_id: str, run_id: str | None = None) -> str | None:
        """Deep link to one workflow execution in the Temporal UI (Cloud or a self-hosted UI); None when no UI is known."""
        base = self.temporal_ui_url
        if base is None and self.temporal_cloud:
            base = f"https://cloud.temporal.io/namespaces/{self.temporal_namespace}"
        if base is None:
            return None
        url = f"{base.rstrip('/')}/workflows/{workflow_id}"
        return f"{url}/{run_id}" if run_id else url

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Build from the process environment plus the env file for the non-secret keys in `FILE_KEYS`.
        The env file's `SWESIM_DATABASE_URL` is also honoured because a local operator keeps it there."""
        env = dict(os.environ if environ is None else environ)
        env_file = Path(env.get("SWESIM_ENV_FILE") or "./.env").expanduser()
        for key, value in _file_values(env_file).items():
            if (key in FILE_KEYS or key == "SWESIM_DATABASE_URL") and value and key not in env:
                env[key] = value
        queues = env.get("SWESIM_TASK_QUEUE", "all").strip()
        workspace = Path(env.get("SWESIM_WORKSPACE") or "./workspace").expanduser()
        return cls(
            env_file=env_file,
            workspace=workspace,
            registry_path=Path(env.get("SWESIM_REGISTRY") or "./registry.json").expanduser(),
            bucket=env.get("SWESIM_BUCKET") or None,
            database_url=SecretStr(env["SWESIM_DATABASE_URL"]) if env.get("SWESIM_DATABASE_URL") else None,
            task_queues=QUEUES if queues in ("", "all") else tuple(q.strip() for q in queues.split(",") if q.strip()),
            max_concurrent_activities=int(env["SWESIM_MAX_CONCURRENT_ACTIVITIES"]) if env.get("SWESIM_MAX_CONCURRENT_ACTIVITIES") else None,
            trial_launcher=env.get("SWESIM_TRIAL_LAUNCHER") or "systemd",  # type: ignore[arg-type]  (validated by Pydantic)
            origin=env.get("SWESIM_ORIGIN") or "laptop",  # type: ignore[arg-type]
            worker_image_digest=env.get("SWESIM_WORKER_IMAGE_DIGEST") or None,
            gcp_project=env.get("SWESIM_GCP_PROJECT") or None,
            gcp_region=env.get("SWESIM_GCP_REGION") or "us-central1",
            worker_image=env.get("SWESIM_WORKER_IMAGE") or None,
            worker_service_account=env.get("SWESIM_WORKER_SERVICE_ACCOUNT") or None,
            sql_connection_name=env.get("SWESIM_SQL_CONNECTION_NAME") or None,
            iap_audience=env.get("SWESIM_IAP_AUDIENCE") or None,
            iap_programmatic_clients=tuple(x.strip() for x in env.get("SWESIM_IAP_PROGRAMMATIC_CLIENTS", "").split(",") if x.strip()),
            evaluators=frozenset(e.strip().lower() for e in env.get("SWESIM_EVALUATORS", "").split(",") if e.strip()),
            spend_cap_usd=Decimal(env["SWESIM_SPEND_CAP_USD"]) if env.get("SWESIM_SPEND_CAP_USD") else None,
            stakeholder_url=env.get("SWESIM_STAKEHOLDER_URL", ""),
            console_url=env.get("SWESIM_CONSOLE_URL") or None,
            temporal_address=env.get("TEMPORAL_ADDRESS") or "localhost:7233",
            temporal_namespace=env.get("TEMPORAL_NAMESPACE") or "default",
            temporal_ui_url=env.get("SWESIM_TEMPORAL_UI_URL") or None,
            direct_stages=env.get("SWESIM_DIRECT") == "1",
            gh_app_id=int(env["GH_APP_ID"]) if env.get("GH_APP_ID") else None,
            gh_app_installation_id=int(env["GH_APP_INSTALLATION_ID"]) if env.get("GH_APP_INSTALLATION_ID") else None,
            gh_app_private_key_path=Path(env["GH_APP_PRIVATE_KEY_PATH"]).expanduser() if env.get("GH_APP_PRIVATE_KEY_PATH") else None,
            gh_extra_tokens=int(env["SWESIM_GH_EXTRA_TOKENS"]) if env.get("SWESIM_GH_EXTRA_TOKENS") else 3,
        )


def _file_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    from dotenv import dotenv_values
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def secret(name: str, settings: Settings | None = None, environ: Mapping[str, str] | None = None) -> str:
    """One credential, read on demand: process environment first, then the env file. Raises `MissingSecret` (without the
    value) when absent. Callers never log, print or serialise the result."""
    env = os.environ if environ is None else environ
    value = env.get(name)
    if not value:
        path = (settings or get_settings()).env_file
        value = _file_values(path).get(name)
        if not value:
            raise MissingSecret(f"{name} is not set in the environment or in {path}")
    return value


def child_environment(**overrides: str) -> dict[str, str]:
    """A copy of the process environment with `overrides` applied, for a subprocess that must receive one credential
    through its environment (never on its command line, where `ps` would show it). The process environment is not
    modified. This is the sanctioned place for that `os.environ` read (the module contract above)."""
    return {**os.environ, **overrides}


def has_secret(name: str, settings: Settings | None = None) -> bool:
    try:
        secret(name, settings)
        return True
    except MissingSecret:
        return False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def reset_settings() -> None:
    """Drop the cached instance (tests, or after the CLI sets SWESIM_* for child processes)."""
    get_settings.cache_clear()


def render_env_example() -> str:
    """The tracked `.env.example`, generated from `ENV_VARS` so the two can never disagree (tests/test_settings.py)."""
    lines = ["# swesim environment file. Copy to ./.env (git-ignored) or point SWESIM_ENV_FILE at your copy.",
             "# Read with python-dotenv; never `export $(cat ...)` or `source` it. Secrets are marked; keep them out of git.", ""]
    for v in ENV_VARS:
        lines.append(f"# {v.purpose}" + (" [secret]" if v.secret else ""))
        if v.where:
            lines.append(f"#   where: {v.where}")
        lines.append(f"{v.name}={'' if v.secret or v.default is None else v.default}")
        lines.append("")
    return "\n".join(lines)
