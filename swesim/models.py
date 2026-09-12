"""The normalized scenario record shared by every pipeline stage (spec section 5).

Every source produces a ScenarioSpec; compile, validate, run and score consume only it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Source(StrEnum):
    SWEBENCH_ANNOTATED = "swebench-annotated"
    GITHUB_PR = "github-pr"
    DEEPSWE = "deepswe"
    SWE_REBENCH = "swe-rebench"
    SWEBENCH_PRO = "swebench-pro"
    AUTHORED = "authored"


class FactKind(StrEnum):
    BEHAVIOR = "behavior"
    INTERFACE = "interface"
    CONSTRAINT = "constraint"
    REPRODUCTION = "reproduction"
    CONTEXT = "context"


class FactProvenance(StrEnum):
    ANNOTATOR_NOTE = "annotator-note"
    ISSUE_THREAD = "issue-thread"
    PR_THREAD = "pr-thread"
    PR_REVIEW = "pr-review"
    REQUIREMENTS_FIELD = "requirements-field"
    INSTRUCTION_BULLET = "instruction-bullet"
    AUTHORED = "authored"


class Discovery(StrEnum):
    BRIEF = "brief"
    ON_REQUEST = "on-request"
    VOLUNTEERED_AT_EVENT = "volunteered-at-event"
    REPO_DERIVABLE = "repo-derivable"
    AUTHOR_ONLY = "author-only"  # may not be required by the contract


class OperationType(StrEnum):
    CLARIFICATION = "clarification"
    EVALUATOR_REPAIR = "evaluator-repair"
    SYNTHETIC_EXTENSION = "synthetic-extension"


class Environment(BaseModel):
    kind: Literal["prebuilt-image", "dockerfile"]
    image: str | None = None
    repo_url: str
    base_commit: str
    language: str
    setup_notes: str = ""


class Brief(BaseModel):
    profile: str  # "full" | "natural" | authored profile name
    text: str
    provenance: Literal["original-issue", "authored"]


class Fact(BaseModel):
    id: str
    text: str
    kind: FactKind
    provenance: FactProvenance
    discovery: Discovery
    tests: list[str] = Field(default_factory=list)


class Operation(BaseModel):
    type: OperationType
    target: str
    rationale: str


class Persona(BaseModel):
    summary: str
    tone: str
    disclosure_style: str
    derived_from: str


class Requirement(BaseModel):
    id: str
    text: str
    fact_id: str | None = None
    tests: list[str] = Field(default_factory=list)
    source_comment_url: str | None = None


class Contract(BaseModel):
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    hidden_test_files: list[str]
    overlay_paths: list[str]
    review_fixtures: list[str] = Field(default_factory=list)
    runner: str


class Patch(BaseModel):
    name: str
    rationale: str
    diff: str


class Controls(BaseModel):
    oracle: Patch
    plausible_wrong: list[Patch] = Field(default_factory=list)
    alternative_correct: list[Patch] = Field(default_factory=list)
    tamper: list[Patch] = Field(default_factory=list)


class Difficulty(BaseModel):
    annotated: str | None = None
    judged: dict | None = None
    measured: dict | None = None


class ScenarioSpec(BaseModel):
    id: str
    version: int = 1
    source: Source
    source_ref: str
    created_at: datetime
    pipeline_version: str
    environment: Environment
    briefs: list[Brief]
    ledger: list[Fact]
    operations: list[Operation] = Field(default_factory=list)
    persona: Persona
    requirements: list[Requirement] = Field(default_factory=list)
    contract: Contract
    controls: Controls
    difficulty: Difficulty = Field(default_factory=Difficulty)
    thread: dict = Field(default_factory=dict)
