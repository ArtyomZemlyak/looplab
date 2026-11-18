"""Real CPU worker subprocesses and the normal native lifecycle; no cross-host claim."""
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

import pytest

from looplab.core.cluster import NATIVE_BINDING, ExecutorOutcomeUnknown, canonical, digest
from looplab.runtime.cluster import ClusterSandbox

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='CPU worker uses Linux process identity')


def config(tmp_path):
    return {'argv': [sys.executable, '-m', 'looplab.runtime.cluster_worker', '--root',
                     str(tmp_path / 'worker'), '--worker-id', 'cpu-fixture'],
            'worker_id': 'cpu-fixture', 'rpc_timeout_seconds': 3., 'poll_seconds': .03}


def binding():
    return {'run_uid': 'synthetic-run-uid', 'node_id': 0, 'generation': 0, 'attempt': 0,
            'invocation_id': 'synthetic-run-uid:n0:g0:a0', 'task_id': 'cpu-fixture',
            'resources': {'gpus': 0, 'cpu_slots': 1}}


def run_bound(sandbox, tmp_path, code, *, assets=None, cancel=None, timeout=5, native=None):
    workdir = tmp_path / 'client'; workdir.mkdir(exist_ok=True)
    for name, text in (assets or {}).items():
        path = workdir / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
    token = NATIVE_BINDING.set({'binding': native or binding(), 'assets': assets or {}})
    try:
        return sandbox.run(code, str(workdir), timeout, cancel=cancel)
    finally:
        NATIVE_BINDING.reset(token)


def test_real_worker_pinned_source_inputs_actual_placement_and_exact_retry(tmp_path):
    sandbox = ClusterSandbox(config(tmp_path))
    code = ('from pathlib import Path\nimport os,json\n'
            'assert os.environ["CUDA_VISIBLE_DEVICES"]==""\n'
            'assert "HARNESS_FAKE_SECRET" not in os.environ\n'
            'p=Path("launches");p.write_text(p.read_text()+"x" if p.exists() else "x")\n'
            'assert Path("nested/input.txt").read_text()=="frozen"\nprint(json.dumps({"metric":.4}))')
    os.environ['HARNESS_FAKE_SECRET'] = 'synthetic-not-a-credential'
    try:
        first = run_bound(sandbox, tmp_path, code, assets={'nested/input.txt': 'frozen'})
        second = run_bound(ClusterSandbox(config(tmp_path)), tmp_path, code, assets={'nested/input.txt': 'frozen'})
    finally:
        del os.environ['HARNESS_FAKE_SECRET']
    assert first.exit_code == second.exit_code == 0 and first.metric == second.metric == .4
    receipt = first.executor_receipt
    assert receipt == second.executor_receipt
    assert receipt['binding'] == binding() and receipt['host'] == os.uname().nodename
    assert receipt['execution_seconds'] > 0 and receipt['billing'] == 'unknown'
    assert receipt['source_sha256'] == hashlib.sha256(code.encode()).hexdigest()
    assert receipt['input_sha256'] == {'nested/input.txt': hashlib.sha256(b'frozen').hexdigest()}
    assert (tmp_path / 'worker' / receipt['job_id'] / 'work/launches').read_text() == 'x'
    assert receipt['output_sha256']['stdout'] == hashlib.sha256(first.stdout.encode()).hexdigest()


def test_same_key_changed_content_never_launches_again(tmp_path):
    sandbox = ClusterSandbox(config(tmp_path))
    run_bound(sandbox, tmp_path, 'print("{\\"metric\\":1}")')
    with pytest.raises(ExecutorOutcomeUnknown):
        run_bound(sandbox, tmp_path, 'raise RuntimeError("different")', timeout=.1)
    assert len(list((tmp_path / 'worker').glob('*/work/solution.py'))) == 1


def test_lost_submit_reply_is_reconciled_without_second_execution(tmp_path):
    class LostReply(ClusterSandbox):
        lost = False
        def _rpc(self, req):
            reply = super()._rpc(req)
            if req['op'] == 'submit' and not self.lost:
                self.lost = True
                raise OSError('synthetic lost accepted reply')
            return reply
    sandbox = LostReply(config(tmp_path))
    result = run_bound(sandbox, tmp_path, 'from pathlib import Path\nPath("once").touch(exist_ok=False)\nprint("{\\"metric\\":1}")')
    assert result.exit_code == 0 and result.metric == 1 and sandbox.lost


@pytest.mark.parametrize('operation', ['timeout', 'cancel'])
def test_worker_timeout_and_native_cancel_wait_for_proven_terminal(tmp_path, operation):
    sandbox = ClusterSandbox(config(tmp_path))
    cancel = threading.Event()
    timer = threading.Timer(.6, cancel.set) if operation == 'cancel' else None
    if timer: timer.start()
    result = run_bound(sandbox, tmp_path, 'import time\nprint("{\\"metric\\":99}",flush=True)\ntime.sleep(30)',
                       cancel=cancel, timeout=.2 if operation == 'timeout' else 4)
    if timer: timer.join()
    assert result.metric is None and result.exit_code != 0
    assert result.executor_receipt['state'] == 'finished'
    assert result.timed_out if operation == 'timeout' else result.executor_receipt['cancel_requested']


def test_unbound_call_refuses_before_transport(tmp_path):
    sandbox = ClusterSandbox(config(tmp_path))
    with pytest.raises(ValueError, match='native attempt'):
        sandbox.run('pass', str(tmp_path))
    assert not (tmp_path / 'worker').exists()


def test_normal_native_engine_records_attempt_result_and_cost(tmp_path):
    import anyio
    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from tests.test_inline_repair import TASK, _Stub
    from tests.test_best_of_n_native_pilot import _Developer
    class CPUResearcher(_Stub):
        def propose(self, state, parent):
            return super().propose(state, parent).model_copy(update={'footprint': {'gpus': 0}})
    engine = Engine(tmp_path / 'native', task=ToyTask.load(TASK), researcher=CPUResearcher(),
        developer=_Developer(['print("{\\"metric\\":0.25}")']), sandbox=ClusterSandbox(config(tmp_path)),
        policy=GreedyTree(n_seeds=1, max_nodes=1), timeout=5, auto_install_deps=False,
        inline_repair=False, concurrent_research=False)
    state = anyio.run(engine.run)
    assert state.finished and state.nodes[0].metric == .25
    events = engine.store.read_all()
    terminals = [e for e in events if e.type in ('node_evaluated', 'node_failed')]
    assert len(terminals) == 1
    receipt = terminals[0].data['executor_receipt']
    claim = next(e for e in events if e.type == 'eval_invocation_claimed')
    settle = next(e for e in events if e.type == 'eval_invocation_settled')
    assert receipt['binding']['invocation_id'] == claim.data['invocation_id']
    assert receipt['binding']['run_uid'] == state.run_uid
    assert settle.data['executor_receipt'] == receipt
    assert settle.data['result']['executor_receipt'] == receipt
    assert terminals[0].data['eval_seconds'] >= receipt['execution_seconds'] > 0
    assert [e.type for e in events].index('node_eval_started') < [e.type for e in events].index('eval_invocation_claimed')


def _native(tmp_path, sandbox, code='print("{\\"metric\\":0.25}")'):
    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from tests.test_inline_repair import TASK, _Stub
    from tests.test_best_of_n_native_pilot import _Developer
    class CPUResearcher(_Stub):
        def propose(self, state, parent):
            return super().propose(state, parent).model_copy(update={'footprint': {'gpus': 0}})
    return Engine(tmp_path / 'native', task=ToyTask.load(TASK), researcher=CPUResearcher(),
        developer=_Developer([code]), sandbox=sandbox, policy=GreedyTree(n_seeds=1, max_nodes=1),
        timeout=5, auto_install_deps=False, inline_repair=False, concurrent_research=False)


class DisconnectedAfterSubmit(ClusterSandbox):
    accepted = False
    def _rpc(self, req):
        if self.accepted:
            raise OSError('synthetic disconnected client')
        reply = super()._rpc(req)
        if req['op'] == 'submit':
            self.accepted = True
            raise OSError('synthetic lost accepted reply')
        return reply


def test_native_unknown_keeps_invocation_open_then_resume_reattaches_once(tmp_path):
    import anyio
    from looplab.events.types import EV_RESUME
    code = 'from pathlib import Path\nimport time\nPath("once").touch(exist_ok=False)\ntime.sleep(.4)\nprint("{\\"metric\\":0.25}")'
    first = _native(tmp_path, DisconnectedAfterSubmit(config(tmp_path)), code)
    unknown = anyio.run(first.run)
    assert unknown.paused and not unknown.finished and unknown.nodes[0].status == 'pending'
    events = first.store.read_all()
    held = next(e for e in events if e.type == 'eval_attempt_withheld')
    assert held.data['reason'] == 'executor_unknown' and held.data['executor_receipt']['state'] == 'unknown'
    assert not [e for e in events if e.type in ('node_evaluated', 'node_failed', 'eval_invocation_settled')]
    first.store.append(EV_RESUME, {'reason': 'synthetic reconnect to same pinned worker'})
    second = _native(tmp_path, ClusterSandbox(config(tmp_path)), code)
    state = anyio.run(second.run)
    assert state.finished and state.nodes[0].metric == .25
    events = second.store.read_all()
    claims = [e.data['invocation_id'] for e in events if e.type == 'eval_invocation_claimed']
    assert len(claims) == 2 and len(set(claims)) == 1
    assert len([e for e in events if e.type in ('node_evaluated', 'node_failed')]) == 1
    assert [p.read_text() for p in (tmp_path / 'worker').glob('*/work/once')] == ['']
    assert len(list((tmp_path / 'worker').glob('*/work/solution.py'))) == 1
    assert state.total_eval_seconds >= held.data['eval_seconds']


def test_reset_reconciles_and_stops_old_remote_attempt_before_new_generation(tmp_path):
    import anyio
    from looplab.events.types import EV_RESUME, EV_NODE_RESET
    code = 'import time\ntime.sleep(1)\nprint("{\\"metric\\":0.25}")'
    first = _native(tmp_path, DisconnectedAfterSubmit(config(tmp_path)), code)
    assert anyio.run(first.run).paused
    first.store.append(EV_NODE_RESET, {'node_id': 0, 'generation': 0, 'from_stage': 'eval'})
    first.store.append(EV_RESUME, {'reason': 'synthetic reset recovery'})
    second = _native(tmp_path, ClusterSandbox(config(tmp_path)), code)
    state = anyio.run(second.run)
    assert state.finished and state.nodes[0].attempt == 1 and state.nodes[0].metric == .25
    events = second.store.read_all()
    retired = next(e for e in events if e.type == 'eval_invocation_settled' and e.data.get('reconciled_for_invocation'))
    assert retired.data['generation'] == 0 and retired.data['outcome'] == 'superseded'
    assert retired.data['executor_receipt']['state'] == 'finished'
    assert retired.data['executor_receipt']['cancel_requested']
    new = next(e for e in events if e.type == 'node_evaluated' and e.data['generation'] == 1)
    assert new.data['executor_receipt']['started_at'] >= retired.data['executor_receipt']['finished_at']
    assert len([e for e in events if e.type == 'node_evaluated']) == 1


def test_open_invocation_cannot_move_to_new_worker_or_root(tmp_path):
    import anyio
    from looplab.events.types import EV_RESUME
    first = _native(tmp_path, DisconnectedAfterSubmit(config(tmp_path)))
    assert anyio.run(first.run).paused
    first.store.append(EV_RESUME, {'reason': 'synthetic transport change must refuse'})
    different = config(tmp_path / 'different')
    class NeverContact(ClusterSandbox):
        def _rpc(self, req):
            raise AssertionError('open invocation must be fenced before contacting a new worker')
    second = _native(tmp_path, NeverContact(different))
    state = anyio.run(second.run)
    assert state.paused and not state.finished
    held = [e for e in second.store.read_all() if e.type == 'eval_attempt_withheld'][-1]
    assert held.data['executor_receipt']['detail'] == 'open_native_invocation_cannot_change_worker_transport'
    assert not (tmp_path / 'different').exists()


def test_worker_restart_without_result_is_unknown_and_never_relaunches(tmp_path):
    class KillOwnExecutor(ClusterSandbox):
        killed = False
        def _rpc(self, req):
            reply = super()._rpc(req)
            work = tmp_path / 'worker' / reply['job_id'] / 'work'
            if reply['state'] == 'running' and (work / 'started').exists() and not self.killed:
                from looplab.runtime.cluster_worker import _proc_token
                assert _proc_token(reply['executor_pid']) == reply['executor_process_token']
                os.kill(reply['executor_pid'], signal.SIGKILL)
                self.killed = True
            return reply
    sandbox = KillOwnExecutor(config(tmp_path))
    code = 'from pathlib import Path\nimport time\nPath("started").touch(exist_ok=False)\ntime.sleep(.3)\nprint("{\\"metric\\":1}")'
    with pytest.raises(ExecutorOutcomeUnknown):
        run_bound(sandbox, tmp_path, code)
    assert sandbox.killed
    with pytest.raises(ExecutorOutcomeUnknown):
        run_bound(ClusterSandbox(config(tmp_path)), tmp_path, code)
    assert len(list((tmp_path / 'worker').glob('*/work/started'))) == 1
    # The fixture's child has a finite .3s lifetime even after its test-owned supervisor was killed.
    time.sleep(.35)


def test_pre_cancelled_fresh_attempt_launches_nothing(tmp_path):
    cancel = threading.Event(); cancel.set()
    result = run_bound(ClusterSandbox(config(tmp_path)), tmp_path, 'raise AssertionError("must not run")', cancel=cancel)
    assert result.metric is None and result.executor_receipt['state'] == 'not_submitted'
    assert not (tmp_path / 'worker').exists()


@pytest.mark.parametrize('field,value', [
    ('run_uid', 123), ('run_uid', []), ('node_id', True), ('node_id', 1.), ('node_id', '1'),
    ('metric', True), ('metric', float('nan')), ('v', True), ('execution_seconds', True),
    ('source_sha256', '0' * 64), ('host', ''), ('host', 'broken\nhost'), ('output_sha256', {}),
    ('accepted_at', None), ('started_at', None), ('finished_at', None),
    ('executor_pid', True), ('executor_process_token', []),
])
def test_malformed_success_receipts_never_become_quality(tmp_path, field, value):
    code = 'print("{\\\"metric\\\":1}")'
    native = {**binding(), 'node_id': 1, 'invocation_id': 'synthetic-run-uid:n1:g0:a0'}
    control = run_bound(ClusterSandbox(config(tmp_path)), tmp_path, code, native=native)
    assert control.exit_code == 0 and control.metric == 1
    class Malformed(ClusterSandbox):
        def _rpc(self, req):
            reply = super()._rpc(req)
            if reply['state'] == 'finished':
                target = reply['binding'] if field in ('run_uid', 'node_id') else reply['result'] if field == 'metric' else reply
                target[field] = value
            return reply
    with pytest.raises(ExecutorOutcomeUnknown):
        run_bound(Malformed(config(tmp_path)), tmp_path, code, native=native)
    assert len(list((tmp_path / 'worker').glob('*/work/solution.py'))) == 1


@pytest.mark.parametrize('args', [
    {'trust_mode': 'untrusted'}, {'external_harness': True}, {'confirm_seeds': 2},
    {'ablate_every': 1}, {'eval_canary': True}, {'eval_noise_seeds': 2}, {'sandbox_memory_local': '1g'},
])
def test_unsupported_placement_modes_refused_in_settings(tmp_path, args):
    from looplab.core.config import Settings
    with pytest.raises(ValueError, match='cluster_executor'):
        Settings(_env_file=None, cluster_executor=config(tmp_path), **args)


def test_unsupported_task_refused_before_paid_role_preflight(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from looplab.cli import _engine
    from looplab.core.config import Settings
    import looplab.cli as cli
    called = []
    monkeypatch.setattr(cli, 'make_roles', lambda *a, **kw: called.append(True))
    task = SimpleNamespace(repo_spec=lambda: {'commands': 'unsupported'})
    with pytest.raises(ValueError, match='code-only CPU'):
        _engine(tmp_path / 'native', task, Settings(_env_file=None, eval_parallel=1, cluster_executor=config(tmp_path)), None)
    assert not called and not (tmp_path / 'native').exists()


def test_rpc_output_and_timeout_are_bounded(tmp_path):
    from looplab.runtime.cluster_rpc import exchange
    from looplab.runtime.cluster import transport_env
    with pytest.raises(OSError, match='output exceeded'):
        exchange([sys.executable, '-c', 'import os,time;os.write(1,b"x"*600000);time.sleep(3)'], b'{}', transport_env(), 2)
    with pytest.raises(OSError, match='timeout'):
        exchange([sys.executable, '-c', 'import time;time.sleep(3)'], b'{}', transport_env(), .1)


def test_declared_input_drift_and_links_refuse_before_transport(tmp_path):
    from looplab.core.errors import ConfigRefusal
    sandbox = ClusterSandbox(config(tmp_path))
    work = tmp_path / 'client'; work.mkdir()
    target = work / 'asset.txt'; target.write_text('changed')
    token = NATIVE_BINDING.set({'binding': binding(), 'assets': {'asset.txt': 'frozen'}})
    try:
        with pytest.raises(ConfigRefusal, match='changed'):
            sandbox.run('pass', str(work))
        target.unlink(); target.symlink_to(work / 'elsewhere')
        with pytest.raises(ConfigRefusal, match='symlink'):
            sandbox.run('pass', str(work))
    finally:
        NATIVE_BINDING.reset(token)
    assert not (tmp_path / 'worker').exists()


def test_encoded_request_limit_is_preflight_not_an_unknown_launch(tmp_path):
    from looplab.core.errors import ConfigRefusal
    assets = {str(i) + '.txt': '\x01' * 64000 for i in range(4)}
    with pytest.raises(ConfigRefusal, match='encoded cluster request'):
        run_bound(ClusterSandbox(config(tmp_path)), tmp_path, 'pass', assets=assets)
    assert not (tmp_path / 'worker').exists()


def test_launch_claim_gap_is_unknown_and_exact_retry_does_not_spawn(tmp_path, monkeypatch):
    from looplab.runtime import cluster_worker as worker
    root = tmp_path / 'worker'; root.mkdir()
    calls = []
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda *a, **kw: calls.append(True))
    payload = {'v': 1, 'binding': binding(), 'code': 'pass', 'assets': {}, 'timeout_seconds': 1., 'env': {}}
    request = {'v': 1, 'worker_id': 'cpu-fixture', 'job_id': digest(binding()['invocation_id']),
               'payload_sha256': digest(payload), 'op': 'submit', 'payload': payload}
    first = worker.handle(root, 'cpu-fixture', request)
    status = root / first['job_id'] / 'status.json'
    record = json.loads(status.read_text()); record['accepted_at'] -= 11
    status.write_bytes(canonical(record))
    second = worker.handle(root, 'cpu-fixture', request)
    assert len(calls) == 1 and second['state'] == 'unknown'
    assert second['detail'] == 'launch_claim_has_no_execution_receipt'


def test_worker_slot_is_shared_across_distinct_run_clients(tmp_path):
    from looplab.runtime import cluster_worker as worker
    root = tmp_path / 'worker'; root.mkdir()
    code = 'import time\ntime.sleep(.5)'
    payload = {'v': 1, 'binding': binding(), 'code': code, 'assets': {}, 'timeout_seconds': 2., 'env': {}}
    first = {'v': 1, 'worker_id': 'cpu-fixture', 'job_id': digest(binding()['invocation_id']),
             'payload_sha256': digest(payload), 'op': 'submit', 'payload': payload}
    assert worker.handle(root, 'cpu-fixture', first)['state'] == 'launch_claimed'
    different = {**binding(), 'run_uid': 'other-run', 'invocation_id': 'other-run:n0:g0:a0'}
    other_payload = {**payload, 'binding': different}
    second = {**first, 'job_id': digest(different['invocation_id']), 'payload_sha256': digest(other_payload), 'payload': other_payload}
    receipt = worker.handle(root, 'cpu-fixture', second)
    assert receipt['state'] == 'refused' and receipt['billing'] == 'not_invoked'
    assert not (root / second['job_id']).exists()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        record = json.loads((root / first['job_id'] / 'status.json').read_text())
        if record['state'] == 'finished': break
        time.sleep(.03)
    assert record['state'] == 'finished'


@pytest.mark.parametrize('change', [{'node_id': True}, {'generation': 0.}, {'attempt': '0'}, {'resources': {'gpus': True, 'cpu_slots': 1}}, {'invocation_id': 'unrelated'}])
def test_native_request_identity_is_exact_and_rederived(tmp_path, change):
    from looplab.core.cluster import validate_binding
    with pytest.raises(ValueError):
        validate_binding({**binding(), **change})


def test_inline_transport_secrets_rejected_without_echoing_value(tmp_path):
    from looplab.core.cluster import normalize_executor
    value = config(tmp_path); value['argv'] += ['--token=synthetic-only']
    with pytest.raises(ValueError, match='inline credentials') as err:
        normalize_executor(value)
    assert 'synthetic-only' not in str(err.value)


def test_placement_change_before_worker_execution_preserves_unknown_without_launch(tmp_path, monkeypatch):
    from looplab.runtime import cluster_worker as worker
    root = tmp_path / 'worker'; root.mkdir()
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda *a, **kw: None)
    payload = {'v': 1, 'binding': binding(), 'code': 'raise AssertionError("cannot launch")', 'assets': {}, 'timeout_seconds': 1., 'env': {}}
    request = {'v': 1, 'worker_id': 'cpu-fixture', 'job_id': digest(binding()['invocation_id']),
               'payload_sha256': digest(payload), 'op': 'submit', 'payload': payload}
    worker.handle(root, 'cpu-fixture', request)
    monkeypatch.setattr(worker.socket, 'gethostname', lambda: 'different-host')
    worker.execute(root, 'cpu-fixture', request['job_id'])
    status = json.loads((root / request['job_id'] / 'status.json').read_text())
    assert status['state'] == 'unknown' and status['detail'] == 'worker_placement_or_process_identity_changed'
    assert not (root / request['job_id'] / 'work').exists()


def test_old_run_snapshot_does_not_activate_ambient_cluster_transport(tmp_path, monkeypatch):
    from looplab.core.config import Settings, settings_from_snapshot
    monkeypatch.setenv('LOOPLAB_CLUSTER_EXECUTOR', json.dumps(config(tmp_path)))
    restored = settings_from_snapshot({'backend': 'toy'})
    assert restored.cluster_executor is None
    assert Settings(_env_file=None, eval_parallel=1).cluster_executor is not None


def test_two_simultaneous_clients_launch_same_native_key_only_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    barrier = threading.Barrier(2)
    code = 'from pathlib import Path\nimport time\nPath("once").touch(exist_ok=False)\ntime.sleep(.2)\nprint("{\\"metric\\":1}")'
    def client(index):
        base = tmp_path / str(index); base.mkdir()
        barrier.wait(timeout=3)
        return run_bound(ClusterSandbox(config(tmp_path)), base, code)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client, i) for i in range(2)]
        results = [f.result(timeout=10) for f in futures]
    assert all(r.exit_code == 0 and r.metric == 1 for r in results)
    assert results[0].executor_receipt == results[1].executor_receipt
    assert len(list((tmp_path / 'worker').glob('*/work/once'))) == 1


def test_native_inline_repair_uses_new_attempt_but_one_node_terminal(tmp_path):
    import anyio
    engine = _native(tmp_path, ClusterSandbox(config(tmp_path)), 'raise RuntimeError("mechanical failure")')
    class Repairable:
        def implement(self, idea):
            return 'raise RuntimeError("mechanical failure")'
        def repair(self, idea, code, error):
            return 'print("{\\"metric\\":0.25}")'
    engine.developer = Repairable()
    engine._inline_repair = True
    engine._inline_repair_attempts = 1
    state = anyio.run(engine.run)
    assert state.finished and state.nodes[0].metric == .25
    events = engine.store.read_all()
    claims = [e.data for e in events if e.type == 'eval_invocation_claimed']
    assert [r['attempt'] for r in claims] == [0, 1]
    assert len({r['invocation_id'] for r in claims}) == 2
    assert len([e for e in events if e.type in ('node_evaluated', 'node_failed')]) == 1
    assert len(list((tmp_path / 'worker').glob('*/work/solution.py'))) == 2


def test_crash_after_remote_settle_recovers_receipt_without_contact_or_rerun(tmp_path):
    import anyio
    class ProcessDeath(BaseException): pass
    first = _native(tmp_path, ClusterSandbox(config(tmp_path)))
    async def die_before_terminal(_attempt):
        raise ProcessDeath('synthetic process loss after real remote settle')
    first._eval_write_terminal = die_before_terminal
    with pytest.raises(BaseException):
        anyio.run(first.run)
    assert not [e for e in first.store.read_all() if e.type in ('node_evaluated', 'node_failed')]
    class NeverContact(ClusterSandbox):
        def _rpc(self, req):
            raise AssertionError('settled native result must recover without another worker call')
    second = _native(tmp_path, NeverContact(config(tmp_path)))
    state = anyio.run(second.run)
    assert state.finished and state.nodes[0].metric == .25
    terminal = next(e for e in second.store.read_all() if e.type == 'node_evaluated')
    assert terminal.data['executor_receipt']['state'] == 'finished'
    assert len(list((tmp_path / 'worker').glob('*/work/solution.py'))) == 1


def test_result_optional_extension_preserves_legacy_positional_drift_argument():
    from looplab.runtime.sandbox import RunResult
    drift = {'primary': 1., 'cross': .5}
    result = RunResult(0, '', '', 1., False, drift)
    assert result.drift == drift and result.executor_receipt is None


def test_artifact_producer_refuses_before_any_remote_payload_work(tmp_path):
    import anyio
    from looplab.core.models import Idea
    engine = _native(tmp_path, ClusterSandbox(config(tmp_path)))
    class ArtifactResearcher:
        def propose(self, state, parent):
            return Idea(operator='draft', footprint={'gpus': 0}, node_kind='artifact')
    engine.researcher = ArtifactResearcher()
    state = anyio.run(engine.run)
    assert not (tmp_path / 'worker').exists()
    assert not [e for e in engine.store.read_all() if e.type == 'node_evaluated']
    settled = [e for e in engine.store.read_all() if e.type == 'eval_invocation_settled']
    assert len(settled) == 1 and settled[0].data['outcome'] == 'setup_refused'


def test_public_sandbox_factory_selects_opt_in_transport_and_preserves_null(tmp_path):
    from looplab.runtime.sandbox import make_sandbox, SubprocessSandbox
    remote = make_sandbox('trusted_local', cluster_executor=config(tmp_path))
    assert isinstance(remote, ClusterSandbox)
    assert isinstance(make_sandbox('trusted_local', cluster_executor=None), SubprocessSandbox)
    with pytest.raises(ValueError, match='local Docker/rlimit'):
        make_sandbox('trusted_local', cluster_executor=config(tmp_path), mem_local='1g')


def test_finalize_cannot_erase_unknown_remote_ownership_then_reconnect_drains(tmp_path):
    import anyio
    from looplab.events.types import EV_RUN_ABORT
    first = _native(tmp_path, DisconnectedAfterSubmit(config(tmp_path)),
                    'import time\ntime.sleep(.4)\nprint("{\\"metric\\":0.25}")')
    assert anyio.run(first.run).paused
    first.store.append(EV_RUN_ABORT, {'reason': 'operator_requested_synthetic_finalize'})
    class Offline(ClusterSandbox):
        def _rpc(self, req):
            raise OSError('synthetic transport remains unavailable')
    offline = _native(tmp_path, Offline(config(tmp_path)))
    pending = anyio.run(offline.run)
    assert not pending.finished, 'an unresolved remote claim is not a quiescent completed run'
    assert not [e for e in offline.store.read_all() if e.type == 'run_finished']
    recovered = _native(tmp_path, ClusterSandbox(config(tmp_path)))
    state = anyio.run(recovered.run)
    assert state.finished
    settled = [e for e in recovered.store.read_all() if e.type == 'eval_invocation_settled']
    assert len(settled) == 1 and settled[0].data['outcome'] == 'aborted'
    assert settled[0].data['executor_receipt']['state'] == 'finished'
    assert len(list((tmp_path / 'worker').glob('*/work/solution.py'))) == 1


def test_finalize_during_healthy_remote_eval_drains_owned_work_before_finish(tmp_path):
    import anyio
    from looplab.events.types import EV_RUN_ABORT
    class FinalizeWhileRunning(ClusterSandbox):
        requested = False
        def _rpc(self, req):
            reply = super()._rpc(req)
            if reply['state'] == 'running' and not self.requested:
                self.requested = True
                engine.store.append(EV_RUN_ABORT, {'reason': 'synthetic operator finalize during CPU work'})
            return reply
    sandbox = FinalizeWhileRunning(config(tmp_path))
    engine = _native(tmp_path, sandbox, 'import time\ntime.sleep(.4)\nprint("{\\"metric\\":0.25}")')
    state = anyio.run(engine.run)
    assert state.finished and sandbox.requested
    events = engine.store.read_all()
    settled = next(e for e in events if e.type == 'eval_invocation_settled')
    finished = next(e for e in events if e.type == 'run_finished')
    assert settled.seq < finished.seq and settled.data['executor_receipt']['state'] == 'finished'
    assert not settled.data['executor_receipt']['cancel_requested']
    assert settled.data['outcome'] == 'ok'
    assert len(list((tmp_path / 'worker').glob('*/work/solution.py'))) == 1


def test_native_node_abort_reaches_remote_cancel_and_records_one_aborted_terminal(tmp_path):
    import anyio
    from looplab.events.types import EV_NODE_ABORT
    class AbortWhileRunning(ClusterSandbox):
        requested = False
        def _rpc(self, req):
            reply = super()._rpc(req)
            if reply['state'] == 'running' and not self.requested:
                self.requested = True
                engine.store.append(EV_NODE_ABORT, {'node_id': 0, 'generation': 0})
            return reply
    sandbox = AbortWhileRunning(config(tmp_path))
    engine = _native(tmp_path, sandbox, 'import time\ntime.sleep(10)\nprint("{\\"metric\\":0.25}")')
    state = anyio.run(engine.run)
    events = engine.store.read_all()
    terminals = [e for e in events if e.type in ('node_evaluated', 'node_failed')]
    assert len(terminals) == 1 and terminals[0].type == 'node_failed' and terminals[0].data['reason'] == 'aborted'
    settled = next(e for e in events if e.type == 'eval_invocation_settled')
    assert settled.data['outcome'] == 'aborted'
    assert settled.data['executor_receipt']['state'] == 'finished'
    assert settled.data['executor_receipt']['cancel_requested']
    assert settled.data['executor_receipt']['execution_seconds'] < 3
    assert len(list((tmp_path / 'worker').glob('*/work/solution.py'))) == 1
