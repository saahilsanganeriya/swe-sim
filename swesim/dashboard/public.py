"""Public structured-data projection shared by HTML, JSON, CSV and artifact previews.

Opaque evaluator records and their nested annotations never cross this boundary.
Model-family/difficulty labels use dedicated public fields, not stakeholder tiers.
"""
from __future__ import annotations

from swesim.dashboard.rows import EVALUATOR_ONLY_KEYS

PRIVATE_KEYS = EVALUATOR_ONLY_KEYS | frozenset({
    "registry_json", "judge", "judge_metadata", "draft", "draft_meta", "draft_metadata",
    "author_only", "author_only_facts", "hidden_tests", "hidden_test_assets", "oracle",
    "source_excerpt", "assets", "ledger", "spec", "scenario", "transcript", "record",
    "api_key", "authorization", "worker_secret", "session_token", "token", "secret",
})


def project(value):
    """Copy public data recursively; never mutate a reader's rows or cached artifacts."""
    if isinstance(value, dict):
        if value.get("discovery") == "author-only":
            return {}
        return {k: project(v) for k, v in value.items() if str(k).lower() not in PRIVATE_KEYS}
    if isinstance(value, (list, tuple)):
        return [project(v) for v in value if not isinstance(v, dict) or v.get("discovery") != "author-only"]
    return value
