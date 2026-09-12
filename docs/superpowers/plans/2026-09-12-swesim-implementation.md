# swesim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build swesim end to end: a pipeline that turns SWE-bench's human-annotated underspecified instances and any GitHub repository URL into three-condition Harbor tasks with a simulated stakeholder, grades them in a separate offline verifier with executed controls, runs them on Modal under Temporal, and shows every episode in a dashboard.

**Architecture:** Every source produces one validated `ScenarioSpec`; `compile` renders Harbor task directories from templates; the grader runs inside the separate verifier on a tarball collected by a root-owned hook; the stakeholder is a hosted Modal service with a session protocol and a limited tool loop; Temporal orchestrates stages and trials on a GCE VM; `score` and `export` read Harbor job directories and stakeholder transcripts joined by trial id.

**Tech Stack:** Python 3.12, uv, pydantic v2, typer, FastAPI, Harbor 0.23 (`harbor` CLI, Modal backend), Modal 1.5 (web endpoint, Sandbox), Temporal (`temporalio` SDK, `temporal` CLI dev server), `datasets` (Hugging Face), `gh` CLI / GitHub REST, Jinja2, pytest, Anthropic and OpenAI SDKs.

**Spec:** `docs/superpowers/specs/2026-09-12-swe-simulator-design.md` (v2). Reference recipe verified on Modal: `references/modal-smoke-task/`. Reviews: `docs/reviews/`.

## Global Constraints

- Python `>=3.12`; run everything through `uv run`; package name `swesim`; CLI entry `swesim`.
- `ScenarioSpec` and `RunConfig` models use `extra="forbid"`; ids match `^[a-z0-9][a-z0-9._-]{2,63}$`.
- `reward.json` contains only finite numbers (Harbor rejects strings and lists). Everything else goes to `report.json`.
- `success` (0/1) drives pass@k and pass^k; `reward` is partial credit by requirement groups with the regression rule "any PASS_TO_PASS failure sets reward to 0".
- Every `incorrect-exploit` tamper control must earn `reward == 0` or the scenario is `invalid`.
- Agent container: `[agent] user = "agent"`; strict allowlist baseline (model API hosts + stakeholder host) in `[environment]` and `[agent]`; `[verifier] network_mode = "no-network"`, `environment_mode = "separate"`.
- Collection: one `[[verifier.collect]]` hook with `user = "root"` and absolute binary paths: `pkill -KILL -u agent`, fresh root-only `/var/swesim-collect` (mode 700), size check, `tar` to `/var/swesim-collect/app.tar`, `POST /freeze` in interactive, receipt `/var/swesim-collect/collect.status` (rc, files, size_kb, sha, user, transcript_hash). Declare both as **file** artifacts. Never use Harbor directory artifacts for the tree; never trust anything under `/logs/artifacts` (world-writable).
- Protected paths always include: contract test files, `**/conftest.py`, `**/sitecustomize.py`, `**/usercustomize.py`, `**/*.pth`, `pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`, `.github/**`, `.gitattributes`, `.gitignore`, lockfiles. Matching is NFC-normalized and case-folded.
- Tests run as user `tester` with `python -I` and `PYTHONSAFEPATH=1`, `PYTHON*` env cleared; the grader cross-checks JUnit, pytest's summary line, and the exit code.
- Outcomes are keyed on `success`; `reward` is partial credit. swesim computes pass@k and pass^k itself.
- Agent toolchain pre-baked per `references/modal-smoke-task/environment/Dockerfile`; `[environment.env]` has `UV_OFFLINE=1`, `UV_NO_PROGRESS=1`, `UV_CACHE_DIR=/tmp/uv-cache`.
- Grader code that runs inside the verifier container (`swesim/verifier/*.py`) uses the Python standard library only.
- Stakeholder LLM context never contains `author-only` facts, the oracle, or hidden test files.
- Temporal: `TrialWorkflow` id equals `trial_id = sha256(spec_hash, condition, run_config_hash, attempt_index)[:24]`; `launch_harbor` activity has `maximum_attempts=1`.
- Commit after every task with a descriptive message ending in `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; push to `origin main`.
- API keys come from `~/Documents/.env` (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`); export them before `harbor run`. Never commit them.

---

## File structure

```
swesim/
  cli.py                      typer app; one subcommand per stage plus `verify`
  models.py                   ScenarioSpec v2, RunConfig, validators, hashing helpers
  llm.py                      chat(model_id, system, messages, json_schema) for anthropic/* and openai/* ids
  paths.py                    Workspace paths: scenarios/<id>/, tasks/<id>__<cond>/, jobs/, registry.json
  stages/
    mine.py                   dispatch to sources; writes candidates.jsonl
    judge.py                  rubric prompt; judged.jsonl
    draft_spec.py             drafts scenario.draft.json (facts w/ excerpts, persona, requirements, checks, controls, groups)
    compile.py                renders three Harbor task dirs from templates; resolves image digest; compile.json
    validate.py               runs controls via harbor (nop, oracle, patch-agent); validation.json
    register.py               registry.json + dataset.toml
    run.py                    trial ids; harbor run wrapper; transcript fetch
    score.py                  outcomes, pass@k, pass^k, interaction counts; scores.json
    export.py                 episode manifest
  sources/
    threads.py                GitHub REST: issue, comments, PR, review comments (gh auth token)
    swebench_annotated.py     HF datasets + annotation CSV → candidates
    github_repo.py            repo URL → merged PRs → linked issues → filters → candidates; environment build recipe
  verifier/                   (stdlib only; copied into tests/Dockerfile)
    policy.py                 protected-path matching, tar member filtering, change-set computation
    grader.py                 entrypoint: receipt → extract → diff → apply → run → parse → reward.json/report.json
    parsers.py                JUnit XML parser; swebench log parser adapter
  stakeholder/
    service.py                FastAPI app for Modal: /sessions, /ask, /review, /freeze, /transcript
    session.py                Session state machine, patience, counters, idempotent request ids
    tools.py                  read_diff, read_changed_file, run_check (Modal Sandbox)
    prompts.py                stakeholder system prompt builder (visible view only)
    modal_app.py              modal.App wiring for deploy
    shims/ask-user            bash shim
    shims/request-review      bash shim
  agents/
    patch_agent.py            Harbor BaseAgent that applies a named control patch (for validate)
  templates/
    task.toml.j2  instruction.md.j2  environment.Dockerfile.j2  tests.Dockerfile.j2  test.sh  solve.sh.j2
  workflows/
    activities.py  workflows.py  worker.py  client.py
  dashboard/
    app.py  templates/runs.html  templates/trial.html
scripts/
  verify.sh                   milestone checks (used by the /goal condition)
  deploy_stakeholder.sh       modal deploy
  vm/setup.sh vm/swesim-temporal.service vm/swesim-worker.service vm/swesim-dashboard.service
tests/                        pytest unit tests (fast, offline)
examples/                     tracked example task package + sanitized run
PLAN.md                       milestone checklist read by the /goal run
CLAUDE.md                     repo conventions and commands
registry.json
```

## Milestones (each ends with `scripts/verify.sh <n>` passing)

1. Models and hashing (Task 1)
2. Verifier grader with unit tests (Tasks 2, 3)
3. Templates and compile for one SWE-bench scenario; oracle and nop on Modal (Tasks 4, 5, 6)
4. Controls and validate; patch-agent (Task 7)
5. Stakeholder service deployed; shims; one interactive episode (Tasks 8, 9, 10)
6. Sources: swebench-annotated mine/judge/draft; github-repo discovery and build (Tasks 11, 12, 13)
7. Temporal workflows, worker, VM deployment (Tasks 14, 15)
8. Run, score, export; registry; tracked example (Tasks 16, 17)
9. Dashboard (Task 18)
10. Docs: README, grading.md, architecture.md; CLAUDE.md; PLAN.md complete (Task 19)

---

### Task 1: Hardened models, RunConfig, hashing

**Files:**
- Modify: `swesim/models.py` (replace v1 entirely)
- Create: `swesim/paths.py`
- Create: `tests/test_models.py`

**Interfaces:**
- Produces: `ScenarioSpec`, `RunConfig`, `Fact`, `Requirement`, `Contract`, `Controls`, `Group`, `Check`, `Asset`, `Brief`, `Persona`, `Operation`, `Environment`, `Difficulty`; `canonical_hash(model) -> str` (sha256 hex of canonical JSON); `trial_id(spec_hash, condition, run_config_hash, attempt_index) -> str` (24 hex chars); `Workspace(root).scenario_dir(id)`, `.task_dir(id, condition)`, `.jobs_dir`, `.registry_path`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_models.py
import json, pytest
from pydantic import ValidationError
from swesim.models import (ScenarioSpec, RunConfig, canonical_hash, trial_id, minimal_spec_dict)

def test_minimal_spec_validates():
    spec = ScenarioSpec.model_validate(minimal_spec_dict())
    assert spec.spec_hash == canonical_hash(spec)

def test_duplicate_fact_ids_rejected():
    d = minimal_spec_dict(); d["ledger"].append(dict(d["ledger"][0]))
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_author_only_fact_cannot_link_tests():
    d = minimal_spec_dict(); d["ledger"][0]["discovery"] = "author-only"; d["ledger"][0]["tests"] = ["t1"]
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_fact_needs_excerpt_or_authored_rationale():
    d = minimal_spec_dict(); d["ledger"][0].pop("source_excerpt"); d["ledger"][0].pop("source_url")
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_empty_f2p_rejected():
    d = minimal_spec_dict(); d["contract"]["fail_to_pass"] = []
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_groups_must_cover_f2p_exactly_once():
    d = minimal_spec_dict(); d["groups"][0]["tests"] = []
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_extra_fields_forbidden():
    d = minimal_spec_dict(); d["budgets"] = {}
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_asset_refs_must_exist():
    d = minimal_spec_dict(); d["controls"]["oracle"] = "missing.diff"
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

def test_trial_id_is_stable_and_distinct():
    a = trial_id("s", "interactive", "r", 0); b = trial_id("s", "interactive", "r", 1)
    assert a == trial_id("s", "interactive", "r", 0) and a != b and len(a) == 24

def test_runconfig_hash_changes_with_patience():
    r1 = RunConfig.default("interactive"); r2 = r1.model_copy(update={"stakeholder": r1.stakeholder.model_copy(update={"patience_profile": {**r1.stakeholder.patience_profile, "start": 3}})})
    assert r1.run_config_hash != r2.run_config_hash
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_models.py -q`
Expected: FAIL with ImportError (`minimal_spec_dict`, `RunConfig` undefined).

- [ ] **Step 3: Implement models**

```python
# swesim/models.py
"""ScenarioSpec v2 and RunConfig (spec section 5). Strict: extra fields forbidden."""
from __future__ import annotations
import hashlib, json, re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, computed_field

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Source(StrEnum):
    SWEBENCH_ANNOTATED = "swebench-annotated"; GITHUB_REPO = "github-repo"; AUTHORED = "authored"

class FactKind(StrEnum):
    BEHAVIOR = "behavior"; INTERFACE = "interface"; CONSTRAINT = "constraint"; REPRODUCTION = "reproduction"; CONTEXT = "context"

class Provenance(StrEnum):
    ISSUE_THREAD = "issue-thread"; PR_THREAD = "pr-thread"; PR_REVIEW = "pr-review"; ANNOTATOR_NOTE = "annotator-note"; AUTHORED = "authored"

class Discovery(StrEnum):
    BRIEF = "brief"; ON_REQUEST = "on-request"; VOLUNTEERED_AT_REVIEW = "volunteered-at-review"; REPO_DERIVABLE = "repo-derivable"; AUTHOR_ONLY = "author-only"

class OperationType(StrEnum):
    CLARIFICATION = "clarification"; EVALUATOR_REPAIR = "evaluator-repair"; SYNTHETIC_EXTENSION = "synthetic-extension"

class Asset(Strict):
    path: str; sha256: str; bytes: int

class Environment(Strict):
    kind: Literal["prebuilt-image", "dockerfile"]
    image: str | None = None
    image_digest: str | None = None
    dockerfile_asset: str | None = None
    build_context_assets: list[str] = Field(default_factory=list)
    repo_url: str; base_commit: str; language: str = "python"
    repo_dir: str = "/app"
    test_command: str; install_notes: str = ""
    @model_validator(mode="after")
    def _kind_fields(self):
        if self.kind == "prebuilt-image" and not self.image: raise ValueError("prebuilt-image needs image")
        if self.kind == "dockerfile" and not self.dockerfile_asset: raise ValueError("dockerfile kind needs dockerfile_asset")
        return self

class Brief(Strict):
    profile: str; text: str; provenance: Literal["original-issue", "authored"]

class Fact(Strict):
    id: str; text: str; kind: FactKind; provenance: Provenance
    source_excerpt: str | None = None; source_url: str | None = None; authored_rationale: str | None = None
    discovery: Discovery; tests: list[str] = Field(default_factory=list)
    @model_validator(mode="after")
    def _rules(self):
        if not ID_RE.match(self.id): raise ValueError(f"bad fact id {self.id}")
        if self.provenance == Provenance.AUTHORED:
            if not self.authored_rationale: raise ValueError(f"{self.id}: authored fact needs authored_rationale")
        elif not (self.source_excerpt and self.source_url): raise ValueError(f"{self.id}: needs source_excerpt and source_url")
        if self.discovery == Discovery.AUTHOR_ONLY and self.tests: raise ValueError(f"{self.id}: author-only facts cannot link tests")
        return self

class Operation(Strict):
    type: OperationType; target: str; rationale: str

class Persona(Strict):
    summary: str; tone: str; disclosure_style: str; derived_from_urls: list[str] = Field(default_factory=list)
    role: Literal["maintainer", "reporter", "product-owner"] = "maintainer"

class Requirement(Strict):
    id: str; text: str; fact_id: str | None = None; tests: list[str] = Field(default_factory=list); source_url: str | None = None

class Runner(Strict):
    kind: Literal["pytest-junit", "swebench-log"]
    command: str                      # run from repo_dir; for pytest-junit must contain {junit_path}
    repo_parser: str | None = None    # swebench repo key when kind == swebench-log
    test_files: list[str] = Field(default_factory=list)

class Contract(Strict):
    fail_to_pass: list[str]; pass_to_pass: list[str]
    test_assets: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)
    expected_base: dict[str, str] = Field(default_factory=dict)
    expected_gold: dict[str, str] = Field(default_factory=dict)
    runner: Runner

class Check(Strict):
    id: str; description: str; command: str; asset: str | None = None; expected_observation: str

class ControlPatch(Strict):
    name: str; asset: str; rationale: str
    expected_success: int | None = None; expected_reward_max: float | None = None; expected_reward: float | None = None
    cls: Literal["plausible-wrong", "alternative-correct", "incorrect-exploit", "benign-edit"] | None = None
    expected_signals: list[str] = Field(default_factory=list)

class Controls(Strict):
    oracle: str
    plausible_wrong: list[ControlPatch] = Field(default_factory=list)
    alternative_correct: list[ControlPatch] = Field(default_factory=list)
    tamper: list[ControlPatch] = Field(default_factory=list)

class Group(Strict):
    id: str; weight: float; tests: list[str]

class Difficulty(Strict):
    annotated: str | None = None; judged: dict | None = None; measured: dict | None = None

class ScenarioSpec(Strict):
    id: str; version: int = Field(1, ge=1); source: Source; source_ref: str
    created_at: datetime; pipeline_version: str
    environment: Environment
    assets: dict[str, Asset] = Field(default_factory=dict)
    briefs: list[Brief]; ledger: list[Fact]
    operations: list[Operation] = Field(default_factory=list)
    persona: Persona
    requirements: list[Requirement] = Field(default_factory=list)
    contract: Contract; checks: list[Check] = Field(default_factory=list)
    controls: Controls; groups: list[Group]
    difficulty: Difficulty = Field(default_factory=Difficulty)
    thread: dict = Field(default_factory=dict)

    @computed_field
    @property
    def spec_hash(self) -> str: return canonical_hash(self)

    @model_validator(mode="after")
    def _integrity(self):
        if not ID_RE.match(self.id): raise ValueError("bad scenario id")
        if not any(b.profile == "natural" and b.provenance == "original-issue" for b in self.briefs): raise ValueError("natural brief required")
        ids = [f.id for f in self.ledger]
        if len(ids) != len(set(ids)): raise ValueError("duplicate fact ids")
        rids = [r.id for r in self.requirements]
        if len(rids) != len(set(rids)): raise ValueError("duplicate requirement ids")
        if not self.contract.fail_to_pass: raise ValueError("fail_to_pass empty")
        tests = set(self.contract.fail_to_pass) | set(self.contract.pass_to_pass)
        for f in self.ledger:
            for t in f.tests:
                if t not in tests: raise ValueError(f"fact {f.id} links unknown test {t}")
        for r in self.requirements:
            if r.fact_id and r.fact_id not in ids: raise ValueError(f"requirement {r.id} links unknown fact")
            for t in r.tests:
                if t not in tests: raise ValueError(f"requirement {r.id} links unknown test {t}")
        covered = [t for g in self.groups for t in g.tests]
        if sorted(covered) != sorted(self.contract.fail_to_pass): raise ValueError("groups must cover fail_to_pass exactly once")
        if abs(sum(g.weight for g in self.groups) - 1.0) > 1e-6: raise ValueError("group weights must sum to 1")
        refs = [self.controls.oracle] + [c.asset for c in self.controls.plausible_wrong + self.controls.alternative_correct + self.controls.tamper] + self.contract.test_assets + [c.asset for c in self.checks if c.asset]
        if self.environment.dockerfile_asset: refs.append(self.environment.dockerfile_asset)
        for ref in refs:
            if ref not in self.assets: raise ValueError(f"asset ref {ref} not in manifest")
        if not self.controls.plausible_wrong: raise ValueError("at least one plausible_wrong control")
        if not any(c.cls == "incorrect-exploit" for c in self.controls.tamper): raise ValueError("at least one incorrect-exploit tamper control")
        for t in self.checks:
            if t.id in tests: raise ValueError("checks must be disjoint from contract tests")
        return self

class PatienceProfile(Strict):
    start: int = 6; question_cost: int = 1; review_cost: int = 2; terse_below: int = 3; refuse_at: int = 0

class StakeholderConfig(Strict):
    model_id: str = "openai/gpt-5.5"; prompt_version: str = "v1"; temperature: float = 0.0
    patience_profile: PatienceProfile = Field(default_factory=PatienceProfile)
    review_mode: Literal["checks", "diff-only"] = "diff-only"; max_tool_calls_per_activation: int = 4

class RunConfig(Strict):
    agent: str = "mini-swe-agent"; agent_version: str | None = None
    model: str = "anthropic/claude-haiku-4-5-20251001"
    condition: Literal["full", "hidden", "interactive"]
    solver_timeout_sec: int = 2400
    stakeholder: StakeholderConfig = Field(default_factory=StakeholderConfig)
    k: int = 3; harbor_version: str = "0.23.0"; environment_provider: Literal["modal"] = "modal"
    @computed_field
    @property
    def run_config_hash(self) -> str: return canonical_hash(self)
    @classmethod
    def default(cls, condition: str) -> "RunConfig": return cls(condition=condition)

def canonical_hash(model: BaseModel) -> str:
    data = model.model_dump(mode="json", exclude={"spec_hash", "run_config_hash"})
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def trial_id(spec_hash: str, condition: str, run_config_hash: str, attempt_index: int) -> str:
    return hashlib.sha256(f"{spec_hash}|{condition}|{run_config_hash}|{attempt_index}".encode()).hexdigest()[:24]

def minimal_spec_dict() -> dict:
    """A complete, valid scenario used by tests and as an authoring template."""
    return {
        "id": "example-scenario", "version": 1, "source": "authored", "source_ref": "example",
        "created_at": datetime(2026, 9, 12, tzinfo=timezone.utc).isoformat(), "pipeline_version": "0.1.0",
        "environment": {"kind": "prebuilt-image", "image": "ubuntu:24.04", "repo_url": "https://example.com/r.git",
                        "base_commit": "0" * 40, "test_command": "pytest -q", "repo_dir": "/app"},
        "assets": {"oracle.diff": {"path": "assets/oracle.diff", "sha256": "0" * 64, "bytes": 1},
                   "wrong.diff": {"path": "assets/wrong.diff", "sha256": "0" * 64, "bytes": 1},
                   "tamper.diff": {"path": "assets/tamper.diff", "sha256": "0" * 64, "bytes": 1}},
        "briefs": [{"profile": "natural", "text": "It breaks.", "provenance": "original-issue"}],
        "ledger": [{"id": "f1", "text": "Raise ValueError", "kind": "behavior", "provenance": "pr-review",
                    "source_excerpt": "raise ValueError", "source_url": "https://example.com/pr#r1",
                    "discovery": "on-request", "tests": ["t1"]}],
        "persona": {"summary": "maintainer", "tone": "terse", "disclosure_style": "answers what is asked"},
        "requirements": [{"id": "r1", "text": "Raise ValueError", "fact_id": "f1", "tests": ["t1"]}],
        "contract": {"fail_to_pass": ["t1"], "pass_to_pass": ["t0"], "runner": {"kind": "pytest-junit", "command": "pytest -q --junitxml={junit_path}"}},
        "controls": {"oracle": "oracle.diff",
                     "plausible_wrong": [{"name": "keyerror", "asset": "wrong.diff", "rationale": "wrong type", "expected_success": 0, "expected_reward_max": 0.0, "cls": "plausible-wrong"}],
                     "tamper": [{"name": "root-conftest", "asset": "tamper.diff", "rationale": "no-op tests", "expected_reward": 0.0, "cls": "incorrect-exploit"}]},
        "groups": [{"id": "g1", "weight": 1.0, "tests": ["t1"]}],
    }
```

```python
# swesim/paths.py
from pathlib import Path
class Workspace:
    def __init__(self, root: Path | str = "workspace"):
        self.root = Path(root)
    def scenario_dir(self, sid: str) -> Path: return self.root / "scenarios" / sid
    def task_dir(self, sid: str, condition: str) -> Path: return self.root / "tasks" / f"{sid}__{condition}"
    @property
    def jobs_dir(self) -> Path: return self.root / "jobs"
    @property
    def candidates_dir(self) -> Path: return self.root / "candidates"
    @property
    def registry_path(self) -> Path: return Path("registry.json")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_models.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add swesim/models.py swesim/paths.py tests/test_models.py
git commit -m "feat(models): ScenarioSpec v2 with strict validation, RunConfig, trial ids"
```

---

### Task 2: Verifier policy and parsers (stdlib only)

**Files:**
- Create: `swesim/verifier/__init__.py`, `swesim/verifier/policy.py`, `swesim/verifier/parsers.py`
- Create: `tests/test_policy.py`, `tests/test_parsers.py`

**Interfaces:**
- Produces: `policy.is_protected(path, globs) -> bool`; `policy.safe_extract(tar_path, dest) -> ExtractResult(rejected: list[tuple[str,str]], extracted: int)`; `policy.change_set(pristine, tree) -> ChangeSet(added, modified, deleted)` (regular files only, content-hash compare); `policy.apply_change_set(pristine_copy, tree, cs, protected_globs) -> ApplyResult(applied: list[str], dropped: list[str])`; `parsers.parse_junit(xml_path) -> dict[test_id, status]`; `parsers.parse_swebench_log(log_text, repo) -> dict[test_id, status]` (delegates to `swebench.harness.grading` when installed, else raises).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_policy.py
import io, os, tarfile, pathlib
from swesim.verifier import policy

PROTECTED = ["tests/**", "**/conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", ".github/**", ".gitattributes", ".gitignore"]

def test_protected_matching_case_and_depth():
    assert policy.is_protected("tests/test_x.py", PROTECTED)
    assert policy.is_protected("pkg/sub/conftest.py", PROTECTED)
    assert policy.is_protected("Tests/Test_X.py", PROTECTED)   # case-folded
    assert not policy.is_protected("pkg/mod.py", PROTECTED)

def _make_tar(tmp_path, members):
    p = tmp_path / "app.tar"
    with tarfile.open(p, "w") as t:
        for name, kind, data in members:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                ti.size = len(data); t.addfile(ti, io.BytesIO(data))
            elif kind == "sym":
                ti.type = tarfile.SYMTYPE; ti.linkname = data.decode(); t.addfile(ti)
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE; t.addfile(ti)
    return p

def test_safe_extract_rejects_symlinks_and_escapes(tmp_path):
    tar = _make_tar(tmp_path, [("./a.py", "file", b"x"), ("./evil", "sym", b"/etc/passwd"), ("../escape", "file", b"y"), ("./d", "dir", b"")])
    res = policy.safe_extract(tar, tmp_path / "out")
    assert (tmp_path / "out" / "a.py").read_text() == "x"
    kinds = {name for name, _ in res.rejected}
    assert "./evil" in kinds and "../escape" in kinds

def test_change_set_and_apply_drops_protected(tmp_path):
    pristine = tmp_path / "p"; tree = tmp_path / "t"
    for root in (pristine, tree):
        (root / "pkg").mkdir(parents=True); (root / "tests").mkdir()
        (root / "pkg" / "mod.py").write_text("a = 1\n"); (root / "tests" / "test_m.py").write_text("def test(): pass\n")
    (tree / "pkg" / "mod.py").write_text("a = 2\n"); (tree / "pkg" / "new.py").write_text("b\n")
    (tree / "tests" / "test_m.py").write_text("def test(): assert True\n"); (pristine / "pkg" / "gone.py").write_text("z\n")
    cs = policy.change_set(pristine, tree)
    assert set(cs.modified) == {"pkg/mod.py", "tests/test_m.py"} and cs.added == ["pkg/new.py"] and cs.deleted == ["pkg/gone.py"]
    target = tmp_path / "apply"; import shutil; shutil.copytree(pristine, target)
    res = policy.apply_change_set(target, tree, cs, PROTECTED)
    assert (target / "pkg" / "mod.py").read_text() == "a = 2\n" and (target / "pkg" / "new.py").exists() and not (target / "pkg" / "gone.py").exists()
    assert (target / "tests" / "test_m.py").read_text() == "def test(): pass\n" and res.dropped == ["tests/test_m.py"]
```

```python
# tests/test_parsers.py
from swesim.verifier import parsers
JUNIT = """<?xml version="1.0"?><testsuites><testsuite name="s"><testcase classname="tests.test_m" name="test_ok"/><testcase classname="tests.test_m" name="test_bad"><failure message="x"/></testcase><testcase classname="tests.test_m" name="test_skip"><skipped/></testcase></testsuite></testsuites>"""
def test_parse_junit_statuses(tmp_path):
    p = tmp_path / "j.xml"; p.write_text(JUNIT)
    out = parsers.parse_junit(p)
    assert out == {"tests/test_m.py::test_ok": "passed", "tests/test_m.py::test_bad": "failed", "tests/test_m.py::test_skip": "skipped"}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_policy.py tests/test_parsers.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# swesim/verifier/policy.py  (stdlib only)
from __future__ import annotations
import fnmatch, hashlib, os, shutil, tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

def _norm(p: str) -> str:
    return PurePosixPath(p.replace("\\", "/")).as_posix().lstrip("./").casefold()

def is_protected(path: str, globs: list[str]) -> bool:
    n = _norm(path)
    for g in globs:
        gg = _norm(g)
        if fnmatch.fnmatchcase(n, gg): return True
        if gg.startswith("**/") and fnmatch.fnmatchcase(n, gg[3:]): return True
        if gg.endswith("/**") and (n == gg[:-3] or n.startswith(gg[:-3] + "/")): return True
    return False

@dataclass
class ExtractResult:
    extracted: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)

def safe_extract(tar_path: Path, dest: Path) -> ExtractResult:
    dest.mkdir(parents=True, exist_ok=True); res = ExtractResult()
    with tarfile.open(tar_path) as t:
        for m in t.getmembers():
            if m.issym() or m.islnk() or m.isdev() or m.isfifo():
                res.rejected.append((m.name, "non-regular")); continue
            norm = os.path.normpath(m.name)
            if norm.startswith("..") or os.path.isabs(norm):
                res.rejected.append((m.name, "escape")); continue
            try:
                t.extract(m, dest, filter="data"); res.extracted += 1
            except Exception as e:
                res.rejected.append((m.name, type(e).__name__))
    return res

def _hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()

def _files(root: Path) -> dict[str, Path]:
    out = {}
    for p in root.rglob("*"):
        if p.is_symlink() or not p.is_file(): continue
        rel = p.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/"): continue
        out[rel] = p
    return out

@dataclass
class ChangeSet:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

def change_set(pristine: Path, tree: Path) -> ChangeSet:
    a, b = _files(pristine), _files(tree); cs = ChangeSet()
    for rel in sorted(set(a) | set(b)):
        if rel in a and rel not in b: cs.deleted.append(rel)
        elif rel in b and rel not in a: cs.added.append(rel)
        elif _hash(a[rel]) != _hash(b[rel]): cs.modified.append(rel)
    return cs

@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

def apply_change_set(target: Path, tree: Path, cs: ChangeSet, protected: list[str]) -> ApplyResult:
    res = ApplyResult()
    for rel in cs.added + cs.modified:
        if is_protected(rel, protected): res.dropped.append(rel); continue
        dst = target / rel; dst.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(tree / rel, dst); res.applied.append(rel)
    for rel in cs.deleted:
        if is_protected(rel, protected): res.dropped.append(rel); continue
        p = target / rel
        if p.exists(): p.unlink(); res.applied.append(rel)
    return res
```

```python
# swesim/verifier/parsers.py  (stdlib only)
from __future__ import annotations
import xml.etree.ElementTree as ET
from pathlib import Path

def _junit_id(classname: str, name: str) -> str:
    # pytest junit: classname "tests.test_m" -> "tests/test_m.py::test_name"
    if not classname: return name
    parts = classname.split(".")
    return "/".join(parts) + ".py::" + name

def parse_junit(xml_path: Path) -> dict[str, str]:
    root = ET.parse(xml_path).getroot(); out = {}
    for tc in root.iter("testcase"):
        tid = _junit_id(tc.get("classname", ""), tc.get("name", ""))
        status = "passed"
        for child in tc:
            tag = child.tag.lower()
            if tag in ("failure",): status = "failed"
            elif tag in ("error",): status = "error"
            elif tag in ("skipped",): status = "skipped"
        out[tid] = status
    return out

def parse_swebench_log(log_text: str, repo: str) -> dict[str, str]:
    """Adapter over swebench's per-repo log parsers; requires the swebench package in the verifier image."""
    from swebench.harness.grading import MAP_REPO_TO_PARSER  # type: ignore
    parser = MAP_REPO_TO_PARSER[repo]
    raw = parser(log_text) if parser.__code__.co_argcount == 1 else parser(log_text, None)
    return {k: v.lower() for k, v in raw.items()}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_policy.py tests/test_parsers.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add swesim/verifier tests/test_policy.py tests/test_parsers.py
git commit -m "feat(verifier): stdlib path policy, safe extraction, change sets, junit parser"
```

---

### Task 3: Grader entrypoint with partial reward and outcome attribution

**Files:**
- Create: `swesim/verifier/grader.py`
- Create: `tests/test_grader.py`

**Interfaces:**
- Consumes: `policy.*`, `parsers.*`.
- Produces: `grade(cfg: GradeConfig) -> GradeResult` where `GradeConfig(artifacts_dir (= /var/swesim-collect in the verifier), pristine_dir, work_dir, expected_tests: dict[str,str] (id -> "f2p"|"p2p"), groups: list[dict], protected: list[str], runner: dict, test_user: str | None, verifier_out: Path)`; writes `reward.json` and `report.json`. `report.json` also carries `transcript_hash` parsed from the receipt. CLI: `python3 /tests/grader.py --config /tests/grader_config.json`. Outcome codes: 1 valid-correct, 2 valid-incorrect, 3 agent-invalid-submission, 4 agent-runtime-failure, 5 collection-failure, 6 simulator-failure (never set by grader), 7 evaluator-failure.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_grader.py
import json, os, tarfile, io, shutil, subprocess, sys
from pathlib import Path
from swesim.verifier import grader

def _tar_tree(tree: Path, out: Path):
    with tarfile.open(out, "w") as t: t.add(tree, arcname=".")

def _pristine(tmp_path):
    p = tmp_path / "pristine"; (p / "pkg").mkdir(parents=True); (p / "tests").mkdir()
    (p / "pkg" / "__init__.py").write_text("")
    (p / "pkg" / "mod.py").write_text("def f(x):\n    return x\n")
    (p / "tests" / "test_mod.py").write_text("from pkg.mod import f\ndef test_new():\n    assert f(2) == 4\ndef test_old():\n    assert f(0) == 0\n")
    return p

def _cfg(tmp_path, pristine, artifacts):
    out = tmp_path / "verifier_out"; out.mkdir(exist_ok=True)
    return grader.GradeConfig(artifacts_dir=artifacts, pristine_dir=pristine, work_dir=tmp_path / "work",
        expected_tests={"tests/test_mod.py::test_new": "f2p", "tests/test_mod.py::test_old": "p2p"},
        groups=[{"id": "g1", "weight": 1.0, "tests": ["tests/test_mod.py::test_new"]}],
        protected=["tests/**", "**/conftest.py", "**/sitecustomize.py", "**/usercustomize.py", "**/*.pth"],
        runner={"kind": "pytest-junit", "command": sys.executable + " -I -m pytest -q -p no:cacheprovider --junitxml={junit_path} tests"},
        test_user=None, verifier_out=out)

def _artifacts(tmp_path, tree):
    a = tmp_path / "artifacts"; a.mkdir(exist_ok=True); _tar_tree(tree, a / "app.tar")
    (a / "collect.status").write_text(f"rc=0 size_kb=1 files=5 sha={grader.sha256_file(a / 'app.tar')} user=root\n"); return a

def test_correct_patch_scores_success(tmp_path):
    pristine = _pristine(tmp_path); tree = tmp_path / "tree"; shutil.copytree(pristine, tree)
    (tree / "pkg" / "mod.py").write_text("def f(x):\n    return x * 2\n")
    res = grader.grade(_cfg(tmp_path, pristine, _artifacts(tmp_path, tree)))
    r = json.loads((tmp_path / "verifier_out" / "reward.json").read_text())
    assert r["success"] == 1 and r["reward"] == 1.0 and r["outcome_code"] == 1

def test_editing_protected_test_is_dropped_and_signaled(tmp_path):
    pristine = _pristine(tmp_path); tree = tmp_path / "tree"; shutil.copytree(pristine, tree)
    (tree / "tests" / "test_mod.py").write_text("def test_new(): pass\ndef test_old(): pass\n")
    grader.grade(_cfg(tmp_path, pristine, _artifacts(tmp_path, tree)))
    r = json.loads((tmp_path / "verifier_out" / "reward.json").read_text()); rep = json.loads((tmp_path / "verifier_out" / "report.json").read_text())
    assert r["success"] == 0 and r["reward"] == 0.0 and rep["dropped_paths"] == ["tests/test_mod.py"] and r["outcome_code"] == 3

def test_regression_zeroes_partial_reward(tmp_path):
    pristine = _pristine(tmp_path); tree = tmp_path / "tree"; shutil.copytree(pristine, tree)
    (tree / "pkg" / "mod.py").write_text("def f(x):\n    return 4\n")   # new passes, old regresses
    grader.grade(_cfg(tmp_path, pristine, _artifacts(tmp_path, tree)))
    r = json.loads((tmp_path / "verifier_out" / "reward.json").read_text())
    assert r["f2p_passed"] == 1 and r["p2p_passed"] == 0 and r["reward"] == 0.0 and r["success"] == 0 and r["outcome_code"] == 2

def test_missing_receipt_is_collection_failure(tmp_path):
    pristine = _pristine(tmp_path); a = tmp_path / "artifacts"; a.mkdir()
    grader.grade(_cfg(tmp_path, pristine, a))
    r = json.loads((tmp_path / "verifier_out" / "reward.json").read_text()); assert r["outcome_code"] == 5 and r["reward"] == 0

def test_reward_json_is_numeric_only(tmp_path):
    pristine = _pristine(tmp_path); tree = tmp_path / "tree"; shutil.copytree(pristine, tree)
    grader.grade(_cfg(tmp_path, pristine, _artifacts(tmp_path, tree)))
    r = json.loads((tmp_path / "verifier_out" / "reward.json").read_text())
    assert all(isinstance(v, (int, float)) for v in r.values())
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_grader.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# swesim/verifier/grader.py  (stdlib only; runs inside the verifier container)
from __future__ import annotations
import argparse, hashlib, json, os, pwd, re, shutil, subprocess, sys, tempfile, time
from dataclasses import dataclass, field, asdict
from pathlib import Path
if __package__:
    from . import policy, parsers
else:  # executed as /tests/grader.py inside the container
    sys.path.insert(0, str(Path(__file__).parent)); import policy, parsers  # type: ignore

OUTCOMES = {"valid-correct": 1, "valid-incorrect": 2, "agent-invalid-submission": 3, "agent-runtime-failure": 4,
            "collection-failure": 5, "simulator-failure": 6, "evaluator-failure": 7}

def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""): h.update(c)
    return h.hexdigest()

@dataclass
class GradeConfig:
    artifacts_dir: Path; pristine_dir: Path; work_dir: Path
    expected_tests: dict[str, str]; groups: list[dict]; protected: list[str]; runner: dict
    test_user: str | None; verifier_out: Path
    test_timeout_sec: int = 1800

@dataclass
class GradeResult:
    reward: dict; report: dict

def _write(cfg: GradeConfig, reward: dict, report: dict) -> GradeResult:
    cfg.verifier_out.mkdir(parents=True, exist_ok=True)
    (cfg.verifier_out / "reward.json").write_text(json.dumps(reward))
    (cfg.verifier_out / "report.json").write_text(json.dumps(report, indent=2))
    return GradeResult(reward, report)

def _base_reward(outcome: str, **kw) -> dict:
    r = {"reward": 0.0, "success": 0, "f2p_passed": 0, "f2p_expected": 0, "p2p_passed": 0, "p2p_expected": 0,
         "groups_passed": 0, "groups_total": 0, "integrity_signal_count": 0, "outcome_code": OUTCOMES[outcome], "patch_applied": 0}
    r.update(kw); return r

def _parse_receipt(p: Path) -> dict | None:
    if not p.exists(): return None
    text = p.read_text().strip()
    if text == "oversized": return {"oversized": True}
    m = dict(re.findall(r"(\w+)=(\S+)", text)); return m or None  # keys: rc files size_kb sha user transcript_hash

def _run_tests(cfg: GradeConfig, repo: Path) -> tuple[dict[str, str], int, str]:
    junit = repo.parent / f"junit-{os.urandom(6).hex()}.xml"
    cmd = cfg.runner["command"].format(junit_path=junit)
    if cfg.test_user:
        cmd = f"runuser -u {cfg.test_user} -- bash -lc {json.dumps(cmd)}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONSAFEPATH="1")
    try:
        proc = subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True, timeout=cfg.test_timeout_sec, env=env)
        rc, out = proc.returncode, proc.stdout + "\n" + proc.stderr
    except subprocess.TimeoutExpired as e:
        return {}, -9, f"timeout after {cfg.test_timeout_sec}s\n{(e.stdout or b'')[-2000:]}"
    if cfg.runner["kind"] == "pytest-junit":
        statuses = parsers.parse_junit(junit) if junit.exists() else {}
        # three-way cross-check: pytest summary line must agree with the JUnit counts
        m = re.search(r"(\d+) passed", out); passed_summary = int(m.group(1)) if m else None
        if passed_summary is not None and passed_summary != sum(1 for v in statuses.values() if v == "passed"):
            statuses = {k: ("disagree" if v == "passed" else v) for k, v in statuses.items()}
    else:
        statuses = parsers.parse_swebench_log(out, cfg.runner["repo_parser"])
    return statuses, rc, out

def grade(cfg: GradeConfig) -> GradeResult:
    t0 = time.time(); report: dict = {"integrity_signals": [], "rejected_members": [], "dropped_paths": [], "per_test": [], "per_group": []}
    receipt = _parse_receipt(cfg.artifacts_dir / "collect.status"); tar = cfg.artifacts_dir / "app.tar"
    report["receipt"] = receipt or "missing"; report["transcript_hash"] = (receipt or {}).get("transcript_hash")
    if receipt is None or (receipt.get("rc") == "0" and not tar.exists()):
        report["outcome"] = "collection-failure"; return _write(cfg, _base_reward("collection-failure"), report)
    if receipt.get("oversized"):
        report["outcome"] = "agent-invalid-submission"; return _write(cfg, _base_reward("agent-invalid-submission"), report)
    if receipt.get("sha") and tar.exists() and receipt["sha"] != sha256_file(tar):
        report["outcome"] = "collection-failure"; report["receipt_mismatch"] = True; return _write(cfg, _base_reward("collection-failure"), report)
    work = cfg.work_dir; shutil.rmtree(work, ignore_errors=True); tree = work / "tree"
    ext = policy.safe_extract(tar, tree); report["rejected_members"] = ext.rejected
    cs = policy.change_set(cfg.pristine_dir, tree)
    target = work / "repo"; shutil.copytree(cfg.pristine_dir, target, symlinks=False)
    applied = policy.apply_change_set(target, tree, cs, cfg.protected)
    report["dropped_paths"] = applied.dropped
    for d in applied.dropped: report["integrity_signals"].append({"kind": "protected-path-edit", "path": d})
    for name, why in ext.rejected: report["integrity_signals"].append({"kind": "rejected-member", "path": name, "reason": why})
    canon = json.dumps({"added": cs.added, "modified": cs.modified, "deleted": cs.deleted, "applied": applied.applied}, sort_keys=True)
    report["patch_sha256"] = hashlib.sha256(canon.encode()).hexdigest()
    if not applied.applied:
        report["outcome"] = "agent-invalid-submission"
        return _write(cfg, _base_reward("agent-invalid-submission", integrity_signal_count=len(report["integrity_signals"]),
                                        f2p_expected=sum(1 for v in cfg.expected_tests.values() if v == "f2p"),
                                        p2p_expected=sum(1 for v in cfg.expected_tests.values() if v == "p2p")), report)
    if cfg.test_user:
        subprocess.run(["chown", "-R", f"{cfg.test_user}:{cfg.test_user}", str(target)], check=False)
    statuses, rc, out = _run_tests(cfg, target)
    report["runner_exit_code"] = rc; report["runner_tail"] = out[-4000:]
    f2p = [t for t, k in cfg.expected_tests.items() if k == "f2p"]; p2p = [t for t, k in cfg.expected_tests.items() if k == "p2p"]
    if rc == -9 or (not statuses and rc != 0):
        report["outcome"] = "agent-runtime-failure"
        return _write(cfg, _base_reward("agent-runtime-failure", patch_applied=1, f2p_expected=len(f2p), p2p_expected=len(p2p),
                                        integrity_signal_count=len(report["integrity_signals"])), report)
    observed = {t: statuses.get(t, "missing") for t in f2p + p2p}
    # cross-check: if runner exit 0 but any expected test not passed, re-run F2P individually
    if rc == 0 and any(observed[t] != "passed" for t in f2p + p2p) and cfg.runner["kind"] == "pytest-junit":
        for t in f2p:
            single = dict(cfg.runner, command=cfg.runner["command"].replace(" tests", " " + t.split("::")[0] + " -k " + json.dumps(t.split("::")[-1])))
            st, _, _ = _run_tests(GradeConfig(**{**asdict(cfg), "runner": single}), target); observed[t] = st.get(t, observed[t])
    report["per_test"] = [{"id": t, "expected": "passed", "observed": observed[t], "kind": cfg.expected_tests[t]} for t in f2p + p2p]
    f2p_passed = sum(1 for t in f2p if observed[t] == "passed"); p2p_passed = sum(1 for t in p2p if observed[t] == "passed")
    regression = p2p_passed < len(p2p)
    groups_passed = 0; reward = 0.0
    for g in cfg.groups:
        ok = all(observed.get(t) == "passed" for t in g["tests"]); groups_passed += int(ok)
        report["per_group"].append({"id": g["id"], "weight": g["weight"], "passed": ok})
        if ok: reward += float(g["weight"])
    if regression: reward = 0.0
    reward = round(min(max(reward, 0.0), 1.0), 6)
    success = int(f2p_passed == len(f2p) and not regression and len(f2p) > 0)
    outcome = "valid-correct" if success else "valid-incorrect"; report["outcome"] = outcome; report["elapsed_sec"] = round(time.time() - t0, 2)
    return _write(cfg, _base_reward(outcome, reward=reward, success=success, f2p_passed=f2p_passed, f2p_expected=len(f2p), p2p_passed=p2p_passed,
                                    p2p_expected=len(p2p), groups_passed=groups_passed, groups_total=len(cfg.groups),
                                    integrity_signal_count=len(report["integrity_signals"]), patch_applied=1), report)

def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--config", required=True); a = ap.parse_args(argv)
    c = json.loads(Path(a.config).read_text())
    cfg = GradeConfig(artifacts_dir=Path(c["artifacts_dir"]), pristine_dir=Path(c["pristine_dir"]), work_dir=Path(c["work_dir"]),
                      expected_tests=c["expected_tests"], groups=c["groups"], protected=c["protected"], runner=c["runner"],
                      test_user=c.get("test_user"), verifier_out=Path(c["verifier_out"]), test_timeout_sec=c.get("test_timeout_sec", 1800))
    try:
        grade(cfg)
    except Exception as e:  # evaluator failure must still produce a numeric reward
        _write(cfg, _base_reward("evaluator-failure"), {"outcome": "evaluator-failure", "error": repr(e)}); raise

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_grader.py -q`
Expected: 5 passed (the tests use the real pytest via `sys.executable -m pytest` on a temp tree).

- [ ] **Step 5: Commit**

```bash
git add swesim/verifier/grader.py tests/test_grader.py
git commit -m "feat(verifier): grader with receipt check, policy apply, partial reward by groups, outcome codes"
```

---
