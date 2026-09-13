"""Immutable workflow plans and linked checkpoints kept outside Temporal history.

Each continuation carries a plan path, cursor and summary. A page contains at most 100
children. Full trial requests and all completed page results remain in the workspace;
retries read the same immutable plan, and page checkpoints are idempotent.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from swesim.paths import Workspace, atomic_json

CHILDREN_PER_RUN = 100


def atomic_text(path: Path, text: str) -> None:
    """Publish JSONL with the workspace's file write-then-rename contract."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(text); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def directory(ws: Workspace, workflow_id: str) -> Path:
    return ws.root / 'evidence' / 'workflows' / hashlib.sha256(workflow_id.encode()).hexdigest()[:32]


def checked_path(ws: Workspace, value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to((ws.root / 'evidence' / 'workflows').resolve()):
        raise ValueError('workflow artifact must be within evidence/workflows')
    return path


def plan_path(ws: Workspace, workflow_id: str, key: str) -> Path:
    return directory(ws, workflow_id) / (hashlib.sha256(key.encode()).hexdigest()[:24] + '.plan.json')


def write_plan(path: Path, items: list[dict], **metadata) -> dict:
    if not path.exists():
        atomic_json(path, {'items': items, **metadata})
    plan = json.loads(path.read_text())
    return {'plan_path': str(path), 'total': len(plan['items']), **{k: v for k, v in plan.items() if k != 'items'}}


def trial_plan(ws: Workspace, path: Path, trials: list[dict]) -> dict:
    items = []
    for request in trials:
        tid = request['trial_id']
        if not tid.isalnum():
            raise ValueError('invalid trial id in workflow plan')
        request_path = path.parent / path.stem / f'{tid}.json'
        if request_path.exists() and json.loads(request_path.read_text()) != request:
            raise ValueError(f'workflow plan already binds trial {tid} to a different request')
        atomic_json(request_path, request)
        items.append({**{k: request[k] for k in ('trial_id', 'scenario_id', 'model', 'condition', 'attempt_index')},
                      'version': request.get('version'), 'purpose': request['run_config']['purpose'],
                      'audit_model': request.get('audit_model'), 'request_path': str(request_path)})
    return write_plan(path, items)


def read_page(ws: Workspace, path: str, offset: int = 0, limit: int = CHILDREN_PER_RUN) -> dict:
    if offset < 0 or not 1 <= limit <= CHILDREN_PER_RUN:
        raise ValueError('plan page requires offset >= 0 and 1 <= limit <= 100')
    plan = json.loads(checked_path(ws, path).read_text())
    items = plan['items'][offset:offset + limit]
    return {'items': items, 'total': len(plan['items']), 'next_offset': offset + len(items)}


def checkpoint(ws: Workspace, workflow_id: str, run_id: str, state: dict) -> str:
    path = directory(ws, workflow_id) / (hashlib.sha256(run_id.encode()).hexdigest()[:24] + '.checkpoint.json')
    if path.exists() and json.loads(path.read_text()) != state:
        raise ValueError('checkpoint retry differs from its recorded state')
    atomic_json(path, state)
    return str(path)
