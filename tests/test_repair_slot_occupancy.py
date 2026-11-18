"""Real native CPU evaluations: LLM repair parks width, future allowance, never ownership/cost."""
import threading

import anyio
import pytest

from looplab.core.models import Idea, NodeStatus
from looplab.events.replay import fold
from tests.test_inline_repair import _engine, _BAD, _GOOD


class GatedRepair:
    def __init__(self):
        self.entered = threading.Event()
        self.sibling_terminal = threading.Event()
        self.calls = 0

    def implement(self, idea):
        return _BAD

    def repair(self, idea, code, error):
        self.calls += 1
        self.entered.set()
        assert self.sibling_terminal.wait(5), 'independent eval never ran while repair owned its lifecycle'
        return _GOOD


def setup_engine(tmp_path, *, parallel=1, developer=None, codes=None):
    dev = developer if developer is not None else GatedRepair()
    engine = _engine(tmp_path / 'run', dev, inline_repair=True, inline_repair_attempts=1,
                     timeout=60, concurrent_research=False)
    engine._eval_parallel = parallel
    # These are CPU Python subprocesses; no GPU discovery/lease or paid model exists here.
    engine._gpu_ids = []; engine._free_gpus = []; engine._gpu_physical_ids = {}; engine._gpu_host_lease_path = None
    engine.store.append('run_started', {'run_id': 'run', 'run_uid': 'test', 'task_id': 'toy', 'direction': 'min'})
    for nid, code in enumerate(codes or [_BAD, _GOOD]):
        engine.store.append('node_created', {'node_id': nid, 'operator': 'draft', 'parent_ids': [],
            'idea': Idea(operator='draft', footprint={'gpus': 0}).model_dump(mode='json'),
            'eval_start_boundary': True, 'code': code})
    original = engine._eval_write_terminal
    observations = []
    async def write_terminal(a):
        await original(a)
        observations.append((a.node_id, set(engine._eval_repair_yielded), engine._reserved_eval_seconds(), engine._unsettled_eval_seconds()))
        if a.node_id == 1 and isinstance(dev, GatedRepair):
            assert dev.entered.is_set(), 'control must be inside the repair'
            assert (0, 0) in engine._eval_repair_yielded
            dev.sibling_terminal.set()
    engine._eval_write_terminal = write_terminal
    return engine, dev, observations


@pytest.mark.parametrize('ceiling', [None, 100.0])
def test_real_width_one_repairs_while_independent_subprocess_finishes(tmp_path, ceiling):
    engine, dev, observations = setup_engine(tmp_path)
    async def drive():
        with anyio.fail_after(12):
            await engine._dispatch_evals([{'node_id': 0}, {'node_id': 1}], fold(engine.store.read_all()), ceiling, research=False)
    anyio.run(drive)
    events = engine.store.read_all(); state = fold(events)
    assert [nid for nid, *_ in observations] == [1, 0]
    assert dev.calls == 1
    assert all(node.status is NodeStatus.evaluated for node in state.nodes.values())
    claims = [e for e in events if e.type == 'eval_invocation_claimed']
    assert [e.data['node_id'] for e in claims] == [0, 1, 0]
    assert len({e.data['invocation_id'] for e in claims}) == 3
    assert len([e for e in events if e.type == 'node_repaired']) == 1
    assert len([e for e in events if e.type == 'node_created']) == 2
    terminals = [e for e in events if e.type in ('node_evaluated', 'node_failed')]
    assert len(terminals) == 2
    assert state.total_eval_seconds > 0
    assert state.total_eval_seconds == pytest.approx(sum(e.data['eval_seconds'] for e in terminals))
    assert engine._reserved_eval_seconds() == engine._unsettled_eval_seconds() == 0
    assert engine._eval_repair_yielded == set() and engine._eval_gpu_reservations == {}
    assert not any(e.type == 'pause' for e in events)


def test_parked_charge_stays_in_admission_budget_and_finally_releases_once(tmp_path):
    from looplab.engine.eval_dispatch import _eval_time_admission_refused
    from looplab.engine.repair_slots import RepairableEvalSlot, occupied_lifecycles
    engine, _, _ = setup_engine(tmp_path)
    engine._reserve_eval_seconds(0, 0, 60)
    assert engine._yield_eval_lane(0, 0, 7.0)
    assert engine._reserved_eval_seconds() == 0 and engine._unsettled_eval_seconds() == 7
    inflight = {(0, 0)}
    assert occupied_lifecycles(engine, inflight) == 0 and inflight == {(0, 0)}
    state = fold(engine.store.read_all())
    # No future owner: one bounded attempt fits; actual already-paid seven seconds remain spent.
    assert not _eval_time_admission_refused(engine, state, state.nodes[1], 100)
    assert _eval_time_admission_refused(engine, state, state.nodes[1], 7)
    engine._reserve_eval_seconds(1, 0, 60)
    assert _eval_time_admission_refused(engine, state, state.nodes[0], 100)
    assert inflight == {(0, 0)}, 'parked lifecycle must not become dispatchable'
    engine._release_eval_seconds(0, 0); engine._release_eval_seconds(0, 0)
    assert engine._unsettled_eval_seconds() == 0 and engine._reserved_eval_seconds() == 60
    engine._release_eval_seconds(1, 0)
    async def tokens():
        sem = anyio.Semaphore(1); await sem.acquire()
        slot = RepairableEvalSlot(sem); slot.release_for_repair(); slot.close()
        assert sem.value == 1, 'a finalizer cannot give a sibling another token'
        await slot.reacquire(); assert sem.value == 0
        slot.close(); slot.close(); assert sem.value == 1
    anyio.run(tokens)


@pytest.mark.parametrize('ceiling', [None, 100.0])
def test_real_card_admission_keeps_lifecycle_owner_while_repair_yields_width(tmp_path, ceiling):
    from looplab.engine.speculation import CardSession
    engine, dev, observations = setup_engine(tmp_path)
    engine._ensure_speculation_state()
    async def drive():
        with anyio.fail_after(12):
            async with anyio.create_task_group() as tg:
                engine._eval_task_group = tg
                session = CardSession(max_eval_seconds=ceiling, wall_deadline=None,
                    eval_task_group=tg, eval_inflight=engine._eval_inflight, research_allowed=False)
                while fold(engine.store.read_all()).pending_nodes() or engine._eval_inflight:
                    await engine._card_phase_admit_evals(session)
                    await anyio.sleep(0.005)
    anyio.run(drive)
    events = engine.store.read_all(); state = fold(events)
    assert [row[0] for row in observations] == [1, 0] and dev.calls == 1
    assert all(node.status is NodeStatus.evaluated for node in state.nodes.values())
    boundaries = [e for e in events if e.type == 'node_eval_started']
    assert [e.data['node_id'] for e in boundaries] == [0, 1], 'owned repairing0 must not be re-dispatched'
    claims = [e for e in events if e.type == 'eval_invocation_claimed']
    assert [e.data['node_id'] for e in claims] == [0, 1, 0]
    assert engine._eval_inflight == engine._eval_repair_yielded == set()
    assert engine._reserved_eval_seconds() == engine._unsettled_eval_seconds() == 0
    assert state.total_eval_seconds == pytest.approx(sum(e.data['eval_seconds'] for e in events if e.type in ('node_evaluated', 'node_failed')))


def test_budget_spent_by_independent_work_during_repair_refuses_new_attempt_and_keeps_cost(tmp_path):
    engine, dev, observations = setup_engine(tmp_path)
    # A genuine CPU subprocess crosses the small bound while0's LLM repair is parked.
    events = engine.store.read_all()
    original = next(e for e in events if e.type == 'node_created' and e.data['node_id'] == 1)
    replacement = original.model_copy(update={'data': {**original.data, 'code': 'import time; time.sleep(0.8)\n' + _GOOD}})
    engine.store.path.write_text(''.join(e.model_dump_json() + '\n' for e in events if e.seq != original.seq) + replacement.model_dump_json() + '\n')
    async def drive():
        with anyio.fail_after(12):
            await engine._dispatch_evals([{'node_id': 0}, {'node_id': 1}], fold(engine.store.read_all()), 0.5, research=False)
    anyio.run(drive)
    events = engine.store.read_all(); state = fold(events)
    assert dev.calls == 1
    assert state.nodes[1].status is NodeStatus.evaluated
    assert state.nodes[0].status is NodeStatus.failed
    terminal = next(e for e in events if e.type == 'node_failed' and e.data['node_id'] == 0)
    assert terminal.data['reason'] == 'eval_budget_exhausted' and terminal.data['eval_seconds'] > 0
    assert [e.data['node_id'] for e in events if e.type == 'eval_invocation_claimed'] == [0, 1]
    assert state.total_eval_seconds > 0.8 and state.paused
    assert state.total_eval_seconds == pytest.approx(sum(e.data['eval_seconds'] for e in events if e.type in ('node_evaluated', 'node_failed')))
    assert engine._reserved_eval_seconds() == engine._unsettled_eval_seconds() == 0


def test_cancel_waiting_reclaim_releases_its_own_state_and_never_sibling_budget(tmp_path):
    from looplab.engine.evaluate import EvalAttempt
    from looplab.engine.repair_slots import RepairableEvalSlot
    engine, _, _ = setup_engine(tmp_path)
    engine._ensure_speculation_state(); engine._eval_inflight.update({(0, 0), (1, 0)})
    engine._reserve_eval_seconds(0, 0, 60); assert engine._yield_eval_lane(0, 0, 7)
    engine._reserve_eval_seconds(1, 0, 60)
    a = EvalAttempt(node_id=0, generation=0, max_es=100, node=fold(engine.store.read_all()).nodes[0], slot=RepairableEvalSlot())
    a.slot.release_for_repair()
    completed = []
    async def drive():
        async with anyio.create_task_group() as tg:
            async def reclaim():
                try:
                    await engine._reclaim_devices_for_attempt(a, a.slot)
                    completed.append('unexpected launch permission')
                finally:
                    engine._release_eval_seconds(0, 0)
                    engine._eval_inflight.discard((0, 0))
                    a.slot.close()
            tg.start_soon(reclaim)
            await anyio.sleep(0.05)
            assert completed == [] and (0, 0) in engine._eval_repair_yielded
            tg.cancel_scope.cancel()
    anyio.run(drive)
    assert completed == [] and engine._eval_inflight == {(1, 0)}
    assert engine._eval_repair_yielded == set() and engine._unsettled_eval_seconds() == 0
    assert engine._eval_time_reservations == {(1, 0): 60}
    engine._release_eval_seconds(1, 0)


def test_two_real_card_repairs_reacquire_one_physical_slot_without_duplicate_ownership(tmp_path):
    from looplab.engine.speculation import CardSession
    sibling_failed = threading.Event()
    class BothRepair:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()
        def repair(self, idea, code, error):
            with self.lock:
                self.calls += 1
                ordinal = self.calls
            if ordinal == 1:
                assert sibling_failed.wait(5), 'second physical eval never got the yielded slot'
            return 'import time; time.sleep(0.05)\n' + _GOOD
    dev = BothRepair()
    engine, _, _ = setup_engine(tmp_path, developer=dev, codes=[_BAD, _BAD])
    engine._ensure_speculation_state()
    original = engine._run_eval
    active = 0; maximum = 0; lock = threading.Lock()
    def run_eval(*args, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1; maximum = max(maximum, active)
        try:
            result = original(*args, **kwargs)
            if args[0].id == 1 and result.exit_code != 0:
                sibling_failed.set()
            return result
        finally:
            with lock:
                active -= 1
    engine._run_eval = run_eval
    async def drive():
        with anyio.fail_after(12):
            async with anyio.create_task_group() as tg:
                engine._eval_task_group = tg
                session = CardSession(max_eval_seconds=None, wall_deadline=None,
                    eval_task_group=tg, eval_inflight=engine._eval_inflight, research_allowed=False)
                while fold(engine.store.read_all()).pending_nodes() or engine._eval_inflight:
                    await engine._card_phase_admit_evals(session)
                    await anyio.sleep(0.005)
    anyio.run(drive)
    events = engine.store.read_all(); state = fold(events)
    assert dev.calls == 2 and maximum == 1 and active == 0
    assert all(n.status is NodeStatus.evaluated for n in state.nodes.values())
    claims = [e for e in events if e.type == 'eval_invocation_claimed']
    assert len(claims) == 4 and len({e.data['invocation_id'] for e in claims}) == 4
    assert sorted(e.data['node_id'] for e in events if e.type == 'node_eval_started') == [0, 1]
    assert engine._eval_inflight == engine._eval_repair_yielded == set()
    assert engine._reserved_eval_seconds() == engine._unsettled_eval_seconds() == 0
