"""Opt-in CPU executor contract. Placement is separate from code trust and GPU ownership."""
from __future__ import annotations

import contextvars
import hashlib
import json
import math
import re
from pathlib import PurePosixPath

NATIVE_BINDING = contextvars.ContextVar('cluster_native_binding', default=None)
MAX_REQUEST = 1024 * 1024
MAX_REPLY = 512 * 1024


class ExecutorOutcomeUnknown(RuntimeError):
    """A durable invocation may still exist. Reconcile this key; never invent a new launch."""
    def __init__(self, receipt):
        super().__init__('cluster executor outcome unknown; reconcile the existing invocation')
        self.receipt = receipt


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def finite_number(value, low, high):
    try:
        return type(value) in (int, float) and math.isfinite(float(value)) and low <= value <= high
    except (ValueError, OverflowError):
        return False


def normalize_executor(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or not {'argv', 'worker_id'} <= set(value)
            or set(value) - {'argv', 'worker_id', 'rpc_timeout_seconds', 'poll_seconds'}):
        raise ValueError('cluster_executor needs argv and worker_id; only bounded RPC/poll times are optional')
    argv = value['argv']
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
            or any(not isinstance(s, str) or not s or len(s) > 2048 or '\x00' in s for s in argv)):
        raise ValueError('cluster_executor.argv must be a bounded fixed argument vector, without a shell')
    worker = value['worker_id']
    if not isinstance(worker, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', worker):
        raise ValueError('cluster_executor.worker_id must be a short explicit placement identity')
    rpc, poll = value.get('rpc_timeout_seconds', 5.), value.get('poll_seconds', .2)
    if not finite_number(rpc, .1, 30) or not finite_number(poll, .02, 5):
        raise ValueError('cluster RPC timeout must be .1..30s; polling .02..5s')
    from looplab.core.envsafe import is_secret_env
    for item in argv:
        item.encode('utf-8')
        if is_secret_env('', item) or re.search(r'(?i)(?:token|password|secret|passwd)=', item):
            raise ValueError('cluster transport argv must not contain inline credentials; use operator-owned transport authentication')
    return {'argv': list(argv), 'worker_id': worker, 'rpc_timeout_seconds': float(rpc),
            'poll_seconds': float(poll)}


def text_assets(assets):
    """Only operator-declared bounded text, never a recursive workdir/model/private-file walk."""
    if not isinstance(assets, dict) or len(assets) > 64:
        raise ValueError('cluster inputs must be at most 64 declared text assets')
    out = {}
    for name, content in assets.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError('cluster assets require text paths and contents')
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or str(path) != name or '\\' in name
                or any(p in ('..', '.') or p.startswith('.') for p in path.parts)
                or any(ord(c) < 32 for c in name) or name == 'solution.py'
                or len(name) > 240 or len(content.encode('utf-8')) > 64 * 1024):
            raise ValueError('unsupported cluster text asset path or size')
        out[name] = content
    if sum(len(s.encode('utf-8')) for s in out.values()) > 256 * 1024:
        raise ValueError('cluster text assets exceed 256KiB')
    return out


def validate_binding(value):
    keys = {'run_uid', 'node_id', 'generation', 'attempt', 'invocation_id', 'task_id', 'resources'}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError('cluster execution requires the native attempt binding')
    for name in ('run_uid', 'invocation_id', 'task_id'):
        v = value[name]
        if not isinstance(v, str) or not v.strip() or len(v) > 512 or any(ord(c) < 32 for c in v):
            raise ValueError('invalid native executor identity')
    if any(type(value[k]) is not int or value[k] < 0 for k in ('node_id', 'generation', 'attempt')):
        raise ValueError('native executor IDs must be exact nonnegative integers')
    expected = f"{value['run_uid']}:n{value['node_id']}:g{value['generation']}:a{value['attempt']}"
    if value['invocation_id'] != expected:
        raise ValueError('executor invocation key does not match its native coordinates')
    resources = value['resources']
    if (not isinstance(resources, dict) or set(resources) != {'gpus', 'cpu_slots'}
            or type(resources['gpus']) is not int or resources['gpus'] != 0
            or type(resources['cpu_slots']) is not int or resources['cpu_slots'] != 1):
        raise ValueError('cluster executor currently supports one CPU slot and zero GPUs only')
    return dict(value)


def validate_receipt(reply, worker, binding, source_sha, inputs_sha, payload_sha=None):
    """Reject coercible or partial HTTP/stdio-style success metadata before accepting a result."""
    allowed = {'v', 'worker_id', 'job_id', 'payload_sha256', 'binding', 'state', 'detail',
        'accepted_at', 'host', 'source_sha256', 'input_sha256', 'execution_seconds', 'billing',
        'resource_occupancy', 'executor_pid', 'executor_process_token', 'started_at', 'finished_at',
        'cancel_requested', 'result', 'output_sha256', 'blocking_job_id'}
    if (not isinstance(reply, dict) or set(reply) - allowed or type(reply.get('v')) is not int
            or reply['v'] != 1 or reply.get('worker_id') != worker
            or reply.get('job_id') != digest(binding['invocation_id'])):
        raise ValueError('unsupported executor receipt envelope')
    validate_binding(reply.get('binding'))
    if canonical(reply['binding']) != canonical(binding):
        raise ValueError('executor native binding mismatch')
    payload = reply.get('payload_sha256')
    if (not isinstance(payload, str) or not re.fullmatch('[a-f0-9]{64}', payload)
            or payload_sha is not None and payload != payload_sha):
        raise ValueError('executor immutable payload mismatch')
    state = reply.get('state')
    if state not in ('launch_claimed', 'running', 'finished', 'unknown', 'refused', 'conflict'):
        raise ValueError('unsupported executor state')
    if state in ('launch_claimed', 'running', 'finished'):
        if (reply.get('source_sha256') != source_sha or reply.get('input_sha256') != inputs_sha
                or not isinstance(reply.get('host'), str) or not 1 <= len(reply['host']) <= 256
                or any(ord(c) < 32 for c in reply['host'])):
            raise ValueError('executor source/input/placement receipt mismatch')
    if state in ('launch_claimed', 'running', 'finished') and not finite_number(reply.get('accepted_at'), 0, 1e12):
        raise ValueError('executor accepted timestamp is missing or invalid')
    if state in ('running', 'finished'):
        token = reply.get('executor_process_token')
        if (not finite_number(reply.get('started_at'), 0, 1e12)
                or not isinstance(token, str) or not re.fullmatch('[0-9]{1,40}', token)
                or type(reply.get('executor_pid')) is not int or reply['executor_pid'] <= 0):
            raise ValueError('executor actual process/timing receipt is incomplete')
    if state == 'finished' and not finite_number(reply.get('finished_at'), 0, 1e12):
        raise ValueError('executor terminal timestamp is missing or invalid')
    for name in ('accepted_at', 'started_at', 'finished_at', 'execution_seconds'):
        if reply.get(name) is not None and not finite_number(reply[name], 0, 1e12):
            raise ValueError('executor timing must be a finite exact number')
    if 'cancel_requested' in reply and type(reply['cancel_requested']) is not bool:
        raise ValueError('executor cancellation receipt must be boolean')
    if 'executor_pid' in reply and (type(reply['executor_pid']) is not int or reply['executor_pid'] <= 0):
        raise ValueError('executor process ID must be an exact positive integer')
    if 'detail' in reply and (not isinstance(reply['detail'], str) or len(reply['detail']) > 256):
        raise ValueError('executor detail exceeds the closed metadata contract')
    if reply.get('billing', 'unknown') not in ('unknown', 'not_invoked'):
        raise ValueError('unverified executor billing claim')
    if reply.get('resource_occupancy', 'unknown') not in ('unknown', 'not_measured'):
        raise ValueError('unverified executor physical occupancy claim')
    return reply
