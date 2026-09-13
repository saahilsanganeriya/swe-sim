"""Compile an approved ScenarioSpec into three Harbor 0.23 task directories (full, hidden, interactive).

The rendered tasks reproduce the Modal recipe verified in references/modal-smoke-task: strict allowlist baseline,
`[agent] user = "agent"`, pre-baked uv toolchain for mini-swe-agent, root `[[verifier.collect]]` hook writing the
root-only /var/swesim-collect artifacts, and a separate offline verifier image.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import stat
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from swesim.models import Discovery, RunConfig, ScenarioSpec
from swesim.paths import Workspace, atomic_json
from swesim.roles import PROVIDERS

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
# Owned by the verifier and stakeholder streams; tests monkeypatch these to placeholder trees.
VERIFIER_SRC = Path(__file__).resolve().parent.parent / "verifier"
SHIMS = Path(__file__).resolve().parent.parent / "stakeholder" / "shims"
VERIFIER_FILES = ("grader.py", "policy.py", "parsers.py", "run_pytest.py")
SHIM_FILES = ("ask-user", "confirm-behavior", "request-review")
# Provider API hosts the sandbox allowlist carries, from the one provider table (`swesim.roles.PROVIDERS`: anthropic, openai, openrouter).
MODEL_HOSTS = {name: [p.host] for name, p in PROVIDERS.items()}
AGENT_PYTHON = "3.12"  # Harbor's mini-swe-agent adapter pin (_MINI_SWE_AGENT_PYTHON_VERSION)
CONDITIONS = ("full", "hidden", "interactive")
VERIFIER_TIMEOUT_SEC = 1800.0
TEST_TIMEOUT_SEC = 600  # per pytest invocation inside the grader; the verifier budget above covers the reruns
# [[verifier.collect]] hook budget: two 240 s freeze attempts (collect.py FREEZE_*) plus killing and archiving the tree.
COLLECT_TIMEOUT_SEC = 600.0
ARTIFACTS_DIR = "/var/swesim-collect"
# Optional local checkout of the base commit inside the scenario dir; when present, submodule rules are enforced at
# compile time and recorded in compile.json. Otherwise the Dockerfile guard below enforces them at image build.
CHECKOUT_DIRNAME = "checkout"
# Shell guard run in both images after the HEAD assertion and before the base manifest. Declared submodule paths
# must be absent or empty directories (the approved Dockerfile clones without --recurse-submodules; pypdf's
# `sample-files` is the motivating case, its test command excludes the dependent tests). Populated paths fail the
# build; empty placeholders are deinit'ed and removed so both images record the same tree.
_GITMODULES_PATHS = "git config -f .gitmodules --get-regexp '^submodule\\..*\\.path$' | awk '{print $2}'"
SUBMODULE_GUARD = (
    "if [ -e .gitmodules ]; then "
    # fail closed: a file git cannot parse, or a declared path the word-splitting loop below could not check
    "git config -f .gitmodules -l >/dev/null 2>&1 || { echo 'malformed .gitmodules rejected' >&2; exit 1; }; "
    "git config -f .gitmodules --get-regexp '^submodule\\..*\\.path$' | awk 'NF>2{bad=1} END{exit bad}' || { echo 'submodule path with whitespace rejected' >&2; exit 1; }; "
    f"for p in $({_GITMODULES_PATHS}); do "
    "if [ -L \"$p\" ] || { [ -e \"$p\" ] && { [ ! -d \"$p\" ] || [ -n \"$(ls -A \"$p\")\" ]; }; }; then echo \"populated submodule path rejected: $p\" >&2; exit 1; fi; done; "
    "git submodule deinit --all -f >/dev/null 2>&1 || true; "
    f"for p in $({_GITMODULES_PATHS}); do {{ [ -d \"$p\" ] && rmdir \"$p\"; }} || true; done; "
    "fi"
)
# Advisory compile-time screen of the approved Dockerfile (line continuations joined first). The build-time guard
# above is the authoritative check; this only catches the common spellings early.
_SUBMODULE_FETCH = re.compile(r"git(\s+-[cC]\s+\S+)*\s+clone\b[^\n]*--recurs|git(\s+-[cC]\s+\S+)*\s+submodule\s+(update|init|foreach|add)\b|submodule\.recurse\s*=\s*true")
# Always protected, in addition to the contract's declared paths and its test files (global constraints).
DEFAULT_PROTECTED = ["**/conftest.py", "**/sitecustomize.py", "**/usercustomize.py", "**/*.pth", "pytest.ini", "tox.ini",
                     "setup.cfg", "pyproject.toml", ".github/**", ".gitattributes", ".gitignore",
                     "uv.lock", "poetry.lock", "Pipfile.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"]


@dataclass
class CompileResult:
    task_dirs: dict[str, Path]
    image_digest: str | None
    compile_json: Path


def resolve_docker_hub_digest(image: str) -> str | None:
    """Anonymous Docker Hub manifest HEAD: returns the sha256 digest or None (network failure, other registry)."""
    try:
        repo, tag = _split_tag(image)
        if "/" not in repo: repo = "library/" + repo
        if "." in repo.split("/")[0] or ":" in repo.split("/")[0]: return None  # not Docker Hub
        tok = json.load(urllib.request.urlopen(f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull", timeout=30))["token"]
        req = urllib.request.Request(f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}", method="HEAD",
                                     headers={"Authorization": f"Bearer {tok}",
                                              "Accept": "application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json"})
        return urllib.request.urlopen(req, timeout=30).headers.get("Docker-Content-Digest")
    except Exception:
        return None


def visible_facts(spec: ScenarioSpec):
    """Facts the full brief may list: everything a stakeholder could disclose, never author-only."""
    return [f for f in spec.ledger if f.discovery != Discovery.AUTHOR_ONLY]


def _split_tag(image: str) -> tuple[str, str]:
    name, _, last = image.rpartition("/")
    if ":" in last:
        repo_last, _, tag = last.partition(":")
        return (name + "/" + repo_last if name else repo_last), tag
    return image, "latest"


def verify_assets(sdir: Path, spec: ScenarioSpec) -> None:
    """Every declared asset must exist inside the scenario directory with the declared size and sha256."""
    root = sdir.resolve()
    for name, asset in spec.assets.items():
        path = (sdir / asset.path).resolve()
        if not path.is_relative_to(root) or not path.is_file(): raise ValueError(f"asset {name}: missing or escaping path {asset.path}")
        data = path.read_bytes()
        if len(data) != asset.bytes or hashlib.sha256(data).hexdigest() != asset.sha256: raise ValueError(f"asset {name}: digest mismatch for {asset.path}")


def parse_gitmodules(text: str) -> list[str]:
    """Declared submodule paths from a .gitmodules file (git-config syntax), in order."""
    declared: list[str] = []; in_submodule = False
    for line in text.splitlines():
        header = re.match(r"^\s*\[(\w+)", line)
        if header: in_submodule = header.group(1) == "submodule"; continue
        m = re.match(r"^\s*path\s*=\s*(.+?)\s*$", line)
        if in_submodule and m and m.group(1) not in declared: declared.append(m.group(1))
    return declared


def inspect_submodules(checkout: Path) -> dict:
    """Parse <checkout>/.gitmodules; a declared path is `populated` when it exists as anything but an empty directory
    (a symlink is never an empty placeholder)."""
    gm = Path(checkout) / ".gitmodules"
    declared = parse_gitmodules(gm.read_text(errors="replace")) if gm.is_file() else []
    populated = []
    for rel in declared:
        target = Path(checkout) / rel
        if target.is_symlink() or (target.exists() and not (target.is_dir() and not any(target.iterdir()))): populated.append(rel)
    return {"declared": declared, "populated": populated}


def check_submodules(checkout: Path) -> dict:
    """Same rule as SUBMODULE_GUARD, evaluated on a local checkout: raise when a declared submodule path is populated
    or cannot be checked by the shell guard (whitespace in the path)."""
    info = inspect_submodules(checkout)
    spaced = [p for p in info["declared"] if re.search(r"\s", p)]
    if spaced: raise ValueError(f"unsupported submodule path with whitespace: {spaced}; the build-time guard rejects it as well")
    if info["populated"]:
        raise ValueError(f"populated submodule path(s) in checkout: {info['populated']}; the pilot requires a checkout without --recurse-submodules")
    return info


def _gitmodules_asset(spec: ScenarioSpec) -> str | None:
    return next((name for name, a in spec.assets.items() if PurePosixPath(a.path).name == ".gitmodules"), None)


def _submodules_record(spec: ScenarioSpec, sdir: Path) -> dict:
    """What compile actually knows about submodules. Never an empty list that would read as 'verified: none'."""
    co = sdir / CHECKOUT_DIRNAME
    if co.is_dir(): return {**check_submodules(co), "evidence": "checkout"}
    name = _gitmodules_asset(spec)
    if name:
        return {"declared": parse_gitmodules((sdir / spec.assets[name].path).read_text(errors="replace")), "populated": None,
                "evidence": f"asset {name}; populated checked by build-time guard"}
    return {"declared": None, "populated": None, "evidence": "build-time guard"}


def _base_stage(spec: ScenarioSpec, sdir: Path, resolve_digest: bool) -> tuple[str, str | None]:
    env = spec.environment
    if env.kind == "prebuilt-image":
        digest = env.image_digest
        if not digest and resolve_digest: digest = resolve_docker_hub_digest(env.image)
        if resolve_digest and not digest: raise RuntimeError("image digest lookup failed; refusing to compile a mutable tag")
        ref = f"{_split_tag(env.image)[0]}@{digest}" if digest else env.image
        return f"FROM {ref}\nRUN cd {shlex.quote(env.repo_dir)} && git reset --hard {env.base_commit}", digest
    text = (sdir / spec.assets[env.dockerfile_asset].path).read_text()
    match = re.search(r"^FROM\s+\S+@(sha256:[a-f0-9]{64})", text, re.M)
    if not match: raise ValueError("approved Dockerfile must pin its base digest (FROM image@sha256:...)")
    if _SUBMODULE_FETCH.search(text.replace("\\\n", " ")):
        raise ValueError("approved Dockerfile must not fetch submodules (--recurse-submodules/--recursive/git submodule update); declared submodule paths stay empty for this pilot")
    return text.rstrip("\n"), match.group(1)


def _hosts(run_config: RunConfig, stakeholder_url: str) -> list[str]:
    provider = run_config.model.partition("/")[0]
    if provider not in MODEL_HOSTS: raise ValueError(f"unknown model provider {provider!r} in {run_config.model}; add it to MODEL_HOSTS")
    hosts = list(MODEL_HOSTS[provider])
    if stakeholder_url:
        u = urlparse(stakeholder_url)
        if u.scheme != "https" or not u.hostname: raise ValueError("HTTPS stakeholder URL required")
        hosts.append(u.hostname)
    return hosts


def _select_briefs(spec: ScenarioSpec, run_config: RunConfig):
    natural = next(b for b in spec.briefs if b.profile == "natural")
    selected = next((b for b in spec.briefs if b.profile == run_config.brief_profile), natural)
    return natural, selected


def _digest_tree(paths) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths) if p.is_file()}


def _compiler_inputs(spec: ScenarioSpec, image_digest: str | None, agent_version: str | None) -> dict:
    return {"spec_hash": spec.spec_hash, "image_digest": image_digest,
              "agent_version": agent_version, "agent_python": AGENT_PYTHON,
              "templates": _digest_tree(TEMPLATES.iterdir()),
              "verifier": _digest_tree(VERIFIER_SRC / f for f in VERIFIER_FILES),
              "shims": _digest_tree(SHIMS / f for f in SHIM_FILES)}


def _canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _build_hash(inputs: dict, config: dict, manifest: dict) -> str:
    return hashlib.sha256(_canonical({"compiler_inputs": inputs, "compile_config": config, "rendered_manifest": manifest})).hexdigest()


def compiled_hash(spec: ScenarioSpec, image_digest: str | None, agent_version: str | None, *,
                  compile_config: dict, rendered_manifest: dict) -> str:
    """Bind compiler sources, canonical render configuration, and every rendered file's content and permissions."""
    return _build_hash(_compiler_inputs(spec, image_digest, agent_version), compile_config, rendered_manifest)


def rendered_manifest(task_dir: Path) -> dict:
    """File digests and modes; normalize only the embedded build hash to avoid a self-reference."""
    manifest = {}
    for path in sorted(task_dir.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"compiled task contains a symlink: {path.relative_to(task_dir)}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"compiled task contains a non-file: {path.relative_to(task_dir)}")
        rel = path.relative_to(task_dir).as_posix()
        data = path.read_bytes()
        if rel == "task.toml":
            parsed = tomllib.loads(data.decode())
            parsed["metadata"]["compiled_hash"] = ""
            data = _canonical(parsed)
        manifest[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "mode": stat.S_IMODE(path.stat().st_mode)}
    return manifest


def verify_compiled_artifacts(task_dir: Path, record: dict, condition: str) -> None:
    """Reject legacy, incomplete or changed compiled inputs before a trial or validation uses them."""
    required = ("compiler_inputs", "compile_config", "rendered_manifest")
    if any(not isinstance(record.get(key), dict) for key in required):
        raise ValueError("compiled inputs lack a bound manifest; recompile and revalidate")
    try:
        expected = _build_hash(*(record[key] for key in required))
        if record.get("compiled_hash") != expected or record.get("spec_hash") != record["compiler_inputs"].get("spec_hash"):
            raise ValueError("compiled record hash mismatch; recompile and revalidate")
        if condition not in record["rendered_manifest"] or not task_dir.is_dir() or task_dir.is_symlink():
            raise ValueError("compiled condition is missing; recompile and revalidate")
        actual = rendered_manifest(task_dir)
        if actual != record["rendered_manifest"][condition]:
            raise ValueError("compiled file manifest mismatch; recompile and revalidate")
        metadata = tomllib.loads((task_dir / "task.toml").read_text())["metadata"]
        if metadata.get("compiled_hash") != expected:
            raise ValueError("compiled task metadata hash mismatch; recompile and revalidate")
    except (OSError, KeyError, TypeError, UnicodeError) as exc:
        raise ValueError("compiled inputs cannot be verified; recompile and revalidate") from exc


def _collect_command(spec: ScenarioSpec, stakeholder_url: str) -> str:
    return ("/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin "
            f"SWESIM_REPO_DIR={shlex.quote(spec.environment.repo_dir)} "
            f"SWESIM_STAKEHOLDER_URL={shlex.quote(stakeholder_url)} "
            'SWESIM_SESSION_TOKEN="$SWESIM_SESSION_TOKEN" '
            "/usr/local/bin/swesim-python -I /opt/swesim/collect.py")


def _grader_config(spec: ScenarioSpec) -> dict:
    protected: list[str] = []
    for p in spec.contract.protected_paths + spec.contract.runner.test_files + DEFAULT_PROTECTED:
        if p not in protected: protected.append(p)
    return {"artifacts_dir": ARTIFACTS_DIR, "pristine_dir": "/pristine", "work_dir": "/tmp/swesim-work",
            "expected_tests": {t: "f2p" for t in spec.contract.fail_to_pass} | {t: "p2p" for t in spec.contract.pass_to_pass},
            "groups": [g.model_dump() for g in spec.groups], "protected": protected,
            "runner": spec.contract.runner.model_dump(), "test_user": "tester", "verifier_out": "/logs/verifier",
            "test_timeout_sec": TEST_TIMEOUT_SEC, "target_dir": spec.environment.repo_dir, "contract_patch": "/tests/test.patch"}


def _copy_exec(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst); dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def compile_scenario(ws: Workspace, spec: ScenarioSpec, run_config: RunConfig, stakeholder_url: str, resolve_digest: bool = True,
                     session_token: str = "", trial_id: str = "") -> CompileResult:
    sdir = ws.scenario_dir(spec.id)
    verify_assets(sdir, spec)
    if len(spec.contract.test_assets) != 1: raise ValueError("pilot expects exactly one declared test asset (the hidden test patch)")
    controls = spec.controls.plausible_wrong + spec.controls.alternative_correct + spec.controls.tamper
    names = [c.name for c in controls]
    if len(names) != len(set(names)): raise ValueError(f"duplicate control name across control lists: {sorted(n for n in names if names.count(n) > 1)}")
    if not run_config.agent_version: raise ValueError("run_config.agent_version must pin the pre-baked mini-swe-agent version")
    base_stage, digest = _base_stage(spec, sdir, resolve_digest)
    submodules = _submodules_record(spec, sdir)
    hosts = _hosts(run_config, stakeholder_url)
    natural, selected = _select_briefs(spec, run_config)
    # Render with a fixed placeholder, hash the resulting tree, then fill only the self-referential metadata field.
    chash = "0" * 64
    compile_config = {"run_config": run_config.model_dump(mode="json"), "stakeholder_url": stakeholder_url,
                      "agent_hosts": hosts, "brief_profile": selected.profile, "base_stage": base_stage,
                      "grader_config": _grader_config(spec), "submodule_guard": SUBMODULE_GUARD,
                      "verifier_timeout_sec": VERIFIER_TIMEOUT_SEC, "collect_timeout_sec": COLLECT_TIMEOUT_SEC,
                      "artifacts_dir": ARTIFACTS_DIR, "trial_id": trial_id,
                      "session_token_sha256": hashlib.sha256(session_token.encode()).hexdigest() if session_token else None}
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True, undefined=StrictUndefined)
    repo_dir = spec.environment.repo_dir
    out: dict[str, Path] = {}
    for condition in CONDITIONS:
        tdir = ws.task_dir(spec.id, condition); shutil.rmtree(tdir, ignore_errors=True)
        for sub in ("environment/shims", "tests", "solution", "controls"): (tdir / sub).mkdir(parents=True)
        ctx = dict(spec=spec, run_config=run_config, condition=condition, task_name=f"{spec.id}--{condition}", image_digest=digest,
                   agent_hosts=hosts, stakeholder_url=stakeholder_url, session_token=session_token if condition == "interactive" else "",
                   trial_id=trial_id, collect_command=_collect_command(spec, stakeholder_url), verifier_timeout=VERIFIER_TIMEOUT_SEC,
                   collect_timeout=COLLECT_TIMEOUT_SEC, submodule_guard=SUBMODULE_GUARD,
                   repo_dir=repo_dir, repo_dir_q=shlex.quote(repo_dir), base_commit=spec.environment.base_commit, base_stage=base_stage,
                   agent_python=AGENT_PYTHON, agent_version=run_config.agent_version, compiled_hash=chash,
                   brief=natural.text if condition == "full" else selected.text, brief_profile="natural" if condition == "full" else selected.profile,
                   visible_facts=visible_facts(spec), solver_minutes=run_config.solver_timeout_sec // 60)
        (tdir / "task.toml").write_text(env.get_template("task.toml.j2").render(**ctx))
        (tdir / "instruction.md").write_text(env.get_template("instruction.md.j2").render(**ctx))
        # Every brief profile is rendered at compile time so the run stage never re-renders from live templates.
        for b in spec.briefs:
            pctx = dict(ctx, brief=natural.text if condition == "full" else b.text, brief_profile="natural" if condition == "full" else b.profile)
            (tdir / f"instruction.{b.profile}.md").write_text(env.get_template("instruction.md.j2").render(**pctx))
        if condition == "interactive":
            shutil.copyfile(TEMPLATES / "mini_swe_agent.interactive.yaml", tdir / "agent_config.yaml")
        # agent image build context
        (tdir / "environment" / "Dockerfile").write_text(env.get_template("environment.Dockerfile.j2").render(**ctx))
        shutil.copyfile(TEMPLATES / "collect.py", tdir / "environment" / "collect.py")
        for shim in SHIM_FILES: _copy_exec(SHIMS / shim, tdir / "environment" / "shims" / shim)
        for asset_name in spec.environment.build_context_assets:
            asset = spec.assets[asset_name]
            for build in (tdir / "environment", tdir / "tests"):
                dst = build / asset.path; dst.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(sdir / asset.path, dst)
        # verifier image build context
        (tdir / "tests" / "Dockerfile").write_text(env.get_template("tests.Dockerfile.j2").render(**ctx))
        _copy_exec(TEMPLATES / "test.sh", tdir / "tests" / "test.sh")
        for f in VERIFIER_FILES: shutil.copyfile(VERIFIER_SRC / f, tdir / "tests" / f)
        shutil.copyfile(sdir / spec.assets[spec.contract.test_assets[0]].path, tdir / "tests" / "test.patch")
        (tdir / "tests" / "grader_config.json").write_text(json.dumps(_grader_config(spec), indent=2))
        # oracle and controls
        (tdir / "solution" / "solve.sh").write_text(env.get_template("solve.sh.j2").render(**ctx)); (tdir / "solution" / "solve.sh").chmod(0o755)
        shutil.copyfile(sdir / spec.assets[spec.controls.oracle].path, tdir / "solution" / "oracle.diff")
        for c in controls: shutil.copyfile(sdir / spec.assets[c.asset].path, tdir / "controls" / f"{c.name}.diff")
        out[condition] = tdir
    manifest = {condition: rendered_manifest(task) for condition, task in out.items()}
    inputs = _compiler_inputs(spec, digest, run_config.agent_version)
    chash = _build_hash(inputs, compile_config, manifest)
    for task in out.values():
        path = task / "task.toml"
        path.write_text(path.read_text().replace('compiled_hash = "' + "0" * 64 + '"', f'compiled_hash = "{chash}"', 1))
    cj = sdir / "compile.json"
    atomic_json(cj, {"compiled_hash": chash, "spec_hash": spec.spec_hash, "run_config_hash": run_config.run_config_hash,
                              "image_digest": digest, "brief_profile": selected.profile, "agent_hosts": hosts, "submodules": submodules,
                              "compiler_inputs": inputs, "compile_config": compile_config, "rendered_manifest": manifest,
                              "task_dirs": {k: str(v) for k, v in out.items()}})
    return CompileResult(out, digest, cj)
