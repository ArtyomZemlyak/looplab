"""Best-effort hardware / runtime-capability detection for HONEST prompt-building.

A task brief must not claim "CPU only, no GPU" on a GPU box, nor "only numpy/pandas/scikit-learn"
when `auto_install_deps` (deps.py) will pip-install torch/xgboost/etc. on first import. A wrong
brief makes the agent downgrade a neural-net idea (tree_dim/num_layers) into a tree model. This
module supplies the capability sentence those briefs should use — gated so it's only ever emitted
for tasks that actually support it (see `task_runtime_caps`).
"""
from __future__ import annotations

import ctypes
import inspect
import os
import shutil
import subprocess
import sys

_GPU_CACHE: "tuple[bool, str | None] | None" = None
_GPUS_CACHE: "list[dict] | None" = None


def detect_gpus() -> list[dict]:
    """All visible GPUs as [{index, name, mem_total_mib, mem_free_mib}], best-effort via nvidia-smi
    (no torch dependency — torch may be auto-installed later). Empty list when none/undetectable.
    Cached for the process. This is the richer counterpart of `detect_gpu()` (which returns only the
    first GPU's name, kept for back-compat)."""
    global _GPUS_CACHE
    if _GPUS_CACHE is not None:
        return _GPUS_CACHE
    gpus: list[dict] = []
    try:
        from looplab.core.parse import to_int
        for parts in (query_nvidia_smi("index,name,memory.total,memory.free") or []):
            if len(parts) >= 4:
                # A GPU name may itself contain a comma (the sibling detect_gpu documents + handles
                # this) — the CSV split then yields >4 fields and fixed positions parts[2]/parts[3]
                # read a name fragment / the wrong column. `index` is the FIRST field and the two
                # memory numbers are the LAST two, so parse from the ends and rejoin the middle as name.
                gpus.append({"index": to_int(parts[0]), "name": ",".join(parts[1:-2]).strip(),
                             "mem_total_mib": to_int(parts[-2]), "mem_free_mib": to_int(parts[-1])})
    except (OSError, ValueError, subprocess.SubprocessError):
        gpus = []
    _GPUS_CACHE = gpus
    return gpus


class _CudaDriverError(RuntimeError):
    """The CUDA Driver API was absent, incomplete, or returned a non-success code."""


class _CudaUuid(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_ubyte * 16)]


def _cuda_symbol(library, names: tuple[str, ...], argtypes: list[object]):
    """Bind the first exported ABI-compatible CUDA symbol in ``names``."""
    for name in names:
        try:
            function = getattr(library, name)
        except AttributeError:
            continue
        function.restype = ctypes.c_int
        function.argtypes = argtypes
        return function
    raise _CudaDriverError(f"CUDA driver is missing required symbol {names[0]}")


class _CtypesCudaDriver:
    """Small Python-valued facade over the CUDA Driver API used by inventory discovery.

    The separate facade is intentional: tests can pass a tiny fake with these methods and exercise
    validation/order/identity without loading a real NVIDIA library.
    """

    def __init__(self, library):
        self._init = _cuda_symbol(library, ("cuInit",), [ctypes.c_uint])
        self._driver_version = _cuda_symbol(
            library, ("cuDriverGetVersion",), [ctypes.POINTER(ctypes.c_int)])
        self._device_count = _cuda_symbol(
            library, ("cuDeviceGetCount",), [ctypes.POINTER(ctypes.c_int)])
        self._device_get = _cuda_symbol(
            library, ("cuDeviceGet",), [ctypes.POINTER(ctypes.c_int), ctypes.c_int])
        self._device_name = _cuda_symbol(
            library, ("cuDeviceGetName",), [ctypes.c_char_p, ctypes.c_int, ctypes.c_int])
        self._device_total_mem = _cuda_symbol(
            library, ("cuDeviceTotalMem_v2", "cuDeviceTotalMem"),
            [ctypes.POINTER(ctypes.c_size_t), ctypes.c_int])
        self._device_uuid = _cuda_symbol(
            library, ("cuDeviceGetUuid_v2", "cuDeviceGetUuid"),
            [ctypes.POINTER(_CudaUuid), ctypes.c_int])
        # A rollout receipt binds both CUDA UUID and PCI identity.  Treating PCI as optional would
        # produce two hardware schemas (and make a later driver upgrade change receipt identity), so
        # an incomplete driver surface fails closed before calibration begins.
        self._device_pci_bus_id = _cuda_symbol(
            library, ("cuDeviceGetPCIBusId",),
            [ctypes.c_char_p, ctypes.c_int, ctypes.c_int])

    @classmethod
    def load(cls, *, platform_name: str | None = None):
        platform_name = sys.platform if platform_name is None else platform_name
        if platform_name == "win32":
            library = ctypes.WinDLL("nvcuda.dll")
        elif platform_name.startswith("linux"):
            library = ctypes.CDLL("libcuda.so.1")
        else:
            raise _CudaDriverError(f"unsupported CUDA driver platform: {platform_name}")
        return cls(library)

    @staticmethod
    def _check(result: int, operation: str) -> None:
        if int(result) != 0:
            raise _CudaDriverError(f"{operation} failed with CUDA result {int(result)}")

    def initialize(self) -> None:
        self._check(self._init(0), "cuInit")

    def driver_version(self) -> int:
        value = ctypes.c_int()
        self._check(self._driver_version(ctypes.byref(value)), "cuDriverGetVersion")
        return int(value.value)

    def device_count(self) -> int:
        value = ctypes.c_int()
        self._check(self._device_count(ctypes.byref(value)), "cuDeviceGetCount")
        return int(value.value)

    def device(self, ordinal: int) -> int:
        value = ctypes.c_int()
        self._check(self._device_get(ctypes.byref(value), ordinal), "cuDeviceGet")
        return int(value.value)

    def device_name(self, device: int) -> str:
        value = ctypes.create_string_buffer(256)
        self._check(self._device_name(value, len(value), device), "cuDeviceGetName")
        return value.value.decode("utf-8", errors="strict").strip()

    def device_total_memory(self, device: int) -> int:
        value = ctypes.c_size_t()
        self._check(self._device_total_mem(ctypes.byref(value), device), "cuDeviceTotalMem")
        return int(value.value)

    def device_uuid(self, device: int) -> bytes:
        value = _CudaUuid()
        self._check(self._device_uuid(ctypes.byref(value), device), "cuDeviceGetUuid")
        return bytes(value.bytes)

    def device_pci_bus_id(self, device: int) -> str:
        value = ctypes.create_string_buffer(64)
        self._check(
            self._device_pci_bus_id(value, len(value), device), "cuDeviceGetPCIBusId")
        return value.value.decode("ascii", errors="strict").strip().lower()


def _canonical_cuda_uuid(raw: object) -> str:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 16 or not any(raw):
        raise _CudaDriverError("CUDA device UUID is missing or malformed")
    encoded = bytes(raw).hex()
    return "GPU-" + "-".join((
        encoded[:8], encoded[8:12], encoded[12:16], encoded[16:20], encoded[20:]))


def _nvidia_driver_versions_by_uuid() -> dict[str, str]:
    """Display-driver versions keyed by exact UUID; physical indices are never joined."""
    rows = query_nvidia_smi("uuid,driver_version") or []
    if not isinstance(rows, list):
        return {}
    versions: dict[str, str] = {}
    for parts in rows:
        if not isinstance(parts, list) or len(parts) != 2:
            return {}
        raw_uuid, raw_version = parts
        uuid = raw_uuid.strip().lower() if isinstance(raw_uuid, str) else ""
        version = raw_version.strip() if isinstance(raw_version, str) else ""
        if (not uuid.startswith(("gpu-", "mig-")) or uuid in versions
                or not version or len(version) > 64
                or any(ord(char) < 32 or ord(char) > 126 for char in version)):
            return {}
        versions[uuid] = version
    return versions


def _cuda_driver_inventory(*, api=None, driver_version_query=None) -> list[dict]:
    """Return a calibration-grade, logical-visible inventory or raise on ambiguity."""
    api = _CtypesCudaDriver.load() if api is None else api
    driver_version_query = (
        _nvidia_driver_versions_by_uuid if driver_version_query is None else driver_version_query)
    api.initialize()
    cuda_driver_version = api.driver_version()
    device_count = api.device_count()
    if (type(cuda_driver_version) is not int or cuda_driver_version <= 0
            or type(device_count) is not int or not 0 <= device_count <= 1024):
        raise _CudaDriverError("CUDA driver returned an invalid version or device count")
    if device_count == 0:
        return []

    display_versions = driver_version_query()
    if not isinstance(display_versions, dict) or not display_versions:
        raise _CudaDriverError("display driver version is unavailable by CUDA UUID")

    inventory: list[dict] = []
    seen_uuids: set[str] = set()
    seen_pci: set[str] = set()
    for logical_index in range(device_count):
        device = api.device(logical_index)
        name = api.device_name(device)
        total_bytes = api.device_total_memory(device)
        uuid = _canonical_cuda_uuid(api.device_uuid(device))
        pci_bus_id = api.device_pci_bus_id(device)
        driver_version = display_versions.get(uuid.lower())
        if (type(device) is not int or not name or len(name) > 256
                or type(total_bytes) is not int or total_bytes < 1024 * 1024
                or uuid in seen_uuids or not isinstance(driver_version, str)
                or not driver_version):
            raise _CudaDriverError("CUDA device identity is incomplete or ambiguous")
        if (not isinstance(pci_bus_id, str) or not pci_bus_id or len(pci_bus_id) > 64
                or pci_bus_id in seen_pci):
            raise _CudaDriverError("CUDA PCI identity is missing, malformed, or ambiguous")
        seen_pci.add(pci_bus_id)
        seen_uuids.add(uuid)
        inventory.append({
            "index": logical_index,
            "uuid": uuid,
            "pci_bus_id": pci_bus_id,
            "name": name,
            "mem_total_mib": total_bytes // (1024 * 1024),
            "driver_version": driver_version,
            "cuda_driver_version": cuda_driver_version,
        })
    return inventory


def effective_gpu_inventory(*, _cuda_api=None, _driver_version_query=None) -> list[dict]:
    """Calibration-grade GPUs visible to this process in CUDA logical order.

    CUDA itself applies ``CUDA_VISIBLE_DEVICES`` before ``cuDeviceGetCount/cuDeviceGet``.  This
    function therefore never guesses that a numeric visibility selector is an ``nvidia-smi``
    physical index.  Every row has CUDA-owned UUID plus required PCI identity, and an exact UUID join
    supplies the display-driver version.  Any missing/ambiguous identity
    fails closed.  ``detect_gpus`` remains the cached, best-effort legacy physical inventory.

    Private keyword seams accept a fake CUDA facade/version query for hardware-free unit tests.
    """
    try:
        return _cuda_driver_inventory(
            api=_cuda_api, driver_version_query=_driver_version_query)
    except Exception:  # noqa: BLE001 -- this public capability boundary is deliberately fail-closed
        return []


def query_nvidia_smi(fields: str, *, timeout: float = 5.0, nounits: bool = True):
    """Run `nvidia-smi --query-gpu=<fields>` and return the comma-split, stripped rows, or None
    when there is no usable GPU signal (no binary / non-zero exit / empty output). The ONE
    launcher+CSV-splitter shared by the inventory here, the name probe below, and the live
    monitor in serve/routers/misc — callers keep their own field lists, timeouts, row shapes
    and exception posture (this raises subprocess/OS errors; callers catch per their contract)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    fmt = "csv,noheader,nounits" if nounits else "csv,noheader"
    out = subprocess.run([exe, f"--query-gpu={fields}", f"--format={fmt}"],
                         capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0 or not (out.stdout or "").strip():
        return None
    return [[c.strip() for c in line.split(",")] for line in out.stdout.strip().splitlines()]


def usable_cpu_count() -> int:
    """Usable CPU cores respecting the cgroup cpuset (sched_getaffinity), falling back to cpu_count.
    This is the number an eval's thread pools are (and should be) sized against."""
    try:
        return len(os.sched_getaffinity(0))   # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def _fmt_gib(mib) -> str:
    return f"{round(mib/1024)} GB" if isinstance(mib, int) else "? GB"


def gpu_summary() -> str:
    """One-line human summary of GPUs, e.g. '2 GPU(s): NVIDIA H200 (143 GB), NVIDIA H200 (143 GB)'."""
    gpus = detect_gpus()
    if not gpus:
        return "0 GPUs (CPU only)"
    parts = [f"{g.get('name','GPU')} ({_fmt_gib(g.get('mem_total_mib'))})" for g in gpus]
    return f"{len(gpus)} GPU(s): " + ", ".join(parts)


def environment_brief() -> str:
    """A concise, HONEST hardware line for agent prompts: usable CPU cores + GPU inventory. Callers
    combine this with repo/data-size notes as needed."""
    return f"Hardware: {usable_cpu_count()} usable CPU cores; {gpu_summary()}."


def operational_attention_points(*, include_env: bool = True) -> str:
    """The shared 'be environment-aware' block appended to every planning/coding agent's system
    prompt (Genesis, Boss, Researcher, Developer, Strategist). CUES, not rules — the agent adapts
    them to the task. Starts with the live hardware line so decisions (GPU count, batch size,
    parallelism) are grounded in what's actually available. Kept in ONE place so all agents share
    the same operational awareness."""
    head = (environment_brief() + "\n") if include_env else ""
    return head + (
        "Operational attention points (consider these and adapt to the task — they are cues, not "
        "rigid rules):\n"
        "- HARDWARE: check the CPU/GPU actually available (via a gpu_info tool when you have one, "
        "else nvidia-smi) and plan experiments, "
        "parallelism, batch sizes and precision from it. By DEFAULT use ALL available GPUs (e.g. "
        "`--gpus <N>` / DataParallel/DDP for N GPUs) unless the task says otherwise; don't leave GPUs "
        "idle or run a tiny single-GPU job on a multi-GPU box without reason.\n"
        "- REPO/DATA SIZE: before copying or seeding, check how big the repo and datasets are. Copy "
        "only what's needed (usually the source code); never deep-copy multi-GB artifacts (model "
        "checkpoints, datasets) into a workspace — reference large read-only inputs by absolute path "
        "instead. If a tree is unexpectedly huge or you're unsure what to copy, ASK rather than "
        "blindly copy.\n"
        "- DEPENDENCIES: analyze the environment first; do NOT (re)install packages needlessly. "
        "Some roles cannot install at all — then work with what is installed; when you CAN install, "
        "install only what's genuinely missing or what the user asked for, and prefer a ONE-TIME "
        "install into the shared interpreter when deps are stable across experiments (vs reinstalling "
        "before every run). Note a venv may be impossible on some mounts (e.g. s3fs) — install "
        "directly when that's the case.\n"
        "- REUSE over REIMPLEMENT: prefer orchestrating the repo's existing, working scripts (via "
        "subprocess/import) to rewriting data loading, models or training from scratch — custom data "
        "formats (pickled classes) usually only load with the repo's own code.\n"
        "- MATCH THE SCRIPT'S CONTRACT EXACTLY: when driving an existing script, match the exact names "
        "and labels it keys on — not just flag names+types but the STRINGS it looks up. E.g. a "
        "checkpoint/early-stop that monitors `val/<metric>` needs the validation split named so the "
        "script logs `val/<metric>` (naming it 'test' makes `test/<metric>` and the monitor fails); a "
        "metric you read back from a filename/results file must use that exact key. When an error says "
        "a key/monitor was 'not found in the returned metrics: [...]', rename YOUR argument to match "
        "one of the listed keys.\n"
        "- HYPERPARAMETERS/BUDGET: when the task doesn't pin them, estimate sane values from the "
        "model size, available GPU memory, and any documented recipe; keep each experiment within the "
        "eval timeout; use ABSOLUTE paths for inputs outside the repo.\n"
        "- EXPENSIVE-STEP REUSE: split the eval into stages (train / score) via the stage manifest "
        "so a cheap late-stage failure never repeats training — the ENGINE re-runs only what changed "
        "and reuses the completed train stage's artifact. Write each artifact to a stable path inside "
        "the eval workdir; do NOT hand-roll 'skip if output exists' checks (a partial or foreign "
        "artifact silently freezes the result); never load artifacts your pipeline didn't produce.\n"
        "- ENVIRONMENT-FIRST: inspect the interpreter, installed packages and paths before assuming; "
        "when a sensitive choice is ambiguous (what to copy, how much to install, how long to train), "
        "prefer the cheap/safe option and surface the question.")


def detect_gpu() -> str | None:
    """The first GPU's name via `nvidia-smi`, or None if none/undetectable. Cached for the process.
    Deliberately NO torch dependency — torch may not be installed yet (it's auto-installed on demand),
    so importing it here would either fail or trigger a heavy import just to probe the device."""
    global _GPU_CACHE
    if _GPU_CACHE is not None:
        return _GPU_CACHE[1]
    name: str | None = None
    try:
        # nounits=False: matches the pre-extraction call (`--format=csv,noheader` — a name-only
        # query has no unit columns to strip).
        rows = query_nvidia_smi("name", nounits=False)
        if rows and rows[0] and rows[0][0]:
            name = ",".join(rows[0]).strip() or None   # a GPU name may contain a comma — rejoin
    except (OSError, ValueError, subprocess.SubprocessError):
        name = None
    _GPU_CACHE = (True, name)
    return name


def runtime_capabilities_brief(*, auto_install: bool, gpu: str | None = None) -> str:
    """The 'what you may use' sentence for a task brief, honest about libraries + hardware.

    `auto_install` True  -> the engine pip-installs missing packages, so deep-learning / boosting
                            frameworks are fair game and the agent should build the model the idea
                            actually calls for instead of forcing sklearn.
    `auto_install` False -> the conservative legacy contract (only the pre-installed stack)."""
    if not auto_install:
        return ("You may use numpy, pandas and scikit-learn (all installed) plus the Python "
                "standard library; CPU only, no GPU/network.")
    hw = (f"a GPU is available ({gpu}); use it when your framework supports it (e.g. torch.cuda)"
          if gpu else "no GPU detected, so assume CPU")
    return ("You may use numpy, pandas and scikit-learn AND deep-learning / gradient-boosting "
            "frameworks (torch, xgboost, lightgbm, catboost): any package you import that isn't "
            "installed is auto-installed and the run retried, so build the model the idea actually "
            "calls for (e.g. a real neural network with the proposed architecture) rather than "
            f"downgrading it to sklearn just to avoid an import. Hardware: {hw}. No internet for "
            "downloading data, but missing Python packages are installed for you.")


def task_runtime_caps(task, *, auto_install: bool, gpu: str | None) -> str | None:
    """The capability sentence for THIS task, or None when the task is locked to the offline stack.

    Task-aware on purpose: synthetic/tutorial tasks (CodeRegressionTask, the offline MLEBenchTask)
    genuinely run with only numpy+stdlib, so they must NEVER be told torch is available — even when
    the engine flag is on. The opt-in signal is whether the task's `llm_roles` accepts a
    `runtime_caps` kwarg; a task that doesn't is treated as locked and gets None (conservative)."""
    roles = getattr(task, "llm_roles", None)
    if not callable(roles):
        return None
    try:
        if "runtime_caps" not in inspect.signature(roles).parameters:
            return None
    except (TypeError, ValueError):
        return None
    return runtime_capabilities_brief(auto_install=auto_install, gpu=gpu)
