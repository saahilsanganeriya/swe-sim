"""Write `infra/env.auto.tfvars.json` (git-ignored) from the operator's environment: the NON-SECRET Terraform inputs only.

Contract
- Inputs: python-dotenv files (`--env-file`, repeatable; the first file that has a key wins), the operator's Google
  account (`--account`, default `gcloud config get account`), the git remote (`--repo-url`, default
  `git remote get-url origin`), `--project` (default `GCP_PROJECT` from the env files, then `gcloud config get project`)
  and `--region` (default `SWESIM_GCP_REGION` from the env files, then us-central1).
- Output keys: `project`, `region`, `temporal_address`, `temporal_namespace`, `stakeholder_url`, `iap_members`
  (`["user:<account>"]`), `evaluators` (`[<account>]`), `github_repository` (`owner/name`), `spend_cap_usd` only when
  `SWESIM_SPEND_CAP_USD` is set, `iap_programmatic_clients` only when `SWESIM_IAP_PROGRAMMATIC_CLIENTS` is set (an
  explicit opt-in for organization-owned OAuth clients; Terraform rejects any other client at plan time), and
  `gh_app_id` / `gh_app_installation_id` (strings) only when both `GH_APP_ID` and `GH_APP_INSTALLATION_ID` are set
  (one without the other is refused here). The App's private key path and content never leave the env file.
- No secret ever reaches the output: `build_tfvars` reads a fixed allow-list of keys and refuses (by construction, tested)
  every name in `swesim.settings.SECRET_NAMES`. Values are written to the file, never printed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from dotenv import dotenv_values

from swesim.settings import SECRET_NAMES

# Env-file key -> Terraform variable. Nothing outside this table is read.
NON_SECRET_KEYS: dict[str, str] = {
    "TEMPORAL_ADDRESS": "temporal_address",
    "TEMPORAL_NAMESPACE": "temporal_namespace",
    "SWESIM_STAKEHOLDER_URL": "stakeholder_url",
    "SWESIM_SPEND_CAP_USD": "spend_cap_usd",
    "SWESIM_IAP_PROGRAMMATIC_CLIENTS": "iap_programmatic_clients",
    "GH_APP_ID": "gh_app_id",
    "GH_APP_INSTALLATION_ID": "gh_app_installation_id",
}
OPTIONAL_PAIR = ("GH_APP_ID", "GH_APP_INSTALLATION_ID")  # written only when both are set
assert not (set(NON_SECRET_KEYS) & SECRET_NAMES), "tfvars_from_env must never map a secret"

_REPO_URL_RE = re.compile(r"^(?:https?://[^/]+/|git@[^:]+:|ssh://(?:[^@]+@)?[^/]+/)(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$")


class TfvarsError(ValueError):
    """Invalid or missing input (a repository URL that is not owner/name, no project id); never a transient failure."""


def repo_slug(url: str) -> str:
    """`owner/name` from an https, ssh or scp-style git remote URL."""
    m = _REPO_URL_RE.match(url.strip())
    if m is None:
        raise TfvarsError(f"cannot read owner/name from remote URL {url!r}")
    return f"{m['owner']}/{m['name']}"


def read_env_files(paths: Iterable[Path]) -> dict[str, str]:
    """Merge dotenv files; the first file that defines a key wins. Missing files are skipped, never an error."""
    merged: dict[str, str] = {}
    for path in paths:
        if not Path(path).is_file():
            continue
        for key, value in dotenv_values(path).items():
            if value and key not in merged:
                merged[key] = value
    return merged


def build_tfvars(values: Mapping[str, str], *, account: str, repo_url: str | None, project: str, region: str) -> dict:
    """The tfvars document. Pure; only allow-listed keys of `values` are consulted."""
    if not project:
        raise TfvarsError("a project id is required (--project, GCP_PROJECT in the env file, or gcloud config)")
    if not account:
        raise TfvarsError("an operator account is required (--account or `gcloud config get account`)")
    out: dict = {
        "project": project,
        "region": region,
        "iap_members": [f"user:{account}"],
        "evaluators": [account],
        "github_repository": repo_slug(repo_url) if repo_url else "",
    }
    for env_key, tf_key in NON_SECRET_KEYS.items():
        value = values.get(env_key, "")
        if tf_key == "iap_programmatic_clients":
            # Explicit opt-in (organization OAuth clients only, see infra/variables.tf): the key is written only when set,
            # so a cleared env file clears the tfvars too. Terraform refuses non-organization clients at plan time.
            clients = [item.strip() for item in value.split(",") if item.strip()]
            if clients:
                out[tf_key] = clients
        elif tf_key == "spend_cap_usd":
            if value:
                out[tf_key] = float(value)
        elif env_key in OPTIONAL_PAIR:
            if value.strip():
                out[tf_key] = value.strip()
        else:
            out[tf_key] = value
    if sum(NON_SECRET_KEYS[k] in out for k in OPTIONAL_PAIR) == 1:
        raise TfvarsError("GH_APP_ID and GH_APP_INSTALLATION_ID must be set together (or neither)")
    assert not (set(out) & SECRET_NAMES)
    return out


def _gcloud_config(key: str) -> str:
    try:
        proc = subprocess.run(["gcloud", "config", "get", key], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_origin() -> str | None:
    try:
        proc = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", action="append", type=Path, default=[], help="dotenv file (repeatable; first wins)")
    parser.add_argument("--out", type=Path, default=Path("infra/env.auto.tfvars.json"))
    parser.add_argument("--project", default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument("--account", default=None, help="Google account for IAP + evaluator access (default: gcloud config)")
    parser.add_argument("--repo-url", default=None, help="git remote URL (default: `git remote get-url origin`)")
    parser.add_argument("--stakeholder-url-file", type=Path, default=Path(".stakeholder_url"),
                        help="URL output from the Modal deploy, used when absent from the env file")
    args = parser.parse_args(argv)

    values = read_env_files(args.env_file)
    if not values.get("SWESIM_STAKEHOLDER_URL") and args.stakeholder_url_file.is_file():
        values["SWESIM_STAKEHOLDER_URL"] = args.stakeholder_url_file.read_text().strip()
    project = args.project or values.get("GCP_PROJECT") or _gcloud_config("project")
    region = args.region or values.get("SWESIM_GCP_REGION") or "us-central1"
    account = args.account or _gcloud_config("account")
    repo_url = args.repo_url or _git_origin()
    try:
        doc = build_tfvars(values, account=account, repo_url=repo_url, project=project, region=region)
    except TfvarsError as e:
        print(f"tfvars_from_env: {e}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out} ({', '.join(sorted(doc))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
