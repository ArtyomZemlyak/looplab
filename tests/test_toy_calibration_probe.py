"""Source-owned CUDA proof embedded by the maintainer-only Toy calibration role."""
from __future__ import annotations

import hashlib

from looplab.agents.roles import (
    SPECULATION_CUDA_PROBE_ALLOC_BYTES,
    SPECULATION_CUDA_PROBE_CODE_PREFIX,
    SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC,
    SPECULATION_CUDA_PROBE_DEVICE_ORDINAL,
    SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS,
    SPECULATION_CUDA_PROBE_STATIC_EXTRA_METRICS,
    SPECULATION_CUDA_PROBE_VERSION,
    ToyObjectiveDeveloper,
)
from looplab.core.models import Idea


def _idea(*, footprint=None):
    return Idea(
        operator="draft",
        params={"x": 1.25, "y": -2.5},
        rationale="test",
        footprint=footprint,
    )


def test_default_toy_objective_bytes_are_unchanged():
    code = ToyObjectiveDeveloper().implement(_idea())

    # Pin the pre-calibration Toy artifact byte-for-byte: the default-off proof flag must not perturb
    # ordinary ToyTask runs, event identities, or their deterministic search trajectory.
    assert hashlib.sha256(code.encode("utf-8")).hexdigest() == (
        "c1cfc8112314ab6d28d53555ade95272c5b9cef1fea7ca9a63f189f2336de0d5")
    assert not code.startswith(SPECULATION_CUDA_PROBE_CODE_PREFIX)


def test_calibration_toy_artifact_has_exact_cuda_driver_proof_and_numeric_metrics():
    developer = ToyObjectiveDeveloper(calibration_gpu_probe=True)
    code = developer.implement(_idea(footprint={"gpus": 1}))

    assert code.startswith(SPECULATION_CUDA_PROBE_CODE_PREFIX)
    assert "nvidia-smi" not in SPECULATION_CUDA_PROBE_CODE_PREFIX
    for symbol in (
        "cuInit", "cuDeviceGetCount", "cuDeviceGet", "cuCtxCreate_v2",
        "cuMemAlloc_v2", "cuMemFree_v2", "cuCtxDestroy_v2",
    ):
        assert symbol in SPECULATION_CUDA_PROBE_CODE_PREFIX
    assert f"_looplab_cuda_alloc_bytes = {SPECULATION_CUDA_PROBE_ALLOC_BYTES}" in code
    assert f"_looplab_cuda_device_ordinal = {SPECULATION_CUDA_PROBE_DEVICE_ORDINAL}" in code
    for metric in SPECULATION_CUDA_PROBE_EXTRA_METRIC_KEYS:
        assert f'"{metric}"' in code
    assert SPECULATION_CUDA_PROBE_DEVICE_COUNT_METRIC == "device_count"
    assert dict(SPECULATION_CUDA_PROBE_STATIC_EXTRA_METRICS) == {
        "speculation_cuda_probe_v": SPECULATION_CUDA_PROBE_VERSION,
        "alloc_bytes": SPECULATION_CUDA_PROBE_ALLOC_BYTES,
        "device_ordinal": SPECULATION_CUDA_PROBE_DEVICE_ORDINAL,
    }
    assert code.rstrip().endswith("}))")
    assert developer.last_footprint == {"gpus": 1}
    compile(code, "<toy-calibration-cuda-proof>", "exec")
