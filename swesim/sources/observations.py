"""Typed, immutable discovery observations; pre-candidate rejects remain first-class evidence.

Contract
- `DiscoveryPRObservation`: one listed PR and the furthest stage it reached (`STAGES`, in funnel order), its decision
  (`status`) and reason. Fetch failures are `error`, never a task rejection, and never carry `gh` stderr.
- `DiscoveryScan`: the immutable record of one source scan (`candidates/<source_key>/scans/<scan_id>.json`), every PR
  observation plus the funnel, the legacy reject histogram it produced and that histogram's artifact sha256. The scan id
  is the sha256 of the file bytes, so a scan file can never be edited in place; `read_scan` refuses a file whose bytes
  do not hash to its name. The histogram in a scan is derived from the observations by a validator, so the console's
  rejects and the per-PR decisions cannot disagree.
- `rejects_bytes(counts)` is the exact serialization `swesim.paths.atomic_json` writes for `<source_key>.rejects.json`;
  `DiscoveryScan.rejects_sha256` is its digest, which lets the index projection find the scan of a rejects artifact
  without any mutable pointer.
"""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from swesim.models import Strict

DiscoveryStage = Literal['listed', 'merged-in-range', 'linked-issue', 'band-passed', 'retained']
STAGES: tuple[str, ...] = ('listed', 'merged-in-range', 'linked-issue', 'band-passed', 'retained')
# Decisions taken from the list page alone; they never entered the legacy `.rejects.json` histogram and still do not.
PREFILTER_REASONS: frozenset[str] = frozenset({'not-merged', 'before-since'})
CAP_REASON = 'max-candidates-cut'
SHA256_RE = r'^[0-9a-f]{64}$'


class DiscoveryPRObservation(Strict):
    seq: int = Field(ge=0)
    pr_number: int = Field(ge=1)
    pr_url: str
    observed_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    stages: list[DiscoveryStage]
    status: Literal['pending', 'retained', 'rejected', 'error']
    reason: str | None = None
    error_class: str | None = None
    linked_issue_numbers: list[int] | None = None
    source_changed_lines: int | None = Field(default=None, ge=0)
    source_file_count: int | None = Field(default=None, ge=0)
    test_file_count: int | None = Field(default=None, ge=0)
    comment_count: int | None = Field(default=None, ge=0)
    base_commit: str | None = None
    merge_commit: str | None = None

    @property
    def stage(self) -> str:
        """The furthest funnel stage reached (`STAGES` order), independent of the order stages were appended."""
        reached = [s for s in STAGES if s in self.stages]
        return reached[-1] if reached else 'listed'


class DiscoveryScan(Strict):
    """One immutable source scan: parameters, every PR decision, the funnel and the rejects histogram it produced."""

    method_version: Literal['discovery-v1'] = 'discovery-v1'
    source_key: str
    repo: str
    band: Literal['bugfix', 'feature']
    since: str
    max_prs: int = Field(ge=1)
    max_candidates: int | None = Field(default=None, ge=1)
    pr: int | None = Field(default=None, ge=1)
    scanned_at: AwareDatetime
    workflow_id: str | None = None
    run_id: str | None = None
    observations: list[DiscoveryPRObservation]
    funnel: dict[str, int]
    rejects: dict[str, int]
    rejects_sha256: str = Field(pattern=SHA256_RE)

    @model_validator(mode='after')
    def consistent(self):
        if len({o.seq for o in self.observations}) != len(self.observations):
            raise ValueError('discovery observation sequence is not unique')
        if len({o.pr_number for o in self.observations}) != len(self.observations):
            raise ValueError('a PR is observed once per scan')
        if self.funnel != funnel(self.observations):
            raise ValueError('discovery funnel differs from recorded PR observations')
        if self.rejects != legacy_rejects(self.observations):
            raise ValueError('discovery rejects histogram differs from recorded PR decisions')
        if self.rejects_sha256 != hashlib.sha256(rejects_bytes(self.rejects)).hexdigest():
            raise ValueError('discovery rejects sha256 differs from the recorded histogram')
        if bool(self.workflow_id) != bool(self.run_id):
            raise ValueError('scan workflow and run ids must both be recorded or both unknown')
        return self


def funnel(observations: list[DiscoveryPRObservation]) -> dict[str, int]:
    return {stage: sum(stage in o.stages for o in observations) for stage in STAGES}


def reject_counts(observations: list[DiscoveryPRObservation]) -> dict[str, int]:
    return dict(Counter(o.reason for o in observations if o.status in ('rejected', 'error') and o.reason))


def legacy_rejects(observations: list[DiscoveryPRObservation]) -> dict[str, int]:
    """The `.rejects.json` histogram: every recorded decision except the list-page prefilters."""
    return {reason: count for reason, count in reject_counts(observations).items() if reason not in PREFILTER_REASONS}


def retain_selected(observations: list[DiscoveryPRObservation], selected: set[int]) -> list[DiscoveryPRObservation]:
    """A source cap is a recorded decision after band acceptance, never a vanished candidate."""
    return [o.model_copy(update={'status': 'rejected', 'reason': CAP_REASON, 'stages': [s for s in o.stages if s != 'retained']})
            if o.status == 'retained' and o.seq not in selected else o for o in observations]


def decisions(observations: list[DiscoveryPRObservation]) -> list[dict]:
    """The compact per-PR decision list the index carries (`DiscoveryRow.decisions`): identity, stage, status, reason."""
    return [{'seq': o.seq, 'pr_number': o.pr_number, 'pr_url': o.pr_url, 'stage': o.stage, 'status': o.status, 'reason': o.reason,
             'error_class': o.error_class, 'linked_issues': list(o.linked_issue_numbers or [])} for o in observations]


# ----------------------------------------------------------------------------- bytes on disk

def rejects_bytes(counts: dict[str, int]) -> bytes:
    """Exactly what `atomic_json` writes for the rejects artifact (sorted keys, indent 2, no NaN)."""
    return json.dumps(counts, sort_keys=True, indent=2, allow_nan=False).encode()


def scan_bytes(scan: DiscoveryScan) -> bytes:
    return json.dumps(scan.model_dump(mode='json'), sort_keys=True, indent=2, allow_nan=False).encode()


def scan_id(scan: DiscoveryScan) -> str:
    return hashlib.sha256(scan_bytes(scan)).hexdigest()


def write_scan(layout, scan: DiscoveryScan) -> tuple[Path, str]:
    """Write `candidates/<source_key>/scans/<sha256>.json` once; an existing file with that name already holds these bytes."""
    data = scan_bytes(scan)
    digest = hashlib.sha256(data).hexdigest()
    path = layout.scan_file(scan.source_key, digest)
    if path.is_file() and path.read_bytes() == data:
        return path, digest
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return path, digest


def read_scan(path: Path) -> DiscoveryScan:
    """A scan file must hash to its own name; anything else is not evidence."""
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if Path(path).stem != digest:
        raise ValueError(f'discovery scan {Path(path).name} does not hash to its name')
    return DiscoveryScan.model_validate_json(data)


def scan_view(path: Path, scan: DiscoveryScan, layout) -> dict:
    """The console's view of one scan file: identity, parameters, funnel and every PR decision (no diff or thread text)."""
    return {"source_key": scan.source_key, "repo": scan.repo, "band": scan.band, "since": scan.since, "max_prs": scan.max_prs,
            "max_candidates": scan.max_candidates, "pr": scan.pr, "discovered_at": scan.scanned_at.isoformat(), "workflow_id": scan.workflow_id,
            "run_id": scan.run_id, "funnel": dict(scan.funnel), "rejects": dict(scan.rejects), "decisions": decisions(scan.observations),
            "scan_path": layout.relative(path), "scan_sha256": Path(path).stem}


def latest_scans(layout, source_key: str | None = None) -> dict[str, dict]:
    """Newest valid scan per source key, read from `candidates/<key>/scans/`; an unreadable or tampered file is skipped
    (the artifact browser still shows it), never rendered as evidence."""
    root = layout.candidates_dir()
    if not root.is_dir():
        return {}
    keys = [source_key] if source_key else sorted(d.name for d in root.iterdir() if d.is_dir() and (d / "scans").is_dir())
    out: dict[str, dict] = {}
    for key in keys:
        try:
            scan_dir = layout.scan_dir(key)
        except ValueError:
            continue
        best: tuple[Path, DiscoveryScan] | None = None
        for path in sorted(scan_dir.glob("*.json")) if scan_dir.is_dir() else []:
            try:
                scan = read_scan(path)
            except (OSError, ValueError):
                continue
            if scan.source_key != key:
                continue
            if best is None or (scan.scanned_at, path.stem) > (best[1].scanned_at, best[0].stem):
                best = (path, scan)
        if best is not None:
            out[key] = scan_view(best[0], best[1], layout)
    return out
