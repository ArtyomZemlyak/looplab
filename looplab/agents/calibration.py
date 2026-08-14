"""The CUDA probe the speculation calibration runs to measure a real device (doc 25 AG-02).

This lived in the middle of `agents/roles.py`, a module whose job is LLM role backends and their
prompt fragments. The probe is neither: it is the source of a small CUDA program the speculation
calibration executes to observe allocation behaviour on an actual GPU, and its only consumers are
`search/speculation_quality.py` and the calibration tests. Sitting inside `roles.py` it made a
1058-line god-module 137 lines longer and put GPU measurement in the file people open to change a
prompt.

Moved VERBATIM — the probe source is an input to a calibration DIGEST, so a whitespace change here
would revoke issued receipts exactly like the implementation digest does. `agents/roles.py`
re-exports every name, so both spellings resolve to the SAME objects.
"""
from __future__ import annotations

# Source-owned, exact GPU proof embedded only in maintainer calibration artifacts.  The rollout gate
# compares the shipped code prefix byte-for-byte with this constant and validates the four numeric
# metrics emitted on the objective's final JSON line.
SPECULATION_CUDA_PROBE_VERSION = 1
SPECULATION_CUDA_PROBE_ALLOC_BYTES = 4096
SPECULATION_CUDA_PROBE_DEVICE_ORDINAL = 0
SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC = "device_count"
SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS = (
    "speculation_cuda_probe_v",
    SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC,
    "alloc_bytes",
    "device_ordinal",
)
SPECULATION_CUDA_PROBE_STATIC_EXTRA_METRICS = (
    ("speculation_cuda_probe_v", SPECULATION_CUDA_PROBE_VERSION),
    ("alloc_bytes", SPECULATION_CUDA_PROBE_ALLOC_BYTES),
    ("device_ordinal", SPECULATION_CUDA_PROBE_DEVICE_ORDINAL),
)

SPECULATION_CUDA_PROBE_CODE_PREFIX = '''\
# LOOPLAB_FOOTPRINT: {"gpus":1}
import ctypes as _looplab_ctypes
import os as _looplab_os
import sys as _looplab_sys

_looplab_cuda_probe_v = 1
_looplab_cuda_alloc_bytes = 4096
_looplab_cuda_device_ordinal = 0
if _looplab_os.name == "nt" and _looplab_sys.platform == "win32":
    _looplab_cuda = _looplab_ctypes.WinDLL("nvcuda.dll")
elif _looplab_sys.platform.startswith("linux"):
    _looplab_cuda = _looplab_ctypes.CDLL("libcuda.so.1")
else:
    raise RuntimeError("speculation calibration requires Windows or Linux CUDA Driver API")

def _looplab_cuda_symbol(*_looplab_names):
    for _looplab_name in _looplab_names:
        try:
            return getattr(_looplab_cuda, _looplab_name)
        except AttributeError:
            pass
    raise RuntimeError("CUDA driver is missing required symbol " + _looplab_names[0])

def _looplab_cuda_bind(_looplab_names, _looplab_argtypes):
    _looplab_function = _looplab_cuda_symbol(*_looplab_names)
    _looplab_function.restype = _looplab_ctypes.c_int
    _looplab_function.argtypes = _looplab_argtypes
    return _looplab_function

def _looplab_cuda_check(_looplab_result, _looplab_operation):
    if int(_looplab_result) != 0:
        raise RuntimeError(
            _looplab_operation + " failed with CUDA result " + str(int(_looplab_result)))

_looplab_cu_init = _looplab_cuda_bind(("cuInit",), [_looplab_ctypes.c_uint])
_looplab_cu_device_count = _looplab_cuda_bind(
    ("cuDeviceGetCount",), [_looplab_ctypes.POINTER(_looplab_ctypes.c_int)])
_looplab_cu_device_get = _looplab_cuda_bind(
    ("cuDeviceGet",),
    [_looplab_ctypes.POINTER(_looplab_ctypes.c_int), _looplab_ctypes.c_int])
_looplab_cu_ctx_create = _looplab_cuda_bind(
    ("cuCtxCreate_v2", "cuCtxCreate"),
    [_looplab_ctypes.POINTER(_looplab_ctypes.c_void_p),
     _looplab_ctypes.c_uint, _looplab_ctypes.c_int])
_looplab_cu_mem_alloc = _looplab_cuda_bind(
    ("cuMemAlloc_v2", "cuMemAlloc"),
    [_looplab_ctypes.POINTER(_looplab_ctypes.c_uint64), _looplab_ctypes.c_size_t])
_looplab_cu_mem_free = _looplab_cuda_bind(
    ("cuMemFree_v2", "cuMemFree"), [_looplab_ctypes.c_uint64])
_looplab_cu_ctx_destroy = _looplab_cuda_bind(
    ("cuCtxDestroy_v2", "cuCtxDestroy"), [_looplab_ctypes.c_void_p])

_looplab_cuda_count = _looplab_ctypes.c_int()
_looplab_cuda_device = _looplab_ctypes.c_int()
_looplab_cuda_context = _looplab_ctypes.c_void_p()
_looplab_cuda_pointer = _looplab_ctypes.c_uint64()
_looplab_cuda_failure = None
_looplab_cuda_cleanup_failures = []
try:
    _looplab_cuda_check(_looplab_cu_init(0), "cuInit")
    _looplab_cuda_check(
        _looplab_cu_device_count(_looplab_ctypes.byref(_looplab_cuda_count)),
        "cuDeviceGetCount")
    if _looplab_cuda_count.value <= 0:
        raise RuntimeError("speculation calibration requires a CUDA-visible device")
    _looplab_cuda_check(
        _looplab_cu_device_get(
            _looplab_ctypes.byref(_looplab_cuda_device), _looplab_cuda_device_ordinal),
        "cuDeviceGet")
    _looplab_cuda_check(
        _looplab_cu_ctx_create(
            _looplab_ctypes.byref(_looplab_cuda_context), 0, _looplab_cuda_device),
        "cuCtxCreate")
    if not _looplab_cuda_context.value:
        raise RuntimeError("cuCtxCreate returned a null context")
    _looplab_cuda_check(
        _looplab_cu_mem_alloc(
            _looplab_ctypes.byref(_looplab_cuda_pointer), _looplab_cuda_alloc_bytes),
        "cuMemAlloc")
    if not _looplab_cuda_pointer.value:
        raise RuntimeError("cuMemAlloc returned a null pointer")
except Exception as _looplab_cuda_caught:
    _looplab_cuda_failure = _looplab_cuda_caught
finally:
    if _looplab_cuda_pointer.value:
        try:
            _looplab_cuda_check(
                _looplab_cu_mem_free(_looplab_cuda_pointer), "cuMemFree")
        except Exception as _looplab_cuda_cleanup_caught:
            _looplab_cuda_cleanup_failures.append(str(_looplab_cuda_cleanup_caught))
    if _looplab_cuda_context.value:
        try:
            _looplab_cuda_check(
                _looplab_cu_ctx_destroy(_looplab_cuda_context), "cuCtxDestroy")
        except Exception as _looplab_cuda_cleanup_caught:
            _looplab_cuda_cleanup_failures.append(str(_looplab_cuda_cleanup_caught))
if _looplab_cuda_failure is not None:
    _looplab_cuda_suffix = (
        "; cleanup: " + "; ".join(_looplab_cuda_cleanup_failures)
        if _looplab_cuda_cleanup_failures else "")
    raise RuntimeError(
        "speculation calibration CUDA proof failed: "
        + str(_looplab_cuda_failure) + _looplab_cuda_suffix) from _looplab_cuda_failure
if _looplab_cuda_cleanup_failures:
    raise RuntimeError(
        "speculation calibration CUDA cleanup failed: "
        + "; ".join(_looplab_cuda_cleanup_failures))
_looplab_cuda_device_count_value = int(_looplab_cuda_count.value)

'''


def engine_declared_extra_metric_keys(code: object) -> frozenset[str]:
    """The extra-metric keys the ENGINE put on this artifact's stdout line, not the candidate.

    THE PROBLEM THIS ANSWERS. `runtime/sandbox.py::json_line_extras` harvests every numeric key off
    an artifact's final JSON line, and the record then calls each of them a metric. Measured over
    the preserved corpus (238 logs under `runs/`, 1642 recorded values, 10 distinct keys), the
    population that arrives through that door is not one population but two:

      * 1636 values / 4 keys — `speculation_cuda_probe_v`, `device_count`, `alloc_bytes`,
        `device_ordinal`. A schema VERSION, a hardware inventory count and two request constants.
        None of them measures the experiment. Every one of them was printed by THIS module's source.
      * 6 values / 6 keys — `train_auc`, `test_auc`, `cv_mean_auc`, `cv_std_auc`, `std`,
        `overfitting_gap`. Genuine measurements, printed by an agent-authored script.

    WHAT ACTUALLY SEPARATES THEM, after the alternatives were tried against that corpus:

      * NOT the name. A name list is the heuristic `json_line_extras` already carries (`trials`,
        `params`, `seconds`, `epoch`, `step`); adding four entries is the same heuristic one entry
        longer, and it is wrong in both directions — `alloc_bytes` and `device_count` are perfectly
        good measurements for a memory benchmark, and an agent that prints
        `{"metric": x, "device_count": 8}` would be silently promoted by a name match.
      * NOT the shape. "A value that never varies is a constant, not a measurement" is untestable
        here (each of the 6 genuine values occurs exactly ONCE, so all ten keys are trivially
        constant), prospectively false (`device_count` IS a measurement of the box — the receipt
        validator range-checks it), and undecidable at capture anyway: variance is a cross-node
        property, so a capture-time tag derived from it would change as later nodes arrived and the
        record would stop being deterministic.
      * WHO WROTE THE PRINT STATEMENT. That is the one property that separates the two populations
        cleanly, and the engine can answer it for exactly the artifacts it authored itself.

    So this is not a key registry with a name check bolted on: the key set is consulted ONLY after
    the artifact's source is authenticated as engine-owned, byte-exactly, by the same
    `SPECULATION_CUDA_PROBE_CODE_PREFIX` comparison `search/speculation_quality.py::
    _validate_cuda_probe_artifact` uses to admit the proof downstream. An agent script that prints
    `speculation_cuda_probe_v` stays `auto`, because its source does not carry the prefix.

    NOT AN IN-SOURCE MARKER, deliberately, though the probe already carries one
    (`# LOOPLAB_FOOTPRINT:`) and a `# LOOPLAB_EXTRA_METRICS: [...]` line would be the obvious
    generalization. The candidate WRITES its own source, so a self-declaring comment is a
    self-promotion primitive: any agent could tag its numbers authenticated. The byte-exact prefix
    check is what makes the declaration the engine's rather than the artifact's, and once it is
    required the marker adds nothing.

    THE HONEST LIMIT. This removes the ambiguity only where the engine authored the writer. For an
    agent-authored artifact nothing available at capture separates a diagnostic from a measurement —
    a script printing `{"metric": .9, "seed": 42, "n_train": 5000, "val_auc": .88}` offers no signal
    that `seed` is bookkeeping and `val_auc` is a result — and there `auto` remains the complete and
    correct answer: the candidate said it, nobody checked it. Total by construction (any non-string,
    any other artifact) so a caller can pass whatever code it has, and the fail-safe direction is
    always `auto`.
    """
    if not isinstance(code, str) or not code.startswith(SPECULATION_CUDA_PROBE_CODE_PREFIX):
        return frozenset()
    return frozenset(SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS)
