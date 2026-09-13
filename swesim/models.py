"""ScenarioSpec v2 and RunConfig (spec section 5). Strict: extra fields forbidden."""
from __future__ import annotations
import hashlib, json, re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    model_id: str = "anthropic/claude-opus-5"; prompt_version: str = "v3"; temperature: float = 0.0  # v3 = v2 prompt + Opus 5 (brief §3.1); v3 trials never pool with v1/v2
    patience_profile: PatienceProfile = Field(default_factory=PatienceProfile)
    review_mode: Literal["behavior", "diff-only"] = "behavior"; max_tool_calls_per_activation: int = Field(2, ge=0, le=4)

class RunConfig(Strict):
    agent: str = "mini-swe-agent"; agent_version: str | None = "2.4.6"
    model: str = "anthropic/claude-haiku-4-5-20251001"
    condition: Literal["full", "hidden", "interactive"]
    brief_profile: Literal["natural", "withheld"] = "withheld"
    solver_timeout_sec: int = Field(2400, ge=60)
    purpose: Literal["calibration", "comparison", "demonstration", "screen"] = "demonstration"  # screen = difficulty screen (brief §5)
    compiled_hash: str = ""  # required for actual run; contract/environment content hash, excluding condition prose
    agent_config: str = ""   # agent scaffold identity (e.g. "mini-swe-agent.interactive:<sha16>"); empty = the agent's packaged defaults
    stakeholder: StakeholderConfig = Field(default_factory=StakeholderConfig)
    k: int = 3; harbor_version: str = "0.23.0"; environment_provider: Literal["modal"] = "modal"
    @property
    def run_config_hash(self) -> str: return canonical_hash(self)
    @classmethod
    def default(cls, condition: str) -> "RunConfig": return cls(condition=condition)

def canonical_hash(model: BaseModel) -> str:
    data = model.model_dump(mode="json", exclude={"spec_hash", "run_config_hash"})
    if data.get("agent_config") == "":  # added after the first runs; an unset scaffold must not change earlier run_config hashes
        data.pop("agent_config")
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
