"""Layer-4 footprint-aware GPU inventory and admission contracts."""
from __future__ import annotations

import threading
import types

import anyio
import pytest

from looplab.core.models import Idea, NodeStatus
from looplab.engine.resources import (ResourceSchedulingMixin, cuda_visible_device_tokens,
                                      detect_gpu_inventory)
from looplab.runtime.sandbox import GpuPinUnenforceable


class _Pool(ResourceSchedulingMixin):
    def __init__(self, ids=(0, 1), mem=None, *, parallel=2, physical=None, lease_path=None):
        self._gpu_ids = list(ids)
        self._gpu_physical_ids = physical or {gpu: str(gpu) for gpu in ids}
        self._gpu_mem = dict(mem or {})
        self._free_gpus = list(ids)
        self._gpu_host_lease_path = lease_path
        self._gpu_host_lease_handle = None
        self._gpu_lock = threading.Lock()
        self._gpu_condition = threading.Condition(self._gpu_lock)
        self._gpu_epoch = 0
        self._eval_gpu_reservations = {}
        self.max_parallel = parallel
        self._eval_parallel = parallel


def _node(node_id, footprint, *, attempt=0):
    return types.SimpleNamespace(
        id=node_id, attempt=attempt,
        idea=types.SimpleNamespace(footprint=footprint))


def test_inventory_joins_logical_to_physical_visible_devices(monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3,7")
    monkeypatch.setattr(
        "looplab.engine.resources.detect_gpus",
        lambda: [
            {"index": 3, "mem_free_mib": 12_000},
            {"index": 7, "mem_free_mib": 24_000},
        ],
    )
    physical, memory = detect_gpu_inventory([0, 1])
    assert physical == {0: "3", 1: "7"}
    assert memory == {0: 12_000, 1: 24_000}


def test_inventory_degrades_whole_memory_join_to_count_only(monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-a,GPU-b")
    monkeypatch.setattr(
        "looplab.engine.resources.detect_gpus",
        lambda: [{"index": 0, "mem_free_mib": 12_000},
                 {"index": 1, "mem_free_mib": 24_000}],
    )
    physical, memory = detect_gpu_inventory([0, 1])
    assert physical == {0: "GPU-a", 1: "GPU-b"}
    assert memory == {}                              # UUID rows cannot be guessed by numeric index


@pytest.mark.parametrize("selector", [
    "3,3", "03,3", "GPU-a,gpu-A", "0,,1", "void,0",
])
def test_visibility_selector_aliases_and_malformed_lists_fail_closed(monkeypatch, selector):
    from looplab.engine.orchestrator import _detect_gpu_ids

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", selector)
    assert cuda_visible_device_tokens(selector) == []
    assert _detect_gpu_ids() == []
    assert detect_gpu_inventory([0, 1]) == ({}, {})


def test_physical_gpu_mapping_must_be_complete_and_unique():
    duplicate = _Pool(ids=(0, 1), physical={0: "3", 1: "03"})
    with pytest.raises(GpuPinUnenforceable, match="malformed or not unique"):
        duplicate._resource_eval_env({"gpu_ids": [0, 1], "cpu_only": False})

    missing = _Pool(ids=(0, 1), physical={0: "3"})
    with pytest.raises(GpuPinUnenforceable, match="no trustworthy physical selector"):
        missing._resource_eval_env({"gpu_ids": [0, 1], "cpu_only": False})


def test_all_or_nothing_first_fit_and_overdeclaration_clamp():
    pool = _Pool(ids=(0, 1, 2), mem={0: 8_000, 1: 24_000, 2: 16_000})
    assert pool._acquire_gpus(2, 12_000) == [1, 2]  # first two that satisfy memory, atomically
    assert pool._free_gpus == [0]
    assert pool._acquire_gpus(1, 12_000) is None    # populated but temporarily non-fitting
    assert pool._acquire_gpus(0, 99_999) == []      # CPU bypass never waits
    pool._release_gpus([1, 2])
    # 99 GPUs / impossible memory clamps to the three-device, 8-GiB-per-device pool envelope.
    assert pool._acquire_gpus(99, 99_999) == [0, 1, 2]


def test_host_gpu_pool_lease_serializes_engines_and_releases_after_last_device(tmp_path):
    lease = tmp_path / "gpu-pool.lock"
    first = _Pool(ids=(0, 1), lease_path=lease)
    second = _Pool(ids=(0, 1), lease_path=lease)

    assert first._acquire_gpus(1) == [0]
    assert first._acquire_gpus(1) == [1]       # one Engine may still fill its own visible pool
    assert second._acquire_gpus(1) is None     # a separate Engine cannot double-allocate it
    first._release_gpus([0])
    assert second._acquire_gpus(1) is None     # lease lives until the final local reservation ends
    first._release_gpus([1])
    assert second._acquire_gpus(1) == [0]
    second._release_gpus([0])


def test_serial_unspecified_eval_holds_whole_pool_without_changing_visibility(tmp_path):
    lease = tmp_path / "gpu-pool.lock"
    serial = _Pool(ids=(0, 1), parallel=1, lease_path=lease)
    sibling = _Pool(ids=(0, 1), parallel=2, lease_path=lease)

    reservation = serial._try_reserve_node_resources(_node(0, None))
    assert reservation is not None
    assert reservation["count"] == 0 and reservation["pin"] is False
    assert reservation["whole_pool_unpinned"] is True
    assert reservation["gpu_ids"] == [0, 1]   # internal ownership, still an unpinned child env
    assert serial._resource_eval_env(reservation) is None
    assert sibling._acquire_gpus(1) is None
    serial._release_gpus(reservation["gpu_ids"])
    assert sibling._acquire_gpus(1) == [0]
    sibling._release_gpus([0])


def test_all_or_nothing_wait_has_no_lost_wakeup_under_concurrent_releases():
    pool = _Pool(ids=(0, 1), mem={0: 16_000, 1: 16_000})
    assert pool._acquire_gpus(2) == [0, 1]
    go = threading.Event()
    workers = [threading.Thread(target=lambda gpu=gpu: (go.wait(), pool._release_gpus([gpu])))
               for gpu in (0, 1)]
    for worker in workers:
        worker.start()

    async def reserve_after_release():
        go.set()
        reservation = await pool._wait_reserve_node_resources(
            _node(4, {"gpus": 2, "gpu_mem_mib": 8_000}))
        assert reservation["gpu_ids"] == [0, 1]
        pool._release_gpus(reservation["gpu_ids"])

    anyio.run(reserve_after_release)
    for worker in workers:
        worker.join()
    assert pool._free_gpus == [0, 1]


def test_footprint_request_preserves_unspecified_legacy_split_and_cpu_bypass():
    serial = _Pool(ids=(0, 1), parallel=1)
    parallel = _Pool(ids=(0, 1), parallel=2)
    assert serial._resource_request_for_node(_node(0, None))["count"] == 0
    assert serial._resource_request_for_node(_node(0, None))["pin"] is False  # whole-box legacy
    assert parallel._resource_request_for_node(_node(0, None))["count"] == 1
    cpu = parallel._resource_request_for_node(_node(1, {"gpus": 0, "gpu_mem_mib": 99}))
    assert cpu["count"] == 0 and cpu["cpu_only"] is True
    assert parallel._resource_request_for_node(_node(2, {"gpus": 99}))["count"] == 2
    one = _Pool(ids=(0,), parallel=2)
    assert one._acquire_gpus(1) == [0]
    legacy_node = _node(3, None)
    assert one._try_reserve_node_resources(legacy_node) is None  # saturation waits; never oversubscribes
    one._release_gpus([0])
    legacy = one._try_reserve_node_resources(legacy_node)
    assert legacy["gpu_ids"] == [0] and legacy["pin"] is True
    assert one._node_resource_reservation_is_current(
        types.SimpleNamespace(cards={}), legacy_node, legacy)


def test_positive_explicit_gpu_requirement_fails_closed_on_gpu_less_host():
    # A positive declaration must become an immediate fail-closed marker: neither an endless resource
    # wait nor a silent CPU/whole-host launch. Explicit gpus=0 remains the only CPU-only declaration.
    pool = _Pool(ids=(), parallel=2)
    node = _node(0, {"gpus": 2, "gpu_mem_mib": 8_000})

    assert pool._clamp_resource_footprint(node.idea.footprint) == {
        "gpus": 2,
        "gpu_mem_mib": 8_000,
    }
    request = pool._resource_request_for_node(node)
    assert request["count"] == 2 and request["pin"] is False
    assert request["required_unavailable"] is True
    assert request["footprint"]["gpus"] == 2
    reservation = pool._try_reserve_node_resources(node)
    assert reservation is not None and reservation["gpu_ids"] == [] and reservation["pin"] is False
    with pytest.raises(GpuPinUnenforceable, match="no GPUs were detected"):
        pool._resource_eval_env(reservation)

    cpu = pool._try_reserve_node_resources(_node(1, {"gpus": 0}))
    assert (cpu is not None and cpu["gpu_ids"] == [] and cpu["cpu_only"] is True
            and cpu["required_unavailable"] is False)


def test_host_lease_open_failure_fails_closed_at_admission_not_engine_abort(tmp_path):
    """A host GPU-pool lease that cannot even be OPENED (here the path is a directory, so os.open raises
    EISDIR) must fail closed AT THE ADMISSION BOUNDARY — a durable reservation marker that the launch
    boundary re-raises into the caller's node-terminal/retry contract — NOT propagate a raw
    GpuPinUnenforceable out of admission that aborts the run mid task-group with no terminal event."""
    pool = _Pool(ids=(0, 1), parallel=2, lease_path=tmp_path)   # a directory: os.open fails EISDIR
    node = _node(0, {"gpus": 1, "gpu_mem_mib": 8_000})

    # Admission returns a marker instead of raising (pre-fix, this call itself raised).
    reservation = pool._try_reserve_node_resources(node)
    assert reservation is not None
    assert reservation["gpu_ids"] == []
    assert "admission_unpinnable" in reservation
    assert reservation["required_unavailable"] is False        # the pool DOES hold devices

    # The marker still matches the source pin, so the current-check does not release/retry-loop it.
    assert pool._node_resource_reservation_is_current(
        types.SimpleNamespace(cards={}), node, reservation) is True

    # The launch boundary re-raises the exact host-lease cause — never an unpinned full-host env.
    with pytest.raises(GpuPinUnenforceable, match="lease cannot be opened"):
        pool._resource_eval_env(reservation)


def test_host_lease_open_failure_fails_closed_for_unspecified_serial_whole_pool(tmp_path):
    """An unspecified SERIAL run reserves the whole pool + the host lease so it can never bypass the
    lease. If the lease cannot be opened, admission must fail closed — the unpinned whole-pool reservation
    must NOT fall through to `_resource_eval_env`'s unpinned-env branch and launch on the whole box."""
    serial = _Pool(ids=(0, 1), parallel=1, lease_path=tmp_path)  # a directory: os.open fails EISDIR
    reservation = serial._try_reserve_node_resources(_node(0, None))

    assert reservation is not None and reservation["gpu_ids"] == []
    assert "admission_unpinnable" in reservation
    with pytest.raises(GpuPinUnenforceable, match="lease cannot be opened"):
        serial._resource_eval_env(reservation)


def test_eval_reservation_under_other_generation_detects_only_a_stale_generation_key():
    pool = _Pool(ids=(0,), parallel=2)
    pool._register_eval_resource_reservation(5, 0, {"gpu_ids": [0], "pin": True})
    # A reset bumped node 5 to attempt 1: the current-generation lookup misses, but the dispatcher still
    # owns the devices under the OLD key — that is the exact stale-generation signature.
    assert pool._eval_reservation_under_other_generation(5, 1) is True
    assert pool._eval_reservation_under_other_generation(5, 0) is False   # its own generation is a match
    assert pool._eval_reservation_under_other_generation(6, 1) is False   # a never-admitted node


class _Span:
    def set(self, *args, **kwargs):
        return None

    def set_many(self, **kwargs):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Tracer:
    def span(self, *args, **kwargs):
        return _Span()


class _ReachedLaunch(Exception):
    """Raised by the stub `_resource_eval_env` to prove `_evaluate` reached the launch-env build."""


class _EvalResetHost(_Pool):
    """Minimal host for the real `Engine._evaluate` up to the resource-pin guard."""

    def __init__(self, *, parallel):
        super().__init__(ids=(0,), mem={0: 16_000}, parallel=parallel)
        self.tracer = _Tracer()
        self.eval_env_calls = []
        self.appended = []
        self.store = types.SimpleNamespace(
            read_all=lambda: [],
            append=lambda event_type, data, **_kwargs: self.appended.append(
                (event_type, dict(data))),
        )

    def _skip_if_aborted(self, _action, _state):
        return False

    def _resource_eval_env(self, reservation, **_kwargs):
        # Record the exact reservation the launch would use, then stop before any candidate process.
        self.eval_env_calls.append(reservation)
        raise _ReachedLaunch()


def _reset_node(attempt):
    return types.SimpleNamespace(
        id=0, attempt=attempt, operator="improve", status=NodeStatus.pending,
        tombstoned=False, rerun_from=None, rerun_stage=None,
        idea=types.SimpleNamespace(card_id=None, footprint={"gpus": 1}))


def _reset_state(node):
    return types.SimpleNamespace(
        nodes={0: node}, aborted_nodes=set(), paused=False, finished=False,
        stop_requested=False, total_eval_seconds=0.0)


def test_parallel_eval_fails_closed_when_reset_superseded_the_reserved_generation(monkeypatch):
    """A node_reset that lands between the dispatcher's admission (which registered the reservation under
    the OLD generation) and this worker's fresher fold makes the current-generation lookup miss. Under
    parallel dispatch, `_evaluate` must fail closed (return without a terminal) so the dispatcher re-admits
    and re-pins the reset lifecycle — NOT fall through to `_resource_eval_env(None)`'s unpinned full-host
    env that sees every sibling's GPU."""
    from looplab.engine.orchestrator import Engine

    host = _EvalResetHost(parallel=2)
    host._register_eval_resource_reservation(0, 0, {"gpu_ids": [0], "pin": True})  # admission gen 0
    monkeypatch.setattr("looplab.engine.evaluate.fold", lambda _events: _reset_state(_reset_node(1)))

    launched = None
    try:
        anyio.run(Engine._evaluate, host, 0, anyio.CapacityLimiter(1), None)
    except _ReachedLaunch:
        launched = host.eval_env_calls

    assert launched is None, f"reset lifecycle launched an eval with reservation {launched}"
    assert host.appended == []                       # no terminal: the reset lifecycle is re-admitted
    assert (0, 0) in host._eval_gpu_reservations     # stale-gen reservation left for the dispatcher finally


def test_parallel_eval_launches_normally_when_the_reservation_matches_the_generation(monkeypatch):
    """The guard is narrow: a reservation registered under the CURRENT generation is not a stale-key miss,
    so `_evaluate` proceeds to launch with that exact PINNED reservation."""
    from looplab.engine.orchestrator import Engine

    host = _EvalResetHost(parallel=2)
    host._register_eval_resource_reservation(0, 1, {"gpu_ids": [0], "pin": True})  # matches current gen 1
    monkeypatch.setattr("looplab.engine.evaluate.fold", lambda _events: _reset_state(_reset_node(1)))

    with pytest.raises(_ReachedLaunch):
        anyio.run(Engine._evaluate, host, 0, anyio.CapacityLimiter(1), None)
    assert host.eval_env_calls == [{"gpu_ids": [0], "pin": True}]


def test_stale_reservation_fails_closed_even_when_eval_parallel_is_lowered_to_one(monkeypatch):
    """The fail-closed keys on the stale RESERVATION, not the live mutable `_eval_parallel`. A
    Strategist/operator that lowers eval_parallel to 1 mid-batch (while pinned siblings are still
    draining) must not flip a reset lifecycle into an unpinned launch — the stale pinned reservation
    still means the dispatcher must re-admit and re-pin it."""
    from looplab.engine.orchestrator import Engine

    host = _EvalResetHost(parallel=1)                  # live-lowered to serial mid-batch
    host._register_eval_resource_reservation(0, 0, {"gpu_ids": [0], "pin": True})  # admitted while parallel
    monkeypatch.setattr("looplab.engine.evaluate.fold", lambda _events: _reset_state(_reset_node(1)))

    launched = None
    try:
        anyio.run(Engine._evaluate, host, 0, anyio.CapacityLimiter(1), None)
    except _ReachedLaunch:
        launched = host.eval_env_calls

    assert launched is None, f"reset lifecycle launched an eval with reservation {launched}"
    assert host.appended == []
    assert (0, 0) in host._eval_gpu_reservations


def test_never_admitted_node_is_exempt_from_the_stale_generation_fail_closed(monkeypatch):
    """The only exemption is a never-admitted node: a recovery/test call that holds NO reservation under
    any generation is not a reset-superseded admission, so `_evaluate` proceeds to launch (unpinned)."""
    from looplab.engine.orchestrator import Engine

    host = _EvalResetHost(parallel=2)                  # no reservation registered at all
    monkeypatch.setattr("looplab.engine.evaluate.fold", lambda _events: _reset_state(_reset_node(1)))

    with pytest.raises(_ReachedLaunch):
        anyio.run(Engine._evaluate, host, 0, anyio.CapacityLimiter(1), None)
    assert host.eval_env_calls == [None]              # unpinned: the defensive recovery/test seam


def test_clamp_helper_persists_effective_nth_device_memory_envelope():
    pool = _Pool(ids=(0, 1, 2), mem={0: 8_000, 1: 24_000, 2: 16_000})
    assert pool._clamp_resource_footprint(
        {"gpus": 9, "gpu_mem_mib": 99_999, "finalized_by": "forged"}
    ) == {"gpus": 3, "gpu_mem_mib": 8_000}
    count_only = _Pool(ids=(0, 1), mem={})
    assert count_only._clamp_resource_footprint(
        {"gpus": 4, "gpu_mem_mib": 99_999}
    ) == {"gpus": 2, "gpu_mem_mib": 99_999}


def test_developer_finalization_uses_exact_call_output_and_pool_envelope():
    from looplab.engine.orchestrator import Engine

    pool = _Pool(ids=(0, 1), mem={0: 8_000, 1: 16_000})
    inner = types.SimpleNamespace(last_footprint={"gpus": 9, "gpu_mem_mib": 99_999})
    developer = types.SimpleNamespace(
        last_footprint={"gpus": 9, "gpu_mem_mib": 99_999},
        inner=inner,
    )
    idea = Idea(operator="draft", footprint={"gpus": 1, "gpu_mem_mib": 4_000})
    finalized, receipt = Engine._finalize_developer_footprint(
        pool, idea, developer, "print('ok')")
    assert receipt is True
    assert finalized.footprint == {"gpus": 2, "gpu_mem_mib": 8_000}
    assert idea.footprint == {"gpus": 1, "gpu_mem_mib": 4_000}  # immutable proposal copy

    Engine._reset_developer_footprint(developer)
    assert developer.last_footprint is None and inner.last_footprint is None

    unspecified, receipt = Engine._finalize_developer_footprint(
        pool, Idea(operator="draft"), developer, "print('legacy')")
    assert receipt is False and unspecified.footprint is None


class _DispatchHost(_Pool):
    def __init__(self):
        super().__init__(ids=(0,), mem={0: 16_000}, parallel=2)
        self.store = types.SimpleNamespace(read_all=lambda: [])
        self._concurrent_research_repeat = False
        self.ran = []

    def _spawn_research(self, _tg, _state):
        return None

    def _skip_if_aborted(self, _action, _state):
        return False

    async def _evaluate(self, node_id, limiter, _max_es):
        async with limiter:
            self.ran.append(node_id)
            if node_id == 1:
                # Simulate the external owner of GPU 0 completing after the CPU candidate passed the
                # blocked GPU head.  The condition wake then admits node 0.
                self._release_gpus([0])
            await anyio.sleep(0)


class _DropWaitHost(_Pool):
    def __init__(self, parallel):
        super().__init__(ids=(0,), mem={0: 16_000}, parallel=parallel)
        self._concurrent_research_repeat = False
        self.ran = []
        self.terminals = []
        self.store = types.SimpleNamespace(
            read_all=lambda: [],
            append=lambda event_type, data, **_kwargs: self.terminals.append(
                (event_type, dict(data))),
        )

    def _spawn_research(self, _tg, _state):
        return None

    def _skip_if_aborted(self, action, state):
        from looplab.engine.orchestrator import Engine
        return Engine._skip_if_aborted(self, action, state)

    async def _evaluate(self, node_id, _limiter, _max_es):
        self.ran.append(node_id)


@pytest.mark.parametrize("parallel", [1, 2])
def test_dispatch_closes_operator_dropped_card_while_waiting_for_gpu(
        monkeypatch, parallel):
    host = _DropWaitHost(parallel)
    assert host._acquire_gpus(1) == [0]  # external owner never releases during this admission
    card = types.SimpleNamespace(
        status="proposed", dropped_by=None, aliases=[], resource_pin=None)
    node = types.SimpleNamespace(
        id=0,
        attempt=0,
        status=NodeStatus.pending,
        tombstoned=False,
        idea=types.SimpleNamespace(card_id="card-0", footprint={"gpus": 1}),
    )
    state = types.SimpleNamespace(
        total_eval_seconds=0.0,
        aborted_nodes=set(),
        nodes={0: node},
        cards={"card-0": card},
        paused=False,
        finished=False,
        stop_requested=False,
    )
    monkeypatch.setattr("looplab.engine.orchestrator.fold", lambda _events: state)
    from looplab.engine.orchestrator import Engine

    wait_entered = threading.Event()
    original_wait = host._wait_for_gpu_change

    def observed_wait(epoch):
        wait_entered.set()
        original_wait(epoch)

    host._wait_for_gpu_change = observed_wait

    async def scenario():
        with anyio.fail_after(3):
            async with anyio.create_task_group() as tg:
                tg.start_soon(
                    Engine._dispatch_evals, host, [{"node_id": 0}], state, None)
                assert await anyio.to_thread.run_sync(wait_entered.wait, 1.0)
                card.status = "dropped"
                card.dropped_by = "operator"

    anyio.run(scenario)

    assert host.ran == []
    assert host.terminals == [("node_failed", {
        "node_id": 0,
        "generation": 0,
        "error": "Card dropped by operator",
        "reason": "card_dropped",
        "eval_seconds": 0.0,
    })]
    assert host._free_gpus == []  # no reservation was acquired or spuriously released


def test_dispatch_cpu_candidate_passes_a_stalled_gpu_head(monkeypatch):
    host = _DispatchHost()
    assert host._acquire_gpus(1) == [0]              # external/in-flight owner stalls the head
    nodes = {
        0: _node(0, {"gpus": 1, "gpu_mem_mib": 8_000}),
        1: _node(1, {"gpus": 0}),
    }
    state = types.SimpleNamespace(
        total_eval_seconds=0.0, aborted_nodes=set(), nodes=nodes)
    monkeypatch.setattr("looplab.engine.orchestrator.fold", lambda _events: state)
    from looplab.engine.orchestrator import Engine

    anyio.run(Engine._dispatch_evals, host,
              [{"node_id": 0}, {"node_id": 1}], state, None)
    assert host.ran == [1, 0]
    assert host._free_gpus == [0]


def test_parallel_dispatch_releases_reservation_when_pause_lands_during_resource_wait(
    monkeypatch,
):
    """A fresh run-level gate wins after a blocked parallel admission wakes."""

    host = _DispatchHost()
    assert host._acquire_gpus(1) == [0]
    node = types.SimpleNamespace(
        id=0,
        attempt=0,
        status=NodeStatus.pending,
        tombstoned=False,
        idea=types.SimpleNamespace(card_id=None, footprint={"gpus": 1}),
    )
    state = types.SimpleNamespace(
        total_eval_seconds=0.0,
        aborted_nodes=set(),
        nodes={0: node},
        cards={},
        paused=False,
        finished=False,
        stop_requested=False,
    )
    monkeypatch.setattr("looplab.engine.orchestrator.fold", lambda _events: state)
    from looplab.engine.orchestrator import Engine

    wait_entered = threading.Event()
    original_wait = host._wait_for_gpu_change

    def observed_wait(epoch):
        wait_entered.set()
        original_wait(epoch)

    host._wait_for_gpu_change = observed_wait

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                Engine._dispatch_evals,
                host,
                [{"node_id": 0}],
                state,
                None,
            )
            with anyio.fail_after(2):
                assert await anyio.to_thread.run_sync(wait_entered.wait, 1.0)
            state.paused = True
            host._release_gpus([0])

    anyio.run(scenario)

    assert host.ran == []
    assert host._free_gpus == [0]


class _PinRaceDispatchHost(_Pool):
    def __init__(self):
        super().__init__(ids=(0,), mem={0: 16_000}, parallel=1)
        self.store = types.SimpleNamespace(read_all=lambda: [])
        self._concurrent_research_repeat = False
        self.releases: list[list[int]] = []
        self.started: list[tuple[int, dict | None, list[list[int]]]] = []

    def _spawn_research(self, _tg, _state):
        return None

    def _skip_if_aborted(self, _action, _state):
        return False

    def _release_gpus(self, gpu_ids):
        self.releases.append(list(gpu_ids or []))
        super()._release_gpus(gpu_ids)

    async def _evaluate(self, node_id, _limiter, _max_es):
        reservation = self._eval_resource_reservation(node_id, 0)
        self.started.append((node_id, reservation, list(self.releases)))


def test_serial_dispatch_releases_stale_pin_reservation_before_eval(monkeypatch):
    """A Card re-pin while the serial waiter sleeps cannot leak its old reservation into eval."""
    host = _PinRaceDispatchHost()
    card = types.SimpleNamespace(
        resource_pin={"gpus": 1, "gpu_mem_mib": 8_000, "pinned_by": "operator"})
    node = types.SimpleNamespace(
        id=0,
        attempt=0,
        status=NodeStatus.pending,
        tombstoned=False,
        idea=types.SimpleNamespace(
            card_id="card-0", footprint={"gpus": 1, "gpu_mem_mib": 8_000}),
    )
    state = types.SimpleNamespace(
        total_eval_seconds=0.0,
        aborted_nodes=set(),
        nodes={0: node},
        cards={"card-0": card},
    )
    waits: list[dict] = []
    monkeypatch.setattr("looplab.engine.orchestrator.fold", lambda _events: state)
    from looplab.engine.orchestrator import Engine

    async def scenario():
        wait_entered = anyio.Event()
        pin_changed = anyio.Event()

        async def reserve(nd, *, resource_pin=None, wait_once=False):
            waits.append(dict(resource_pin or {}))
            request = host._resource_request_for_node(nd, resource_pin=resource_pin)
            if len(waits) == 1:
                wait_entered.set()
                await pin_changed.wait()
                return {**request, "gpu_ids": [0]}
            return {**request, "gpu_ids": []}

        host._wait_reserve_node_resources = reserve
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                Engine._dispatch_evals,
                host,
                [{"node_id": 0}],
                state,
                None,
            )
            with anyio.fail_after(2):
                await wait_entered.wait()
            card.resource_pin = {"gpus": 0, "pinned_by": "operator"}
            pin_changed.set()

    anyio.run(scenario)

    assert [pin["gpus"] for pin in waits] == [1, 0]
    assert host.releases == [[0], []]
    assert len(host.started) == 1
    node_id, admitted, releases_at_start = host.started[0]
    assert node_id == 0 and releases_at_start == [[0]]
    assert admitted is not None
    assert admitted["count"] == 0 and admitted["cpu_only"] is True
    assert admitted["gpu_ids"] == []


def test_serial_dispatch_refolds_pin_after_bounded_wait_without_gpu_release(monkeypatch):
    """A GPU->CPU re-pin progresses even though the old busy GPU never changes pool epoch."""
    host = _PinRaceDispatchHost()
    assert host._acquire_gpus(1) == [0]              # external owner stays busy for the whole test
    card = types.SimpleNamespace(
        resource_pin={"gpus": 1, "gpu_mem_mib": 8_000, "pinned_by": "operator"})
    node = types.SimpleNamespace(
        id=0,
        attempt=0,
        status=NodeStatus.pending,
        tombstoned=False,
        idea=types.SimpleNamespace(
            card_id="card-0", footprint={"gpus": 1, "gpu_mem_mib": 8_000}),
    )
    state = types.SimpleNamespace(
        total_eval_seconds=0.0,
        aborted_nodes=set(),
        nodes={0: node},
        cards={"card-0": card},
    )
    monkeypatch.setattr("looplab.engine.orchestrator.fold", lambda _events: state)
    from looplab.engine.orchestrator import Engine

    wait_entered = threading.Event()
    original_wait = host._wait_for_gpu_change

    def observed_wait(epoch):
        wait_entered.set()
        original_wait(epoch)

    host._wait_for_gpu_change = observed_wait

    async def scenario():
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                Engine._dispatch_evals,
                host,
                [{"node_id": 0}],
                state,
                None,
            )
            with anyio.fail_after(2):
                assert await anyio.to_thread.run_sync(wait_entered.wait, 1.0)
            card.resource_pin = {"gpus": 0, "pinned_by": "operator"}

    anyio.run(scenario)

    assert host._free_gpus == []                    # no GPU release was needed to make progress
    assert len(host.started) == 1
    _, admitted, releases_at_start = host.started[0]
    assert admitted is not None
    assert admitted["count"] == 0 and admitted["gpu_ids"] == []
    assert releases_at_start == []


def test_docker_device_remap_uses_cached_runtime_probe(monkeypatch, tmp_path):
    from looplab.runtime import sandbox
    from looplab.runtime.command_eval import make_docker_wrap

    calls = []
    monkeypatch.setattr(sandbox, "_DOCKER_NVIDIA_RUNTIME_CACHE", None)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout='{"nvidia": {}}')

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    wrap = make_docker_wrap(
        str(tmp_path), "img:gpu",
        env={"CUDA_VISIBLE_DEVICES": "3,7", "NVIDIA_VISIBLE_DEVICES": "all", "X": "y"})
    argv = wrap(["python", "train.py"], str(tmp_path))
    assert argv[argv.index("--gpus") + 1] == "device=3,7"
    assert not any("CUDA_VISIBLE_DEVICES=" in part for part in argv)
    assert not any("NVIDIA_VISIBLE_DEVICES=" in part for part in argv)
    assert "X=y" in argv
    # A second factory reads the process cache rather than probing the daemon again.
    make_docker_wrap(str(tmp_path), "img:gpu", env={"CUDA_VISIBLE_DEVICES": "3"})
    assert len(calls) == 1


def test_docker_gpu_probe_failure_rejects_scheduler_owned_pin(monkeypatch, tmp_path):
    from looplab.runtime import sandbox
    from looplab.runtime.command_eval import make_docker_wrap

    monkeypatch.setattr(sandbox, "_DOCKER_NVIDIA_RUNTIME_CACHE", None)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        sandbox.subprocess, "run",
        lambda *_a, **_kw: types.SimpleNamespace(returncode=0, stdout='{"runc": {}}'))
    pool = _Pool(ids=(0,), physical={0: "3"})
    reservation = pool._try_reserve_node_resources(_node(0, {"gpus": 1}))
    env = pool._resource_eval_env(reservation)

    with pytest.raises(RuntimeError, match="refusing to launch an unpinned container"):
        make_docker_wrap(str(tmp_path), "img:gpu", env=env)


def test_docker_gpu_runsc_runtime_uses_scheduler_owned_pin(monkeypatch, tmp_path):
    from looplab.runtime import sandbox

    monkeypatch.setattr(sandbox, "_DOCKER_NVIDIA_RUNTIME_CACHE", True)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    seen = {}

    def fake_run_argv(argv, *_args, **_kwargs):
        seen["argv"] = list(argv)
        return 0, '{"metric": 1.0}', "", False

    monkeypatch.setattr(sandbox, "_run_argv", fake_run_argv)
    pool = _Pool(ids=(0,), physical={0: "4"})
    reservation = pool._try_reserve_node_resources(_node(0, {"gpus": 1}))
    env = pool._resource_eval_env(
        reservation, base={"NVIDIA_VISIBLE_DEVICES": "all"})

    result = sandbox.DockerSandbox(image="img:gpu", runtime="runsc").run(
        "print(1)", str(tmp_path), 30, env)
    argv = seen["argv"]
    assert argv[argv.index("--runtime") + 1] == "runsc"
    assert argv[argv.index("--gpus") + 1] == "device=4"
    assert not any("CUDA_VISIBLE_DEVICES=" in part for part in argv)
    assert not any("NVIDIA_VISIBLE_DEVICES=" in part for part in argv)
    assert result.metric == 1.0


def test_docker_gpu_incompatible_runtime_rejects_scheduler_owned_pin(monkeypatch, tmp_path):
    from looplab.runtime import sandbox

    monkeypatch.setattr(sandbox, "_DOCKER_NVIDIA_RUNTIME_CACHE", True)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    pool = _Pool(ids=(0,), physical={0: "4"})
    reservation = pool._try_reserve_node_resources(_node(0, {"gpus": 1}))
    env = pool._resource_eval_env(reservation)

    with pytest.raises(RuntimeError, match="OCI runtime 'custom-no-gpu'.*refusing"):
        sandbox.DockerSandbox(image="img:gpu", runtime="custom-no-gpu").run(
            "print(1)", str(tmp_path), 30, env)


def test_docker_unspecified_and_cpu_legacy_requests_need_no_gpu_runtime(monkeypatch, tmp_path):
    from looplab.runtime import sandbox
    from looplab.runtime.command_eval import make_docker_wrap

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        sandbox, "docker_nvidia_runtime_available",
        lambda: pytest.fail("CPU/unspecified paths must not probe the NVIDIA runtime"))

    unspecified = make_docker_wrap(str(tmp_path), "img:cpu")
    assert "--gpus" not in unspecified(["python", "train.py"], str(tmp_path))

    pool = _Pool(ids=(0,))
    cpu_reservation = pool._try_reserve_node_resources(_node(0, {"gpus": 0}))
    cpu_env = pool._resource_eval_env(
        cpu_reservation, base={"NVIDIA_VISIBLE_DEVICES": "all"})
    cpu = make_docker_wrap(str(tmp_path), "img:cpu", env=cpu_env)
    cpu_argv = cpu(["python", "train.py"], str(tmp_path))
    assert "--gpus" not in cpu_argv
    assert "CUDA_VISIBLE_DEVICES=" in cpu_argv
    assert "NVIDIA_VISIBLE_DEVICES=void" in cpu_argv
    assert "NVIDIA_VISIBLE_DEVICES=all" not in cpu_argv

    seen = {}

    def fake_run_argv(argv, *_args, **_kwargs):
        seen["argv"] = list(argv)
        return 0, '{"metric": 1.0}', "", False

    monkeypatch.setattr(sandbox, "_run_argv", fake_run_argv)
    result = sandbox.DockerSandbox(image="img:cpu", runtime="nvidia").run(
        "print(1)", str(tmp_path), 30, cpu_env)
    sandbox_argv = seen["argv"]
    assert "--gpus" not in sandbox_argv
    assert "CUDA_VISIBLE_DEVICES=" in sandbox_argv
    assert "NVIDIA_VISIBLE_DEVICES=void" in sandbox_argv
    assert "NVIDIA_VISIBLE_DEVICES=all" not in sandbox_argv
    assert result.metric == 1.0


def test_resource_eval_env_inherit_host_strips_secret_named_vars(monkeypatch):
    """SECURITY: _resource_eval_env(inherit_host=True) is the TRUSTED explicit-env channel that both
    sandbox tiers forward verbatim — run_argv overlays it on top of its own secret-filtered base
    (re-adding secrets), and the Docker tier forwards each key via -e. So the inherited host names must
    be SECRET_ENV-filtered here, or a pinned/CPU reservation would hand LLM_API_KEY/creds to candidate
    code. The engine's own `base` (LOOPLAB_EVAL_SEED) and CUDA_VISIBLE_DEVICES still pass."""
    monkeypatch.setenv("LLM_API_KEY", "sk-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "creds")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("HARMLESS_HOST_VAR", "keepme")
    pool = _Pool(ids=(0,), physical={0: "5"})
    reservation = pool._try_reserve_node_resources(_node(0, {"gpus": 1}))
    env = pool._resource_eval_env(
        reservation, base={"LOOPLAB_EVAL_SEED": "7"}, inherit_host=True)
    assert "LLM_API_KEY" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env.get("HARMLESS_HOST_VAR") == "keepme"        # non-secret host vars are inherited
    assert env.get("LOOPLAB_EVAL_SEED") == "7"             # engine's explicit base is kept
    assert env.get("CUDA_VISIBLE_DEVICES") == "5"          # physical pin still applied
    # A CPU-only reservation with inherit_host also strips secrets and fences the device to "".
    cpu_pool = _Pool(ids=(0,))
    cpu = cpu_pool._resource_eval_env(
        cpu_pool._try_reserve_node_resources(_node(0, {"gpus": 0})), inherit_host=True)
    assert "LLM_API_KEY" not in cpu and cpu.get("CUDA_VISIBLE_DEVICES") == ""


def test_solution_docker_sandbox_uses_same_device_remap(monkeypatch, tmp_path):
    from looplab.runtime import sandbox

    monkeypatch.setattr(sandbox, "_DOCKER_NVIDIA_RUNTIME_CACHE", True)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    seen = {}

    def fake_run_argv(argv, *_args, **_kwargs):
        seen["argv"] = list(argv)
        return 0, '{"metric": 1.0}', "", False

    monkeypatch.setattr(sandbox, "_run_argv", fake_run_argv)
    result = sandbox.DockerSandbox(image="img:gpu").run(
        "print(1)", str(tmp_path), 30,
        {"CUDA_VISIBLE_DEVICES": "4", "NVIDIA_VISIBLE_DEVICES": "all",
         "LOOPLAB_EVAL_SEED": "2"})
    argv = seen["argv"]
    assert argv[argv.index("--gpus") + 1] == "device=4"
    assert not any("CUDA_VISIBLE_DEVICES=" in part for part in argv)
    assert not any("NVIDIA_VISIBLE_DEVICES=" in part for part in argv)
    assert "LOOPLAB_EVAL_SEED=2" in argv
    assert result.metric == 1.0
