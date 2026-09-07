"""`expect.numeric` — the model-free stage contract (doc 52 row 24): a declared relation the ENGINE
evaluates against the last value the stage printed, held after exit 0 and before the next stage.

Driven the way the artifact half is driven in `tests/test_stage_contract.py`: through the real
`run_command_eval` over a real staged subprocess pipeline, so what has to hold is that the PIPELINE
stops, the failure is attributed to the right stage, no metric is read, and a passing relation lets
the next stage run — plus the parser's truth table and the rule that a numeric failure is never
salvaged.
"""
from __future__ import annotations

import json
import sys

import pytest

from looplab.engine.metric_salvage import salvage_condition
from looplab.runtime.command_eval import (NUMERIC_DECLARED_KEY, NUMERIC_VALUES_KEY, STAGE_EXPECT_KEYS,
                                          run_command_eval, validate_stages)
from looplab.runtime.numeric_contract import (MAX_STAGE_NUMERIC_RELATIONS, NUMERIC_OPS, last_values,
                                              numeric_contract_defects, validate_numeric)

_M = {"kind": "stdout_json", "key": "metric"}


# ------------------------------------------------------------------ the vocabulary and the parser

def test_the_expect_key_set_is_the_closed_triple():
    assert STAGE_EXPECT_KEYS == ("files", "assert", "numeric")
    assert NUMERIC_OPS == ("<", "<=", ">", ">=", "==", "!=")


@pytest.mark.parametrize("bad, needle", [
    ("params <= 2", "must be a list"),
    ([{"key": "params", "op": "~", "value": 1}], "op"),
    ([{"key": "params", "op": "<", "value": "1"}], "finite number"),
    ([{"key": "params", "op": "<", "value": float("inf")}], "finite number"),
    ([{"key": "params", "op": "<", "value": True}], "finite number"),
    ([{"key": "bad key!", "op": "<", "value": 1}], "key"),
    ([{"key": "params", "op": "<", "value": 1, "extra": 2}], "exactly"),
    ([{"key": "k", "op": "<", "value": 1}] * (MAX_STAGE_NUMERIC_RELATIONS + 1), "at most"),
])
def test_a_malformed_relation_is_refused_with_the_fix_named(bad, needle):
    clean, err = validate_numeric("train", bad)
    assert clean is None and needle in err, err


def test_a_well_formed_relation_is_canonical_and_survives_validate_stages():
    clean, err = validate_numeric("train", [{"key": " params ", "op": "<=", "value": 2000000}])
    assert err is None and clean == [{"key": "params", "op": "<=", "value": 2000000.0}]
    stages, err = validate_stages([{"name": "train", "command": ["python", "t.py"], "timeout": 5,
                                    "expect": {"numeric": [{"key": "params", "op": "<=", "value": 2e6}]}}])
    assert err is None and stages[0]["expect"] == {"numeric": clean}
    _, err = validate_stages([{"name": "train", "command": ["python", "t.py"], "timeout": 5,
                               "expect": {"numeric": []}}])
    assert err is not None and "declares nothing" in err


def test_the_last_printed_value_wins_in_every_spelling_and_a_nonfinite_one_is_not_a_value():
    text = ("epoch 1 val_loss: 0.9  val_loss_epoch: 5\n"
            "epoch 2 val_loss=0.4 params=3e6\n"
            '{"metric": 0.71, "VAL_NDCG": 0.72}\n'
            "'params': 3000000 grad_norm: inf\n")
    got = last_values(text, ["val_loss", "params", "val_ndcg", "grad_norm", "missing"])
    assert got == {"val_loss": 0.4, "params": 3000000.0, "val_ndcg": 0.72}
    assert last_values("", ["x"]) == {}


def test_defects_name_the_bound_and_the_value_and_an_unprinted_key_is_a_defect():
    defects, values = numeric_contract_defects(
        "params=3000000\nval_ndcg: 0.75\n",
        [{"key": "params", "op": "<=", "value": 2e6}, {"key": "val_ndcg", "op": ">=", "value": 0.71},
         {"key": "recall", "op": ">", "value": 0.5}])
    assert defects == ["params <= 2e+06 — the stage printed params = 3e+06",
                       "recall > 0.5 — the stage never printed 'recall'"]
    assert values == {"params": 3000000.0, "val_ndcg": 0.75}
    assert numeric_contract_defects("x = 1", [{"key": "x", "op": "==", "value": 1}])[0] == []
    assert numeric_contract_defects("x = 1", [{"key": "x", "op": "!=", "value": 1}])[0]


# ------------------------------------------------------------------ the pipeline, driven

def _pipeline(tmp_path, prints: str, relations, *, key="params"):
    (tmp_path / "train.py").write_text(prints, encoding="utf-8")
    (tmp_path / "score.py").write_text('import json; print(json.dumps({"metric": 0.5}))\n', encoding="utf-8")
    # two stages, like `test_stage_contract._prep_stage`: the contract sits on `train`, and whether
    # `score` runs at all is the pipeline property under test
    return [{"name": "train", "command": [sys.executable, "train.py"], "timeout": 30,
             "expect": {"numeric": relations}},
            {"name": "score", "command": [sys.executable, "score.py"], "timeout": 30}]


def test_a_stage_that_prints_past_its_declared_bound_fails_the_pipeline(tmp_path):
    stages = _pipeline(tmp_path, "print('trained'); print('params: 3000000')\n",
                       [{"key": "params", "op": "<=", "value": 2000000}])
    res = run_command_eval([sys.executable, "score.py"], str(tmp_path), 30, _M, stages=stages)
    assert res.failed_stage == "train" and res.metric is None
    assert [s["name"] for s in res.stages] == ["train"], "the score stage ran on a refused train"
    row = res.stages[0]
    assert row["status"] == "expect_failed"
    assert "params <= 2e+06" in res.stderr and "3e+06" in res.stderr
    assert row[NUMERIC_DECLARED_KEY] == [{"key": "params", "op": "<=", "value": 2000000.0}]
    assert row[NUMERIC_VALUES_KEY] == {"params": 3000000.0}
    assert "do not delete the declaration" in row["concern"]


def test_a_stage_that_never_prints_the_key_fails_closed(tmp_path):
    stages = _pipeline(tmp_path, "print('trained quietly')\n", [{"key": "params", "op": "<=", "value": 2e6}])
    res = run_command_eval([sys.executable, "score.py"], str(tmp_path), 30, _M, stages=stages)
    assert res.failed_stage == "train" and "never printed 'params'" in res.stderr


def test_a_stage_inside_its_bound_passes_and_the_pipeline_continues(tmp_path):
    stages = _pipeline(tmp_path, "print('params=1500000'); print('val_ndcg: 0.73')\n",
                       [{"key": "params", "op": "<=", "value": 2e6}, {"key": "val_ndcg", "op": ">=", "value": 0.71}])
    res = run_command_eval([sys.executable, "score.py"], str(tmp_path), 30, _M, stages=stages)
    assert res.failed_stage is None and res.metric == 0.5
    assert [s["name"] for s in res.stages] == ["train", "score"]
    row = res.stages[0]
    assert row["status"] == "ok" and row[NUMERIC_VALUES_KEY] == {"params": 1500000.0, "val_ndcg": 0.73}, (
        "the values read are on the row on a PASS too")


def test_a_numeric_failure_is_never_salvaged_while_an_artifact_failure_still_is():
    class _Res:
        """The eval result `salvage_condition` reads: a failed last stage that exited 0."""
        def __init__(self, stages):
            self.stages, self.failed_stage, self.exit_code = stages, "train", 0
            self.timed_out, self.stalled, self.diverged, self.metric = False, False, False, None
            self.stdout, self.stderr = "", ""

    numeric_row = [{"name": "train", "status": "expect_failed", NUMERIC_DECLARED_KEY: [{"key": "p", "op": "<", "value": 1}]}]
    artifact_row = [{"name": "train", "status": "expect_failed", "expect_declared": ["ckpt.pt"]}]
    assert salvage_condition(_Res(numeric_row), "expect_failed") is None
    assert salvage_condition(_Res(artifact_row), "expect_failed") == "artifact_contract"
