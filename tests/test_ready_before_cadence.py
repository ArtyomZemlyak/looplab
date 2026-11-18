"""A ready consumer is admitted before a paid cadence without opening a producer lane."""
import threading

import anyio
import pytest

from looplab.events.replay import fold
from tests.test_card_speculation_engine import (
    _admit_unit_speculation_receipt, _engine, _start, _add_ready_draft, _commit_speculative_node,
)


class _CadenceObserved(RuntimeError):
    pass


@pytest.mark.parametrize('enabled',[False,True])
def test_real_outer_loop_admits_committed_pending_node_before_cadence_without_a_build_head(tmp_path,monkeypatch,enabled):
    engine,_=_engine(tmp_path/'run');_start(engine);_add_ready_draft(engine,'card-1')
    nid=_commit_speculative_node(engine)
    state=fold(engine.store.read_all())
    assert engine._head_request(state) is None and not state.buildings
    release=threading.Event();observed=[]
    async def evaluate(node_id,limiter,max_es):
        with anyio.fail_after(5):
            while not release.is_set():await anyio.sleep(.001)
        async with engine._write_lock:
            engine.store.append('node_evaluated',{'node_id':node_id,'generation':0,'metric':.25,'eval_seconds':0})
    def cadence(state):
        current=fold(engine._event_store.read_all())
        observed.append(current.nodes[nid].eval_started)
        release.set()
        raise _CadenceObserved('synthetic paid cadence boundary')
    monkeypatch.setattr(engine,'_evaluate',evaluate)
    monkeypatch.setattr(engine,'_run_cadences',cadence)
    monkeypatch.setattr(engine,'_spawn_research',lambda *_:pytest.fail('admission-only fill started research'))
    monkeypatch.setattr(engine,'_request_card_build',lambda **_:pytest.fail('admission-only fill elected a producer'))
    if not enabled:
        async def old_entry(*_):return False
        monkeypatch.setattr(engine,'_admit_ready_before_cadence',old_entry)
    with pytest.raises(_CadenceObserved):anyio.run(engine.run)
    assert observed==[enabled]
    assert sum(e.type=='node_eval_started' for e in engine.store.read_all())==int(enabled)


@pytest.mark.parametrize('blocked',['pause','time_budget','resources','inflight','wall_deadline','stale','changed_pin','abort','outer_rebuild'])
def test_shared_fill_preserves_admission_refusals_without_starting_paid_work(tmp_path,monkeypatch,blocked):
    engine,_=_engine(tmp_path/'run');_start(engine);_add_ready_draft(engine,'card-1')
    nid=_commit_speculative_node(engine);engine._ensure_speculation_state()
    if blocked=='pause':engine.store.append('pause',{})
    if blocked=='resources':monkeypatch.setattr(engine,'_try_reserve_node_resources',lambda *_a,**_k:None)
    if blocked=='inflight':engine._eval_inflight.add((nid,0))
    if blocked=='stale':monkeypatch.setattr('looplab.engine.speculation.speculative_card_is_fresh',lambda *_a,**_k:False)
    if blocked=='changed_pin':monkeypatch.setattr(engine,'_node_resource_reservation_is_current',lambda *_a,**_k:False)
    if blocked=='abort':engine.store.append('node_abort',{'node_id':nid})
    if blocked=='outer_rebuild':engine.store.append('node_reset',{'node_id':nid,'generation':0,'from_stage':'implement'})
    monkeypatch.setattr(engine,'_spawn_research',lambda *_:pytest.fail('research not allowed'))
    before=engine.store.path.read_bytes()
    async def run():
        async with anyio.create_task_group() as tg:
            engine._eval_task_group=tg
            changed=await engine._admit_ready_before_cadence(0 if blocked=='time_budget' else None,
                                                             0 if blocked=='wall_deadline' else None)
            # Releasing a stale reservation requests a fresh outer scan; it is not an admission.
            assert changed is (blocked=='changed_pin')
    anyio.run(run)
    assert engine.store.path.read_bytes()==before


def test_a_persistent_pin_rescan_still_pays_the_outer_cadence_once(tmp_path,monkeypatch):
    engine,_=_engine(tmp_path/'run');_start(engine);_add_ready_draft(engine,'card-1')
    nid=_commit_speculative_node(engine);checks=[];cadences=[]
    def pin_changed(*_):
        checks.append(True)
        assert len(checks)==1,'a repeated pre-cadence fill starved the owed cadence'
        return False
    def cadence(state):
        cadences.append(True)
        assert not fold(engine._event_store.read_all()).nodes[nid].eval_started
        raise _CadenceObserved('a refused reservation still reaches cadence')
    monkeypatch.setattr(engine,'_node_resource_reservation_is_current',pin_changed)
    monkeypatch.setattr(engine,'_run_cadences',cadence)
    monkeypatch.setattr(engine,'_spawn_research',lambda *_:pytest.fail('no paid research before cadence'))
    with pytest.raises(_CadenceObserved):anyio.run(engine.run)
    assert checks==[True] and cadences==[True]
