# swesim Implementation Plan

> **Execution:** One implementation agent, task by task. Use available repository instructions; no unavailable skill is required. Steps use checkbox (`- [ ]`) syntax. Human approval applies only to authored scenario content, not routine implementation. Never mark a milestone complete from filenames alone.

**Goal:** Build swesim end to end: a pipeline that turns SWE-bench's human-annotated underspecified instances and any GitHub repository URL into three-condition Harbor tasks with a simulated stakeholder, grades them in a separate offline verifier with executed controls, runs them on Modal under Temporal, and shows every episode in a dashboard.

**Architecture:** Every source produces one validated `ScenarioSpec`; `compile` renders Harbor task directories from templates; the grader runs inside the separate verifier on a tarball collected by a root-owned hook; the stakeholder is a hosted Modal service with a session protocol and a limited tool loop; Temporal orchestrates stages and trials on a GCE VM; `score` and `export` read Harbor job directories and stakeholder transcripts joined by trial id.

**Tech Stack:** Python 3.12, uv, pydantic v2, typer, FastAPI, Harbor 0.23 (`harbor` CLI, Modal backend), Modal 1.5 (web endpoint, Sandbox), Temporal (`temporalio` SDK, `temporal` CLI dev server), `datasets` (Hugging Face), `gh` CLI / GitHub REST, Jinja2, pytest, Anthropic and OpenAI SDKs.

**Spec:** `docs/superpowers/specs/2026-09-12-swe-simulator-design.md` (v2.1). Reference recipe verified on Modal: `references/modal-smoke-task/`. Reviews: `docs/reviews/`.

## Global Constraints

- Python 3.12 for the package and verifier controller; historical task tests use their pinned environment interpreter. In Task 1 run `uv add "harbor[modal]==0.23.0" "modal==1.5.5" "temporalio>=1.8" python-dotenv tomli-w` and `uv add --dev pytest pytest-asyncio`; commit pyproject.toml and uv.lock. Use `uv sync --extra dev` on clean installs. The project environment must import Harbor as well as invoke its CLI; a separate uv tool install is insufficient for PatchAgent tests.
- `ScenarioSpec` and `RunConfig` models use `extra="forbid"`; ids match `^[a-z0-9][a-z0-9._-]{2,63}$`.
- `reward.json` contains only finite numbers (Harbor rejects strings and lists). Everything else goes to `report.json`.
- `success` (0/1) drives pass@k and pass^k; `reward` is partial credit by requirement groups with the regression rule "any PASS_TO_PASS failure sets reward to 0".
- Every `incorrect-exploit` tamper control must earn `reward == 0` or the scenario is `invalid`.
- Agent container: `[agent] user = "agent"`; strict allowlist baseline (model API hosts + stakeholder host) in `[environment]` and `[agent]`; `[verifier] network_mode = "no-network"`, `environment_mode = "separate"`.
- Collection: root hook calls the installed collector with a clean environment. Kill agent processes, enforce apparent-byte/member bounds, archive into root-only `/var/swesim-collect/app.tar`; freeze in `finally` and write a JSON `collect.status` receipt. Declare both as file artifacts. `/logs/artifacts` is never a trust anchor. Unit-test collector decisions; the hardened file-transfer path still requires a fresh Modal control run.
- Protected paths always include: contract test files, `**/conftest.py`, `**/sitecustomize.py`, `**/usercustomize.py`, `**/*.pth`, `pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`, `.github/**`, `.gitattributes`, `.gitignore`, lockfiles. Matching is NFC-normalized and case-folded.
- Tests run as `tester` using the pinned task interpreter with `-I /tests/run_pytest.py`; the trusted launcher imports pytest then adds submitted source roots. Clear inherited Python and pytest options. Require exact collected IDs, JUnit, terminal summary and exit-code agreement. Disagreement earns zero; single-test reruns are diagnostics and never replace failed regression evidence.
- Outcomes are keyed on `success`; `reward` is partial credit. swesim computes pass@k and pass^k itself.
- Agent toolchain pre-baked per `references/modal-smoke-task/environment/Dockerfile`; `[environment.env]` has `UV_OFFLINE=1`, `UV_NO_PROGRESS=1`, `UV_CACHE_DIR=/tmp/uv-cache`.
- Grader code that runs inside the verifier container (`swesim/verifier/*.py`) uses the Python standard library only.
- Stakeholder LLM context never contains `author-only` facts, the oracle, or hidden test files.
- Temporal: `TrialWorkflow` id is `trial_id(spec_hash, condition, run_config_hash, attempt_index)`; include the actual model, condition, compiled environment digest and purpose in RunConfig. Launch is not retried. On the VM launch a per-trial systemd unit so a Temporal worker restart cannot kill its Harbor process; polling and result ingestion are retryable and idempotent.
- Commit each coherent task and push milestone commits to origin main. Commit messages end with the environment's default attribution line `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and no other attribution. Stage only the task files; preserve concurrent edits.
- Load only required keys from `SWESIM_ENV_FILE` (default `~/Documents/.env`) with python-dotenv, never shell evaluation or `export $(...)`. Save credentials only in ignored mode-0600 files. Do not put worker/provider secrets in task artifacts, command arguments, Temporal payloads, or log output. Model IDs are explicit config, verified by a small provider call before a sweep; do not substitute models silently.

---

## File structure

```
swesim/
  cli.py                      typer app; one subcommand per stage plus run/view commands
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
    tools.py                  read_diff, read_diff and bounded supplied files; check execution deferred
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

## Milestones (cumulative `bash scripts/verify.sh N` checks)

1. Models, dependencies, fixture round-trip (Task 1).
2. Safe collection policy and independently checked test evidence (Tasks 2–3).
3. Compile and PatchAgent import/command tests (Tasks 4–5). No cloud evidence claimed yet.
4. SWE-bench mine → judge → draft → human approval → compiled Modal controls (Tasks 6–7).
5. Durable stakeholder sessions and one real interactive episode (Tasks 8–9).
6. Repo URL discovery → automatic candidate selection → approved, validated and run task (Task 10).
7. Score, export and registration plus Temporal orchestration (Task 12 **before** Task 11).
8. VM execution independent of laptop and Temporal worker, with recorded recovery evidence (Task 13).
9. Runs table and episode page using actual files/transcripts (Task 14).
10. Tracked runnable example and documentation, final checks (Tasks 15–16).

Execution order: 1–10, 12, 11, 14, 13, 15–16. Task 14 code must exist before Task 13 enables the dashboard unit. By six hours remaining, snapshot the first
valid task and write initial docs (Tasks 15–16) even if task two is blocked;
this does not mark M10 complete. Run one tier through all three conditions
first; add the second tier only after the runnable package is safe. The repo-URL
path is required; do not replace it with a hand-picked PR. If no candidate
survives, retain the complete reject report and report the unfinished milestone.
Budget the last two hours for checks, docs and presentation; optional work is
listed under Deferred. A failed milestone remains unchecked.

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

def test_roundtrip_hash():
    spec = ScenarioSpec.model_validate(minimal_spec_dict())
    assert ScenarioSpec.model_validate_json(spec.model_dump_json()).spec_hash == spec.spec_hash

def test_invalid_weights_and_overlap():
    for weight in (float("nan"), -1.0, 0.0):
        d = minimal_spec_dict(); d["groups"][0]["weight"] = weight
        with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)
    d = minimal_spec_dict(); d["contract"]["pass_to_pass"] = ["t1"]
    with pytest.raises(ValidationError): ScenarioSpec.model_validate(d)

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

def test_withheld_brief_provenance_and_leak():
    d=minimal_spec_dict();d['briefs'][0]['text']='Import fails\nTraceback: SECRET_ERROR'
    d['briefs'].append({'profile':'withheld','text':'Import fails','provenance':'withheld'})
    d['ledger'].append({'id':'withheld-001','text':'SECRET_ERROR','kind':'reproduction','provenance':'original-issue',
        'source_excerpt':'SECRET_ERROR','source_url':'https://example.com/issue','discovery':'on-request','tests':[]})
    d['operations']=[{'type':'withholding','target':'withheld-001','rationale':'Removed error_message'}]
    ScenarioSpec.model_validate(d)
    d['briefs'][1]['text']='Import fails SECRET_ERROR'
    with pytest.raises(ValidationError):ScenarioSpec.model_validate(d)

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
    r1 = RunConfig.default("interactive"); r2 = RunConfig.model_validate({**r1.model_dump(),"stakeholder":{**r1.stakeholder.model_dump(),"patience_profile":{**r1.stakeholder.patience_profile.model_dump(),"start":3}}})
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
    ORIGINAL_ISSUE = "original-issue"; ISSUE_THREAD = "issue-thread"; PR_THREAD = "pr-thread"; PR_REVIEW = "pr-review"; ANNOTATOR_NOTE = "annotator-note"; AUTHORED = "authored"

class Discovery(StrEnum):
    BRIEF = "brief"; ON_REQUEST = "on-request"; VOLUNTEERED_AT_REVIEW = "volunteered-at-review"; REPO_DERIVABLE = "repo-derivable"; AUTHOR_ONLY = "author-only"

class OperationType(StrEnum):
    WITHHOLDING = "withholding"; CLARIFICATION = "clarification"; EVALUATOR_REPAIR = "evaluator-repair"; SYNTHETIC_EXTENSION = "synthetic-extension"

class Asset(Strict):
    path: str; sha256: str = Field(pattern=r"^[0-9a-f]{64}$"); bytes: int = Field(ge=0)
    @model_validator(mode="after")
    def _path(self):
        from pathlib import PurePosixPath
        if self.path.startswith("/") or "\\" in self.path or ".." in PurePosixPath(self.path).parts:
            raise ValueError("asset path must stay in scenario directory")
        return self

class Environment(Strict):
    kind: Literal["prebuilt-image", "dockerfile"]
    image: str | None = None
    image_digest: str | None = None
    dockerfile_asset: str | None = None
    build_context_assets: list[str] = Field(default_factory=list)
    repo_url: str; base_commit: str = Field(pattern=r"^[0-9a-f]{40}$"); language: str = "python"
    gold_commit: str | None = None
    repo_dir: str = "/app"
    test_command: str; install_notes: str = ""
    @model_validator(mode="after")
    def _kind_fields(self):
        if self.kind == "prebuilt-image" and not self.image: raise ValueError("prebuilt-image needs image")
        if self.kind == "dockerfile" and not self.dockerfile_asset: raise ValueError("dockerfile kind needs dockerfile_asset")
        return self

class Brief(Strict):
    profile: str; text: str; provenance: Literal["original-issue", "withheld", "authored"]

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
    kind: Literal["pytest-ra"]
    command: str                      # run from repo_dir; must contain {junit_path}; e.g. "pytest -rA -p no:cacheprovider --junitxml={junit_path} tests/test_x.py"
    single_command: str = ""          # same with {nodeid} for per-test re-runs
    test_files: list[str] = Field(default_factory=list)
    nodeid_map: dict[str, str] = Field(default_factory=dict)  # historical id -> exact collected pytest nodeid

class Contract(Strict):
    fail_to_pass: list[str]; pass_to_pass: list[str]
    test_assets: list[str] = Field(min_length=1)
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
    id: str; weight: float = Field(gt=0, le=1, allow_inf_nan=False); tests: list[str] = Field(min_length=1)

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
        if len({b.profile for b in self.briefs})!=len(self.briefs): raise ValueError("duplicate brief profile")
        withheld=next((b for b in self.briefs if b.profile=='withheld'),None)
        natural=next(b for b in self.briefs if b.profile=='natural')
        if withheld:
            if withheld.provenance!='withheld': raise ValueError('withheld brief provenance')
            ops=[o for o in self.operations if o.type==OperationType.WITHHOLDING]
            if not ops: raise ValueError('withheld brief requires recorded removals')
            for op in ops:
                fact=next((f for f in self.ledger if f.id==op.target),None)
                if not fact or fact.provenance!=Provenance.ORIGINAL_ISSUE or not fact.source_excerpt:
                    raise ValueError('withholding must reference original-issue excerpt')
                if fact.source_excerpt not in natural.text: raise ValueError('removed span absent from natural issue')
                if fact.source_excerpt.casefold() in withheld.text.casefold(): raise ValueError('removed excerpt still visible')
            # Keep the literal title, a conservative check; human approval assesses symptom/goal fidelity.
            if natural.text.splitlines()[0].strip() not in withheld.text: raise ValueError('withheld brief must retain title')
        elif any(o.type==OperationType.WITHHOLDING for o in self.operations): raise ValueError('missing withheld profile')
        if not self.contract.fail_to_pass: raise ValueError("fail_to_pass empty")
        f2p, p2p = self.contract.fail_to_pass, self.contract.pass_to_pass
        if len(f2p) != len(set(f2p)) or len(p2p) != len(set(p2p)) or set(f2p) & set(p2p):
            raise ValueError("contract ids must be unique and disjoint")
        if len({g.id for g in self.groups}) != len(self.groups): raise ValueError("duplicate group ids")
        tests = set(f2p) | set(p2p)
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
        refs += self.environment.build_context_assets
        if self.environment.dockerfile_asset: refs.append(self.environment.dockerfile_asset)
        for r in self.requirements:
            if r.fact_id and next(f for f in self.ledger if f.id == r.fact_id).discovery == Discovery.AUTHOR_ONLY:
                raise ValueError("stakeholder requirement cannot depend on author-only fact")
        if not self.assets.get(self.controls.oracle) or self.assets[self.controls.oracle].bytes <= 0:
            raise ValueError("nonempty oracle asset required")
        if self.difficulty.judged and self.difficulty.judged.get("structural_tests") and not self.controls.alternative_correct:
            raise ValueError("structural tests require an alternative-correct control")
        for ref in refs:
            if ref not in self.assets: raise ValueError(f"asset ref {ref} not in manifest")
        if not self.controls.plausible_wrong: raise ValueError("at least one plausible_wrong control")
        if not any(c.cls == "incorrect-exploit" for c in self.controls.tamper): raise ValueError("at least one incorrect-exploit tamper control")
        for t in self.checks:
            if t.id in tests: raise ValueError("checks must be disjoint from contract tests")
        return self

class PatienceProfile(Strict):
    start: int = Field(6, ge=0); question_cost: int = Field(1, ge=1); review_cost: int = Field(2, ge=1)
    terse_below: int = Field(3, ge=0); refuse_at: int = Field(0, ge=0)
    @model_validator(mode="after")
    def _bounds(self):
        if not 0 <= self.refuse_at <= self.terse_below <= self.start: raise ValueError("invalid patience thresholds")
        return self

class StakeholderConfig(Strict):
    model_id: str = "openai/gpt-5.5"; prompt_version: str = "v1"; temperature: float = 0.0
    patience_profile: PatienceProfile = Field(default_factory=PatienceProfile)
    review_mode: Literal["diff-only"] = "diff-only"; max_tool_calls_per_activation: int = Field(2, ge=0, le=4)

class RunConfig(Strict):
    agent: str = "mini-swe-agent"; agent_version: str | None = "2.4.6"
    model: str = "anthropic/claude-haiku-4-5-20251001"
    condition: Literal["full", "hidden", "interactive"]
    brief_profile: Literal["natural", "withheld"] = "withheld"
    solver_timeout_sec: int = Field(2400, ge=60)
    purpose: Literal["calibration", "comparison", "demonstration"] = "demonstration"
    compiled_hash: str = ""  # required for actual run; contract/environment content hash, excluding condition prose
    stakeholder: StakeholderConfig = Field(default_factory=StakeholderConfig)
    k: int = 3; harbor_version: str = "0.23.0"; environment_provider: Literal["modal"] = "modal"
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
                   "tamper.diff": {"path": "assets/tamper.diff", "sha256": "0" * 64, "bytes": 1},
                   "test.patch": {"path":"assets/test.patch", "sha256":"0"*64,"bytes":1}},
        "briefs": [{"profile": "natural", "text": "It breaks.", "provenance": "original-issue"}],
        "ledger": [{"id": "f01", "text": "Raise ValueError", "kind": "behavior", "provenance": "pr-review",
                    "source_excerpt": "raise ValueError", "source_url": "https://example.com/pr#r1",
                    "discovery": "on-request", "tests": ["t1"]}],
        "persona": {"summary": "maintainer", "tone": "terse", "disclosure_style": "answers what is asked"},
        "requirements": [{"id": "r01", "text": "Raise ValueError", "fact_id": "f01", "tests": ["t1"]}],
        "contract": {"fail_to_pass": ["t1"], "pass_to_pass": ["t0"], "test_assets":["test.patch"], "runner": {"kind": "pytest-ra", "command": "pytest -rA --junitxml={junit_path} tests", "single_command": "pytest -rA --junitxml={junit_path} {nodeid}"}},
        "controls": {"oracle": "oracle.diff",
                     "plausible_wrong": [{"name": "keyerror", "asset": "wrong.diff", "rationale": "wrong type", "expected_success": 0, "expected_reward_max": 0.0, "cls": "plausible-wrong"}],
                     "tamper": [{"name": "root-conftest", "asset": "tamper.diff", "rationale": "no-op tests", "expected_reward": 0.0, "cls": "incorrect-exploit"}]},
        "groups": [{"id": "g01", "weight": 1.0, "tests": ["t1"]}],
    }
```

```python
# swesim/paths.py
import json, os, tempfile
from pathlib import Path

def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, sort_keys=True, indent=2, allow_nan=False); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)

class Workspace:
    def __init__(self, root: Path | str = "workspace", registry: Path | str = "registry.json"):
        self.root = Path(root).resolve(); self.registry_path = Path(registry).resolve()
    def scenario_dir(self, sid):
        from swesim.models import ID_RE
        if not ID_RE.fullmatch(sid): raise ValueError("invalid scenario id")
        return self.root / "scenarios" / sid
    def task_dir(self, sid, condition):
        if condition not in {"full", "hidden", "interactive"}: raise ValueError("condition")
        return self.root / "tasks" / f"{sid}__{condition}"
    @property
    def jobs_dir(self): return self.root / "jobs"
    @property
    def candidates_dir(self): return self.root / "candidates"
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_models.py -q`
Expected: all listed tests pass, with no skips.

- [ ] **Step 5: Commit**

```bash
git add swesim/models.py swesim/paths.py tests/test_models.py pyproject.toml uv.lock
git commit -m "feat(models): ScenarioSpec v2 with strict validation, RunConfig, trial ids"
```

---

### Task 2: Verifier policy and parsers (stdlib only)

**Files:**
- Create: `swesim/verifier/__init__.py`, `swesim/verifier/policy.py`, `swesim/verifier/parsers.py`
- Create: `tests/test_policy.py`, `tests/test_parsers.py`

**Interfaces:**
- Produces `is_protected(path,globs)`, `safe_extract(tar_path,dest)`, `change_set(base,tree)`, `change_hash(base,tree,changes)` and `apply_change_set(target,tree,changes,protected)`.
- `parse_pytest_ra(stdout,expected_nodeids)` preserves whole parameterized IDs; `parse_junit(path,expected_nodeids)` maps exact class/method IDs; `reconcile(stdout,xml_path,expected,rc)` requires complete matching evidence. Historical SWE-bench IDs are not assumed identical: `Runner.nodeid_map` is an explicit bijection validated on base/gold. Unsupported mappings reject the candidate; non-pytest adapters are deferred.

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
========================= 2 passed, 1 failed, 1 skipped in 0.12s ========================="""
def test_parse_pytest_ra():
    out = parsers.parse_pytest_ra(RA)
    assert out["tests/test_m.py::test_ok"] == "passed"
    assert out["tests/test_m.py::TestC::test_in_class[param-1]"] == "passed"
    assert out["tests/test_m.py::test_bad"] == "failed"
    assert out["tests/test_m.py::test_xf"] == "xfail" and out["tests/test_m.py::test_err"] == "error"
    assert parsers.summary_counts(RA) == {"passed": 2, "failed": 1, "skipped": 1}

def test_ra_preserves_parameter_spaces():
    tid = "tests/test_m.py::TestC::test_x[param with - spaces]"
    assert parsers.parse_pytest_ra("PASSED " + tid, [tid]) == {tid:"passed"}

def test_duplicate_terminal_id_rejected():
    import pytest
    with pytest.raises(parsers.EvidenceError):
        parsers.parse_pytest_ra("PASSED a.py::test_x\nPASSED a.py::test_x", ["a.py::test_x"])

def test_class_junit_mapping(tmp_path):
    p=tmp_path/"j.xml"; p.write_text('<testsuite><testcase classname="tests.test_m.TestC" name="test_x[param with spaces]"/></testsuite>')
    tid="tests/test_m.py::TestC::test_x[param with spaces]"
    assert parsers.parse_junit(p,[tid]) == {tid:"passed"}

```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_policy.py tests/test_parsers.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# swesim/verifier/policy.py
from __future__ import annotations
import fnmatch, hashlib, os, shutil, tarfile, unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_BYTES = 500 * 1024 * 1024
MAX_FILES = 100000
EXCLUDED = {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache'}
DEFAULT_PROTECTED = ['**/conftest.py', '**/sitecustomize.py', '**/usercustomize.py', '**/*.pth',
    'pytest.ini', 'tox.ini', 'setup.cfg', 'pyproject.toml', '.github/**', '.gitattributes', '.gitignore',
    '**/uv.lock', '**/poetry.lock', '**/Pipfile.lock', '**/requirements*.txt']

def _norm(p):
    p = unicodedata.normalize('NFC', p.replace('\\', '/'))
    while p.startswith('./'): p = p[2:]
    return p.casefold()

def is_protected(path, globs):
    n = _norm(path)
    for g in globs:
        g = _norm(g)
        if fnmatch.fnmatchcase(n, g) or (g.startswith('**/') and fnmatch.fnmatchcase(n, g[3:])):
            return True
        if g.endswith('/**') and (n == g[:-3] or n.startswith(g[:-3] + '/')): return True
    return False

@dataclass
class ExtractResult:
    extracted: int = 0
    rejected: list = field(default_factory=list)

class InvalidArchive(ValueError): pass

def safe_extract(tar_path, dest):
    dest.mkdir(parents=True, exist_ok=True); res = ExtractResult(); seen = set(); total = 0
    with tarfile.open(tar_path) as t:
        for i, m in enumerate(t):
            if i >= MAX_FILES: raise InvalidArchive('too many members')
            p = PurePosixPath(m.name)
            if p.is_absolute() or '..' in p.parts or '\\' in m.name:
                res.rejected.append((m.name, 'escape')); continue
            if not (m.isfile() or m.isdir()):
                res.rejected.append((m.name, 'non-regular')); continue
            if any(part in EXCLUDED for part in p.parts): continue
            key = _norm(p.as_posix())
            if key in seen and key != '.': raise InvalidArchive('duplicate or case/Unicode collision')
            seen.add(key); total += m.size
            if m.size < 0 or total > MAX_BYTES: raise InvalidArchive('oversized')
            dst = dest.joinpath(*p.parts)
            if m.isdir(): dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                with t.extractfile(m) as src, open(dst, 'xb') as out: shutil.copyfileobj(src, out)
                dst.chmod(0o755 if m.mode & 0o111 else 0o644)
            res.extracted += 1
    return res

def _hash(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''): h.update(chunk)
    return h.hexdigest()

def _files(root):
    out = {}
    for p in root.rglob('*'):
        rel = p.relative_to(root)
        if any(part in EXCLUDED for part in rel.parts): continue
        if p.is_symlink(): raise InvalidArchive('symlink in supported source tree')
        if p.is_file(): out[rel.as_posix()] = p
    return out

@dataclass
class ChangeSet:
    added: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    deleted: list = field(default_factory=list)

def change_set(pristine, tree):
    a, b = _files(pristine), _files(tree); cs = ChangeSet()
    for rel in sorted(a.keys() | b.keys()):
        if rel not in b: cs.deleted.append(rel)
        elif rel not in a: cs.added.append(rel)
        elif _hash(a[rel]) != _hash(b[rel]): cs.modified.append(rel)
    return cs

def change_hash(base, tree, cs):
    import json
    rows = [{'path': p, 'before': _hash(base/p) if (base/p).is_file() else None,
             'after': _hash(tree/p) if (tree/p).is_file() else None}
            for p in sorted(cs.added + cs.modified + cs.deleted)]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

@dataclass
class ApplyResult:
    applied: list = field(default_factory=list)
    dropped: list = field(default_factory=list)

def apply_change_set(target, tree, cs, protected):
    res = ApplyResult()
    for rel in sorted(cs.added + cs.modified + cs.deleted):
        if is_protected(rel, protected): res.dropped.append(rel); continue
        dst = target / rel
        if rel in cs.deleted:
            if dst.is_file(): dst.unlink()
        else:
            dst.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(tree / rel, dst)
        res.applied.append(rel)
    return res
```

```python
# swesim/verifier/parsers.py
from __future__ import annotations
import re
import xml.etree.ElementTree as ET

class EvidenceError(ValueError): pass

def junit_key(nodeid):
    path, *parts = nodeid.split('::')
    if not parts or not path.endswith('.py'): raise EvidenceError('unsupported nodeid')
    return ('.'.join([path[:-3].replace('/', '.'), *parts[:-1]]), parts[-1])

def parse_junit(xml_path, expected=None):
    root = ET.parse(xml_path).getroot(); out = {}; inverse = {}
    if expected is not None:
        for nodeid in expected:
            key = junit_key(nodeid)
            if key in inverse: raise EvidenceError('ambiguous JUnit mapping')
            inverse[key] = nodeid
    for tc in root.iter('testcase'):
        key = (tc.get('classname', ''), tc.get('name', ''))
        if expected is None:
            # Only simple functions in this convenience path. Grading always supplies a manifest.
            tid = key[0].replace('.', '/') + '.py::' + key[1]
        else:
            if key not in inverse: raise EvidenceError('unexpected JUnit testcase: '+repr(key))
            tid = inverse[key]
        if tid in out: raise EvidenceError('duplicate JUnit id')
        children = {c.tag for c in tc}
        out[tid] = 'error' if 'error' in children else 'failed' if 'failure' in children else 'skipped' if 'skipped' in children else 'passed'
    if expected is not None and set(out) != set(expected): raise EvidenceError('incomplete JUnit report')
    return out

def parse_pytest_ra(stdout, expected=None):
    out = {}; nodeids = sorted(expected or [], key=len, reverse=True)
    for line in stdout.splitlines():
        m = re.match(r'^(PASSED|FAILED|ERROR|XFAIL|XPASS) (.+)$', line.strip())
        if not m: continue
        state, tail = m.groups()
        if expected is not None:
            matches = [n for n in nodeids if tail == n or tail.startswith(n + ' - ')]
            if not matches: continue
            nodeid = matches[0]
        else:
            # Used only for the simple parser fixture. Probe obtains exact ids from collection.
            nodeid = tail.split(' - ', 1)[0]
        if nodeid in out: raise EvidenceError('duplicate terminal id')
        out[nodeid] = {'PASSED':'passed','FAILED':'failed','ERROR':'error','XFAIL':'xfail','XPASS':'xpass'}[state]
    return out

def summary_counts(stdout):
    for line in reversed(stdout.splitlines()):
        if re.match(r'^=+ .* in [0-9.]', line.strip()):
            return {('error' if k.startswith('error') else k): int(n)
                    for n,k in re.findall(r'(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)',line)}
    return {}

def reconcile(stdout, xml_path, expected, rc):
    j = parse_junit(xml_path, expected); ra = parse_pytest_ra(stdout, expected); counts = summary_counts(stdout)
    if not counts: raise EvidenceError('missing terminal summary')
    for key in ('passed','failed','error','skipped'):
        # xfail is a skipped JUnit case; no expected skipped test earns credit.
        observed = sum(v == key for v in j.values())
        terminal = counts.get(key,0) + (counts.get('xfailed',0) if key == 'skipped' else 0)
        if observed != terminal: raise EvidenceError('summary disagrees: '+key)
    for tid, status in j.items():
        if status in {'passed','failed','error'} and ra.get(tid) != status:
            raise EvidenceError('terminal/JUnit id disagreement: '+tid)
    expected_rc = 1 if any(v in {'failed','error'} for v in j.values()) else 0
    if rc != expected_rc: raise EvidenceError('exit disagrees')
    return j
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_policy.py tests/test_parsers.py -q`
Expected: all listed tests pass, with no skips.

- [ ] **Step 5: Commit**

```bash
git add swesim/verifier tests/test_policy.py tests/test_parsers.py
git commit -m "feat(verifier): stdlib path policy, safe extraction, change sets, junit parser"
```

---

### Task 3: Grader entrypoint with partial reward and outcome attribution

**Files:**
- Create: `swesim/verifier/grader.py`, `swesim/verifier/run_pytest.py`
- Create: `tests/test_grader.py`

**Interfaces:**
- Consumes: `policy.*`, `parsers.*`, `run_pytest.py`.
- `GradeConfig(artifacts_dir, pristine_dir, work_dir, expected_tests, groups, protected, runner, test_user, verifier_out, test_timeout_sec=1500, target_dir=None, contract_patch=None)` and `grade(cfg) -> GradeResult(reward,report)`. The untouched base is `pristine_dir`; tests overlay only `target_dir` after changes. Receipt is JSON `{status,rc,user,sha256,bytes,files,freeze_status,transcript_hash}`. Outcome codes: 1 valid-correct, 2 valid-incorrect, 3 agent-invalid-submission, 4 agent-runtime-failure, 5 collection-failure, 6 simulator-failure, 7 evaluator-failure. Reports always contain `per_test`, `per_group`, `patch_sha256`, `transcript_hash`, `reviewed_diff_sha256`, dropped paths and integrity signals. Worker adds `reviewed_equals_submitted:null` and simulator validity.

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
        groups=[{"id": "g01", "weight": 1.0, "tests": ["tests/test_mod.py::test_new"]}],
        protected=["tests/**", "**/conftest.py", "**/sitecustomize.py", "**/usercustomize.py", "**/*.pth"],
        runner={"kind": "pytest-ra", "command": sys.executable + " -I " + str(Path(grader.__file__).with_name("run_pytest.py")) + " -rA -p no:cacheprovider --junitxml={junit_path} tests",
                "single_command": sys.executable + " -I " + str(Path(grader.__file__).with_name("run_pytest.py")) + " -rA -p no:cacheprovider --junitxml={junit_path} {nodeid}"},
        test_user=None, verifier_out=out)

def _artifacts(tmp_path, tree):
    a = tmp_path / "artifacts"; a.mkdir(exist_ok=True); _tar_tree(tree, a / "app.tar")
    (a / "collect.status").write_text(json.dumps({"status":"ok", "rc":0, "sha256":grader.sha256_file(a / "app.tar"), "user":"root"})); return a

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

def test_nop_records_real_base_statuses(tmp_path):
    p=_pristine(tmp_path); result=grader.grade(_cfg(tmp_path,p,_artifacts(tmp_path,p)))
    assert {x["id"]:x["observed"] for x in result.report["per_test"]} == {
        "tests/test_mod.py::test_new":"failed", "tests/test_mod.py::test_old":"passed"}

def test_receipt_nonzero_is_not_validated(tmp_path):
    p=_pristine(tmp_path); a=_artifacts(tmp_path,p)
    receipt=json.loads((a/"collect.status").read_text()); receipt["rc"]=2
    (a/"collect.status").write_text(json.dumps(receipt))
    assert grader.grade(_cfg(tmp_path,p,a)).reward["outcome_code"] == 5

def test_hash_includes_contents(tmp_path):
    from swesim.verifier import policy
    p=_pristine(tmp_path); t=tmp_path/"tree"; shutil.copytree(p,t)
    f=t/"pkg/mod.py"; f.write_text("x=1"); cs=policy.change_set(p,t); a=policy.change_hash(p,t,cs)
    f.write_text("x=2"); assert a!=policy.change_hash(p,t,cs)

```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_grader.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement**

```python
# swesim/verifier/grader.py
from __future__ import annotations
import argparse, hashlib, json, os, pwd, shutil, subprocess, sys, tempfile, time, shlex
from dataclasses import dataclass
from pathlib import Path
if __package__:
    from . import policy, parsers
else:
    sys.path.insert(0, str(Path(__file__).parent)); import policy, parsers
OUTCOMES = {'valid-correct':1,'valid-incorrect':2,'agent-invalid-submission':3,'agent-runtime-failure':4,
            'collection-failure':5,'simulator-failure':6,'evaluator-failure':7}
@dataclass
class GradeConfig:
    artifacts_dir: Path; pristine_dir: Path; work_dir: Path
    expected_tests: dict; groups: list; protected: list; runner: dict
    test_user: str | None; verifier_out: Path
    test_timeout_sec: int = 1500
    target_dir: Path | None = None  # /testbed or /app: preserve editable-install paths
    contract_patch: Path | None = None
@dataclass
class GradeResult:
    reward: dict; report: dict

def sha256_file(p): return policy._hash(p)
def _write(cfg,r,report):
    cfg.verifier_out.mkdir(parents=True,exist_ok=True)
    for name,value in [('reward.json',r),('report.json',report)]:
        (cfg.verifier_out/name).write_text(json.dumps(value,indent=2,allow_nan=False))
    return GradeResult(r,report)
def _base_reward(outcome,**kw):
    r=dict(reward=0.0,success=0,f2p_passed=0,f2p_expected=0,p2p_passed=0,p2p_expected=0,
           groups_passed=0,groups_total=0,integrity_signal_count=0,outcome_code=OUTCOMES[outcome],patch_applied=0)
    r.update(kw); return r

def _run_tests(cfg, repo, expected):
    # Every run gets new output paths. The test user can write reports, not the final reward.
    d=Path(tempfile.mkdtemp(prefix='swesim-results-')); d.chmod(0o755)
    if cfg.test_user: shutil.chown(d,user=cfg.test_user,group=cfg.test_user)
    junit=d/'junit.xml'; ids=d/'nodeids.json'
    cmd=cfg.runner['command'].format(junit_path=shlex.quote(str(junit)))
    env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(d),'LANG':'C.UTF-8','SWESIM_NODEIDS':str(ids)}
    argv=['/bin/bash','--noprofile','--norc','-c',cmd]
    if cfg.test_user: argv=['/usr/sbin/runuser','-u',cfg.test_user,'--',*argv]
    try:
        p=subprocess.Popen(argv,cwd=repo,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env=env,start_new_session=True)
        try: out,_=p.communicate(timeout=cfg.test_timeout_sec)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(p.pid,signal.SIGKILL); out,_=p.communicate(); return {},-9,out,'timeout'
        collected=json.loads(ids.read_text())
        if len(collected)!=len(set(collected)): raise parsers.EvidenceError('duplicate collection ids')
        # Runner selects exactly the contract ids in validation; probe can pass None.
        if expected is not None and set(collected)!=set(expected): raise parsers.EvidenceError('collection differs from manifest')
        st=parsers.reconcile(out,junit,collected,p.returncode)
        return st,p.returncode,out,None
    except (OSError,ValueError,parsers.ET.ParseError) as e:
        return {},p.returncode if 'p' in locals() and p.returncode is not None else -1,locals().get('out',''),str(e)

def grade(cfg):
    cfg.work_dir.mkdir(parents=True,exist_ok=True)
    report={'integrity_signals':[],'rejected_members':[],'dropped_paths':[],'per_test':[],'per_group':[],
            'transcript_hash':None,'patch_sha256':None,'reviewed_diff_sha256':None}
    def finish(outcome,**kw):
        report['outcome']=outcome
        return _write(cfg,_base_reward(outcome,f2p_expected=sum(x=='f2p' for x in cfg.expected_tests.values()),
            p2p_expected=sum(x=='p2p' for x in cfg.expected_tests.values()),groups_total=len(cfg.groups),
            integrity_signal_count=len(report['integrity_signals']),**kw),report)
    try: receipt=json.loads((cfg.artifacts_dir/'collect.status').read_text())
    except (OSError,ValueError): return finish('collection-failure')
    report['receipt']=receipt; report['transcript_hash']=receipt.get('transcript_hash')
    if receipt.get('status') in {'oversized','invalid-tree'}: return finish('agent-invalid-submission')
    tar=cfg.artifacts_dir/'app.tar'
    if receipt.get('status')!='ok' or receipt.get('rc')!=0 or receipt.get('user')!='root' or not tar.is_file():
        return finish('collection-failure')
    if receipt.get('sha256')!=sha256_file(tar): return finish('collection-failure')
    tree=cfg.work_dir/'tree'; target=cfg.target_dir or cfg.work_dir/'repo'
    try:
        if tree.exists(): shutil.rmtree(tree)
        ext=policy.safe_extract(tar,tree); cs=policy.change_set(cfg.pristine_dir,tree)
        if target.exists(): shutil.rmtree(target)
        shutil.copytree(cfg.pristine_dir,target)
        applied=policy.apply_change_set(target,tree,cs,cfg.protected+policy.DEFAULT_PROTECTED)
    except (policy.InvalidArchive,OSError,ValueError) as e:
        report['invalid_tree']=str(e); return finish('agent-invalid-submission')
    report['rejected_members']=ext.rejected; report['dropped_paths']=applied.dropped
    report['integrity_signals']=[{'kind':'protected-path-edit','path':x} for x in applied.dropped]
    report['patch_sha256']=policy.change_hash(cfg.pristine_dir,target,policy.change_set(cfg.pristine_dir,target))
    import difflib
    patch=[]
    for rel in applied.applied:
        a=cfg.pristine_dir/rel; b=target/rel
        try:
            old=a.read_text().splitlines(keepends=True) if a.is_file() else []
            new=b.read_text().splitlines(keepends=True) if b.is_file() else []
        except UnicodeDecodeError:
            report.setdefault('binary_changes',[]).append(rel); continue
        patch.extend(difflib.unified_diff(old,new,fromfile='a/'+rel if a.exists() else '/dev/null',tofile='b/'+rel if b.exists() else '/dev/null'))
    cfg.verifier_out.mkdir(parents=True,exist_ok=True)
    (cfg.verifier_out/'submission.diff').write_text(''.join(patch))
    # Diff against untouched base FIRST. Overlay hidden assertions only after applying source changes.
    if cfg.contract_patch:
        proc=subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null','apply','--whitespace=nowarn',str(cfg.contract_patch)],cwd=target,capture_output=True)
        if proc.returncode: return finish('evaluator-failure')
    if cfg.test_user:
        subprocess.run(['/usr/bin/chown','-R',f'{cfg.test_user}:{cfg.test_user}',str(target)],check=True)
    mapping=cfg.runner.get('nodeid_map',{})
    actual=[mapping.get(t,t) for t in cfg.expected_tests]
    statuses,rc,out,error=_run_tests(cfg,target,actual)
    report.update(runner_exit_code=rc,runner_tail=out[-4000:],runner_error=error)
    if error:
        # Never promote inconsistent aggregate evidence into credit; record per-test reruns as diagnostics.
        report['reruns']=[]
        for t,k in cfg.expected_tests.items():
            if k!='f2p': continue
            single=dict(cfg.runner,command=cfg.runner['single_command'].replace('{nodeid}',shlex.quote(mapping.get(t,t))))
            from dataclasses import replace
            st,sr,so,se=_run_tests(replace(cfg,runner=single),target,[mapping.get(t,t)])
            report['reruns'].append({'id':t,'statuses':st,'exit_code':sr,'error':se})
        return finish('agent-runtime-failure',patch_applied=int(bool(applied.applied)))
    observed={t:statuses.get(mapping.get(t,t),'missing') for t in cfg.expected_tests}
    report['per_test']=[dict(id=t,kind=k,expected='passed',observed=observed[t]) for t,k in cfg.expected_tests.items()]
    f=sum(observed[t]=='passed' for t,k in cfg.expected_tests.items() if k=='f2p')
    pp=sum(observed[t]=='passed' for t,k in cfg.expected_tests.items() if k=='p2p')
    regression=any(observed[t]!='passed' for t,k in cfg.expected_tests.items() if k=='p2p')
    reward=0.; ng=0
    for g in cfg.groups:
        ok=all(observed[t]=='passed' for t in g['tests']); ng+=ok
        report['per_group'].append(dict(id=g['id'],weight=g['weight'],passed=ok))
        reward+=g['weight']*ok
    success=int(all(v=='passed' for v in observed.values()) and bool(observed))
    outcome='valid-correct' if success else 'valid-incorrect'
    # No-op still executes tests for base evidence; it cannot pass a validated F2P contract.
    if not applied.applied: outcome='agent-invalid-submission'; success=0; reward=0.
    return finish(outcome,reward=0. if regression else round(reward,8),success=success,
                  f2p_passed=f,p2p_passed=pp,groups_passed=ng,patch_applied=int(bool(applied.applied)))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',required=True); a=ap.parse_args()
    c=json.loads(Path(a.config).read_text())
    for k in ('artifacts_dir','pristine_dir','work_dir','verifier_out','target_dir','contract_patch'):
        if c.get(k) is not None: c[k]=Path(c[k])
    cfg=GradeConfig(**c)
    try: grade(cfg)
    except Exception as e:
        _write(cfg,_base_reward('evaluator-failure'),{'outcome':'evaluator-failure','error':repr(e)})
if __name__=='__main__': main()
```

`swesim/verifier/run_pytest.py` (copied to `/tests/run_pytest.py`; only this runner imports pytest):
```python
# This root-owned launcher starts under the task interpreter with -I.
# Import the trusted runner first, then explicitly add the submitted source roots.
import json, os, sys
from pathlib import Path
import pytest

class Manifest:
    def pytest_collection_finish(self, session):
        Path(os.environ['SWESIM_NODEIDS']).write_text(json.dumps([i.nodeid for i in session.items]))

for rel in reversed(['.', 'src']):
    p = Path.cwd() / rel
    if p.is_dir(): sys.path.insert(0, str(p))
raise SystemExit(pytest.main(sys.argv[1:], plugins=[Manifest()]))
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_grader.py -q`
Expected: all listed tests pass, with no skips.

- [ ] **Step 5: Commit**

```bash
git add swesim/verifier/grader.py tests/test_grader.py
git commit -m "feat(verifier): grader with receipt check, policy apply, partial reward by groups, outcome codes"
```

---

### Task 4: LLM client and Harbor task templates + compile

**Files:**
- Create: `swesim/llm.py`, `swesim/templates/task.toml.j2`, `swesim/templates/instruction.md.j2`, `swesim/templates/environment.Dockerfile.j2`, `swesim/templates/tests.Dockerfile.j2`, `swesim/templates/test.sh`, `swesim/templates/solve.sh.j2`, `swesim/templates/collect.py`, `swesim/stages/__init__.py`, `swesim/stages/compile.py`
- Create: `tests/test_compile.py`

**Interfaces:**
- Consumes: `ScenarioSpec`, `RunConfig`, `Workspace`.
- Produces: `llm.chat_json(model_id: str, system: str, messages: list[dict], schema_hint: str) -> dict` (routes `anthropic/*` to the Anthropic SDK and `openai/*` to the OpenAI SDK; temperature and max_tokens optional; raises `LLMError` or provider/JSON error); `compile.compile_scenario(ws, spec, run_config, stakeholder_url, resolve_digest=True, session_token="", trial_id="") -> CompileResult(task_dirs: dict[condition, Path], image_digest: str | None, compile_json: Path)`. Task dir layout per Harbor: `task.toml`, `instruction.md`, `environment/Dockerfile`, `environment/shims/ask-user`, `environment/shims/request-review`, `tests/Dockerfile`, `tests/test.sh`, `tests/grader.py`, `tests/policy.py`, `tests/parsers.py`, `tests/grader_config.json`, `tests/test.patch` (hidden tests), `solution/solve.sh`, `solution/oracle.diff`, `controls/<name>.diff`.

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
    for name in ("oracle.diff", "wrong.diff", "tamper.diff"): (sdir / "assets" / name).write_text("x")
    import hashlib
    d["assets"] = {n: {"path":"assets/"+n,"sha256":hashlib.sha256(b"x").hexdigest(),"bytes":1} for n in ("oracle.diff","wrong.diff","tamper.diff","test.patch")}
    spec=ScenarioSpec.model_validate(d)
    (sdir / "assets" / "test.patch").write_text("x")
    res = comp.compile_scenario(ws, spec, RunConfig.default("interactive"), stakeholder_url="https://x.modal.run", resolve_digest=False)
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
    import hashlib
    for a in d["assets"].values(): a["sha256"]=hashlib.sha256(b"x").hexdigest()
    spec=ScenarioSpec.model_validate(d)
    res = comp.compile_scenario(ws, spec, RunConfig.default("full"), stakeholder_url="https://x.modal.run", resolve_digest=False)
    for cond in ("full", "hidden", "interactive"):
        assert "SECRET-HELPER-NAME" not in (res.task_dirs[cond] / "instruction.md").read_text()

def test_compiler_selects_withheld_profile(tmp_path):
    import hashlib
    d=minimal_spec_dict(); d['briefs'][0]['text']='Import fails\nERROR42'
    d['briefs'].append({'profile':'withheld','text':'Import fails','provenance':'withheld'})
    d['ledger'].append({'id':'withheld-001','text':'ERROR42','kind':'reproduction','provenance':'original-issue','source_excerpt':'ERROR42','source_url':'https://example.com/issue','discovery':'on-request'})
    d['operations']=[{'type':'withholding','target':'withheld-001','rationale':'error_message'}]
    ws=Workspace(tmp_path/'w'); assets=ws.scenario_dir(d['id'])/'assets';assets.mkdir(parents=True)
    for name,a in d['assets'].items():
        (assets/name).write_text('x');a['sha256']=hashlib.sha256(b'x').hexdigest()
    spec=ScenarioSpec.model_validate(d)
    result=comp.compile_scenario(ws,spec,RunConfig.default('interactive'),'https://x.modal.run',resolve_digest=False)
    assert 'ERROR42' in (result.task_dirs['full']/'instruction.md').read_text()
    for cond in ('hidden','interactive'):assert 'ERROR42' not in (result.task_dirs[cond]/'instruction.md').read_text()

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
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(os.environ.get('SWESIM_ENV_FILE',str(Path.home()/'Documents/.env')))
    provider, _, name = model_id.partition("/")
    sys_prompt = system + "\n\nRespond with a single JSON object only, matching this schema:\n" + schema_hint
    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"],timeout=120,max_retries=1)
        r = client.messages.create(model=name, system=sys_prompt, messages=messages, max_tokens=max_tokens, temperature=temperature)
        return _extract_json("".join(b.text for b in r.content if getattr(b, "type", "") == "text"))
    if provider == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"],timeout=120,max_retries=1)
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
description = {{ (spec.source ~ " " ~ spec.source_ref ~ " [" ~ condition ~ "]") | tojson }}

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
command = {{ collect_command | tojson }}

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
`swesim/templates/collect.py` (COPY to `/opt/swesim/collect.py`, root-owned mode 0755):
```python
# Root-owned, called with -I and a cleared environment; no repository code or git runs here.
import hashlib, json, os, pathlib, signal, subprocess, tarfile, time, urllib.request
OUT=pathlib.Path('/var/swesim-collect'); LIMIT=500*1024*1024
EXCLUDED={'.git','.venv','node_modules','__pycache__','.pytest_cache'}
def main():
    OUT.mkdir(mode=0o700,exist_ok=True)
    for p in OUT.iterdir():
        if p.is_file() or p.is_symlink(): p.unlink()
    receipt={'status':'collection-failure','rc':1,'user':'root','transcript_hash':None,'freeze_status':'n/a'}
    try:
        for _ in range(10):
            p=subprocess.run(['/usr/bin/pkill','-KILL','-u','agent'],capture_output=True)
            if p.returncode==1: break
            if p.returncode!=0: raise RuntimeError('pkill failed')
            time.sleep(.05)
        else: raise RuntimeError('agent processes remain')
        repo=pathlib.Path(os.environ['SWESIM_REPO_DIR'])
        if repo.is_symlink() or not repo.is_dir(): receipt['status']='invalid-tree'; return
        files=[]; total=0
        for top,dirs,names in os.walk(repo,followlinks=False):
            dirs[:]=[d for d in dirs if d not in EXCLUDED]
            for name in names + list(dirs):
                path=pathlib.Path(top)/name; st=path.lstat()
                if not path.is_symlink() and path.is_file(): total+=st.st_size
                files.append(path)
                if total>LIMIT or len(files)>100000: receipt['status']='oversized'; return
        with tarfile.open(OUT/'app.tar','w',dereference=False) as t:
            for p in sorted(files): t.add(p,arcname=p.relative_to(repo).as_posix(),recursive=False)
        h=hashlib.sha256()
        with (OUT/'app.tar').open('rb') as f:
            for b in iter(lambda:f.read(1<<20),b''): h.update(b)
        receipt.update(status='ok',rc=0,bytes=total,files=len(files),sha256=h.hexdigest())
    except Exception as e: receipt['error']=type(e).__name__+': '+str(e)
    finally:
        token=os.environ.get('SWESIM_SESSION_TOKEN','')
        if token:
            receipt['freeze_status']='failed'
            for _ in range(2):
                try:
                    req=urllib.request.Request(os.environ['SWESIM_STAKEHOLDER_URL']+'/freeze',data=b'{}',
                        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                    with urllib.request.urlopen(req,timeout=30) as r: frozen=json.load(r)
                    if frozen.get('frozen') and len(frozen.get('transcript_hash',''))==64:
                        receipt.update(freeze_status='ok',transcript_hash=frozen['transcript_hash']); break
                except Exception: pass
        (OUT/'collect.status').write_text(json.dumps(receipt))
if __name__=='__main__': main()
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
{{ base_stage }}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl bash git ca-certificates procps && rm -rf /var/lib/apt/lists/*
RUN id agent >/dev/null 2>&1 || useradd -m agent
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv python install 3.12 && ln -s "$(uv python find 3.12)" /usr/local/bin/swesim-python
# A new base-only git object database removes remote refs, future objects and reflogs.
RUN cd {{ repo_dir }} && test "$(git rev-parse HEAD)" = {{ spec.environment.base_commit }} \
 && rm -rf .git && git init -q && git config user.name swesim && git config user.email swesim@localhost \
 && git add -A && git -c core.hooksPath=/dev/null commit -qm 'scenario base'
RUN mkdir -p /opt/swesim/bin /var/swesim-collect && chmod 700 /var/swesim-collect
COPY collect.py /opt/swesim/collect.py
COPY shims/ask-user shims/request-review /opt/swesim/bin/
RUN chmod 755 /opt/swesim/bin/* && ln -s /opt/swesim/bin/ask-user /usr/local/bin/ask-user && ln -s /opt/swesim/bin/request-review /usr/local/bin/request-review
RUN chown -R agent:agent {{ repo_dir }} /home/agent
USER agent
ENV HOME=/home/agent
ENV UV_PYTHON_INSTALL_DIR=/home/agent/.local/share/uv/python
RUN uv python install 3.12 && uv tool install --python 3.12 'mini-swe-agent==2.4.6' --with litellm --with orjson --with fastapi
USER root
WORKDIR {{ repo_dir }}
```

`base_stage` for a prebuilt image is `FROM <resolved digest>` followed by
`RUN cd <repo_dir> && git reset --hard <base_commit>`.
For Dockerfile-kind it is **the approved Dockerfile asset's text**, with its
pinned FROM and its checkout/install steps. Preserve installed build products
(such as in-place extension modules and egg metadata); `git clean -ffdx` and
`git archive` are not valid substitutes for this prepared runtime tree. Copy only its declared build
context assets into both build contexts. Both rendered images assert HEAD and
record the same base file manifest. Reject symlinks, submodules, LFS pointer
fixtures and pre-existing agent/tester user collisions during environment
validation for this pilot. Do not silently change a historical Python version.

`swesim/templates/tests.Dockerfile.j2`:
```dockerfile
{{ base_stage }}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN id tester >/dev/null 2>&1 || useradd -m tester
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv python install 3.12 && ln -s "$(uv python find 3.12)" /usr/local/bin/swesim-python
RUN cd {{ repo_dir }} && test "$(git rev-parse HEAD)" = {{ spec.environment.base_commit }} \
 && /usr/local/bin/swesim-python -I -c 'import shutil; shutil.copytree("{{ repo_dir }}", "/pristine", ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"))'
COPY test.patch /tests/test.patch
COPY grader.py policy.py parsers.py run_pytest.py grader_config.json test.sh /tests/
RUN chmod 755 /tests/test.sh && chmod -R a+rX /tests /pristine
```

`swesim/templates/test.sh`:
```bash
#!/bin/bash
mkdir -p /logs/verifier
trap 'if [ ! -f /logs/verifier/reward.json ]; then echo "{\"reward\": 0, \"success\": 0, \"outcome_code\": 7}" > /logs/verifier/reward.json; fi' EXIT
set -o pipefail
/usr/local/bin/swesim-python -I /tests/grader.py --config /tests/grader_config.json 2>&1 | tee /logs/verifier/grader.log
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
from jinja2 import Environment, FileSystemLoader, StrictUndefined
import shlex, hashlib
from urllib.parse import urlparse
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
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), keep_trailing_newline=True, undefined=StrictUndefined)
    sdir = ws.scenario_dir(spec.id); assets = sdir / "assets"
    for asset in spec.assets.values():
        path = (sdir / asset.path).resolve()
        if not path.is_relative_to(sdir.resolve()) or not path.is_file(): raise ValueError("missing/escaping asset")
        data = path.read_bytes()
        if len(data) != asset.bytes or hashlib.sha256(data).hexdigest() != asset.sha256: raise ValueError("asset digest mismatch")
    digest = resolve_docker_hub_digest(spec.environment.image) if (resolve_digest and spec.environment.kind == "prebuilt-image") else None
    base_image = spec.environment.image if spec.environment.kind == "prebuilt-image" else "python:3.12-slim"
    if digest and spec.environment.kind == "prebuilt-image": base_image = f"{spec.environment.image.rsplit(':',1)[0]}@{digest}"
    if resolve_digest and spec.environment.kind == "prebuilt-image" and not digest:
        raise RuntimeError("image digest lookup failed; do not compile mutable tag")
    if spec.environment.kind == "prebuilt-image":
        base_stage = f"FROM {base_image}\nRUN cd {shlex.quote(spec.environment.repo_dir)} && git reset --hard {spec.environment.base_commit}"
    else:
        base_stage = (sdir / spec.assets[spec.environment.dockerfile_asset].path).read_text()
        import re
        match=re.search(r'^FROM\s+[^\s]+@(sha256:[a-f0-9]{64})',base_stage,re.M)
        if not match: raise ValueError('approved Dockerfile must pin its base digest')
        digest=match.group(1)
    host = urlparse(stakeholder_url).hostname if stakeholder_url else None
    if stakeholder_url and (urlparse(stakeholder_url).scheme != "https" or not host): raise ValueError("HTTPS stakeholder URL required")
    hosts = MODEL_HOSTS[run_config.model.split("/")[0]] + ([host] if host else [])
    natural = next(b for b in spec.briefs if b.profile == "natural")
    selected=next((b for b in spec.briefs if b.profile==run_config.brief_profile),natural)
    effective_profile=selected.profile
    collect = ('/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin '
        'SWESIM_REPO_DIR=' + shlex.quote(spec.environment.repo_dir) + ' '
        'SWESIM_STAKEHOLDER_URL=' + shlex.quote(stakeholder_url) + ' '
        'SWESIM_SESSION_TOKEN="$SWESIM_SESSION_TOKEN" '
        '/usr/local/bin/swesim-python -I /opt/swesim/collect.py')
    out: dict[str, Path] = {}
    for condition in ("full", "hidden", "interactive"):
        tdir = ws.task_dir(spec.id, condition); shutil.rmtree(tdir, ignore_errors=True)
        (tdir / "environment" / "shims").mkdir(parents=True); (tdir / "tests").mkdir(); (tdir / "solution").mkdir(); (tdir / "controls").mkdir()
        ctx = dict(spec=spec, run_config=run_config, condition=condition, task_name=f"{spec.id}--{condition}", image_digest=digest,
                   agent_hosts=hosts, stakeholder_url=stakeholder_url, session_token=session_token if condition == "interactive" else "",
                   trial_id=trial_id, collect_command=collect, verifier_timeout=1800.0, repo_dir=spec.environment.repo_dir,
                   base_stage=base_stage, base_image=base_image, install_command=spec.environment.install_notes or "true", agent_python=AGENT_PYTHON,
                   brief=natural.text if condition=="full" else selected.text, visible_facts=visible_facts(spec), solver_minutes=run_config.solver_timeout_sec // 60, python="/usr/bin/python3")
        (tdir / "task.toml").write_text(env.get_template("task.toml.j2").render(**ctx))
        (tdir / "instruction.md").write_text(env.get_template("instruction.md.j2").render(**ctx))
        (tdir / "environment" / "Dockerfile").write_text(env.get_template("environment.Dockerfile.j2").render(**ctx))
        shutil.copyfile(TEMPLATES / "collect.py", tdir / "environment" / "collect.py")
        for asset_name in spec.environment.build_context_assets:
            asset = spec.assets[asset_name]
            for build in (tdir / "environment", tdir / "tests"):
                dst = build / asset.path; dst.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(sdir / asset.path, dst)
        for shim in ("ask-user", "request-review"):
            dst = tdir / "environment" / "shims" / shim; shutil.copyfile(SHIMS / shim, dst); dst.chmod(dst.stat().st_mode | stat.S_IEXEC)
        (tdir / "tests" / "Dockerfile").write_text(env.get_template("tests.Dockerfile.j2").render(**ctx))
        shutil.copyfile(TEMPLATES / "test.sh", tdir / "tests" / "test.sh")
        for f in ("grader.py", "policy.py", "parsers.py", "run_pytest.py"): shutil.copyfile(VERIFIER_SRC / f, tdir / "tests" / f)
        if spec.contract.test_assets != ['test.patch']: raise ValueError('pilot expects one declared test.patch')
        shutil.copyfile(sdir / spec.assets['test.patch'].path,tdir/'tests/test.patch')
        expected = {t: "f2p" for t in spec.contract.fail_to_pass} | {t: "p2p" for t in spec.contract.pass_to_pass}
        grader_cfg = {"artifacts_dir": "/var/swesim-collect", "pristine_dir": "/pristine", "work_dir": "/tmp/swesim-work", "target_dir": spec.environment.repo_dir, "contract_patch": "/tests/test.patch",
                      "expected_tests": expected, "groups": [g.model_dump() for g in spec.groups],
                      "protected": spec.contract.protected_paths + ["**/conftest.py", "**/sitecustomize.py", "**/usercustomize.py", "**/*.pth", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", ".github/**", ".gitattributes", ".gitignore"],
                      "runner": spec.contract.runner.model_dump(), "test_user": "tester", "verifier_out": "/logs/verifier", "test_timeout_sec": 300}
        (tdir / "tests" / "grader_config.json").write_text(json.dumps(grader_cfg, indent=2))
        (tdir / "solution" / "solve.sh").write_text(env.get_template("solve.sh.j2").render(**ctx)); (tdir / "solution" / "solve.sh").chmod(0o755)
        shutil.copyfile(sdir / spec.assets[spec.controls.oracle].path, tdir / "solution" / "oracle.diff")
        for c in spec.controls.plausible_wrong + spec.controls.alternative_correct + spec.controls.tamper:
            shutil.copyfile(sdir / spec.assets[c.asset].path, tdir / "controls" / f"{c.name}.diff")
        out[condition] = tdir
    cj = sdir / "compile.json"
    contract_inputs={"spec_hash":spec.spec_hash,"image_digest":digest,
        "templates":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in TEMPLATES.iterdir() if p.is_file()},
        "verifier":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in VERIFIER_SRC.glob("*.py")}}
    compiled_hash=hashlib.sha256(json.dumps(contract_inputs,sort_keys=True).encode()).hexdigest()
    cj.write_text(json.dumps({"compiled_hash":compiled_hash,"spec_hash": spec.spec_hash, "run_config_hash": run_config.run_config_hash, "image_digest": digest,
                              "brief_profile":effective_profile, "task_dirs": {k: str(v) for k, v in out.items()}}, indent=2))
    return CompileResult(out, digest, cj)
```

The final shims are copied from Task 8's code now; add them in Task 4 so a
compiled task never contains a placeholder. In full/hidden they give the
explicit unavailability response because the session token is empty.

- [ ] **Step 6: Run tests, commit**

Run: `uv run pytest tests/test_compile.py -q` → all listed tests pass.
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

- [ ] **Step 1: Confirm the pinned API with this offline command**

```bash
uv run python -c "import inspect; from harbor.agents.base import BaseAgent; from harbor.environments.base import BaseEnvironment; print(inspect.signature(BaseAgent)); print(inspect.signature(BaseEnvironment.upload_file)); print(BaseAgent.__abstractmethods__)"
```
The implementation below uses `version()`, `setup(environment)`,
`run(instruction, environment, context)`, `upload_file(source_path,target_path)`
and `ExecResult.return_code`, all verified in Harbor 0.23.

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

def build_probe_command(repo_dir: str, gold_lines: list[str], gold_sha: str = "") -> str:
    gold_probe = f"git cat-file -e {shlex.quote(gold_sha)} 2>/dev/null && echo GOLD_OBJECT_REACHABLE || true" if gold_sha else "echo GOLD_OBJECT_UNVERIFIED"
    greps = " ; ".join(f"grep -rF --exclude-dir=.git -l -- {shlex.quote(l)} . | head -5" for l in gold_lines)
    return (f"cd {shlex.quote(repo_dir)} && echo REFS=$(git rev-list --all | wc -l) && echo REFLOG=$(git reflog | wc -l) "
            f"&& echo HITS_BEGIN && ({gold_probe}; {greps}) ; echo HITS_END")

class PatchAgent(BaseAgent):
    """Applies a named control patch (validate stage) or runs the history/gold-line reachability probe."""
    def __init__(self, logs_dir: Path, model_name: str | None = None, patch: str | None = None, probe: bool = False, gold_lines: str = "[]", gold_sha: str = "", **kw):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kw)
        self._patch = patch; self._probe = probe in (True, "true", "1"); self._gold_lines = json.loads(gold_lines) if isinstance(gold_lines,str) else list(gold_lines); self._gold_sha=gold_sha
    @staticmethod
    def name() -> str: return "patch-agent"
    def version(self) -> str: return "1.0.0"
    async def setup(self, environment) -> None: return None
    async def run(self, instruction: str, environment, context: AgentContext) -> None:
        r = await environment.exec('printf "%s" "$SWESIM_REPO_DIR"', user="agent")
        repo_dir = r.stdout.strip()
        if not repo_dir.startswith("/"): raise ValueError("missing repo_dir")
        if self._probe:
            r = await environment.exec(f"bash -lc {shlex.quote(build_probe_command(repo_dir, self._gold_lines, self._gold_sha))}")
            (self.logs_dir / "probe.txt").write_text((r.stdout or "") + "\n" + (r.stderr or "")); return
        assert self._patch, "patch kwarg required"
        await environment.upload_file(source_path=Path(self._patch), target_path="/tmp/control.diff")
        r = await environment.exec(f"bash -lc {shlex.quote(build_apply_command(repo_dir, '/tmp/control.diff'))}")
        (self.logs_dir / "apply.txt").write_text(f"rc={r.return_code}\n{r.stdout}\n{r.stderr}")
        if r.return_code != 0: raise RuntimeError(f"control patch failed to apply: {r.stderr[-500:]}")
```

- [ ] **Step 4: Run local adapter tests; the real patch application is checked by Task 7 controls**

```bash
uv run pytest tests/test_patch_agent.py -q
uv run python -c "from pathlib import Path; from swesim.agents.patch_agent import PatchAgent; assert PatchAgent(Path('/tmp')).version() == '1.0.0'"
```

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
- Produces: `swebench_annotated.pool_a(csv_path) -> list[str]` instance ids; `swebench_annotated.candidates(instance_ids, out_path, pytest_repos_only=True) -> int` writes `candidates.jsonl` rows `{instance_id, repo, base_commit, version, problem_statement, hints_text, fail_to_pass, pass_to_pass, patch, test_patch, image, annotation: {underspecified, underspecified_notes, false_negative, false_negative_notes, difficulty}, thread: {issue_url, issue_comments, pr_url, pr_comments, review_comments}}`; `threads.fetch_pr_thread(repo, pr_number) -> dict` and `threads.fetch_issue_thread(repo, issue_number) -> dict` via `gh api`; `judge.issue_contract_delta(row) -> list[dict]` (code-extracted candidate gaps from test exceptions, asserted literals and new public names absent from the issue); `judge.judge_candidate(row, model_id) -> dict` with keys `gaps` (per-gap `derivable_from_repo`, `consequential`, `resolved_by`, `category`), `verified_rubric_score` (0-3, annotator rubric), `complexity, test_behavioralness, self_contained, alternatives_accepted, keep` where `keep` is the code-owned rule `accept()`; `judge.calibrate(csv_path, rows_by_id, model_id, n) -> dict` (agreement with the human labels at the >=2 threshold, written by `swesim judge-calibrate` to `workspace/judge_calibration.json` and reported in docs/grading.md); `draft_spec.draft(row, judged, model_id, out_dir) -> Path` writing `scenario.draft.json` plus `assets/` (oracle.diff, test.patch, control diffs) with sha256 manifest; `tamper.root_conftest_noop(repo_test_paths) -> str`, `tamper.skip_marker(test_patch_text) -> str`, `tamper.sitecustomize_junit_forge() -> str` (code-generated diffs).
- Image name rule: `swebench/sweb.eval.x86_64.` + instance_id with `__` replaced by `_1776_`, tag `latest`. Repo dir in SWE-bench images: `/testbed`. Runner: `pytest-ra` with command `/opt/miniconda3/envs/testbed/bin/python -I /tests/run_pytest.py -rA -p no:cacheprovider --junitxml={junit_path} <test files from test_patch>`; `single_command` same with `{nodeid}`.
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
def test_withheld_brief_has_no_excerpts(monkeypatch):
    from swesim.stages import draft_spec
    issue='Import fails\nTraceback: ERROR42\nrun_import("bad.csv")'
    monkeypatch.setattr(draft_spec,'chat_json',lambda *a,**k:{'withheld_brief':'Import fails',
        'removed':[{'category':'error_message','excerpt':'ERROR42','kind':'reproduction','discovery':'on-request'},
                   {'category':'reproduction','excerpt':'run_import("bad.csv")','kind':'reproduction','discovery':'on-request'}]})
    result=draft_spec.withhold(issue,'fake/model')
    assert all(x['excerpt'] not in result['withheld_brief'] for x in result['removed'])

def test_issue_contract_delta_finds_unmentioned_requirements():
    from swesim.stages.judge import issue_contract_delta, accept
    row = {"problem_statement": "weighted mean should raise when the dim does not exist",
           "test_patch": "diff --git a/t.py b/t.py\n+    with pytest.raises(ValueError, match=\"dimension 'z' not found\"):\n+        w.mean('z')\n",
           "patch": "diff --git a/w.py b/w.py\n+def _check_dim(d):\n+    pass\n+class WeightedError(Exception):\n+    pass\n"}
    gaps = issue_contract_delta(row); srcs = {g["source"] for g in gaps}
    assert {"test-exception", "test-literal", "patch-public-name"} <= srcs and all("_check_dim" not in g["text"] for g in gaps)
    judged = {"gaps": [{"derivable_from_repo": False, "consequential": True, "resolved_by": "thread"}], "self_contained": True}
    assert accept(judged) and not accept({"gaps": [{"derivable_from_repo": True, "consequential": True, "resolved_by": "thread"}], "self_contained": True})
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
    d = tamper.skip_marker({"tests/test_a.py":"def test_old():\n    assert True\n"})
    assert "pytestmark=pytest.mark.skip" in d and "a/tests/test_a.py" in d
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
import json, re, subprocess, base64
from urllib.parse import urlencode

def _gh(path: str):
    # A single response is one JSON document; do not json.loads concatenated --paginate pages.
    return json.loads(subprocess.run(["gh","api",path],capture_output=True,text=True,check=True,timeout=60).stdout)
def pages(path: str, max_pages: int = 10):
    out=[]
    for page in range(1,max_pages+1):
        rows=_gh(path+('&' if '?' in path else '?')+urlencode({'per_page':100,'page':page}))
        if not isinstance(rows,list): raise ValueError("expected page array")
        out.extend(rows)
        if len(rows)<100: return out
    raise ValueError("thread pagination exceeded bound; do not silently truncate")
def base_text(repo, commit, path):
    try: r=_gh(f"repos/{repo}/contents/{path}?ref={commit}")
    except subprocess.CalledProcessError as e:
        if "404" in e.stderr: return None
        raise
    return base64.b64decode(r["content"]).decode()
def _strip(c: dict) -> dict:
    return {"user": c["user"]["login"], "created_at": c.get("created_at", c.get("submitted_at")), "body": c.get("body") or "", "url": c.get("html_url"), "path": c.get("path")}
def fetch_issue_thread(repo: str, number: int) -> dict:
    issue = _gh(f"repos/{repo}/issues/{number}"); comments = pages(f"repos/{repo}/issues/{number}/comments?per_page=100")
    return {"issue_url": issue["html_url"], "title": issue["title"], "body": issue.get("body") or "", "user": issue["user"]["login"],
            "created_at": issue["created_at"], "comments": [_strip(c) for c in comments if not c["user"]["login"].endswith("[bot]")]}
def linked_numbers(repo,pr,body):
    found={int(n) for n in re.findall(r"(?:[Ff]ix(?:es|ed)?|[Cc]los(?:es|ed)?|[Rr]esolves?)\s*:?\s*#(\d+)",body)}
    if not found:
        for event in pages(f"repos/{repo}/issues/{pr}/timeline",max_pages=3):
            issue=event.get('source',{}).get('issue',{})
            if event.get('event')=='cross-referenced' and issue and not issue.get('pull_request'):
                if issue.get('html_url','').startswith('https://github.com/'+repo+'/issues/'):
                    found.add(issue['number'])
    return sorted(found)

def fetch_pr_thread(repo: str, pr: int) -> dict:
    p = _gh(f"repos/{repo}/pulls/{pr}")
    body = p.get("body") or ""
    linked = linked_numbers(repo,pr,body)
    out = {"pr_url": p["html_url"], "pr_title": p["title"], "pr_body": body, "author": p["user"]["login"], "merged_at": p.get("merged_at"),
           "gold_commit":p.get("merge_commit_sha"), "pr_created_at": p["created_at"], "reviews": [_strip(c) for c in pages(f"repos/{repo}/pulls/{pr}/reviews")],
           "pr_comments": [_strip(c) for c in pages(f"repos/{repo}/issues/{pr}/comments?per_page=100") if not c["user"]["login"].endswith("[bot]")],
           "review_comments": [_strip(c) for c in pages(f"repos/{repo}/pulls/{pr}/comments?per_page=100")], "linked_issues": []}
    for n in linked[:3]:
        try: out["linked_issues"].append(fetch_issue_thread(repo, n))
        except subprocess.CalledProcessError as e: out.setdefault("fetch_errors", []).append({"issue":n,"error":str(e)})
    return out
```

- [ ] **Step 3: Implement tamper.py (code-generated exploit controls)**

```python
# swesim/tamper.py
import difflib

def file_diff(path, before, after):
    old=before.splitlines(keepends=True) if before is not None else []
    if old and not old[-1].endswith("\n"): raise ValueError("control requires final-newline source")
    header=f"diff --git a/{path} b/{path}\n"
    if before is None: header+="new file mode 100644\n"
    return header+''.join(difflib.unified_diff(old,after.splitlines(keepends=True),
        fromfile='a/'+path if before is not None else '/dev/null',tofile='b/'+path))

def root_conftest_noop(before=None):
    return file_diff('conftest.py',before,(before or '')+"\nimport pytest\n@pytest.hookimpl(tryfirst=True)\ndef pytest_runtest_call(item):\n    item.runtest=lambda:None\n")
def sitecustomize_junit_forge(before=None):
    # Startup execution probe; do not claim this closes in-process JUnit forgery.
    return file_diff('sitecustomize.py',before,(before or '')+"\nprint('SWESIM_STARTUP_HOOK_EXECUTED')\n")
def skip_marker(base_files):
    return ''.join(file_diff(path,text,text+"\nimport pytest\npytestmark=pytest.mark.skip(reason='control')\n")
        for path,text in base_files.items() if text is not None)
def all_controls(base_files):
    return {'tamper-root-conftest':root_conftest_noop(base_files.get('conftest.py')),
        'tamper-sitecustomize':sitecustomize_junit_forge(base_files.get('sitecustomize.py')),
        'tamper-skip-marker':skip_marker({p:t for p,t in base_files.items() if p not in {'conftest.py','sitecustomize.py'}})}
```

- [ ] **Step 4: Implement judge.py and draft_spec.py**

```python
# swesim/stages/judge.py
from __future__ import annotations
import csv, json, random, re
from pathlib import Path
from swesim.llm import chat_json
JUDGE_PROMPT_VERSION = "judge-v2"
SYSTEM = """You review a GitHub issue, its discussion, the merged fix, and the fix's tests to decide whether it makes a good
interactive software-engineering task in which a simulated maintainer can answer an agent's questions.
You are given CANDIDATE GAPS extracted by code: things the tests or the fix require that the issue text never mentions.
Classify each gap; never judge the issue as a whole. Also score the issue's underspecification 0-3 with the SWE-bench
Verified annotator rubric (0: well specified; 1: minor blanks a reasonable solver fills; 2: multiple valid solutions,
the tests accept one; 3: nearly impossible without more information). Be strict and concrete."""
SCHEMA = """{"gaps": [{"id": "g1", "text": "...", "source": "test-literal|test-exception|patch-public-name|diff-branch|thread",
  "derivable_from_repo": true|false, "consequential": true|false, "resolved_by": "thread|patch|none",
  "category": "behavior|interface|constraint|reproduction|context"}],
"verified_rubric_score": 0|1|2|3, "complexity": 1-5, "test_behavioralness": 1-5, "self_contained": true|false,
"alternatives_accepted": true|false, "rationale": "2-4 sentences"}"""

_EXC = re.compile(r"pytest\.raises\(\s*([A-Za-z_][\w.]*)")
_LIT = re.compile(r"(?:assert[^\n]*?|match=)\s*[\"']([^\"'\n]{6,80})[\"']")
_DEF = re.compile(r"^\+\s*(?:def|class)\s+([A-Za-z_]\w*)", re.M)

def issue_contract_delta(row: dict) -> list[dict]:
    """Code-extracted candidate gaps: items the tests or the fix require that the issue never mentions."""
    issue = (row.get("problem_statement") or "").lower(); gaps = []
    for name in sorted(set(_EXC.findall(row.get("test_patch", "")))):
        if name.lower() not in issue: gaps.append({"source": "test-exception", "text": f"tests expect {name}"})
    for lit in sorted(set(_LIT.findall(row.get("test_patch", "")))):
        if lit.lower() not in issue: gaps.append({"source": "test-literal", "text": f"tests assert the literal {lit!r}"})
    for name in sorted(set(_DEF.findall(row.get("patch", "")))):
        if not name.startswith("_") and name.lower() not in issue: gaps.append({"source": "patch-public-name", "text": f"the fix introduces public name {name}"})
    return gaps[:25]

def accept(judged: dict) -> bool:
    """Code-owned acceptance rule: at least one gap that is non-derivable, consequential and resolved by a stakeholder statement."""
    return any(not g.get("derivable_from_repo") and g.get("consequential") and g.get("resolved_by") == "thread" for g in judged.get("gaps", [])) \
        and bool(judged.get("self_contained"))

def judge_candidate(row: dict, model_id: str) -> dict:
    thread = row.get("thread", {})
    if thread.get("fetch_errors"): raise ValueError("incomplete source evidence")
    payload = {"candidate": row, "candidate_gaps": issue_contract_delta(row)}
    user = json.dumps(payload, ensure_ascii=False)
    if len(user) > 120000: raise ValueError("candidate exceeds authoring context budget")
    out = chat_json(model_id, SYSTEM, [{"role": "user", "content": user}], SCHEMA)
    out["candidate_gaps"] = payload["candidate_gaps"]; out["keep"] = accept(out)
    out["structural_tests"] = not bool(out.get("alternatives_accepted"))
    out["prompt_version"] = JUDGE_PROMPT_VERSION; out["model_id"] = model_id; return out

def calibrate(csv_path: Path, rows_by_id: dict[str, dict], model_id: str, n: int, seed: int = 0) -> dict:
    """Agreement of the judge's verified_rubric_score with the human ensembled labels at the >=2 threshold."""
    with open(csv_path, newline="") as f: ann = {r["instance_id"]: r for r in csv.DictReader(f)}
    ids = [i for i in ann if i in rows_by_id]; random.Random(seed).shuffle(ids); ids = ids[:n]
    tp = fp = tn = fn = 0; per = []
    for iid in ids:
        human = float(ann[iid]["underspecified"]) >= 2
        judged = judge_candidate(rows_by_id[iid], model_id); model = int(judged.get("verified_rubric_score", 0)) >= 2
        per.append({"instance_id": iid, "human": float(ann[iid]["underspecified"]), "model": judged.get("verified_rubric_score")})
        tp += human and model; fp += (not human) and model; tn += (not human) and (not model); fn += human and (not model)
    total = max(1, tp + fp + tn + fn)
    return {"n": len(ids), "agreement": (tp + tn) / total, "precision": tp / max(1, tp + fp), "recall": tp / max(1, tp + fn),
            "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn}, "model_id": model_id, "prompt_version": JUDGE_PROMPT_VERSION, "per_instance": per}
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
"ledger": [{"id": "f01", "text": "...", "kind": "behavior|interface|constraint|reproduction|context",
  "provenance": "issue-thread|pr-thread|pr-review|annotator-note|authored", "source_excerpt": "...", "source_url": "...",
  "authored_rationale": null, "discovery": "brief|on-request|volunteered-at-review|repo-derivable|author-only", "tests": ["nodeid"]}],
"operations": [{"type": "clarification|evaluator-repair|synthetic-extension", "target": "...", "rationale": "..."}],
"persona": {"summary": "...", "tone": "...", "disclosure_style": "...", "derived_from_urls": ["..."], "role": "maintainer|reporter"},
"requirements": [{"id": "r01", "text": "...", "fact_id": "f01", "tests": ["nodeid"], "source_url": "..."}],
"checks": [{"id": "c01", "description": "...", "command": "python -c '...'", "expected_observation": "..."}],
"groups": [{"id": "g01", "weight": 0.5, "tests": ["nodeid"]}],
"plausible_wrong": {"name": "...", "rationale": "...", "diff": "unified diff text"},
"protected_paths_extra": ["paths the fix's tests depend on"]}"""

def withhold(natural_issue,model_id):
    categories={'reproduction','error_message','expected_output','location','version','proposed_fix'}
    schema='{"withheld_brief":"text","removed":[{"category":"reproduction|error_message|expected_output|location|version|proposed_fix","excerpt":"verbatim contiguous span","kind":"reproduction|context|behavior|interface|constraint","discovery":"on-request|repo-derivable"}]}'
    prompt="Remove selected spans by category; preserve the first line exactly and a recognizable symptom and desired outcome. Do not add requirements. Record EVERY removal verbatim. No free summary. File locations and explicit fix proposals are repo-derivable."
    messages=[{'role':'user','content':natural_issue}]
    for attempt in range(2):
        out=chat_json(model_id,prompt,messages,schema,max_tokens=5000)
        bad=[]; text=out.get('withheld_brief','')
        if not text or natural_issue.splitlines()[0].strip() not in text: bad.append('missing title')
        if not out.get('removed'): bad.append('no removed spans')
        for item in out.get('removed',[]):
            span=item.get('excerpt','')
            if item.get('category') not in categories or not span or span not in natural_issue: bad.append('invalid removed span')
            elif span.casefold() in text.casefold(): bad.append(span)
        if not bad:return out
        messages.append({'role':'assistant','content':json.dumps(out)})
        messages.append({'role':'user','content':'Correct these violations: '+json.dumps(bad)})
    raise ValueError('withholding failed validation after two attempts')

def _sha(b: bytes) -> str: return hashlib.sha256(b).hexdigest()

def draft(row: dict, judged: dict, model_id: str, out_dir: Path, pipeline_version: str = "0.1.0") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True); assets = out_dir / "assets"; assets.mkdir(exist_ok=True)
    user=json.dumps({"candidate":row,"judge":judged},ensure_ascii=False)
    if len(user)>120000: raise ValueError("candidate exceeds authoring context budget")
    d = chat_json(model_id, SYSTEM, [{"role": "user", "content": user}], SCHEMA, max_tokens=8000)
    files = {"oracle.diff": row["patch"].encode(), "test.patch": row["test_patch"].encode(), "plausible-wrong.diff": d["plausible_wrong"]["diff"].encode()}
    test_files = test_files_from_patch(row["test_patch"])
    from swesim.sources.threads import base_text
    base_files={p:base_text(row["repo"],row["base_commit"],p) for p in [*test_files,"conftest.py","sitecustomize.py"]}
    control_diffs=tamper.all_controls(base_files)
    for name,diff in control_diffs.items():
        if not diff: raise ValueError("empty tamper control")
        files[f"{name}.diff"]=diff.encode()
    manifest = {}
    for name, data in files.items():
        (assets / name).write_bytes(data); manifest[name] = {"path": f"assets/{name}", "sha256": _sha(data), "bytes": len(data)}
    import shlex
    selected=[row.get("nodeid_map",{}).get(t,t) for t in row["fail_to_pass"]+row["pass_to_pass"]]
    interpreter=row.get("test_python","/opt/miniconda3/envs/testbed/bin/python")
    prefix=shlex.quote(interpreter)+" -I /tests/run_pytest.py -rA -p no:cacheprovider --junitxml={junit_path} "
    runner_cmd=prefix+" ".join(shlex.quote(t) for t in selected)
    spec = {"id": row["instance_id"].lower().replace("__", "--"), "version": 1, "source": row.get("source","swebench-annotated"), "source_ref": row["instance_id"],
            "created_at": datetime.now(timezone.utc).isoformat(), "pipeline_version": pipeline_version,
            "environment": {"kind": "prebuilt-image", "image": row["image"], "repo_url": f"https://github.com/{row['repo']}", "base_commit": row["base_commit"],
                            "language": "python", "repo_dir": row.get("repo_dir", "/testbed"), "test_command": runner_cmd, "gold_commit":row.get("thread",{}).get("gold_commit")},
            "assets": manifest, "briefs": [{"profile":"natural","text":row["problem_statement"],"provenance":"original-issue"}],
            "ledger": d["ledger"], "operations": d.get("operations", []), "persona": d["persona"], "requirements": d.get("requirements", []),
            "contract": {"fail_to_pass": row["fail_to_pass"], "pass_to_pass": row["pass_to_pass"], "test_assets": ["test.patch"],
                         "protected_paths": sorted(set(test_files) | set(d.get("protected_paths_extra", []))),
                         "runner": {"kind": "pytest-ra", "command": runner_cmd, "single_command": prefix+"{nodeid}", "test_files":test_files, "nodeid_map":row.get("nodeid_map",{})}},
            "checks": d.get("checks", []),
            "controls": {"oracle": "oracle.diff",
                         "plausible_wrong": [{"name": d["plausible_wrong"]["name"], "asset": "plausible-wrong.diff", "rationale": d["plausible_wrong"]["rationale"], "expected_success": 0, "expected_reward_max": 0.5, "cls": "plausible-wrong"}],
                         "alternative_correct": [],
                         "tamper": [{"name": n, "asset": f"{n}.diff", "rationale": "code-generated exploit", "expected_reward": 0.0, "cls": "incorrect-exploit"} for n in control_diffs]},
            "groups": d["groups"], "difficulty": {"annotated": row["annotation"]["difficulty"], "judged": judged},
            "thread": {k: v for k, v in row.get("thread", {}).items()}}
    if row.get("source")=="github-repo":
        recipe=row["dockerfile"]; data=recipe.encode(); (assets/"Dockerfile.base").write_bytes(data)
        spec["assets"]["Dockerfile.base"]={"path":"assets/Dockerfile.base","sha256":_sha(data),"bytes":len(data)}
        spec["environment"].update(kind="dockerfile",image=None,dockerfile_asset="Dockerfile.base")
    withheld=withhold(row['problem_statement'],model_id)
    spec['briefs'].append({'profile':'withheld','text':withheld['withheld_brief'],'provenance':'withheld'})
    issue_url=next((i['issue_url'] for i in row.get('thread',{}).get('linked_issues',[])),row.get('thread',{}).get('pr_url'))
    if not issue_url: raise ValueError('withheld facts need source URL')
    for i,item in enumerate(withheld['removed']):
        fid=f'withheld-{i+1:03d}'
        if any(f['id']==fid for f in spec['ledger']): raise ValueError('reserved withholding fact id')
        discovery='repo-derivable' if item['category'] in {'location','proposed_fix'} else item['discovery']
        spec['ledger'].append({'id':fid,'text':item['excerpt'],'kind':item['kind'],'provenance':'original-issue',
            'source_excerpt':item['excerpt'],'source_url':issue_url,'discovery':discovery,'tests':[]})
        spec['operations'].append({'type':'withholding','target':fid,'rationale':'Removed category: '+item['category']})
    from swesim.models import ScenarioSpec
    parsed=ScenarioSpec.model_validate(spec)
    p=out_dir/"scenario.draft.json"; p.write_text(parsed.model_dump_json(indent=2)); return p
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

Run: `uv run pytest tests/test_swebench_source.py tests/test_tamper.py -q` → all listed tests pass.
Run: `uv run python -c "from swesim.paths import Workspace; from swesim.stages.mine import mine_swebench; print(mine_swebench(Workspace('workspace'), instance_ids=['pydata__xarray-5362','psf__requests-1963']))"` then inspect `workspace/candidates/swebench-annotated.jsonl` (two rows, threads present).
```bash
git add swesim/sources swesim/stages/mine.py swesim/stages/judge.py swesim/stages/draft_spec.py swesim/tamper.py tests/test_swebench_source.py tests/test_tamper.py
git commit -m "feat(sources): swebench-annotated pool A mining with threads, judge rubric, spec drafting, code-generated tamper controls"
```

---

### Task 7: validate stage (controls on Modal) and approve/compile CLI wiring

**Files:**
- Create: `swesim/stages/validate.py`, `swesim/harbor_cli.py`
- Create: `swesim/stages/approve.py`, `tests/test_approve.py`
- Modify: `swesim/cli.py` (replace stubs for `mine`, `judge`, `draft`, `approve`, `compile`, `validate`)
- Create: `tests/test_validate.py`

**Interfaces:**
- Produces: `harbor_cli.run_task(task_dir: Path, agent: str, jobs_dir: Path, job_name: str, model: str | None = None, agent_kwargs: dict | None = None, env_provider: str = "modal", timeout_sec: int = 10800) -> Path` (returns the trial directory; returns agent-error results for attribution; raises `HarborRunError` for missing/ambiguous launch results); `harbor_cli.read_reward(trial_dir) -> dict`, `harbor_cli.read_report(trial_dir) -> dict`; `validate.validate_scenario(ws, spec, run_config, stakeholder_url) -> dict` writing `workspace/scenarios/<id>/validation.json` with `{status: "valid"|"invalid", failures: [...], expected_base: {...}, expected_gold: {...}, controls: {name: {reward, success, outcome, expected}}, probe: {...}, trial_dirs: {...}}`; approval flow: `swesim approve <id>` validates `scenario.draft.json` (edited by the human) and writes `scenario.json`.
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
             env_provider: str = "modal", timeout_sec: int = 10800) -> Path:
    cmd = ["harbor", "run", "--max-retries", "0", "-p", str(task_dir), "--agent", agent, "-e", env_provider, "--jobs-dir", str(jobs_dir), "--job-name", job_name, "--yes"]
    if model: cmd += ["-m", model]
    for k, v in (agent_kwargs or {}).items(): cmd += ["--ak", f"{k}={v}"]
    env = {k:v for k,v in os.environ.items() if not any(x in k for x in ('GH_TOKEN','GITHUB_TOKEN','SWESIM_WORKER_SECRET'))}
    env.update(PYTHONPATH=str(Path(__file__).resolve().parent.parent) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    if (jobs_dir/job_name).exists():
        matches=list((jobs_dir/job_name).glob("*/result.json"))
        if len(matches)==1: return matches[0].parent
        raise HarborRunError("job exists without a unique terminal result; reconcile, do not relaunch")
    try: proc = subprocess.run(cmd,capture_output=True,text=True,timeout=timeout_sec,env=env)
    except subprocess.TimeoutExpired as e: raise HarborRunError("Harbor wrapper deadline; inspect remote/job state before retry") from e
    trials = [p.parent for p in (jobs_dir/job_name).glob("*/result.json")]
    if len(trials)>1: raise HarborRunError("ambiguous job; never pick a random last trial")
    if not trials: raise HarborRunError(f"no trial dir; harbor stdout tail: {proc.stdout[-1500:]} stderr: {proc.stderr[-1500:]}")
    result = json.loads((trials[-1] / "result.json").read_text())
    # Return agent-error trials too; attribution and denominators need the recorded result.
    if proc.returncode and not result: raise HarborRunError("Harbor failed without result")
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
    required={"oracle","nop"} | {f"plausible-wrong:{c['name']}" for c in controls['plausible_wrong']} | {f"alt-correct:{c['name']}" for c in controls['alternative_correct']} | {f"tamper:{c['name']}" for c in controls['tamper']}
    if not required <= results.keys(): return False,["missing control results"]
    for name in required:
        if results[name].get('outcome_code') not in {1,2,3,4}: failures.append(name+": infrastructure is not a passing control")
        if not isinstance(results[name].get('reward'),(int,float)): failures.append(name+": missing numeric reward")
    if results["oracle"].get("success") != 1 or results["oracle"].get("reward") != 1: failures.append("oracle: not fully correct")
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
    task = comp.task_dirs["full"]
    compiled_hash=json.loads(comp.compile_json.read_text())['compiled_hash']
    jobs=ws.jobs_dir/'validate'/spec.id/compiled_hash[:16]; results={}; trial_dirs={}; run_failures=[]
    def run(name: str, agent: str, kwargs: dict | None = None):
        td = harbor_cli.run_task(task, agent, jobs, f"{spec.id}--{name.replace(':','-')}", agent_kwargs=kwargs)
        trial_dirs[name] = str(td); results[name] = harbor_cli.read_reward(td); results[name]["report"] = harbor_cli.read_report(td)
        if json.loads((td/"result.json").read_text()).get("exception_info"): run_failures.append(name+": control agent failed before a clean submission")
    run("nop", "nop"); run("oracle", "oracle")
    for c in spec.controls.plausible_wrong: run(f"plausible-wrong:{c.name}", PATCH_AGENT, {"patch": str((task / "controls" / f"{c.name}.diff").resolve())})
    for c in spec.controls.alternative_correct: run(f"alt-correct:{c.name}", PATCH_AGENT, {"patch": str((task / "controls" / f"{c.name}.diff").resolve())})
    for c in spec.controls.tamper: run(f"tamper:{c.name}", PATCH_AGENT, {"patch": str((task / "controls" / f"{c.name}.diff").resolve())})
    probe_dir = harbor_cli.run_task(task, PATCH_AGENT, jobs, f"{spec.id}--probe", agent_kwargs={"probe": "true", "gold_sha":spec.environment.gold_commit or "", "gold_lines": json.dumps(gold_lines((task / "solution" / "oracle.diff").read_text()))})
    probe_txt = (probe_dir / "agent" / "probe.txt").read_text() if (probe_dir / "agent" / "probe.txt").exists() else ""
    hits = re.search(r"HITS_BEGIN\n(.*?)HITS_END", probe_txt, re.S); probe = {"raw": probe_txt[-2000:], "gold_hits": (hits.group(1).strip().splitlines() if hits else [])}
    ok, failures = evaluate_controls(results, spec.controls.model_dump())
    if run_failures: failures.extend(run_failures); ok=False
    base = {t["id"]: t["observed"] for t in results["nop"]["report"].get("per_test", [])}
    gold = {t["id"]: t["observed"] for t in results["oracle"]["report"].get("per_test", [])}
    wanted=set(spec.contract.fail_to_pass)|set(spec.contract.pass_to_pass)
    if set(base)!=wanted or any(base.get(t) not in {"failed","error"} for t in spec.contract.fail_to_pass) or any(base.get(t)!="passed" for t in spec.contract.pass_to_pass):
        failures.append("base must execute every target as failed/error and every regression as passed"); ok=False
    if set(gold)!=wanted or any(gold[t]!="passed" for t in wanted): failures.append("gold exact-id manifest failed"); ok=False
    if not probe_txt or not hits: failures.append("missing reachability evidence"); ok=False
    if probe["gold_hits"]: failures.append(f"gold patch lines reachable in agent image: {probe['gold_hits'][:3]}"); ok = False
    out = {"status": "valid" if ok else "invalid", "failures": failures, "expected_base": base, "expected_gold": gold,
           "controls": {k: {kk: vv for kk, vv in v.items() if kk != "report"} for k, v in results.items()}, "probe": probe,
           "trial_dirs": trial_dirs, "spec_hash": spec.spec_hash, "source":spec.source.value, "source_ref":spec.source_ref, "environment_provider":"modal", "image_digest": comp.image_digest, "compiled_hash":json.loads(comp.compile_json.read_text())["compiled_hash"]}
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
from dotenv import load_dotenv
import os
load_dotenv(os.environ.get('SWESIM_ENV_FILE',str(Path.home()/'Documents/.env')))

@app.command()
def mine(source: str = "swebench-annotated", pool: str = "A", instance: list[str] = typer.Option(None), repo: str = "", pr: int = 0, no_threads: bool = False, kind: str = "feature", since: str = "2026-06-01", max_prs: int = 400, max_builds: int = 3):
    if source == "swebench-annotated":
        from swesim.stages.mine import mine_swebench; typer.echo(mine_swebench(WS, pool, instance or None, fetch_threads=not no_threads))
    elif source == "github-repo":
        from swesim.stages.mine import mine_github_repo; typer.echo(mine_github_repo(WS,repo,pr or None,since=since,max_prs=max_prs,max_builds=max_builds,kind=kind))
    else: raise typer.BadParameter(source)

@app.command()
def judge(candidates: Path, model: str = "openai/gpt-5.5", limit: int = 20):
    """Judge model is deliberately different from the draft model so their errors are not correlated (spec section 7)."""
    from swesim.stages.judge import judge_candidate
    out = candidates.with_suffix(".judged.jsonl"); n = 0
    with open(candidates) as f, open(out, "w") as g:
        for line in f:
            if n >= limit: break
            row = json.loads(line); row["judge"] = judge_candidate(row, model); g.write(json.dumps(row) + "\n"); n += 1
    typer.echo(out)

@app.command(name="judge-calibrate")
def judge_calibrate(n: int = 150, model: str = "openai/gpt-5.5", seed: int = 0):
    """Measure the judge's agreement with the 1,699 human annotations before using it on new repositories."""
    from datasets import load_dataset
    from swesim.stages.judge import calibrate
    from swesim.sources.swebench_annotated import image_for
    ds = load_dataset("princeton-nlp/SWE-bench", split="test")
    rows = {r["instance_id"]: {"instance_id": r["instance_id"], "repo": r["repo"], "problem_statement": r["problem_statement"], "hints_text": r["hints_text"],
            "patch": r["patch"], "test_patch": r["test_patch"], "fail_to_pass": json.loads(r["FAIL_TO_PASS"]), "pass_to_pass": json.loads(r["PASS_TO_PASS"]),
            "image": image_for(r["instance_id"]), "annotation": {}, "thread": {}} for r in ds}
    out = calibrate(Path("data/annotations/ensembled_annotations_public.csv"), rows, model, n, seed)
    Path("workspace").mkdir(exist_ok=True); Path("workspace/judge_calibration.json").write_text(json.dumps(out, indent=2))
    typer.echo(json.dumps({k: v for k, v in out.items() if k != "per_instance"}, indent=2))

@app.command()
def draft(judged: Path, instance: str, model: str = "anthropic/claude-sonnet-5"):
    from swesim.stages.draft_spec import draft as _draft
    row = next(json.loads(l) for l in open(judged) if json.loads(l)["instance_id"] == instance)
    sid = instance.lower().replace("__", "--"); typer.echo(_draft(row, row["judge"], model, WS.scenario_dir(sid)))

@app.command()
def approve(scenario_id: str, auto: bool = typer.Option(False, "--auto", help="Unattended mode: code-owned gates approve instead of a human; recorded as approved_by=auto-validators, review_needed=true")):
    """Freeze scenario.draft.json as scenario.json. Human mode records a human command; --auto records that the validators approved."""
    from swesim.stages.approve import approve_scenario
    out = approve_scenario(WS, scenario_id, auto=auto)
    typer.echo(json.dumps(out)); raise typer.Exit(0 if out["approved"] else 1)

def _load(scenario_id: str) -> ScenarioSpec:
    return ScenarioSpec.model_validate_json((WS.scenario_dir(scenario_id) / "scenario.json").read_text())

@app.command()
def compile(scenario_id: str, stakeholder_url: str = typer.Option("", envvar="SWESIM_STAKEHOLDER_URL"), condition: str = "interactive"):
    from swesim.stages.compile import compile_scenario
    res = compile_scenario(WS, _load(scenario_id), RunConfig.default(condition), stakeholder_url); typer.echo(json.dumps({k: str(v) for k, v in res.task_dirs.items()}))

@app.command()
def validate(scenario_id: str, stakeholder_url: str = typer.Option("", envvar="SWESIM_STAKEHOLDER_URL")):
    from swesim.stages.validate import validate_scenario
    out = validate_scenario(WS, _load(scenario_id), RunConfig.default("full"), stakeholder_url); typer.echo(json.dumps({"status": out["status"], "failures": out["failures"]}, indent=2))
    raise typer.Exit(0 if out["status"] == "valid" else 1)
```

- [ ] **Step 3b: Approval stage shared by the CLI and Temporal (`swesim/stages/approve.py`)**

```python
# swesim/stages/approve.py
"""Approval freezes a draft into scenario.json. Two modes:
human: a person edited the draft and runs `swesim approve <id>`.
auto (unattended runs): code-owned gates decide; the record says so and flags the scenario for post-hoc human review."""
from __future__ import annotations
import hashlib, json
from swesim.models import ScenarioSpec, Discovery, OperationType
from swesim.paths import Workspace, atomic_json

def auto_gates(spec: ScenarioSpec, sdir) -> list[str]:
    """Return failure reasons; empty means the validators approve."""
    fails = []
    judged = (spec.difficulty.judged or {})
    if judged.get("keep") is not True: fails.append("judge did not accept (no non-derivable, consequential, thread-resolved gap)")
    f2p = set(spec.contract.fail_to_pass)
    if not any(f.discovery == Discovery.ON_REQUEST and set(f.tests) & f2p for f in spec.ledger): fails.append("no on-request fact is linked to a FAIL_TO_PASS test; interaction would not be load-bearing")
    withheld = next((b for b in spec.briefs if b.profile == "withheld"), None)
    if withheld is not None:
        targets = {o.target for o in spec.operations if o.type == OperationType.WITHHOLDING}
        for f in spec.ledger:
            if f.id in targets and f.source_excerpt and f.source_excerpt.strip().lower() in withheld.text.lower(): fails.append(f"withheld brief still contains excerpt of {f.id}")
        title = next(b for b in spec.briefs if b.profile == "natural").text.strip().splitlines()[0][:60].lower()
        if title and title not in withheld.text.lower(): fails.append("withheld brief lost the natural title line")
    if not spec.controls.plausible_wrong or not any(c.cls == "incorrect-exploit" for c in spec.controls.tamper): fails.append("controls incomplete")
    return fails

def approve_scenario(ws: Workspace, scenario_id: str, auto: bool) -> dict:
    sdir = ws.scenario_dir(scenario_id); spec = ScenarioSpec.model_validate_json((sdir / "scenario.draft.json").read_text())
    for asset in spec.assets.values():
        p = (sdir / asset.path).resolve()
        if not p.is_relative_to(sdir.resolve()) or not p.is_file(): return {"approved": False, "reasons": [f"asset path {asset.path}"]}
        data = p.read_bytes()
        if len(data) != asset.bytes or hashlib.sha256(data).hexdigest() != asset.sha256: return {"approved": False, "reasons": [f"asset mismatch {asset.path}"]}
    reasons = auto_gates(spec, sdir) if auto else []
    if reasons: return {"approved": False, "reasons": reasons, "mode": "auto"}
    atomic_json(sdir / "scenario.json", spec.model_dump(mode="json"))
    atomic_json(sdir / "approval.json", {"spec_hash": spec.spec_hash, "version": spec.version,
                "approved_by": "auto-validators" if auto else "human-command", "review_needed": bool(auto),
                "gates": ["schema", "assets", "judge-accept", "on-request-fact-linked-to-f2p", "withholding-leak", "controls"] if auto else ["schema", "assets"]})
    return {"approved": True, "spec_hash": spec.spec_hash, "mode": "auto" if auto else "human"}
```

Unit test (`tests/test_approve.py`): a minimal spec with `difficulty.judged.keep = True` and an on-request fact linked to a FAIL_TO_PASS test approves in auto mode and writes `approved_by: auto-validators`; the same spec with `keep = False`, or with the withheld brief still containing a withheld excerpt, returns `approved: False` with the reason and writes nothing.

- [ ] **Step 4: Run the first real scenario through draft → approve → validate on Modal**

```bash
uv run swesim mine
uv run swesim judge workspace/candidates/swebench-annotated.jsonl
uv run python - <<'PYCODE'
import json, subprocess
p='workspace/candidates/swebench-annotated.judged.jsonl'
rows=[json.loads(x) for x in open(p)]
row=next((r for r in rows if r['judge']['keep']),None)
if row is None: raise SystemExit('no-valid-candidate; retain rejection report')
subprocess.run(['uv','run','swesim','draft',p,row['instance_id']],check=True)
print('DRAFTED:',row['instance_id'].lower().replace('__','--'))
PYCODE
```
**Unattended mode (the overnight run):** approve with the code-owned gates,
`uv run swesim approve --auto <selected-id>`. If it returns `approved: false`,
read the reasons: for a judge or load-bearing failure pick the next candidate
with `keep`; for a withholding leak re-run `draft` once (the drafter is told
the violating excerpts) and retry; after two failures record the reasons in
`docs/ops.md` and move to the next candidate. Never edit `scenario.draft.json`
by hand to satisfy a gate. The record says `approved_by: auto-validators,
review_needed: true`, and the human reviews `scenario.json` in the morning.
**Supervised mode:** print the draft path and `uv run swesim approve
<selected-id>` and wait for the human. Then run:
```bash
uv run python - <<'PYCODE'
import pathlib,subprocess
approved=sorted(pathlib.Path('workspace/scenarios').glob('*/approval.json'))
if len(approved)!=1: raise SystemExit('choose explicit approved scenario; do not guess')
subprocess.run(['uv','run','swesim','validate',approved[0].parent.name],check=True)
PYCODE
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
- Produces HTTP API (FastAPI): `POST /sessions` (header `X-Worker-Secret`) body `{trial_id, spec_hash, condition, run_config_hash, visible_view: {...}, stakeholder_config: {...}, image: str}` → `{token}`; `POST /ask` (header `Authorization: Bearer <token>`) body `{request_id, question}` → `{reply, patience_remaining, tier}`; fact annotations and tool audit remain worker-only; `POST /review` body `{request_id, diff, files: {path: content}}` → same shape plus `reviewed_diff_sha256`; `POST /freeze` → `{transcript_hash, frozen: true}`; `GET /transcript/{trial_id}` (worker secret) → full transcript JSON; `GET /health`.
- `session.Session(trial_id, visible_view, config)` with `ask(request_id, question, llm) -> Reply`, `review(request_id, diff, files, llm, tools) -> Reply`, `async freeze() -> str`, `to_dict()`; patience tiers per spec section 8; serialized via an `asyncio.Lock` per session; cached replies by `request_id`.
- `prompts.system_prompt(visible_view, persona, patience_state, review_mode) -> str` builds the context from the stakeholder-visible view only: facts with `discovery in {brief, on-request, volunteered-at-review, repo-derivable}`, requirements, checks descriptions; never `author-only`, never oracle or test assets.
- Pilot review is diff-only. `read_diff` and `read_changed_file` expose agent-supplied text labeled unverified; no sandbox or code execution is invoked. Deferred check execution must use a prepared user image, no secrets/network, byte upload rather than a diff interpolated into a shell heredoc.
- Visible view construction (in the worker, Task 11): `stakeholder.view.visible_view(spec) -> dict` (put in `swesim/stakeholder/view.py`).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_session.py
import asyncio
from swesim.stakeholder.session import Session
from swesim.models import RunConfig
VIEW = {"brief": "It breaks.", "facts": [{"id": "f01", "text": "Raise ValueError, not KeyError", "discovery": "on-request", "kind": "interface"},
        {"id": "f04", "text": "weighted.py holds the code", "discovery": "repo-derivable", "kind": "context"}],
        "requirements": [{"id": "r01", "text": "Raise ValueError"}], "persona": {"summary": "maintainer", "tone": "terse", "disclosure_style": "answers what is asked", "role": "maintainer"}, "checks": []}
class FakeLLM:
    def __init__(self): self.calls = 0
    def __call__(self, system, messages, schema):
        self.calls += 1; return {"reply": "ValueError.", "disclosed_fact_ids": ["f01"]}
def test_patience_tiers_and_idempotency():
    from swesim.models import StakeholderConfig
    cfg=StakeholderConfig.model_validate({"patience_profile":{"start":3,"question_cost":1,"review_cost":2,"terse_below":2,"refuse_at":0}})
    s = Session("t1", VIEW, cfg); llm = FakeLLM()
    r1 = asyncio.run(s.ask("q1", "Which exception?", llm)); assert r1.reply == "ValueError." and r1.patience_remaining == 2 and r1.disclosed_fact_ids == ["f01"]
    r1b = asyncio.run(s.ask("q1", "Which exception?", llm)); assert r1b.reply == r1.reply and llm.calls == 1   # cached by request_id
    r2 = asyncio.run(s.ask("q2", "And the message?", llm)); assert r2.patience_remaining == 1 and r2.tier == "terse"
    r3 = asyncio.run(s.ask("q3", "More?", llm)); assert r3.patience_remaining == 0 and r3.tier == "terse" and llm.calls == 3
    h = asyncio.run(s.freeze()); assert len(h) == 64
    import pytest
    with pytest.raises(Session.Frozen): asyncio.run(s.ask("q4", "x", llm))
def test_system_prompt_excludes_author_only():
    from swesim.stakeholder.prompts import system_prompt
    view = dict(VIEW); view["facts"] = VIEW["facts"] + [{"id": "f09", "text": "SECRET", "discovery": "author-only", "kind": "interface"}]
    assert "SECRET" not in system_prompt(view, view["persona"], {"tier": "full", "remaining": 3}, "diff-only")
```

```python
# tests/test_service.py
from fastapi.testclient import TestClient
from swesim.stakeholder import service
def test_session_lifecycle(monkeypatch,tmp_path):
    monkeypatch.setattr(service,"STORE",tmp_path)
    monkeypatch.setenv("SWESIM_WORKER_SECRET", "s3")
    monkeypatch.setattr(service, "call_llm", lambda system, messages, schema, cfg: {"reply": "ValueError.", "disclosed_fact_ids": ["f01"]})
    c = TestClient(service.app)
    r = c.post("/sessions", headers={"X-Worker-Secret": "s3"}, json={"trial_id": "t1", "spec_hash": "h", "condition": "interactive", "run_config_hash": "r",
        "visible_view": {"brief": "b", "facts": [{"id": "f01", "text": "x", "discovery": "on-request", "kind": "interface"}], "requirements": [], "persona": {"summary": "m", "tone": "t", "disclosure_style": "d", "role": "maintainer"}, "checks": []},
        "stakeholder_config": {"model_id": "openai/gpt-5.5", "prompt_version": "v1", "temperature": 0.0, "patience_profile": {"start": 2, "question_cost": 1, "review_cost": 2, "terse_below": 1, "refuse_at": 0}, "review_mode": "diff-only", "max_tool_calls_per_activation": 4}, "image": "ubuntu:24.04"})
    tok = r.json()["token"]; h = {"Authorization": f"Bearer {tok}"}
    a = c.post("/ask", headers=h, json={"request_id": "1", "question": "which exception?"}).json(); assert a["reply"] == "ValueError." and a["patience_remaining"] == 1
    f = c.post("/freeze", headers=h).json(); assert f["frozen"] and len(f["transcript_hash"]) == 64
    assert c.post("/ask", headers=h, json={"request_id": "2", "question": "x"}).status_code == 410
    # Service uses persisted snapshots, not globals holding session instances.
    c2=TestClient(service.app)
    assert c2.post("/freeze",headers=h).json()==f
    assert c2.post("/sessions",headers={"X-Worker-Secret":"wrong"},json={}).status_code in (401,422)

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
REFUSAL="I have to step away now. Please finish with the information available."
@dataclass
class Reply:
    reply:str; disclosed_fact_ids:list; patience_remaining:int; tier:str
    tool_calls:list=field(default_factory=list); reviewed_diff_sha256:str|None=None
    def public(self): return {k:v for k,v in asdict(self).items() if k in {'reply','patience_remaining','tier','reviewed_diff_sha256'}}
class Session:
    class Frozen(Exception): pass
    class Conflict(Exception): pass
    def __init__(self,trial_id,view,cfg,image=None):
        self.trial_id=trial_id; self.view=view; self.cfg=cfg; self.image=image
        self.p=cfg.patience_profile.model_dump(); self.remaining=self.p['start']
        self.events=[]; self.cache={}; self.frozen=False; self.transcript_hash=None
        self.created_at=time.time(); self.lock=asyncio.Lock()
    def _tier(self):
        return 'refused' if self.remaining<=self.p['refuse_at'] else 'terse' if self.remaining<=self.p['terse_below'] else 'full'
    def _record(self,kind,**kw): self.events.append(dict(kind=kind,ts=time.time(),seq=len(self.events),**kw))
    async def _exchange(self,rid,kind,content,cost,llm,sha=None):
        fingerprint=hashlib.sha256(json.dumps([kind,content]).encode()).hexdigest()
        async with self.lock:
            if self.frozen: raise Session.Frozen()
            if rid in self.cache:
                old=self.cache[rid]
                if old['fingerprint']!=fingerprint: raise Session.Conflict()
                return Reply(**old['reply'])
            if self.frozen: raise Session.Frozen()
            tier=self._tier()
            if self.remaining-cost<self.p['refuse_at']: tier='refused'
            calls=[]
            if kind=='review': calls=[dict(tool='read_diff',sha256=sha,verified=False)]
            if tier=='refused': reply=Reply(REFUSAL,[],self.remaining,tier,calls,sha)
            else:
                history=[{'role':e['role'],'content':e['content']} for e in self.events if e['kind']=='message']
                system=system_prompt(self.view,self.view['persona'],{'tier':tier,'remaining':self.remaining},'diff-only')
                out=await asyncio.to_thread(llm,system,history+[{'role':'user','content':content}],REPLY_SCHEMA)
                if not isinstance(out.get('reply'),str) or not out['reply']: raise ValueError('empty stakeholder reply')
                self.remaining-=cost
                allowed={f['id'] for f in self.view['facts']}
                reply=Reply(out['reply'],[i for i in out.get('disclosed_fact_ids',[]) if i in allowed],self.remaining,tier,calls,sha)
            self._record('message',role='user',actor='coding-agent',content=content,request_id=rid,exchange=kind)
            self._record('message',role='assistant',actor='stakeholder',content=reply.reply,request_id=rid,exchange=kind,
                disclosed_fact_ids=reply.disclosed_fact_ids,tier=reply.tier,patience_remaining=reply.patience_remaining,
                tool_calls=calls,reviewed_diff_sha256=sha,delivery='generated')
            self.cache[rid]={'fingerprint':fingerprint,'reply':asdict(reply)}
            return reply
    async def ask(self,rid,question,llm): return await self._exchange(rid,'ask',question,self.p['question_cost'],llm)
    async def review(self,rid,diff,files,llm,tools=None):
        if tools is not None: raise ValueError('checks review deferred')
        if len(diff.encode())>200000: raise ValueError('diff too large; do not silently truncate')
        sha=hashlib.sha256(diff.encode()).hexdigest()
        return await self._exchange(rid,'review','Review this agent-supplied diff; it is not verified against the workspace.\n'+diff,
            self.p['review_cost'],llm,sha)
    async def freeze(self):
        async with self.lock:
            self.frozen=True
            self.transcript_hash=hashlib.sha256(json.dumps(self.events,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            return self.transcript_hash
    def to_dict(self):
        return dict(trial_id=self.trial_id,events=self.events,patience_start=self.p['start'],patience_end=self.remaining,
            frozen=self.frozen,transcript_hash=self.transcript_hash,created_at=self.created_at)
    def snapshot(self):
        return dict(**self.to_dict(),view=self.view,config=self.cfg.model_dump(),cache=self.cache,image=self.image)
    @classmethod
    def restore(cls,data):
        from swesim.models import StakeholderConfig
        s=cls(data['trial_id'],data['view'],StakeholderConfig.model_validate(data['config']),data.get('image'))
        for key in ('events','cache','frozen','transcript_hash','created_at'): setattr(s,key,data[key])
        s.remaining=data['patience_end']; return s
```

- [ ] **Step 3: Implement tools.py, service.py, modal_app.py, shims**

```python
# swesim/stakeholder/tools.py
def read_diff(diff):
    if len(diff.encode())>200000: raise ValueError("review snapshot too large")
    return {"tool":"read_diff","content":diff,"verified":False}

def read_changed_file(files,path,changed_paths):
    if path not in changed_paths or path not in files: raise ValueError("path not in supplied snapshot")
    return {"tool":"read_changed_file","path":path,"content":files[path],"verified":False}
```

```python
# swesim/stakeholder/service.py
from __future__ import annotations
import asyncio, hashlib, hmac, json, os
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from swesim.models import StakeholderConfig
from swesim.llm import chat_json
from swesim.stakeholder.session import Session
from swesim.paths import atomic_json
app=FastAPI(title='swesim stakeholder'); LOCK=asyncio.Lock()
# One active Modal container, serialized transactions. Snapshots committed before HTTP replies.
STORE=Path(os.environ.get('SWESIM_SESSION_STORE','workspace/stakeholder'))
SYNC=lambda:None
RELOAD=lambda:None

def call_llm(system,messages,schema,cfg): return chat_json(cfg.model_id,system,messages,schema,temperature=cfg.temperature)
def _worker(secret):
    if not secret or not os.environ.get('SWESIM_WORKER_SECRET') or not hmac.compare_digest(secret,os.environ['SWESIM_WORKER_SECRET']): raise HTTPException(401)
def _path(tid):
    if not tid or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in tid): raise HTTPException(400,'trial id')
    return STORE/(tid+'.json')
def _token(tid,binding):
    return tid+'.'+hmac.new(os.environ['SWESIM_WORKER_SECRET'].encode(),binding.encode(),hashlib.sha256).hexdigest()
def _load(auth):
    tok=(auth or '').removeprefix('Bearer '); tid=tok.split('.')[0]
    p=_path(tid)
    if not p.exists(): raise HTTPException(401)
    d=json.loads(p.read_text())
    if not hmac.compare_digest(tok,_token(tid,d['binding'])): raise HTTPException(401)
    return p,d
class Strict(BaseModel): model_config=ConfigDict(extra='forbid')
class NewSession(Strict):
    trial_id:str; spec_hash:str; condition:str; run_config_hash:str
    visible_view:dict; stakeholder_config:dict; image:str|None=None
class Ask(Strict): request_id:str=Field(min_length=1,max_length=100); question:str=Field(min_length=1,max_length=20000)
class Review(Strict): request_id:str=Field(min_length=1,max_length=100); diff:str=Field(max_length=200000); files:dict=Field(default_factory=dict)
@app.get('/health')
async def health(): return {'ok':True}
@app.post('/sessions')
async def new_session(body:NewSession,x_worker_secret:str|None=Header(default=None)):
    _worker(x_worker_secret)
    if body.condition!='interactive': raise HTTPException(400,'interactive only')
    if any(f.get('discovery')=='author-only' for f in body.visible_view.get('facts',[])): raise HTTPException(400,'invalid visible view')
    binding=hashlib.sha256(json.dumps(body.model_dump(),sort_keys=True).encode()).hexdigest()
    async with LOCK:
        RELOAD(); p=_path(body.trial_id)
        if p.exists():
            if json.loads(p.read_text())['binding']!=binding: raise HTTPException(409,'session binding changed')
        else:
            s=Session(body.trial_id,body.visible_view,StakeholderConfig.model_validate(body.stakeholder_config),body.image)
            s._record('session',spec_hash=body.spec_hash,condition=body.condition,run_config_hash=body.run_config_hash)
            atomic_json(p,{'binding':binding,'session':s.snapshot(),'pending':None}); SYNC()
        return {'token':_token(body.trial_id,binding)}
async def exchange(kind,body,auth):
    async with LOCK:
        RELOAD(); p,d=_load(auth); s=Session.restore(d['session'])
        if d.get('pending'):
            raise HTTPException(503,'interrupted prior request; simulator failure requires worker adjudication')
        d['pending']={'request_id':body.request_id,'kind':kind}; atomic_json(p,d); SYNC()
        try:
            fn=lambda sy,m,sc:call_llm(sy,m,sc,s.cfg)
            reply=await s.ask(body.request_id,body.question,fn) if kind=='ask' else await s.review(body.request_id,body.diff,body.files,fn)
        except Session.Frozen: d['pending']=None; atomic_json(p,d); SYNC(); raise HTTPException(410,'frozen')
        except Session.Conflict: d['pending']=None; atomic_json(p,d); SYNC(); raise HTTPException(409,'request id changed')
        except Exception as e:
            d['failure']={'request_id':body.request_id,'kind':type(e).__name__}; atomic_json(p,d); SYNC(); raise HTTPException(503,'stakeholder failure')
        d.update(session=s.snapshot(),pending=None); atomic_json(p,d); SYNC()
        return reply.public()
@app.post('/ask')
async def ask(body:Ask,authorization:str|None=Header(default=None)): return await exchange('ask',body,authorization)
@app.post('/review')
async def review(body:Review,authorization:str|None=Header(default=None)): return await exchange('review',body,authorization)
@app.post('/freeze')
async def freeze(authorization:str|None=Header(default=None)):
    async with LOCK:
        RELOAD(); p,d=_load(authorization); s=Session.restore(d['session']); h=await s.freeze()
        d['session']=s.snapshot(); atomic_json(p,d); SYNC(); return {'frozen':True,'transcript_hash':h}
@app.get('/transcript/{trial_id}')
async def transcript(trial_id:str,x_worker_secret:str|None=Header(default=None)):
    _worker(x_worker_secret)
    async with LOCK:
        RELOAD(); p=_path(trial_id)
        if not p.exists(): raise HTTPException(404)
        d=json.loads(p.read_text()); s=Session.restore(d['session'])
        return {**s.to_dict(),'service_failure':d.get('failure'),'pending':d.get('pending')}
```

```python
# swesim/stakeholder/modal_app.py
import modal
app=modal.App("swesim-stakeholder")
volume=modal.Volume.from_name("swesim-stakeholder-state",create_if_missing=True)
image=(modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi[standard]","pydantic>=2.7","anthropic>=0.40","openai>=1.50")
    .env({"SWESIM_SESSION_STORE":"/state/sessions"}).add_local_python_source("swesim"))
@app.function(image=image,secrets=[modal.Secret.from_name("swesim-llm")],
    volumes={"/state":volume},min_containers=1,max_containers=1,timeout=600)
@modal.asgi_app()
def web():
    from swesim.stakeholder import service
    service.SYNC=volume.commit; service.RELOAD=volume.reload
    return service.app
```

Both shims are root-owned Python executables using `/usr/local/bin/swesim-python`.
Create `ask-user` with the following content, and create `request-review` with
identical content (the basename chooses the endpoint):
```python
#!/usr/local/bin/swesim-python -I
import json,os,pathlib,subprocess,sys,uuid,urllib.request,urllib.error
base=os.environ.get('SWESIM_STAKEHOLDER_URL',''); token=os.environ.get('SWESIM_SESSION_TOKEN','')
if not token: print('No stakeholder is available in this task.'); raise SystemExit(0)
kind='ask' if pathlib.Path(sys.argv[0]).name=='ask-user' else 'review'
body={'request_id':uuid.uuid4().hex}
if kind=='ask': body['question']=' '.join(sys.argv[1:])
else:
    repo=os.environ['SWESIM_REPO_DIR']
    subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null','add','-N','.'],cwd=repo,check=True)
    diff=subprocess.run(['/usr/bin/git','--no-pager','diff','--no-ext-diff','--no-textconv','--no-color','HEAD'],cwd=repo,capture_output=True,check=True).stdout
    if len(diff)>200000: raise SystemExit('Review diff exceeds 200KB; submit a smaller explicit review snapshot')
    body.update(diff=diff.decode(),files={})
req=urllib.request.Request(base+'/'+kind,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
try:
    with urllib.request.urlopen(req,timeout=180) as response: data=json.load(response)
    print(data['reply']); print('[availability remaining: '+str(data['patience_remaining'])+']')
except urllib.error.HTTPError as e:
    if e.code==410: print('Stakeholder session closed.')
    else: raise SystemExit('Stakeholder transport failed: HTTP '+str(e.code))
```
The shims do not secure the protocol: the agent can call HTTP directly. All
budget, binding and freeze enforcement lives on the service. Agent-supplied
diffs may omit committed work; reviews are advisory and explicitly unverified.
Task 12 reports `reviewed_equals_submitted: null` until a common trusted
snapshot protocol exists; do not compare a textual diff hash to a tree hash.

`scripts/deploy_stakeholder.sh`:
```bash
#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv run python - <<'PYCODE'
from dotenv import dotenv_values
from pathlib import Path
import modal,os
keys=dotenv_values(os.environ.get('SWESIM_ENV_FILE',str(Path.home()/'Documents/.env')))
required=['ANTHROPIC_API_KEY','OPENAI_API_KEY','SWESIM_WORKER_SECRET']
if not all(keys.get(k) for k in required): raise SystemExit('Set all required keys once in the ignored env file; do not regenerate on deploy')
modal.Secret.objects.create('swesim-llm',{k:keys[k] for k in required},allow_existing=True)
PYCODE
uv run modal deploy swesim/stakeholder/modal_app.py
uv run python - <<'PYCODE'
from pathlib import Path
import modal
url=modal.Function.from_name('swesim-stakeholder','web').get_web_url()
if not url: raise SystemExit('deployment has no web URL')
Path('.stakeholder_url').write_text(url+'\n')
PYCODE
```


Before deployment add the ignored runtime files:
```bash
cat >> .gitignore <<'IGNORE'
.stakeholder_url
registry.json.lock
*.env
IGNORE
```

- [ ] **Step 4: Run unit tests, deploy, probe**

Run: `uv run pytest tests/test_session.py tests/test_service.py -q` → all listed tests pass.
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
    d = minimal_spec_dict(); d["ledger"].append({"id": "f09", "text": "SECRET", "kind": "interface", "provenance": "authored", "authored_rationale": "x", "discovery": "author-only"})
    v = visible_view(ScenarioSpec.model_validate(d)); assert all(f["id"] != "f09" for f in v["facts"]) and v["brief"] == "It breaks."
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
    spec_hash: str = ""; model: str = ""; purpose: str = "demonstration"; compiled_hash: str = ""; brief_profile: str = "natural"

def materialize_trial_task(ws: Workspace, compiled_task_dir: Path, tid: str, token: str) -> Path:
    dst = ws.root / "trials" / tid / "task"
    if dst.exists(): raise ValueError('trial task already exists; reconcile before materialization')
    shutil.copytree(compiled_task_dir,dst); dst.chmod(0o700)
    p = dst / "task.toml"; s = p.read_text()
    s = re.sub(r'SWESIM_SESSION_TOKEN = ".*"', f'SWESIM_SESSION_TOKEN = "{token}"', s); s = re.sub(r'SWESIM_TRIAL_ID = ".*"', f'SWESIM_TRIAL_ID = "{tid}"', s)
    import tomllib,tomli_w
    cfg=tomllib.loads(s)
    cfg.setdefault('metadata',{})['trial_id']=tid
    p.write_text(tomli_w.dumps(cfg)); p.chmod(0o600); return dst

def _post(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}); return json.load(urllib.request.urlopen(req, timeout=60))
def _get(url: str, headers: dict) -> dict:
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60))

def run_trial(ws: Workspace, spec: ScenarioSpec, rc: RunConfig, attempt_index: int, stakeholder_url: str, worker_secret: str) -> TrialRecord:
    comp=json.loads((ws.scenario_dir(spec.id)/"compile.json").read_text())
    val=json.loads((ws.scenario_dir(spec.id)/"validation.json").read_text())
    if val['status']!='valid' or val['spec_hash']!=spec.spec_hash or val['compiled_hash']!=comp['compiled_hash']:
        raise ValueError('scenario/build not validated')
    profile='natural' if rc.condition=='full' or not any(b.profile==rc.brief_profile for b in spec.briefs) else rc.brief_profile
    rc=RunConfig.model_validate({**rc.model_dump(),"compiled_hash":comp['compiled_hash'],"brief_profile":profile})
    tid = make_trial_id(spec.spec_hash, rc.condition, rc.run_config_hash, attempt_index)
    prior=ws.root/'trials'/tid/'record.json'
    if prior.exists(): return TrialRecord(**json.loads(prior.read_text()))
    compiled = ws.task_dir(spec.id, rc.condition); token = ""
    expected_host=__import__('urllib.parse',fromlist=['urlparse']).urlparse(stakeholder_url).hostname
    import tomllib
    tc=tomllib.loads((compiled/'task.toml').read_text())
    if rc.condition=='interactive' and expected_host not in tc['environment']['allowed_hosts']:
        raise ValueError('recompile and validate for actual stakeholder endpoint')
    if int(tc['agent']['timeout_sec'])!=rc.solver_timeout_sec: raise ValueError('compiled timeout differs from run config')
    if rc.condition == "interactive":
        token = _post(f"{stakeholder_url}/sessions", {"trial_id": tid, "spec_hash": spec.spec_hash, "condition": rc.condition, "run_config_hash": rc.run_config_hash,
                      "visible_view": visible_view(spec), "stakeholder_config": rc.stakeholder.model_dump(), "image": spec.environment.image}, {"X-Worker-Secret": worker_secret})["token"]
    task = materialize_trial_task(ws, compiled, tid, token); tdir = ws.root / "trials" / tid
    import tomli_w
    tc=tomllib.loads((task/'task.toml').read_text())
    tc['metadata'].update(run_config_hash=rc.run_config_hash,compiled_hash=rc.compiled_hash,condition=rc.condition)
    natural=next(b for b in spec.briefs if b.profile=='natural')
    selected=next((b for b in spec.briefs if b.profile==rc.brief_profile),natural)
    from jinja2 import Environment,FileSystemLoader,StrictUndefined
    from swesim.stages.compile import TEMPLATES,visible_facts
    env=Environment(loader=FileSystemLoader(str(TEMPLATES)),undefined=StrictUndefined)
    instruction=env.get_template('instruction.md.j2').render(brief=natural.text if rc.condition=='full' else selected.text,
        condition=rc.condition,visible_facts=visible_facts(spec),repo_dir=spec.environment.repo_dir,solver_minutes=rc.solver_timeout_sec//60)
    (task/'instruction.md').write_text(instruction)
    tc['metadata']['brief_profile']=rc.brief_profile
    provider_host={'anthropic':'api.anthropic.com','openai':'api.openai.com'}[rc.model.split('/')[0]]
    for phase in ('environment','agent'): tc[phase]['allowed_hosts']=sorted(set(tc[phase]['allowed_hosts']+[provider_host]))
    (task/'task.toml').write_text(tomli_w.dumps(tc))
    (tdir / "status.json").write_text(json.dumps({"status": "running", "trial_id": tid}))
    status = "completed"; trial_dir = None; reward = {}; report = {}; transcript = {}
    try:
        trial_dir = harbor_cli.run_task(task, rc.agent, ws.jobs_dir / "runs", tid, model=rc.model, timeout_sec=rc.solver_timeout_sec + 7200)
        reward = harbor_cli.read_reward(trial_dir); report = harbor_cli.read_report(trial_dir)
        raw=json.loads((trial_dir/'result.json').read_text()); report['harbor_exception']=raw.get('exception_info')
        if raw.get('exception_info'):
            name=raw['exception_info'].get('exception_type','')
            code=3 if 'AgentTimeout' in name else 4 if 'Agent' in name else 7
            reward.update(reward=0.,success=0,outcome_code=code)
            report['outcome']={3:'agent-invalid-submission',4:'agent-runtime-failure',7:'evaluator-failure'}[code]
        if not reward: reward={'reward':0.,'success':0,'outcome_code':7}
    except harbor_cli.HarborRunError as e:
        status = "launch-failed"; report = {"error": str(e),"outcome":"evaluator-failure"}; reward={"reward":0.,"success":0,"outcome_code":7}
    if rc.condition == "interactive":
        try:
            _post(f"{stakeholder_url}/freeze", {}, {"Authorization": f"Bearer {token}"})
        except Exception: pass
        try: transcript = _get(f"{stakeholder_url}/transcript/{tid}", {"X-Worker-Secret": worker_secret})
        except Exception as e: transcript={"fetch_error":type(e).__name__}
        bound=report.get('transcript_hash')
        if not bound or bound!=transcript.get('transcript_hash') or not transcript.get('frozen'):
            report['transcript_binding_valid']=False; report['training_eligible']=False
            # Keep correctness scores; a missing transcript does not make code failure disappear.
        else: report['transcript_binding_valid']=True
        if transcript.get('service_failure'):
            report['service_failure_evidence']=transcript['service_failure']
            report['training_eligible']=False
        if report.get("transcript_hash") and transcript.get("transcript_hash") and report["transcript_hash"] != transcript["transcript_hash"]:
            report["transcript_hash_mismatch"] = True
    report['reviewed_equals_submitted']=None
    report['image_digest']=comp.get('image_digest')
    report['simulator_validity']='unadjudicated' if rc.condition=='interactive' else 'n/a'
    rec = TrialRecord(tid,spec.id,rc.condition,attempt_index,rc.run_config_hash,str(task),str(trial_dir) if trial_dir else None,
        reward,report,transcript,status,spec.spec_hash,rc.model,rc.purpose,rc.compiled_hash,rc.brief_profile)
    for name, obj in (("run_config.json", rc.model_dump(mode="json")), ("spec.json",spec.model_dump(mode="json")), ("reward.json", reward), ("report.json", report), ("transcript.json", transcript), ("record.json", asdict(rec))): (tdir / name).write_text(json.dumps(obj, indent=2))
    (tdir / "status.json").write_text(json.dumps({"status": status, "trial_id": tid})); return rec
```

Add to `swesim/cli.py`:
```python
@app.command()
def run(scenario_id: str, condition: str = "interactive", attempt: int = 0, model: str = "anthropic/claude-haiku-4-5-20251001", brief_profile: str = "withheld",
        stakeholder_url: str = typer.Option("", envvar="SWESIM_STAKEHOLDER_URL"), worker_secret: str = typer.Option(..., envvar="SWESIM_WORKER_SECRET")):
    from swesim.stages.run import run_trial
    rc = RunConfig(condition=condition,model=model,brief_profile=brief_profile)
    rec = run_trial(WS, _load(scenario_id), rc, attempt, stakeholder_url, worker_secret); typer.echo(json.dumps({"trial_id": rec.trial_id, "status": rec.status, "reward": rec.reward}))
```

- [ ] **Step 3: Run the first interactive episode**

```bash
: # CLI loads the ignored env file using python-dotenv; export SWESIM_STAKEHOLDER_URL="$(cat .stakeholder_url)"
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
- Produces: `github_repo.discover(repo: str, since: str, max_prs: int = 400, min_comments: int = 4, diff_min: int = 20, diff_max: int = 600, pr: int | None = None, kind="feature") -> tuple[list[dict], dict]` (candidates and a reject histogram); each candidate `{repo, pr, issue, base_commit, merge_commit, title, issue_body, thread, patch, test_patch, source_files, test_files, additions, deletions}`; `github_repo.build_recipe(repo, base_commit, profile="python-pytest") -> dict(install_command, test_command, repo_dir="/app")`; `github_repo.candidate_to_row(c) -> dict` in the same shape as SWE-bench rows (`problem_statement` = issue title + body, `hints_text` = issue comments before the PR, `fail_to_pass`/`pass_to_pass` filled by `probe_tests` below).
- `github_repo.probe_tests(row, ws) -> dict` runs, via Harbor on Modal with the `dockerfile` environment (`nop` agent on a probe task whose verifier runs the PR's test files on base and after applying the gold patch), and returns `fail_to_pass`/`pass_to_pass` ids (tests that fail on base and pass with gold; tests passing in both). This replaces SWE-bench's precomputed lists. Bounded by `--max-builds`.
- Pre-selected demonstration repository: `py-pdf/pypdf` (install: `pip install -e '.[full]' -r requirements/ci.txt`; test command `python -I /tests/run_pytest.py -rA -p no:cacheprovider -m "not samples and not enable_socket" --junitxml={junit_path} <files>`); since `2026-06-01`.

Candidate repositories to try, one repo URL per invocation: `fgmacedo/python-statemachine`,
`celery/kombu`, `graphql-python/gql`, `narwhals-dev/narwhals`, `pytest-dev/pytest`,
`Textualize/textual`, `py-pdf/pypdf`. The supplied repo-yield note is a screening
prior; each pinned revision still needs the same environment probe and controls.
If the pypdf feature search is empty, use another listed repository URL and
retain both searches. Do not supply a PR to satisfy repo-only acceptance.

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
def normalize_repo(value):
    value=value.removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",value): raise ValueError("GitHub repository URL or owner/repo required")
    return value

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
    if c.get("thread") and not c["thread"].get("linked_issues"): return "missing-linked-issue"
    if c.get("thread") and any(i["created_at"]>=c["thread"]["pr_created_at"] for i in c["thread"]["linked_issues"]): return "issue-not-before-pr"
    if not c.get("test_files"): return "no-test-changes"
    if not c.get("source_files"): return "no-source-changes"
    n = c["additions"] + c["deletions"]
    if n < diff_min: return "diff-too-small"
    if n > diff_max: return "diff-too-large"
    if c.get("comments_total", 0) < min_comments: return "thin-thread"
    return None

def _gh(path: str, paginate: bool = False):
    cmd = ["gh", "api", path] + (["--paginate"] if paginate else []); return json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)

def discover(repo: str, since: str, max_prs: int = 400, min_comments: int = 4, diff_min: int = 20, diff_max: int = 600, pr: int | None = None, kind: str = "feature") -> tuple[list[dict], dict]:
    repo=normalize_repo(repo)
    if kind=="feature": min_comments,diff_min,diff_max=8,200,1500
    elif kind!="bugfix": raise ValueError("kind must be feature or bugfix")
    summaries=[]
    if pr: summaries=[threads._gh(f"repos/{repo}/pulls/{pr}")]
    else:
        for page in range(1,(max_prs+99)//100+1):
            rows=threads._gh(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}")
            summaries.extend(rows)
            if len(rows)<100: break
    prs=[]
    for p in summaries[:max_prs]:
        detail=threads._gh(f"repos/{repo}/pulls/{p['number']}")
        if detail.get("merged_at") and detail["merged_at"]>=since: prs.append(detail)
    cands, rejects = [], Counter()
    for p in prs:
        issues = threads.linked_numbers(repo,p["number"],p.get("body") or "")
        patch = subprocess.run(["gh", "api", f"repos/{repo}/pulls/{p['number']}", "-H", "Accept: application/vnd.github.v3.diff"], capture_output=True, text=True, check=True, timeout=60).stdout
        src, tst = split_patch(patch)
        c = {"repo": repo, "pr": p["number"], "title": p["title"], "base_commit": p["base"]["sha"], "merge_commit": p.get("merge_commit_sha"),
             "additions": sum(l.startswith("+") and not l.startswith("+++") for l in src.splitlines()), "deletions": sum(l.startswith("-") and not l.startswith("---") for l in src.splitlines()), "patch": src, "test_patch": tst,
             "source_files": re.findall(r"^diff --git a/(\S+)", src, re.M), "test_files": re.findall(r"^diff --git a/(\S+)", tst, re.M), "issue": None, "thread": None}
        if issues:
            c["issue"] = issues[0]; c["thread"] = threads.fetch_pr_thread(repo, p["number"])
            c["comments_total"] = len(c["thread"]["pr_comments"]) + len(c["thread"]["review_comments"]) + sum(len(i["comments"]) for i in c["thread"]["linked_issues"])
        c['labels']=[x['name'].lower() for x in p.get('labels',[])]
        r = reject_reason(c, min_comments, diff_min, diff_max)
        if not r and kind=='feature':
            if not 3<=len(c['source_files'])<=30: r='feature-source-file-band'
            elif not any(any(word in label for word in ('enhancement','feature','rfc','proposal')) for label in c['labels']): r='feature-label-missing'
        c['discovery']={'kind':kind,'repo_only':pr is None,'since':since,'max_prs':max_prs}
        if r: rejects[r] += 1
        else: cands.append(c)
    return cands, dict(rejects)

def build_recipe(repo: str, base_commit: str, profile: str = "python-pytest") -> dict:
    k = KNOWN_RECIPES.get(repo, {}); install = k.get("install_command", "pip install -e . && pip install pytest")
    marker = k.get("test_marker", "")
    return {"repo_dir": "/app", "install_command": install, "test_python":"/usr/local/bin/python",
            "test_command": f"python -I /tests/run_pytest.py -rA -p no:cacheprovider {marker} --junitxml={{junit_path}} {{test_files}}".replace("  ", " ")}

def candidate_to_row(c: dict) -> dict:
    iss = (c["thread"] or {}).get("linked_issues", [{}])[0] if c.get("thread") else {}
    before_pr = [x for x in iss.get("comments", []) if x["created_at"] < c["thread"]["pr_created_at"]]
    return {"source":"github-repo", "instance_id": f"{c['repo'].replace('/', '__')}-{c['pr']}", "repo": c["repo"], "base_commit": c["base_commit"], "version": "",
            "problem_statement": (iss.get("title", c["title"]) + "\n\n" + iss.get("body", "")).strip(), "hints_text": "\n\n".join(x["body"] for x in before_pr),
            "test_files":c["test_files"], "fail_to_pass": [], "pass_to_pass": [], "patch": c["patch"], "test_patch": c["test_patch"], "image": None, "repo_dir": "/app",
            "discovery":c.get("discovery",{}), "annotation": {"underspecified": "", "underspecified_notes": "", "false_negative": "", "false_negative_notes": "", "difficulty": ""}, "thread": c["thread"] or {}}
```

Add this code to `swesim/sources/github_repo.py`:
```python
def probe_tests(row, ws):
    """Separate authoring probe. No ScenarioSpec is constructed until test ids exist."""
    import hashlib, json, shlex, shutil
    from pathlib import Path
    from swesim import harbor_cli
    from swesim.stages.compile import resolve_docker_hub_digest, VERIFIER_SRC
    from swesim.paths import atomic_json
    name=row['instance_id'].lower().replace('__','--')
    root=ws.root/'probes'/name; env=root/'environment'; tests=root/'tests'
    env.mkdir(parents=True,exist_ok=True); tests.mkdir(exist_ok=True)
    digest=resolve_docker_hub_digest('python:3.12-slim')
    if not digest: raise RuntimeError('base image digest unavailable')
    dockerfile=f"""FROM python@{digest}
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN git clone {shlex.quote('https://github.com/'+row['repo'])} /app && cd /app && git checkout {row['base_commit']} && {row['install_command']}
WORKDIR /app
"""
    row['dockerfile']=dockerfile
    (env/'Dockerfile').write_text(dockerfile)
    (tests/'Dockerfile').write_text(dockerfile+'\nCOPY . /tests/\n')
    for p in VERIFIER_SRC.glob('*.py'): shutil.copyfile(p,tests/p.name)
    (tests/'test.patch').write_text(row['test_patch']); (tests/'oracle.diff').write_text(row['patch'])
    files=[p for p in row['test_files'] if p.endswith('.py')]
    command='/usr/local/bin/python -I /tests/run_pytest.py -rA -p no:cacheprovider --junitxml={junit_path} '+' '.join(shlex.quote(p) for p in files)
    atomic_json(tests/'probe_config.json',{'command':command})
    (tests/'test.sh').write_text('#!/bin/bash\nset -euo pipefail\n/usr/local/bin/python -I /tests/probe.py\n')
    (tests/'probe.py').write_text('''import sys,json,subprocess
from pathlib import Path
sys.path.insert(0,'/tests')
from grader import GradeConfig,_run_tests
cfg=GradeConfig(Path('/tmp/a'),Path('/app'),Path('/tmp/w'),{},[],[],json.loads(Path('/tests/probe_config.json').read_text()),None,Path('/logs/verifier'),test_timeout_sec=300)
results={}
for stage,patch in [('base',None),('test','/tests/test.patch'),('gold','/tests/oracle.diff')]:
    if patch: subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null','apply',patch],cwd='/app',check=True)
    st,rc,out,error=_run_tests(cfg,Path('/app'),None)
    results[stage]={'statuses':st,'exit_code':rc,'error':error,'tail':out[-2000:]}
Path('/logs/verifier').mkdir(parents=True,exist_ok=True)
Path('/logs/verifier/probe.json').write_text(json.dumps(results))
Path('/logs/verifier/reward.json').write_text(json.dumps({'reward':int(not results['gold']['error'])}))
''')
    (root/'instruction.md').write_text('Environment authoring probe; no solver work.')
    (root/'task.toml').write_text('''schema_version="1.4"
[environment]
network_mode="no-network"
build_timeout_sec=3600
[agent]
timeout_sec=60
[verifier]
environment_mode="separate"
network_mode="no-network"
timeout_sec=1200
[verifier.environment]
build_timeout_sec=3600
''')
    td=harbor_cli.run_task(root,'nop',ws.jobs_dir/'probe',name,timeout_sec=9000)
    data=json.loads((td/'verifier/probe.json').read_text())
    before=data['test']['statuses']; after=data['gold']['statuses']
    if data['test']['error'] or data['gold']['error'] or not after or set(before)!=set(after):
        raise ValueError('unreliable base/gold probe; keep evidence and reject')
    f=[t for t in before if before[t] in {'failed','error'} and after[t]=='passed']
    p=[t for t in before if before[t]==after[t]=='passed']
    if not f or not p: raise ValueError('probe needs target failure and regression coverage')
    return {'fail_to_pass':f,'pass_to_pass':p,'dockerfile':dockerfile,'probe_dir':str(td),'test_python':'/usr/local/bin/python'}
```

Add to `swesim/stages/mine.py`:
```python
def mine_github_repo(ws: Workspace, repo: str, pr: int | None = None, since: str = "2026-06-01", max_candidates: int = 20, max_builds: int = 3, max_prs: int = 400, kind: str = "feature") -> Path:
    import json
    from swesim.sources import github_repo as gr
    repo=gr.normalize_repo(repo)
    cands, rejects = gr.discover(repo,since,pr=pr,max_prs=max_prs,kind=kind)
    out = ws.candidates_dir / f"github-repo--{repo.replace('/', '__')}.jsonl"; out.parent.mkdir(parents=True, exist_ok=True)
    built = 0
    with open(out, "w") as f:
        for c in cands[:max_candidates]:
            row = gr.candidate_to_row(c); row.update(gr.build_recipe(repo, c["base_commit"]))
            if built < max_builds:
                built += 1
                try: row.update(gr.probe_tests(row,ws))
                except Exception as e:
                    rejects["build-or-probe-failed"]=rejects.get("build-or-probe-failed",0)+1
                    (out.parent/(row["instance_id"]+".error.json")).write_text(json.dumps({"error":str(e)})); continue
            else:
                rejects["build-budget-exhausted"]=rejects.get("build-budget-exhausted",0)+1; continue
            if not row["fail_to_pass"]: rejects["no-f2p-after-probe"] = rejects.get("no-f2p-after-probe", 0) + 1; continue
            f.write(json.dumps(row) + "\n")
    (out.with_suffix(".rejects.json")).write_text(json.dumps(rejects, indent=2))
    if out.stat().st_size == 0: (out.with_suffix(".status")).write_text("no-valid-candidate")
    return out
```

- [ ] **Step 3: Run on pypdf, then draft/approve/validate the chosen candidate**

```bash
uv run swesim mine --source github-repo --repo https://github.com/py-pdf/pypdf
uv run swesim judge workspace/candidates/github-repo--py-pdf__pypdf.jsonl --limit 5
uv run python - <<'PYCODE'
import json,subprocess
p='workspace/candidates/github-repo--py-pdf__pypdf.judged.jsonl'
rows=[json.loads(l) for l in open(p)]
r=next((x for x in rows if x['judge']['keep']),None)
if r is None: raise SystemExit('no-valid-candidate; do not supply a hand-picked PR to satisfy repo-only acceptance')
subprocess.run(['uv','run','swesim','draft',p,r['instance_id']],check=True)
print('DRAFTED:',r['instance_id'].lower().replace('__','--'))
PYCODE
uv run swesim approve --auto <drafted-id>     # unattended mode; supervised mode: human runs `swesim approve <id>`
```
Use the approved GitHub scenario ID for `validate`, `register` and `run
--condition interactive`. Task
12 supplies registration before Temporal is exercised. Record discovered PR,
filters, candidate rejections, build count and the real trial id. The test
`tests/test_github_repo.py` must cover URL normalization, more than one page,
missing list-response statistics, no surviving candidates, build-budget
exhaustion and a feature candidate admitted only after its detailed PR fetch.


- [ ] **Step 4: Commit**

```bash
git add swesim/sources/github_repo.py swesim/stages/mine.py tests/test_github_repo.py
git commit -m "feat(sources): github-repo discovery with bounded filters, reject histogram, build recipe, test probe"
```

---

### Task 11: Temporal workflows, activities, worker, client (thin)

**Files:**
- Create: `swesim/workflows/__init__.py`, `swesim/workflows/activities.py`, `swesim/workflows/workflows.py`, `swesim/workflows/worker.py`, `swesim/workflows/client.py`
- Modify: `pyproject.toml` (add `temporalio>=1.8`), `swesim/cli.py` (add `pipeline`, `approve-signal`, `matrix`, `worker`)
- Create: `tests/test_workflows.py`

**Interfaces:**
- Activities carry IDs, paths, hashes and model configuration, never secrets. Blocking LLM/build/Harbor operations run in threads with a heartbeat every 30 seconds.
- `PipelineWorkflow`: mine → judge → draft → wait for `{scenario_id,spec_hash}` signal → verify approval file/hash → compile → validate → register → one trial → score/export. No Boolean is read from `wait_condition`.
- `RunMatrixWorkflow` receives precomputed trial requests containing the actual per-model RunConfig. It launches serial `TrialWorkflow` children using `REJECT_DUPLICATE`.
- `TrialWorkflow`: launch one named user systemd unit once, then retryable heartbeating polling. The Harbor process outlives the Temporal worker. Full VM loss is an explicitly unresolved attempt, never an automatic fresh solver launch.
- Worker task queue `swesim`; concurrency 4 for lightweight activities, pilot trials serial. Execute Task 12 before this task.

- [ ] **Step 1: Workflow test with mocked activities (time-skipping test env)**

```python
# tests/test_workflows.py
import asyncio, pytest
from temporalio.common import WorkflowIDReusePolicy
from swesim.workflows import workflows as W

def test_approval_wait_none_is_success(monkeypatch):
    async def scenario():
        w=W.PipelineWorkflow(); calls=[]
        async def act(name,p,minutes=10):
            calls.append(name)
            return {"candidates_path":"c","judged_path":"j","instance_ids":["a"],"scenario_id":"aaa","draft_path":"d",
                "status":"valid","failures":[],"trials":[],"spec_hash":"h"}
        async def wait(predicate,timeout):
            w.approve_spec({"scenario_id":"aaa","spec_hash":"h"}); assert predicate(); return None
        monkeypatch.setattr(W,'act',act); monkeypatch.setattr(W.workflow,'wait_condition',wait)
        result=await w.run({'approval_mode':'human'})
        assert result['scenarios'][0]['status']=='complete' and 'verify_approval_activity' in calls and 'validate_activity' in calls
    asyncio.run(scenario())

@pytest.mark.parametrize('approved',[True,False])
def test_auto_approval_never_waits_and_rejection_stops_validation(monkeypatch,approved):
    async def scenario():
        w=W.PipelineWorkflow(); calls=[]
        async def act(name,p,minutes=10):
            calls.append(name)
            if name=='auto_approve_activity':
                return {'approved':approved,'spec_hash':'h','reasons':[] if approved else ['gate rejected']}
            return {'candidates_path':'c','judged_path':'j','instance_ids':['a'],
                'scenario_id':'aaa','draft_path':'d','status':'valid','failures':[],
                'trials':[],'spec_hash':'h'}
        async def wait(*args,**kwargs):
            raise AssertionError('automatic approval must not wait for a person')
        monkeypatch.setattr(W,'act',act); monkeypatch.setattr(W.workflow,'wait_condition',wait)
        result=await w.run({})
        assert 'auto_approve_activity' in calls
        assert ('verify_approval_activity' in calls)==approved
        assert ('validate_activity' in calls)==approved
        assert ('register_activity' in calls)==approved
        expected='complete' if approved else 'auto-approval-rejected'
        assert result['scenarios'][0]['status']==expected
    asyncio.run(scenario())

def test_matrix_uses_precomputed_distinct_model_ids(monkeypatch):
    async def scenario():
        ids=[]
        async def child(fn,p,**kw):
            ids.append(kw['id']); assert kw['id_reuse_policy']==WorkflowIDReusePolicy.REJECT_DUPLICATE
            return {'trial_id':kw['id']}
        async def act(*args,**kw): return {}
        monkeypatch.setattr(W.workflow,'execute_child_workflow',child); monkeypatch.setattr(W,'act',act)
        await W.RunMatrixWorkflow().run({'scenario_id':'aaa','trials':[{'trial_id':'model-a'},{'trial_id':'model-b'}]})
        assert ids==['model-a','model-b']
    asyncio.run(scenario())
```

- [ ] **Step 2: Implement**

```python
# swesim/workflows/activities.py
import asyncio, json, os, subprocess, time
from pathlib import Path
from temporalio import activity
from swesim.models import ScenarioSpec,RunConfig,trial_id
from swesim.paths import Workspace,atomic_json
WS=Workspace(os.environ.get('SWESIM_WORKSPACE','workspace'))
def _spec(sid): return ScenarioSpec.model_validate_json((WS.scenario_dir(sid)/'scenario.json').read_text())
async def blocking(fn):
    future=asyncio.create_task(asyncio.to_thread(fn))
    while not future.done():
        activity.heartbeat()
        done,_=await asyncio.wait([future],timeout=30)
    return future.result()
@activity.defn
async def mine_activity(p):
    from swesim.stages.mine import mine_swebench,mine_github_repo
    path=await blocking(lambda:mine_swebench(WS,instance_ids=p.get('instance_ids')) if p['source']=='swebench-annotated'
        else mine_github_repo(WS,p['repo'],p.get('pr')))
    return {'candidates_path':str(path)}
@activity.defn
async def judge_activity(p):
    from swesim.stages.judge import judge_candidate
    out=Path(p['candidates_path']).with_suffix('.judged.jsonl'); rows=[]
    # Idempotent stage output is retained; an explicit new pipeline version is required to regenerate.
    if out.exists(): rows=[json.loads(l) for l in out.read_text().splitlines()]
    else:
        for line in Path(p['candidates_path']).read_text().splitlines()[:p.get('max_candidates',20)]:
            row=json.loads(line); row['judge']=await blocking(lambda:judge_candidate(row,p['judge_model'])); rows.append(row)
        out.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    return {'judged_path':str(out),'instance_ids':[r['instance_id'] for r in rows if r['judge']['keep']]}
@activity.defn
async def draft_activity(p):
    from swesim.stages.draft_spec import draft
    row=next(json.loads(l) for l in Path(p['judged_path']).read_text().splitlines() if json.loads(l)['instance_id']==p['instance_id'])
    sid=p['instance_id'].lower().replace('__','--'); path=WS.scenario_dir(sid)/'scenario.draft.json'
    if not path.exists(): await blocking(lambda:draft(row,row['judge'],p['draft_model'],path.parent))
    return {'scenario_id':sid,'draft_path':str(path)}
@activity.defn
async def auto_approve_activity(p):
    from swesim.stages.approve import approve_scenario
    return approve_scenario(WS, p['scenario_id'], auto=True)
@activity.defn
async def verify_approval_activity(p):
    spec=_spec(p['scenario_id']); approval=json.loads((WS.scenario_dir(spec.id)/'approval.json').read_text())
    if p['spec_hash']!=spec.spec_hash or approval['spec_hash']!=spec.spec_hash: raise ValueError('approval hash differs from frozen spec')
    return {'scenario_id':spec.id,'spec_hash':spec.spec_hash}
@activity.defn
async def compile_activity(p):
    from swesim.stages.compile import compile_scenario
    r=await blocking(lambda:compile_scenario(WS,_spec(p['scenario_id']),RunConfig.default('interactive'),p['stakeholder_url']))
    return {'compile_path':str(r.compile_json)}
@activity.defn
async def validate_activity(p):
    from swesim.stages.validate import validate_scenario
    r=await blocking(lambda:validate_scenario(WS,_spec(p['scenario_id']),RunConfig.default('full'),p['stakeholder_url']))
    return {'status':r['status'],'failures':r['failures']}
@activity.defn
async def register_activity(p):
    from swesim.stages.register import register_scenario
    return {'registry_entry':await blocking(lambda:register_scenario(WS,_spec(p['scenario_id'])))}
def plan_trials(p):
    spec=_spec(p['scenario_id']); comp=json.loads((WS.scenario_dir(spec.id)/'compile.json').read_text()); trials=[]
    for cond in p.get('conditions',['interactive']):
        for model in p['models']:
            for i in range(p.get('k',1)):
                profile=p.get('brief_profile','withheld')
                if cond=='full' or not any(b.profile==profile for b in spec.briefs): profile='natural'
                rc=RunConfig(condition=cond,model=model,compiled_hash=comp['compiled_hash'],purpose=p.get('purpose','demonstration'),brief_profile=profile)
                tid=trial_id(spec.spec_hash,cond,rc.run_config_hash,i)
                trials.append(dict(trial_id=tid,scenario_id=spec.id,spec_hash=spec.spec_hash,condition=cond,model=model,
                    run_config=rc.model_dump(),run_config_hash=rc.run_config_hash,attempt_index=i,stakeholder_url=p['stakeholder_url']))
    if len({t['trial_id'] for t in trials})!=len(trials): raise ValueError('duplicate matrix entries')
    return {'scenario_id':spec.id,'trials':trials}
@activity.defn
async def plan_trials_activity(p): return plan_trials(p)
@activity.defn
async def launch_trial_activity(p):
    tid=p['trial_id']; d=WS.root/'trials'/tid; d.mkdir(parents=True,exist_ok=True)
    request=d/'request.json'
    if request.exists() and json.loads(request.read_text())!=p: raise ValueError('trial binding conflict')
    atomic_json(request,p)
    unit='swesim-trial-'+tid
    # No credentials or arbitrary shell scripts are put in command arguments.
    proc=await blocking(lambda:subprocess.run(['systemd-run','--user','--unit='+unit,'--property=RemainAfterExit=yes',
        '--working-directory='+str(Path.cwd()),str(Path.cwd()/'.venv/bin/python'),'-m','swesim.workflows.trial_runner',str(request)],
        capture_output=True,text=True))
    atomic_json(d/'launch.json',{'trial_id':tid,'unit':unit,'returncode':proc.returncode,'stderr':proc.stderr[-1000:]})
    if proc.returncode: raise RuntimeError('launch requires reconciliation: '+unit)
    return {'trial_id':tid,'unit':unit}
@activity.defn
async def await_trial_activity(p):
    d=WS.root/'trials'/p['trial_id']; deadline=time.monotonic()+3*3600
    while time.monotonic()<deadline:
        activity.heartbeat()
        if (d/'record.json').exists():
            r=json.loads((d/'record.json').read_text()); return {'trial_id':r['trial_id'],'status':r['status'],'reward':r['reward']}
        if (d/'runner-error.json').exists(): return {'trial_id':p['trial_id'],'status':'runner-error'}
        await asyncio.sleep(10)
    raise TimeoutError('no terminal evidence; inspect same unit; never relaunch')
@activity.defn
async def score_activity(p):
    from swesim.stages.score import score_scenario
    return {'scores_path':str(await blocking(lambda:score_scenario(WS,p['scenario_id'])))}
@activity.defn
async def export_activity(p):
    from swesim.stages.export import export_scenario
    return {'manifest_paths':[str(x) for x in await blocking(lambda:export_scenario(WS,p['scenario_id']))]}
ALL=[mine_activity,judge_activity,draft_activity,auto_approve_activity,verify_approval_activity,compile_activity,validate_activity,
     register_activity,plan_trials_activity,launch_trial_activity,await_trial_activity,score_activity,export_activity]
```

```python
# swesim/workflows/workflows.py
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
ONE=RetryPolicy(maximum_attempts=1)
READ=RetryPolicy(maximum_attempts=3)
async def act(name,p,minutes=10):
    return await workflow.execute_activity(name,p,start_to_close_timeout=timedelta(minutes=minutes),
        heartbeat_timeout=timedelta(minutes=2),retry_policy=ONE)
@workflow.defn
class TrialWorkflow:
    @workflow.run
    async def run(self,p):
        try: await act('launch_trial_activity',p,5)
        except Exception:
            # Lost launch acknowledgement is ambiguous. Poll the same unit/id, never relaunch it.
            pass
        return await workflow.execute_activity('await_trial_activity',p,
            start_to_close_timeout=timedelta(hours=3),heartbeat_timeout=timedelta(minutes=2),retry_policy=READ)
@workflow.defn
class RunMatrixWorkflow:
    @workflow.run
    async def run(self,p):
        results=[]
        # Serial pilot runs are deliberate: global launch concurrency = 1, cheap recovery reasoning.
        for trial in p['trials']:
            r=await workflow.execute_child_workflow(TrialWorkflow.run,trial,id=trial['trial_id'],
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
            results.append(r)
        await act('score_activity',{'scenario_id':p['scenario_id']})
        await act('export_activity',{'scenario_id':p['scenario_id']})
        return {'trials':results}
@workflow.defn
class PipelineWorkflow:
    def __init__(self): self.approvals={}; self.state={'stage':'starting'}
    @workflow.signal
    def approve_spec(self,p:dict): self.approvals[p['scenario_id']]=p['spec_hash']
    @workflow.query
    def progress(self): return self.state
    @workflow.run
    async def run(self,p):
        mined=await act('mine_activity',p,180)
        judged=await act('judge_activity',{**p,**mined},60); out=[]
        if not judged['instance_ids']: return {'status':'no-valid-candidate','scenarios':[]}
        for iid in judged['instance_ids'][:p.get('max_scenarios',1)]:
            draft=await act('draft_activity',{**p,**judged,'instance_id':iid},30); sid=draft['scenario_id']
            if p.get('approval_mode','auto')=='auto':
                self.state={'stage':'auto-approving',**draft}
                approved=await act('auto_approve_activity',{'scenario_id':sid})   # code-owned gates; approval.json says auto-validators
                if not approved['approved']: out.append({'scenario_id':sid,'status':'auto-approval-rejected','reasons':approved['reasons']}); continue
                self.approvals[sid]=approved['spec_hash']
            else:
                self.state={'stage':'awaiting-approval',**draft}
                try: await workflow.wait_condition(lambda:sid in self.approvals,timeout=timedelta(hours=24))
                except TimeoutError: return {'status':'awaiting-approval-timeout','scenarios':out,'pending':draft}
            accepted=await act('verify_approval_activity',{'scenario_id':sid,'spec_hash':self.approvals[sid]})
            step={**p,**accepted}
            await act('compile_activity',step,30); validation=await act('validate_activity',step,180)
            if validation['status']!='valid': out.append({'scenario_id':sid,**validation}); continue
            await act('register_activity',step)
            trials=await act('plan_trials_activity',step)
            for trial in trials['trials']:
                await workflow.execute_child_workflow(TrialWorkflow.run,trial,id=trial['trial_id'],
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
            await act('score_activity',step); await act('export_activity',step)
            out.append({'scenario_id':sid,'status':'complete'})
        self.state={'stage':'complete'}; return {'status':'complete','scenarios':out}
```

```python
# swesim/workflows/worker.py
import asyncio, os
from temporalio.client import Client
from temporalio.worker import Worker
from swesim.workflows import activities as A
from swesim.workflows.workflows import TrialWorkflow, RunMatrixWorkflow, PipelineWorkflow
async def main():
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"))
    async with Worker(client, task_queue="swesim", workflows=[TrialWorkflow, RunMatrixWorkflow, PipelineWorkflow], activities=A.ALL, max_concurrent_activities=4):
        await asyncio.Event().wait()
if __name__ == "__main__": asyncio.run(main())
```

```python
# swesim/workflows/client.py
import os,uuid
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from swesim.workflows.workflows import PipelineWorkflow,RunMatrixWorkflow
async def connect(): return await Client.connect(os.environ.get("TEMPORAL_ADDRESS","localhost:7233"))
async def start_pipeline(params):
    c=await connect(); wid="pipeline-"+uuid.uuid4().hex
    await c.start_workflow(PipelineWorkflow.run,params,id=wid,task_queue="swesim",id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
    return wid
async def approve(workflow_id,scenario_id,spec_hash):
    c=await connect()
    await c.get_workflow_handle(workflow_id).signal(PipelineWorkflow.approve_spec,{"scenario_id":scenario_id,"spec_hash":spec_hash})
async def start_matrix(params):
    c=await connect(); wid="matrix-"+uuid.uuid4().hex
    await c.start_workflow(RunMatrixWorkflow.run,params,id=wid,task_queue="swesim",id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
    return wid
```

`swesim/workflows/trial_runner.py` (the per-trial systemd process, survives worker restart):
```python
import json,os,sys
from pathlib import Path
from dotenv import load_dotenv
from swesim.models import ScenarioSpec,RunConfig
from swesim.paths import Workspace,atomic_json
from swesim.stages.run import run_trial
p=json.loads(Path(sys.argv[1]).read_text()); ws=Workspace(os.environ.get('SWESIM_WORKSPACE','workspace'))
load_dotenv(os.environ.get('SWESIM_ENV_FILE',str(Path.home()/'.config/swesim/worker.env')))
try:
    spec=ScenarioSpec.model_validate_json((ws.scenario_dir(p['scenario_id'])/'scenario.json').read_text())
    rc=RunConfig.model_validate(p['run_config'])
    if spec.spec_hash!=p['spec_hash']: raise ValueError('frozen spec changed')
    run_trial(ws,spec,rc,p['attempt_index'],p['stakeholder_url'],os.environ['SWESIM_WORKER_SECRET'])
except Exception as e:
    atomic_json(ws.root/'trials'/p['trial_id']/'runner-error.json',{'trial_id':p['trial_id'],'error':repr(e)})
    raise
```

Add to `swesim/cli.py`:
```python
@app.command()
def worker():
    import asyncio
    from swesim.workflows.worker import main
    asyncio.run(main())
@app.command()
def pipeline(source:str='swebench-annotated',repo:str='',judge_model:str=typer.Option(...),draft_model:str=typer.Option(...),
             model:str=typer.Option(...),stakeholder_url:str=typer.Option(...,envvar='SWESIM_STAKEHOLDER_URL')):
    import asyncio
    from swesim.workflows.client import start_pipeline
    typer.echo(asyncio.run(start_pipeline({'source':source,'repo':repo,'judge_model':judge_model,'draft_model':draft_model,
        'models':[model],'conditions':['interactive'],'k':1,'stakeholder_url':stakeholder_url})))
@app.command('approve-signal')
def approve_signal(workflow_id:str,scenario_id:str,spec_hash:str):
    import asyncio
    from swesim.workflows.client import approve
    typer.echo(asyncio.run(approve(workflow_id,scenario_id,spec_hash)))
@app.command()
def matrix(scenario_id:str,models:str=typer.Option(...),conditions:str='full,hidden,interactive',k:int=3,purpose:str='comparison',brief_profile:str='withheld',
           stakeholder_url:str=typer.Option(...,envvar='SWESIM_STAKEHOLDER_URL')):
    import asyncio
    from swesim.workflows.activities import plan_trials
    from swesim.workflows.client import start_matrix
    # Invoke on the VM so plan_trials reads its approved specs/builds.
    p=plan_trials({'scenario_id':scenario_id,'models':models.split(','),'conditions':conditions.split(','),'k':k,
        'purpose':purpose,'brief_profile':brief_profile,'stakeholder_url':stakeholder_url})
    typer.echo(asyncio.run(start_matrix(p)))
```

- [ ] **Step 3: Offline tests, then explicit VM integration and restart evidence in Task 13**

```bash
uv run pytest tests/test_workflows.py -q
```
Create `tests/integration/test_temporal.py`:
```python
import asyncio,os,uuid
from temporalio import activity
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from swesim.workflows.workflows import TrialWorkflow
@activity.defn(name='launch_trial_activity')
async def launch(p):return {'trial_id':p['trial_id']}
@activity.defn(name='await_trial_activity')
async def collect(p):return {'trial_id':p['trial_id'],'status':'completed'}
def test_temporal_rejects_duplicate():
    async def run():
        async with await WorkflowEnvironment.start_time_skipping(test_server_existing_path=os.environ['TEMPORAL_TEST_SERVER']) as env:
            async with Worker(env.client,task_queue='swesim',workflows=[TrialWorkflow],activities=[launch,collect]):
                tid=uuid.uuid4().hex
                await env.client.execute_workflow(TrialWorkflow.run,{'trial_id':tid},id=tid,task_queue='swesim')
                try:
                    await env.client.start_workflow(TrialWorkflow.run,{'trial_id':tid},id=tid,task_queue='swesim',id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
                except WorkflowAlreadyStartedError:return
                raise AssertionError('duplicate completed workflow was accepted')
    asyncio.run(run())
```
Run explicitly on the VM after downloading the test-server binary from the
Temporal SDK's documented distribution; record the selected binary/version:
```bash
: "${TEMPORAL_TEST_SERVER:?Absolute path to preinstalled Temporal test server}"
uv run pytest tests/integration/test_temporal.py -q
```
This explicit integration check starts a local test server during implementation,
not during offline milestone verification or this plan review. The real worker
restart check remains Task 13; a mock cannot establish process survival.

- [ ] **Step 4: Commit**

```bash
git add swesim/workflows swesim/cli.py pyproject.toml tests/test_workflows.py
git commit -m "feat(temporal): thin pipeline and trial workflows with trial-id dedup, no launch retries, worker and client"
```

---

### Task 12: register, score, export

**Files:**
- Create: `swesim/stages/register.py`, `swesim/stages/score.py`, `swesim/stages/export.py`
- Modify: `swesim/cli.py` (add `register`, `score`, `export`)
- Create: `tests/test_score.py`

**Interfaces:**
- `register.register_scenario(ws, spec) -> dict` appends or updates `registry.json` (`{scenarios: {id: {version, spec_hash, source, source_ref, validation_status, validation_path, conditions, image_digest, calibration_runs: [], comparison_runs: []}}}`) and `workspace/dataset.json` (`[[tasks]] path = "workspace/tasks/<id>__<cond>"` entries).
- `score.pass_at_k(n, c, k) -> float` (= 1 − C(n−c,k)/C(n,k)); `score.pass_pow_k(n, c, k) -> float` (= C(c,k)/C(n,k); null if n<k); `score.score_scenario(ws, scenario_id) -> Path` writing `workspace/scenarios/<id>/scores.json`: per `(condition, model, run_config_hash)`: `n`, outcome counts, `success_count`, `pass_at_k`, `pass_pow_k`, `mean_reward`, `questions_asked`, `reviews`, `patience_end`, `reviewed_equals_submitted` (null in diff-only pilot), `labels: {}`.
- `export.export_scenario(ws, scenario_id) -> list[Path]` writing `workspace/exports/<trial_id>.episode.json` with `policy_visible` (instruction text, agent ATIF steps from `agent/trajectory.json`, stakeholder replies delivered), `evaluator_only` (ledger, oracle asset name, test asset names, report), `lineage` (spec_hash, run_config_hash, image_digest, patch_sha256, transcript_hash, harbor trial dir), `family_id` (source_ref), `simulator_validity`, `interaction`.

- [ ] **Step 1: Tests**

```python
# tests/test_score.py
from swesim.stages.score import pass_at_k, pass_pow_k, summarize
def test_pass_metrics():
    import pytest
    assert pass_at_k(3, 2, 1) == pytest.approx(2/3) and round(pass_at_k(3, 2, 3), 9) == 1.0 and pass_at_k(3, 0, 3) == 0.0
    assert pass_pow_k(3,2,3)==0 and pass_pow_k(3,3,3)==1
    assert pass_at_k(2,1,3) is None and pass_pow_k(2,1,3) is None
def test_summarize_outcomes():
    recs = [{"condition": "interactive", "run_config_hash": "r", "reward": {"success": 1, "reward": 1.0, "outcome_code": 1}, "transcript": {"events": [{"kind": "message", "role": "user", "exchange": "ask"}], "patience_end": 5, "patience_start": 6}, "report": {}, "status": "completed"},
            {"condition": "interactive", "run_config_hash": "r", "reward": {"success": 0, "reward": 0.5, "outcome_code": 2}, "transcript": {"events": []}, "report": {}, "status": "completed"},
            {"condition": "interactive", "run_config_hash": "r", "reward": {"outcome_code": 5}, "transcript": {}, "report": {}, "status": "completed"}]
    s = summarize(recs, k=3)["interactive|natural|r"]
    assert s["n_valid"] == 2 and s["success_count"] == 1 and s["outcomes"]["collection-failure"] == 1 and s["mean_reward"] == 0.75 and s["questions_asked"] == [1, 0]
```

- [ ] **Step 2: Implement**

```python
# swesim/stages/score.py
from __future__ import annotations
import json
from math import comb
from pathlib import Path
from swesim.paths import Workspace
OUTCOME_NAMES = {1: "valid-correct", 2: "valid-incorrect", 3: "agent-invalid-submission", 4: "agent-runtime-failure", 5: "collection-failure", 6: "simulator-failure", 7: "evaluator-failure"}
INFRA = {5, 6, 7}
def pass_at_k(n: int, c: int, k: int) -> float:
    if not 0<=c<=n or k<1: raise ValueError("invalid pass metric counts")
    if n == 0 or k > n: return None
    return 1.0 - comb(n - c, k) / comb(n, k)
def pass_pow_k(n: int, c: int, k: int) -> float | None:
    if not 0<=c<=n or k<1: raise ValueError("invalid pass metric counts")
    if n<k: return None
    return comb(c,k)/comb(n,k)  # probability all k succeed, without replacement
def summarize(records: list[dict], k: int) -> dict:
    groups: dict[str, list[dict]] = {}
    seen=set()
    for r in records:
        if r.get('trial_id'):
            if r['trial_id'] in seen: raise ValueError('duplicate attempt')
            seen.add(r['trial_id'])
        groups.setdefault(f"{r['condition']}|{r.get('brief_profile','natural')}|{r['run_config_hash']}",[]).append(r)
    out = {}
    for key, recs in groups.items():
        valid = [r for r in recs if r["reward"].get("outcome_code", 7) not in INFRA]; n = len(valid); c = sum(int(r["reward"].get("success", 0)) for r in valid)
        counts = {}
        for r in recs: counts[OUTCOME_NAMES.get(r["reward"].get("outcome_code", 7), "evaluator-failure")] = counts.get(OUTCOME_NAMES.get(r["reward"].get("outcome_code", 7), "evaluator-failure"), 0) + 1
        qs = [sum(1 for e in r.get("transcript", {}).get("events", []) if e.get("kind") == "message" and e.get("role") == "user" and e.get("exchange") == "ask") for r in valid]
        rv = [sum(1 for e in r.get("transcript", {}).get("events", []) if e.get("kind") == "message" and e.get("role") == "user" and e.get("exchange") == "review") for r in valid]
        out[key] = {"requested_k":k,"estimator":"without-replacement", "n_total": len(recs), "n_valid": n, "success_count": c, "pass_at_k": pass_at_k(n, c, k), "pass_pow_k": pass_pow_k(n, c, k),
                    "mean_reward": (sum(float(r["reward"].get("reward", 0)) for r in valid) / n) if n else 0.0, "outcomes": counts,
                    "questions_asked": qs, "reviews_requested": rv, "patience_end": [r.get("transcript", {}).get("patience_end") for r in valid],
                    "reviewed_equals_submitted": [None for r in valid],
                    "labels": {}}
    return out
def score_scenario(ws: Workspace, scenario_id: str, k: int = 3) -> Path:
    recs = []
    for rec_path in (ws.root / "trials").glob("*/record.json"):
        rec = json.loads(rec_path.read_text())
        if rec["scenario_id"] != scenario_id: continue
        recs.append(rec)
    # run_config_hash includes model/build/purpose; do not pool calibration and comparison.
    for rec in recs:
        if rec.get('trial_id') and not rec.get('spec_hash'): raise ValueError('missing frozen lineage')
    out = ws.scenario_dir(scenario_id) / "scores.json"; out.write_text(json.dumps(summarize(recs, k), indent=2)); return out
```

```python
# swesim/stages/register.py
import json,fcntl
from pathlib import Path
from swesim.paths import atomic_json

def register_scenario(ws,spec):
    sdir=ws.scenario_dir(spec.id); val=json.loads((sdir/'validation.json').read_text())
    comp=json.loads((sdir/'compile.json').read_text())
    if val['status']!='valid' or val['spec_hash']!=spec.spec_hash or val['compiled_hash']!=comp['compiled_hash']:
        raise ValueError('cannot register unvalidated or changed scenario')
    key=f"{spec.id}@{spec.version}"
    ws.registry_path.parent.mkdir(parents=True,exist_ok=True)
    with open(str(ws.registry_path)+'.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        reg=json.loads(ws.registry_path.read_text()) if ws.registry_path.exists() else {'scenarios':{}}
        previous=reg['scenarios'].get(key)
        if previous and previous['spec_hash']!=spec.spec_hash: raise ValueError('increment scenario version')
        entry=dict(id=spec.id,version=spec.version,spec_hash=spec.spec_hash,source=spec.source.value,source_ref=spec.source_ref,
            validation_status='valid',validation_path=str(sdir/'validation.json'),conditions=['full','hidden','interactive'],
            compiled_hash=comp['compiled_hash'],image_digest=comp.get('image_digest'),calibration_runs=[],comparison_runs=[],demonstration_runs=[])
        for p in (ws.root/'trials').glob('*/record.json'):
            r=json.loads(p.read_text())
            if r.get('spec_hash')==spec.spec_hash: entry[r['purpose']+'_runs'].append(r['trial_id'])
        reg['scenarios'][key]=entry; atomic_json(ws.registry_path,reg)
        # swesim manifest, not claimed to be Harbor's undocumented dataset.toml format.
        atomic_json(ws.root/'dataset.json',{'tasks':[str(ws.task_dir(e['id'],c)) for e in reg['scenarios'].values() for c in e['conditions']]})
    return entry
```

```python
# swesim/stages/export.py
from __future__ import annotations
import json
from pathlib import Path
from swesim.models import ScenarioSpec, Discovery
from swesim.paths import Workspace
def export_scenario(ws: Workspace, scenario_id: str) -> list[Path]:
    outs = []
    (ws.root / "exports").mkdir(parents=True, exist_ok=True)
    for rec_path in (ws.root / "trials").glob("*/record.json"):
        rec = json.loads(rec_path.read_text())
        if rec["scenario_id"] != scenario_id: continue
        tdir = rec_path.parent; traj = {}
        spec=ScenarioSpec.model_validate_json((tdir/"spec.json").read_text())
        if rec.get("trial_dir") and (Path(rec["trial_dir"]) / "agent" / "trajectory.json").exists(): traj = json.loads((Path(rec["trial_dir"]) / "agent" / "trajectory.json").read_text())
        instruction = (Path(rec["task_dir"]) / "instruction.md").read_text()
        replies = [e for e in rec.get("transcript", {}).get("events", []) if e.get("kind") == "message"]
        manifest = {"trial_id": rec["trial_id"], "family_id": spec.source_ref, "condition": rec["condition"], "brief_profile":rec["brief_profile"],
                    "policy_visible": {"instruction": instruction, "agent_trajectory": traj},
                    "training_eligible": bool(traj) and (rec['condition']!='interactive' or (rec['report'].get('transcript_binding_valid') is True and rec['report'].get('simulator_validity')=='ok')),
                    "evaluator_only": {"ledger": [f.model_dump() for f in spec.ledger], "author_only_fact_ids": [f.id for f in spec.ledger if f.discovery == Discovery.AUTHOR_ONLY],
                                       "oracle_asset": spec.controls.oracle, "test_assets": spec.contract.test_assets, "report": rec["report"], "stakeholder_audit":replies},
                    "lineage": {"spec_hash": spec.spec_hash, "run_config_hash": rec["run_config_hash"], "compiled_hash":rec["compiled_hash"], "image_digest":rec["report"].get("image_digest"), "purpose":rec["purpose"], "patch_sha256": rec["report"].get("patch_sha256"),
                                "transcript_hash": rec.get("transcript", {}).get("transcript_hash"), "harbor_trial_dir": rec.get("trial_dir")},
                    "reward": rec["reward"], "simulator_validity": "unadjudicated" if rec["condition"] == "interactive" else "n/a",
                    "interaction": {"patience_start": rec.get("transcript", {}).get("patience_start"), "patience_end": rec.get("transcript", {}).get("patience_end"),
                                    "questions": sum(1 for e in replies if e.get("role") == "user" and e.get("exchange") == "ask"), "reviews": sum(1 for e in replies if e.get("role") == "user" and e.get("exchange") == "review")}}
        p = ws.root / "exports" / f"{rec['trial_id']}.episode.json"; p.write_text(json.dumps(manifest, indent=2)); outs.append(p)
    return outs
```

- [ ] **Step 3: Wire CLI and run tests**

```python
# Add to swesim/cli.py
@app.command()
def register(scenario_id:str):
    from swesim.stages.register import register_scenario
    typer.echo(json.dumps(register_scenario(WS,_load(scenario_id))))
@app.command()
def score(scenario_id:str,k:int=3):
    from swesim.stages.score import score_scenario
    typer.echo(score_scenario(WS,scenario_id,k))
@app.command()
def export(scenario_id:str):
    from swesim.stages.export import export_scenario
    typer.echo([str(p) for p in export_scenario(WS,scenario_id)])
```



```bash
uv run pytest tests/test_score.py -q
git add swesim/stages/register.py swesim/stages/score.py swesim/stages/export.py swesim/cli.py tests/test_score.py
git commit -m "feat(score,export,register): pass@k and pass^k over success, outcome counts, episode manifests, registry and dataset.toml"
```

---

### Task 13: VM deployment (Temporal server, worker, dashboard) and the laptop-off acceptance test

**Files:**
- Create: `scripts/vm/create.sh`, `scripts/vm/setup.sh`, `scripts/vm/swesim-temporal.service`, `scripts/vm/swesim-worker.service`, `scripts/vm/swesim-dashboard.service`, `scripts/vm/tunnel.sh`, `docs/ops.md`

- [ ] **Step 1: Scripts**

**Interfaces:** GCE host stores the persistent Temporal DB and workspace;
user systemd trial units survive `swesim-worker.service` restarts. All ports
bind to loopback and are accessed through SSH. A git archive deploys the exact
commit without requiring a private-repo token on the VM. GitHub mining uses
a separately supplied read-only GitHub credential, never the local gh login store.

- [ ] **Step 0: Failing deployment-file tests first**
```bash
bash -n scripts/vm/create.sh scripts/vm/setup.sh scripts/vm/tunnel.sh
```
These fail until the files below exist. After creation also run
`systemd-analyze verify` on the rendered unit files on the VM before enabling.

`scripts/vm/create.sh`:
```bash
#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${GCP_PROJECT:?Set project}" "${GCP_ZONE:?Set zone}"
SWESIM_VM=${VM_NAME:-swesim-worker}
SWESIM_ARCHIVE=$(mktemp /tmp/swesim-source.XXXXXX.tar)
trap 'rm -f "$SWESIM_ARCHIVE"' EXIT
git archive --format=tar HEAD -o "$SWESIM_ARCHIVE"
gcloud compute instances create --quiet "$SWESIM_VM" --project "$GCP_PROJECT" --zone "$GCP_ZONE" --machine-type e2-standard-2 --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud --boot-disk-size 50GB
# Prepare ~/.config/swesim/worker.env with only required service/provider keys;
# mining.env contains a read-only GH_TOKEN. Both must already be mode 0600.
test -f "$HOME/.config/swesim/worker.env"
test -f "$HOME/.config/swesim/mining.env"
gcloud compute ssh --quiet "$SWESIM_VM" --project "$GCP_PROJECT" --zone "$GCP_ZONE" --command 'mkdir -p ~/.config/swesim ~/swesim && chmod 700 ~/.config/swesim'
gcloud compute scp --quiet "$SWESIM_ARCHIVE" "$SWESIM_VM":~/source.tar --project "$GCP_PROJECT" --zone "$GCP_ZONE"
gcloud compute scp --quiet "$HOME/.config/swesim/worker.env" "$HOME/.config/swesim/mining.env" "$SWESIM_VM":~/.config/swesim/ --project "$GCP_PROJECT" --zone "$GCP_ZONE"
gcloud compute scp --quiet "$HOME/.modal.toml" "$SWESIM_VM":~/.modal.toml --project "$GCP_PROJECT" --zone "$GCP_ZONE"
gcloud compute ssh --quiet "$SWESIM_VM" --project "$GCP_PROJECT" --zone "$GCP_ZONE" --command 'chmod 600 ~/.modal.toml ~/.config/swesim/*.env && tar -xf ~/source.tar -C ~/swesim && bash ~/swesim/scripts/vm/setup.sh'
```

`scripts/vm/setup.sh`:
```bash
#!/bin/bash
set -euo pipefail
sudo apt-get update
sudo apt-get install -y git gh curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
# Pin CLI version; inspect download result before extraction.
curl -fLsS 'https://temporal.download/cli/archive/v1.8.3?platform=linux&arch=amd64' -o /tmp/temporal.tar.gz
tar -xzf /tmp/temporal.tar.gz -C "$HOME/.local/bin" temporal
cd "$HOME/swesim"
uv sync --extra dev --frozen
mkdir -p "$HOME/.local/state/swesim" "$HOME/.config/systemd/user"
sudo loginctl enable-linger "$USER"
for unit in swesim-temporal swesim-worker swesim-dashboard; do
  sed "s|__HOME__|$HOME|g" "scripts/vm/$unit.service" > "$HOME/.config/systemd/user/$unit.service"
done
systemd-analyze --user verify "$HOME/.config/systemd/user/"*.service
systemctl --user daemon-reload
systemctl --user enable --now swesim-temporal swesim-worker swesim-dashboard
systemctl --user is-active swesim-temporal swesim-worker swesim-dashboard
```

`scripts/vm/swesim-temporal.service`:
```ini
[Unit]
Description=swesim persistent Temporal development server
[Service]
ExecStart=__HOME__/.local/bin/temporal server start-dev --db-filename __HOME__/.local/state/swesim/temporal.db --ip 127.0.0.1 --ui-port 8233
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
```

`scripts/vm/swesim-worker.service`:
```ini
[Unit]
Description=swesim Temporal worker
After=swesim-temporal.service
[Service]
WorkingDirectory=__HOME__/swesim
EnvironmentFile=__HOME__/.config/swesim/worker.env
EnvironmentFile=__HOME__/.config/swesim/mining.env
Environment=PATH=__HOME__/swesim/.venv/bin:__HOME__/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=SWESIM_ENV_FILE=__HOME__/.config/swesim/worker.env
ExecStart=__HOME__/swesim/.venv/bin/python -m swesim.workflows.worker
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
```

`scripts/vm/swesim-dashboard.service`:
```ini
[Unit]
Description=swesim episode viewer
[Service]
WorkingDirectory=__HOME__/swesim
EnvironmentFile=__HOME__/.config/swesim/worker.env
ExecStart=__HOME__/swesim/.venv/bin/uvicorn swesim.dashboard.app:app --host 127.0.0.1 --port 8080
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
```

`scripts/vm/tunnel.sh`:
```bash
#!/bin/bash
set -euo pipefail
: "${GCP_PROJECT:?}" "${GCP_ZONE:?}"
gcloud compute ssh --quiet "${VM_NAME:-swesim-worker}" --project "$GCP_PROJECT" --zone "$GCP_ZONE" -- -N -L 7233:localhost:7233 -L 8233:localhost:8233 -L 8080:localhost:8080
```

- [ ] **Step 2: Acceptance test (record actual evidence in docs/ops.md and workspace/evidence/ops.json)**

```bash
# Run commands on the VM, with worker.env loaded by the CLI's dotenv loader.
uv run swesim pipeline --source github-repo --repo https://github.com/py-pdf/pypdf --judge-model "$JUDGE_MODEL" --draft-model "$DRAFT_MODEL" --model "$SOLVER_MODEL" --stakeholder-url "$SWESIM_STAKEHOLDER_URL"
```
Close the laptop for 20 minutes. Reconnect and save Temporal's history JSON:
```bash
: "${WORKFLOW_ID:?Use the id printed by pipeline}"
temporal workflow show --workflow-id "$WORKFLOW_ID" --output json > workspace/evidence/pipeline-history.json
```
In unattended mode (`--approval-mode auto`, the default) the workflow approves
with the code-owned gates and continues; no human step exists. In supervised
mode (`--approval-mode human`) approval happens on the VM: download/edit/upload
the draft, then the human runs these with the pending ID from the workflow
query:
```bash
: "${SCENARIO_ID:?Pending scenario ID}"
uv run swesim approve "$SCENARIO_ID"
SWESIM_HASH=$(uv run python -c 'import json,sys; print(json.load(open("workspace/scenarios/"+sys.argv[1]+"/approval.json"))["spec_hash"])' "$SCENARIO_ID")
uv run swesim approve-signal "$WORKFLOW_ID" "$SCENARIO_ID" "$SWESIM_HASH"
```
Close the laptop again until the workflow has run a trial and exported it.
For recovery, restart **only** the worker while its trial unit is running:
```bash
systemctl --user restart swesim-worker
systemctl --user list-units 'swesim-trial-*' --all
```
Save the unit's journal, its launch.json, Harbor config/result, record.json,
export path and Temporal history. `ops.json` must contain `provider:"gce"`,
`laptop_off:{passed:true,workflow_id,history_path,record_path,export_path}` and
`worker_restart:{passed:true,trial_id,unit,journal_path,launch_count:1,record_path}`.
The values come from inspected artifacts; a prose assertion or the word
“acceptance” does not pass M8. A missing result after restart is a failure;
never restart the same solver to manufacture recovery evidence.

- [ ] **Step 3: Commit**

```bash
git add scripts/vm docs/ops.md
git commit -m "ops: GCE VM provisioning, systemd units for Temporal server, worker, dashboard; laptop-off acceptance test"
```

---

### Task 14: Dashboard (runs table, episode page)

**Files:**
- Create: `swesim/dashboard/__init__.py`, `swesim/dashboard/app.py`
- Create: `tests/test_dashboard.py`

**Interfaces:**
- `GET /` runs table from `registry.json`, `workspace/scenarios/*/scores.json`, `workspace/trials/*/status.json`; `GET /trial/{trial_id}` episode page from `record.json`, `transcript.json` (or live `GET /transcript` on the stakeholder service when `status == running`), Harbor `agent/trajectory.json`, `report.json`; `GET /api/trial/{trial_id}/timeline` JSON merged by timestamp: `[{ts, actor: agent|stakeholder|verifier, kind, text, meta}]`; `POST /trial/{trial_id}/label` stores `{label, note}` into `workspace/trials/<id>/label.json`.
- Timeline merge: ATIF steps carry `timestamp`; transcript events carry `ts`; sort ascending; stakeholder `tool_calls` become their own rows.

- [ ] **Step 1: Test**

```python
# tests/test_dashboard.py
from swesim.dashboard.app import merge_timeline

def test_merge_orders_by_time():
    traj={'steps':[{'timestamp':'2026-09-13T10:00:05Z','source':'agent','message':'reading'}]}
    from datetime import datetime
    t=datetime.fromisoformat('2026-09-13T10:00:00+00:00').timestamp()
    transcript={'events':[{'kind':'message','ts':t,'role':'user','content':'Which exception?','exchange':'ask'},
        {'kind':'message','ts':t+3,'role':'assistant','content':'ValueError.','tool_calls':[{'tool':'read_diff'}]}]}
    rows=merge_timeline(traj,transcript)
    assert [r['actor'] for r in rows]==['agent','stakeholder','stakeholder','agent']
    assert [r['ts'] for r in rows]==[t,t+3,t+3,t+5]
```

- [ ] **Step 2: Implement the two-page viewer**

```python
# swesim/dashboard/app.py
import json,os,re
from pathlib import Path
from datetime import datetime
from html import escape
from fastapi import FastAPI,HTTPException
from fastapi.responses import HTMLResponse
from swesim.paths import atomic_json
app=FastAPI(); ROOT=Path(os.environ.get('SWESIM_WORKSPACE','workspace')).resolve()
def read(p,default=None):
    try:return json.loads(p.read_text())
    except (OSError,ValueError):return default if default is not None else {}
def trial_path(tid):
    if not re.fullmatch('[a-f0-9]{24}',tid): raise HTTPException(404)
    return ROOT/'trials'/tid
def merge_timeline(traj,transcript):
    rows=[]
    for i,s in enumerate(traj.get('steps',[])):
        stamp=s.get('timestamp'); ts=datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp() if stamp else None
        rows.append(dict(ts=ts,seq=i,actor=s.get('source','agent'),kind='agent-step',text=str(s.get('message','')),meta=s))
    for i,e in enumerate(transcript.get('events',[])):
        if e.get('kind')!='message':continue
        rows.append(dict(ts=e['ts'],seq=i,actor='agent' if e['role']=='user' else 'stakeholder',
            kind=e.get('exchange','reply'),text=e['content'],meta=e))
        for tool in e.get('tool_calls',[]):rows.append(dict(ts=e['ts'],seq=i,actor='stakeholder',kind='tool_call',text=json.dumps(tool),meta=tool))
    return sorted(rows,key=lambda x:(x['ts'] is None,x['ts'] or 0,x['seq']))
@app.get('/',response_class=HTMLResponse)
def runs():
    links=[]
    for p in sorted((ROOT/'trials').glob('*/status.json')):
        r=read(p); links.append('<li><a href="/trial/'+p.parent.name+'">'+p.parent.name+'</a> '+escape(str(r.get('status')))+'</li>')
    return '<h1>swesim runs</h1><ul>'+''.join(links)+'</ul>'
@app.get('/api/trial/{tid}/timeline')
def timeline(tid:str):
    d=trial_path(tid); record=read(d/'record.json'); transcript=read(d/'transcript.json')
    if not record and read(d/'status.json').get('status')=='running':
        from swesim.stages.run import _get
        try: transcript=_get(os.environ['SWESIM_STAKEHOLDER_URL']+'/transcript/'+tid,{'X-Worker-Secret':os.environ['SWESIM_WORKER_SECRET']})
        except Exception: transcript={'events':[]}
    traj=read(Path(record['trial_dir'])/'agent/trajectory.json') if record.get('trial_dir') else {}
    return {'timeline':merge_timeline(traj,transcript),'report':record.get('report',{}),'status':read(d/'status.json')}
@app.get('/trial/{tid}',response_class=HTMLResponse)
def episode(tid:str):
    d=trial_path(tid)
    if not d.exists():raise HTTPException(404)
    ins=d/'task/instruction.md'; brief=escape(ins.read_text()) if ins.exists() else 'Preparing task'
    return '<h1>Episode '+tid+'</h1><pre>'+brief+'</pre><pre id="data"></pre><script>async function refresh(){const r=await fetch("/api/trial/'+tid+'/timeline");document.getElementById("data").textContent=JSON.stringify(await r.json(),null,2)};refresh();setInterval(refresh,5000);</script>'
@app.post('/trial/{tid}/label')
def label(tid:str,body:dict):
    d=trial_path(tid)
    if not d.exists():raise HTTPException(404)
    if body.get('label') not in {'unresolved','no-question','disclosed-not-used','simulator-omission','derived-without-asking'}:raise HTTPException(400)
    atomic_json(d/'label.json',{'label':body['label'],'note':str(body.get('note',''))[:2000]})
    return {'saved':True}
```

The page displays observable messages and report evidence; a JSON tree is
intentional for the pilot. Never label missing timestamps as chronological
facts, or an LLM annotation as proof of understanding. No live agent stream
is promised; the finished ATIF trajectory is interleaved once present.

- [ ] **Step 3: Run `uv run pytest tests/test_dashboard.py -q`, then `uv run uvicorn swesim.dashboard.app:app --port 8080` and open http://localhost:8080; commit**

```bash
git add swesim/dashboard tests/test_dashboard.py
git commit -m "feat(dashboard): runs table and episode page with merged agent/stakeholder/verifier timeline and near-live chat polling"
```

---

### Task 15: Tracked example package and sanitized run

**Files:**
- Create: `examples/README.md`, `examples/<scenario-id>/` (copy of `workspace/scenarios/<id>/scenario.json`, `assets/`, `validation.json`, the three compiled task dirs, `scores.json`), `examples/<scenario-id>/runs/<trial_id>/` (`record.json`, `reward.json`, `report.json`, `transcript.json`, `episode.json`, Harbor `agent/trajectory.json`, `verifier/`)
- Create: `scripts/snapshot_example.sh` (copies from workspace, strips `SWESIM_SESSION_TOKEN` values and any `Authorization` headers from copied files, verifies no `sk-` strings remain)

**Interfaces:** `snapshot_example.sh SID TID` validates inputs, copies required
frozen artifacts, redacts known credential values before writing, rejects
remaining credential-like values, writes `manifest.json` with file hashes,
and rewrites host paths as package-relative references. It never overwrites
an existing example without an explicit new destination.

- [ ] **Step 1: Failing snapshot test, then script**
```bash
bash -n scripts/snapshot_example.sh
```

`scripts/snapshot_example.sh`:
```bash
#!/bin/bash
set -euo pipefail
uv run python - "$1" "$2" <<'PYCODE'
import hashlib,json,os,re,shutil,sys
from pathlib import Path
from dotenv import dotenv_values
sid,tid=sys.argv[1:]
if not re.fullmatch('[a-z0-9][a-z0-9._-]{2,63}',sid) or not re.fullmatch('[a-f0-9]{24}',tid):raise SystemExit('invalid id')
src=Path('workspace/scenarios')/sid; trial=Path('workspace/trials')/tid; dst=Path('examples')/sid
if dst.exists():raise SystemExit('snapshot exists; use a new scenario version')
r=json.loads((trial/'record.json').read_text())
required=[src/'scenario.json',src/'validation.json',src/'scores.json',trial/'record.json',trial/'reward.json',trial/'report.json',trial/'transcript.json',Path('workspace/exports')/(tid+'.episode.json')]
if any(not p.is_file() for p in required):raise SystemExit('snapshot is incomplete')
if r['scenario_id']!=sid:raise SystemExit('run/scenario mismatch')
secrets=[v for k,v in dotenv_values(os.environ.get('SWESIM_ENV_FILE',str(Path.home()/'Documents/.env'))).items() if v and any(x in k for x in ('KEY','TOKEN','SECRET'))]
import tomllib
token=tomllib.loads((trial/'task/task.toml').read_text())['environment']['env'].get('SWESIM_SESSION_TOKEN','')
if token:secrets.append(token)
def copy_file(a,b):
    data=a.read_bytes()
    if b.suffix in {'.json','.toml','.txt','.log','.md','.sh','.py','.diff','.patch',''}:
        try:text=data.decode()
        except UnicodeDecodeError:raise SystemExit('unexpected binary log')
        for value in secrets:text=text.replace(value,'REDACTED')
        text=re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._-]+',r'\1REDACTED',text)
        if re.search(r'(sk-(?:ant|proj)-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{12,}',text):raise SystemExit('credential-like value remains')
        data=text.encode()
    b.parent.mkdir(parents=True,exist_ok=True);b.write_bytes(data)
for path in required:copy_file(path,dst/(path.name if path.parent==src else 'runs/'+tid+'/'+path.name))
for a in (src/'assets').rglob('*'):
    if a.is_file():copy_file(a,dst/'assets'/a.relative_to(src/'assets'))
for c in ['full','hidden','interactive']:
    base=Path('workspace/tasks')/(sid+'__'+c)
    if not base.exists():raise SystemExit('missing compiled condition')
    for a in base.rglob('*'):
        if a.is_file():copy_file(a,dst/('task__'+c)/a.relative_to(base))
for rel in ['agent/trajectory.json','verifier/submission.diff']:
    a=Path(r['trial_dir'])/rel
    if not a.is_file():raise SystemExit('missing '+rel)
    copy_file(a,dst/'runs'/tid/rel)
# Exports preserve hashes of originals and explicitly declare sanitization; never claim redacted bytes hash to the original.
manifest={p.relative_to(dst).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in dst.rglob('*') if p.is_file()}
(dst/'manifest.json').write_text(json.dumps({'sanitized':True,'files':manifest,'source_trial_id':tid},indent=2))
print(dst)
PYCODE
```

- [ ] **Step 2: `examples/README.md`**: how to re-run the example task with `harbor run -p examples/<id>/task__full -a oracle -e modal --yes` and how to read `validation.json`, `report.json`, and `episode.json`.

- [ ] **Step 3: Run the script for the first SWE-bench scenario and its interactive trial; commit**

```bash
git add examples scripts/snapshot_example.sh
git commit -m "examples: tracked scenario package with executed controls and one interactive run"
```

---

### Task 16: Documentation: README, grading.md, architecture.md; finalize CLAUDE.md and PLAN.md

**Files:**
- Modify: `README.md`; Create: `docs/grading.md`, `docs/architecture.md`; Modify: `CLAUDE.md`, `PLAN.md`

**Interfaces:** Documentation uses only the tracked example and measured
outcomes. It labels unsupported features and unverified claims, and supplies
commands that run from a fresh clone without author workspace paths.

- [ ] **Step 0: Run the final acceptance check before writing docs; it must fail for missing deliverables**
```bash
bash scripts/verify.sh 10
```

- [ ] **Step 1: README.md sections**: What this is (3 sentences); Install (`uv sync`, `uv sync --extra dev --frozen`, `modal token new`, `.env` keys, `bash scripts/deploy_stakeholder.sh`); Run the example (`harbor run -p examples/...`); Author a task from SWE-bench (mine, judge, draft, edit, approve, compile, validate, register, run, score, export with exact commands); Author from a repository URL (`swesim mine --source github-repo --repo ...` then the same); Run unattended on the VM (`scripts/vm/create.sh`, tunnel, `swesim pipeline`, approve-signal); View (Temporal UI, `harbor view`, dashboard); Layout; Limits and claims (link spec section 22).
- [ ] **Step 2: docs/grading.md**: reward and success definitions (spec 10.1, 10.5) with the actual `reward.json` and `report.json` from the example run; outcome classes and attribution table; the attack-versus-defense table with the executed control outcomes from `validation.json` pasted in; residual risks; how to add a control.
- [ ] **Step 3: docs/architecture.md**: condensed spec sections 1, 4, 5, 6, 8, 9, 11, 13, 14 with the topology figure from `docs/explainer/index.html` referenced; realism choices (natural underspecification, persona from thread, patience, review boundaries); prior-art table; claims made and not made.
Use these exact checks for the docs, fixing any command that differs from CLI help:
```bash
uv run swesim --help
uv run swesim mine --help
uv run swesim pipeline --help
uv run swesim matrix --help
bash scripts/verify.sh 10
```

- [ ] **Step 4: Finalize CLAUDE.md (commands, conventions, pitfalls learned during implementation) and check every PLAN.md milestone box that `scripts/verify.sh` confirms.**
- [ ] **Step 5: Commit and push; run `bash scripts/verify.sh 10`.**

```bash
git add README.md docs/grading.md docs/architecture.md CLAUDE.md PLAN.md
git commit -m "docs: README, grading, architecture; finalize CLAUDE.md and PLAN.md"
git push
```

---

## Deferred

- `checks` stakeholder review and its Modal Sandbox execution: removed to pay for durable sessions, transcript binding and grading controls. The frozen pilot mode is diff-only, explicitly unverified. Any later implementation must upload patch bytes to a file, never interpolate them into a shell heredoc; create Sandbox with `app=` when outside a Modal container, no secrets/network and a non-root check user.
- DeepSWE, Pro, SWE-rebench, Trac, non-pytest parsers, arbitrary-repo setup inference, multi-PR/multi-step tasks and CodeClash-style competitive rounds. The repo URL path remains required for the supported Python/pytest profile; unsupported repos get a recorded rejection.
- Third scenario, more than two model tiers, a 5,000-line minimum, elaborate dashboards, live agent streaming, automatic semantic failure labels and a learned patience penalty.
- Two-way uncertainty intervals and broad model rankings. Report exact counts and null pass@k/pass^k where fewer than k valid attempts exist; run at least one recorded attempt in each condition on both accepted tasks before expanding repeats.
- Trusted review-snapshot equivalence. Diff-only reviews may inspect an agent-supplied diff. `reviewed_equals_submitted` stays null; no content-equality claim until review and grading share a trusted snapshot protocol.
- Full VM-loss continuation of a live Harbor process. Worker restart recovery is required; machine-loss attempts are retained as interrupted and reconciled before a new attempt index is authorized.

## Verification boundaries

Read-only spec issues and APIs requiring real deployment checks are listed in
`docs/reviews/2026-09-12-codex-plan-review.md`. This plan does not claim that
separate grading prevents in-process JUnit/terminal forgery, that a broad
question cannot elicit a useful requirement list, or that larger diffs prove
higher model difficulty. CodeClash's iterative competition is a different
objective; this pilot measures implementation and use of stakeholder feedback.
