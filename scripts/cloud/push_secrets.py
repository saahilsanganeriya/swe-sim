"""Push secret values from the operator's env file(s) into Secret Manager, idempotently, without ever printing them.

Contract
- Names: every secret in `swesim.settings.ENV_VARS` except `SWESIM_DATABASE_URL` (Terraform writes that one from the
  generated password), or an explicit `--names A,B`.
- Sources: `--env-file` (repeatable; the first file that has a key wins), read with `dotenv_values` — never sourced or
  exported — and `--modal-toml`, whose credentials table (`[default]`, else the profile with `active = true`, else the
  only profile) supplies `token_id / token_secret` as `MODAL_TOKEN_ID / MODAL_TOKEN_SECRET` when the env files do not.
- Idempotent: a new version is added only when sha256(latest payload) != sha256(new value); re-runs print "unchanged".
  The secret resource must already exist (`terraform apply` creates it); a missing resource is reported as an error.
- Output: one line per name, `NAME: updated | unchanged | missing`; never a value, never a hash, never gcloud's stdout.
  Exit status 1 when any value is missing from the sources, a resource is missing, or a gcloud call fails.
- gcloud runs as a subprocess with the value on stdin (`versions add --data-file=-`); the runner is injectable for tests.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from dotenv import dotenv_values

from swesim.settings import SECRET_NAMES

TERRAFORM_MANAGED: frozenset[str] = frozenset({"SWESIM_DATABASE_URL"})
DEFAULT_NAMES: tuple[str, ...] = tuple(sorted(SECRET_NAMES - TERRAFORM_MANAGED))
MODAL_KEYS: dict[str, str] = {"token_id": "MODAL_TOKEN_ID", "token_secret": "MODAL_TOKEN_SECRET"}

Runner = Callable[[list[str], bytes | None], subprocess.CompletedProcess]


class SecretPushError(RuntimeError):
    """A gcloud call failed. The message carries gcloud's stderr (which never contains the payload), not the value."""


def subprocess_runner(args: list[str], stdin: bytes | None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=stdin, capture_output=True, check=False)


def collect_values(env_files: Iterable[Path], modal_toml: Path | None, names: Iterable[str]) -> dict[str, str]:
    """Values for `names` found in the sources (first env file wins, then ~/.modal.toml for the Modal pair)."""
    wanted = set(names)
    found: dict[str, str] = {}
    for path in env_files:
        if not Path(path).is_file():
            continue
        for key, value in dotenv_values(path).items():
            if key in wanted and value and key not in found:
                found[key] = value
    if modal_toml is not None and Path(modal_toml).is_file():
        with open(modal_toml, "rb") as fh:
            profile = modal_profile(tomllib.load(fh))
        for toml_key, name in MODAL_KEYS.items():
            value = profile.get(toml_key)
            if name in wanted and isinstance(value, str) and value and name not in found:
                found[name] = value
    return found


def modal_profile(doc: Mapping[str, object]) -> Mapping[str, object]:
    """The credentials table of a ~/.modal.toml: `[default]` when present, else the profile marked `active = true`,
    else the only profile; an empty mapping when none applies (the Modal pair is then reported missing)."""
    tables = {k: v for k, v in doc.items() if isinstance(v, Mapping)}
    if "default" in tables:
        return tables["default"]
    active = [t for t in tables.values() if t.get("active") is True]
    if len(active) == 1:
        return active[0]
    if len(tables) == 1:
        return next(iter(tables.values()))
    return {}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SecretPusher:
    """Idempotent writes to Secret Manager through gcloud. Never logs payloads."""

    def __init__(self, project: str, run: Runner = subprocess_runner) -> None:
        self.project = project
        self.run = run

    def _gcloud(self, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
        return self.run(["gcloud", *args, "--project", self.project, "--quiet"], stdin)

    def exists(self, name: str) -> bool:
        return self._gcloud("secrets", "describe", name, stdin=None).returncode == 0

    def current_sha(self, name: str) -> str | None:
        """sha256 of the latest enabled version's payload, or None when the secret has no accessible version."""
        proc = self._gcloud("secrets", "versions", "access", "latest", "--secret", name)
        if proc.returncode != 0:
            return None
        return _sha256(proc.stdout)

    def add_version(self, name: str, value: str) -> None:
        proc = self._gcloud("secrets", "versions", "add", name, "--data-file=-", stdin=value.encode())
        if proc.returncode != 0:
            raise SecretPushError(f"gcloud secrets versions add {name} failed: {proc.stderr.decode(errors='replace').strip()}")

    def push(self, name: str, value: str) -> str:
        """Returns 'updated' or 'unchanged'. Raises SecretPushError when the resource is missing or gcloud fails."""
        if not self.exists(name):
            raise SecretPushError(f"secret {name} does not exist in {self.project}; run `terraform apply` first")
        if self.current_sha(name) == _sha256(value.encode()):
            return "unchanged"
        self.add_version(name, value)
        return "updated"


def push_all(pusher: SecretPusher, values: Mapping[str, str], names: Iterable[str], out=sys.stdout) -> int:
    """Push every name; print `NAME: status` lines; return the exit status (1 if anything was missing or failed)."""
    status = 0
    for name in names:
        value = values.get(name)
        if not value:
            print(f"{name}: missing", file=out)
            status = 1
            continue
        try:
            print(f"{name}: {pusher.push(name, value)}", file=out)
        except SecretPushError as e:
            print(f"{name}: error ({e})", file=out)
            status = 1
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True)
    parser.add_argument("--env-file", action="append", type=Path, default=[], help="dotenv file (repeatable; first wins)")
    parser.add_argument("--modal-toml", type=Path, default=None, help="~/.modal.toml for MODAL_TOKEN_ID/SECRET")
    parser.add_argument("--names", default=None, help="comma list; default: every secret in swesim.settings except SWESIM_DATABASE_URL")
    args = parser.parse_args(argv)
    names = tuple(n.strip() for n in args.names.split(",") if n.strip()) if args.names else DEFAULT_NAMES
    unknown = set(names) - SECRET_NAMES
    if unknown:
        print(f"push_secrets: not secrets in swesim.settings: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    values = collect_values(args.env_file, args.modal_toml, names)
    return push_all(SecretPusher(args.project), values, names)


if __name__ == "__main__":
    sys.exit(main())
