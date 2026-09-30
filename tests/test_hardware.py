"""Honest runtime-capability brief + task-aware gating (no torch claim for offline tasks)."""
from __future__ import annotations

import os

import pytest

import looplab.core.hardware as hw
from looplab.core.hardware import runtime_capabilities_brief, task_runtime_caps


@pytest.fixture(autouse=True)
def _fresh_gpu_probe_state(monkeypatch):
    """Every test here starts from an unprobed process. The inventory cache and the failed-probe
    window are module state any Engine built earlier in the session may have set — a real probe
    that failed on a box whose `nvidia-smi` errors put the next test inside its 60 s window, where
    `detect_gpus()` answered `[]` without calling the probe a test had patched in (critic
    2026-09-30: `test_detect_gpus_handles_comma_in_gpu_name` raised IndexError after one)."""
    monkeypatch.setattr(hw, "_GPUS_CACHE", None)
    monkeypatch.setattr(hw, "_GPUS_FAILED_AT", None)


def test_caps_off_is_conservative():
    out = runtime_capabilities_brief(auto_install=False, gpu="RTX 5090")
    assert "scikit-learn" in out and "CPU only, no GPU/network" in out
    assert "torch" not in out            # locked stack: never advertise deep-learning frameworks


def test_caps_on_advertises_frameworks_and_gpu():
    out = runtime_capabilities_brief(auto_install=True, gpu="RTX 5090")
    assert "torch" in out and "xgboost" in out
    assert "RTX 5090" in out
    assert "auto-installed" in out
    assert "downgrading it to sklearn" in out   # the exact anti-pattern the bug exhibited


def test_caps_on_no_gpu_says_cpu():
    out = runtime_capabilities_brief(auto_install=True, gpu=None)
    assert "torch" in out and "no GPU detected" in out


class _CapableTask:
    def llm_roles(self, client, parser="tool_call", runtime_caps=None):
        return None, None


class _LockedTask:                      # offline/synthetic: llm_roles has no runtime_caps kwarg
    def llm_roles(self, client, parser="tool_call"):
        return None, None


def test_task_caps_gated_on_opt_in():
    # A task that accepts runtime_caps gets the sentence; one that doesn't is left locked (None),
    # so a synthetic numpy+stdlib task is never told torch is available even with the flag on.
    assert task_runtime_caps(_CapableTask(), auto_install=True, gpu="X") is not None
    assert task_runtime_caps(_LockedTask(), auto_install=True, gpu="X") is None


def test_task_caps_reflects_auto_install():
    capable = _CapableTask()
    assert "torch" in task_runtime_caps(capable, auto_install=True, gpu=None)
    assert "torch" not in task_runtime_caps(capable, auto_install=False, gpu=None)


def test_detect_gpus_handles_comma_in_gpu_name(monkeypatch):
    """Architecture review: a GPU name containing a comma shifts the CSV columns; detect_gpus must
    parse index from the head and the memory numbers from the tail (rejoining the name), matching the
    sibling detect_gpu — not read fixed positions that land on a name fragment."""
    import looplab.core.hardware as hw
    monkeypatch.setattr(hw, "_GPUS_CACHE", None)
    # nvidia-smi row for a comma-bearing name: index, "NVIDIA A100, SXM4", mem.total, mem.free
    monkeypatch.setattr(hw, "query_nvidia_smi",
                        lambda *a, **k: [["0", "NVIDIA A100", "SXM4", "40960", "40000"]])
    g = hw.detect_gpus()[0]
    assert g["index"] == 0
    assert g["name"] == "NVIDIA A100,SXM4"
    assert g["mem_total_mib"] == 40960 and g["mem_free_mib"] == 40000


_UUID_A = bytes.fromhex("00112233445566778899aabbccddeeff")
_UUID_B = bytes.fromhex("ffeeddccbbaa99887766554433221100")
_UUID_A_TEXT = "GPU-00112233-4455-6677-8899-aabbccddeeff"
_UUID_B_TEXT = "GPU-ffeeddcc-bbaa-9988-7766-554433221100"


class _FakeCudaApi:
    """Python-valued seam matching hardware._CtypesCudaDriver; never loads CUDA."""

    def __init__(self, rows, *, cuda_driver_version=12080, initialize_error=None):
        self.rows = rows
        self.cuda_driver_version = cuda_driver_version
        self.initialize_error = initialize_error
        self.requested_ordinals = []

    def initialize(self):
        if self.initialize_error is not None:
            raise self.initialize_error

    def driver_version(self):
        return self.cuda_driver_version

    def device_count(self):
        return len(self.rows)

    def device(self, ordinal):
        self.requested_ordinals.append(ordinal)
        return ordinal + 100

    def _row(self, device):
        return self.rows[device - 100]

    def device_name(self, device):
        return self._row(device)["name"]

    def device_total_memory(self, device):
        return self._row(device)["bytes"]

    def device_uuid(self, device):
        return self._row(device)["uuid"]

    def device_pci_bus_id(self, device):
        return self._row(device).get("pci_bus_id")


def _cuda_rows():
    return [
        {"name": "GPU B", "bytes": 48 * 1024**3, "uuid": _UUID_B,
         "pci_bus_id": "00000000:65:00.0"},
        {"name": "GPU A", "bytes": 24 * 1024**3, "uuid": _UUID_A,
         "pci_bus_id": "00000000:17:00.0"},
    ]


def _display_versions():
    return {_UUID_A_TEXT.lower(): "572.83", _UUID_B_TEXT.lower(): "572.83"}


def test_effective_gpu_inventory_uses_cuda_logical_order_and_exact_schema(monkeypatch):
    import looplab.core.hardware as hw

    api = _FakeCudaApi(_cuda_rows())
    # A numeric CVD is deliberately NOT interpreted as an nvidia-smi physical index.  The fake API
    # already represents the post-CVD logical order, just as cuDeviceGetCount/cuDeviceGet do.
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "9,2")
    monkeypatch.setattr(
        hw, "detect_gpus", lambda: (_ for _ in ()).throw(AssertionError("legacy path used")))

    effective = hw.effective_gpu_inventory(
        _cuda_api=api, _driver_version_query=_display_versions)

    assert effective == [
        {
            "index": 0,
            "uuid": _UUID_B_TEXT,
            "pci_bus_id": "00000000:65:00.0",
            "name": "GPU B",
            "mem_total_mib": 49_152,
            "driver_version": "572.83",
            "cuda_driver_version": 12080,
        },
        {
            "index": 1,
            "uuid": _UUID_A_TEXT,
            "pci_bus_id": "00000000:17:00.0",
            "name": "GPU A",
            "mem_total_mib": 24_576,
            "driver_version": "572.83",
            "cuda_driver_version": 12080,
        },
    ]
    assert api.requested_ordinals == [0, 1]
    assert all("mem_free_mib" not in row for row in effective)


def test_effective_gpu_inventory_fails_closed_without_pci_identity():
    import looplab.core.hardware as hw

    row = _cuda_rows()[0]
    row.pop("pci_bus_id")
    assert hw.effective_gpu_inventory(
        _cuda_api=_FakeCudaApi([row]), _driver_version_query=_display_versions) == []


def test_effective_gpu_inventory_zero_visible_devices_does_not_need_smi_join():
    import looplab.core.hardware as hw

    called = []
    assert hw.effective_gpu_inventory(
        _cuda_api=_FakeCudaApi([]),
        _driver_version_query=lambda: called.append(True),
    ) == []
    assert called == []


def test_effective_gpu_inventory_fails_closed_without_cuda_identity_or_uuid_join():
    import looplab.core.hardware as hw

    good = _cuda_rows()[0]
    cases = [
        ({**good, "uuid": bytes(16)}, _display_versions),
        (good, lambda: {}),
        (good, lambda: {_UUID_A_TEXT.lower(): "572.83"}),
    ]
    for row, versions in cases:
        assert hw.effective_gpu_inventory(
            _cuda_api=_FakeCudaApi([row]), _driver_version_query=versions) == []


def test_effective_gpu_inventory_fails_closed_on_cuda_or_duplicate_identity_errors():
    import looplab.core.hardware as hw

    assert hw.effective_gpu_inventory(
        _cuda_api=_FakeCudaApi([], initialize_error=OSError("no driver")),
        _driver_version_query=_display_versions,
    ) == []
    duplicate = [_cuda_rows()[0], {**_cuda_rows()[1], "uuid": _UUID_B}]
    assert hw.effective_gpu_inventory(
        _cuda_api=_FakeCudaApi(duplicate), _driver_version_query=_display_versions) == []


# --- the CPU budget: the affinity mask bounded by the cgroup CFS quota (doc 69 §8) -------------------
#
# `usable_cpu_count` sizes every eval's BLAS/OpenMP pools (`runtime/sandbox.py::run_argv`) and is the
# "usable CPU cores" the agents are told. It answered the affinity mask alone — 192 on the box doc 69
# measured, whose `cpu.max` allows 80 CPUs — so the quota is read from a cgroup tree the tests below
# fake on disk, one file layout per case, and nothing here depends on the box they run on.


def _tree(base, proc_lines, files):
    """A fake cgroup mount + `/proc/self/cgroup` under `base`, as the readers' keyword arguments.
    `files` maps a path under the mount to its content; `proc_lines=None` leaves no proc file."""
    root = base / "sys-fs-cgroup"
    root.mkdir(parents=True)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("ascii"))
    proc = base / "proc-self-cgroup"
    if proc_lines is not None:
        proc.write_text("".join(f"{line}\n" for line in proc_lines), encoding="ascii")
    return {"cgroup_root": str(root), "proc_cgroup": str(proc)}


def _affinity() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def test_a_v2_quota_bounds_the_budget_rounded_up(tmp_path):
    measured = _tree(tmp_path / "doc69", ["0::/"], {"cpu.max": "8000000 100000\n"})
    assert hw.cgroup_cpu_limit(**measured) == 80          # the box doc 69 §8 measured
    assert hw.usable_cpu_count(**measured) == min(_affinity(), 80)
    # A fractional quota is rounded UP: 1.5 CPUs of bandwidth keep two threads busy, and a quota
    # below one CPU still leaves one thread, never zero.
    assert hw.cgroup_cpu_limit(**_tree(tmp_path / "half", ["0::/"], {"cpu.max": "150000 100000"})) == 2
    small = _tree(tmp_path / "tiny", ["0::/"], {"cpu.max": "50000 100000"})
    assert hw.cgroup_cpu_limit(**small) == 1
    assert hw.usable_cpu_count(**small) == 1


def test_a_v2_max_is_no_quota_and_the_budget_is_the_affinity(tmp_path):
    unlimited = _tree(tmp_path, ["0::/"], {"cpu.max": "max 100000\n"})
    assert hw.cgroup_cpu_limit(**unlimited) is None
    assert hw.usable_cpu_count(**unlimited) == _affinity()


def test_a_v2_process_is_bounded_by_every_ancestor_of_its_own_cgroup(tmp_path):
    # Not namespaced: /proc/self/cgroup names the full path, and a pod-level limit sits on the pod's
    # cgroup while the container's own leaf says `max`. The tightest level on the path is the bound.
    pod = _tree(tmp_path / "pod", ["0::/kubepods/pod1/ctr"], {
        "kubepods/pod1/ctr/cpu.max": "max 100000", "kubepods/pod1/cpu.max": "400000 100000"})
    assert hw.cgroup_cpu_limit(**pod) == 4
    leaf = _tree(tmp_path / "leaf", ["0::/kubepods/pod1/ctr"], {
        "kubepods/pod1/ctr/cpu.max": "200000 100000", "kubepods/pod1/cpu.max": "400000 100000"})
    assert hw.cgroup_cpu_limit(**leaf) == 2
    # A path this mount does not hold (the process's cgroup is outside what is mounted here) falls
    # through to the mount root, which is where a namespaced container's own quota lives.
    moved = _tree(tmp_path / "moved", ["0::/elsewhere/deep"], {"cpu.max": "300000 100000"})
    assert hw.cgroup_cpu_limit(**moved) == 3


def test_a_path_that_climbs_out_of_the_mount_reads_only_the_mount_root(tmp_path):
    escape = _tree(tmp_path, ["0::/../outside"], {"cpu.max": "max 100000"})
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "cpu.max").write_text("100000 100000", encoding="ascii")
    assert hw.cgroup_cpu_limit(**escape) is None


def test_a_v1_quota_bounds_the_budget_and_minus_one_is_no_quota(tmp_path):
    # A v1 container: /proc/self/cgroup names the host-side path, the mount holds only the
    # container's own cgroup at its root, and the directory is the joined controller list.
    lines = ["12:cpu,cpuacct:/docker/abc", "1:name=systemd:/docker/abc", "0::/docker/abc"]
    limited = _tree(tmp_path / "v1", lines, {"cpu,cpuacct/cpu.cfs_quota_us": "200000\n",
                                             "cpu,cpuacct/cpu.cfs_period_us": "100000\n"})
    assert hw.cgroup_cpu_limit(**limited) == 2
    assert hw.usable_cpu_count(**limited) == min(_affinity(), 2)
    unlimited = _tree(tmp_path / "v1-unlimited", lines, {"cpu,cpuacct/cpu.cfs_quota_us": "-1\n",
                                                         "cpu,cpuacct/cpu.cfs_period_us": "100000\n"})
    assert hw.cgroup_cpu_limit(**unlimited) is None
    assert hw.usable_cpu_count(**unlimited) == _affinity()
    # No readable /proc/self/cgroup: the conventional `cpu` mount is still read at its root.
    alias = _tree(tmp_path / "v1-alias", None, {"cpu/cpu.cfs_quota_us": "250000",
                                                "cpu/cpu.cfs_period_us": "100000"})
    assert hw.cgroup_cpu_limit(**alias) == 3


def test_v1_is_read_only_where_v2_gives_no_answer(tmp_path):
    v1 = {"cpu/cpu.cfs_quota_us": "200000", "cpu/cpu.cfs_period_us": "100000"}
    both = _tree(tmp_path / "both", ["0::/"], {"cpu.max": "max 100000", **v1})
    assert hw.cgroup_cpu_limit(**both) is None            # v2 answered "no quota"
    garbage_v2 = _tree(tmp_path / "garbage", ["0::/"], {"cpu.max": "banana", **v1})
    assert hw.cgroup_cpu_limit(**garbage_v2) == 2          # an unparseable file says nothing


def test_missing_files_leave_the_budget_at_the_affinity(tmp_path):
    nothing = _tree(tmp_path, None, {})
    assert hw.cgroup_cpu_limit(**nothing) is None
    assert hw.usable_cpu_count(**nothing) == _affinity()


@pytest.mark.parametrize("content", [
    "banana", "100000", "0 100000", "-5 100000", "100000 0", "100000 -1", "max", "max 0",
    "1 2 3", "", b"\xff\xfe 100000", "1.5 100000"])
def test_a_garbage_cpu_max_is_no_quota(tmp_path, content):
    tree = _tree(tmp_path, ["0::/"], {"cpu.max": content})
    assert hw.cgroup_cpu_limit(**tree) is None
    assert hw.usable_cpu_count(**tree) == _affinity()


@pytest.mark.parametrize("quota, period", [
    ("abc", "100000"), ("100000", "0"), ("100000", "x"), ("0", "100000"), ("-7", "100000"),
    ("", "100000")])
def test_a_garbage_v1_pair_is_no_quota(tmp_path, quota, period):
    tree = _tree(tmp_path, None, {"cpu/cpu.cfs_quota_us": quota, "cpu/cpu.cfs_period_us": period})
    assert hw.cgroup_cpu_limit(**tree) is None
    assert hw.usable_cpu_count(**tree) == _affinity()


def test_on_this_box_the_budget_is_within_its_own_cgroup_quota():
    """The real box, read independently of the helper: where the mount's own `cpu.max` carries a
    quota, the budget may not exceed it (the root is an ancestor of every level the helper reads)."""
    try:
        with open("/sys/fs/cgroup/cpu.max", encoding="ascii") as fh:
            quota, period = fh.read().split()
    except (OSError, ValueError):
        pytest.skip("no readable cgroup v2 cpu.max on this box")
    if quota == "max":
        pytest.skip("this box's cgroup carries no CPU quota")
    assert hw.usable_cpu_count() <= -(-int(quota) // int(period))


# ------------------------------------------------ doc 69 69.23a: a failed probe is not an answer
class _Probe:
    """`query_nvidia_smi` as a scripted sequence: each call pops the next answer (an exception is
    raised), and the calls are counted."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def __call__(self, *_args, **_kwargs):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, BaseException):
            raise answer
        return answer


_H200 = [["0", "NVIDIA H200", "143771", "143000"], ["1", "NVIDIA H200", "143771", "143000"]]


def _fresh(monkeypatch, hw, probe, *, binary=True, retry_s=60.0):
    monkeypatch.setattr(hw, "_GPUS_CACHE", None)
    monkeypatch.setattr(hw, "_GPUS_FAILED_AT", None)
    monkeypatch.setattr(hw, "_GPU_PROBE_RETRY_S", retry_s)
    monkeypatch.setattr(hw, "query_nvidia_smi", probe)
    monkeypatch.setattr(hw.shutil, "which",
                        lambda name: "/usr/bin/nvidia-smi" if binary else None)


def test_a_failed_inventory_probe_is_retried_not_cached_for_the_process(monkeypatch):
    """v10: an empty `detect_gpus()` after a failed probe was cached for the whole process, and
    the Strategist's prompt said "0 GPUs (CPU only)" on four H200s. MUTATION: cache the failure."""
    import subprocess

    import looplab.core.hardware as hw

    probe = _Probe(subprocess.TimeoutExpired("nvidia-smi", 5.0), _H200)
    _fresh(monkeypatch, hw, probe, retry_s=0.0)
    assert hw.detect_gpus() == []
    assert [g["name"] for g in hw.detect_gpus()] == ["NVIDIA H200", "NVIDIA H200"]
    assert hw.detect_gpus() and probe.calls == 2, "an inventory IS the answer, and is cached"
    assert hw.gpu_summary().startswith("2 GPU(s): NVIDIA H200")


def test_a_failed_probe_is_not_repeated_inside_its_retry_window(monkeypatch):
    """MUTATION: drop the window -> every prompt build re-runs a failing `nvidia-smi` (5 s each)."""
    import looplab.core.hardware as hw

    probe = _Probe(None, _H200)
    _fresh(monkeypatch, hw, probe, retry_s=3600.0)
    assert hw.detect_gpus() == [] and hw.detect_gpus() == [] and probe.calls == 1


def test_a_box_without_nvidia_smi_is_an_answer_and_is_cached(monkeypatch):
    """No driver tooling at all is definitive, not a failure: probed once. MUTATION: treat it as a
    failure -> probed again on every call past the window."""
    import looplab.core.hardware as hw

    probe = _Probe(None, _H200)
    _fresh(monkeypatch, hw, probe, binary=False, retry_s=0.0)
    assert hw.detect_gpus() == [] and hw.detect_gpus() == [] and probe.calls == 1


def test_the_drivers_no_devices_answer_is_cached(monkeypatch):
    """`nvidia-smi` exit 6 ("No devices were found") is the driver ANSWERING, on a GPU-less box
    with the tooling installed; before, it was a failed probe re-run every window, forever
    (critic 2026-09-30: 10 spawns over ten simulated minutes). MUTATION: drop `rows == []`."""
    import looplab.core.hardware as hw

    probe = _Probe([], _H200)
    _fresh(monkeypatch, hw, probe, retry_s=0.0)
    assert hw.detect_gpus() == [] and hw.detect_gpus() == [] and probe.calls == 1


def test_the_inventory_reads_a_real_exit_6_as_that_answer(monkeypatch):
    """The same answer through the REAL launcher, with only `nvidia-smi` itself scripted: the
    inventory must ASK for the exit-6 reading, or the launcher's None is a failure re-probed every
    window. MUTATION: drop `no_devices_empty=True` from `detect_gpus` -> probed twice."""
    import subprocess

    import looplab.core.hardware as hw

    spawned: list = []

    def _run(argv, **_kwargs):
        spawned.append(argv)
        return subprocess.CompletedProcess(argv, hw.NVIDIA_SMI_NO_DEVICES,
                                           stdout="No devices were found\n", stderr="")

    monkeypatch.setattr(hw, "_GPUS_CACHE", None)
    monkeypatch.setattr(hw, "_GPUS_FAILED_AT", None)
    monkeypatch.setattr(hw, "_GPU_PROBE_RETRY_S", 0.0)
    monkeypatch.setattr(hw.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(hw.subprocess, "run", _run)
    assert hw.detect_gpus() == [] and hw.detect_gpus() == [] and len(spawned) == 1


def test_only_the_inventory_asks_for_the_no_devices_answer(monkeypatch):
    """The exit-6 answer is opt-in: the live GPU monitor keeps reading None as "unavailable"."""
    import subprocess

    import looplab.core.hardware as hw

    monkeypatch.setattr(hw.shutil, "which", lambda name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(hw.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], hw.NVIDIA_SMI_NO_DEVICES, stdout="No devices were found\n", stderr=""))
    assert hw.query_nvidia_smi("name") is None
    assert hw.query_nvidia_smi("name", no_devices_empty=True) == []


def test_the_retry_window_runs_on_the_monotonic_clock(monkeypatch):
    """A wall-clock step (NTP, a suspended VM) must neither pin a failure nor skip the window.
    MUTATION: read `time.time()` -> the patched monotonic clock moves and nothing re-probes."""
    import looplab.core.hardware as hw

    now = [1_000.0]
    monkeypatch.setattr(hw.time, "monotonic", lambda: now[0])
    probe = _Probe(None, _H200)
    _fresh(monkeypatch, hw, probe, retry_s=60.0)
    assert hw.detect_gpus() == [] and probe.calls == 1
    now[0] = 1_059.0
    assert hw.detect_gpus() == [] and probe.calls == 1, "inside the window: no probe"
    now[0] = 1_061.0
    assert len(hw.detect_gpus()) == 2 and probe.calls == 2, "past it: probed again, and answered"


def test_concurrent_callers_share_one_probe(monkeypatch):
    """After a window expires, every prompt build racing for the inventory used to spawn its own
    `nvidia-smi` (up to 5 s each). MUTATION: drop the lock -> several probes."""
    import threading
    import time

    import looplab.core.hardware as hw

    gate = threading.Event()

    class _Slow(_Probe):
        def __call__(self, *args, **kwargs):
            gate.wait(5)
            time.sleep(0.05)
            return super().__call__(*args, **kwargs)

    probe = _Slow(_H200)
    _fresh(monkeypatch, hw, probe)
    seen: list = []
    threads = [threading.Thread(target=lambda: seen.append(len(hw.detect_gpus())))
               for _ in range(6)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(10)
    assert seen == [2] * 6 and probe.calls == 1


def test_the_name_probe_shares_the_inventorys_answer_and_its_retry(monkeypatch):
    """`detect_gpu` cached a failed name probe for the process, so one prompt said "no GPU detected"
    beside an environment brief naming the GPUs (critic 2026-09-30). It is the inventory's first
    row now. MUTATION: cache the first answer in `detect_gpu` -> None forever."""
    import subprocess

    import looplab.core.hardware as hw

    probe = _Probe(subprocess.TimeoutExpired("nvidia-smi", 5.0), _H200)
    _fresh(monkeypatch, hw, probe, retry_s=0.0)
    assert hw.detect_gpu() is None
    assert hw.detect_gpu() == "NVIDIA H200"
    assert hw.gpu_summary().startswith("2 GPU(s): NVIDIA H200")
