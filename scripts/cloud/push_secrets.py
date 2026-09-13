"""Push secret values from the operator's env file(s) into Secret Manager, idempotently, without ever printing them.

Contract
- Names: every secret in `swesim.settings.ENV_VARS` except `SWESIM_DATABASE_URL` (Terraform writes that one from the
  generated password), or an explicit `--names A,B`.
- Sources: `--env-file` (repeatable; the first file that has a key wins), read with `dotenv_values` — never sourced or
  exported — and `--modal-toml`, whose credentials table (`[default]`, else the profile with `active = true`, else the
  only profile) supplies `token_id / token_secret` as `MODAL_TOKEN_ID / MODAL_TOKEN_SECRET` when the env files do not.
- File-valued secrets: `--file NAME=PATH` (repeatable) pushes the file's exact bytes as NAME; without it, a secret in
  `FILE_PATH_KEYS` is read from the file the env files name (`GH_APP_PRIVATE_KEY` from `GH_APP_PRIVATE_KEY_PATH`) when
  the env files do not carry its content. An unreadable path counts as missing.
- Optional secrets (`OPTIONAL`, the GitHub App key) are reported `skipped (optional ...)` when absent and do not fail
  the run; every other absent value does.
- Idempotent: a new version is added only when sha256(latest payload) != sha256(new value); re-runs print "unchanged".
  The secret resource must already exist (`terraform apply` creates it); a missing resource is reported as an error.
- Output: one line per name, `NAME: updated | unchanged | missing | skipped (optional ...)`; never a value, never a
  hash, never a path's content, never gcloud's stdout. Exit status 1 when any required value is missing from the
  sources, a resource is missing, or a gcloud call fails; 2 for a bad argument (an unknown name, a `--file` that is not
  `NAME=PATH` or names no file).
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
# Secret name -> the env-file key that names a file whose bytes are the secret's value.
FILE_PATH_KEYS: dict[str, str] = {"GH_APP_PRIVATE_KEY": "GH_APP_PRIVATE_KEY_PATH"}
# Absent values that do not fail the run: the GitHub App is an optional extra quota bucket (docs/setup.md).
OPTIONAL: frozenset[str] = frozenset({"GH_APP_PRIVATE_KEY"})
Value = str | bytes

Runner = Callable[[list[str], bytes | None], subprocess.CompletedProcess]


class SecretPushError(RuntimeError):
    """A gcloud call failed. The message carries gcloud's stderr (which never contains the payload), not the value."""


def subprocess_runner(args: list[str], stdin: bytes | None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=stdin, capture_output=True, check=False)


def _read_file_bytes(path: Path) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def collect_values(env_files: Iterable[Path], modal_toml: Path | None, names: Iterable[str],
                   files: Mapping[str, Path] | None = None) -> dict[str, Value]:
    """Values for `names` found in the sources: `files` (explicit NAME=PATH, the file's bytes) first, then the env files
    (first file wins), then a file named by an env-file key in `FILE_PATH_KEYS`, then ~/.modal.toml for the Modal pair."""
    wanted = set(names)
    found: dict[str, Value] = {}
    for name, path in (files or {}).items():
        content = _read_file_bytes(path) if name in wanted else None
        if content is not None:
            found[name] = content
    paths: dict[str, str] = {}
    for path in env_files:
        if not Path(path).is_file():
            continue
        for key, value in dotenv_values(path).items():
            if key in wanted and value and key not in found:
                found[key] = value
            if key in FILE_PATH_KEYS.values() and value and key not in paths:
                paths[key] = value
    for name, path_key in FILE_PATH_KEYS.items():
        if name in wanted and name not in found and paths.get(path_key):
            content = _read_file_bytes(Path(paths[path_key]).expanduser())
            if content is not None:
                found[name] = content
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


def _payload(value: Value) -> bytes:
    return value if isinstance(value, bytes) else value.encode()


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

    def add_version(self, name: str, value: Value) -> None:
        proc = self._gcloud("secrets", "versions", "add", name, "--data-file=-", stdin=_payload(value))
        if proc.returncode != 0:
            raise SecretPushError(f"gcloud secrets versions add {name} failed: {proc.stderr.decode(errors='replace').strip()}")

    def push(self, name: str, value: Value) -> str:
        """Returns 'updated' or 'unchanged'. Raises SecretPushError when the resource is missing or gcloud fails."""
        if not self.exists(name):
            raise SecretPushError(f"secret {name} does not exist in {self.project}; run `terraform apply` first")
        if self.current_sha(name) == _sha256(_payload(value)):
            return "unchanged"
        self.add_version(name, value)
        return "updated"


def push_all(pusher: SecretPusher, values: Mapping[str, Value], names: Iterable[str], out=None) -> int:
    """Push every name; print `NAME: status` lines; return the exit status (1 if anything required was missing or failed)."""
    out = sys.stdout if out is None else out  # resolved per call, so a captured stdout (tests, Make's tee) is the one written to
    status = 0
    for name in names:
        value = values.get(name)
        if not value:
            if name in OPTIONAL:
                path_key = FILE_PATH_KEYS.get(name, "")
                hint = f"; set {path_key} in the env file or pass --file {name}=PATH" if path_key else ""
                print(f"{name}: skipped (optional{hint})", file=out)
                continue
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
    parser.add_argument("--file", action="append", default=[], metavar="NAME=PATH",
                        help="push the file's bytes as secret NAME (repeatable; e.g. GH_APP_PRIVATE_KEY=~/app.pem)")
    args = parser.parse_args(argv)
    names = tuple(n.strip() for n in args.names.split(",") if n.strip()) if args.names else DEFAULT_NAMES
    files: dict[str, Path] = {}
    for item in args.file:
        name, sep, raw = item.partition("=")
        if not sep or not name.strip() or not raw.strip():
            print(f"push_secrets: --file takes NAME=PATH, got {item!r}", file=sys.stderr)
            return 2
        path = Path(raw.strip()).expanduser()
        if not path.is_file():
            print(f"push_secrets: --file {name.strip()}: {path} is not a file", file=sys.stderr)
            return 2
        files[name.strip()] = path
    unknown = (set(names) | set(files)) - SECRET_NAMES
    if unknown:
        print(f"push_secrets: not secrets in swesim.settings: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    values = collect_values(args.env_file, args.modal_toml, names, files=files)
    return push_all(SecretPusher(args.project, run=subprocess_runner), values, names)


if __name__ == "__main__":
    sys.exit(main())
