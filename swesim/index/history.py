"""Projections of the immutable row snapshots (`swesim.storage.history`) into the history-backed tables.

Contract
- `workflow_rows`, `action_rows`, `rebuild_rows`, `candidate_events` read every snapshot of one entity kind under
  `Layout.history_dir` (through the object API when `Settings.bucket` is set) and reduce them to one row per primary
  key. They are what `IndexStore.rebuild_from_layout` inserts and what `FileIndexReader` answers with, so a fresh
  database rebuilt from the bucket equals the live one.
- Workflows: `choose_workflow` gives a server-observed terminal state (`lifecycle_source == "temporal"`) precedence over
  any hook report, a terminal hook report precedence over a `running` one, and among equals the latest; cost
  accounting is independent evidence and is carried onto the winner. Snapshots are folded in `updated_at` order so
  the result does not depend on directory order. `legacy` rows (Harbor job directories) are folded in first.
- Operator actions: the history snapshots plus the console's file-mode log `evidence/console/actions.jsonl`
  (written when the index is off); a malformed legacy line is logged and skipped.
- A snapshot that does not validate or does not match its path is a `ValueError` from the storage layer: the caller
  (rebuild) records the skip for that table instead of guessing.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from swesim.index import models as M
from swesim.settings import Settings
from swesim.storage.history import latest_rows, read_rows
from swesim.storage.layout import Layout

log = logging.getLogger(__name__)
TERMINAL_AUTHORITY = {"temporal": 3}


def choose_workflow(first: M.WorkflowRow | None, second: M.WorkflowRow) -> M.WorkflowRow:
    """A server-observed terminal state cannot be reopened by a late workflow hook."""
    if first is None:
        return second

    def rank(row: M.WorkflowRow):
        terminal = row.status != "running"
        authority = TERMINAL_AUTHORITY.get(row.lifecycle_source or "", 2) if terminal else 1
        return authority, row.closed_at or row.updated_at, row.updated_at

    winner = max((first, second), key=rank)
    # Cost records are independent evidence and may arrive after the workflow closes.
    loser = first if winner is second else second
    if loser.cost_usd is not None or "cost_accounting" in loser.params_summary:
        summary = dict(winner.params_summary)
        if "cost_accounting" in loser.params_summary and "cost_accounting" not in summary:
            summary["cost_accounting"] = loser.params_summary["cost_accounting"]
        winner = winner.model_copy(update={"cost_usd": winner.cost_usd if winner.cost_usd is not None else loser.cost_usd,
                                           "params_summary": summary})
    return winner


def workflow_rows(layout: Layout, settings: Settings | None = None, *, legacy: Iterable[M.WorkflowRow] = ()) -> list[M.WorkflowRow]:
    rows: dict[tuple[str, str], M.WorkflowRow] = {}
    snapshots = sorted(read_rows(layout, "workflows", settings), key=lambda row: (row.updated_at, row.closed_at or row.updated_at))
    for row in [*legacy, *snapshots]:
        key = row.workflow_id, row.run_id
        rows[key] = choose_workflow(rows.get(key), row)
    return list(rows.values())


def legacy_action_rows(layout: Layout) -> list[M.OperatorActionRow]:
    """The console's file-mode audit log, one JSON object per line."""
    rows: list[M.OperatorActionRow] = []
    path = layout.evidence_dir("console") / "actions.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return rows
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            data.setdefault("created_at", data.get("at"))
            data.setdefault("updated_at", data.get("at"))
            rows.append(M.OperatorActionRow.model_validate({k: v for k, v in data.items() if k in M.OperatorActionRow.model_fields}))
        except (TypeError, ValueError, AttributeError) as exc:
            log.warning("%s line %d not an operator action: %s", path, i + 1, type(exc).__name__)
    return rows


def action_rows(layout: Layout, settings: Settings | None = None) -> list[M.OperatorActionRow]:
    return latest_rows([*legacy_action_rows(layout), *read_rows(layout, "operator_actions", settings)])


def rebuild_rows(layout: Layout, settings: Settings | None = None) -> list[M.IndexRebuildRow]:
    return sorted(latest_rows(read_rows(layout, "index_rebuilds", settings)), key=lambda row: (row.started_at, row.rebuild_id), reverse=True)


def candidate_events(layout: Layout, settings: Settings | None = None) -> list[M.CandidateEventRow]:
    return sorted(latest_rows(read_rows(layout, "candidate_events", settings)), key=lambda row: (row.occurred_at, row.event_id))


__all__ = ["choose_workflow", "workflow_rows", "legacy_action_rows", "action_rows", "rebuild_rows", "candidate_events"]
