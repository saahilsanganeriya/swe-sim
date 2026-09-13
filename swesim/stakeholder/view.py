"""The stakeholder-visible view of a ScenarioSpec, built in the worker before ``POST /sessions``.

Structural exclusion: the view carries only brief, non-author-only facts (id, text, kind, discovery), requirement
texts, persona, checks and the repo dir. It never references ``spec.assets``, ``spec.controls`` (oracle) or
``spec.contract`` (hidden tests), so nothing downstream can leak them by accident.
"""
from __future__ import annotations

from swesim.models import Discovery, ScenarioSpec


def visible_view(spec: ScenarioSpec) -> dict:
    return {
        "brief": next(b.text for b in spec.briefs if b.profile == "natural"),
        "facts": [{"id": f.id, "text": f.text, "kind": f.kind.value, "discovery": f.discovery.value}
                  for f in spec.ledger if f.discovery != Discovery.AUTHOR_ONLY],
        "requirements": [{"id": r.id, "text": r.text} for r in spec.requirements],
        "persona": spec.persona.model_dump(),
        "checks": [c.model_dump() for c in spec.checks],
        "repo_dir": spec.environment.repo_dir,
    }
