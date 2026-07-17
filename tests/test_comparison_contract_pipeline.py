"""Typed launch provenance and phase-authoritative scope measurements."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.adapters.tasks import validate_task  # noqa: E402
from looplab.cli import app  # noqa: E402
from looplab.core.comparison import (  # noqa: E402
    ComparisonContract,
    canonical_comparison_contract,
    comparison_measurement,
)
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


def _contract(phase: str = "search", *, direction: str = "min") -> dict:
    return {
        "schema": 1,
        "dataset_lineage": "dataset:v1",
        "split_or_candidate_pool_lineage": "validation:v1",
        "evaluator_uid": "eval.objective",
        "evaluator_version": "1",
        "population": "all candidates",
        "filter": "feasible only",
        "metric_uid": "objective",
        "unit": "loss",
        "direction": direction,
        "aggregation": "mean",
        "cutoff": "none",
        "measurement_phase": phase,
        "uncertainty_protocol": "mean_std" if phase == "confirmed" else "none",
        "constraints_digest": "sha256:none",
    }


def _toy(phase: str = "search") -> dict:
    return {
        "kind": "quadratic",
        "id": "comparison-toy",
        "goal": "minimize objective",
        "direction": "min",
        "bounds": {"x": [-2.0, 2.0]},
        "comparison_contract": _contract(phase),
    }


def test_contract_phase_is_strict_and_shared_task_validation_binds_direction():
    canonical = canonical_comparison_contract(_contract("confirmed"))
    assert canonical is not None
    assert canonical["measurement_phase"] == "confirmed"
    assert len(canonical["contract_id"]) == 64
    assert canonical_comparison_contract(_contract("validation")) is None
    assert canonical_comparison_contract({**_contract(), "schema": True}) is None
    assert canonical_comparison_contract({
        key: value for key, value in _contract().items() if key != "schema"
    }) is None
    assert canonical_comparison_contract({
        **_contract("confirmed"), "uncertainty_protocol": "none",
    }) is None

    task = validate_task(_toy("holdout"))
    dumped = task.model_dump(mode="json")
    assert dumped["comparison_contract"]["measurement_phase"] == "holdout"
    assert dumped["comparison_contract"]["contract_id"]
    with pytest.raises(ValueError, match="must match the task direction"):
        validate_task({**_toy(), "comparison_contract": _contract(direction="max")})


def test_typed_contract_cannot_reuse_a_digest_after_semantic_mutation():
    contract = ComparisonContract.model_validate(_contract())
    original_id = contract.contract_id
    with pytest.raises(Exception):
        contract.metric_uid = "different"  # type: ignore[misc]

    # CODEX AGENT: a hostile caller can bypass any Python frozen model with object.__setattr__;
    # canonicalization must still re-bind semantics instead of trusting the instance marker.
    object.__setattr__(contract, "metric_uid", "different")
    assert contract.contract_id == original_id
    assert canonical_comparison_contract(contract) is None


@pytest.mark.parametrize("source", ["inline", "file"])
def test_shared_web_tui_api_preflight_returns_canonical_contract(tmp_path, source):
    client = TestClient(make_app(tmp_path))
    request = {"run_id": f"contract-{source}"}
    if source == "inline":
        request["task"] = _toy("confirmed")
    else:
        task_file = tmp_path / "comparison-task.json"
        task_file.write_text(json.dumps(_toy("confirmed")), encoding="utf-8")
        request["task_file"] = str(task_file)

    response = client.post("/api/start/preflight", json=request)

    assert response.status_code == 200, response.text
    contract = response.json()["preview"]["task"]["comparison_contract"]
    assert contract == canonical_comparison_contract(_contract("confirmed"))


def test_cli_snapshot_persists_adapter_canonical_contract(tmp_path):
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(_toy()), encoding="utf-8")
    run_dir = tmp_path / "run"

    result = CliRunner().invoke(
        app, ["run", str(task_file), "--out", str(run_dir), "--max-nodes", "1"])

    assert result.exit_code == 0, result.output
    snapshot = json.loads((run_dir / "task.snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["comparison_contract"] == canonical_comparison_contract(_contract())


@pytest.mark.parametrize(
    ("phase", "expected_value", "expected_source"),
    [
        ("search", 7.0, "best.metric"),
        ("confirmed", 5.0, "best.confirmed_mean"),
        ("holdout", 3.0, "best.holdout_metric"),
    ],
)
def test_scope_brief_maps_contract_phase_to_authoritative_evidence(
        tmp_path, monkeypatch, phase, expected_value, expected_source):
    run_dir = tmp_path / "measured"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {
        "run_id": "measured", "task_id": "comparison-task", "goal": "g",
        "direction": "min", "holdout_select": True,
    })
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": ""},
    })
    store.append("node_evaluated", {"node_id": 0, "metric": 7.0})
    store.append("node_confirmed", {
        "node_id": 0, "mean": 5.0, "std": 0.25, "seeds": 3,
    })
    store.append("holdout_evaluated", {
        "node_id": 0, "metric": 3.0, "search_epoch": 0,
    })
    (run_dir / "task.snapshot.json").write_text(
        json.dumps(_toy(phase)), encoding="utf-8")
    captured: list[dict] = []

    def capture(_scope, briefs, _client, **_kwargs):
        captured.extend(briefs)
        return {"headline": "captured"}

    monkeypatch.setattr("looplab.serve.scope_report.generate_scope_report", capture)
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda _settings: object())
    response = TestClient(make_app(tmp_path)).post(
        "/api/scope-report/task/comparison-task/generate",
        headers={"Idempotency-Key": "12345678-1234-4234-9234-123456789abc"})

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    receipt = captured[0]["comparison_measurement"]
    assert receipt["authority"] == "declared"
    assert receipt["value"] == expected_value
    assert receipt["phase"] == phase
    assert receipt["source"] == expected_source
    assert receipt["uncertainty"]["protocol"] == _contract(phase)["uncertainty_protocol"]
    assert captured[0]["best_metric"] == expected_value
    if phase == "confirmed":
        assert receipt["uncertainty"] == {
            "protocol": "mean_std",
            "std": 0.25,
            "std_source": "best.confirmed_std",
            "seeds": 3,
            "seeds_source": "best.confirmed_seeds",
        }


@pytest.mark.parametrize(
    ("phase", "field", "bad"),
    [
        ("search", "metric", float("nan")),
        ("confirmed", "confirmed_mean", None),
        ("confirmed", "confirmed_std", float("inf")),
        ("holdout", "holdout_metric", None),
    ],
)
def test_phase_measurement_rejects_missing_or_nonfinite_evidence(phase, field, bad):
    best = SimpleNamespace(
        metric=7.0,
        confirmed_mean=5.0,
        confirmed_std=0.25,
        confirmed_seeds=3,
        holdout_metric=3.0,
    )
    setattr(best, field, bad)

    assert comparison_measurement(_contract(phase), best) is None
