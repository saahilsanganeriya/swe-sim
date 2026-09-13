"""Contracts of scripts/cloud/push_secrets.py and scripts/cloud/tfvars_from_env.py (infra stream). Offline: gcloud is a fake."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from swesim.settings import SECRET_NAMES

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "cloud"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"cloud_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


push_secrets = _load("push_secrets")
tfvars_from_env = _load("tfvars_from_env")


class FakeSecretManager:
    """In-memory Secret Manager behind the gcloud argv the script issues. Records every call."""

    def __init__(self, existing: set[str]):
        self.versions: dict[str, list[bytes]] = {name: [] for name in existing}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], stdin: bytes | None) -> subprocess.CompletedProcess:
        self.calls.append(args)
        assert args[0] == "gcloud" and "--project" in args and "--quiet" in args
        verb = args[1:4]
        if verb[:2] == ["secrets", "describe"]:
            return _proc(0 if args[3] in self.versions else 1, b"", b"NOT_FOUND" if args[3] not in self.versions else b"")
        if verb == ["secrets", "versions", "access"]:
            name = args[args.index("--secret") + 1]
            payloads = self.versions.get(name, [])
            return _proc(0, payloads[-1], b"") if payloads else _proc(1, b"", b"NOT_FOUND: no versions")
        if verb == ["secrets", "versions", "add"]:
            name = args[4]
            assert args[5] == "--data-file=-" and stdin is not None
            self.versions[name].append(stdin)
            return _proc(0, b"Created version [1]", b"")
        raise AssertionError(f"unexpected gcloud call {args}")

    def adds(self) -> int:
        return sum(1 for c in self.calls if c[1:4] == ["secrets", "versions", "add"])


def _proc(rc: int, out: bytes, err: bytes) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


@pytest.fixture
def sources(tmp_path: Path):
    primary = tmp_path / "primary.env"
    primary.write_text("ANTHROPIC_API_KEY=anth-primary\nGH_TOKEN='gh-quoted'\nTEMPORAL_API_KEY=tk\n")
    secondary = tmp_path / "secondary.env"
    secondary.write_text("ANTHROPIC_API_KEY=anth-secondary\nOPENROUTER_API_KEY=or-key\n")
    modal = tmp_path / "modal.toml"
    modal.write_text('[default]\ntoken_id = "ak-modal"\ntoken_secret = "as-modal"\nactive = true\n')
    return primary, secondary, modal


def test_default_names_are_every_secret_except_the_terraform_managed_url():
    assert set(push_secrets.DEFAULT_NAMES) == SECRET_NAMES - {"SWESIM_DATABASE_URL"}
    assert "SWESIM_DATABASE_URL" not in push_secrets.DEFAULT_NAMES


def test_collect_values_first_env_file_wins_and_modal_toml_fills_the_gap(sources):
    primary, secondary, modal = sources
    names = ["ANTHROPIC_API_KEY", "GH_TOKEN", "OPENROUTER_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "OPENAI_API_KEY"]
    values = push_secrets.collect_values([primary, secondary, primary.with_name("absent.env")], modal, names)
    assert values["ANTHROPIC_API_KEY"] == "anth-primary"
    assert values["GH_TOKEN"] == "gh-quoted"  # dotenv strips the quotes
    assert values["OPENROUTER_API_KEY"] == "or-key"
    assert values["MODAL_TOKEN_ID"] == "ak-modal" and values["MODAL_TOKEN_SECRET"] == "as-modal"
    assert "OPENAI_API_KEY" not in values


def test_modal_toml_named_active_profile_is_used_when_there_is_no_default(tmp_path: Path):
    modal = tmp_path / "modal.toml"
    modal.write_text('[work-profile]\ntoken_id = "ak-work"\ntoken_secret = "as-work"\nactive = true\n'
                     '[other]\ntoken_id = "ak-other"\ntoken_secret = "as-other"\n')
    values = push_secrets.collect_values([], modal, ["MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"])
    assert values == {"MODAL_TOKEN_ID": "ak-work", "MODAL_TOKEN_SECRET": "as-work"}
    # default wins over active; a single profile needs no marker; two unmarked profiles resolve to nothing
    assert push_secrets.modal_profile({"default": {"token_id": "d"}, "p": {"token_id": "p", "active": True}})["token_id"] == "d"
    assert push_secrets.modal_profile({"only": {"token_id": "o"}})["token_id"] == "o"
    assert push_secrets.modal_profile({"a": {"token_id": "a"}, "b": {"token_id": "b"}}) == {}


def test_push_is_idempotent_and_adds_a_version_only_when_the_value_changes():
    names = ["ANTHROPIC_API_KEY", "GH_TOKEN"]
    fake = FakeSecretManager(existing=set(names))
    pusher = push_secrets.SecretPusher("proj", run=fake)
    out = io.StringIO()
    assert push_secrets.push_all(pusher, {"ANTHROPIC_API_KEY": "a1", "GH_TOKEN": "g1"}, names, out=out) == 0
    assert fake.adds() == 2 and fake.versions["ANTHROPIC_API_KEY"] == [b"a1"]
    assert out.getvalue().splitlines() == ["ANTHROPIC_API_KEY: updated", "GH_TOKEN: updated"]

    out = io.StringIO()
    assert push_secrets.push_all(pusher, {"ANTHROPIC_API_KEY": "a1", "GH_TOKEN": "g1"}, names, out=out) == 0
    assert fake.adds() == 2, "a re-run with the same values must not add versions"
    assert out.getvalue().splitlines() == ["ANTHROPIC_API_KEY: unchanged", "GH_TOKEN: unchanged"]

    out = io.StringIO()
    assert push_secrets.push_all(pusher, {"ANTHROPIC_API_KEY": "a2", "GH_TOKEN": "g1"}, names, out=out) == 0
    assert fake.versions["ANTHROPIC_API_KEY"] == [b"a1", b"a2"] and fake.versions["GH_TOKEN"] == [b"g1"]
    assert out.getvalue().splitlines() == ["ANTHROPIC_API_KEY: updated", "GH_TOKEN: unchanged"]


def test_output_never_contains_a_value_or_its_hash():
    fake = FakeSecretManager(existing={"OPENAI_API_KEY"})
    out = io.StringIO()
    push_secrets.push_all(push_secrets.SecretPusher("proj", run=fake), {"OPENAI_API_KEY": "sk-very-secret"}, ["OPENAI_API_KEY"], out=out)
    text = out.getvalue()
    assert "sk-very-secret" not in text and push_secrets._sha256(b"sk-very-secret") not in text
    assert text == "OPENAI_API_KEY: updated\n"


def test_missing_value_and_missing_resource_are_reported_and_fail_the_run():
    fake = FakeSecretManager(existing={"GH_TOKEN"})
    out = io.StringIO()
    rc = push_secrets.push_all(push_secrets.SecretPusher("proj", run=fake), {"OPENAI_API_KEY": "x"}, ["GH_TOKEN", "OPENAI_API_KEY"], out=out)
    assert rc == 1
    lines = out.getvalue().splitlines()
    assert lines[0] == "GH_TOKEN: missing"
    assert lines[1].startswith("OPENAI_API_KEY: error (") and "terraform apply" in lines[1]
    assert fake.adds() == 0


def test_main_refuses_names_that_are_not_secrets(capsys):
    assert push_secrets.main(["--project", "p", "--names", "SWESIM_WORKSPACE"]) == 2
    assert "not secrets" in capsys.readouterr().err


@pytest.mark.parametrize("url", [
    "https://github.com/acme/swesim.git",
    "https://github.com/acme/swesim",
    "git@github.com:acme/swesim.git",
    "ssh://git@github.com/acme/swesim.git",
])
def test_repo_slug_accepts_https_ssh_and_scp_forms(url):
    assert tfvars_from_env.repo_slug(url) == "acme/swesim"


def test_repo_slug_rejects_non_repository_urls():
    with pytest.raises(tfvars_from_env.TfvarsError):
        tfvars_from_env.repo_slug("https://example.com/")


def test_build_tfvars_has_the_terraform_inputs_and_no_secret(tmp_path: Path):
    env = tmp_path / "ops.env"
    env.write_text("TEMPORAL_ADDRESS=region.gcp.api.temporal.io:7233\nTEMPORAL_NAMESPACE=ns.abcde\n"
                   "SWESIM_STAKEHOLDER_URL=https://stakeholder.example\nTEMPORAL_API_KEY=should-never-appear\n"
                   "ANTHROPIC_API_KEY=nor-this\nGCP_PROJECT=proj-from-file\n")
    values = tfvars_from_env.read_env_files([env, tmp_path / "missing.env"])
    doc = tfvars_from_env.build_tfvars(values, account="op@example.com", repo_url="git@github.com:acme/swesim.git",
                                       project=values["GCP_PROJECT"], region="us-central1")
    assert doc == {
        "project": "proj-from-file", "region": "us-central1",
        "temporal_address": "region.gcp.api.temporal.io:7233", "temporal_namespace": "ns.abcde",
        "stakeholder_url": "https://stakeholder.example",
        "iap_members": ["user:op@example.com"], "evaluators": ["op@example.com"],
        "github_repository": "acme/swesim",
    }
    assert not (set(doc) & SECRET_NAMES)
    assert "should-never-appear" not in str(doc) and "nor-this" not in str(doc)


def test_iap_programmatic_clients_are_an_explicit_opt_in():
    """Absent or empty -> no key at all (a cleared env file clears the tfvars); set -> the list, for Terraform to vet."""
    base = dict(account="op@example.com", repo_url=None, project="p", region="r")
    assert "iap_programmatic_clients" not in tfvars_from_env.build_tfvars({}, **base)
    assert "iap_programmatic_clients" not in tfvars_from_env.build_tfvars({"SWESIM_IAP_PROGRAMMATIC_CLIENTS": " , "}, **base)
    doc = tfvars_from_env.build_tfvars({"SWESIM_IAP_PROGRAMMATIC_CLIENTS": "123-abc.apps.googleusercontent.com, 456-def.apps.googleusercontent.com"}, **base)
    assert doc["iap_programmatic_clients"] == ["123-abc.apps.googleusercontent.com", "456-def.apps.googleusercontent.com"]


def test_build_tfvars_spend_cap_only_when_set_and_project_required():
    doc = tfvars_from_env.build_tfvars({"SWESIM_SPEND_CAP_USD": "150"}, account="a@b.c", repo_url=None, project="p", region="r")
    assert doc["spend_cap_usd"] == 150.0 and doc["github_repository"] == ""
    with pytest.raises(tfvars_from_env.TfvarsError):
        tfvars_from_env.build_tfvars({}, account="a@b.c", repo_url=None, project="", region="r")
    with pytest.raises(tfvars_from_env.TfvarsError):
        tfvars_from_env.build_tfvars({}, account="", repo_url=None, project="p", region="r")


def test_main_writes_the_json_file(tmp_path: Path, capsys):
    env = tmp_path / "ops.env"
    env.write_text("TEMPORAL_ADDRESS=a:7233\nTEMPORAL_NAMESPACE=n\nSWESIM_STAKEHOLDER_URL=u\n")
    out = tmp_path / "infra" / "env.auto.tfvars.json"
    rc = tfvars_from_env.main(["--env-file", str(env), "--out", str(out), "--project", "p", "--region", "r",
                               "--account", "op@example.com", "--repo-url", "https://github.com/acme/swesim"])
    assert rc == 0 and out.is_file()
    import json
    doc = json.loads(out.read_text())
    assert doc["project"] == "p" and doc["github_repository"] == "acme/swesim" and doc["temporal_namespace"] == "n"
    assert "wrote" in capsys.readouterr().out


@pytest.mark.parametrize("state,expected", [("module.run[0].pool\n", False), ("module.storage.bucket\n", True)])
def test_bootstrap_never_disables_existing_workloads(tmp_path: Path, state: str, expected: bool):
    """Exercise the actual bootstrap recipe with fake cloud tools and no live resources."""
    fake = tmp_path / "bin"
    fake.mkdir()
    for name, body in {
        "gcloud": "exit 0",
        "terraform": 'if [ "$1" = "state" ]; then printf "%s" "$TEST_STATE"; else exit 1; fi',
    }.items():
        path = fake / name
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o755)
    override = tmp_path / "override.mk"
    calls = tmp_path / "calls"
    override.write_text("require-project tf-init tfvars open-executions deploy-stakeholder push-secrets bootstrap-namespace seed-registry pin-image write-pin migrate verify-cloud:\n\t@true\n"
                        "apply:\n\t@printf '%s\\n' '$(TF_ARGS)' >> $(CALLS)\n")
    root = SCRIPTS.parents[1]
    env = {**os.environ, "TEST_STATE": state}
    result = subprocess.run(["make", "--no-print-directory", "-f", str(root / "Makefile"), "-f", str(override),
                             "cloud-bootstrap", "PROJECT=test", f"PATH={fake}:/usr/bin:/bin", "TF=terraform",
                             f"CALLS={calls}", f"MAKE=make --no-print-directory -f {root / 'Makefile'} -f {override}"],
                            cwd=root, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("deploy_workloads=false" in calls.read_text()) is expected


@pytest.mark.parametrize("build_body", ["exit 7", "printf 'not-a-pinned-image\\n'"])
def test_ci_build_failure_never_publishes_a_deploy_digest(tmp_path: Path, build_body: str):
    import yaml

    workflow = yaml.safe_load((SCRIPTS.parents[1] / ".github/workflows/deploy.yml").read_text())
    job = workflow["jobs"]["deploy"]
    assert job["defaults"]["run"]["shell"] == "bash"
    step = next(s for s in job["steps"] if s.get("id") == "build")
    body = step["run"].replace("${{ steps.sha.outputs.sha }}", "a" * 40)
    scripts = tmp_path / "scripts/cloud"
    scripts.mkdir(parents=True)
    (scripts / "build.sh").write_text("#!/bin/bash\n" + build_body + "\n")
    output, summary = tmp_path / "output", tmp_path / "summary"
    env = {**os.environ, "GCP_PROJECT": "test", "GCP_REGION": "region", "GITHUB_OUTPUT": str(output),
           "GITHUB_STEP_SUMMARY": str(summary)}
    result = subprocess.run(["bash", "-eo", "pipefail", "-c", body], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert not output.exists(), "no digest output may reach Terraform after a build failure"


@pytest.mark.parametrize("override", [False, True])
def test_local_deploy_checks_open_workflows_before_build(tmp_path: Path, override: bool):
    fake = tmp_path / "bin"
    fake.mkdir()
    for name, body in {"gcloud": "exit 0", "uv": "printf 'TrialWorkflow\\t1\\n'"}.items():
        tool = fake / name
        tool.write_text("#!/bin/bash\n" + body + "\n")
        tool.chmod(0o755)
    calls = tmp_path / "calls"
    overrides = tmp_path / "override.mk"
    overrides.write_text("require-project apply write-pin migrate verify-cloud:\n\t@true\n"
                         "pin-image:\n\t@printf 'build\\n' >> $(CALLS)\n")
    root = SCRIPTS.parents[1]
    result = subprocess.run(["make", "--no-print-directory", "-f", str(root / "Makefile"), "-f", str(overrides),
                             "deploy", "PROJECT=test", "UV=uv", f"PATH={fake}:/usr/bin:/bin", f"CALLS={calls}",
                             f"EVIDENCE_DIR={tmp_path / 'evidence'}", f"OVERRIDE_OPEN_EXECUTIONS={int(override)}",
                             f"MAKE=make --no-print-directory -f {root / 'Makefile'} -f {overrides}"],
                            cwd=root, capture_output=True, text=True)
    assert (result.returncode == 0) is override, result.stdout + result.stderr
    assert calls.exists() is override
    evidence = next((tmp_path / "evidence").glob("open-types-*.txt")).read_text()
    assert "TrialWorkflow\t1" in evidence
    assert ("Explicit operator override" in evidence) is override


@pytest.mark.parametrize("target", ["apply", "open-executions"])
def test_make_propagates_cloud_failures_with_legacy_make(tmp_path: Path, target: str):
    fake = tmp_path / "bin"
    fake.mkdir()
    for name, body in {"gcloud": "exit 0", "terraform": "echo cloud-failed >&2; exit 7", "uv": "exit 9"}.items():
        tool = fake / name
        tool.write_text("#!/bin/bash\n" + body + "\n")
        tool.chmod(0o755)
    after = tmp_path / "after"
    override = tmp_path / "override.mk"
    override.write_text("outputs:\n\t@touch $(AFTER)\n")
    root = SCRIPTS.parents[1]
    result = subprocess.run(["make", "--no-print-directory", "-f", str(root / "Makefile"), "-f", str(override), target,
                             "PROJECT=test", "UV=uv", "TF=terraform", f"PATH={fake}:/usr/bin:/bin", f"AFTER={after}",
                             f"EVIDENCE_DIR={tmp_path / 'evidence'}",
                             f"MAKE=make --no-print-directory -f {root / 'Makefile'} -f {override}"],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not after.exists(), "a failed apply cannot continue as a successful deployment"


# ----------------------------------------------------------------------------------------------- pin guard (F4)
pin_guard = _load("pin_guard")

DIGEST_A = "r/worker@sha256:" + "a" * 64
DIGEST_B = "r/worker@sha256:" + "b" * 64
TAG = "r/worker:latest"


def _running(image: str, console: str | None = None) -> dict[str, str]:
    out = {f"pool/swesim-{q}": image for q in ("authoring", "build", "trials", "control")}
    out["service/swesim-console"] = console or image
    return out


@pytest.mark.parametrize("pin,running,ok,code", [
    (None, {}, True, "no-workloads"),                         # bootstrap: nothing deployed
    (None, _running(TAG), True, "unpinned"),                  # bootstrap apply rolled :latest, no pin yet
    (DIGEST_A, _running(DIGEST_A), True, "match"),
    (None, _running(DIGEST_A), False, "pin-missing"),         # fresh clone: apply would roll the pools to :latest
    (DIGEST_A, _running(DIGEST_B), False, "mismatch"),        # stale laptop pin after a CI deploy (F4)
    (DIGEST_B, _running(DIGEST_A), False, "mismatch"),        # a new build not yet applied
    (DIGEST_A, _running(DIGEST_A, console=DIGEST_B), False, "mixed"),
    (DIGEST_A, None, False, "unknown"),
    (DIGEST_A, _running(pin_guard.UNKNOWN), False, "unknown"),
])
def test_pin_guard_decides_from_the_pin_and_the_running_images(pin, running, ok, code):
    d = pin_guard.decide(pin, running)
    assert (d.ok, d.code) == (ok, code), d.message
    if not ok:
        assert "pull-pin" in d.message or "gcloud" in d.message or "roll back" in d.message
        forced = pin_guard.decide(pin, running, force=True)
        assert forced.ok and forced.code == "forced" and code in forced.message


def test_pin_guard_unknown_and_empty_are_distinct():
    assert pin_guard.decide(DIGEST_A, {}).code == "no-workloads"
    assert pin_guard.decide(DIGEST_A, None).code == "unknown"
    assert pin_guard.decide(DIGEST_A, {}).ok and not pin_guard.decide(DIGEST_A, None).ok


def test_pin_file_round_trip_and_refusals(tmp_path: Path):
    pin = tmp_path / "image.auto.tfvars.json"
    assert pin_guard.read_pin(pin) is None
    pin_guard.write_pin(pin, DIGEST_A)
    assert pin_guard.read_pin(pin) == DIGEST_A
    assert json.loads(pin.read_text()) == {"image": DIGEST_A}
    with pytest.raises(ValueError):
        pin_guard.write_pin(pin, TAG)                          # a failed build can never pin a tag
    with pytest.raises(ValueError):
        pin_guard.write_pin(pin, "")
    pin.write_text('{"other": 1}\n')
    with pytest.raises(ValueError):
        pin_guard.read_pin(pin)                                # malformed is an error, not "no pin"


def _gcloud_json(names_images: dict[str, str]) -> str:
    return json.dumps([{"metadata": {"name": n}, "spec": {"template": {"spec": {"containers": [{"image": i}]}}}} for n, i in names_images.items()])


def test_pin_guard_parses_gcloud_lists_and_ignores_foreign_workloads():
    pools = _gcloud_json({"swesim-trials": DIGEST_A, "other-pool": TAG, "swesim-build": DIGEST_A})
    services = _gcloud_json({"swesim-console": DIGEST_A, "unrelated": TAG})
    assert pin_guard.parse_workloads(pools, services, console="swesim-console") == {
        "pool/swesim-trials": DIGEST_A, "pool/swesim-build": DIGEST_A, "service/swesim-console": DIGEST_A}
    assert pin_guard.parse_workloads("", "", console="swesim-console") == {}
    assert pin_guard.parse_workloads(json.dumps([{"metadata": {"name": "swesim-x"}}]), "[]", console="c") == {"pool/swesim-x": pin_guard.UNKNOWN}


def test_pin_guard_running_images_is_none_when_gcloud_fails():
    def failing(args):
        return _proc(1, b"", b"ERROR: (gcloud.run.worker-pools.list) PERMISSION_DENIED")
    assert pin_guard.running_images("p", "r", console="c", run=failing) is None
    calls: list[list[str]] = []

    def ok(args):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="[]", stderr="")
    assert pin_guard.running_images("p", "r", console="c", run=ok) == {}
    assert [c[2:4] for c in calls] == [["worker-pools", "list"], ["services", "list"]]
    assert all("--project" in c and "p" in c and "--format=json" in c for c in calls)


def test_pin_guard_main_exit_codes(tmp_path: Path, capsys):
    pin = tmp_path / "image.auto.tfvars.json"
    pin_guard.write_pin(pin, DIGEST_A)

    def running_b(args):
        body = _gcloud_json({"swesim-trials": DIGEST_B}) if args[2] == "worker-pools" else "[]"
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=body, stderr="")
    common = ["--pin-file", str(pin), "check", "--project", "p", "--region", "r"]
    assert pin_guard.main(common, run=running_b) == 1
    assert "pin-guard: mismatch" in capsys.readouterr().err
    assert pin_guard.main([*common, "--force"], run=running_b) == 0
    assert "pin-guard: forced" in capsys.readouterr().out
    assert pin_guard.main(["--pin-file", str(pin), "write", "--image", TAG]) == 2
    assert pin_guard.read_pin(pin) == DIGEST_A, "a refused write leaves the pin untouched"
    assert pin_guard.main(["--pin-file", str(pin), "write", "--image", DIGEST_B]) == 0
    assert pin_guard.read_pin(pin) == DIGEST_B


@pytest.mark.parametrize("force,applies", [(0, False), (1, True)])
def test_make_apply_refuses_a_stale_pin_unless_forced(tmp_path: Path, force: int, applies: bool):
    """The real `apply` recipe with fake cloud tools: pin A on disk, the pools run B."""
    fake = tmp_path / "bin"
    fake.mkdir()
    pools = _gcloud_json({"swesim-trials": DIGEST_B, "swesim-build": DIGEST_B})
    (fake / "gcloud").write_text("#!/bin/bash\ncase \"$*\" in *worker-pools*list*) printf '%s' \"$POOLS\" ;; *services*list*) printf '[]' ;; *) exit 0 ;; esac\n")
    (fake / "terraform").write_text("#!/bin/bash\ntouch \"$TF_CALLED\"\n")
    for tool in ("gcloud", "terraform"):
        (fake / tool).chmod(0o755)
    pin = tmp_path / "image.auto.tfvars.json"
    pin_guard.write_pin(pin, DIGEST_A)
    called = tmp_path / "tf-called"
    override = tmp_path / "override.mk"
    override.write_text("outputs:\n\t@true\n")
    root = SCRIPTS.parents[1]
    env = {**os.environ, "POOLS": pools, "TF_CALLED": str(called)}
    result = subprocess.run(["make", "--no-print-directory", "-f", str(root / "Makefile"), "-f", str(override), "apply",
                             "PROJECT=test", "TF=terraform", f"PIN_FILE={pin}", f"FORCE_PIN={force}", f"PATH={fake}:/usr/bin:/bin",
                             f"EVIDENCE_DIR={tmp_path / 'evidence'}", f"MAKE=make --no-print-directory -f {root / 'Makefile'} -f {override}"],
                            cwd=root, env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is applies, result.stdout + result.stderr
    assert called.exists() is applies, "terraform apply must not run while the pin disagrees with the pools"
    assert ("pin-guard: mismatch" in result.stderr) is not applies
    assert ("pin-guard: forced" in result.stdout) is applies


def test_make_deploy_forces_its_own_pin_and_writes_it_back(tmp_path: Path):
    """`make deploy` passes FORCE_PIN=1 to its apply (it just built the pin) and rewrites the pin from Terraform afterwards."""
    fake = tmp_path / "bin"
    fake.mkdir()
    for name, body in {"gcloud": "exit 0", "uv": "exit 0"}.items():
        (fake / name).write_text("#!/bin/bash\n" + body + "\n")
        (fake / name).chmod(0o755)
    calls = tmp_path / "calls"
    override = tmp_path / "override.mk"
    override.write_text("require-project open-executions pin-image migrate verify-cloud:\n\t@true\n"
                        "apply:\n\t@printf 'apply FORCE_PIN=%s\\n' '$(FORCE_PIN)' >> $(CALLS)\n"
                        "write-pin:\n\t@printf 'write-pin\\n' >> $(CALLS)\n")
    root = SCRIPTS.parents[1]
    result = subprocess.run(["make", "--no-print-directory", "-f", str(root / "Makefile"), "-f", str(override), "deploy",
                             "PROJECT=test", f"PATH={fake}:/usr/bin:/bin", f"CALLS={calls}",
                             f"MAKE=make --no-print-directory -f {root / 'Makefile'} -f {override}"],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().splitlines() == ["apply FORCE_PIN=1", "write-pin"]


# ----------------------------------------------------------------------------------------------- namespace bootstrap (P2-14)
bootstrap_namespace = _load("bootstrap_namespace")


class FakeOperatorService:
    """ListSearchAttributes/AddSearchAttributes as a namespace would answer them; `refuse` makes add (or both) raise."""

    def __init__(self, present: set[str], refuse_add=None, refuse_list=None):
        self.present = set(present)
        self.refuse_add, self.refuse_list = refuse_add, refuse_list
        self.added: list[dict] = []

    async def list_search_attributes(self, request):
        if self.refuse_list is not None:
            raise self.refuse_list
        from types import SimpleNamespace
        return SimpleNamespace(custom_attributes={n: 1 for n in self.present}, system_attributes={"WorkflowType": 1})

    async def add_search_attributes(self, request):
        if self.refuse_add is not None:
            raise self.refuse_add
        self.added.append(dict(request.search_attributes))
        self.present |= set(request.search_attributes)


class FakeClient:
    """`count_workflows` answers like visibility: INVALID_ARGUMENT for an unregistered attribute, 0 for a registered one."""

    def __init__(self, ops: FakeOperatorService, namespace: str = "swesim.test", query_error=None):
        self.operator_service, self.namespace, self.query_error = ops, namespace, query_error
        self.queries: list[str] = []

    async def count_workflows(self, query: str) -> int:
        from temporalio.service import RPCStatusCode
        self.queries.append(query)
        if self.query_error is not None:
            raise self.query_error
        if query.split(" ")[0] not in self.operator_service.present:
            raise _rpc(RPCStatusCode.INVALID_ARGUMENT)
        return 0


def _rpc(status):
    from temporalio.service import RPCError
    return RPCError("refused by the namespace", status, b"")


def test_bootstrap_namespace_is_idempotent():
    import asyncio
    from swesim.workflows.search_attributes import KEYS
    ops = FakeOperatorService(present=set(KEYS))
    outcome = asyncio.run(bootstrap_namespace.bootstrap(FakeClient(ops)))
    assert outcome.code == 0 and "all 6 present" in outcome.summary and ops.added == []


def test_bootstrap_namespace_registers_only_the_missing_keywords():
    import asyncio
    from temporalio.api.enums.v1 import IndexedValueType
    from swesim.workflows.search_attributes import KEYS
    ops = FakeOperatorService(present={"swesim_scenario_id", "swesim_model"})
    outcome = asyncio.run(bootstrap_namespace.bootstrap(FakeClient(ops), "swesim.abcde"))
    assert outcome.code == 0
    assert set(outcome.attributes) == set(KEYS) - {"swesim_scenario_id", "swesim_model"}
    assert len(ops.added) == 1 and set(ops.added[0]) == set(outcome.attributes)
    assert set(ops.added[0].values()) == {IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD}
    assert asyncio.run(bootstrap_namespace.bootstrap(FakeClient(ops), "swesim.abcde")).code == 0 and len(ops.added) == 1


@pytest.mark.parametrize("status_name", ["PERMISSION_DENIED", "UNIMPLEMENTED"])
def test_bootstrap_namespace_prints_the_exact_operator_step_when_cloud_refuses(status_name: str):
    import asyncio
    from temporalio.service import RPCStatusCode
    ops = FakeOperatorService(present={"swesim_status"}, refuse_add=_rpc(getattr(RPCStatusCode, status_name)))
    outcome = asyncio.run(bootstrap_namespace.bootstrap(FakeClient(ops, "swesim.abcde")))
    assert outcome.code == 2 and "swesim_status" not in outcome.attributes and len(outcome.attributes) == 5
    lines: list[str] = []
    assert bootstrap_namespace.report(outcome, "swesim.abcde", out=lines.append) == 2
    text = "\n".join(lines)
    assert "tcld namespace search-attributes add --namespace swesim.abcde" in text
    assert "--search-attribute swesim_purpose=Keyword" in text and "swesim_status=Keyword" not in text
    assert "Custom Search Attributes" in text and status_name in text


def test_bootstrap_namespace_verifies_by_visibility_probe_when_cloud_refuses_even_listing():
    """Temporal Cloud (measured): ListSearchAttributes is PERMISSION_DENIED too; the probe decides, read-only."""
    import asyncio
    from temporalio.service import RPCStatusCode
    from swesim.workflows.search_attributes import KEYS
    denied = _rpc(RPCStatusCode.PERMISSION_DENIED)
    present = FakeClient(FakeOperatorService(present=set(KEYS), refuse_add=denied, refuse_list=denied), "swesim.abcde")
    outcome = asyncio.run(bootstrap_namespace.bootstrap(present))
    assert outcome.code == 0 and "verified by visibility" in outcome.summary and len(present.queries) == 6
    partial = FakeClient(FakeOperatorService(present={"swesim_model"}, refuse_add=denied, refuse_list=denied))
    outcome = asyncio.run(bootstrap_namespace.bootstrap(partial))
    assert outcome.code == 2 and set(outcome.attributes) == set(KEYS) - {"swesim_model"} and "5 missing" in outcome.summary
    unverifiable = FakeClient(FakeOperatorService(present=set(), refuse_add=denied, refuse_list=denied), query_error=_rpc(RPCStatusCode.UNAVAILABLE))
    outcome = asyncio.run(bootstrap_namespace.bootstrap(unverifiable))
    assert outcome.code == 2 and outcome.attributes == KEYS and "6 unverifiable" in outcome.summary and "0 missing" in outcome.summary


def test_bootstrap_namespace_other_rpc_errors_are_failures_not_operator_steps():
    import asyncio
    from temporalio.service import RPCStatusCode
    ops = FakeOperatorService(present=set(), refuse_list=_rpc(RPCStatusCode.UNAVAILABLE))
    outcome = asyncio.run(bootstrap_namespace.bootstrap(FakeClient(ops)))
    assert outcome.code == 1 and "UNAVAILABLE" in outcome.summary and outcome.attributes == ()
    lines: list[str] = []
    assert bootstrap_namespace.report(outcome, "ns", out=lines.append) == 1 and len(lines) == 1
