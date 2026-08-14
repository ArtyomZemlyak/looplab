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

MOVED AGAIN, 2026-08-14, out of `agents/` and DOWN into `core/`. The move was made for the layering
rule `tests/test_package_contracts.py::test_runtime_imports_nothing_above_core` states, because
`runtime/sandbox.py` was then asking this module which keys the engine declared and `runtime` may
import nothing above `core`. LATER THE SAME DAY that caller went away — the extra-metric channel
grant moved UP to `engine/eval_dispatch.py`, since the question it answers is "did the ENGINE author
this artifact?" and `runtime` is handed an opaque string (see `engine_declared_extra_metric_keys`).
`core` is still the right home: the readers are now `engine` and `search`, both of which may import
`core` freely, and the alternative — a second copy of the key tuple — is the one thing that must NOT
happen, because the probe source is an input to a calibration digest and a second copy of its keys is
how the two silently drift apart. So this is a MOVE, exactly like `core/jsonlio.py` out of `events/`
and `core/envsafe.py`'s `SECRET_ENV` out of `runtime/sandbox.py`: one object, every old spelling
still naming it. `agents/roles.py` re-exports every name as before, and
`looplab/__init__.py::_RENAMED` routes the retired `looplab.agents.calibration` path to this module
rather than leaving a shim file behind — a shim would be a SECOND module object, and
`tests/test_auto_extra_metrics.py` monkeypatches `engine_declared_extra_metric_keys` through that
exact path to prove the grant site resolves the classifier through the probe's own module on every
call. A second object would make that patch a silent no-op instead of an error.

Nothing here is reached by PATH rather than by import, so the move costs no pinned identity:
`search/speculation_quality.py::speculation_implementation_digest` hashes every shipped `.py` with
its relative path in each manifest row, so it moves — but it already moves for any semantic edit and
today's merges moved it regardless, and no test pins its value. The Settings-derived
`SPECULATION_CALIBRATION_PROFILE_DIGEST` (`tests/test_calibration_profile_home.py`) does NOT move:
its preimage is the declared `Settings` field set, which a file move cannot touch.
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


def engine_declared_extra_metric_keys(code: object, *, engine_authored: bool) -> frozenset[str]:
    """The extra-metric keys the ENGINE put on this artifact's stdout line, not the candidate.

    `engine_authored` IS THE AUTHENTICATION; `code` only says WHICH engine artifact this is. It is a
    required keyword with no default, and that is the whole 2026-08-14 correction — see the section
    at the bottom of this docstring for what the one-argument version admitted.

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

    NOT AN IN-SOURCE MARKER, deliberately, though the probe already carries one
    (`# LOOPLAB_FOOTPRINT:`) and a `# LOOPLAB_EXTRA_METRICS: [...]` line would be the obvious
    generalization. The candidate WRITES its own source, so a self-declaring comment is a
    self-promotion primitive: any agent could tag its numbers authenticated.

    WHY THE BYTE PREFIX IS NOT THAT AUTHENTICATION EITHER, which is the correction this signature
    carries (found 2026-08-14 auditing the merge day against docs/36). Until then the whole grant was
    `code.startswith(SPECULATION_CUDA_PROBE_CODE_PREFIX)`, and `runtime/sandbox.py` passed the
    candidate's own `solution.py` as that `code` — the bytes an external coding agent
    (`agents/cli_agent.py::CliAgentDeveloper`) authors with a real shell. The prefix is a public
    constant in the shipped tree, not a secret: driven, `tools/env_inspect.py::read_installed` pages
    this module out verbatim with no package allow-list. So prefix + one `print` earned
    `{'device_count': 'engine', 'alloc_bytes': 'engine'}`, and the operator's "authenticated only"
    switch (`Settings.auto_extra_metrics=false`) then ADMITTED the forged pair. The reasoning above
    is unchanged and still right — WHO WROTE THE PRINT STATEMENT is the separating property — but a
    predicate over source the candidate can rewrite cannot answer it, and narrowing the predicate
    (exact key SET, the three static values) does not help: those values are public constants too,
    so a forgery just prints them.
    Nothing derivable from the artifact's CONTENT can authenticate its AUTHOR.

    So the fact is CARRIED rather than re-derived: `engine_authored` is the caller's assertion that
    the engine itself produced this artifact's source, and only `engine/speculation_gate.py::
    engine_authored_artifacts` may make it — it reads the engine's OWN role wiring (the exact
    `ToyObjectiveDeveloper` with `calibration_gpu_probe` true, the one splicer that exists), which is
    state no candidate can reach. The prefix check then keeps its real job, which was never
    authentication: it says WHICH engine artifact this is, and therefore which keys are its own. Both
    conjuncts are required and the keyword has no default, so a caller that cannot make the assertion
    cannot silently get the old behaviour — it gets a TypeError.

    THE HONEST LIMIT. This removes the ambiguity only where the engine authored the writer. For an
    agent-authored artifact nothing available at capture separates a diagnostic from a measurement —
    a script printing `{"metric": .9, "seed": 42, "n_train": 5000, "val_auc": .88}` offers no signal
    that `seed` is bookkeeping and `val_auc` is a result — and there `auto` remains the complete and
    correct answer: the candidate said it, nobody checked it. Total by construction (any non-string,
    any other artifact) so a caller can pass whatever code it has, and the fail-safe direction is
    always `auto`.
    """
    if not engine_authored:
        return frozenset()
    if not isinstance(code, str) or not code.startswith(SPECULATION_CUDA_PROBE_CODE_PREFIX):
        return frozenset()
    return frozenset(SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS)
