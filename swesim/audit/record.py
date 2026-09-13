"""Reading a scenario or a trial from the record for the audit: the frozen files, the agent trajectory, the deny-list.

Contract
- Paths come from the layout passed in (``swesim.storage.layout.Layout`` once merged, ``swesim.paths.Workspace`` today):
  ``root`` and ``scenario_dir(sid)`` are required; ``trial_dir``/``export_path``/``registry_path`` are used when present and
  derived from ``root`` otherwise. Ids are validated here; a bad id or a missing required file raises ``AuditInputError``
  (a ``ValueError``: the caller's input, never retried).
- A trial bundle carries only what exists: the frozen ``spec.json`` (else the scenario's current ``scenario.json``),
  ``run_config.json``, ``record.json``, ``report.json``, ``transcript.json`` (required for interactive trials) and the
  first trajectory found among ``record.trial_dir/agent/trajectory.json``, ``<root>/jobs/runs/<tid>/*/agent/trajectory.json``
  and the export's ``policy_visible.agent_trajectory`` (``trajectory_source`` says which; both are actual agent
  observations). Nothing is inferred when a file is absent: the field stays ``None``.
- ``forbidden_terms`` is recomputed with ``swesim.stakeholder.view.forbidden_terms`` from the scenario assets that match
  the trial's frozen spec (current dir when the spec hash matches, else ``archive/v<version>``), so the audit measures the
  same deny-list the session used; when neither is on disk the list is empty and ``forbidden_terms_source`` says so.
- The stakeholder version is evidence-based (``stakeholder_version``): ``run_config.stakeholder.prompt_version`` when it
  says v2, else the v2 reply filter's annotations on the transcript's replies, else ``None`` when there was no exchange.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from swesim.models import Discovery, OperationType, ScenarioSpec
from swesim.stakeholder.session import CONFIRM_PREFIX
from swesim.stakeholder.view import forbidden_terms as compute_forbidden_terms

TRIAL_ID_RE = re.compile(r"^[0-9a-f]{24}$")
V2_ANNOTATION_KEYS = ("regenerated", "redacted_terms")


class AuditInputError(ValueError):
    """The record lacks what the audit needs (bad id, missing scenario/trial/transcript/spec). Non-retryable."""


class RecordPaths:
    """Path derivation over whichever layout object the caller has (see the module contract)."""

    def __init__(self, layout) -> None:
        self.layout = layout
        self.root = Path(layout.root)

    def scenario_dir(self, scenario_id: str) -> Path:
        try:
            return Path(self.layout.scenario_dir(scenario_id))
        except ValueError as e:
            raise AuditInputError(f"invalid scenario id {scenario_id!r}") from e

    def trial_dir(self, trial_id: str) -> Path:
        if not isinstance(trial_id, str) or not TRIAL_ID_RE.match(trial_id):
            raise AuditInputError(f"invalid trial id {trial_id!r}")
        fn = getattr(self.layout, "trial_dir", None)
        return Path(fn(trial_id)) if callable(fn) else self.root / "trials" / trial_id

    def export_path(self, trial_id: str) -> Path:
        fn = getattr(self.layout, "export_path", None)
        return Path(fn(trial_id)) if callable(fn) else self.root / "exports" / f"{trial_id}.episode.json"

    def registry_path(self) -> Path:
        rp = getattr(self.layout, "registry_path", None)
        if callable(rp):
            return Path(rp())
        return Path(rp) if rp else self.root / "registry.json"

    def runs_dir(self, trial_id: str) -> Path:
        return self.root / "jobs" / "runs" / trial_id

    def trials_root(self) -> Path:
        return self.trial_dir("0" * 24).parent

    def scenarios_root(self) -> Path:
        return self.root / "scenarios"


def _read_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _fact_view(f) -> dict:
    return {"id": f.id, "text": f.text, "kind": f.kind.value, "discovery": f.discovery.value, "provenance": f.provenance.value}


def _spec_views(spec: ScenarioSpec) -> dict:
    natural = next((b.text for b in spec.briefs if b.profile == "natural"), "")
    withheld = next((b.text for b in spec.briefs if b.profile == "withheld"), None)
    facts = {f.id: f for f in spec.ledger}
    removed = []
    for op in spec.operations:
        if op.type != OperationType.WITHHOLDING:
            continue
        f = facts.get(op.target)
        removed.append({"fact_id": op.target, "text": f.text if f else None, "excerpt": f.source_excerpt if f else None,
                        "rationale": op.rationale})
    return {
        "natural_brief": natural, "withheld_brief": withheld, "removed_spans": removed,
        "visible_facts": [_fact_view(f) for f in spec.ledger if f.discovery != Discovery.AUTHOR_ONLY],
        "author_only_facts": [_fact_view(f) for f in spec.ledger if f.discovery == Discovery.AUTHOR_ONLY],
        "requirements": [{"id": r.id, "text": r.text, "fact_id": r.fact_id, "tests": list(r.tests)} for r in spec.requirements],
    }


@dataclass
class ScenarioBundle:
    scenario_id: str
    spec: ScenarioSpec
    test_assets: dict[str, str]
    oracle_diff: str | None
    forbidden_terms: list[str]
    validation_status: str | None
    asset_hashes: dict[str, str]
    views: dict = field(default_factory=dict)

    @classmethod
    def from_spec(cls, scenario_id: str, spec: ScenarioSpec, *, test_assets: dict[str, str], oracle_diff: str | None,
                  forbidden_terms: list[str], validation_status: str | None, asset_hashes: dict[str, str]) -> "ScenarioBundle":
        return cls(scenario_id, spec, dict(test_assets), oracle_diff, list(forbidden_terms), validation_status, dict(asset_hashes))

    def __post_init__(self) -> None:
        self.views = _spec_views(self.spec)

    @property
    def spec_hash(self) -> str:
        return self.spec.spec_hash

    @property
    def natural_brief(self) -> str:
        return self.views["natural_brief"]

    @property
    def withheld_brief(self) -> str | None:
        return self.views["withheld_brief"]

    @property
    def removed_spans(self) -> list[dict]:
        return self.views["removed_spans"]

    @property
    def visible_facts(self) -> list[dict]:
        return self.views["visible_facts"]

    @property
    def author_only_facts(self) -> list[dict]:
        return self.views["author_only_facts"]

    @property
    def requirements(self) -> list[dict]:
        return self.views["requirements"]

    @property
    def fail_to_pass(self) -> list[str]:
        return list(self.spec.contract.fail_to_pass)

    @property
    def pass_to_pass(self) -> list[str]:
        return list(self.spec.contract.pass_to_pass)


def _assets_dir_for(paths: RecordPaths, spec: ScenarioSpec) -> tuple[Path | None, str]:
    """The scenario directory whose assets belong to this spec: the current one when the spec hash matches, else the
    archived version; (None, reason) when neither is on disk."""
    sdir = paths.scenario_dir(spec.id)
    current = _read_json(sdir / "scenario.json")
    if isinstance(current, dict):
        try:
            if ScenarioSpec.model_validate(current).spec_hash == spec.spec_hash:
                return sdir, "scenario dir (current spec)"
        except ValidationError:
            pass
    archived = sdir / "archive" / f"v{spec.version}"
    if (archived / "assets").exists():
        return archived, f"archive/v{spec.version}"
    if (sdir / "assets").exists() and current is None:
        return sdir, "scenario dir (no current scenario.json to compare)"
    return None, f"no assets on disk for spec {spec.spec_hash[:8]} (version {spec.version})"


def _read_assets(spec: ScenarioSpec, sdir: Path) -> tuple[dict[str, str], str | None, dict[str, str]]:
    tests: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for name in spec.contract.test_assets:
        asset = spec.assets.get(name)
        text = _read_text(sdir / asset.path) if asset else None
        if text is None:
            raise AuditInputError(f"test asset {name!r} of {spec.id} is not readable under {sdir}")
        tests[name] = text
        hashes[name] = _sha(text)
    oracle = None
    asset = spec.assets.get(spec.controls.oracle)
    if asset is not None:
        oracle = _read_text(sdir / asset.path)
        if oracle is not None:
            hashes[spec.controls.oracle] = _sha(oracle)
    return tests, oracle, hashes


def _forbidden(spec: ScenarioSpec, sdir: Path) -> list[str]:
    try:
        return compute_forbidden_terms(spec, sdir)
    except (OSError, KeyError):  # an asset listed in the spec is missing from this directory
        return []


def load_scenario(layout, scenario_id: str) -> ScenarioBundle:
    """The scenario's current spec with its hidden tests, oracle diff, deny-list and validation status."""
    paths = RecordPaths(layout)
    sdir = paths.scenario_dir(scenario_id)
    raw = _read_json(sdir / "scenario.json")
    if not isinstance(raw, dict):
        raise AuditInputError(f"scenario.json not found for {scenario_id} under {sdir}")
    try:
        spec = ScenarioSpec.model_validate(raw)
    except ValidationError as e:
        raise AuditInputError(f"scenario.json of {scenario_id} is not a valid ScenarioSpec: {e}") from e
    tests, oracle, hashes = _read_assets(spec, sdir)
    validation = _read_json(sdir / "validation.json")
    status = validation.get("status") if isinstance(validation, dict) else None
    return ScenarioBundle.from_spec(scenario_id, spec, test_assets=tests, oracle_diff=oracle, forbidden_terms=_forbidden(spec, sdir),
                                    validation_status=status if isinstance(status, str) else None, asset_hashes=hashes)


# ---------------------------------------------------------------- trials

def stakeholder_version(run_config: dict, transcript: dict | None) -> tuple[str | None, str]:
    """("v1" | "v2" | None, evidence). See the module contract."""
    pv = ((run_config or {}).get("stakeholder") or {}).get("prompt_version")
    if pv == "v2":
        return "v2", "run_config.stakeholder.prompt_version"
    replies = [e for e in ((transcript or {}).get("events") or [])
               if isinstance(e, dict) and e.get("kind") == "message" and e.get("role") == "assistant"]
    if not replies:
        return None, "no exchange in the transcript to tell the stakeholder version from"
    if any(k in e for e in replies for k in V2_ANNOTATION_KEYS) or any(e.get("exchange") == "confirm" for e in replies):
        return "v2", "replies carry the v2 reply-filter annotations (regenerated/redacted_terms) or the confirm channel"
    return "v1", f"run_config.stakeholder.prompt_version={pv!r} and replies carry no filter annotations"


def find_trajectory(paths: RecordPaths, trial_id: str, record: dict) -> tuple[dict | None, str | None]:
    """The agent's ATIF trajectory and where it came from (``harbor``, ``jobs/runs``, ``export``); (None, None) if absent."""
    tdir = record.get("trial_dir") if isinstance(record, dict) else None
    if isinstance(tdir, str) and tdir:
        traj = _read_json(Path(tdir) / "agent" / "trajectory.json")
        if isinstance(traj, dict) and traj.get("steps"):
            return traj, "harbor"
    runs = paths.runs_dir(trial_id)
    if runs.exists():
        for p in sorted(runs.glob("*/agent/trajectory.json")):
            traj = _read_json(p)
            if isinstance(traj, dict) and traj.get("steps"):
                return traj, "jobs/runs"
    export = _read_json(paths.export_path(trial_id))
    if isinstance(export, dict):
        traj = (export.get("policy_visible") or {}).get("agent_trajectory")
        if isinstance(traj, dict) and traj.get("steps"):
            return traj, "export"
    return None, None


def submission_paths(report: dict) -> list[str]:
    """Files the submission touched, as the grader recorded them: applied, dropped (protected) and refused paths."""
    out: list[str] = []
    for key in ("applied_paths", "dropped_paths", "refused_paths"):
        for p in (report or {}).get(key) or []:
            if isinstance(p, str) and p and p not in out:
                out.append(p)
    return out


def strip_confirm_prefix(text: str) -> str:
    return text[len(CONFIRM_PREFIX):] if isinstance(text, str) and text.startswith(CONFIRM_PREFIX) else (text or "")


@dataclass
class TrialBundle:
    trial_id: str
    spec: ScenarioSpec
    run_config: dict
    record: dict
    report: dict
    transcript: dict
    trajectory: dict | None
    trajectory_source: str | None
    forbidden_terms: list[str]
    test_assets: dict[str, str]
    forbidden_terms_source: str = ""
    views: dict = field(default_factory=dict)
    stakeholder_version: str | None = None
    version_evidence: str = ""

    @classmethod
    def from_parts(cls, trial_id: str, spec: ScenarioSpec, *, run_config: dict, record: dict, report: dict, transcript: dict,
                   trajectory: dict | None, trajectory_source: str | None, forbidden_terms: list[str], test_assets: dict[str, str],
                   forbidden_terms_source: str = "") -> "TrialBundle":
        return cls(trial_id, spec, dict(run_config or {}), dict(record or {}), dict(report or {}), dict(transcript or {}),
                   trajectory, trajectory_source, list(forbidden_terms), dict(test_assets), forbidden_terms_source)

    def __post_init__(self) -> None:
        self.views = _spec_views(self.spec)
        self.stakeholder_version, self.version_evidence = stakeholder_version(self.run_config, self.transcript)

    # lineage
    @property
    def scenario_id(self) -> str:
        return self.record.get("scenario_id") or self.spec.id

    @property
    def spec_hash(self) -> str:
        return self.record.get("spec_hash") or self.spec.spec_hash

    @property
    def condition(self) -> str | None:
        return self.run_config.get("condition") or self.record.get("condition")

    @property
    def interactive(self) -> bool:
        return self.condition == "interactive"

    @property
    def brief_profile(self) -> str | None:
        return self.run_config.get("brief_profile") or self.record.get("brief_profile")

    @property
    def solver_model(self) -> str | None:
        return self.run_config.get("model") or self.record.get("model")

    @property
    def legacy(self) -> bool:
        return self.stakeholder_version == "v1"

    @property
    def submission_paths(self) -> list[str]:
        return submission_paths(self.report)

    @property
    def integrity_signals(self) -> list:
        return list(self.report.get("integrity_signals") or [])

    @property
    def transcript_hash(self) -> str | None:
        return self.transcript.get("transcript_hash")

    # transcript
    @property
    def messages(self) -> list[dict]:
        events = [e for e in (self.transcript.get("events") or []) if isinstance(e, dict) and e.get("kind") == "message"]
        return sorted(events, key=lambda e: e.get("seq", 0))

    @property
    def questions(self) -> list[dict]:
        return [e for e in self.messages if e.get("role") == "user" and e.get("exchange") == "ask"]

    @property
    def agent_turns(self) -> list[dict]:
        return [e for e in self.messages if e.get("role") == "user"]

    @property
    def replies(self) -> list[dict]:
        return [e for e in self.messages if e.get("role") == "assistant"]

    def prompt_for(self, reply: dict) -> str:
        """The agent's text the stakeholder answered in this exchange (confirm prefix stripped)."""
        for e in self.agent_turns:
            if e.get("request_id") == reply.get("request_id"):
                return strip_confirm_prefix(e.get("content", ""))
        return ""

    # spec views
    @property
    def natural_brief(self) -> str:
        return self.views["natural_brief"]

    @property
    def brief_seen_by_agent(self) -> str:
        if self.brief_profile == "withheld" and self.views["withheld_brief"]:
            return self.views["withheld_brief"]
        return self.views["natural_brief"]

    @property
    def visible_facts(self) -> list[dict]:
        return self.views["visible_facts"]

    @property
    def requirements(self) -> list[dict]:
        return self.views["requirements"]

    @property
    def fail_to_pass(self) -> list[str]:
        return list(self.spec.contract.fail_to_pass)


def _spec_for_trial(paths: RecordPaths, tdir: Path, record: dict) -> ScenarioSpec:
    raw = _read_json(tdir / "spec.json")
    source = "trial spec.json"
    if not isinstance(raw, dict):
        sid = record.get("scenario_id")
        raw = _read_json(paths.scenario_dir(sid) / "scenario.json") if isinstance(sid, str) and sid else None
        source = "scenario.json"
    if not isinstance(raw, dict):
        raise AuditInputError(f"no frozen spec for trial {tdir.name} (neither spec.json nor the scenario's scenario.json)")
    try:
        return ScenarioSpec.model_validate(raw)
    except ValidationError as e:
        raise AuditInputError(f"{source} of trial {tdir.name} is not a valid ScenarioSpec: {e}") from e


def load_trial(layout, trial_id: str) -> TrialBundle:
    """Everything the audit reads for one trial (see the module contract)."""
    paths = RecordPaths(layout)
    tdir = paths.trial_dir(trial_id)
    if not tdir.is_dir():
        raise AuditInputError(f"trial {trial_id} not found under {tdir.parent}")
    record = _read_json(tdir / "record.json")
    record = record if isinstance(record, dict) else {}
    run_config = _read_json(tdir / "run_config.json")
    run_config = run_config if isinstance(run_config, dict) else (record.get("run_config") or {})
    report = _read_json(tdir / "report.json")
    report = report if isinstance(report, dict) else (record.get("report") if isinstance(record.get("report"), dict) else {})
    condition = run_config.get("condition") or record.get("condition")
    transcript = _read_json(tdir / "transcript.json")
    if not isinstance(transcript, dict):
        if condition == "interactive":
            raise AuditInputError(f"transcript.json missing or unreadable for interactive trial {trial_id}")
        transcript = record.get("transcript") if isinstance(record.get("transcript"), dict) else {}
    spec = _spec_for_trial(paths, tdir, record)
    assets_dir, assets_source = _assets_dir_for(paths, spec)
    terms: list[str] = []
    tests: dict[str, str] = {}
    if assets_dir is not None:
        terms = _forbidden(spec, assets_dir)
        try:
            tests, _oracle, _hashes = _read_assets(spec, assets_dir)
        except AuditInputError:
            tests = {}
    trajectory, source = find_trajectory(paths, trial_id, record)
    return TrialBundle.from_parts(trial_id, spec, run_config=run_config, record=record, report=report, transcript=transcript,
                                  trajectory=trajectory, trajectory_source=source, forbidden_terms=terms, test_assets=tests,
                                  forbidden_terms_source=assets_source)
