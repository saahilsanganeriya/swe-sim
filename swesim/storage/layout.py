"""Layout of the record (interfaces §1): the bucket *is* the workspace, mounted at `SWESIM_WORKSPACE`.

Contract
- Every path under the record root is derived by a method here; callers never join path segments themselves. The same
  method names are used on a laptop directory, on the VM and on the bucket mount, so a path string is portable.
- Ids are validated before they touch the filesystem: scenario ids against `swesim.models.ID_RE`, trial ids against
  `^[0-9a-f]{24}$`; file names against the closed `Literal` sets below. Anything else raises `LayoutError` (a
  `ValueError`: invalid input, never retried).
- `relative(path)` gives the bucket object name (POSIX, relative to root) for any path under the root.
- Nothing here reads or writes files; `atomic_json` stays in `swesim.paths`.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import field_validator

from swesim.models import ID_RE, Strict

ScenarioFile = Literal["scenario.json", "scenario.draft.json", "draft.meta.json", "judge.json", "approval.json", "compile.json",
                       "validation.json", "difficulty.json", "audit.json", "scores.json"]
TrialFile = Literal["request.json", "launch.json", "status.json", "record.json", "report.json", "reward.json", "run_config.json",
                    "spec.json", "transcript.json", "runner-error.json", "launcher.log", "costs.json", "audit.json"]
CandidateStage = Literal["mined", "judged", "probed", "probe_summary", "rejects", "status"]
JobKind = Literal["probe", "runs", "validate"]
Condition = Literal["full", "hidden", "interactive"]

SCENARIO_FILES: frozenset[str] = frozenset(ScenarioFile.__args__)
TRIAL_FILES: frozenset[str] = frozenset(TrialFile.__args__)
CANDIDATE_STAGES: frozenset[str] = frozenset(CandidateStage.__args__)
JOB_KINDS: frozenset[str] = frozenset(JobKind.__args__)
CONDITIONS: tuple[str, ...] = Condition.__args__
EVIDENCE_KINDS: tuple[str, ...] = ("cloudrun", "cloud", "sweep", "console", "audit")  # directories; the model probe is one file

TRIAL_ID_RE = re.compile(r"^[0-9a-f]{24}$")
# One path segment: no separators, no `.`/`..`, the characters the pipeline uses for keys, instance ids and evidence kinds.
SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Temporal workflow ids (`create:<sid>@v2`, `run:<...>`, trial ids) as one path segment: `:` and `@` allowed, no separators.
WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")
_CANDIDATE_SUFFIX: dict[str, str] = {"mined": ".jsonl", "judged": ".judged.jsonl", "probed": ".judged.probed.jsonl",
                                     "probe_summary": ".judged.probed.summary.json", "rejects": ".rejects.json", "status": ".status"}


class LayoutError(ValueError):
    """An id, name or path that the layout refuses to derive a location for."""


def _segment(value: str, what: str) -> str:
    if not isinstance(value, str) or not SEGMENT_RE.fullmatch(value) or value in (".", ".."):
        raise LayoutError(f"invalid {what}: {value!r}")
    return value


def scenario_id(value: str) -> str:
    """The validated scenario id (the same rule as `ScenarioSpec.id`)."""
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise LayoutError(f"invalid scenario id: {value!r}")
    return value


def trial_id(value: str) -> str:
    """The validated trial id: 24 lowercase hex characters (`swesim.models.trial_id`)."""
    if not isinstance(value, str) or not TRIAL_ID_RE.fullmatch(value):
        raise LayoutError(f"invalid trial id: {value!r}")
    return value


def model_slug(model_id: str) -> str:
    """A path segment for a model id: `openai/gpt-5.5` -> `openai--gpt-5.5`."""
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", model_id.replace("/", "--"))
    return _segment(slug, "model slug")


class Layout(Strict):
    """All record paths, derived from one root."""

    root: Path

    @field_validator("root", mode="before")
    @classmethod
    def _as_path(cls, value: object) -> Path:
        if isinstance(value, Path):
            return value
        if isinstance(value, str) and value:
            return Path(value)
        raise ValueError("root must be a path")

    # ------------------------------------------------------------------ scenarios
    def scenario_dir(self, scenario_id_: str) -> Path:
        return self.root / "scenarios" / scenario_id(scenario_id_)

    def scenario_file(self, scenario_id_: str, name: ScenarioFile) -> Path:
        if name not in SCENARIO_FILES:
            raise LayoutError(f"not a scenario record file: {name!r}")
        return self.scenario_dir(scenario_id_) / name

    def scenario_assets_dir(self, scenario_id_: str) -> Path:
        return self.scenario_dir(scenario_id_) / "assets"

    def scenario_archive_dir(self, scenario_id_: str, version: int) -> Path:
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise LayoutError(f"invalid scenario version: {version!r}")
        return self.scenario_dir(scenario_id_) / "archive" / f"v{version}"

    # ------------------------------------------------------------------ compiled tasks
    def task_dir(self, scenario_id_: str, condition: Condition) -> Path:
        if condition not in CONDITIONS:
            raise LayoutError(f"invalid condition: {condition!r}")
        return self.root / "tasks" / f"{scenario_id(scenario_id_)}__{condition}"

    # ------------------------------------------------------------------ trials
    def trial_dir(self, trial_id_: str) -> Path:
        return self.root / "trials" / trial_id(trial_id_)

    def trial_file(self, trial_id_: str, name: TrialFile) -> Path:
        if name not in TRIAL_FILES:
            raise LayoutError(f"not a trial record file: {name!r}")
        return self.trial_dir(trial_id_) / name

    def trial_task_dir(self, trial_id_: str) -> Path:
        """The per-trial task copy; it holds the session token, so the console never lists it."""
        return self.trial_dir(trial_id_) / "task"

    # ------------------------------------------------------------------ exports
    def export_path(self, trial_id_: str) -> Path:
        return self.root / "exports" / f"{trial_id(trial_id_)}.episode.json"

    # ------------------------------------------------------------------ candidates
    def candidates_dir(self) -> Path:
        return self.root / "candidates"

    def candidates_file(self, source_key: str, stage: CandidateStage) -> Path:
        if stage not in CANDIDATE_STAGES:
            raise LayoutError(f"invalid candidate stage: {stage!r}")
        return self.candidates_dir() / (_segment(source_key, "source key") + _CANDIDATE_SUFFIX[stage])

    def probe_dir(self, instance_id: str) -> Path:
        return self.root / "probes" / _segment(instance_id, "instance id")

    # ------------------------------------------------------------------ evidence, calibration, jobs
    def evidence_dir(self, kind: str) -> Path:
        return self.root / "evidence" / _segment(kind, "evidence kind")

    def model_probe_path(self) -> Path:
        """The resolved model roles (`probe_models_activity`): one file, `root/evidence/model-probe.json`."""
        return self.root / "evidence" / "model-probe.json"

    def calibration_path(self, judge_model: str, day: date) -> Path:
        if not isinstance(day, date):
            raise LayoutError(f"invalid calibration day: {day!r}")
        return self.root / "calibration" / model_slug(judge_model) / f"{day.isoformat()}.json"

    def jobs_dir(self, kind: JobKind) -> Path:
        if kind not in JOB_KINDS:
            raise LayoutError(f"invalid job kind: {kind!r}")
        return self.root / "jobs" / kind

    # ------------------------------------------------------------------ costs outside a trial (swesim/costs.py)
    def costs_dir(self) -> Path:
        return self.root / "costs"

    def workflow_costs_path(self, workflow_id: str) -> Path:
        """Cost records of a workflow that has no trial (judge, drafter, audit...): `root/costs/<workflow_id>.json`."""
        if not isinstance(workflow_id, str) or not WORKFLOW_ID_RE.fullmatch(workflow_id) or workflow_id in (".", ".."):
            raise LayoutError(f"invalid workflow id: {workflow_id!r}")
        return self.costs_dir() / f"{workflow_id}.json"

    def registry_path(self) -> Path:
        """The record's registry; `Settings.registry_path` may point elsewhere on a laptop."""
        return self.root / "registry.json"

    def dataset_path(self) -> Path:
        return self.root / "dataset.json"

    # ------------------------------------------------------------------ object names
    def relative(self, path: Path) -> str:
        """Bucket object name (POSIX, relative to root) for a path under the root."""
        try:
            return Path(path).relative_to(self.root).as_posix()
        except ValueError as e:
            raise LayoutError(f"{path} is not under {self.root}") from e
