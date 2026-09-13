"""Compatible evaluation cohorts, independent of task-specific build and model IDs.

Unknown configurations retain their recorded run-config identity. We never infer a
modern scaffold or stakeholder version for an older record with missing evidence.
"""
from __future__ import annotations

import hashlib
import json

VARIABLE_FIELDS = {'model', 'condition', 'brief_profile', 'compiled_hash', 'purpose', 'k'}


def semantic_config(run_config: dict, report: dict) -> dict | None:
    if not run_config:
        return None
    config = {k: v for k, v in run_config.items() if k not in VARIABLE_FIELDS}
    recorded_scaffold = report.get('agent_config_sha256')
    if recorded_scaffold:
        config['agent_config'] = 'mini-swe-agent.interactive:' + recorded_scaffold[:16]
    return config


def cohort_of(trial) -> tuple[str, str]:
    config = trial.evaluation_config
    identity = {'origin': trial.origin, 'stakeholder_model': trial.stakeholder_model,
                'stakeholder_prompt_version': trial.stakeholder_prompt_version,
                'config': config if config is not None else {'unknown_config_hash': trial.run_config_hash}}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]
    parts = [trial.origin, trial.stakeholder_prompt_version or 'stakeholder version unknown',
             trial.stakeholder_model or 'stakeholder model unknown', digest[:8]]
    if config is None:
        parts.append('configuration unverified')
    return digest, ' · '.join(parts)
