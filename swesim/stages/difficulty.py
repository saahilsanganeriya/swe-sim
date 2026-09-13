"""Measure the hidden-2/full-1 difficulty screen from one frozen model and build.

Infrastructure or missing evidence cannot label a task. Full-arm failure wins over
hidden non-saturation. A result records the exact trial IDs, spec and build that
were screened; comparison and calibration attempts are never pooled into it.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, model_validator

from swesim.models import Strict

SCREEN_MODEL = 'openrouter/deepseek/deepseek-v3.2'
SCREEN_COUNTS = {'hidden': 2, 'full': 1}


class Counts(Strict):
    n: int = Field(ge=0)
    successes: int = Field(ge=0)

    @model_validator(mode='after')
    def valid_counts(self):
        if self.successes > self.n:
            raise ValueError('successes exceed attempts')
        return self


class Difficulty(Strict):
    tier: Literal['saturated', 'informative', 'broken']
    model: str
    hidden: Counts
    full: Counts
    labelled_at: datetime
    trial_ids: list[str]
    spec_hash: str | None = None
    compiled_hash: str | None = None


def label(scores_for_screen: list[dict], *, labelled_at: datetime | None = None,
          spec_hash: str | None = None, compiled_hash: str | None = None) -> Difficulty:
    if len(scores_for_screen) != 2 or {s['condition'] for s in scores_for_screen} != set(SCREEN_COUNTS):
        raise ValueError('screen requires exactly one hidden group and one full group')
    models = {s.get('model') for s in scores_for_screen}
    if len(models) != 1 or not next(iter(models)):
        raise ValueError('screen groups must use one model')
    groups = {s['condition']: s for s in scores_for_screen}
    counts = {}; trial_ids = []
    for condition, n in SCREEN_COUNTS.items():
        s = groups[condition]
        if s.get('purpose') != 'screen':
            raise ValueError('difficulty accepts only purpose=screen evidence')
        if s.get('n') != n or s.get('n_infra_failures', 0) or s.get('attempts_unresolved', 0) or s.get('inconsistent_reward'):
            raise ValueError(f'{condition} screen requires {n} attributable, consistent results with no infrastructure failures')
        ids = s.get('trial_ids') or []
        if len(ids) != n or any(not isinstance(t, str) or not t for t in ids):
            raise ValueError('screen requires the exact measured trial IDs')
        trial_ids.extend(ids)
        counts[condition] = Counts(n=n, successes=s['success_count'])
    if len(set(trial_ids)) != 3:
        raise ValueError('screen cannot reuse trial IDs')
    tier = 'broken' if counts['full'].successes == 0 else 'saturated' if counts['hidden'].successes == 2 else 'informative'
    return Difficulty(tier=tier, model=next(iter(models)), **counts, trial_ids=trial_ids,
                      labelled_at=labelled_at or datetime.now(UTC), spec_hash=spec_hash, compiled_hash=compiled_hash)
