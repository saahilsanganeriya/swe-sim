"""Durable operator record mutations run on a writable CONTROL worker.

The console submits a confirmation-bound request ID. Temporal records review signals;
immutable prepare/commit receipts make replay and repeated confirmation idempotent.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from swesim.models import ScenarioSpec
    from swesim.paths import atomic_json
    from swesim.settings import get_settings
    from swesim.storage.layout import Layout
    from swesim.workflows.payloads import check_payload

LABELS = ('unresolved', 'no-question', 'disclosed-not-used', 'simulator-omission', 'derived-without-asking')


def _request(p: dict) -> tuple[dict, Path]:
    check_payload(p, 'operator action')
    if p.get('action') not in ('label', 'review') or not re.fullmatch('[a-f0-9]{64}', str(p.get('request_id', ''))):
        raise ValueError('invalid operator action or request ID')
    if not isinstance(p.get('actor'), str) or not p['actor'] or not isinstance(p.get('params'), dict):
        raise ValueError('operator action requires actor and parameters')
    request = {k: p[k] for k in ('action', 'request_id', 'actor', 'params')}
    path = get_settings().workspace / 'evidence' / 'console' / 'requests' / p['request_id']
    return request, path


def _validate_record(request: dict, *, after_signal: bool = False) -> None:
    layout = Layout(root=get_settings().workspace)
    p = request['params']
    if request['action'] == 'label':
        if p.get('label') not in LABELS:
            raise ValueError('invalid episode label')
        if not layout.trial_dir(p.get('trial_id')).is_dir():
            raise ValueError('no such trial')
        return
    directory = layout.scenario_dir(p.get('scenario_id'))
    draft = directory / 'scenario.draft.json'
    if not draft.is_file():
        raise ValueError('no draft on disk for this scenario')
    spec = ScenarioSpec.model_validate_json(draft.read_text())
    if spec.spec_hash != p.get('spec_hash'):
        raise ValueError('the draft changed since review; reload and review again')
    if p.get('decision') not in ('approve', 'reject'):
        raise ValueError('decision must be approve or reject')
    if p['decision'] == 'approve' and not p.get('workflow_id'):
        raise ValueError('approval requires a holding workflow')
    approval = directory / 'approval.json'
    if approval.exists() and not (after_signal and p['decision'] == 'approve' and json.loads(approval.read_text()).get('spec_hash') == spec.spec_hash):
        raise ValueError('already approved; the review is read-only')


@activity.defn
async def prepare_operator_action_activity(p: dict) -> dict:
    try:
        request, directory = _request(p)
        path = directory / 'request.json'
        if path.exists():
            saved = json.loads(path.read_text())
            if saved['request'] != request:
                raise ValueError('request ID already binds different operator parameters')
            return saved
        _validate_record(request)
        target = None
        if request['action'] == 'review' and request['params'].get('workflow_id'):
            from swesim.workflows.client import _candidate_id_from_parent, connect
            target = request['params']['workflow_id']
            if not target.startswith('create:'):
                target = await _candidate_id_from_parent(await connect(), target, request['params']['scenario_id'])
        s = get_settings()
        saved = {'request': request, 'signal_target': target, 'decided_at': datetime.now(UTC).isoformat(),
                 'worker_image_digest': s.worker_image_digest, 'origin': s.origin}
        check_payload(saved, "prepared operator action")
        atomic_json(path, saved)
        return saved
    except ValueError as error:
        raise ApplicationError(str(error), type='OperatorInputError', non_retryable=True) from error


@activity.defn
async def commit_operator_action_activity(p: dict) -> dict:
    try:
        request, directory = _request(p['request'])
        if json.loads((directory / 'request.json').read_text()) != p:
            raise ValueError('commit differs from the prepared operator request')
        result_path = directory / 'result.json'
        if result_path.exists():
            return json.loads(result_path.read_text())
        _validate_record(request, after_signal=True)
        params = request['params']
        layout = Layout(root=get_settings().workspace)
        current = get_settings()
        provenance = {'decided_at': p['decided_at'], 'worker_image_digest': current.worker_image_digest, 'origin': current.origin}
        if activity.in_activity():
            provenance.update(workflow_id=activity.info().workflow_id, run_id=activity.info().workflow_run_id)
        if request['action'] == 'label':
            record = {**params, **provenance, 'decided_by': request['actor'], 'request_id': request['request_id']}
            atomic_json(layout.trial_dir(params['trial_id']) / 'label.json', record)
        else:
            record = {**params, **provenance, 'decided_by': request['actor'], 'request_id': request['request_id'], 'signalled': p['signal_target']}
            atomic_json(layout.scenario_dir(params['scenario_id']) / 'review.json', record)
        result = {'ok': True, 'saved': True, 'action': request['action'], 'request_id': request['request_id'],
                  'decision': params.get('decision'), 'scenario_id': params.get('scenario_id'), 'trial_id': params.get('trial_id'),
                  'spec_hash': params.get('spec_hash'), 'signalled': p['signal_target'], 'via': 'control workflow',
                  'worker_image_digest': current.worker_image_digest, 'origin': current.origin}
        check_payload(result, "committed operator action")
        atomic_json(result_path, result)
        return result
    except ValueError as error:
        raise ApplicationError(str(error), type='OperatorInputError', non_retryable=True) from error


@workflow.defn
class OperatorActionWorkflow:
    def __init__(self) -> None:
        self.state = {"stage": "starting"}

    @workflow.query
    def progress(self) -> dict:
        return self.state

    @workflow.run
    async def run(self, p: dict) -> dict:
        from swesim.workflows.workflows import act, hook, index_workflow
        from swesim.workflows import retries
        await index_workflow('running', {'action': p['action'], 'request_id': p['request_id']})
        try:
            self.state = {'stage': 'validating', 'action': p['action']}
            prepared = await act('prepare_operator_action_activity', p, retry=retries.READ)
            if prepared['signal_target']:
                self.state['stage'] = 'signalling'
                q = prepared['request']['params']
                await workflow.get_external_workflow_handle(prepared['signal_target']).signal('approve', {
                    'scenario_id': q['scenario_id'], 'spec_hash': q['spec_hash'], 'decision': q['decision'], 'note': q.get('note', '')})
            self.state['stage'] = 'writing'
            result = await act('commit_operator_action_activity', prepared, retry=retries.READ)
            field = 'trial_id' if p['action'] == 'label' else 'scenario_id'
            await hook('index_trial_activity' if field == 'trial_id' else 'index_scenario_activity', {field: p['params'][field]})
            await index_workflow('completed', {'action': p['action'], 'request_id': p['request_id']})
            self.state['stage'] = 'complete'
            return result
        except Exception:
            self.state['stage'] = 'failed'
            await index_workflow('failed', {'action': p['action'], 'request_id': p['request_id']}, 'Operator action failed; see workflow history')
            raise


ALL = [prepare_operator_action_activity, commit_operator_action_activity]


@activity.defn
async def read_candidate_source_activity(p: dict) -> dict:
    """Select the exact recorded candidate from an immutable workflow snapshot, without rediscovery."""
    from swesim.workflows import batching
    from swesim.paths import Workspace
    from swesim.storage.layout import SEGMENT_RE
    import hashlib
    try:
        check_payload(p, 'frozen candidate source input')
        s = get_settings()
        ws = Workspace(s.workspace, s.registry_path)
        path = batching.checked_path(ws, p['candidates_path'])
        if not SEGMENT_RE.fullmatch(p['source_key']) or not isinstance(p.get('instance_id'), str):
            raise ValueError('invalid frozen candidate identity')
        raw = path.read_bytes()
        rows = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
        selected = [row for row in rows if row.get('instance_id') == p['instance_id']]
        if len(selected) != 1:
            raise ValueError('frozen source must contain exactly one matching candidate')
        wid = activity.info().workflow_id if activity.in_activity() else 'direct'
        selected_text = json.dumps(selected[0], sort_keys=True) + '\n'
        digest = hashlib.sha256(selected_text.encode()).hexdigest()
        out = batching.directory(ws, wid) / 'retry' / (digest + '.jsonl')
        batching.atomic_text(out, selected_text)
        return {'candidates_path': str(out), 'source_key': p['source_key'], 'written': 1, 'discovered': 1,
                'source_snapshot_sha256': hashlib.sha256(raw).hexdigest(), 'retried_instance_id': p['instance_id'],
                'worker_image_digest': s.worker_image_digest, 'origin': s.origin}
    except (KeyError, ValueError) as error:
        raise ApplicationError(str(error), type='OperatorInputError', non_retryable=True) from error


ALL.append(read_candidate_source_activity)
