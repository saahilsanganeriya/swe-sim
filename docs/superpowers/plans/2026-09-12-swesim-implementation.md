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
    kind: Literal["pytest-ra", "swebench-log"]
    command: str                      # run from repo_dir; must contain {junit_path}; e.g. "pytest -rA -p no:cacheprovider --junitxml={junit_path} tests/test_x.py"
    single_command: str = ""          # same with {nodeid} for per-test re-runs
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
        "contract": {"fail_to_pass": ["t1"], "pass_to_pass": ["t0"], "runner": {"kind": "pytest-ra", "command": "pytest -rA --junitxml={junit_path} tests", "single_command": "pytest -rA --junitxml={junit_path} {nodeid}"}},
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
- Produces: `policy.is_protected(path, globs) -> bool`; `policy.safe_extract(tar_path, dest) -> ExtractResult(rejected: list[tuple[str,str]], extracted: int)`; `policy.change_set(pristine, tree) -> ChangeSet(added, modified, deleted)` (regular files only, content-hash compare); `policy.apply_change_set(pristine_copy, tree, cs, protected_globs) -> ApplyResult(applied: list[str], dropped: list[str])`; `parsers.parse_pytest_ra(stdout) -> dict[test_id, status]` (primary: parses `PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS <nodeid>` lines from `pytest -rA`, ids identical to SWE-bench FAIL_TO_PASS ids); `parsers.parse_junit(xml_path) -> dict[test_id, status]` (cross-check only); `parsers.parse_swebench_log(log_text, repo)` (adapter over the `swebench` package for non-pytest repos; not used in the trial).

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

RA = """tests/test_m.py::test_ok PASSED
=========================== short test summary info ============================
PASSED tests/test_m.py::test_ok
PASSED tests/test_m.py::TestC::test_in_class[param-1]
FAILED tests/test_m.py::test_bad - AssertionError: x
SKIPPED [1] tests/test_m.py:9: reason
XFAIL tests/test_m.py::test_xf
ERROR tests/test_m.py::test_err - fixture
========================= 2 passed, 1 failed, 1 skipped ========================="""
def test_parse_pytest_ra():
    out = parsers.parse_pytest_ra(RA)
    assert out["tests/test_m.py::test_ok"] == "passed"
    assert out["tests/test_m.py::TestC::test_in_class[param-1]"] == "passed"
    assert out["tests/test_m.py::test_bad"] == "failed"
    assert out["tests/test_m.py::test_xf"] == "xfail" and out["tests/test_m.py::test_err"] == "error"
    assert parsers.summary_counts(RA) == {"passed": 2, "failed": 1, "skipped": 1}
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
import re
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

_RA = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+)")
def parse_pytest_ra(stdout: str) -> dict[str, str]:
    """Parse the `-rA` short test summary. SKIPPED lines carry a location, not a nodeid, and are ignored
    (a skipped expected test simply stays 'missing', which the grader counts as failed)."""
    out = {}
    for line in stdout.splitlines():
        m = _RA.match(line.strip())
        if not m: continue
        status, nodeid = m.group(1), m.group(2)
        if status == "SKIPPED": continue
        out[nodeid] = {"PASSED": "passed", "FAILED": "failed", "ERROR": "error", "XFAIL": "xfail", "XPASS": "xpass"}[status]
    return out

_SUM = re.compile(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)")
def summary_counts(stdout: str) -> dict[str, int]:
    counts = {}
    for line in stdout.splitlines()[::-1]:
        if "passed" in line or "failed" in line:
            for n, k in _SUM.findall(line): counts[k.rstrip("s") if k.startswith("error") else k] = int(n)
            if counts: break
    return counts

def parse_swebench_log(log_text: str, repo: str) -> dict[str, str]:
    """Adapter over swebench's per-repo log parsers; requires the swebench package in the verifier image."""
    from swebench.harness.grading import MAP_REPO_TO_PARSER  # type: ignore
    parser = MAP_REPO_TO_PARSER[repo]
    raw = parser(log_text) if parser.__code__.co_argcount == 1 else parser(log_text, None)
    return {k: v.lower() for k, v in raw.items()}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_policy.py tests/test_parsers.py -q`
Expected: 5 passed.

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
        runner={"kind": "pytest-ra", "command": sys.executable + " -I -m pytest -rA -p no:cacheprovider --junitxml={junit_path} tests",
                "single_command": sys.executable + " -I -m pytest -rA -p no:cacheprovider --junitxml={junit_path} {nodeid}"},
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

def junit_path_for(cfg: GradeConfig) -> Path:
    return cfg.work_dir / f"junit-{os.urandom(6).hex()}.xml"

def _run_tests(cfg: GradeConfig, repo: Path) -> tuple[dict[str, str], int, str]:
    junit = junit_path_for(cfg)
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
    kind = cfg.runner["kind"]
    if kind in ("pytest-ra", "pytest-junit"):
        statuses = parsers.parse_pytest_ra(out)
        # cross-checks: the summary line and (when present) the JUnit file must agree with the -rA lines
        counts = parsers.summary_counts(out)
        if counts.get("passed") is not None and counts["passed"] != sum(1 for v in statuses.values() if v == "passed"):
            statuses = {k: ("disagree" if v == "passed" else v) for k, v in statuses.items()}
        if junit.exists():
            j = parsers.parse_junit(junit)
            if sum(1 for v in j.values() if v == "passed") != sum(1 for v in statuses.values() if v == "passed"):
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

### Task 4: LLM client and Harbor task templates + compile

**Files:**
- Create: `swesim/llm.py`, `swesim/templates/task.toml.j2`, `swesim/templates/instruction.md.j2`, `swesim/templates/environment.Dockerfile.j2`, `swesim/templates/tests.Dockerfile.j2`, `swesim/templates/test.sh`, `swesim/templates/solve.sh.j2`, `swesim/templates/collect.sh.j2`, `swesim/stages/__init__.py`, `swesim/stages/compile.py`
- Create: `tests/test_compile.py`

**Interfaces:**
- Consumes: `ScenarioSpec`, `RunConfig`, `Workspace`.
- Produces: `llm.chat_json(model_id: str, system: str, messages: list[dict], schema_hint: str) -> dict` (routes `anthropic/*` to the Anthropic SDK and `openai/*` to the OpenAI SDK; temperature from caller; raises `LLMError`); `compile.compile_scenario(ws, spec, run_config, stakeholder_url) -> CompileResult(task_dirs: dict[condition, Path], image_digest: str | None, compile_json: Path)`. Task dir layout per Harbor: `task.toml`, `instruction.md`, `environment/Dockerfile`, `environment/shims/ask-user`, `environment/shims/request-review`, `tests/Dockerfile`, `tests/test.sh`, `tests/grader.py`, `tests/policy.py`, `tests/parsers.py`, `tests/grader_config.json`, `tests/test.patch` (hidden tests), `solution/solve.sh`, `solution/oracle.diff`, `controls/<name>.diff`.

- [ ] **Step 1: Write failing test**

```python
# tests/test_compile.py
import json, tomllib
from pathlib import Path
from swesim.models import ScenarioSpec, RunConfig, minimal_spec_dict
from swesim.paths import Workspace
from swesim.stages import compile as comp

def test_compile_renders_three_conditions(tmp_path):
    ws = Workspace(tmp_path / "ws"); d = minimal_spec_dict(); spec = ScenarioSpec.model_validate(d)
    sdir = ws.scenario_dir(spec.id); (sdir / "assets").mkdir(parents=True)
    for name in ("oracle.diff", "wrong.diff", "tamper.diff"): (sdir / "assets" / name).write_text("diff --git a/x b/x\n")
    (sdir / "assets" / "test.patch").write_text("diff --git a/tests/t.py b/tests/t.py\n")
    res = comp.compile_scenario(ws, spec, RunConfig.default("interactive"), stakeholder_url="https://x.modal.run", resolve_digest=False)
    assert set(res.task_dirs) == {"full", "hidden", "interactive"}
    t = tomllib.loads((res.task_dirs["interactive"] / "task.toml").read_text())
    assert t["agent"]["user"] == "agent" and t["environment"]["network_mode"] == "allowlist"
    assert "x.modal.run" in t["environment"]["allowed_hosts"] and t["verifier"]["environment_mode"] == "separate"
    assert t["artifacts"] == ["/var/swesim-collect/app.tar", "/var/swesim-collect/collect.status"]
    assert t["verifier"]["collect"][0]["user"] == "root" and "/usr/bin/pkill -KILL -u agent" in t["verifier"]["collect"][0]["command"]
    assert t["environment"]["env"]["UV_OFFLINE"] == "1"
    ins = (res.task_dirs["interactive"] / "instruction.md").read_text(); assert "ask-user" in ins and "It breaks." in ins
    full = (res.task_dirs["full"] / "instruction.md").read_text(); assert "Raise ValueError" in full and "ask-user" not in full
    hidden = (res.task_dirs["hidden"] / "instruction.md").read_text(); assert "Raise ValueError" not in hidden
    cfg = json.loads((res.task_dirs["full"] / "tests" / "grader_config.json").read_text())
    assert cfg["expected_tests"] == {"t1": "f2p", "t0": "p2p"} and cfg["artifacts_dir"] == "/var/swesim-collect"
    assert (res.task_dirs["full"] / "tests" / "grader.py").exists() and (res.task_dirs["full"] / "solution" / "solve.sh").exists()

def test_full_brief_excludes_author_only(tmp_path):
    ws = Workspace(tmp_path / "ws"); d = minimal_spec_dict()
    d["ledger"].append({"id": "f9", "text": "SECRET-HELPER-NAME", "kind": "interface", "provenance": "authored", "authored_rationale": "private name", "discovery": "author-only"})
    spec = ScenarioSpec.model_validate(d); sdir = ws.scenario_dir(spec.id); (sdir / "assets").mkdir(parents=True)
    for name in ("oracle.diff", "wrong.diff", "tamper.diff", "test.patch"): (sdir / "assets" / name).write_text("x\n")
    res = comp.compile_scenario(ws, spec, RunConfig.default("full"), stakeholder_url="https://x.modal.run", resolve_digest=False)
    for cond in ("full", "hidden", "interactive"):
        assert "SECRET-HELPER-NAME" not in (res.task_dirs[cond] / "instruction.md").read_text()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_compile.py -q` → ImportError.

- [ ] **Step 3: Implement llm.py**

```python
# swesim/llm.py
from __future__ import annotations
import json, os, re
class LLMError(RuntimeError): pass

def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m: raise LLMError(f"no JSON in model output: {text[:200]}")
    return json.loads(m.group(0))

def chat_json(model_id: str, system: str, messages: list[dict], schema_hint: str, temperature: float = 0.0, max_tokens: int = 4000) -> dict:
    """messages: [{"role": "user"|"assistant", "content": str}]. Returns parsed JSON. Provider by id prefix."""
    provider, _, name = model_id.partition("/")
    sys_prompt = system + "\n\nRespond with a single JSON object only, matching this schema:\n" + schema_hint
    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        r = client.messages.create(model=name, system=sys_prompt, messages=messages, max_tokens=max_tokens, temperature=temperature)
        return _extract_json("".join(b.text for b in r.content if getattr(b, "type", "") == "text"))
    if provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        r = client.chat.completions.create(model=name, messages=[{"role": "system", "content": sys_prompt}, *messages], temperature=temperature, response_format={"type": "json_object"})
        return _extract_json(r.choices[0].message.content or "")
    raise LLMError(f"unknown provider in model id {model_id}")
```

- [ ] **Step 4: Write templates**

`swesim/templates/task.toml.j2`:
```toml
schema_version = "1.4"
artifacts = ["/var/swesim-collect/app.tar", "/var/swesim-collect/collect.status"]

[task]
name = "swesim/{{ task_name }}"
version = "{{ spec.version }}"
description = "{{ spec.source }} {{ spec.source_ref }} [{{ condition }}]"

[metadata]
source = "{{ spec.source }}"
source_ref = "{{ spec.source_ref }}"
spec_hash = "{{ spec.spec_hash }}"
condition = "{{ condition }}"
run_config_hash = "{{ run_config.run_config_hash }}"
annotated_difficulty = "{{ spec.difficulty.annotated or 'unknown' }}"
image_digest = "{{ image_digest or '' }}"

[verifier]
timeout_sec = {{ verifier_timeout }}
network_mode = "no-network"
environment_mode = "separate"

[verifier.environment]
build_timeout_sec = 1800.0

[[verifier.collect]]
user = "root"
timeout_sec = 300.0
command = "{{ collect_command }}"

[agent]
timeout_sec = {{ run_config.solver_timeout_sec }}
user = "agent"
network_mode = "allowlist"
allowed_hosts = {{ agent_hosts | tojson }}

[environment]
network_mode = "allowlist"
allowed_hosts = {{ agent_hosts | tojson }}
build_timeout_sec = 3600.0
os = "linux"
cpus = 2
memory_mb = 8192
storage_mb = 20480

[environment.env]
UV_OFFLINE = "1"
UV_NO_PROGRESS = "1"
UV_CACHE_DIR = "/tmp/uv-cache"
SWESIM_STAKEHOLDER_URL = "{{ stakeholder_url }}"
SWESIM_SESSION_TOKEN = "{{ session_token }}"
SWESIM_TRIAL_ID = "{{ trial_id }}"
SWESIM_REPO_DIR = "{{ spec.environment.repo_dir }}"
```

`swesim/templates/collect.sh.j2` (rendered into one line for `command`; kept as a file for readability and tests):
```bash
/usr/bin/pkill -KILL -u agent >/dev/null 2>&1 || true
/bin/rm -rf /var/swesim-collect && /bin/mkdir -m 700 /var/swesim-collect
cd {{ repo_dir }} || { echo "rc=101 user=$(/usr/bin/id -un)" > /var/swesim-collect/collect.status; exit 0; }
SZ=$(/usr/bin/du -sk --exclude=.git --exclude=.venv . | /usr/bin/cut -f1)
if [ "$SZ" -gt 512000 ]; then echo oversized > /var/swesim-collect/collect.status; exit 0; fi
/usr/bin/tar --numeric-owner --exclude=./.git --exclude=./.venv --exclude=./node_modules --exclude='./**/__pycache__' -cf /var/swesim-collect/app.tar . ; RC=$?
TH=""
if [ -n "$SWESIM_SESSION_TOKEN" ]; then TH=$(/usr/bin/curl -s -m 20 -X POST "$SWESIM_STAKEHOLDER_URL/freeze" -H "Authorization: Bearer $SWESIM_SESSION_TOKEN" | /usr/bin/python3 -c 'import sys,json; print(json.load(sys.stdin).get("transcript_hash",""))' 2>/dev/null || true); fi
echo "rc=$RC size_kb=$SZ files=$(/usr/bin/tar -tf /var/swesim-collect/app.tar 2>/dev/null | /usr/bin/wc -l | /usr/bin/tr -d ' ') sha=$(/usr/bin/sha256sum /var/swesim-collect/app.tar | /usr/bin/cut -d' ' -f1) user=$(/usr/bin/id -un) transcript_hash=$TH" > /var/swesim-collect/collect.status
```

`swesim/templates/instruction.md.j2`:
```
{{ brief }}

{% if condition == "full" %}
## Requirements (complete)
{% for f in visible_facts %}- {{ f.text }}
{% endfor %}
No stakeholder is available for questions.
{% elif condition == "hidden" %}
No stakeholder is available for questions.
{% else %}
## Working with the stakeholder
A stakeholder who filed this request is available. They can answer a limited number of questions before they have to leave; investigate what you can from the repository first.
- Ask a question: `ask-user "your question"`
- Request a review of your current changes: `request-review`
{% endif %}

Repository: `{{ repo_dir }}`. Time budget: {{ solver_minutes }} minutes. Work in place; your changes are collected automatically when you finish.
```

`swesim/templates/environment.Dockerfile.j2`:
```dockerfile
FROM {{ base_image }}
USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN (apt-get update && apt-get install -y --no-install-recommends curl bash build-essential git ca-certificates python3 procps && rm -rf /var/lib/apt/lists/*) || true
RUN id agent >/dev/null 2>&1 || useradd -m -u 1001 agent
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh && uv --version
{% if spec.environment.kind == "dockerfile" %}
RUN git clone {{ spec.environment.repo_url }} {{ repo_dir }} && cd {{ repo_dir }} && git checkout -q {{ spec.environment.base_commit }} \
 && {{ install_command }}
{% endif %}
# git time travel (DeepSWE v1.1): real branch at base, no remotes, no future refs, reflog expired
RUN cd {{ repo_dir }} && git checkout -q -B main {{ spec.environment.base_commit }} \
 && (git remote remove origin || true) \
 && for b in $(git for-each-ref --format='%(refname:short)' refs/heads | grep -vx main); do git branch -D "$b" || true; done \
 && for t in $(git tag); do git merge-base --is-ancestor "$t" HEAD 2>/dev/null || git tag -d "$t"; done \
 && git reflog expire --expire=now --all && git gc --prune=now -q && git config core.hooksPath /dev/null
RUN mkdir -m 700 /var/swesim-collect && mkdir -p /opt/swesim/bin
COPY shims/ask-user shims/request-review /opt/swesim/bin/
RUN chmod 755 /opt/swesim/bin/* && ln -sf /opt/swesim/bin/ask-user /usr/local/bin/ask-user && ln -sf /opt/swesim/bin/request-review /usr/local/bin/request-review
RUN chown -R agent:agent {{ repo_dir }}
USER agent
ENV HOME=/home/agent
RUN uv python install {{ agent_python }} && uv tool install --python {{ agent_python }} mini-swe-agent --with litellm --with orjson --with fastapi \
 && /home/agent/.local/bin/mini-swe-agent --help >/dev/null && echo 'export PATH="$HOME/.local/bin:$PATH"' >> /home/agent/.bashrc
USER root
RUN chown -R agent:agent /home/agent
WORKDIR {{ repo_dir }}
```

`swesim/templates/tests.Dockerfile.j2`:
```dockerfile
FROM {{ base_image }}
USER root
RUN (apt-get update && apt-get install -y --no-install-recommends python3 git && rm -rf /var/lib/apt/lists/*) || true
RUN id tester >/dev/null 2>&1 || useradd -m tester
{% if spec.environment.kind == "dockerfile" %}
RUN git clone {{ spec.environment.repo_url }} {{ repo_dir }} && cd {{ repo_dir }} && git checkout -q {{ spec.environment.base_commit }} && {{ install_command }}
{% endif %}
RUN cp -a {{ repo_dir }} /pristine && cd /pristine && git config core.hooksPath /dev/null
COPY test.patch /tests/test.patch
RUN cd /pristine && git apply --whitespace=nowarn /tests/test.patch && chown -R tester:tester /pristine
COPY grader.py policy.py parsers.py grader_config.json test.sh /tests/
RUN chmod +x /tests/test.sh
```

`swesim/templates/test.sh`:
```bash
#!/bin/bash
mkdir -p /logs/verifier
trap 'if [ ! -f /logs/verifier/reward.json ]; then echo "{\"reward\": 0, \"success\": 0, \"outcome_code\": 7}" > /logs/verifier/reward.json; fi' EXIT
{{ python }} /tests/grader.py --config /tests/grader_config.json 2>&1 | tee /logs/verifier/grader.log
```

`swesim/templates/solve.sh.j2`:
```bash
#!/bin/bash
set -e
cd {{ repo_dir }}
git apply --whitespace=nowarn /solution/oracle.diff
```

- [ ] **Step 5: Implement compile.py**

```python
# swesim/stages/compile.py
from __future__ import annotations
import json, shutil, stat, urllib.request
from dataclasses import dataclass
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from swesim.models import ScenarioSpec, RunConfig, Discovery
from swesim.paths import Workspace

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
VERIFIER_SRC = Path(__file__).resolve().parent.parent / "verifier"
SHIMS = Path(__file__).resolve().parent.parent / "stakeholder" / "shims"
MODEL_HOSTS = {"anthropic": ["api.anthropic.com"], "openai": ["api.openai.com"]}
AGENT_PYTHON = "3.12"   # Harbor's mini-swe-agent adapter pin (_MINI_SWE_AGENT_PYTHON_VERSION)

@dataclass
class CompileResult:
    task_dirs: dict[str, Path]; image_digest: str | None; compile_json: Path

def resolve_docker_hub_digest(image: str) -> str | None:
    """Anonymous Docker Hub manifest HEAD: returns sha256 digest or None."""
    try:
        repo, _, tag = image.partition(":"); tag = tag or "latest"
        if "/" not in repo: repo = "library/" + repo
        tok = json.load(urllib.request.urlopen(f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"))["token"]
        req = urllib.request.Request(f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}", method="HEAD",
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.index.v1+json"})
        return urllib.request.urlopen(req).headers.get("Docker-Content-Digest")
    except Exception:
        return None

def visible_facts(spec: ScenarioSpec):
    return [f for f in spec.ledger if f.discovery != Discovery.AUTHOR_ONLY]

def compile_scenario(ws: Workspace, spec: ScenarioSpec, run_config: RunConfig, stakeholder_url: str, resolve_digest: bool = True,
                     session_token: str = "", trial_id: str = "") -> CompileResult:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), keep_trailing_newline=True)
    sdir = ws.scenario_dir(spec.id); assets = sdir / "assets"
    digest = resolve_docker_hub_digest(spec.environment.image) if (resolve_digest and spec.environment.kind == "prebuilt-image") else None
    base_image = spec.environment.image if spec.environment.kind == "prebuilt-image" else "python:3.12-slim"
    if digest and spec.environment.kind == "prebuilt-image": base_image = f"{spec.environment.image.split(':')[0]}@{digest}"
    hosts = MODEL_HOSTS[run_config.model.split("/")[0]] + [stakeholder_url.replace("https://", "").split("/")[0]]
    natural = next(b for b in spec.briefs if b.profile == "natural")
    collect = env.get_template("collect.sh.j2").render(repo_dir=spec.environment.repo_dir).strip().replace("\n", " ; ").replace('"', '\\"')
    out: dict[str, Path] = {}
    for condition in ("full", "hidden", "interactive"):
        tdir = ws.task_dir(spec.id, condition); shutil.rmtree(tdir, ignore_errors=True)
        (tdir / "environment" / "shims").mkdir(parents=True); (tdir / "tests").mkdir(); (tdir / "solution").mkdir(); (tdir / "controls").mkdir()
        ctx = dict(spec=spec, run_config=run_config, condition=condition, task_name=f"{spec.id}--{condition}", image_digest=digest,
                   agent_hosts=hosts, stakeholder_url=stakeholder_url, session_token=session_token if condition == "interactive" else "",
                   trial_id=trial_id, collect_command=collect, verifier_timeout=1800.0, repo_dir=spec.environment.repo_dir,
                   base_image=base_image, install_command=spec.environment.install_notes or "true", agent_python=AGENT_PYTHON,
                   brief=natural.text, visible_facts=visible_facts(spec), solver_minutes=run_config.solver_timeout_sec // 60, python="/usr/bin/python3")
        (tdir / "task.toml").write_text(env.get_template("task.toml.j2").render(**ctx))
        (tdir / "instruction.md").write_text(env.get_template("instruction.md.j2").render(**ctx))
        (tdir / "environment" / "Dockerfile").write_text(env.get_template("environment.Dockerfile.j2").render(**ctx))
        for shim in ("ask-user", "request-review"):
            dst = tdir / "environment" / "shims" / shim; shutil.copyfile(SHIMS / shim, dst); dst.chmod(dst.stat().st_mode | stat.S_IEXEC)
        (tdir / "tests" / "Dockerfile").write_text(env.get_template("tests.Dockerfile.j2").render(**ctx))
        (tdir / "tests" / "test.sh").write_text(env.get_template("test.sh").render(**ctx) if False else (TEMPLATES / "test.sh").read_text().replace("{{ python }}", "/usr/bin/python3"))
        for f in ("grader.py", "policy.py", "parsers.py"): shutil.copyfile(VERIFIER_SRC / f, tdir / "tests" / f)
        shutil.copyfile(assets / "test.patch", tdir / "tests" / "test.patch")
        expected = {t: "f2p" for t in spec.contract.fail_to_pass} | {t: "p2p" for t in spec.contract.pass_to_pass}
        grader_cfg = {"artifacts_dir": "/var/swesim-collect", "pristine_dir": "/pristine", "work_dir": "/tmp/swesim-work",
                      "expected_tests": expected, "groups": [g.model_dump() for g in spec.groups],
                      "protected": spec.contract.protected_paths + ["**/conftest.py", "**/sitecustomize.py", "**/usercustomize.py", "**/*.pth", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", ".github/**", ".gitattributes", ".gitignore"],
                      "runner": spec.contract.runner.model_dump(), "test_user": "tester", "verifier_out": "/logs/verifier", "test_timeout_sec": 1500}
        (tdir / "tests" / "grader_config.json").write_text(json.dumps(grader_cfg, indent=2))
        (tdir / "solution" / "solve.sh").write_text(env.get_template("solve.sh.j2").render(**ctx)); (tdir / "solution" / "solve.sh").chmod(0o755)
        shutil.copyfile(assets / spec.assets[spec.controls.oracle].path.split("/")[-1], tdir / "solution" / "oracle.diff")
        for c in spec.controls.plausible_wrong + spec.controls.alternative_correct + spec.controls.tamper:
            shutil.copyfile(assets / spec.assets[c.asset].path.split("/")[-1], tdir / "controls" / f"{c.name}.diff")
        out[condition] = tdir
    cj = sdir / "compile.json"
    cj.write_text(json.dumps({"spec_hash": spec.spec_hash, "run_config_hash": run_config.run_config_hash, "image_digest": digest,
                              "task_dirs": {k: str(v) for k, v in out.items()}}, indent=2))
    return CompileResult(out, digest, cj)
```

Note for the implementer: the `test.sh` line above must simply copy the template and substitute the python path; keep it as written (no Jinja on that file). The shims are written in Task 8; until then create two placeholder executables `swesim/stakeholder/shims/ask-user` and `request-review` containing `#!/bin/bash\necho "stakeholder not available"` so compile can copy them.

- [ ] **Step 6: Run tests, commit**

Run: `uv run pytest tests/test_compile.py -q` → 2 passed.
```bash
git add swesim/llm.py swesim/templates swesim/stages swesim/stakeholder/shims tests/test_compile.py
git commit -m "feat(compile): Harbor task templates (strict allowlist, root collect hook, separate verifier) and compile stage"
```

---

### Task 5: patch-agent and reachability probe for validation

**Files:**
- Create: `swesim/agents/__init__.py`, `swesim/agents/patch_agent.py`
- Create: `tests/test_patch_agent.py`

**Interfaces:**
- Produces: `PatchAgent(BaseAgent)` registered by import path `swesim.agents.patch_agent:PatchAgent`; kwargs `patch` (host path to a diff, uploaded and applied with `git apply` in `$SWESIM_REPO_DIR` as the agent user) or `probe=true` (runs the reachability probe and writes `/logs/agent/probe.json`: `{"reflog_entries": n, "all_refs": n, "gold_line_hits": [...]}` given kwarg `gold_lines` (JSON list)). Harbor invocation: `harbor run -p <task> --agent swesim.agents.patch_agent:PatchAgent --ak patch=/abs/path.diff -e modal`.

- [ ] **Step 1: Read Harbor's oracle agent to mirror its API**

Run: `sed -n '1,140p' ~/.local/share/uv/tools/harbor/lib/python3.13/site-packages/harbor/agents/oracle.py` and `grep -n "def \|class " ~/.local/share/uv/tools/harbor/lib/python3.13/site-packages/harbor/agents/base.py | head -40`. Use the same base class, `setup`, `run(instruction, environment, context)`, `environment.upload_file(source, target)` and `environment.exec(command, user=...)` signatures you see there. The code below assumes `BaseAgent.__init__(self, logs_dir, model_name=None, **kwargs)`; adjust to the actual signature.

- [ ] **Step 2: Write the test (unit level: command construction)**

```python
# tests/test_patch_agent.py
from swesim.agents.patch_agent import build_apply_command, build_probe_command
def test_apply_command_targets_repo_dir():
    cmd = build_apply_command("/testbed", "/tmp/control.diff")
    assert cmd.startswith("cd /testbed && git apply --whitespace=nowarn /tmp/control.diff")
def test_probe_command_greps_gold_lines():
    cmd = build_probe_command("/testbed", ["def new_helper(", "raise ValueError(\"dim\""])
    assert "git rev-list --all" in cmd and "grep -rF" in cmd and "new_helper" in cmd
```

- [ ] **Step 3: Implement**

```python
# swesim/agents/patch_agent.py
from __future__ import annotations
import json, shlex
from pathlib import Path
from harbor.agents.base import BaseAgent          # verify path in step 1
from harbor.models.agent.context import AgentContext

def build_apply_command(repo_dir: str, remote_patch: str) -> str:
    return f"cd {shlex.quote(repo_dir)} && git apply --whitespace=nowarn {shlex.quote(remote_patch)} && git status --porcelain | head -50"

def build_probe_command(repo_dir: str, gold_lines: list[str]) -> str:
    greps = " ; ".join(f"grep -rF --exclude-dir=.git -l -- {shlex.quote(l)} . | head -5" for l in gold_lines)
    return (f"cd {shlex.quote(repo_dir)} && echo REFS=$(git rev-list --all | wc -l) && echo REFLOG=$(git reflog | wc -l) "
            f"&& echo HITS_BEGIN && ({greps}) ; echo HITS_END")

class PatchAgent(BaseAgent):
    """Applies a named control patch (validate stage) or runs the history/gold-line reachability probe."""
    def __init__(self, logs_dir: Path, model_name: str | None = None, patch: str | None = None, probe: bool = False, gold_lines: str = "[]", **kw):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kw)
        self._patch = patch; self._probe = probe in (True, "true", "1"); self._gold_lines = json.loads(gold_lines)
    @staticmethod
    def name() -> str: return "patch-agent"
    async def setup(self, environment) -> None: return None
    async def run(self, instruction: str, environment, context: AgentContext) -> None:
        repo_dir = "${SWESIM_REPO_DIR:-/app}"
        if self._probe:
            r = await environment.exec(f"bash -lc {shlex.quote(build_probe_command(repo_dir, self._gold_lines))}")
            (self.logs_dir / "probe.txt").write_text(r.stdout + "\n" + r.stderr); return
        assert self._patch, "patch kwarg required"
        await environment.upload_file(Path(self._patch), "/tmp/control.diff")
        r = await environment.exec(f"bash -lc {shlex.quote(build_apply_command(repo_dir, '/tmp/control.diff'))}")
        (self.logs_dir / "apply.txt").write_text(f"rc={r.return_code}\n{r.stdout}\n{r.stderr}")
        if r.return_code != 0: raise RuntimeError(f"control patch failed to apply: {r.stderr[-500:]}")
```

- [ ] **Step 4: Run, then smoke on Modal against the reference task**

Run: `uv run pytest tests/test_patch_agent.py -q` → 2 passed.
Run (adjust the diff to touch `references/modal-smoke-task` repo dir `/app`): `printf 'diff --git a/hello.txt b/hello.txt\nnew file mode 100644\n--- /dev/null\n+++ b/hello.txt\n@@ -0,0 +1 @@\n+hello\n' > /tmp/hello.diff && cd references/modal-smoke-task && git -C /tmp init -q 2>/dev/null; uv run harbor run -p . --agent swesim.agents.patch_agent:PatchAgent --ak patch=/tmp/hello.diff -e modal --jobs-dir /tmp/swesim/jobs --job-name patch-agent-smoke --yes` (the smoke image has no git repo at /app; if `git apply` fails, use `--ak probe=true` to at least confirm the agent loads, and re-run the patch path on the first compiled scenario in Task 6).

- [ ] **Step 5: Commit**

```bash
git add swesim/agents tests/test_patch_agent.py
git commit -m "feat(agents): patch-agent for control patches and history reachability probe"
```

---

### Task 6: SWE-bench annotated source: mine, judge, draft_spec

**Files:**
- Create: `swesim/sources/__init__.py`, `swesim/sources/threads.py`, `swesim/sources/swebench_annotated.py`, `swesim/stages/mine.py`, `swesim/stages/judge.py`, `swesim/stages/draft_spec.py`, `swesim/tamper.py`
- Create: `tests/test_swebench_source.py`, `tests/test_tamper.py`

**Interfaces:**
- Produces: `swebench_annotated.pool_a(csv_path) -> list[str]` instance ids; `swebench_annotated.candidates(instance_ids, out_path, pytest_repos_only=True) -> int` writes `candidates.jsonl` rows `{instance_id, repo, base_commit, version, problem_statement, hints_text, fail_to_pass, pass_to_pass, patch, test_patch, image, annotation: {underspecified, underspecified_notes, false_negative, false_negative_notes, difficulty}, thread: {issue_url, issue_comments, pr_url, pr_comments, review_comments}}`; `threads.fetch_pr_thread(repo, pr_number) -> dict` and `threads.fetch_issue_thread(repo, issue_number) -> dict` via `gh api`; `judge.judge_candidate(row, model_id) -> dict` with keys `complexity, spec_gap, test_behavioralness, self_contained, missing, thread_supplies, legitimate_requirement, alternatives_accepted, non_derivable_choice, engineering_consequence, keep`; `draft_spec.draft(row, judged, model_id, out_dir) -> Path` writing `scenario.draft.json` plus `assets/` (oracle.diff, test.patch, control diffs) with sha256 manifest; `tamper.root_conftest_noop(repo_test_paths) -> str`, `tamper.skip_marker(test_patch_text) -> str`, `tamper.sitecustomize_junit_forge() -> str` (code-generated diffs).
- Image name rule: `swebench/sweb.eval.x86_64.` + instance_id with `__` replaced by `_1776_`, tag `latest`. Repo dir in SWE-bench images: `/testbed`. Runner: `pytest-ra` with command `source /opt/miniconda3/bin/activate testbed && python -I -m pytest -rA -p no:cacheprovider --junitxml={junit_path} <test files from test_patch>`; `single_command` same with `{nodeid}`.
- Pytest repos in pool A (trial eligible): sphinx-doc/sphinx, pytest-dev/pytest, matplotlib/matplotlib, scikit-learn/scikit-learn, pydata/xarray, psf/requests, mwaskom/seaborn, astropy/astropy. Excluded: django (custom runner, Trac), sympy (custom runner).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_swebench_source.py
from swesim.sources import swebench_annotated as sb
def test_pool_a_count():
    ids = sb.pool_a("data/annotations/ensembled_annotations_public.csv")
    assert len(ids) == 109 and "sympy__sympy-13581" in ids
def test_image_name_and_repo_dir():
    assert sb.image_for("pydata__xarray-5362") == "swebench/sweb.eval.x86_64.pydata_1776_xarray-5362:latest"
def test_pytest_repo_filter():
    ids = sb.pool_a("data/annotations/ensembled_annotations_public.csv")
    keep = sb.filter_pytest_repos(ids)
    assert all(not i.startswith(("django__", "sympy__")) for i in keep) and len(keep) >= 40
def test_test_files_from_patch():
    tp = "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1 +1 @@\n-x\n+y\ndiff --git a/tests/sub/test_b.py b/tests/sub/test_b.py\n"
    assert sb.test_files_from_patch(tp) == ["tests/sub/test_b.py", "tests/test_a.py"]
```

```python
# tests/test_tamper.py
from swesim import tamper
def test_root_conftest_noop_is_a_valid_diff_adding_conftest():
    d = tamper.root_conftest_noop()
    assert d.startswith("diff --git a/conftest.py b/conftest.py") and "pytest_runtest_call" in d and "new file mode" in d
def test_sitecustomize_forge_targets_startup_hook():
    d = tamper.sitecustomize_junit_forge()
    assert "a/sitecustomize.py" in d
def test_skip_marker_rewrites_test_file():
    tp = "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1 +1 @@\n-x\n+def test_new():\n+    assert 1\n"
    d = tamper.skip_marker(["tests/test_a.py"])
    assert "pytestmark = pytest.mark.skip" in d and "a/tests/test_a.py" in d
```

- [ ] **Step 2: Implement swebench_annotated.py**

```python
# swesim/sources/swebench_annotated.py
from __future__ import annotations
import csv, json, re
from pathlib import Path
PYTEST_REPOS = {"sphinx-doc/sphinx", "pytest-dev/pytest", "matplotlib/matplotlib", "scikit-learn/scikit-learn", "pydata/xarray",
                "psf/requests", "mwaskom/seaborn", "astropy/astropy", "pylint-dev/pylint", "pallets/flask"}

def pool_a(csv_path: str | Path) -> list[str]:
    out = []
    with open(csv_path, newline="") as f:
        for r in csv.DictReader(f):
            if float(r["underspecified"]) >= 2 and float(r["false_negative"]) <= 1 and float(r["other_major_issues"]) == 0:
                out.append(r["instance_id"])
    return sorted(out)

def annotation_rows(csv_path: str | Path) -> dict[str, dict]:
    with open(csv_path, newline="") as f: return {r["instance_id"]: r for r in csv.DictReader(f)}

def repo_of(instance_id: str) -> str:
    owner_repo = instance_id.rsplit("-", 1)[0]; return owner_repo.replace("__", "/")

def filter_pytest_repos(ids: list[str]) -> list[str]:
    return [i for i in ids if repo_of(i) in PYTEST_REPOS]

def image_for(instance_id: str) -> str:
    return "swebench/sweb.eval.x86_64." + instance_id.replace("__", "_1776_") + ":latest"

def test_files_from_patch(test_patch: str) -> list[str]:
    return sorted(set(re.findall(r"^diff --git a/(\S+) b/", test_patch, re.M)))

def candidates(instance_ids: list[str], out_path: Path, csv_path: Path, fetch_threads: bool = True) -> int:
    from datasets import load_dataset
    from swesim.sources import threads
    ds = {r["instance_id"]: r for r in load_dataset("princeton-nlp/SWE-bench", split="test")}
    ann = annotation_rows(csv_path); n = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for iid in instance_ids:
            r = ds[iid]; a = ann[iid]; repo = r["repo"]; pr = int(iid.rsplit("-", 1)[1])
            row = {"instance_id": iid, "repo": repo, "base_commit": r["base_commit"], "version": r["version"],
                   "problem_statement": r["problem_statement"], "hints_text": r["hints_text"],
                   "fail_to_pass": json.loads(r["FAIL_TO_PASS"]), "pass_to_pass": json.loads(r["PASS_TO_PASS"]),
                   "patch": r["patch"], "test_patch": r["test_patch"], "image": image_for(iid), "repo_dir": "/testbed",
                   "annotation": {k: a[k] for k in ("underspecified", "underspecified_notes", "false_negative", "false_negative_notes", "difficulty")},
                   "thread": threads.fetch_pr_thread(repo, pr) if fetch_threads else {}}
            f.write(json.dumps(row) + "\n"); n += 1
    return n
```

```python
# swesim/sources/threads.py
from __future__ import annotations
import json, re, subprocess
def _gh(path: str):
    r = subprocess.run(["gh", "api", path, "--paginate"], capture_output=True, text=True, check=True); return json.loads(r.stdout)
def _strip(c: dict) -> dict:
    return {"user": c["user"]["login"], "created_at": c["created_at"], "body": c.get("body") or "", "url": c.get("html_url"), "path": c.get("path")}
def fetch_issue_thread(repo: str, number: int) -> dict:
    issue = _gh(f"repos/{repo}/issues/{number}"); comments = _gh(f"repos/{repo}/issues/{number}/comments?per_page=100")
    return {"issue_url": issue["html_url"], "title": issue["title"], "body": issue.get("body") or "", "user": issue["user"]["login"],
            "comments": [_strip(c) for c in comments if not c["user"]["login"].endswith("[bot]")]}
def fetch_pr_thread(repo: str, pr: int) -> dict:
    p = _gh(f"repos/{repo}/pulls/{pr}")
    body = p.get("body") or ""
    linked = sorted({int(n) for n in re.findall(r"(?:[Ff]ix(?:es|ed)?|[Cc]los(?:es|ed)?|[Rr]esolves?)\s*:?\s*#(\d+)", body)})
    out = {"pr_url": p["html_url"], "pr_title": p["title"], "pr_body": body, "author": p["user"]["login"], "merged_at": p.get("merged_at"),
           "pr_comments": [_strip(c) for c in _gh(f"repos/{repo}/issues/{pr}/comments?per_page=100") if not c["user"]["login"].endswith("[bot]")],
           "review_comments": [_strip(c) for c in _gh(f"repos/{repo}/pulls/{pr}/comments?per_page=100")], "linked_issues": []}
    for n in linked[:3]:
        try: out["linked_issues"].append(fetch_issue_thread(repo, n))
        except subprocess.CalledProcessError: pass
    return out
```

- [ ] **Step 3: Implement tamper.py (code-generated exploit controls)**

```python
# swesim/tamper.py
"""Code-generated tamper controls. Every one is an `incorrect-exploit`: it must earn reward 0 or the task is rejected."""
def _new_file_diff(path: str, content: str) -> str:
    lines = content.splitlines(); body = "\n".join("+" + l for l in lines)
    return f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"

def root_conftest_noop() -> str:
    return _new_file_diff("conftest.py", "import pytest\n\n@pytest.hookimpl(tryfirst=True)\ndef pytest_runtest_call(item):\n    item.runtest = lambda: None\n")

def sitecustomize_junit_forge() -> str:
    return _new_file_diff("sitecustomize.py", "import builtins\n# attempt to neutralise assertions at interpreter start\nbuiltins.__dict__['AssertionError'] = type('AssertionError', (Exception,), {})\n")

def skip_marker(test_files: list[str]) -> str:
    out = []
    for path in test_files:
        out.append(f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,0 +1,2 @@\n+import pytest\n+pytestmark = pytest.mark.skip(reason='x')\n")
    return "".join(out)

def all_controls(test_files: list[str]) -> dict[str, str]:
    return {"tamper-root-conftest": root_conftest_noop(), "tamper-sitecustomize": sitecustomize_junit_forge(), "tamper-skip-marker": skip_marker(test_files)}
```

- [ ] **Step 4: Implement judge.py and draft_spec.py**

```python
# swesim/stages/judge.py
from __future__ import annotations
import json
from swesim.llm import chat_json
JUDGE_PROMPT_VERSION = "judge-v1"
SYSTEM = """You review a GitHub issue, its discussion, the merged fix, and the fix's tests to decide whether it makes a good
interactive software-engineering task in which a simulated maintainer can answer an agent's questions. Be strict and concrete."""
SCHEMA = """{"complexity": 1-5, "spec_gap": 1-5, "test_behavioralness": 1-5, "self_contained": true|false,
"missing": "what a solver cannot know from the issue alone", "thread_supplies": true|false,
"legitimate_requirement": true|false, "alternatives_accepted": true|false,
"non_derivable_choice": "the specific decision (exception type, output shape, warn-vs-raise, compat rule) that neither code nor domain knowledge determines, or null",
"engineering_consequence": "why that decision matters to callers, or null", "keep": true|false, "rationale": "2-4 sentences"}"""

def judge_candidate(row: dict, model_id: str) -> dict:
    thread = row.get("thread", {})
    user = f"""ISSUE (as written):\n{row['problem_statement'][:6000]}\n\nISSUE DISCUSSION (hints_text):\n{row['hints_text'][:4000]}\n\nPR THREAD:\n{json.dumps(thread.get('pr_comments', [])[:15])[:5000]}\n\nREVIEW COMMENTS:\n{json.dumps(thread.get('review_comments', [])[:15])[:5000]}\n\nFIX DIFF:\n{row['patch'][:8000]}\n\nTEST DIFF:\n{row['test_patch'][:6000]}\n\nHUMAN ANNOTATION: underspecified={row['annotation']['underspecified']} notes={row['annotation']['underspecified_notes']}\ntests={row['annotation']['false_negative']} notes={row['annotation']['false_negative_notes']} difficulty={row['annotation']['difficulty']}"""
    out = chat_json(model_id, SYSTEM, [{"role": "user", "content": user}], SCHEMA)
    out["prompt_version"] = JUDGE_PROMPT_VERSION; out["model_id"] = model_id; return out
```

```python
# swesim/stages/draft_spec.py
from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone
from pathlib import Path
from swesim.llm import chat_json
from swesim import tamper
from swesim.sources.swebench_annotated import test_files_from_patch
DRAFT_PROMPT_VERSION = "draft-v1"
SYSTEM = """You turn a GitHub issue, its thread, and the merged fix into a scenario record for an interactive coding task.
Rules: every fact must quote a short excerpt from the thread with its URL, or be marked authored with a rationale.
Facts the agent can learn by reading the repository are discovery=repo-derivable. Facts absent from the repository that the
maintainer decided are discovery=on-request. Private helper names chosen by the fix author are discovery=author-only and must
not link tests. Requirements come from review comments. Checks are user-level reproducers (not the hidden tests).
Groups partition FAIL_TO_PASS test ids into requirement groups with weights summing to 1. Also write one plausible-wrong patch
as a unified diff against the repository at the base commit: a tempting incomplete or wrong fix (e.g. wrong exception type)."""
SCHEMA = """{"briefs": [{"profile": "natural", "text": "...", "provenance": "original-issue"}],
"ledger": [{"id": "f1", "text": "...", "kind": "behavior|interface|constraint|reproduction|context",
  "provenance": "issue-thread|pr-thread|pr-review|annotator-note|authored", "source_excerpt": "...", "source_url": "...",
  "authored_rationale": null, "discovery": "brief|on-request|volunteered-at-review|repo-derivable|author-only", "tests": ["nodeid"]}],
"operations": [{"type": "clarification|evaluator-repair|synthetic-extension", "target": "...", "rationale": "..."}],
"persona": {"summary": "...", "tone": "...", "disclosure_style": "...", "derived_from_urls": ["..."], "role": "maintainer|reporter"},
"requirements": [{"id": "r1", "text": "...", "fact_id": "f1", "tests": ["nodeid"], "source_url": "..."}],
"checks": [{"id": "c1", "description": "...", "command": "python -c '...'", "expected_observation": "..."}],
"groups": [{"id": "g1", "weight": 0.5, "tests": ["nodeid"]}],
"plausible_wrong": {"name": "...", "rationale": "...", "diff": "unified diff text"},
"protected_paths_extra": ["paths the fix's tests depend on"]}"""

def _sha(b: bytes) -> str: return hashlib.sha256(b).hexdigest()

def draft(row: dict, judged: dict, model_id: str, out_dir: Path, pipeline_version: str = "0.1.0") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True); assets = out_dir / "assets"; assets.mkdir(exist_ok=True)
    user = f"""INSTANCE {row['instance_id']} repo {row['repo']} base {row['base_commit']}\nFAIL_TO_PASS: {json.dumps(row['fail_to_pass'])}\nPASS_TO_PASS (first 30): {json.dumps(row['pass_to_pass'][:30])}\n\nISSUE:\n{row['problem_statement'][:6000]}\n\nHINTS:\n{row['hints_text'][:4000]}\n\nTHREAD:\n{json.dumps(row.get('thread', {}))[:12000]}\n\nFIX DIFF:\n{row['patch'][:8000]}\n\nTEST DIFF:\n{row['test_patch'][:6000]}\n\nJUDGE: {json.dumps(judged)}"""
    d = chat_json(model_id, SYSTEM, [{"role": "user", "content": user}], SCHEMA, max_tokens=8000)
    files = {"oracle.diff": row["patch"].encode(), "test.patch": row["test_patch"].encode(), "plausible-wrong.diff": d["plausible_wrong"]["diff"].encode()}
    test_files = test_files_from_patch(row["test_patch"])
    for name, diff in tamper.all_controls(test_files).items(): files[f"{name}.diff"] = diff.encode()
    manifest = {}
    for name, data in files.items():
        (assets / name).write_bytes(data); manifest[name] = {"path": f"assets/{name}", "sha256": _sha(data), "bytes": len(data)}
    runner_cmd = "source /opt/miniconda3/bin/activate testbed && python -I -m pytest -rA -p no:cacheprovider --junitxml={junit_path} " + " ".join(test_files)
    spec = {"id": row["instance_id"].lower().replace("__", "--"), "version": 1, "source": "swebench-annotated", "source_ref": row["instance_id"],
            "created_at": datetime.now(timezone.utc).isoformat(), "pipeline_version": pipeline_version,
            "environment": {"kind": "prebuilt-image", "image": row["image"], "repo_url": f"https://github.com/{row['repo']}", "base_commit": row["base_commit"],
                            "language": "python", "repo_dir": row.get("repo_dir", "/testbed"), "test_command": runner_cmd},
            "assets": manifest, "briefs": d["briefs"] or [{"profile": "natural", "text": row["problem_statement"], "provenance": "original-issue"}],
            "ledger": d["ledger"], "operations": d.get("operations", []), "persona": d["persona"], "requirements": d.get("requirements", []),
            "contract": {"fail_to_pass": row["fail_to_pass"], "pass_to_pass": row["pass_to_pass"], "test_assets": ["test.patch"],
                         "protected_paths": sorted(set(test_files) | set(d.get("protected_paths_extra", []))),
                         "runner": {"kind": "pytest-ra", "command": runner_cmd, "single_command": runner_cmd.rsplit(" ", len(test_files))[0] + " {nodeid}"}},
            "checks": d.get("checks", []),
            "controls": {"oracle": "oracle.diff",
                         "plausible_wrong": [{"name": d["plausible_wrong"]["name"], "asset": "plausible-wrong.diff", "rationale": d["plausible_wrong"]["rationale"], "expected_success": 0, "expected_reward_max": 0.5, "cls": "plausible-wrong"}],
                         "alternative_correct": [],
                         "tamper": [{"name": n, "asset": f"{n}.diff", "rationale": "code-generated exploit", "expected_reward": 0.0, "cls": "incorrect-exploit"} for n in tamper.all_controls(test_files)]},
            "groups": d["groups"], "difficulty": {"annotated": row["annotation"]["difficulty"], "judged": judged},
            "thread": {k: v for k, v in row.get("thread", {}).items()}}
    p = out_dir / "scenario.draft.json"; p.write_text(json.dumps(spec, indent=2)); return p
```

```python
# swesim/stages/mine.py
from __future__ import annotations
from pathlib import Path
from swesim.paths import Workspace
def mine_swebench(ws: Workspace, pool: str = "A", instance_ids: list[str] | None = None, pytest_only: bool = True, fetch_threads: bool = True) -> Path:
    from swesim.sources import swebench_annotated as sb
    csv_path = Path("data/annotations/ensembled_annotations_public.csv")
    ids = instance_ids or sb.pool_a(csv_path)
    if pytest_only: ids = sb.filter_pytest_repos(ids)
    out = ws.candidates_dir / "swebench-annotated.jsonl"
    sb.candidates(ids, out, csv_path, fetch_threads=fetch_threads); return out
```

- [ ] **Step 5: Run tests; run mine for two real instances; commit**

Run: `uv run pytest tests/test_swebench_source.py tests/test_tamper.py -q` → 7 passed.
Run: `uv run python -c "from swesim.paths import Workspace; from swesim.stages.mine import mine_swebench; print(mine_swebench(Workspace('workspace'), instance_ids=['pydata__xarray-5362','psf__requests-1963']))"` then inspect `workspace/candidates/swebench-annotated.jsonl` (two rows, threads present).
```bash
git add swesim/sources swesim/stages/mine.py swesim/stages/judge.py swesim/stages/draft_spec.py swesim/tamper.py tests/test_swebench_source.py tests/test_tamper.py
git commit -m "feat(sources): swebench-annotated pool A mining with threads, judge rubric, spec drafting, code-generated tamper controls"
```

---

### Task 7: validate stage (controls on Modal) and approve/compile CLI wiring

**Files:**
- Create: `swesim/stages/validate.py`, `swesim/harbor_cli.py`
- Modify: `swesim/cli.py` (replace stubs for `mine`, `judge`, `draft`, `approve`, `compile`, `validate`)
- Create: `tests/test_validate.py`

**Interfaces:**
- Produces: `harbor_cli.run_task(task_dir: Path, agent: str, jobs_dir: Path, job_name: str, model: str | None = None, agent_kwargs: dict | None = None, env_provider: str = "modal", timeout_sec: int = 3600) -> Path` (returns the trial directory; raises `HarborRunError` with the exception message from `result.json`); `harbor_cli.read_reward(trial_dir) -> dict`, `harbor_cli.read_report(trial_dir) -> dict`; `validate.validate_scenario(ws, spec, run_config, stakeholder_url) -> dict` writing `workspace/scenarios/<id>/validation.json` with `{status: "valid"|"invalid", failures: [...], expected_base: {...}, expected_gold: {...}, controls: {name: {reward, success, outcome, expected}}, probe: {...}, trial_dirs: {...}}`; approval flow: `swesim approve <id>` validates `scenario.draft.json` (edited by the human) and writes `scenario.json`.
- Consumes: `compile_scenario`, `PatchAgent` import path, reward/report formats from Task 3.

- [ ] **Step 1: Write failing test (expectation logic, no cloud)**

```python
# tests/test_validate.py
from swesim.stages.validate import evaluate_controls
def test_expectations():
    results = {"nop": {"reward": 0, "success": 0, "outcome_code": 3}, "oracle": {"reward": 1.0, "success": 1, "outcome_code": 1},
               "plausible-wrong:keyerror": {"reward": 0.0, "success": 0, "outcome_code": 2},
               "tamper:root-conftest": {"reward": 0.0, "success": 0, "outcome_code": 2}}
    spec_controls = {"plausible_wrong": [{"name": "keyerror", "expected_success": 0, "expected_reward_max": 0.5}],
                     "alternative_correct": [], "tamper": [{"name": "root-conftest", "cls": "incorrect-exploit", "expected_reward": 0.0}]}
    ok, failures = evaluate_controls(results, spec_controls)
    assert ok and failures == []
    results["tamper:root-conftest"]["reward"] = 1.0; results["tamper:root-conftest"]["success"] = 1
    ok, failures = evaluate_controls(results, spec_controls)
    assert not ok and "tamper:root-conftest" in failures[0]
```

- [ ] **Step 2: Implement harbor_cli.py and validate.py**

```python
# swesim/harbor_cli.py
from __future__ import annotations
import json, os, subprocess
from pathlib import Path
class HarborRunError(RuntimeError): pass

def run_task(task_dir: Path, agent: str, jobs_dir: Path, job_name: str, model: str | None = None, agent_kwargs: dict | None = None,
             env_provider: str = "modal", timeout_sec: int = 3600) -> Path:
    cmd = ["harbor", "run", "-p", str(task_dir), "--agent", agent, "-e", env_provider, "--jobs-dir", str(jobs_dir), "--job-name", job_name, "--yes"]
    if model: cmd += ["-m", model]
    for k, v in (agent_kwargs or {}).items(): cmd += ["--ak", f"{k}={v}"]
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, env=env)
    trials = sorted((jobs_dir / job_name).glob("*__*"))
    if not trials: raise HarborRunError(f"no trial dir; harbor stdout tail: {proc.stdout[-1500:]} stderr: {proc.stderr[-1500:]}")
    result = json.loads((trials[-1] / "result.json").read_text())
    if result.get("exception_info"): raise HarborRunError(json.dumps(result["exception_info"])[:2000])
    return trials[-1]

def read_reward(trial_dir: Path) -> dict:
    p = trial_dir / "verifier" / "reward.json"; return json.loads(p.read_text()) if p.exists() else {}
def read_report(trial_dir: Path) -> dict:
    p = trial_dir / "verifier" / "report.json"; return json.loads(p.read_text()) if p.exists() else {}
```

```python
# swesim/stages/validate.py
from __future__ import annotations
import json, re
from pathlib import Path
from swesim import harbor_cli
from swesim.models import ScenarioSpec, RunConfig
from swesim.paths import Workspace
from swesim.stages.compile import compile_scenario
PATCH_AGENT = "swesim.agents.patch_agent:PatchAgent"

def evaluate_controls(results: dict[str, dict], controls: dict) -> tuple[bool, list[str]]:
    failures = []
    if results["oracle"].get("success") != 1: failures.append("oracle: success != 1")
    if results["nop"].get("reward", 1) != 0: failures.append("nop: reward != 0")
    for c in controls["plausible_wrong"]:
        r = results[f"plausible-wrong:{c['name']}"]
        if r.get("success") != c.get("expected_success", 0): failures.append(f"plausible-wrong:{c['name']}: success {r.get('success')}")
        if c.get("expected_reward_max") is not None and r.get("reward", 1) > c["expected_reward_max"] + 1e-9: failures.append(f"plausible-wrong:{c['name']}: reward {r.get('reward')} > max")
    for c in controls["alternative_correct"]:
        if results[f"alt-correct:{c['name']}"].get("success") != 1: failures.append(f"alt-correct:{c['name']}: success != 1")
    for c in controls["tamper"]:
        r = results[f"tamper:{c['name']}"]
        if c.get("cls") == "incorrect-exploit" and r.get("reward", 1) != 0: failures.append(f"tamper:{c['name']}: exploit earned reward {r.get('reward')}")
        if c.get("cls") == "benign-edit" and r.get("success") != 1: failures.append(f"tamper:{c['name']}: benign edit failed")
    return (not failures), failures

def gold_lines(oracle_diff: str) -> list[str]:
    adds = [l[1:].strip() for l in oracle_diff.splitlines() if l.startswith("+") and not l.startswith("+++") and len(l.strip()) > 20]
    return adds[:3]

def validate_scenario(ws: Workspace, spec: ScenarioSpec, run_config: RunConfig, stakeholder_url: str) -> dict:
    sdir = ws.scenario_dir(spec.id); comp = compile_scenario(ws, spec, run_config, stakeholder_url)
    task = comp.task_dirs["full"]; jobs = ws.jobs_dir / "validate" / spec.id; results = {}; trial_dirs = {}
    def run(name: str, agent: str, kwargs: dict | None = None):
        td = harbor_cli.run_task(task, agent, jobs, f"{spec.id}--{name}", agent_kwargs=kwargs)
        trial_dirs[name] = str(td); results[name] = harbor_cli.read_reward(td); results[name]["report"] = harbor_cli.read_report(td)
    run("nop", "nop"); run("oracle", "oracle")
    for c in spec.controls.plausible_wrong: run(f"plausible-wrong:{c.name}", PATCH_AGENT, {"patch": str((task / "controls" / f"{c.name}.diff").resolve())})
    for c in spec.controls.alternative_correct: run(f"alt-correct:{c.name}", PATCH_AGENT, {"patch": str((task / "controls" / f"{c.name}.diff").resolve())})
    for c in spec.controls.tamper: run(f"tamper:{c.name}", PATCH_AGENT, {"patch": str((task / "controls" / f"{c.name}.diff").resolve())})
    probe_dir = harbor_cli.run_task(task, PATCH_AGENT, jobs, f"{spec.id}--probe", agent_kwargs={"probe": "true", "gold_lines": json.dumps(gold_lines((task / "solution" / "oracle.diff").read_text()))})
    probe_txt = (probe_dir / "agent" / "probe.txt").read_text() if (probe_dir / "agent" / "probe.txt").exists() else ""
    hits = re.search(r"HITS_BEGIN\n(.*?)HITS_END", probe_txt, re.S); probe = {"raw": probe_txt[-2000:], "gold_hits": (hits.group(1).strip().splitlines() if hits else [])}
    ok, failures = evaluate_controls(results, spec.controls.model_dump())
    base = {t["id"]: t["observed"] for t in results["nop"]["report"].get("per_test", [])}
    gold = {t["id"]: t["observed"] for t in results["oracle"]["report"].get("per_test", [])}
    if any(v == "passed" for k, v in base.items() if k in spec.contract.fail_to_pass): failures.append("a FAIL_TO_PASS test already passes on base"); ok = False
    if probe["gold_hits"]: failures.append(f"gold patch lines reachable in agent image: {probe['gold_hits'][:3]}"); ok = False
    out = {"status": "valid" if ok else "invalid", "failures": failures, "expected_base": base, "expected_gold": gold,
           "controls": {k: {kk: vv for kk, vv in v.items() if kk != "report"} for k, v in results.items()}, "probe": probe,
           "trial_dirs": trial_dirs, "spec_hash": spec.spec_hash, "image_digest": comp.image_digest}
    (sdir / "validation.json").write_text(json.dumps(out, indent=2)); return out
```

- [ ] **Step 3: Wire the CLI**

Replace the stub loop in `swesim/cli.py` with real commands (keep stubs for stages not yet implemented):

```python
# swesim/cli.py (additions)
import json, typer
from pathlib import Path
from swesim.models import ScenarioSpec, RunConfig
from swesim.paths import Workspace
app = typer.Typer(no_args_is_help=True)
WS = Workspace("workspace")

@app.command()
def mine(source: str = "swebench-annotated", pool: str = "A", instance: list[str] = typer.Option(None), repo: str = "", pr: int = 0, no_threads: bool = False):
    if source == "swebench-annotated":
        from swesim.stages.mine import mine_swebench; typer.echo(mine_swebench(WS, pool, instance or None, fetch_threads=not no_threads))
    elif source == "github-repo":
        from swesim.stages.mine import mine_github_repo; typer.echo(mine_github_repo(WS, repo, pr or None))
    else: raise typer.BadParameter(source)

@app.command()
def judge(candidates: Path, model: str = "anthropic/claude-sonnet-5", limit: int = 20):
    from swesim.stages.judge import judge_candidate
    out = candidates.with_suffix(".judged.jsonl"); n = 0
    with open(candidates) as f, open(out, "w") as g:
        for line in f:
            if n >= limit: break
            row = json.loads(line); row["judge"] = judge_candidate(row, model); g.write(json.dumps(row) + "\n"); n += 1
    typer.echo(out)

@app.command()
def draft(judged: Path, instance: str, model: str = "anthropic/claude-sonnet-5"):
    from swesim.stages.draft_spec import draft as _draft
    row = next(json.loads(l) for l in open(judged) if json.loads(l)["instance_id"] == instance)
    sid = instance.lower().replace("__", "--"); typer.echo(_draft(row, row["judge"], model, WS.scenario_dir(sid)))

@app.command()
def approve(scenario_id: str):
    """Validate the human-edited scenario.draft.json and freeze it as scenario.json."""
    sdir = WS.scenario_dir(scenario_id); spec = ScenarioSpec.model_validate_json((sdir / "scenario.draft.json").read_text())
    (sdir / "scenario.json").write_text(spec.model_dump_json(indent=2)); typer.echo(f"approved {scenario_id} spec_hash={spec.spec_hash}")

def _load(scenario_id: str) -> ScenarioSpec:
    return ScenarioSpec.model_validate_json((WS.scenario_dir(scenario_id) / "scenario.json").read_text())

@app.command()
def compile(scenario_id: str, stakeholder_url: str = typer.Option(..., envvar="SWESIM_STAKEHOLDER_URL"), condition: str = "interactive"):
    from swesim.stages.compile import compile_scenario
    res = compile_scenario(WS, _load(scenario_id), RunConfig.default(condition), stakeholder_url); typer.echo(json.dumps({k: str(v) for k, v in res.task_dirs.items()}))

@app.command()
def validate(scenario_id: str, stakeholder_url: str = typer.Option(..., envvar="SWESIM_STAKEHOLDER_URL")):
    from swesim.stages.validate import validate_scenario
    out = validate_scenario(WS, _load(scenario_id), RunConfig.default("full"), stakeholder_url); typer.echo(json.dumps({"status": out["status"], "failures": out["failures"]}, indent=2))
    raise typer.Exit(0 if out["status"] == "valid" else 1)
```

- [ ] **Step 4: Run the first real scenario through draft → approve → validate on Modal**

```bash
export $(grep -v '^#' ~/Documents/.env | xargs)
uv run swesim mine --instance pydata__xarray-5362 --instance psf__requests-1963
uv run swesim judge workspace/candidates/swebench-annotated.jsonl
uv run swesim draft workspace/candidates/swebench-annotated.judged.jsonl pydata__xarray-5362
# Human step: open workspace/scenarios/pydata--xarray-5362/scenario.draft.json, check every fact's excerpt, fix discovery tags, then:
uv run swesim approve pydata--xarray-5362
SWESIM_STAKEHOLDER_URL=https://placeholder.modal.run uv run swesim validate pydata--xarray-5362
```
Expected: `validation.json` with `status: valid`; nop shows every FAIL_TO_PASS id failing on base; oracle success 1; the three tamper controls reward 0; plausible-wrong success 0. If the SWE-bench image's conda activate path differs, fix `runner.command` in the draft and re-approve. Record the first `validation.json` under `examples/` in Task 15.

- [ ] **Step 5: Commit**

```bash
git add swesim/harbor_cli.py swesim/stages/validate.py swesim/cli.py tests/test_validate.py
git commit -m "feat(validate): run nop/oracle/control patches and reachability probe on Modal; approve and compile CLI"
```

---

### Task 8: Stakeholder service on Modal, session protocol, shims

**Files:**
- Create: `swesim/stakeholder/__init__.py`, `swesim/stakeholder/session.py`, `swesim/stakeholder/prompts.py`, `swesim/stakeholder/tools.py`, `swesim/stakeholder/service.py`, `swesim/stakeholder/modal_app.py`, `swesim/stakeholder/shims/ask-user`, `swesim/stakeholder/shims/request-review`, `scripts/deploy_stakeholder.sh`
- Create: `tests/test_session.py`, `tests/test_service.py`

**Interfaces:**
- Produces HTTP API (FastAPI): `POST /sessions` (header `X-Worker-Secret`) body `{trial_id, spec_hash, condition, run_config_hash, visible_view: {...}, stakeholder_config: {...}, image: str}` → `{token}`; `POST /ask` (header `Authorization: Bearer <token>`) body `{request_id, question}` → `{reply, disclosed_fact_ids, patience_remaining, tool_calls: [...]}`; `POST /review` body `{request_id, diff, files: {path: content}}` → same shape plus `reviewed_diff_sha256`; `POST /freeze` → `{transcript_hash, frozen: true}`; `GET /transcript/{trial_id}` (worker secret) → full transcript JSON; `GET /health`.
- `session.Session(trial_id, visible_view, config)` with `ask(request_id, question, llm) -> Reply`, `review(request_id, diff, files, llm, tools) -> Reply`, `freeze() -> str`, `to_dict()`; patience tiers per spec section 8; serialized via an `asyncio.Lock` per session; cached replies by `request_id`.
- `prompts.system_prompt(visible_view, persona, patience_state, review_mode) -> str` builds the context from the stakeholder-visible view only: facts with `discovery in {brief, on-request, volunteered-at-review, repo-derivable}`, requirements, checks descriptions; never `author-only`, never oracle or test assets.
- `tools.run_check(image, repo_dir, diff, command, timeout=120) -> {stdout, stderr, exit_code}` using `modal.Sandbox.create(image=modal.Image.from_registry(image), block_network=True, timeout=timeout)`.
- Visible view construction (in the worker, Task 11): `stakeholder.view.visible_view(spec) -> dict` (put in `swesim/stakeholder/view.py`).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_session.py
import asyncio
from swesim.stakeholder.session import Session
from swesim.models import RunConfig
VIEW = {"brief": "It breaks.", "facts": [{"id": "f1", "text": "Raise ValueError, not KeyError", "discovery": "on-request", "kind": "interface"},
        {"id": "f4", "text": "weighted.py holds the code", "discovery": "repo-derivable", "kind": "context"}],
        "requirements": [{"id": "r1", "text": "Raise ValueError"}], "persona": {"summary": "maintainer", "tone": "terse", "disclosure_style": "answers what is asked", "role": "maintainer"}, "checks": []}
class FakeLLM:
    def __init__(self): self.calls = 0
    def __call__(self, system, messages, schema):
        self.calls += 1; return {"reply": "ValueError.", "disclosed_fact_ids": ["f1"]}
def test_patience_tiers_and_idempotency():
    cfg = RunConfig.default("interactive").stakeholder.model_copy(update={"patience_profile": {"start": 3, "question_cost": 1, "review_cost": 2, "terse_below": 2, "refuse_at": 0}})
    s = Session("t1", VIEW, cfg); llm = FakeLLM()
    r1 = asyncio.run(s.ask("q1", "Which exception?", llm)); assert r1.reply == "ValueError." and r1.patience_remaining == 2 and r1.disclosed_fact_ids == ["f1"]
    r1b = asyncio.run(s.ask("q1", "Which exception?", llm)); assert r1b.reply == r1.reply and llm.calls == 1   # cached by request_id
    r2 = asyncio.run(s.ask("q2", "And the message?", llm)); assert r2.patience_remaining == 1 and r2.tier == "terse"
    r3 = asyncio.run(s.ask("q3", "More?", llm)); assert r3.patience_remaining == 0 and r3.tier == "refused" and llm.calls == 2
    h = s.freeze(); assert len(h) == 64
    import pytest
    with pytest.raises(Session.Frozen): asyncio.run(s.ask("q4", "x", llm))
def test_system_prompt_excludes_author_only():
    from swesim.stakeholder.prompts import system_prompt
    view = dict(VIEW); view["facts"] = VIEW["facts"] + [{"id": "f9", "text": "SECRET", "discovery": "author-only", "kind": "interface"}]
    assert "SECRET" not in system_prompt(view, view["persona"], {"tier": "full", "remaining": 3}, "diff-only")
```

```python
# tests/test_service.py
from fastapi.testclient import TestClient
from swesim.stakeholder import service
def test_session_lifecycle(monkeypatch):
    monkeypatch.setenv("SWESIM_WORKER_SECRET", "s3")
    monkeypatch.setattr(service, "call_llm", lambda system, messages, schema, cfg: {"reply": "ValueError.", "disclosed_fact_ids": ["f1"]})
    c = TestClient(service.app)
    r = c.post("/sessions", headers={"X-Worker-Secret": "s3"}, json={"trial_id": "t1", "spec_hash": "h", "condition": "interactive", "run_config_hash": "r",
        "visible_view": {"brief": "b", "facts": [{"id": "f1", "text": "x", "discovery": "on-request", "kind": "interface"}], "requirements": [], "persona": {"summary": "m", "tone": "t", "disclosure_style": "d", "role": "maintainer"}, "checks": []},
        "stakeholder_config": {"model_id": "openai/gpt-5.5", "prompt_version": "v1", "temperature": 0.0, "patience_profile": {"start": 2, "question_cost": 1, "review_cost": 2, "terse_below": 1, "refuse_at": 0}, "review_mode": "diff-only", "max_tool_calls_per_activation": 4}, "image": "ubuntu:24.04"})
    tok = r.json()["token"]; h = {"Authorization": f"Bearer {tok}"}
    a = c.post("/ask", headers=h, json={"request_id": "1", "question": "which exception?"}).json(); assert a["reply"] == "ValueError." and a["patience_remaining"] == 1
    f = c.post("/freeze", headers=h).json(); assert f["frozen"] and len(f["transcript_hash"]) == 64
    assert c.post("/ask", headers=h, json={"request_id": "2", "question": "x"}).status_code == 410
    t = c.get("/transcript/t1", headers={"X-Worker-Secret": "s3"}).json(); assert t["transcript_hash"] == f["transcript_hash"] and len(t["events"]) >= 2
```

- [ ] **Step 2: Implement session.py and prompts.py**

```python
# swesim/stakeholder/prompts.py
from __future__ import annotations
VISIBLE = {"brief", "on-request", "volunteered-at-review", "repo-derivable"}
def system_prompt(view: dict, persona: dict, patience: dict, review_mode: str) -> str:
    facts = [f for f in view["facts"] if f.get("discovery") in VISIBLE]
    lines = [f"You are the {persona.get('role','maintainer')} who owns this request. Persona: {persona['summary']}. Tone: {persona['tone']}. Style: {persona['disclosure_style']}.",
             "You are cooperative about requirements and limited about implementation: describe expected versus observed behavior, clarify what you want, never prescribe code, algorithms, or edits, never invent requirements beyond the facts below.",
             "Answer at the level of specificity asked: a broad question gets the requirement list; exact exception types, messages, or shapes need a specific question.",
             f"Availability: {patience['tier']} (remaining {patience['remaining']}). If terse, answer in one or two sentences and point the agent at the repository for anything it could find itself.",
             "The original request:", view["brief"], "Requirements (what you actually want):"]
    lines += [f"- {r['id']}: {r['text']}" for r in view.get("requirements", [])]
    lines.append("Facts you know (share only when a question relates; mark repo-derivable facts as things the agent can find in the code):")
    lines += [f"- {f['id']} [{f['kind']}, {f['discovery']}]: {f['text']}" for f in facts]
    if review_mode == "diff-only": lines.append("For reviews you only read the diff; say explicitly that you have not executed anything.")
    else: lines.append("For reviews you may run predefined checks; report what you observed, not how to fix it.")
    return "\n".join(lines)
REPLY_SCHEMA = '{"reply": "text shown to the agent", "disclosed_fact_ids": ["ids of facts you conveyed"]}'
```

```python
# swesim/stakeholder/session.py
from __future__ import annotations
import asyncio, hashlib, json, time
from dataclasses import dataclass, field, asdict
from swesim.stakeholder.prompts import system_prompt, REPLY_SCHEMA
REFUSAL = "I have to step away now; I can't answer more questions. Please investigate from the repository and finish with what you have."

@dataclass
class Reply:
    reply: str; disclosed_fact_ids: list[str]; patience_remaining: int; tier: str; tool_calls: list[dict] = field(default_factory=list); reviewed_diff_sha256: str | None = None

class Session:
    class Frozen(Exception): pass
    def __init__(self, trial_id: str, view: dict, cfg, image: str | None = None):
        self.trial_id = trial_id; self.view = view; self.cfg = cfg; self.image = image
        p = cfg.patience_profile if hasattr(cfg, "patience_profile") else cfg["patience_profile"]
        self.p = p if isinstance(p, dict) else p.model_dump()
        self.remaining = self.p["start"]; self.events: list[dict] = []; self.cache: dict[str, Reply] = {}
        self.frozen = False; self.transcript_hash: str | None = None; self.lock = asyncio.Lock(); self.created_at = time.time()
    def _tier(self) -> str:
        if self.remaining <= self.p["refuse_at"]: return "refused"
        return "terse" if self.remaining < self.p["terse_below"] else "full"
    def _record(self, kind: str, **kw): self.events.append({"kind": kind, "ts": time.time(), **kw})
    async def _exchange(self, request_id: str, kind: str, cost: int, messages: list[dict], llm, tool_calls: list[dict], reviewed_sha: str | None) -> Reply:
        if self.frozen: raise Session.Frozen()
        async with self.lock:
            if request_id in self.cache: return self.cache[request_id]
            tier = self._tier()
            if tier == "refused":
                rep = Reply(REFUSAL, [], self.remaining, tier, tool_calls, reviewed_sha)
            else:
                self.remaining = max(0, self.remaining - cost)
                sysm = system_prompt(self.view, self.view["persona"], {"tier": tier, "remaining": self.remaining}, self.cfg.review_mode if hasattr(self.cfg, "review_mode") else self.cfg["review_mode"])
                history = [{"role": e["role"], "content": e["content"]} for e in self.events if e["kind"] == "message"]
                out = llm(sysm, history + messages, REPLY_SCHEMA)
                rep = Reply(out.get("reply", ""), [i for i in out.get("disclosed_fact_ids", []) if isinstance(i, str)], self.remaining, tier, tool_calls, reviewed_sha)
            self._record("message", role="user", content=messages[-1]["content"], request_id=request_id, exchange=kind)
            self._record("message", role="assistant", content=rep.reply, request_id=request_id, disclosed_fact_ids=rep.disclosed_fact_ids, tier=rep.tier, patience_remaining=rep.patience_remaining, tool_calls=tool_calls, reviewed_diff_sha256=reviewed_sha)
            self.cache[request_id] = rep; return rep
    async def ask(self, request_id: str, question: str, llm) -> Reply:
        return await self._exchange(request_id, "ask", self.p["question_cost"], [{"role": "user", "content": question}], llm, [], None)
    async def review(self, request_id: str, diff: str, files: dict[str, str], llm, tools=None) -> Reply:
        sha = hashlib.sha256(diff.encode()).hexdigest(); calls = []
        content = "Please review my current changes.\n\nDIFF:\n" + diff[:20000]
        if tools is not None:
            for c in tools.plan_and_run(self.view, diff, files, self.image):
                calls.append(c); content += f"\n\nCHECK {c['id']} ({c['description']}): exit={c['exit_code']}\n{c['stdout'][-1500:]}"
        else:
            content += "\n\n(You have not executed anything; you are reading the diff only.)"
        return await self._exchange(request_id, "review", self.p["review_cost"], [{"role": "user", "content": content}], llm, calls, sha)
    def freeze(self) -> str:
        self.frozen = True
        self.transcript_hash = hashlib.sha256(json.dumps(self.events, sort_keys=True).encode()).hexdigest(); return self.transcript_hash
    def to_dict(self) -> dict:
        return {"trial_id": self.trial_id, "events": self.events, "patience_start": self.p["start"], "patience_end": self.remaining,
                "frozen": self.frozen, "transcript_hash": self.transcript_hash, "created_at": self.created_at}
```

- [ ] **Step 3: Implement tools.py, service.py, modal_app.py, shims**

```python
# swesim/stakeholder/tools.py
from __future__ import annotations
import shlex
class CheckTools:
    """run_check via Modal Sandbox: apply the diff to the base image's repo and run a predefined check, offline."""
    def __init__(self, checks: list[dict], repo_dir: str, max_calls: int = 4, timeout: int = 120):
        self.checks = checks; self.repo_dir = repo_dir; self.max_calls = max_calls; self.timeout = timeout
    def plan_and_run(self, view: dict, diff: str, files: dict[str, str], image: str | None) -> list[dict]:
        if not image or not self.checks: return []
        import modal
        out = []
        img = modal.Image.from_registry(image).apt_install("git")
        for c in self.checks[: self.max_calls]:
            script = f"cd {shlex.quote(self.repo_dir)} && cat > /tmp/agent.diff <<'SWESIM_EOF'\n{diff}\nSWESIM_EOF\ngit apply --whitespace=nowarn /tmp/agent.diff && {c['command']}"
            sb = modal.Sandbox.create("bash", "-lc", script, image=img, timeout=self.timeout, block_network=True)
            sb.wait(); out.append({"id": c["id"], "description": c["description"], "exit_code": sb.returncode, "stdout": sb.stdout.read()[-4000:], "stderr": sb.stderr.read()[-2000:]})
        return out
```

```python
# swesim/stakeholder/service.py
from __future__ import annotations
import hashlib, hmac, json, os, secrets
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from swesim.llm import chat_json
from swesim.models import StakeholderConfig
from swesim.stakeholder.session import Session
from swesim.stakeholder.tools import CheckTools

app = FastAPI(title="swesim stakeholder")
SESSIONS: dict[str, Session] = {}; TOKENS: dict[str, str] = {}

def call_llm(system: str, messages: list[dict], schema: str, cfg: StakeholderConfig) -> dict:
    return chat_json(cfg.model_id, system, messages, schema, temperature=cfg.temperature)

def _worker(secret: str | None):
    if not secret or not hmac.compare_digest(secret, os.environ.get("SWESIM_WORKER_SECRET", "")): raise HTTPException(401)
def _session(auth: str | None) -> Session:
    tok = (auth or "").removeprefix("Bearer ").strip(); tid = TOKENS.get(tok)
    if not tid: raise HTTPException(401)
    s = SESSIONS[tid]
    if s.frozen: raise HTTPException(410, "session frozen")
    return s

class NewSession(BaseModel):
    trial_id: str; spec_hash: str; condition: str; run_config_hash: str; visible_view: dict; stakeholder_config: dict; image: str | None = None
class Ask(BaseModel): request_id: str; question: str
class Review(BaseModel): request_id: str; diff: str; files: dict[str, str] = {}

@app.get("/health")
def health(): return {"ok": True, "sessions": len(SESSIONS)}

@app.post("/sessions")
def new_session(body: NewSession, x_worker_secret: str | None = Header(default=None)):
    _worker(x_worker_secret)
    if body.trial_id in SESSIONS: return {"token": next(t for t, tid in TOKENS.items() if tid == body.trial_id)}
    cfg = StakeholderConfig.model_validate(body.stakeholder_config)
    s = Session(body.trial_id, body.visible_view, cfg, image=body.image); s._record("session", spec_hash=body.spec_hash, condition=body.condition, run_config_hash=body.run_config_hash)
    tok = secrets.token_urlsafe(24); SESSIONS[body.trial_id] = s; TOKENS[tok] = body.trial_id; return {"token": tok}

@app.post("/ask")
async def ask(body: Ask, authorization: str | None = Header(default=None)):
    s = _session(authorization)
    r = await s.ask(body.request_id, body.question, lambda sy, m, sc: call_llm(sy, m, sc, s.cfg)); return r.__dict__

@app.post("/review")
async def review(body: Review, authorization: str | None = Header(default=None)):
    s = _session(authorization)
    tools = CheckTools(s.view.get("checks", []), s.view.get("repo_dir", "/app"), s.cfg.max_tool_calls_per_activation) if s.cfg.review_mode == "checks" else None
    r = await s.review(body.request_id, body.diff, body.files, lambda sy, m, sc: call_llm(sy, m, sc, s.cfg), tools); return r.__dict__

@app.post("/freeze")
def freeze(authorization: str | None = Header(default=None)):
    tok = (authorization or "").removeprefix("Bearer ").strip(); tid = TOKENS.get(tok)
    if not tid: raise HTTPException(401)
    s = SESSIONS[tid]; h = s.transcript_hash or s.freeze(); return {"frozen": True, "transcript_hash": h}

@app.get("/transcript/{trial_id}")
def transcript(trial_id: str, x_worker_secret: str | None = Header(default=None)):
    _worker(x_worker_secret)
    if trial_id not in SESSIONS: raise HTTPException(404)
    return SESSIONS[trial_id].to_dict()
```

```python
# swesim/stakeholder/modal_app.py
import modal
app = modal.App("swesim-stakeholder")
image = (modal.Image.debian_slim(python_version="3.12").pip_install("fastapi[standard]", "pydantic>=2.7", "anthropic>=0.40", "openai>=1.50")
         .add_local_python_source("swesim"))
@app.function(image=image, secrets=[modal.Secret.from_name("swesim-llm")], min_containers=1, max_containers=1, timeout=3600)
@modal.asgi_app()
def web():
    from swesim.stakeholder.service import app as fastapi_app
    return fastapi_app
```

`swesim/stakeholder/shims/ask-user`:
```bash
#!/bin/bash
# Usage: ask-user "question". Sends one question to the stakeholder; prints the reply.
set -u
if [ -z "${SWESIM_SESSION_TOKEN:-}" ]; then echo "No stakeholder is available in this task."; exit 0; fi
Q="$*"; RID=$(cat /proc/sys/kernel/random/uuid)
python3 - "$SWESIM_STAKEHOLDER_URL" "$SWESIM_SESSION_TOKEN" "$RID" "$Q" <<'PY'
import json, sys, urllib.request
url, tok, rid, q = sys.argv[1:5]
req = urllib.request.Request(url + "/ask", data=json.dumps({"request_id": rid, "question": q}).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok})
try:
    r = json.load(urllib.request.urlopen(req, timeout=180)); print(r["reply"]); print(f"\n[stakeholder availability remaining: {r['patience_remaining']}]")
except urllib.error.HTTPError as e:
    print("The stakeholder is no longer available." if e.code == 410 else f"stakeholder error {e.code}")
PY
```

`swesim/stakeholder/shims/request-review`:
```bash
#!/bin/bash
# Sends the current diff (tracked + untracked, capped) to the stakeholder for review; prints the feedback.
set -u
if [ -z "${SWESIM_SESSION_TOKEN:-}" ]; then echo "No stakeholder is available in this task."; exit 0; fi
cd "${SWESIM_REPO_DIR:-/app}" || exit 0
git add -N . >/dev/null 2>&1
DIFF=$(git diff --no-ext-diff --no-color | head -c 200000)
RID=$(cat /proc/sys/kernel/random/uuid)
python3 - "$SWESIM_STAKEHOLDER_URL" "$SWESIM_SESSION_TOKEN" "$RID" <<'PY' "$DIFF"
import json, sys, urllib.request
url, tok, rid, diff = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
req = urllib.request.Request(url + "/review", data=json.dumps({"request_id": rid, "diff": diff, "files": {}}).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok})
try:
    r = json.load(urllib.request.urlopen(req, timeout=300)); print(r["reply"]); print(f"\n[stakeholder availability remaining: {r['patience_remaining']}]")
except urllib.error.HTTPError as e:
    print("The stakeholder is no longer available." if e.code == 410 else f"stakeholder error {e.code}")
PY
```

`scripts/deploy_stakeholder.sh`:
```bash
#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
export $(grep -v '^#' ~/Documents/.env | xargs)
modal secret create swesim-llm ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" OPENAI_API_KEY="$OPENAI_API_KEY" SWESIM_WORKER_SECRET="${SWESIM_WORKER_SECRET:-$(openssl rand -hex 16)}" --force
uv run modal deploy swesim/stakeholder/modal_app.py
uv run python -c "import modal; print(modal.Function.from_name('swesim-stakeholder','web').get_web_url())" | tee .stakeholder_url
```

- [ ] **Step 4: Run unit tests, deploy, probe**

Run: `uv run pytest tests/test_session.py tests/test_service.py -q` → 3 passed.
Run: `bash scripts/deploy_stakeholder.sh && curl -s "$(cat .stakeholder_url)/health"` → `{"ok":true,...}`. Record the URL in `CLAUDE.md` and `.env` as `SWESIM_STAKEHOLDER_URL`; record the worker secret in `~/Documents/.env` as `SWESIM_WORKER_SECRET` (never commit).

- [ ] **Step 5: Commit**

```bash
git add swesim/stakeholder scripts/deploy_stakeholder.sh tests/test_session.py tests/test_service.py .gitignore
git commit -m "feat(stakeholder): session protocol with patience tiers, idempotent requests, freeze, diff-only and checks review, Modal deploy, shims"
```

---

### Task 9: One interactive episode end to end (run stage, minimal)

**Files:**
- Create: `swesim/stakeholder/view.py`, `swesim/stages/run.py`
- Modify: `swesim/cli.py` (add `run`)
- Create: `tests/test_run.py`

**Interfaces:**
- Produces: `view.visible_view(spec) -> dict` (`brief`, `facts` without author-only, `requirements`, `persona`, `checks`, `repo_dir`); `run.run_trial(ws, spec, run_config, attempt_index, stakeholder_url, worker_secret) -> TrialRecord(trial_id, condition, task_dir, trial_dir, reward, report, transcript, status)`; per-trial task materialization: copies the compiled condition dir to `workspace/trials/<trial_id>/task/` and sets `SWESIM_SESSION_TOKEN` and `SWESIM_TRIAL_ID` in `[environment.env]`; after `harbor run` returns, fetches `GET /transcript/{trial_id}`, checks `transcript_hash` against the receipt's, writes `workspace/trials/<trial_id>/{reward.json,report.json,transcript.json,status.json}`.

- [ ] **Step 1: Test the visible view and task materialization**

```python
# tests/test_run.py
import tomllib
from swesim.models import ScenarioSpec, RunConfig, minimal_spec_dict, trial_id
from swesim.stakeholder.view import visible_view
from swesim.stages.run import materialize_trial_task
from swesim.paths import Workspace
def test_visible_view_drops_author_only():
    d = minimal_spec_dict(); d["ledger"].append({"id": "f9", "text": "SECRET", "kind": "interface", "provenance": "authored", "authored_rationale": "x", "discovery": "author-only"})
    v = visible_view(ScenarioSpec.model_validate(d)); assert all(f["id"] != "f9" for f in v["facts"]) and v["brief"] == "It breaks."
def test_materialize_sets_token(tmp_path):
    ws = Workspace(tmp_path); src = tmp_path / "task"; src.mkdir(); (src / "task.toml").write_text('[environment.env]\nSWESIM_SESSION_TOKEN = ""\nSWESIM_TRIAL_ID = ""\n')
    out = materialize_trial_task(ws, src, "abc123", "tok")
    t = tomllib.loads((out / "task.toml").read_text()); assert t["environment"]["env"]["SWESIM_SESSION_TOKEN"] == "tok" and t["environment"]["env"]["SWESIM_TRIAL_ID"] == "abc123"
```

- [ ] **Step 2: Implement**

```python
# swesim/stakeholder/view.py
from swesim.models import ScenarioSpec, Discovery
def visible_view(spec: ScenarioSpec) -> dict:
    return {"brief": next(b.text for b in spec.briefs if b.profile == "natural"),
            "facts": [{"id": f.id, "text": f.text, "kind": f.kind.value, "discovery": f.discovery.value} for f in spec.ledger if f.discovery != Discovery.AUTHOR_ONLY],
            "requirements": [{"id": r.id, "text": r.text} for r in spec.requirements], "persona": spec.persona.model_dump(),
            "checks": [c.model_dump() for c in spec.checks], "repo_dir": spec.environment.repo_dir}
```

```python
# swesim/stages/run.py
from __future__ import annotations
import json, re, shutil, urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from swesim import harbor_cli
from swesim.models import ScenarioSpec, RunConfig, trial_id as make_trial_id
from swesim.paths import Workspace
from swesim.stakeholder.view import visible_view

@dataclass
class TrialRecord:
    trial_id: str; scenario_id: str; condition: str; attempt_index: int; run_config_hash: str; task_dir: str; trial_dir: str | None
    reward: dict; report: dict; transcript: dict; status: str

def materialize_trial_task(ws: Workspace, compiled_task_dir: Path, tid: str, token: str) -> Path:
    dst = ws.root / "trials" / tid / "task"; shutil.rmtree(dst, ignore_errors=True); shutil.copytree(compiled_task_dir, dst)
    p = dst / "task.toml"; s = p.read_text()
    s = re.sub(r'SWESIM_SESSION_TOKEN = ".*"', f'SWESIM_SESSION_TOKEN = "{token}"', s); s = re.sub(r'SWESIM_TRIAL_ID = ".*"', f'SWESIM_TRIAL_ID = "{tid}"', s)
    p.write_text(s); return dst

def _post(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}); return json.load(urllib.request.urlopen(req, timeout=60))
def _get(url: str, headers: dict) -> dict:
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60))

def run_trial(ws: Workspace, spec: ScenarioSpec, rc: RunConfig, attempt_index: int, stakeholder_url: str, worker_secret: str) -> TrialRecord:
    tid = make_trial_id(spec.spec_hash, rc.condition, rc.run_config_hash, attempt_index)
    compiled = ws.task_dir(spec.id, rc.condition); token = ""
    if rc.condition == "interactive":
        token = _post(f"{stakeholder_url}/sessions", {"trial_id": tid, "spec_hash": spec.spec_hash, "condition": rc.condition, "run_config_hash": rc.run_config_hash,
                      "visible_view": visible_view(spec), "stakeholder_config": rc.stakeholder.model_dump(), "image": spec.environment.image}, {"X-Worker-Secret": worker_secret})["token"]
    task = materialize_trial_task(ws, compiled, tid, token); tdir = ws.root / "trials" / tid
    (tdir / "status.json").write_text(json.dumps({"status": "running", "trial_id": tid}))
    status = "completed"; trial_dir = None; reward = {}; report = {}; transcript = {}
    try:
        trial_dir = harbor_cli.run_task(task, rc.agent, ws.jobs_dir / "runs", tid, model=rc.model, timeout_sec=rc.solver_timeout_sec + 2400)
        reward = harbor_cli.read_reward(trial_dir); report = harbor_cli.read_report(trial_dir)
    except harbor_cli.HarborRunError as e:
        status = "launch-failed"; report = {"error": str(e)}
    if rc.condition == "interactive":
        try:
            _post(f"{stakeholder_url}/freeze", {}, {"Authorization": f"Bearer {token}"})
        except Exception: pass
        transcript = _get(f"{stakeholder_url}/transcript/{tid}", {"X-Worker-Secret": worker_secret})
        if report.get("transcript_hash") and transcript.get("transcript_hash") and report["transcript_hash"] != transcript["transcript_hash"]:
            report["transcript_hash_mismatch"] = True
    rec = TrialRecord(tid, spec.id, rc.condition, attempt_index, rc.run_config_hash, str(task), str(trial_dir) if trial_dir else None, reward, report, transcript, status)
    for name, obj in (("reward.json", reward), ("report.json", report), ("transcript.json", transcript), ("record.json", asdict(rec))): (tdir / name).write_text(json.dumps(obj, indent=2))
    (tdir / "status.json").write_text(json.dumps({"status": status, "trial_id": tid})); return rec
```

Add to `swesim/cli.py`:
```python
@app.command()
def run(scenario_id: str, condition: str = "interactive", attempt: int = 0, model: str = "anthropic/claude-haiku-4-5-20251001",
        stakeholder_url: str = typer.Option(..., envvar="SWESIM_STAKEHOLDER_URL"), worker_secret: str = typer.Option(..., envvar="SWESIM_WORKER_SECRET")):
    from swesim.stages.run import run_trial
    rc = RunConfig.default(condition).model_copy(update={"model": model})
    rec = run_trial(WS, _load(scenario_id), rc, attempt, stakeholder_url, worker_secret); typer.echo(json.dumps({"trial_id": rec.trial_id, "status": rec.status, "reward": rec.reward}))
```

- [ ] **Step 3: Run the first interactive episode**

```bash
export $(grep -v '^#' ~/Documents/.env | xargs); export SWESIM_STAKEHOLDER_URL=$(cat .stakeholder_url)
uv run swesim compile pydata--xarray-5362
uv run swesim run pydata--xarray-5362 --condition interactive --attempt 0
```
Expected: `workspace/trials/<tid>/transcript.json` has at least one `ask` exchange with `disclosed_fact_ids`, `reward.json` numeric, `report.json` `transcript_hash` equal to the transcript's. Inspect with `harbor view workspace/jobs`.

- [ ] **Step 4: Commit**

```bash
git add swesim/stakeholder/view.py swesim/stages/run.py swesim/cli.py tests/test_run.py
git commit -m "feat(run): per-trial task materialization with session token, harbor run, transcript fetch and hash binding"
```

---

### Task 10: github-repo source: discovery, filters, environment build recipe

**Files:**
- Create: `swesim/sources/github_repo.py`
- Modify: `swesim/stages/mine.py` (add `mine_github_repo`), `swesim/stages/draft_spec.py` (accept `dockerfile` environments)
- Create: `tests/test_github_repo.py`

**Interfaces:**
- Produces: `github_repo.discover(repo: str, since: str, max_prs: int = 200, min_comments: int = 4, diff_min: int = 20, diff_max: int = 600, pr: int | None = None) -> tuple[list[dict], dict]` (candidates and a reject histogram); each candidate `{repo, pr, issue, base_commit, merge_commit, title, issue_body, thread, patch, test_patch, source_files, test_files, additions, deletions}`; `github_repo.build_recipe(repo, base_commit, profile="python-pytest") -> dict(install_command, test_command, repo_dir="/app")`; `github_repo.candidate_to_row(c) -> dict` in the same shape as SWE-bench rows (`problem_statement` = issue title + body, `hints_text` = issue comments before the PR, `fail_to_pass`/`pass_to_pass` filled by `probe_tests` below).
- `github_repo.probe_tests(row, jobs_dir) -> dict` runs, via Harbor on Modal with the `dockerfile` environment (`nop` agent on a probe task whose verifier runs the PR's test files on base and after applying the gold patch), and returns `fail_to_pass`/`pass_to_pass` ids (tests that fail on base and pass with gold; tests passing in both). This replaces SWE-bench's precomputed lists. Bounded by `--max-builds`.
- Pre-selected demonstration repository: `py-pdf/pypdf` (install: `pip install -e '.[full]' -r requirements/ci.txt`; test command `python -I -m pytest -rA -p no:cacheprovider -m "not samples and not enable_socket" --junitxml={junit_path} <files>`); since `2026-06-01`.

- [ ] **Step 1: Write failing tests (pure functions)**

```python
# tests/test_github_repo.py
from swesim.sources import github_repo as gr
def test_linked_issue_extraction():
    assert gr.linked_issues("Fixes #12 and closes #34; see #99 unrelated") == [12, 34]
def test_split_patch_by_tests():
    patch = "diff --git a/pypdf/a.py b/pypdf/a.py\n--- a/pypdf/a.py\n+++ b/pypdf/a.py\n@@ -1 +1 @@\n-x\n+y\ndiff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1 +1 @@\n-a\n+b\n"
    src, tst = gr.split_patch(patch)
    assert "pypdf/a.py" in src and "tests/test_a.py" not in src and "tests/test_a.py" in tst
def test_filters_reject_reasons():
    c = {"comments_total": 1, "additions": 5, "deletions": 0, "test_files": [], "source_files": ["a.py"], "issue": None}
    assert gr.reject_reason(c, min_comments=4, diff_min=20, diff_max=600) == "no-linked-issue"
def test_build_recipe_pypdf():
    r = gr.build_recipe("py-pdf/pypdf", "abc")
    assert "pip install -e" in r["install_command"] and r["repo_dir"] == "/app" and "-rA" in r["test_command"]
```

- [ ] **Step 2: Implement**

```python
# swesim/sources/github_repo.py
from __future__ import annotations
import json, re, subprocess
from collections import Counter
from swesim.sources import threads
LINK = re.compile(r"(?:[Ff]ix(?:es|ed)?|[Cc]los(?:es|ed)?|[Rr]esolves?)\s*:?\s*#(\d+)")
KNOWN_RECIPES = {"py-pdf/pypdf": {"install_command": "pip install -e '.[full]' -r requirements/ci.txt", "test_marker": '-m "not samples and not enable_socket"'}}

def linked_issues(body: str) -> list[int]: return sorted({int(n) for n in LINK.findall(body or "")})

def split_patch(patch: str) -> tuple[str, str]:
    src, tst = [], []
    for chunk in re.split(r"(?=^diff --git )", patch, flags=re.M):
        if not chunk.strip(): continue
        path = re.match(r"diff --git a/(\S+)", chunk).group(1)
        (tst if re.search(r"(^|/)tests?/|test_.*\.py$|_test\.py$|conftest\.py$", path) else src).append(chunk)
    return "".join(src), "".join(tst)

def reject_reason(c: dict, min_comments: int, diff_min: int, diff_max: int) -> str | None:
    if not c.get("issue"): return "no-linked-issue"
    if not c.get("test_files"): return "no-test-changes"
    if not c.get("source_files"): return "no-source-changes"
    n = c["additions"] + c["deletions"]
    if n < diff_min: return "diff-too-small"
    if n > diff_max: return "diff-too-large"
    if c.get("comments_total", 0) < min_comments: return "thin-thread"
    return None

def _gh(path: str, paginate: bool = False):
    cmd = ["gh", "api", path] + (["--paginate"] if paginate else []); return json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)

def discover(repo: str, since: str, max_prs: int = 200, min_comments: int = 4, diff_min: int = 20, diff_max: int = 600, pr: int | None = None) -> tuple[list[dict], dict]:
    prs = [ _gh(f"repos/{repo}/pulls/{pr}") ] if pr else [p for p in _gh(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100", paginate=True)[:max_prs*2] if p.get("merged_at") and p["merged_at"] >= since][:max_prs]
    cands, rejects = [], Counter()
    for p in prs:
        issues = linked_issues(p.get("body") or "")
        patch = subprocess.run(["gh", "api", f"repos/{repo}/pulls/{p['number']}", "-H", "Accept: application/vnd.github.v3.diff"], capture_output=True, text=True).stdout
        src, tst = split_patch(patch)
        c = {"repo": repo, "pr": p["number"], "title": p["title"], "base_commit": p["base"]["sha"], "merge_commit": p.get("merge_commit_sha"),
             "additions": p.get("additions", 0), "deletions": p.get("deletions", 0), "patch": src, "test_patch": tst,
             "source_files": re.findall(r"^diff --git a/(\S+)", src, re.M), "test_files": re.findall(r"^diff --git a/(\S+)", tst, re.M), "issue": None, "thread": None}
        if issues:
            c["issue"] = issues[0]; c["thread"] = threads.fetch_pr_thread(repo, p["number"])
            c["comments_total"] = len(c["thread"]["pr_comments"]) + len(c["thread"]["review_comments"]) + sum(len(i["comments"]) for i in c["thread"]["linked_issues"])
        r = reject_reason(c, min_comments, diff_min, diff_max)
        if r: rejects[r] += 1
        else: cands.append(c)
    return cands, dict(rejects)

def build_recipe(repo: str, base_commit: str, profile: str = "python-pytest") -> dict:
    k = KNOWN_RECIPES.get(repo, {}); install = k.get("install_command", "pip install -e . && pip install pytest")
    marker = k.get("test_marker", "")
    return {"repo_dir": "/app", "install_command": f"pip install --upgrade pip && {install} && pip install pytest",
            "test_command": f"python -I -m pytest -rA -p no:cacheprovider {marker} --junitxml={{junit_path}} {{test_files}}".replace("  ", " ")}

def candidate_to_row(c: dict) -> dict:
    iss = (c["thread"] or {}).get("linked_issues", [{}])[0] if c.get("thread") else {}
    before_pr = [x for x in iss.get("comments", [])]
    return {"instance_id": f"{c['repo'].replace('/', '__')}-{c['pr']}", "repo": c["repo"], "base_commit": c["base_commit"], "version": "",
            "problem_statement": (iss.get("title", c["title"]) + "\n\n" + iss.get("body", "")).strip(), "hints_text": "\n\n".join(x["body"] for x in before_pr),
            "fail_to_pass": [], "pass_to_pass": [], "patch": c["patch"], "test_patch": c["test_patch"], "image": None, "repo_dir": "/app",
            "annotation": {"underspecified": "", "underspecified_notes": "", "false_negative": "", "false_negative_notes": "", "difficulty": ""}, "thread": c["thread"] or {}}
```

`probe_tests` (in the same file): build a probe Harbor task from the `dockerfile` environment with `tests/test.sh` that runs the PR's test files on `/pristine` (before test.patch) → JSON, then applies `test.patch` → JSON, then applies `oracle.diff` too → JSON, and writes `reward.json` `{"reward": 1}`; read the three status maps from `/logs/verifier/probe.json`: `fail_to_pass = tests failing after test.patch and passing after gold; pass_to_pass = tests passing in both`. Implement as `probe_tests(row, ws) -> dict` using `compile_scenario`'s templates with a minimal spec stub; if the build fails, record the reason and return `{}`.

Add to `swesim/stages/mine.py`:
```python
def mine_github_repo(ws: Workspace, repo: str, pr: int | None = None, since: str = "2026-06-01", max_candidates: int = 20, max_builds: int = 3) -> Path:
    import json
    from swesim.sources import github_repo as gr
    cands, rejects = gr.discover(repo, since, pr=pr)
    out = ws.candidates_dir / f"github-repo--{repo.replace('/', '__')}.jsonl"; out.parent.mkdir(parents=True, exist_ok=True)
    built = 0
    with open(out, "w") as f:
        for c in cands[:max_candidates]:
            row = gr.candidate_to_row(c); row.update(gr.build_recipe(repo, c["base_commit"]))
            if built < max_builds:
                probe = gr.probe_tests(row, ws); built += 1; row.update(probe)
            if not row["fail_to_pass"]: rejects["no-f2p-after-probe"] = rejects.get("no-f2p-after-probe", 0) + 1; continue
            f.write(json.dumps(row) + "\n")
    (out.with_suffix(".rejects.json")).write_text(json.dumps(rejects, indent=2))
    if out.stat().st_size == 0: (out.with_suffix(".status")).write_text("no-valid-candidate")
    return out
```

- [ ] **Step 3: Run on pypdf, then draft/approve/validate the chosen candidate**

```bash
uv run swesim mine --source github-repo --repo py-pdf/pypdf
uv run swesim judge workspace/candidates/github-repo--py-pdf__pypdf.jsonl --limit 5
uv run swesim draft workspace/candidates/github-repo--py-pdf__pypdf.judged.jsonl py-pdf__pypdf-<PR>
# edit scenario.draft.json (environment.kind = dockerfile with dockerfile_asset produced by draft for github-repo rows), then
uv run swesim approve py-pdf--pypdf-<pr> && uv run swesim validate py-pdf--pypdf-<pr>
```

- [ ] **Step 4: Commit**

```bash
git add swesim/sources/github_repo.py swesim/stages/mine.py tests/test_github_repo.py
git commit -m "feat(sources): github-repo discovery with bounded filters, reject histogram, build recipe, test probe"
```
