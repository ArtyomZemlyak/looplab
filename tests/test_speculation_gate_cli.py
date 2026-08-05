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

    gate_calls = []

    def _gate(pairs, **kwargs):
        gate_calls.append((list(pairs), kwargs))
        observed["gate"] = (pairs, kwargs)
        return report

    def _publish(path, body):
        observed["publish"] = (Path(path), body)
        return receipt

    monkeypatch.setattr(quality, "speculation_quality_gate", _gate)
    monkeypatch.setattr(quality, "publish_speculation_gate_receipt", _publish)
    # The whole point of the split (doc 25 SE-01): the CLI must PUBLISH the body it already
    # computed. Re-entering `write_speculation_gate_receipt` would run the entire gate a second
    # time — six run directories re-parsed, the scorer matrix re-executed — so reaching for it here
    # is a hard failure rather than a silent doubling.
    monkeypatch.setattr(
        quality,
        "write_speculation_gate_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("the CLI must not recompute the gate to publish it")),
    )
    run_dirs = [tmp_path / f"run-{index}" for index in range(6)]
    result = CliRunner().invoke(app, [
        "speculation-gate",
        *map(str, run_dirs),
        "--output", str(output),
    ])

    assert result.exit_code == 0, result.output
    assert observed["gate"][0] == list(zip(run_dirs[0::2], run_dirs[1::2]))
    assert observed["gate"][1] == {"require_gpu": True}
    assert len(gate_calls) == 1, f"the gate ran {len(gate_calls)} times, not once"
    assert observed["publish"][0] == output
    # The published body is the SAME object the gate returned — not a re-derivation of it.
    assert observed["publish"][1] is report
    assert receipt["self_digest"] in result.output
    assert json.loads(result.output)["receipt"] == str(output.resolve())
    assert '"calibration_seeds"' in result.output
    assert '"admitted_max_nodes": 8' in result.output
    assert receipt["runtime_scope_sha256"] in result.output


def test_the_publisher_still_refuses_a_failing_body(tmp_path):
    """`publish_speculation_gate_receipt` carries the contract it was split out of: a body that did
    not pass is never written, whoever computed it."""
    output = tmp_path / "receipt.json"
    with pytest.raises(ValueError, match="did not pass"):
        quality.publish_speculation_gate_receipt(output, {"passed": False, "errors": ["nope"]})
    assert not output.exists()
