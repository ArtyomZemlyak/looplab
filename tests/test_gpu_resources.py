"""Layer-4 footprint-aware GPU inventory and admission contracts."""
from __future__ import annotations

import threading
import types

import anyio
import pytest

from looplab.core.models import Idea
from looplab.engine.resources import ResourceSchedulingMixin, detect_gpu_inventory


class _Pool(ResourceSchedulingMixin):
    def __init__(self, ids=(0, 1), mem=None, *, parallel=2, physical=None):
        self._gpu_ids = list(ids)
        self._gpu_physical_ids = physical or {gpu: str(gpu) for gpu in ids}
        self._gpu_mem = dict(mem or {})
        self._free_gpus = list(ids)
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


def test_all_or_nothing_first_fit_and_overdeclaration_clamp():
    pool = _Pool(ids=(0, 1, 2), mem={0: 8_000, 1: 24_000, 2: 16_000})
    assert pool._acquire_gpus(2, 12_000) == [1, 2]  # first two that satisfy memory, atomically
    assert pool._free_gpus == [0]
    assert pool._acquire_gpus(1, 12_000) is None    # populated but temporarily non-fitting
    assert pool._acquire_gpus(0, 99_999) == []      # CPU bypass never waits
    pool._release_gpus([1, 2])
    # 99 GPUs / impossible memory clamps to the three-device, 8-GiB-per-device pool envelope.
    assert pool._acquire_gpus(99, 99_999) == [0, 1, 2]


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
    legacy = one._try_reserve_node_resources(_node(3, None))
    assert legacy["gpu_ids"] == [] and legacy["pin"] is False  # drained pool -> old unpinned branch


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


def test_docker_device_remap_uses_cached_runtime_probe(monkeypatch, tmp_path):
    from looplab.runtime import sandbox
    from looplab.runtime.command_eval import make_docker_wrap

    calls = []
    monkeypatch.setattr(sandbox, "_DOCKER_NVIDIA_RUNTIME_CACHE", None)
    monkeypatch.setattr(sandbox, "_DOCKER_GPU_FALLBACK_WARNED", False)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout='{"nvidia": {}}')

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    wrap = make_docker_wrap(
        str(tmp_path), "img:gpu", env={"CUDA_VISIBLE_DEVICES": "3,7", "X": "y"})
    argv = wrap(["python", "train.py"], str(tmp_path))
    assert argv[argv.index("--gpus") + 1] == "device=3,7"
    assert not any("CUDA_VISIBLE_DEVICES=" in part for part in argv)
    assert "X=y" in argv
    # A second factory reads the process cache rather than probing the daemon again.
    make_docker_wrap(str(tmp_path), "img:gpu", env={"CUDA_VISIBLE_DEVICES": "3"})
    assert len(calls) == 1


def test_docker_gpu_probe_failure_warns_and_falls_back_unpinned(monkeypatch, tmp_path):
    from looplab.runtime import sandbox
    from looplab.runtime.command_eval import make_docker_wrap

    monkeypatch.setattr(sandbox, "_DOCKER_NVIDIA_RUNTIME_CACHE", None)
    monkeypatch.setattr(sandbox, "_DOCKER_GPU_FALLBACK_WARNED", False)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        sandbox.subprocess, "run",
        lambda *_a, **_kw: types.SimpleNamespace(returncode=0, stdout='{"runc": {}}'))
    with pytest.warns(RuntimeWarning, match="without a Docker device pin"):
        wrap = make_docker_wrap(
            str(tmp_path), "img:cpu", env={"CUDA_VISIBLE_DEVICES": "3"})
    argv = wrap(["python", "train.py"], str(tmp_path))
    assert "--gpus" not in argv
    assert "CUDA_VISIBLE_DEVICES=3" in argv          # legacy/unpinned fallback remains explicit


def test_solution_docker_sandbox_uses_same_device_remap(monkeypatch, tmp_path):
    from looplab.runtime import sandbox

    monkeypatch.setattr(sandbox, "_DOCKER_NVIDIA_RUNTIME_CACHE", True)
    monkeypatch.setattr(sandbox, "_DOCKER_GPU_FALLBACK_WARNED", False)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/docker")
    seen = {}

    def fake_run_argv(argv, *_args, **_kwargs):
        seen["argv"] = list(argv)
        return 0, '{"metric": 1.0}', "", False

    monkeypatch.setattr(sandbox, "_run_argv", fake_run_argv)
    result = sandbox.DockerSandbox(image="img:gpu").run(
        "print(1)", str(tmp_path), 30,
        {"CUDA_VISIBLE_DEVICES": "4", "LOOPLAB_EVAL_SEED": "2"})
    argv = seen["argv"]
    assert argv[argv.index("--gpus") + 1] == "device=4"
    assert not any("CUDA_VISIBLE_DEVICES=" in part for part in argv)
    assert "LOOPLAB_EVAL_SEED=2" in argv
    assert result.metric == 1.0
