"""Honest runtime-capability brief + task-aware gating (no torch claim for offline tasks)."""
from __future__ import annotations

from looplab.core.hardware import runtime_capabilities_brief, task_runtime_caps


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
