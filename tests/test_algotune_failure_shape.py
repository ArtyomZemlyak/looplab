"""`evaluator_error` covers two failures that need opposite answers, and the reason cannot say which.

Measured 2026-08-29 on two live probes, both printing `reason: evaluator_error` and the same
"Unexpected results format from evaluate_code_on_dataset" verdict:

  * dsDL node_0  — eval_seconds 504.0, timeouts: one instance hit the 120 s ceiling and AlgoTune's
    early-exit failed every remaining run. The solver WORKS and is too slow.
  * dsRBF node_0 — eval_seconds 35.6, `error_type: execution_error`, `num_errors: 3`,
    `num_timeouts: 0`: the solver RAISED its own `LinAlgError("Singular matrix in RBF solve.")`.
    Nothing was slow; the code is wrong.

"Make it faster" and "make it correct" are opposite instructions, and the model was given the same
word for both. The discriminating fields were already in the harness payload and simply not carried
out of it.

The refuter is `test_the_two_shapes_are_distinguishable`: drop `failure_shape` from the block (or
return {} from it) and the two payloads become indistinguishable.
"""
import importlib.util
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "looplab_eval.py"

# Verbatim tails from the two probes, trimmed to the fields that matter.
_RBF = ("...raise np.linalg.LinAlgError(\"Singular matrix in RBF solve.\")..."
        "'timeout_occurred': False, 'error_type': 'execution_error', 'runs': 3, "
        "'num_errors': 3, 'num_timeouts': 0}")
_DL = ("[isolated_benchmark] Run 1/3 timed out after 120.0s "
       "'timeout_occurred': True, 'error_type': 'timeout', 'runs': 3, "
       "'num_errors': 0, 'num_timeouts': 3}")


@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location("looplab_eval_shape", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_two_shapes_are_distinguishable(bridge):
    rbf = bridge.failure_shape(_RBF)
    dl = bridge.failure_shape(_DL)
    assert rbf != dl, "the whole point is that these two must not read the same"
    assert rbf["num_timeouts"] == 0 and rbf["num_errors"] == 3
    assert dl["num_timeouts"] == 3 and dl["num_errors"] == 0


def test_the_error_type_is_carried_verbatim(bridge):
    assert bridge.failure_shape(_RBF)["error_type"] == "execution_error"
    assert bridge.failure_shape(_DL)["error_type"] == "timeout"


def test_a_stderr_with_none_of_it_yields_nothing(bridge):
    # An empty dict, not a block of zeros: absent evidence must not read as "zero timeouts".
    assert bridge.failure_shape("some unrelated traceback") == {}
    assert bridge.failure_shape("") == {}


def test_the_block_carries_the_shape(bridge):
    block = bridge._no_speedup("evaluator_error", stderr=_RBF)
    assert block["reason"] == "evaluator_error"
    assert block["failure_shape"]["error_type"] == "execution_error", (
        "the reason word alone cannot separate 'too slow' from 'wrong'")


def test_the_vocabulary_is_untouched(bridge):
    # The registry rule: a new reason is a bigger change than surfacing evidence. This fix adds no
    # word, and `test_algotune_bridge_says_why.py` derives the emitted set from the AST.
    assert "failure_shape" not in bridge.NO_SPEEDUP_REASONS
    assert "execution_error" not in bridge.NO_SPEEDUP_REASONS


def test_the_first_occurrence_wins(bridge):
    # A stderr tail can hold several payloads; the FIRST is the one that stopped the evaluation.
    doubled = _RBF + " " + _DL
    assert bridge.failure_shape(doubled)["num_timeouts"] == 0
