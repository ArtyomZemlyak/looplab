"""An inline repair gives its GPU back while an LLM works, and takes one back before it re-runs.

Measured 2026-09-24 on a one-GPU MiniOneRec run: node 0's eval failed, and its inline repair -- a
32-minute triage, then Developer sessions, every second of it LLM calls -- held the node's device
reservation for 1h40m with the GPU at 0% and 0 MiB, while node 1 sat built and unevaluated behind it.
The reservation is the lifecycle's, not the devices': the devices go back to the pool for the repair,
and the next attempt waits for the same count like any admission.
"""
from __future__ import annotations

import threading
import types

import anyio

from looplab.engine.evaluate import EvaluateMixin
from looplab.engine.resources import ResourceSchedulingMixin
from tests._source_scan import called_or_offloaded_names


class _Pool(ResourceSchedulingMixin):
    def __init__(self, ids=(0,), parallel=2):
        self._gpu_ids = list(ids)
        self._gpu_physical_ids = {gpu: str(gpu) for gpu in ids}
        self._gpu_mem = {}
        self._free_gpus = list(ids)
        self._gpu_host_lease_path = None
        self._gpu_host_lease_handle = None
        self._gpu_lock = threading.Lock()
        self._gpu_condition = threading.Condition(self._gpu_lock)
        self._gpu_epoch = 0
        self._eval_gpu_reservations = {}
        self._eval_parallel = parallel


def _node(node_id):
    return types.SimpleNamespace(id=node_id, attempt=0,
                                 idea=types.SimpleNamespace(footprint={"gpus": 1}))


# ------------------------------------------------------------ the registry, directly

def test_a_yield_frees_the_device_and_the_reclaim_waits_for_one():
    pool = _Pool()
    res = pool._try_reserve_node_resources(_node(0))
    pool._register_eval_resource_reservation(0, 0, res)
    assert pool._free_gpus == []
    assert pool._yield_eval_devices(0, 0) == [0]
    assert pool._free_gpus == [0], "the repair must not hold the device"
    other = pool._try_reserve_node_resources(_node(1))
    assert other is not None and other["gpu_ids"] == [0], "a sibling can take it meanwhile"

    got: list = []

    async def reclaim_then_release():
        async with anyio.create_task_group() as tg:
            async def reclaim():
                got.append(await pool._reclaim_eval_devices(0, 0))
            tg.start_soon(reclaim)
            await anyio.sleep(0.2)
            assert got == [], "a reclaim never takes a device a sibling holds"
            pool._release_gpus(other["gpu_ids"])
    anyio.run(reclaim_then_release)
    assert got[0]["gpu_ids"] == [0] and "yielded_gpus" not in got[0]
    assert pool._eval_resource_reservation(0, 0)["gpu_ids"] == [0]


def test_the_lane_frees_what_it_holds_now_not_what_it_was_admitted_with():
    pool = _Pool(ids=(0, 1))
    admitted = pool._try_reserve_node_resources(_node(0))
    assert admitted["gpu_ids"] == [0]
    pool._register_eval_resource_reservation(0, 0, admitted)
    pool._yield_eval_devices(0, 0)
    sibling = pool._acquire_gpus(1)                  # takes GPU 0 while node 0 repairs
    assert sibling == [0]

    async def reclaim():
        return await pool._reclaim_eval_devices(0, 0)
    assert anyio.run(reclaim)["gpu_ids"] == [1]
    pool._settle_eval_resource_reservation(0, 0, admitted)
    assert pool._free_gpus == [1], "releasing the ADMISSION ids would free the sibling's GPU 0"
    assert pool._eval_resource_reservation(0, 0) is None


def test_a_lane_that_ends_mid_repair_frees_nothing():
    pool = _Pool()
    admitted = pool._try_reserve_node_resources(_node(0))
    pool._register_eval_resource_reservation(0, 0, admitted)
    pool._yield_eval_devices(0, 0)
    sibling = pool._acquire_gpus(1)
    pool._settle_eval_resource_reservation(0, 0, admitted)
    assert pool._free_gpus == [] and sibling == [0], "the sibling's device stays the sibling's"


def test_a_cpu_or_unregistered_lifecycle_has_nothing_to_yield():
    pool = _Pool()
    assert pool._yield_eval_devices(5, 0) == []
    pool._register_eval_resource_reservation(1, 0, {"gpu_ids": [], "cpu_only": True})
    assert pool._yield_eval_devices(1, 0) == []

    async def reclaim():
        return await pool._reclaim_eval_devices(1, 0)
    assert anyio.run(reclaim) is None


# ------------------------------------------------------------ the driver

def test_the_driver_yields_before_the_repair_and_reclaims_before_every_attempt():
    calls = [c for c in called_or_offloaded_names(EvaluateMixin._evaluate)
             if c.startswith("self._")]
    assert calls.index("self._reclaim_devices_for_attempt") < calls.index("self._eval_run_attempt")
    assert (calls.index("self._eval_salvage") < calls.index("self._yield_devices_for_repair")
            < calls.index("self._eval_decide_repair"))


def test_the_reclaimed_devices_are_the_ones_the_next_attempt_is_pinned_to():
    class _Host(_Pool):
        _yield_devices_for_repair = EvaluateMixin._yield_devices_for_repair
        _reclaim_devices_for_attempt = EvaluateMixin._reclaim_devices_for_attempt

        def _fenced_env(self, env):
            return env

    host = _Host(ids=(0, 1))
    admitted = host._try_reserve_node_resources(_node(0))
    host._register_eval_resource_reservation(0, 0, admitted)
    a = types.SimpleNamespace(node_id=0, generation=0, _resource_reservation=admitted,
                              eval_env={"CUDA_VISIBLE_DEVICES": "0"},
                              sp=types.SimpleNamespace(set=lambda *_: None))
    host._yield_devices_for_repair(a)
    host._acquire_gpus(1)                            # a sibling lands on GPU 0 meanwhile
    anyio.run(host._reclaim_devices_for_attempt, a)
    assert a.eval_env["CUDA_VISIBLE_DEVICES"] == "1"
    assert a._resource_reservation["gpu_ids"] == [1]


# ------------------------------------------------------------ through the real dispatcher

class _DispatchHost(_Pool):
    """Real `_dispatch_evals` parallel branch, real pool, one GPU, width 2. Node 0's stub lifecycle
    runs, fails, repairs (holding the lane) and re-runs; node 1 only runs."""

    _yield_devices_for_repair = EvaluateMixin._yield_devices_for_repair
    _reclaim_devices_for_attempt = EvaluateMixin._reclaim_devices_for_attempt

    def __init__(self, *, yield_in_repair: bool):
        super().__init__(ids=(0,), parallel=2)
        self.store = types.SimpleNamespace(read_all=lambda: [])
        self._concurrent_research_repeat = False
        self.yield_in_repair = yield_in_repair
        self.timeline: list = []
        self.on_gpu: set = set()
        self.overlap = False
        self.node1_ran = anyio.Event()

    def _fenced_env(self, env):
        return env

    def _spawn_research(self, _tg, _state):
        return None

    def _skip_if_aborted(self, _action, _state):
        return False

    async def _attempt(self, node_id):
        if self.on_gpu:
            self.overlap = True
        self.on_gpu.add(node_id)
        self.timeline.append(f"run{node_id}")
        await anyio.sleep(0.05)
        self.on_gpu.discard(node_id)

    async def _evaluate(self, node_id, limiter, _max_es):
        a = types.SimpleNamespace(node_id=node_id, generation=0, eval_env=None,
                                  _resource_reservation=None,
                                  sp=types.SimpleNamespace(set=lambda *_: None))
        async with limiter:
            if node_id == 1:
                await self._attempt(1)
                self.node1_ran.set()
                return
            await self._reclaim_devices_for_attempt(a)
            await self._attempt(0)
            if self.yield_in_repair:
                self._yield_devices_for_repair(a)
            self.timeline.append("repair0")
            with anyio.move_on_after(1.0):           # the LLM repair: ends early once node 1 ran
                await self.node1_ran.wait()
            await self._reclaim_devices_for_attempt(a)
            await self._attempt(0)


def _dispatch(host, monkeypatch):
    nodes = {0: _node(0), 1: _node(1)}
    state = types.SimpleNamespace(total_eval_seconds=0.0, aborted_nodes=set(), nodes=nodes)
    monkeypatch.setattr("looplab.engine.orchestrator.fold", lambda _events: state)
    from looplab.engine.orchestrator import Engine

    async def drive():
        with anyio.fail_after(10):
            await Engine._dispatch_evals(host, [{"node_id": 0}, {"node_id": 1}], state, None)
    anyio.run(drive)


def test_a_sibling_evaluates_on_the_one_gpu_while_a_node_repairs(monkeypatch):
    host = _DispatchHost(yield_in_repair=True)
    _dispatch(host, monkeypatch)
    assert host.timeline == ["run0", "repair0", "run1", "run0"], host.timeline
    assert not host.overlap, "two attempts never shared the device"
    assert host._free_gpus == [0] and host._eval_gpu_reservations == {}


def test_without_the_yield_the_sibling_waits_out_the_whole_repair(monkeypatch):
    # The measured defect, kept as the control: the repair holds the only GPU, so node 1 cannot be
    # admitted until node 0's whole lifecycle -- repair included -- has ended.
    host = _DispatchHost(yield_in_repair=False)
    _dispatch(host, monkeypatch)
    assert host.timeline == ["run0", "repair0", "run0", "run1"], host.timeline
