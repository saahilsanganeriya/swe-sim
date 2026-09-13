"""Compile stage: template rendering, asset verification, root collector decisions. Offline."""
import ast, hashlib, importlib.util, json, os, stat, sys, tarfile, tomllib
from pathlib import Path

import pytest

from swesim.models import ScenarioSpec, RunConfig, minimal_spec_dict
from swesim.paths import Workspace
from swesim.stages import compile as comp

VERIFIER_FILES = ("grader.py", "policy.py", "parsers.py", "run_pytest.py")
SHIM_FILES = ("ask-user", "request-review")
GRADE_CONFIG_FIELDS = {"artifacts_dir", "pristine_dir", "work_dir", "expected_tests", "groups", "protected", "runner",
                       "test_user", "verifier_out", "test_timeout_sec", "target_dir", "contract_patch"}
URL = "https://x.modal.run"


@pytest.fixture(autouse=True)
def neighbours(tmp_path, monkeypatch):
    """The verifier and stakeholder streams own these files; tests point the compiler at placeholders."""
    v = tmp_path / "verifier_src"; v.mkdir()
    for n in VERIFIER_FILES: (v / n).write_text(f"# placeholder {n}\n")
    s = tmp_path / "shims"; s.mkdir()
    for n in SHIM_FILES: (s / n).write_text("#!/usr/local/bin/swesim-python -I\nprint('shim')\n")
    monkeypatch.setattr(comp, "VERIFIER_SRC", v); monkeypatch.setattr(comp, "SHIMS", s)
    return v, s


def _materialize(tmp_path, d, content=b"x"):
    ws = Workspace(tmp_path / "ws"); sdir = ws.scenario_dir(d["id"]); (sdir / "assets").mkdir(parents=True, exist_ok=True)
    for a in d["assets"].values():
        p = sdir / a["path"]; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(content)
        a["sha256"] = hashlib.sha256(content).hexdigest(); a["bytes"] = len(content)
    return ws, ScenarioSpec.model_validate(d)


# ---- brief tests (verbatim contracts) ---------------------------------------------------------

def test_compile_renders_three_conditions(tmp_path):
    ws = Workspace(tmp_path / "ws"); d = minimal_spec_dict(); spec = ScenarioSpec.model_validate(d)
    sdir = ws.scenario_dir(spec.id); (sdir / "assets").mkdir(parents=True)
    for name in ("oracle.diff", "wrong.diff", "tamper.diff"): (sdir / "assets" / name).write_text("x")
    d["assets"] = {n: {"path": "assets/" + n, "sha256": hashlib.sha256(b"x").hexdigest(), "bytes": 1} for n in ("oracle.diff", "wrong.diff", "tamper.diff", "test.patch")}
    spec = ScenarioSpec.model_validate(d)
    (sdir / "assets" / "test.patch").write_text("x")
    res = comp.compile_scenario(ws, spec, RunConfig.default("interactive"), stakeholder_url=URL, resolve_digest=False)
    assert set(res.task_dirs) == {"full", "hidden", "interactive"}
    t = tomllib.loads((res.task_dirs["interactive"] / "task.toml").read_text())
    assert t["agent"]["user"] == "agent" and t["environment"]["network_mode"] == "allowlist"
    assert "x.modal.run" in t["environment"]["allowed_hosts"] and t["verifier"]["environment_mode"] == "separate"
    assert t["artifacts"] == ["/var/swesim-collect/app.tar", "/var/swesim-collect/collect.status"]
    assert t["verifier"]["collect"][0]["user"] == "root" and "/opt/swesim/collect.py" in t["verifier"]["collect"][0]["command"]
    assert t["environment"]["env"]["UV_OFFLINE"] == "1"
    ins = (res.task_dirs["interactive"] / "instruction.md").read_text(); assert "ask-user" in ins and "It breaks." in ins
    full = (res.task_dirs["full"] / "instruction.md").read_text(); assert "Raise ValueError" in full and "ask-user" not in full
    hidden = (res.task_dirs["hidden"] / "instruction.md").read_text(); assert "Raise ValueError" not in hidden
    cfg = json.loads((res.task_dirs["full"] / "tests" / "grader_config.json").read_text())
    assert cfg["expected_tests"] == {"t1": "f2p", "t0": "p2p"} and cfg["artifacts_dir"] == "/var/swesim-collect"
    assert (res.task_dirs["full"] / "tests" / "grader.py").exists() and (res.task_dirs["full"] / "solution" / "solve.sh").exists()


def test_full_brief_excludes_author_only(tmp_path):
    ws = Workspace(tmp_path / "ws"); d = minimal_spec_dict()
    d["ledger"].append({"id": "f09", "text": "SECRET-HELPER-NAME", "kind": "interface", "provenance": "authored", "authored_rationale": "private name", "discovery": "author-only"})
    spec = ScenarioSpec.model_validate(d); sdir = ws.scenario_dir(spec.id); (sdir / "assets").mkdir(parents=True)
    for name in ("oracle.diff", "wrong.diff", "tamper.diff", "test.patch"): (sdir / "assets" / name).write_text("x")
    for a in d["assets"].values(): a["sha256"] = hashlib.sha256(b"x").hexdigest()
    spec = ScenarioSpec.model_validate(d)
    res = comp.compile_scenario(ws, spec, RunConfig.default("full"), stakeholder_url=URL, resolve_digest=False)
    for cond in ("full", "hidden", "interactive"):
        assert "SECRET-HELPER-NAME" not in (res.task_dirs[cond] / "instruction.md").read_text()


def test_compiler_selects_withheld_profile(tmp_path):
    d = minimal_spec_dict(); d['briefs'][0]['text'] = 'Import fails\nERROR42'
    d['briefs'].append({'profile': 'withheld', 'text': 'Import fails', 'provenance': 'withheld'})
    d['ledger'].append({'id': 'withheld-001', 'text': 'ERROR42', 'kind': 'reproduction', 'provenance': 'original-issue', 'source_excerpt': 'ERROR42', 'source_url': 'https://example.com/issue', 'discovery': 'on-request'})
    d['operations'] = [{'type': 'withholding', 'target': 'withheld-001', 'rationale': 'error_message'}]
    ws = Workspace(tmp_path / 'w'); assets = ws.scenario_dir(d['id']) / 'assets'; assets.mkdir(parents=True)
    for name, a in d['assets'].items():
        (assets / name).write_text('x'); a['sha256'] = hashlib.sha256(b'x').hexdigest()
    spec = ScenarioSpec.model_validate(d)
    result = comp.compile_scenario(ws, spec, RunConfig.default('interactive'), URL, resolve_digest=False)
    assert 'ERROR42' in (result.task_dirs['full'] / 'instruction.md').read_text()
    for cond in ('hidden', 'interactive'): assert 'ERROR42' not in (result.task_dirs[cond] / 'instruction.md').read_text()
    assert json.loads(result.compile_json.read_text())["brief_profile"] == "withheld"


# ---- Harbor recipe fidelity -------------------------------------------------------------------

def test_task_toml_validates_against_harbor_schema(tmp_path):
    from harbor.models.task.config import TaskConfig
    from harbor.models.task.task import Task
    ws, spec = _materialize(tmp_path, minimal_spec_dict())
    res = comp.compile_scenario(ws, spec, RunConfig.default("interactive"), URL, resolve_digest=False, session_token="tok", trial_id="trial1")
    for cond, tdir in res.task_dirs.items():
        cfg = TaskConfig.model_validate_toml((tdir / "task.toml").read_text())
        assert cfg.agent.user == "agent" and cfg.agent.network_mode.value == "allowlist"
        assert cfg.environment.network_mode.value == "allowlist" and cfg.environment.allowed_hosts == ["api.anthropic.com", "x.modal.run"]
        assert cfg.agent.allowed_hosts == cfg.environment.allowed_hosts
        assert cfg.verifier.network_mode.value == "no-network" and cfg.verifier.environment_mode.value == "separate"
        assert cfg.verifier.environment.network_mode.value == "no-network"  # baseline of the separate verifier image, not only the phase
        assert cfg.verifier.collect[0].user == "root" and cfg.verifier.collect[0].command.startswith("/usr/bin/env -i ")
        assert "SWESIM_SESSION_TOKEN=\"$SWESIM_SESSION_TOKEN\"" in cfg.verifier.collect[0].command
        assert cfg.artifacts == ["/var/swesim-collect/app.tar", "/var/swesim-collect/collect.status"]
        assert cfg.environment.env["UV_OFFLINE"] == "1" and cfg.environment.env["UV_NO_PROGRESS"] == "1" and cfg.environment.env["UV_CACHE_DIR"] == "/tmp/uv-cache"
        assert cfg.environment.env["SWESIM_REPO_DIR"] == "/app" and cfg.environment.env["SWESIM_STAKEHOLDER_URL"] == URL
        assert cfg.environment.env["SWESIM_SESSION_TOKEN"] == ("tok" if cond == "interactive" else "")
        assert cfg.environment.env["SWESIM_TRIAL_ID"] == "trial1"
        assert cfg.agent.timeout_sec == RunConfig.default(cond).solver_timeout_sec
        assert cfg.metadata["condition"] == cond and cfg.metadata["spec_hash"] == spec.spec_hash
        assert cfg.metadata["compiled_hash"] == json.loads(res.compile_json.read_text())["compiled_hash"]
        Task(tdir)  # loads instruction.md + task.toml exactly as harbor run does


def test_environment_dockerfile_reproduces_verified_recipe(tmp_path):
    ws, spec = _materialize(tmp_path, minimal_spec_dict())
    res = comp.compile_scenario(ws, spec, RunConfig.default("full"), URL, resolve_digest=False)
    df = (res.task_dirs["full"] / "environment" / "Dockerfile").read_text()
    assert df.startswith("FROM ubuntu:24.04\n")
    assert "git reset --hard " + "0" * 40 in df
    assert 'test "$(git rev-parse HEAD)" = ' + "0" * 40 in df
    assert "useradd -m agent" in df and "USER agent" in df and "procps" in df
    assert "UV_INSTALL_DIR=/usr/local/bin" in df and "/usr/local/bin/swesim-python" in df
    assert "uv tool install --python 3.12 'mini-swe-agent==2.4.6' --with litellm --with orjson --with fastapi" in df
    assert "export PATH=\"$HOME/.local/bin:$PATH\"" in df  # proven pre-bake shape (smoke6)
    assert "chown -R agent:agent /home/agent" in df
    assert "COPY collect.py /opt/swesim/collect.py" in df and "chmod 700 /var/swesim-collect" in df
    assert "COPY shims/ask-user shims/request-review /opt/swesim/bin/" in df
    assert "rm -rf .git && git init" in df  # base-only object database
    assert df.rstrip().endswith("WORKDIR /app")
    # root owns the collector and the shims; the agent owns the repository
    assert "chown -R agent:agent /app" in df
    assert df.index("COPY collect.py") > df.index("USER root")


def test_tests_dockerfile_builds_offline_verifier(tmp_path):
    ws, spec = _materialize(tmp_path, minimal_spec_dict())
    res = comp.compile_scenario(ws, spec, RunConfig.default("full"), URL, resolve_digest=False)
    df = (res.task_dirs["full"] / "tests" / "Dockerfile").read_text()
    assert df.startswith("FROM ubuntu:24.04\n") and "useradd -m tester" in df
    assert 'test "$(git rev-parse HEAD)" = ' + "0" * 40 in df
    assert '"/pristine"' in df and 'ignore_patterns(".git"' in df
    assert "COPY test.patch /tests/test.patch" in df
    assert "COPY grader.py policy.py parsers.py run_pytest.py grader_config.json test.sh /tests/" in df
    assert "chmod 755 /tests/test.sh" in df
    assert "type l" in df and ".gitmodules" in df and "git-lfs" in df  # pilot rejects symlinks/submodules/LFS pointers


def test_task_dir_layout_and_permissions(tmp_path):
    ws, spec = _materialize(tmp_path, minimal_spec_dict())
    res = comp.compile_scenario(ws, spec, RunConfig.default("full"), URL, resolve_digest=False)
    t = res.task_dirs["hidden"]
    for rel in ("task.toml", "instruction.md", "environment/Dockerfile", "environment/collect.py", "environment/shims/ask-user",
                "environment/shims/request-review", "tests/Dockerfile", "tests/test.sh", "tests/grader.py", "tests/policy.py",
                "tests/parsers.py", "tests/run_pytest.py", "tests/grader_config.json", "tests/test.patch", "solution/solve.sh",
                "solution/oracle.diff", "controls/keyerror.diff", "controls/root-conftest.diff"):
        assert (t / rel).is_file(), rel
    for rel in ("solution/solve.sh", "environment/shims/ask-user", "environment/shims/request-review", "tests/test.sh"):
        assert (t / rel).stat().st_mode & stat.S_IXUSR, rel
    assert (t / "environment" / "collect.py").read_bytes() == (comp.TEMPLATES / "collect.py").read_bytes()
    assert (t / "tests" / "test.sh").read_bytes() == (comp.TEMPLATES / "test.sh").read_bytes()
    solve = (t / "solution" / "solve.sh").read_text()
    assert "cd /app" in solve and "git apply --whitespace=nowarn /solution/oracle.diff" in solve
    assert "/tests/grader.py --config /tests/grader_config.json" in (t / "tests" / "test.sh").read_text()
    assert '"outcome_code\\": 7' in (t / "tests" / "test.sh").read_text()  # evaluator-failure fallback reward


def test_grader_config_matches_grade_config_fields(tmp_path):
    ws, spec = _materialize(tmp_path, minimal_spec_dict())
    res = comp.compile_scenario(ws, spec, RunConfig.default("full"), URL, resolve_digest=False)
    cfg = json.loads((res.task_dirs["full"] / "tests" / "grader_config.json").read_text())
    assert set(cfg) == GRADE_CONFIG_FIELDS
    assert cfg["pristine_dir"] == "/pristine" and cfg["target_dir"] == "/app" and cfg["contract_patch"] == "/tests/test.patch"
    assert cfg["test_user"] == "tester" and cfg["verifier_out"] == "/logs/verifier" and cfg["work_dir"] == "/tmp/swesim-work"
    assert cfg["runner"] == spec.contract.runner.model_dump() and cfg["groups"] == [g.model_dump() for g in spec.groups]
    assert isinstance(cfg["test_timeout_sec"], int) and cfg["test_timeout_sec"] > 0
    for p in ("**/conftest.py", "**/sitecustomize.py", "**/usercustomize.py", "**/*.pth", "pytest.ini", "tox.ini", "setup.cfg",
              "pyproject.toml", ".github/**", ".gitattributes", ".gitignore", "uv.lock", "poetry.lock"):
        assert p in cfg["protected"], p


def test_compile_json_and_compiled_hash(tmp_path, neighbours):
    ws, spec = _materialize(tmp_path, minimal_spec_dict())
    rc = RunConfig.default("full")
    a = json.loads(comp.compile_scenario(ws, spec, rc, URL, resolve_digest=False).compile_json.read_text())
    b = json.loads(comp.compile_scenario(ws, spec, rc, URL, resolve_digest=False).compile_json.read_text())
    assert a == b and set(a) >= {"compiled_hash", "spec_hash", "run_config_hash", "image_digest", "brief_profile", "task_dirs"}
    assert a["spec_hash"] == spec.spec_hash and a["run_config_hash"] == rc.run_config_hash and a["image_digest"] is None
    assert a["brief_profile"] == "natural"  # withheld requested, absent from spec: fall back and record it
    (neighbours[0] / "grader.py").write_text("# changed\n")
    c = json.loads(comp.compile_scenario(ws, spec, rc, URL, resolve_digest=False).compile_json.read_text())
    assert c["compiled_hash"] != a["compiled_hash"]


def test_session_token_only_reaches_interactive(tmp_path):
    ws, spec = _materialize(tmp_path, minimal_spec_dict())
    res = comp.compile_scenario(ws, spec, RunConfig.default("interactive"), URL, resolve_digest=False, session_token="s3cret-token")
    for cond in ("full", "hidden"):
        assert "s3cret-token" not in (res.task_dirs[cond] / "task.toml").read_text()
    assert "s3cret-token" in (res.task_dirs["interactive"] / "task.toml").read_text()


def test_asset_hash_mismatch_and_escape_rejected(tmp_path):
    ws, spec = _materialize(tmp_path, minimal_spec_dict())
    (ws.scenario_dir(spec.id) / "assets" / "oracle.diff").write_text("tampered")
    with pytest.raises(ValueError, match="asset"):
        comp.compile_scenario(ws, spec, RunConfig.default("full"), URL, resolve_digest=False)
    ws2, spec2 = _materialize(tmp_path / "b", minimal_spec_dict())
    (ws2.scenario_dir(spec2.id) / "assets" / "wrong.diff").unlink()
    with pytest.raises(ValueError, match="asset"):
        comp.compile_scenario(ws2, spec2, RunConfig.default("full"), URL, resolve_digest=False)


def test_prebuilt_digest_pinning(tmp_path, monkeypatch):
    d = minimal_spec_dict(); d["environment"]["image_digest"] = "sha256:" + "a" * 64
    ws, spec = _materialize(tmp_path, d)
    monkeypatch.setattr(comp, "resolve_docker_hub_digest", lambda image: pytest.fail("spec digest must win; no lookup"))
    res = comp.compile_scenario(ws, spec, RunConfig.default("full"), URL, resolve_digest=True)
    assert res.image_digest == "sha256:" + "a" * 64
    df = (res.task_dirs["full"] / "environment" / "Dockerfile").read_text()
    assert df.startswith("FROM ubuntu@sha256:" + "a" * 64 + "\n")
    # without a spec digest, an online lookup that fails must refuse to compile the mutable tag
    ws2, spec2 = _materialize(tmp_path / "b", minimal_spec_dict())
    monkeypatch.setattr(comp, "resolve_docker_hub_digest", lambda image: None)
    with pytest.raises(RuntimeError, match="digest"):
        comp.compile_scenario(ws2, spec2, RunConfig.default("full"), URL, resolve_digest=True)
    monkeypatch.setattr(comp, "resolve_docker_hub_digest", lambda image: "sha256:" + "c" * 64 if image == "ubuntu:24.04" else None)
    res = comp.compile_scenario(ws2, spec2, RunConfig.default("full"), URL, resolve_digest=True)
    assert res.image_digest == "sha256:" + "c" * 64 and json.loads(res.compile_json.read_text())["image_digest"] == "sha256:" + "c" * 64
    assert comp._split_tag("swebench/sweb.eval.x86_64.django_1776_django-11099:latest") == ("swebench/sweb.eval.x86_64.django_1776_django-11099", "latest")
    assert comp._split_tag("registry:5000/team/img") == ("registry:5000/team/img", "latest")


def test_dockerfile_kind_consumes_approved_asset_in_both_images(tmp_path):
    d = minimal_spec_dict(); digest = "sha256:" + "b" * 64
    d["environment"] = {"kind": "dockerfile", "dockerfile_asset": "Dockerfile", "build_context_assets": ["req.txt"], "repo_url": "https://example.com/r.git",
                        "base_commit": "1" * 40, "test_command": "pytest -q", "repo_dir": "/testbed"}
    d["assets"]["Dockerfile"] = {"path": "assets/Dockerfile", "sha256": "0" * 64, "bytes": 1}
    d["assets"]["req.txt"] = {"path": "assets/req.txt", "sha256": "0" * 64, "bytes": 1}
    ws, spec = _materialize(tmp_path, d)
    dockerfile = f"FROM python:3.12-slim@{digest}\nCOPY assets/req.txt /tmp/req.txt\nRUN git clone https://example.com/r.git /testbed && cd /testbed && git checkout {'1' * 40} && pip install -e .\n"
    sdir = ws.scenario_dir(spec.id); (sdir / "assets" / "Dockerfile").write_text(dockerfile)
    d["assets"]["Dockerfile"] = {"path": "assets/Dockerfile", "sha256": hashlib.sha256(dockerfile.encode()).hexdigest(), "bytes": len(dockerfile.encode())}
    spec = ScenarioSpec.model_validate(d)
    res = comp.compile_scenario(ws, spec, RunConfig.default("full"), URL, resolve_digest=True)
    assert res.image_digest == digest
    for build in ("environment", "tests"):
        df = (res.task_dirs["full"] / build / "Dockerfile").read_text()
        assert df.startswith(dockerfile) and "git reset --hard" not in df  # the approved Dockerfile is the base stage, untouched
        assert (res.task_dirs["full"] / build / "assets" / "req.txt").read_bytes() == b"x"
        assert 'test "$(git rev-parse HEAD)" = ' + "1" * 40 in df and "/testbed" in df
    unpinned = dockerfile.replace(f"@{digest}", "")
    (sdir / "assets" / "Dockerfile").write_text(unpinned)
    d["assets"]["Dockerfile"] = {"path": "assets/Dockerfile", "sha256": hashlib.sha256(unpinned.encode()).hexdigest(), "bytes": len(unpinned.encode())}
    with pytest.raises(ValueError, match="pin"):
        comp.compile_scenario(ws, ScenarioSpec.model_validate(d), RunConfig.default("full"), URL, resolve_digest=True)


def test_rejects_bad_inputs(tmp_path):
    ws, spec = _materialize(tmp_path, minimal_spec_dict())
    with pytest.raises(ValueError, match="HTTPS"):
        comp.compile_scenario(ws, spec, RunConfig.default("full"), "http://x.modal.run", resolve_digest=False)
    with pytest.raises(ValueError, match="provider"):
        comp.compile_scenario(ws, spec, RunConfig.model_validate({"condition": "full", "model": "mystery/model-1"}), URL, resolve_digest=False)
    d = minimal_spec_dict(); d["controls"]["tamper"].append({"name": "keyerror", "asset": "tamper.diff", "rationale": "dup", "cls": "benign-edit"})
    ws2, spec2 = _materialize(tmp_path / "b", d)
    with pytest.raises(ValueError, match="control name"):
        comp.compile_scenario(ws2, spec2, RunConfig.default("full"), URL, resolve_digest=False)
    # an empty stakeholder URL is allowed (full/hidden compile before deployment) and yields model hosts only
    res = comp.compile_scenario(ws, spec, RunConfig.default("full"), "", resolve_digest=False)
    assert tomllib.loads((res.task_dirs["full"] / "task.toml").read_text())["environment"]["allowed_hosts"] == ["api.anthropic.com"]


def test_visible_facts_exclude_author_only():
    d = minimal_spec_dict()
    d["ledger"].append({"id": "f09", "text": "hidden", "kind": "context", "provenance": "authored", "authored_rationale": "x", "discovery": "author-only"})
    d["ledger"].append({"id": "f10", "text": "derivable", "kind": "context", "provenance": "authored", "authored_rationale": "x", "discovery": "repo-derivable"})
    spec = ScenarioSpec.model_validate(d)
    assert [f.id for f in comp.visible_facts(spec)] == ["f01", "f10"]


# ---- root collector (swesim/templates/collect.py), exercised in-process --------------------------

def _collector():
    spec = importlib.util.spec_from_file_location("swesim_collect_under_test", comp.TEMPLATES / "collect.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


def _tree(root: Path):
    (root / "pkg").mkdir(parents=True); (root / "pkg" / "a.py").write_bytes(b"a" * 10); (root / "pkg" / "b.py").write_bytes(b"bb" * 10)
    (root / "empty").mkdir(); (root / ".git").mkdir(); (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "__pycache__").mkdir(); (root / "__pycache__" / "x.pyc").write_bytes(b"junk")
    os.symlink("/etc/passwd", root / "abs-link"); os.symlink("pkg/a.py", root / "rel-link")
    return root


def test_collector_is_stdlib_only_and_never_runs_git():
    src = (comp.TEMPLATES / "collect.py").read_text(); tree = ast.parse(src)
    for node in ast.walk(tree):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
        for n in names: assert n.split(".")[0] in sys.stdlib_module_names, n
    assert "'git'" not in src and '"git"' not in src and "/usr/bin/git" not in src


def test_collector_archives_tree_and_writes_receipt(tmp_path):
    c = _collector(); repo = _tree(tmp_path / "app"); out = tmp_path / "collect"
    receipt = c.collect(repo, out, stakeholder_url="", token="", kill=lambda: None)
    assert set(receipt) >= set(c.RECEIPT_KEYS)
    assert receipt["status"] == "ok" and receipt["rc"] == 0 and receipt["user"] == "root" and receipt["freeze_status"] == "n/a" and receipt["transcript_hash"] is None
    assert receipt["bytes"] == 30 and receipt["sha256"] == hashlib.sha256((out / "app.tar").read_bytes()).hexdigest()
    assert json.loads((out / "collect.status").read_text()) == receipt
    with tarfile.open(out / "app.tar") as t:
        names = {m.name: m for m in t.getmembers()}
    assert {"pkg", "pkg/a.py", "pkg/b.py", "empty", "abs-link", "rel-link"} == set(names)
    assert names["abs-link"].issym() and names["abs-link"].linkname == "/etc/passwd" and names["rel-link"].issym()
    assert receipt["files"] == 6
    assert stat.S_IMODE(out.stat().st_mode) == 0o700


def test_collector_oversized_and_member_caps_still_freeze(tmp_path):
    c = _collector(); repo = _tree(tmp_path / "app"); out = tmp_path / "collect"; calls = []

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"frozen": True, "transcript_hash": "f" * 64}).encode()

    def opener(req, timeout=None):
        calls.append((req.full_url, req.get_header("Authorization"), req.data)); return _Resp()

    r = c.collect(repo, out, stakeholder_url="https://s.modal.run", token="tok", kill=lambda: None, opener=opener, limit_bytes=25)
    assert r["status"] == "oversized" and r["sha256"] is None and not (out / "app.tar").exists()
    assert r["freeze_status"] == "ok" and r["transcript_hash"] == "f" * 64
    assert calls == [("https://s.modal.run/freeze", "Bearer tok", b"{}")]
    r = c.collect(repo, out, stakeholder_url="https://s.modal.run", token="tok", kill=lambda: None, opener=opener, limit_members=3)
    assert r["status"] == "oversized" and len(calls) == 2
    assert json.loads((out / "collect.status").read_text())["status"] == "oversized"


def test_collector_invalid_tree_and_failures(tmp_path):
    c = _collector(); out = tmp_path / "collect"
    os.symlink(tmp_path, tmp_path / "link"); r = c.collect(tmp_path / "link", out, "", "", kill=lambda: None)
    assert r["status"] == "invalid-tree" and r["rc"] != 0
    r = c.collect(tmp_path / "missing", out, "", "", kill=lambda: None)
    assert r["status"] == "invalid-tree"

    def boom(): raise RuntimeError("pkill failed")
    frozen = []
    def opener(req, timeout=None):
        frozen.append(1); raise OSError("stakeholder down")
    r = c.collect(_tree(tmp_path / "app"), out, "https://s.modal.run", "tok", kill=boom, opener=opener)
    assert r["status"] == "collection-failure" and r["rc"] == 1 and "pkill failed" in r["error"]
    assert frozen and r["freeze_status"] == "failed" and r["transcript_hash"] is None  # freeze attempted in finally, twice
    assert len(frozen) == 2


def test_collector_kill_loop(monkeypatch):
    c = _collector(); rcs = iter([0, 0, 1]); seen = []

    class P:
        def __init__(self, rc): self.returncode = rc
    def fake_run(cmd, **kw): seen.append(cmd); return P(next(rcs))
    monkeypatch.setattr(c.subprocess, "run", fake_run)
    c.kill_agent_processes()
    assert seen and all(cmd[:1] == ["/usr/bin/pkill"] and "-u" in cmd and "agent" in cmd for cmd in seen) and len(seen) == 3
    monkeypatch.setattr(c.subprocess, "run", lambda cmd, **kw: P(0))
    with pytest.raises(RuntimeError, match="remain"): c.kill_agent_processes(attempts=3)
    monkeypatch.setattr(c.subprocess, "run", lambda cmd, **kw: P(2))
    with pytest.raises(RuntimeError, match="pkill"): c.kill_agent_processes()


def test_collector_main_reads_cleared_env(tmp_path, monkeypatch):
    c = _collector(); repo = _tree(tmp_path / "app"); out = tmp_path / "collect"
    monkeypatch.setattr(c, "OUT", out)
    monkeypatch.setattr(c, "kill_agent_processes", lambda: None)
    monkeypatch.setenv("SWESIM_REPO_DIR", str(repo)); monkeypatch.setenv("SWESIM_STAKEHOLDER_URL", ""); monkeypatch.setenv("SWESIM_SESSION_TOKEN", "")
    c.main()
    assert json.loads((out / "collect.status").read_text())["status"] == "ok"
    monkeypatch.delenv("SWESIM_REPO_DIR")
    c.main()  # missing env is a collection failure, not a crash without a receipt
    assert json.loads((out / "collect.status").read_text())["status"] == "collection-failure"


# ---- llm client helpers (offline) --------------------------------------------------------------

def test_llm_helpers(tmp_path, monkeypatch):
    from swesim import llm
    assert llm._extract_json('noise {"a": 1} trailing') == {"a": 1}
    with pytest.raises(llm.LLMError): llm._extract_json("no json here")
    with pytest.raises(llm.LLMError, match="provider"): llm.chat_json("mystery/model", "s", [], "{}")
    envf = tmp_path / ".env"; envf.write_text("ANTHROPIC_API_KEY=file-key\nOTHER=zzz\n")
    monkeypatch.setenv("SWESIM_ENV_FILE", str(envf)); monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False); monkeypatch.delenv("OTHER", raising=False)
    assert llm._secret("ANTHROPIC_API_KEY") == "file-key"
    assert "OTHER" not in os.environ  # only required keys are read; nothing else is exported
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    assert llm._secret("ANTHROPIC_API_KEY") == "env-key"
    with pytest.raises(llm.LLMError, match="OPENAI_API_KEY"): llm._secret("OPENAI_API_KEY")
    assert llm._openai_kwargs("gpt-5.5", 0.0, 100) == {"response_format": {"type": "json_object"}, "max_completion_tokens": 100}
    assert llm._openai_kwargs("gpt-4.1", 0.0, 100)["temperature"] == 0.0
    assert "temperature" not in llm._openai_kwargs("o3", 0.2, 100) and "temperature" not in llm._openai_kwargs("gpt-4.1", None, 100)
