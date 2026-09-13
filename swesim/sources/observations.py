"""Typed, immutable discovery observations; pre-candidate rejects remain first-class evidence."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from swesim.models import Strict

DiscoveryStage = Literal['listed', 'merged-in-range', 'linked-issue', 'band-passed', 'retained']
STAGES = ('listed', 'merged-in-range', 'linked-issue', 'band-passed', 'retained')


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


class DiscoveryObservationRecord(Strict):
    method_version: Literal['discovery-v1'] = 'discovery-v1'
    repo: str
    band: Literal['bugfix', 'feature']
    since: str
    max_prs: int = Field(ge=1)
    observations: list[DiscoveryPRObservation]
    funnel: dict[str, int]

    @model_validator(mode='after')
    def consistent(self):
        if len({o.seq for o in self.observations}) != len(self.observations):
            raise ValueError('discovery observation sequence is not unique')
        if self.funnel != funnel(self.observations):
            raise ValueError('discovery funnel differs from recorded PR observations')
        return self


def funnel(observations: list[DiscoveryPRObservation]) -> dict[str, int]:
    return {stage: sum(stage in o.stages for o in observations) for stage in STAGES}


def reject_counts(observations: list[DiscoveryPRObservation]) -> dict[str, int]:
    return dict(Counter(o.reason for o in observations if o.status in ('rejected', 'error') and o.reason))


def retain_selected(observations: list[DiscoveryPRObservation], selected: set[int]) -> list[DiscoveryPRObservation]:
    """A source cap is a recorded decision after band acceptance, never a vanished candidate."""
    return [o.model_copy(update={'status': 'rejected', 'reason': 'max-candidates-cut', 'stages': [s for s in o.stages if s != 'retained']})
            if o.status == 'retained' and o.seq not in selected else o for o in observations]
