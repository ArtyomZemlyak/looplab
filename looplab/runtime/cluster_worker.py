"""One bounded JSON RPC per process; operator-owned durable claims survive client reconnects.

Invoke via a fixed transport argv (for example an installed remote SSH command). No listener,
credential copying, scheduler discovery or GPU reservation is created by this module.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time

from looplab.core.cluster import (MAX_REQUEST, canonical, digest, finite_number, text_assets,
                                  validate_binding)
from looplab.core.envsafe import validate_env_map
from looplab.runtime.cluster import transport_env
from looplab.runtime.sandbox import SubprocessSandbox


def _write(path, value):
    fd, temp = tempfile.mkstemp(prefix='.atomic-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _read(path):
    if path.is_symlink() or path.stat().st_size > MAX_REQUEST:
        raise ValueError('invalid worker record')
    return json.loads(path.read_bytes())


def _proc_token(pid):
    try:
        fields = Path('/proc', str(pid), 'stat').read_text().rpartition(')')[2].split()
        return fields[19] if fields[0] not in ('Z', 'X') else None
    except (OSError, IndexError):
        return None


@contextmanager
def _lock(job):
    with (job / 'lock').open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _base(request, worker):
    if (not isinstance(request, dict) or type(request.get('v')) is not int or request.get('v') != 1
            or request.get('worker_id') != worker or request.get('op') not in ('submit', 'status', 'cancel', 'inspect')
            or set(request) - {'v', 'worker_id', 'job_id', 'payload_sha256', 'op', 'payload'}
            or any(not isinstance(request.get(k), str) or not re.fullmatch('[0-9a-f]{64}', request[k])
                   for k in (('job_id',) if request.get('op') == 'inspect' else ('job_id', 'payload_sha256')))):
        raise ValueError('invalid worker RPC envelope')
    return {k: request[k] for k in ('v', 'worker_id', 'job_id', 'payload_sha256') if k in request}


def _payload(value):
    if (not isinstance(value, dict) or set(value) != {'v', 'binding', 'code', 'assets', 'timeout_seconds', 'env'}
            or type(value['v']) is not int or value['v'] != 1 or not isinstance(value['code'], str)
            or len(value['code'].encode('utf-8')) > 128 * 1024
            or not finite_number(value['timeout_seconds'], .1, 86400)):
        raise ValueError('invalid worker execution payload')
    validate_binding(value['binding']); text_assets(value['assets'])
    env, error = validate_env_map('worker environment', value['env'])
    if error or env != value['env']:
        raise ValueError(error or 'invalid worker environment')
    return value


def _handle_locked(root, worker, request):
    base = _base(request, worker)
    job = root / base['job_id']
    if request['op'] == 'submit':
        payload = _payload(request.get('payload'))
        if (digest(payload) != base['payload_sha256']
                or digest(payload['binding']['invocation_id']) != base['job_id']):
            raise ValueError('worker payload identity mismatch')
        if not job.exists() and (root / 'active.json').exists():
            active = _read(root / 'active.json')
            if not isinstance(active.get('job_id'), str) or not re.fullmatch('[0-9a-f]{64}', active['job_id']):
                raise ValueError('invalid active worker claim')
            previous = _read(root / active['job_id'] / 'status.json')
            if previous.get('state') != 'finished':
                return {**base, 'binding': payload['binding'], 'state': 'refused',
                    'detail': 'worker_slot_has_unfinished_invocation', 'blocking_job_id': active['job_id'],
                    'execution_seconds': 0., 'billing': 'not_invoked'}
        job.mkdir(mode=0o700, exist_ok=True)
    elif not job.is_dir():
        return {**base, 'state': 'unknown', 'detail': 'job_not_found'}
    if job.is_symlink():
        raise ValueError('worker job path is not a real directory')
    with _lock(job):
        if not (job / 'request.json').exists():
            if request['op'] != 'submit':
                return {**base, 'state': 'unknown', 'detail': 'claim_not_found'}
            _write(job / 'request.json', payload)
            receipt = {**base, 'binding': payload['binding'], 'state': 'launch_claimed',
                'accepted_at': time.time(), 'host': socket.gethostname(),
                'source_sha256': hashlib.sha256(payload['code'].encode()).hexdigest(),
                'input_sha256': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in payload['assets'].items()},
                'execution_seconds': None, 'billing': 'unknown', 'resource_occupancy': 'unknown'}
            # A durable launch claim precedes the side effect. A crash in the gap is UNKNOWN,
            # never authority for another Popen. Lost submit replies re-read this same claim.
            _write(job / 'status.json', receipt)
            _write(root / 'active.json', {'job_id': base['job_id']})
            try:
                subprocess.Popen([sys.executable, '-m', 'looplab.runtime.cluster_worker',
                    '--root', str(root), '--worker-id', worker, '--execute', base['job_id']],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=transport_env(), start_new_session=True, close_fds=True)
            except OSError:
                receipt.update(state='unknown', detail='launch_not_confirmed')
                _write(job / 'status.json', receipt)
            return receipt
        original = _payload(_read(job / 'request.json'))
        receipt = _read(job / 'status.json')
        if request['op'] != 'inspect' and digest(original) != base['payload_sha256']:
            return {**receipt, 'state': 'conflict', 'detail': 'immutable_invocation_payload_changed'}
        if request['op'] == 'cancel':
            _write(job / 'cancel.json', {'requested_at': time.time()})
        if receipt['state'] == 'launch_claimed' and time.time() - receipt['accepted_at'] > 10:
            receipt.update(state='unknown', detail='launch_claim_has_no_execution_receipt')
            _write(job / 'status.json', receipt)
        if receipt['state'] == 'running' and (
                receipt.get('host') != socket.gethostname()
                or _proc_token(receipt['executor_pid']) != receipt.get('executor_process_token')):
            receipt.update(state='unknown', detail='executor_lost_without_terminal_receipt', resource_occupancy='unknown')
            _write(job / 'status.json', receipt)
        return receipt



def handle(root, worker, request):
    # One declared CPU slot per worker root, shared by all clients/runs. This is not a GPU lease.
    with _lock(root):
        return _handle_locked(root, worker, request)


def execute(root, worker, job_id):
    if not re.fullmatch('[0-9a-f]{64}', job_id):
        raise ValueError('invalid worker job id')
    job = root / job_id
    with _lock(job):
        receipt = _read(job / 'status.json')
        if receipt['worker_id'] != worker or receipt['state'] != 'launch_claimed':
            return  # a second executor process never buys a second command
        if receipt.get('host') != socket.gethostname() or not _proc_token(os.getpid()):
            receipt.update(state='unknown', detail='worker_placement_or_process_identity_changed')
            _write(job / 'status.json', receipt); return
        payload = _payload(_read(job / 'request.json'))
        if digest(payload) != receipt['payload_sha256']:
            receipt.update(state='unknown', detail='stored_payload_changed')
            _write(job / 'status.json', receipt); return
        receipt.update(state='running', started_at=time.time(), executor_pid=os.getpid(),
                       executor_process_token=_proc_token(os.getpid()))
        _write(job / 'status.json', receipt)
    started = time.monotonic()
    try:
        workdir = job / 'work'; workdir.mkdir(mode=0o700)
        for name, content in payload['assets'].items():
            path = workdir / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        class Cancellation:
            def is_set(self):
                return (job / 'cancel.json').exists()
        result = SubprocessSandbox().run(payload['code'], str(workdir), payload['timeout_seconds'],
            {**payload['env'], 'CUDA_VISIBLE_DEVICES': '', 'OMP_NUM_THREADS': '1',
             'MKL_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'}, cancel=Cancellation())
        (job / 'stdout.log').write_text(result.stdout, encoding='utf-8')
        (job / 'stderr.log').write_text(result.stderr, encoding='utf-8')
        receipt.update(state='finished', finished_at=time.time(),
            execution_seconds=round(time.monotonic() - started, 6), resource_occupancy='not_measured',
            cancel_requested=(job / 'cancel.json').exists(), result=asdict(result),
            output_sha256={'stdout': hashlib.sha256(result.stdout.encode()).hexdigest(),
                           'stderr': hashlib.sha256(result.stderr.encode()).hexdigest()})
    except Exception:  # noqa: BLE001 — worker failure must write an explicit unknown receipt rather than aborting status.json
        # A worker exception is no fabricated failed metric, exit or proven stop.
        receipt.update(state='unknown', detail='worker_exception_without_terminal_receipt', resource_occupancy='unknown')
    with _lock(job):
        _write(job / 'status.json', receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--worker-id', required=True)
    parser.add_argument('--execute', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != 'linux' or not _proc_token(os.getpid()) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', args.worker_id):
        parser.error('worker requires Linux process identity and an explicit short worker ID')
    if not args.root.is_absolute() or args.root.is_symlink():
        parser.error('worker root must be an explicit real absolute durable directory')
    args.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.execute:
        execute(args.root, args.worker_id, args.execute)
        return
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError('worker request exceeds 1MiB')
        response = handle(args.root, args.worker_id, json.loads(raw))
        sys.stdout.buffer.write(canonical(response))
    except (OSError, ValueError, TypeError, KeyError):
        # Do not echo payload/source/transport argv/private paths into an error receipt.
        sys.stderr.write('cluster worker refused an invalid request or unavailable durable state\n')
        raise SystemExit(2)


if __name__ == '__main__':
    main()
