"""Native ownership and reconciliation seam for the opt-in CPU transport."""
from __future__ import annotations

import time
from looplab.core.cluster import NATIVE_BINDING
from looplab.core.errors import ConfigRefusal
from looplab.events.types import EV_PAUSE


def preflight(task, settings):
    if getattr(settings, 'cluster_executor', None) is None:
        return
    import os
    if os.name != 'posix':
        raise ConfigRefusal('cluster stdio transport requires a POSIX client and Linux worker')
    for hook in ('repo_spec', 'eval_spec', 'host_grader'):
        fn = getattr(task, hook, None)
        if callable(fn) and fn():
            raise ConfigRefusal('cluster executor supports code-only CPU tasks; repository commands and host grading remain local')
    from looplab.core.cluster import text_assets
    fn = getattr(task, 'assets', None)
    text_assets(fn() if callable(fn) else {})


def run_attempt(engine, a, cancel):
    request = engine._resource_request_for_node(a.node)
    if (getattr(engine, '_eval_parallel', 1) != 1 or not request['cpu_only'] or request['count'] != 0 or a.node.files or a.node.uses
            or getattr(engine, '_eval_spec', None) or getattr(engine, '_host_grader', None)
            or getattr(engine, '_repo_spec', None) or getattr(a.node, 'kind', None) == 'artifact'):
        raise ConfigRefusal('cluster executor needs an explicit zero-GPU code-only native candidate, without side files, artifact producers/parents or host grading')
    binding = {'run_uid': a.state.run_uid, 'node_id': a.node_id, 'generation': a.generation,
               'attempt': a.attempt, 'invocation_id': a.invocation_id,
               'task_id': str(engine.task.id), 'resources': {'gpus': 0, 'cpu_slots': 1}}
    if (a.eval_env or {}).get('CUDA_VISIBLE_DEVICES', '') not in ('', '-1'):
        raise ConfigRefusal('cluster executor cannot move a local GPU reservation to another host')
    token = NATIVE_BINDING.set({'binding': binding, 'assets': dict(engine._assets),
                                'reconcile': a.invocation_id in a.unsettled_at_start})
    try:
        return engine._run_eval(a.node, str(a.workdir), {'CUDA_VISIBLE_DEVICES': ''}, None, cancel, a.next_start)
    finally:
        NATIVE_BINDING.reset(token)


async def withhold_unknown(engine, a, exc):
    """No terminal/repair/new key. Keep the invocation open and its client wait honestly charged."""
    async with engine._write_lock:
        engine.store.append(EV_PAUSE, {'reason': 'cluster executor outcome UNKNOWN; reconcile the existing native invocation before resuming'})
    await engine._record_eval_withheld(
        a, 'executor_reconcile', time.time() - a._t0, reason='executor_unknown',
        executor_receipt=exc.receipt)


def claim_record(engine, a):
    from looplab.core.cluster import digest
    import hashlib
    config = engine.sandbox.config
    return {'worker_id': config['worker_id'],
        'transport_sha256': digest({'argv': config['argv'], 'worker_id': config['worker_id']}),
        'binding': {'run_uid': a.state.run_uid, 'node_id': a.node_id, 'generation': a.generation,
                    'attempt': a.attempt, 'invocation_id': a.invocation_id,
                    'task_id': str(engine.task.id), 'resources': {'gpus': 0, 'cpu_slots': 1}},
        'source_sha256': hashlib.sha256(a.node.code.encode('utf-8')).hexdigest(),
        'input_sha256': {k: hashlib.sha256(v.encode('utf-8')).hexdigest() for k, v in engine._assets.items()}}


async def reconcile_previous(engine, a):
    """A reset cannot erase remote execution ownership or move an open key to a different worker."""
    import anyio
    from looplab.core.cluster import ExecutorOutcomeUnknown
    from looplab.events.types import EV_EVAL_INVOCATION_CLAIMED, EV_EVAL_INVOCATION_SETTLED
    current = claim_record(engine, a)
    claims = open_claims(a.events_at_start)
    # A local superseded/aborted node terminal is not proof that a remote process stopped.
    for key, previous in claims.items():
        if (previous.get('transport_sha256') != current['transport_sha256']
                or previous.get('worker_id') != current['worker_id']):
            raise ExecutorOutcomeUnknown({**previous, 'state': 'unknown',
                'detail': 'open_native_invocation_cannot_change_worker_transport'})
        if key == a.invocation_id:
            continue  # the normal invocation path reattaches and consumes its result once
        receipt = await anyio.to_thread.run_sync(engine.sandbox.reconcile_claim, previous)
        async with engine._write_lock:
            engine.store.append(EV_EVAL_INVOCATION_SETTLED, {
                'node_id': previous['binding']['node_id'], 'generation': previous['binding']['generation'],
                'attempt': previous['binding']['attempt'], 'invocation_id': key,
                'outcome': 'superseded', 'eval_seconds': 0., 'executor_receipt': receipt,
                'reconciled_for_invocation': a.invocation_id,
                'cost_evidence': 'remote execution receipt retained; client wait/remote wall overlap and billing not inferred'})


def open_claims(events):
    """Only a remote settle closes remote ownership; local node terminals cannot do so."""
    from looplab.events.types import EV_EVAL_INVOCATION_CLAIMED, EV_EVAL_INVOCATION_SETTLED
    claims = {}
    for event in events:
        key = event.data.get('invocation_id')
        if event.type == EV_EVAL_INVOCATION_CLAIMED and event.data.get('executor_contract'):
            claims[key] = event.data['executor_contract']
        elif event.type == EV_EVAL_INVOCATION_SETTLED:
            claims.pop(key, None)
    return claims


async def drain_before_finish(engine):
    """After native eval children join, stop/reconcile their remote claims before any finish."""
    import anyio
    from looplab.core.cluster import ExecutorOutcomeUnknown, digest
    from looplab.events.types import EV_EVAL_INVOCATION_SETTLED, EV_EVAL_ATTEMPT_WITHHELD
    config = engine.sandbox.config
    transport_sha = digest({'argv': config['argv'], 'worker_id': config['worker_id']})
    for key, previous in open_claims(engine.store.read_all()).items():
        binding = previous['binding']
        started = time.monotonic()
        try:
            if previous.get('transport_sha256') != transport_sha:
                raise ExecutorOutcomeUnknown({**previous, 'state': 'unknown',
                    'detail': 'open_native_invocation_cannot_change_worker_transport'})
            receipt = await anyio.to_thread.run_sync(engine.sandbox.reconcile_claim, previous)
        except ExecutorOutcomeUnknown as exc:
            async with engine._write_lock:
                engine.store.append(EV_PAUSE, {'reason': 'finalize deferred: remote execution outcome UNKNOWN; reconcile original worker ownership'})
                engine.store.append(EV_EVAL_ATTEMPT_WITHHELD, {
                    'node_id': binding['node_id'], 'generation': binding['generation'],
                    'attempt': binding['attempt'], 'at': 'executor_reconcile', 'reason': 'executor_unknown',
                    'eval_seconds': round(time.monotonic() - started, 3), 'executor_receipt': exc.receipt})
            return False
        async with engine._write_lock:
            engine.store.append(EV_EVAL_INVOCATION_SETTLED, {
                'node_id': binding['node_id'], 'generation': binding['generation'],
                'attempt': binding['attempt'], 'invocation_id': key, 'outcome': 'aborted',
                'eval_seconds': 0., 'executor_receipt': receipt, 'reconciled_for_invocation': 'finalize',
                'cost_evidence': 'remote execution receipt retained; client wait/remote wall overlap and billing not inferred'})
    return True
