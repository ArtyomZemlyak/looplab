"""A task may DECLARE its objective deterministic, and the engine records what follows (doc 75 UX-13).

The offline demo's contract said `uncertainty_protocol: "none: deterministic objective"`, and the
Report, the chat and the run list still told the user to repeat the selected experiment with more
seeds. "none" there means "no protocol declared", not "deterministic", so the declaration is an
explicit, optional contract field; and the consequence — `repeat_checks: "not_applicable"` — is
written by the engine onto the node's comparability record, beside the contract key it comes from,
so no reader has to re-open a `task.snapshot.json` sidecar to learn it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from looplab.cli import app
from looplab.core.comparison import ComparisonContract, canonical_comparison_contract
from looplab.engine.comparability import comparability_record

ROOT = Path(__file__).resolve().parents[1]


def _contract(**over):
    demo = yaml.safe_load((ROOT / "examples" / "demo.yaml").read_text(encoding="utf-8"))
    contract = dict(demo["task"]["comparison_contract"])
    contract.pop("deterministic", None)
    contract.update(over)
    return contract


def test_an_undeclared_contract_keeps_its_identity():
    """`exclude_none`: every contract written before the field keeps its `contract_id`."""
    plain = ComparisonContract.model_validate(_contract())
    assert "deterministic" not in plain.model_dump(by_alias=True, exclude_none=True)
    declared = ComparisonContract.model_validate(_contract(deterministic=True))
    assert declared.contract_id != plain.contract_id


def test_a_deterministic_contract_cannot_ask_for_a_confirmation():
    with pytest.raises(ValueError, match="deterministic"):
        ComparisonContract.model_validate(_contract(
            deterministic=True, measurement_phase="confirmed", uncertainty_protocol="3 seeds"))


@pytest.mark.parametrize("declared,expected", [(True, "not_applicable"), (None, None), (False, None)])
def test_the_record_carries_repeat_checks_only_for_a_declared_deterministic_objective(declared, expected):
    contract = canonical_comparison_contract(_contract(**({} if declared is None
                                                         else {"deterministic": declared})))
    record = comparability_record(task={"kind": "quadratic", "comparison_contract": contract})
    assert record is not None and record.get("repeat_checks") == expected
    assert "repeat_checks" not in record["keys"], "it decides no comparison"


def test_none_in_the_uncertainty_protocol_is_not_a_declaration():
    contract = canonical_comparison_contract(_contract(uncertainty_protocol="none: not measured"))
    record = comparability_record(task={"kind": "quadratic", "comparison_contract": contract})
    assert "repeat_checks" not in record


def test_the_demo_records_it_on_every_evaluated_node(tmp_path):
    run_dir = tmp_path / "demo"
    result = CliRunner().invoke(app, ["run", str(ROOT / "examples" / "demo.yaml"), "--out", str(run_dir)])
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    evaluated = [row for row in rows if row["type"] == "node_evaluated"]
    assert evaluated and all(
        row["data"]["metric_provenance"]["comparability"].get("repeat_checks") == "not_applicable"
        for row in evaluated)
