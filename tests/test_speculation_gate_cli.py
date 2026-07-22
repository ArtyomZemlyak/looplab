from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from looplab.cli import app
import looplab.search.speculation_quality as quality


@pytest.mark.parametrize("run_count", (5, 8))
def test_speculation_gate_cli_requires_exactly_three_complete_pairs(
    tmp_path, monkeypatch, run_count,
):
    monkeypatch.setattr(
        quality,
        "speculation_quality_gate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    result = CliRunner().invoke(app, [
        "speculation-gate",
        *[str(tmp_path / f"run-{index}") for index in range(run_count)],
    ])
    assert result.exit_code == 2
    assert "exactly three" in result.output


def test_speculation_gate_cli_reports_failure_without_writing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(quality, "speculation_quality_gate", lambda pairs, **kwargs: {
        "passed": False,
        "errors": ["quality threshold failed"],
        "pairs": len(pairs),
    })
    monkeypatch.setattr(
        quality,
        "write_speculation_gate_receipt",
        lambda *_args, **_kwargs: calls.append(True),
    )
    result = CliRunner().invoke(app, [
        "speculation-gate",
        *[str(tmp_path / f"run-{index}") for index in range(6)],
        "--output", str(tmp_path / "receipt.json"),
    ])
    assert result.exit_code == 2
    assert "quality threshold failed" in result.output
    assert calls == []


def test_speculation_gate_cli_writes_passing_receipt(tmp_path, monkeypatch):
    output = tmp_path / "receipt.json"
    observed = {}
    report = {
        "passed": True,
        "aggregates": {"pair_count": 3, "mean_hit_rate": 1.0},
    }
    receipt = {
        **report,
        "self_digest": "sha256:" + "a" * 64,
        "implementation_digest": "sha256:" + "b" * 64,
        "environment_sha256": "sha256:" + "c" * 64,
        "gpu_inventory": [{
            "index": 0,
            "uuid": "GPU-11111111-2222-3333-4444-555555555555",
            "pci_bus_id": "00000000:01:00.0",
            "name": "Synthetic GPU",
            "mem_total_mib": 24_576,
            "driver_version": "595.79",
            "cuda_driver_version": 13000,
        }],
        "policy_scope": "greedy",
        "workload_scope": "quadratic_toy",
        "calibration_seeds": [0, 1, 2],
        "task_profile_sha256": "sha256:" + "d" * 64,
        "admitted_depth": 1,
        "admitted_max_nodes": 8,
        "runtime_scope_sha256": "sha256:" + "e" * 64,
        "calibration_profile_digest": "sha256:" + "f" * 64,
    }

    def _gate(pairs, **kwargs):
        observed["gate"] = (pairs, kwargs)
        return report

    def _write(path, pairs, **kwargs):
        observed["write"] = (Path(path), pairs, kwargs)
        return receipt

    monkeypatch.setattr(quality, "speculation_quality_gate", _gate)
    monkeypatch.setattr(quality, "write_speculation_gate_receipt", _write)
    run_dirs = [tmp_path / f"run-{index}" for index in range(6)]
    result = CliRunner().invoke(app, [
        "speculation-gate",
        *map(str, run_dirs),
        "--output", str(output),
    ])

    assert result.exit_code == 0, result.output
    assert observed["gate"][1] == {"require_gpu": True}
    assert observed["write"][0] == output
    assert observed["write"][1] == list(zip(run_dirs[0::2], run_dirs[1::2]))
    assert observed["write"][2] == {"require_gpu": True}
    assert receipt["self_digest"] in result.output
    assert json.loads(result.output)["receipt"] == str(output.resolve())
    assert '"calibration_seeds"' in result.output
    assert '"admitted_max_nodes": 8' in result.output
    assert receipt["runtime_scope_sha256"] in result.output
