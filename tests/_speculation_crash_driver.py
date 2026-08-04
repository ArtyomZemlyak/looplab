"""Out-of-process driver for the "killed mid-evaluation" speculation repro.

Not collected by pytest (leading underscore): it is spawned by
``tests/test_card_budget_refund.py::test_crash_interrupted_speculative_eval_is_never_refunded``
and then SIGKILLed while a real sandbox is executing, which is the only way to produce the byte
prefix the refund predicate has to survive — a speculative node whose evaluation genuinely burned
compute and whose process died before it could write a terminal.

Usage: ``python -m tests._speculation_crash_driver <run_dir> <sentinel_path>``
The sentinel file is written by the sandboxed solution itself, so its appearance proves the
evaluation is really running (not merely dispatched) before the parent kills this process.
"""
from __future__ import annotations

import sys
from pathlib import Path

import anyio

import looplab.search.speculation_quality as speculation_quality
from looplab.adapters.toytask import ToyTask
from looplab.engine.orchestrator import SPECULATION_CALIBRATION_PROFILE_DIGEST
from looplab.search.speculation_calibration import (
    SPECULATION_CALIBRATION_SEEDS,
    speculation_runtime_scope_digest,
)
from looplab.core.config import Settings


def _install_unit_receipt() -> None:
    """The same public-boundary receipt stub the engine suite's autouse fixture installs."""
    task = ToyTask()

    def _validated(path):
        from looplab.engine.orchestrator import SPECULATION_CALIBRATION_PROFILE_SETTINGS
        max_nodes, depth = map(int, Path(path).stem.rsplit("-", 2)[-2:])
        runtime_scope = speculation_runtime_scope_digest({
            **SPECULATION_CALIBRATION_PROFILE_SETTINGS,
            "max_nodes": max_nodes,
            "speculation_depth": depth,
            "speculation_gate_receipt": str(path),
        })
        return {
            "self_digest": "sha256:" + "a" * 64,
            "implementation_digest": "sha256:" + "b" * 64,
            "require_gpu": True,
            "gpu_inventory": [{
                "index": 0,
                "uuid": "GPU-11111111-2222-3333-4444-555555555555",
                "pci_bus_id": "00000000:01:00.0",
                "name": "unit-gpu",
                "mem_total_mib": 24_576,
                "driver_version": "595.79",
                "cuda_driver_version": 13000,
            }],
            "policy_scope": "greedy",
            "admitted_depth": depth,
            "admitted_max_nodes": max_nodes,
            "runtime_scope_sha256": runtime_scope,
            "calibration_profile_digest": SPECULATION_CALIBRATION_PROFILE_DIGEST,
            "calibration_seeds": list(SPECULATION_CALIBRATION_SEEDS),
            "workload_scope": "quadratic_toy",
            "task_profile_sha256": speculation_quality.speculation_task_profile_digest(task),
        }

    speculation_quality.validated_speculation_gate_receipt = _validated
    Settings.model_config["env_file"] = None


def main(run_dir: str, sentinel: str) -> None:
    _install_unit_receipt()
    from tests.test_card_speculation_engine import (
        _add_ready_draft,
        _commit_speculative_node,
        _engine,
        _start,
    )

    engine, producer = _engine(Path(run_dir))
    # A solution that announces itself and then blocks: the sentinel proves the sandbox is executing
    # this node's code when the parent kills us, so the interrupted node really did burn compute.
    producer.code = (
        "import time, pathlib\n"
        f"pathlib.Path({sentinel!r}).write_text('running')\n"
        "time.sleep(600)\n"
        "print('metric: 0.0')\n"
    )
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    _add_ready_draft(engine, "card-2", x=0.8)
    node_id = _commit_speculative_node(engine)
    print(f"speculative_node={node_id}", flush=True)
    anyio.run(engine._evaluate, node_id, anyio.CapacityLimiter(1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
