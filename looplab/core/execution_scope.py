"""Operator execution scope shared by HTTP admission and the actual engine launch."""
from __future__ import annotations

import json
from pathlib import Path

from looplab.core.node_evidence import read_bounded_regular_file

JOB_ROLES = ('prepare', 'profile', 'fit', 'score', 'recovery')
EXECUTION_MODES = ('science', 'optimization_only')
EXECUTION_POLICY_FILE = 'harness_policy.json'


def normalize_execution(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get('mode') not in EXECUTION_MODES:
        raise ValueError('execution.mode must be science or optimization_only')
    roles = raw.get('allowed_job_roles')
    if (not isinstance(roles, list) or not roles
            or any(role not in JOB_ROLES for role in roles) or len(set(roles)) != len(roles)):
        raise ValueError(f'execution.allowed_job_roles must be unique values from {JOB_ROLES}')
    return {'mode': raw['mode'], 'allowed_job_roles': list(roles), 'version': 1}


def scope_error(execution, role):
    if not execution or execution.get('mode') != 'optimization_only':
        return None
    allowed = execution['allowed_job_roles']
    if role in allowed:
        return None
    return {'code': 'optimization_scope_refused',
            'message': f'job_role {role!r} is outside the operator\'s optimization-only scope',
            'allowed_job_roles': allowed, 'execution_version': execution.get('version', 1)}


def launch_scope_error(rd: Path, role):
    path = Path(rd) / EXECUTION_POLICY_FILE
    try:
        path.lstat()
    except FileNotFoundError:
        return None  # legacy runs have no restriction
    except OSError:
        return {'code': 'harness_policy_unreadable', 'message': 'Execution scope could not be verified.'}
    raw = read_bounded_regular_file(path, 256 * 1024 + 1)
    try:
        if raw is None or len(raw) > 256 * 1024:
            raise ValueError('unreadable execution policy')
        policy = json.loads(raw)
        if not isinstance(policy, dict):
            raise ValueError('invalid execution policy')
        execution = normalize_execution(policy.get('execution'))
    except (ValueError, TypeError, UnicodeDecodeError):
        return {'code': 'harness_policy_unreadable', 'message': 'Execution scope could not be verified.'}
    return scope_error(execution, role)
