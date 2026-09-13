"""Durable audit-call receipts, written before publishing a successful audit.

A receipt identifies the actual Temporal run/activity attempt and call ordinal.
Replaying it repairs a missing ledger write without repeating the provider request.
Calls without usage stay explicit in receipts and audit metadata, never as zero costs.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from swesim import costs
from swesim.models import Strict
from swesim.paths import atomic_json


class Receipt(Strict):
    event_key: str
    workflow_id: str
    trial_id: str | None
    model: str
    usage: costs.Usage | None
    usage_complete: bool = True
    error_type: str | None
    recorded_at: str


class Ledger:
    def __init__(self, layout, target: Path, workflow_id: str, run_id: str, activity_id: str, attempt: int, trial_id: str | None = None):
        self.layout = layout
        self.directory = target / 'audit-usage'
        self.workflow_id = workflow_id
        self.trial_id = trial_id
        self.prefix = json.dumps([workflow_id, run_id, activity_id, attempt], separators=(',', ':'))

    def capture(self, call: dict, ordinal: int) -> None:
        key = f'{self.prefix}:{ordinal}'
        path = self.directory / (hashlib.sha256(key.encode()).hexdigest() + '.json')
        usage = costs.Usage.model_validate(call['usage']) if call.get('usage') is not None else None
        if usage is not None and not (usage.input_tokens or usage.output_tokens or usage.reported_cost_usd is not None):
            usage = None  # a response with no measured token fields is not proof of a free call
        receipt = Receipt(event_key=key, workflow_id=self.workflow_id, trial_id=self.trial_id, model=call['model'], usage=usage,
                          usage_complete=usage is not None and call.get('usage_complete', True),
                          error_type=call.get('error_type'), recorded_at=datetime.now(UTC).isoformat())
        if path.exists():
            previous = Receipt.model_validate_json(path.read_text())
            if previous.model_dump(exclude={'recorded_at'}) != receipt.model_dump(exclude={'recorded_at'}):
                raise ValueError('audit call receipt already binds different usage')
            return
        atomic_json(path, receipt.model_dump(mode='json'))

    def receipts(self) -> list[tuple[Path, Receipt]]:
        return [(path, Receipt.model_validate_json(path.read_text())) for path in sorted(self.directory.glob('*.json'))]

    def replay(self) -> None:
        for _path, receipt in self.receipts():
            if receipt.usage is not None:
                costs.record_cost(self.layout, receipt.trial_id, receipt.workflow_id, 'audit', receipt.model, receipt.usage,
                                  idempotency_key=receipt.event_key)

    def summary(self, *, retained_without_receipt: bool = False) -> dict:
        records = self.receipts()
        unknown = sum(receipt.usage is None or not receipt.usage_complete for _, receipt in records)
        return {'usage_complete': not unknown and not retained_without_receipt,
                'unknown_usage_count': unknown,
                'usage_receipts': [str(path.relative_to(Path(self.layout.root))) for path, _ in records]}
