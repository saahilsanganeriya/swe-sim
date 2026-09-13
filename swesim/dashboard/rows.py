"""The row, page, filter and report models the console codes against: re-exported from ``swesim.index.models``
(interfaces §2, owned by the storage stream) so the console never defines a parallel shape, plus the console-side
constants that depend on them (the browsable tables, the evaluator-only keys, the public projection of an exchange).
"""
from __future__ import annotations

from swesim.index.models import (  # noqa: F401 - re-exports are the point of this module
    CandidateFilter,
    CandidateRow,
    CostFilter,
    CostRow,
    EVALUATOR_ONLY_EXCHANGE_COLUMNS,
    FUNNEL_ORDER,
    ExchangeRow,
    FunnelStep,
    IndexRebuildRow,
    LeaderboardFilter,
    LeaderboardRow,
    OperatorActionRow,
    Page,
    PageFilter,
    ScenarioFilter,
    ScenarioRow,
    ScoreFilter,
    ScoreRow,
    SpendRow,
    TableFilter,
    TrialFilter,
    TrialRow,
    WorkflowFilter,
    WorkflowRow,
)

TABLES: tuple[str, ...] = ("scenarios", "candidates", "trials", "exchanges", "scores", "workflows", "costs", "operator_actions", "index_rebuilds")
ROW_OF: dict[str, type] = {"scenarios": ScenarioRow, "candidates": CandidateRow, "trials": TrialRow, "exchanges": ExchangeRow, "scores": ScoreRow,
                           "workflows": WorkflowRow, "costs": CostRow, "operator_actions": OperatorActionRow, "index_rebuilds": IndexRebuildRow}
# Evaluator-only keys (brief §2.5, Codex B-P2-6): never present in a non-evaluator response; the leak test enumerates the routes.
EVALUATOR_ONLY_KEYS: frozenset[str] = frozenset(EVALUATOR_ONLY_EXCHANGE_COLUMNS) | frozenset(
    {"withheld_sentences", "forbidden_terms", "tier", "tool_calls", "delivery", "task_sense"})


def public_exchange(row) -> dict:
    """An exchange row with the evaluator-only columns blanked (the reader does this too; the console never trusts one layer)."""
    d = row.model_dump(mode="json") if hasattr(row, "model_dump") else dict(row)
    d["disclosed_fact_ids"] = []
    d["redacted_terms"] = []
    d["regenerated"] = None
    d["audit"] = None
    return d
