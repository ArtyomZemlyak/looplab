"""Bounded stdio RPC to an operator-declared CPU worker. No daemon, shell, or local GPU lease copy."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from looplab.core.cluster import (NATIVE_BINDING, MAX_REQUEST, ExecutorOutcomeUnknown,
    canonical, digest, finite_number, normalize_executor, text_assets, validate_binding, validate_receipt)
from looplab.core.envsafe import validate_env_map
from looplab.core.errors import ConfigRefusal
from looplab.runtime.sandbox import (RunResult, _parse_metric, json_line_extras, json_line_trials,
                                     stdout_extra_metric_channels)


def transport_env():
    # No local harness credentials, settings, private env or candidate-supplied interpreter paths.
    return {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL') if k in os.environ}


class ClusterSandbox:
    requires_native_binding = True

    def __init__(self, config):
        if os.name != 'posix':
            raise ConfigRefusal('cluster stdio transport requires a POSIX client and Linux worker')
        self.config = normalize_executor(config)

    def _rpc(self, request):
        from looplab.runtime.cluster_rpc import exchange
        try:
            reply = json.loads(exchange(self.config['argv'], canonical(request), transport_env(),
                                        self.config['rpc_timeout_seconds']))
        except (ValueError, UnicodeError, RecursionError, subprocess.SubprocessError) as exc:
            raise OSError('executor RPC reply invalid') from exc
        if not isinstance(reply, dict) or reply.get('worker_id') != self.config['worker_id']:
            raise OSError('executor placement identity mismatch')
        return reply

    def run(self, code, workdir, timeout=30., env=None, cancel=None):
        context = NATIVE_BINDING.get()
        if context is None:
            raise ConfigRefusal('cluster execution requires a normally admitted native attempt; unbound probes/confirmations cannot launch')
        binding = validate_binding(context['binding'])
        assets = text_assets(context['assets'])
        if not isinstance(code, str) or len(code.encode('utf-8')) > 128 * 1024 or not finite_number(timeout, .1, 86400):
            raise ConfigRefusal('cluster code or native timeout exceeds the bounded CPU contract')
        clean = dict(env or {})
        if clean.pop('CUDA_VISIBLE_DEVICES', '') not in ('', '-1'):
            raise ConfigRefusal('cluster transport cannot forward a host-local GPU lease')
        clean, error = validate_env_map('cluster evaluation environment', clean)
        if error:
            raise ConfigRefusal(error)
        # Fail on changed staged assets before contacting a worker. Do not walk the local workdir.
        for name, content in assets.items():
            path = Path(workdir) / name
            if any(p.is_symlink() for p in [path, *path.parents[:len(Path(name).parts)]]):
                raise ConfigRefusal('cluster declared input contains a symlink')
            if path.read_bytes() != content.encode('utf-8'):
                raise ConfigRefusal('cluster declared input changed before submission')
        payload = {'v': 1, 'binding': binding, 'code': code, 'assets': assets,
                   'timeout_seconds': float(timeout), 'env': clean}
        payload_sha, job = digest(payload), digest(binding['invocation_id'])
        if cancel is not None and cancel.is_set() and not context.get('reconcile'):
            return RunResult(exit_code=-1, stdout='', stderr='cancelled before cluster submission',
                metric=None, timed_out=False, executor_receipt={'state': 'not_submitted',
                    'binding': binding, 'payload_sha256': payload_sha, 'execution_seconds': 0.,
                    'host': None, 'billing': 'not_invoked'})

        base = {'v': 1, 'worker_id': self.config['worker_id'], 'job_id': job, 'payload_sha256': payload_sha}
        if len(canonical({**base, 'op': 'submit', 'payload': payload})) > MAX_REQUEST:
            raise ConfigRefusal('encoded cluster request exceeds 1MiB; narrow the declared text input before submission')
        receipt = {**base, 'binding': binding, 'state': 'unknown', 'billing': 'unknown',
                   'execution_seconds': None, 'resource_occupancy': 'unknown'}
        # On every retry submit is reconciliation of THIS immutable key; the worker never relaunches it.
        last_reply, submitted, failures = None, False, 0
        if cancel is not None and cancel.is_set() and context.get('reconcile'):
            submitted = True
        stop_at = time.monotonic() + timeout + self.config['rpc_timeout_seconds'] * 2 + 5
        while time.monotonic() < stop_at:
            cancelling = cancel is not None and cancel.is_set()
            op = 'cancel' if cancelling and submitted else ('status' if submitted else 'submit')
            req = {**base, 'op': op}
            if op == 'submit':
                req['payload'] = payload
            try:
                reply = self._rpc(req)
                validate_receipt(reply, self.config['worker_id'], binding,
                    hashlib.sha256(code.encode('utf-8')).hexdigest(),
                    {k: hashlib.sha256(v.encode('utf-8')).hexdigest() for k, v in assets.items()}, None if reply.get('state') == 'conflict' else payload_sha)
                last_reply = reply
                submitted = True
                state = reply.get('state')
                if state == 'finished':
                    result = reply.get('result')
                    if (not isinstance(result, dict) or type(result.get('exit_code')) is not int
                            or type(result.get('timed_out')) is not bool
                            or any(not isinstance(result.get(k), str) or len(result[k].encode('utf-8')) > 256000
                                   for k in ('stdout', 'stderr'))
                            or result.get('metric') is not None and not finite_number(result['metric'], -1e308, 1e308)
                            or not isinstance(reply.get('host'), str) or not reply['host'].strip()
                            or not finite_number(reply.get('execution_seconds'), 0, 86460)):

                        raise OSError('executor terminal result is invalid')
                    # The worker's result is still candidate self-report, with normal metric parsing/trust.
                    from dataclasses import asdict
                    out, timed_out = result['stdout'], result['timed_out']
                    extras = None if timed_out else (json_line_extras(out) or None)
                    res = RunResult(exit_code=result['exit_code'], stdout=out, stderr=result['stderr'],
                        metric=None if timed_out else _parse_metric(out), timed_out=timed_out,
                        extra_metrics=extras, extra_metrics_provenance=stdout_extra_metric_channels(extras),
                        trials=None if timed_out else json_line_trials(out))
                    if canonical(asdict(res)) != canonical(result) or reply.get('output_sha256') != {
                            k: hashlib.sha256(result[k].encode('utf-8')).hexdigest() for k in ('stdout', 'stderr')}:
                        raise OSError('executor result/captured-output identity mismatch')

                    res.executor_receipt = {k: v for k, v in reply.items() if k != 'result'}
                    Path(workdir, 'cluster-receipt.json').write_bytes(canonical(res.executor_receipt))
                    return res
                if state in ('unknown', 'conflict', 'refused'):
                    raise ExecutorOutcomeUnknown({k: v for k, v in reply.items() if k != 'result'})
                if state not in ('launch_claimed', 'running'):
                    raise OSError('executor receipt state is unsupported')
            except (OSError, ValueError, TypeError, RecursionError):
                # A lost submit reply is not proof of no submission; repeat the SAME immutable key.
                failures += 1
                if failures >= 3:
                    break
            else:
                failures = 0
            time.sleep(self.config['poll_seconds'])
        if last_reply:
            receipt.update({k: v for k, v in last_reply.items() if k != 'result'})
        receipt['state'] = 'unknown'
        receipt['detail'] = 'terminal_receipt_unavailable'
        raise ExecutorOutcomeUnknown(receipt)

    def reconcile_claim(self, claim):
        """Stop/reconcile an obsolete native attempt; never submit a replacement for it."""
        binding = validate_binding(claim['binding'])
        base = {'v': 1, 'worker_id': self.config['worker_id'],
                'job_id': digest(binding['invocation_id'])}
        unknown = {**claim, **base, 'state': 'unknown', 'detail': 'retired_invocation_not_reconciled'}
        deadline = time.monotonic() + self.config['rpc_timeout_seconds'] * 2 + 5
        try:
            reply = self._rpc({**base, 'op': 'inspect'})
            while True:
                validate_receipt(reply, self.config['worker_id'], binding,
                    claim['source_sha256'], claim['input_sha256'])
                unknown = {k: v for k, v in reply.items() if k != 'result'}
                if reply.get('state') == 'finished':
                    if not finite_number(reply.get('execution_seconds'), 0, 86460):
                        raise OSError('retired receipt execution timing invalid')
                    return unknown
                if reply.get('state') not in ('running', 'launch_claimed') or time.monotonic() >= deadline:
                    break
                time.sleep(self.config['poll_seconds'])
                reply = self._rpc({**base, 'op': 'cancel', 'payload_sha256': reply['payload_sha256']})
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            pass
        unknown['state'] = 'unknown'
        raise ExecutorOutcomeUnknown(unknown)
