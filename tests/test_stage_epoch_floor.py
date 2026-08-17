"""The DECLARED-EPOCH FLOOR under `declared_condition_violated` (doc 36 / doc 39 site #1).

What is being held, in one sentence: **a stage whose trainer wrote its own end-of-training summary
inside the last DECLARED epoch may not be killed for not having reached that epoch — and a stage
whose schedule was genuinely shrunk still is.**

Why it is worth a file. `declared_condition_violated` is the only member of
`STAGE_CHECK_HARD_KINDS` that is not a claim about MECHANISM, and it is the only one whose evidence
is a number the trainer prints about itself. That made it unstable in the most expensive way: on
`runs/rubertlite-dr-unified-v9` node 0 the same `train` stage, re-run, drew two different verdicts —

    attempt A  {'train_runtime': 5137.504,  …, 'epoch': 14.87}  ->  FAIL declared_condition_violated
    attempt B  {'train_runtime': 5128.1169, …, 'epoch': 14.87}  ->  OK

— and the refusal bought a full re-train: **8,399.9 stage seconds, 2.33 GPU-h**, for a result that
came back the same shape. Both prompts are in this file's fixture, verbatim, with the model's own
replies.

THE FIXTURES ARE THE REAL CORPUS. `tests/fixtures/stage_epoch_floor_corpus.json` holds the exact
`stdout` tails and the exact declared conditions those five live checker calls were made on, lifted
out of each run's `spans.jsonl`, together with the reply the model actually returned. Three of them
are the regression fixtures the fix must NOT move: v8 node 8 (a repair cut `n_epochs` 15 -> 8 and
triage then abandoned the node rather than score a shrunken run), v8 node 9 (6 of a declared 10) and
v9 node 1 (1 of a declared 50).

WHAT THIS FILE DELIBERATELY DOES NOT DO is pin source text. Every property below is driven through a
real `run_command_eval` over a real subprocess that really emits those bytes, because the thing that
must not regress is whether the NEXT STAGE RAN and whether the node kept its metric.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from looplab.engine.eval_stages import parse_stage_check_reply
from looplab.runtime.command_eval import (DECLARED_EPOCH_TOLERANCE, EPOCH_COMPLETION_REACHED,
                                          EPOCH_COMPLETION_SHORT, EPOCH_COMPLETION_UNKNOWN,
                                          STAGE_CHECK_EPOCH_VETO_KEY, STAGE_CHECK_HARD_KINDS,
                                          STAGE_CHECK_INCONCLUSIVE, STAGE_CHECK_INCONCLUSIVE_KEY,
                                          declared_epoch_completion, declared_epoch_target,
                                          epoch_floor_acquits, run_command_eval,
                                          training_summary_epoch)

_M = {"kind": "stdout_json", "key": "metric"}
_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "stage_epoch_floor_corpus.json").read_text("utf-8"))


def _case(run: str, node: int, verdict: str) -> dict:
    """One live checker call from the corpus, by (run, node, what the model answered)."""
    for row in _CORPUS:
        if row["run"] == run and row["node_id"] == node \
                and row["model_reply"].strip().upper().startswith(verdict):
            return row
    raise AssertionError(f"fixture missing: {run} node {node} {verdict}")


# The five calls, named. `V9_N0_REFUSED` and `V9_N0_PASSED` are the same stage re-run: the whole
# defect in two rows.
V9_N0_REFUSED = _case("rubertlite-dr-unified-v9", 0, "FAIL")
V9_N0_PASSED = _case("rubertlite-dr-unified-v9", 0, "OK")
V9_N1_SHRUNK = _case("rubertlite-dr-unified-v9", 1, "FAIL")
V8_N8_SHRUNK = _case("rubertlite-dr-unified-v8", 8, "FAIL")
V8_N9_SHRUNK = _case("rubertlite-dr-unified-v8", 9, "FAIL")
GENUINE_SHRINKAGES = [V9_N1_SHRUNK, V8_N8_SHRUNK, V8_N9_SHRUNK]


# --------------------------------------------------------------------------------------------
# The corpus, re-derived: the two attempts really were the same evidence
# --------------------------------------------------------------------------------------------

def test_the_two_v9_node0_attempts_differ_only_in_training_noise():
    """The premise the whole fix rests on, checked against the bytes rather than assumed.

    If the INPUT had differed materially, the non-determinism would be in what the checker was fed
    and the remedy would be a different one (canonicalize the input). It did not: the two tails are
    not byte-identical — two runs of the same training never are — but every field that bears on
    "did the declared epochs run" is the same in both, and the model still answered FAIL and OK."""
    a, b = V9_N0_REFUSED["stdout_tail"], V9_N0_PASSED["stdout_tail"]
    assert a != b, "two independent trainings would not be bit-identical; that is the point"
    assert V9_N0_REFUSED["declared_condition"] == V9_N0_PASSED["declared_condition"]
    # The salient reading is identical, so nothing in the difference is evidence about the claim.
    assert training_summary_epoch(a) == training_summary_epoch(b) == 14.87
    assert declared_epoch_target(V9_N0_REFUSED["declared_condition"]) == 15
    # …and the verdicts are opposite.
    assert V9_N0_REFUSED["model_reply"].startswith("FAIL declared_condition_violated")
    assert V9_N0_PASSED["model_reply"].startswith("OK")


def test_the_progress_bar_is_not_even_in_the_evidence():
    """Why "did the step counter reach its total?" is not the deterministic pre-answer, part one.

    The bar the operator's own framing quotes (`1695/1695`) is written to STDERR; the checker is
    handed `run.out`. It is in none of the five prompts, so a rung keyed on it would be reading a
    number this path never captured."""
    for row in _CORPUS:
        assert "1695/1695" not in row["stdout_tail"]
        assert "%|" not in row["stdout_tail"]


def test_every_genuine_shrinkage_also_completed_its_own_schedule():
    """Why the step counter is not the answer, part two — and this is the measurement that shapes
    the rule.

    Each of the three genuine shrinkages had its SCHEDULE cut and then ran that schedule to the end:
    v8 node 8 reached `11232/11232`, v8 node 9 `4236/4236`, v9 node 1 `5652/5652`, each writing its
    own `train_runtime` summary. "The counter reached its total" is TRUE of all of them, so a
    pre-answer built on it would have acquitted exactly the node this rung exists to refuse. What
    separates them is the trainer's own final EPOCH against the DECLARED one, and nothing else."""
    for row in GENUINE_SHRINKAGES:
        assert training_summary_epoch(row["stdout_tail"]) is not None, \
            "the trainer finished the schedule it was given"
        state, target, observed = declared_epoch_completion(row["declared_condition"],
                                                            row["stdout_tail"])
        assert state == EPOCH_COMPLETION_SHORT
        assert target - observed >= 4.0, "the corpus separation is whole epochs, not a rounding"


# --------------------------------------------------------------------------------------------
# The rule, as a truth table
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sentence,target", [
    ("all 15 training epochs completed and the final model saved to x/final", 15),
    ("all 50 epochs of DCL+R-Drop nll_cos training completed and the model saved", 50),
    ("all 10 epochs completed with mined negatives disabled (train.negatives=None)", 10),
    # No single target -> no opinion.
    ("all training epochs completed and the final model checkpoint saved", None),
    ("all 15 epochs completed, then all 3 fine-tuning epochs", None),
    ("the model trained for 15 epochs", None),
    ("hard negatives mined for at least 90% of training queries", None),
    ("all 0 epochs completed", None),
    ("", None),
])
def test_declared_epoch_target_reads_a_declaration_and_never_prose(sentence, target):
    assert declared_epoch_target(sentence) == target


def test_the_summary_epoch_is_read_from_the_summary_and_not_from_the_last_line():
    """A script that trains TWICE and dies inside the second training still has the FIRST
    training's `train_runtime` in the window. Binding the epoch to the summary record — rather than
    asking separately for "the last epoch" and "is there a summary" — is what stops that pairing."""
    two = ("{'train_runtime': 100.0, 'epoch': 15.0}\n"
           "{'loss': 3.0, 'epoch': 0.4}\n"
           "{'loss': 2.9, 'epoch': 0.8}\n")
    assert training_summary_epoch(two) == 15.0, "the last SUMMARY, not the last epoch"
    killed = "{'loss': 3.0, 'epoch': 14.9}\n"
    assert training_summary_epoch(killed) is None, "no summary means the trainer did not finish"
    assert declared_epoch_completion("all 15 epochs completed", killed)[0] == EPOCH_COMPLETION_UNKNOWN


@pytest.mark.parametrize("observed,state", [
    (15.0, EPOCH_COMPLETION_REACHED),
    (14.87, EPOCH_COMPLETION_REACHED),          # v9 node 0, the false refusal
    (14.01, EPOCH_COMPLETION_REACHED),
    (14.0, EPOCH_COMPLETION_SHORT),             # exactly one whole epoch short
    (7.99, EPOCH_COMPLETION_SHORT),             # v8 node 8, against a declared 15
    (1.0, EPOCH_COMPLETION_SHORT),
])
def test_the_boundary_is_one_whole_epoch(observed, state):
    """A trainer that completed N epochs reports a final epoch in (N-1, N] — the fraction is where
    the step budget ran out inside the last one. Measured over all 42 declared-condition checks in
    `runs/`: 18 land inside the last declared epoch with a shortfall of 0.00-0.13, and 4 fall short
    by 4.01-49.00. The boundary sits in that gap and is not tuned to it."""
    assert DECLARED_EPOCH_TOLERANCE == 1.0
    text = "{'train_runtime': 500.0, 'train_loss': 1.0, 'epoch': %s}" % observed
    assert declared_epoch_completion("all 15 epochs completed", text)[0] == state


def test_the_veto_reaches_exactly_one_kind_and_only_behind_a_verified_artifact_contract():
    """The four conjuncts, each as its own row. The five PHYSICAL kinds must be untouchable: they
    are claims about mechanism that no epoch reading contradicts."""
    cond, tail = V9_N0_REFUSED["declared_condition"], V9_N0_REFUSED["stdout_tail"]
    assert epoch_floor_acquits("declared_condition_violated", cond, tail,
                               artifact_contract=True)[0] is True
    for kind in STAGE_CHECK_HARD_KINDS:
        if kind == "declared_condition_violated":
            continue
        assert epoch_floor_acquits(kind, cond, tail, artifact_contract=True)[0] is False, kind
    # No `expect.files` means the artifact half of the declaration was never checked on disk, so
    # there is nothing to rescue the non-epoch clause and the refusal stands.
    assert epoch_floor_acquits("declared_condition_violated", cond, tail,
                               artifact_contract=False)[0] is False


@pytest.mark.parametrize("row", GENUINE_SHRINKAGES,
                         ids=lambda r: f"{r['run'][-2:]}-node{r['node_id']}")
def test_a_genuine_shrinkage_is_never_acquitted(row):
    """HOLD THE LINE. These three are the whole reason the rung earns its keep."""
    assert epoch_floor_acquits("declared_condition_violated", row["declared_condition"],
                               row["stdout_tail"], artifact_contract=True) == (False, "")


# --------------------------------------------------------------------------------------------
# The effect, driven through a real pipeline over the real logs
# --------------------------------------------------------------------------------------------

def _pipeline(tmp_path: Path, row: dict):
    """A real two-stage pipeline whose `train` stage really emits that node's real stdout tail and
    really writes the artifact its `expect.files` declares, followed by a `score` stage that prints
    the metric. What the assertions read is whether `score` RAN."""
    (tmp_path / "tail.txt").write_text(row["stdout_tail"], encoding="utf-8")
    (tmp_path / "train.py").write_text(
        "import os, pathlib, sys\n"
        "os.makedirs('final', exist_ok=True)\n"
        "pathlib.Path('final/model.safetensors').write_bytes(b'weights')\n"
        "sys.stdout.write(pathlib.Path('tail.txt').read_text(encoding='utf-8'))\n",
        encoding="utf-8")
    (tmp_path / "score.py").write_text(
        "import json; open('scored','w').write('1'); print(json.dumps({'metric': 0.697699}))\n",
        encoding="utf-8")
    return [{"name": "train", "command": [sys.executable, "train.py"], "check": True,
             "expect": {"files": ["final/model.safetensors"],
                        "assert": row["declared_condition"]}},
            {"name": "score", "command": [sys.executable, "score.py"]}]


def _run(tmp_path: Path, row: dict):
    """Drive it with the model's REAL reply, parsed by the shipped parser."""
    def check_fn(stage_name, tail, expect: str = ""):
        return parse_stage_check_reply(row["model_reply"], declared=bool(expect))

    return run_command_eval([sys.executable, "score.py"], str(tmp_path), 120, _M,
                            stages=_pipeline(tmp_path, row), check_fn=check_fn)


def test_the_false_refusal_no_longer_costs_a_retrain(tmp_path):
    """THE PROPERTY. v9 node 0's real log, the model's real `FAIL declared_condition_violated`, and
    the pipeline goes on to score — which is what the 8,399.9 s re-train bought and did not get."""
    res = _run(tmp_path, V9_N0_REFUSED)
    assert (tmp_path / "scored").exists(), "the score stage never ran"
    assert res.failed_stage is None
    assert res.metric == pytest.approx(0.697699)
    train = res.stages[0]
    assert train["status"] == "ok"
    # BOTH readings are on the row. The model's doubt is kept (it still vetoes salvage), and the
    # engine's contradiction is recorded beside it under a key of its own.
    assert train[STAGE_CHECK_INCONCLUSIVE_KEY], "the model's answer was dropped, not overridden"
    assert "14.87" in train[STAGE_CHECK_EPOCH_VETO_KEY]
    assert "15 epochs" in train[STAGE_CHECK_EPOCH_VETO_KEY]
    assert "concern" not in train, "a stage that did not fail must not carry a failure reason"


def test_the_attempt_that_already_passed_is_byte_for_byte_unchanged(tmp_path):
    """The other half of the same stage. An `OK` never reaches the veto, so its row gains nothing —
    no new key, no doubt, no change."""
    res = _run(tmp_path, V9_N0_PASSED)
    assert (tmp_path / "scored").exists()
    assert res.stages[0]["status"] == "ok"
    assert STAGE_CHECK_EPOCH_VETO_KEY not in res.stages[0]
    assert STAGE_CHECK_INCONCLUSIVE_KEY not in res.stages[0]


@pytest.mark.parametrize("row", GENUINE_SHRINKAGES,
                         ids=lambda r: f"{r['run'][-2:]}-node{r['node_id']}")
def test_a_shrunken_experiment_is_still_refused_end_to_end(tmp_path, row):
    """HOLD THE LINE, driven. v8 node 8 is the case where triage ABANDONED the node instead of
    scoring a shrunken run; if this test ever goes green in the other direction, the fix is worse
    than the defect it removes."""
    res = _run(tmp_path, row)
    assert not (tmp_path / "scored").exists(), "a shrunken experiment reached the scorer"
    assert res.failed_stage == "train"
    assert res.metric is None
    train = res.stages[0]
    assert train["status"] == "check_failed"
    assert "declared_condition_violated" in train["concern"]
    assert STAGE_CHECK_EPOCH_VETO_KEY not in train


def test_a_physical_failure_on_the_same_log_still_stops_the_pipeline(tmp_path):
    """The veto is scoped to one kind, driven rather than asserted about the function. Same real
    log, same real declaration, a NaN verdict instead — and the stage still dies."""
    row = dict(V9_N0_REFUSED, model_reply="FAIL nan_or_inf_loss: loss=nan from step 40 onward")
    res = _run(tmp_path, row)
    assert not (tmp_path / "scored").exists()
    assert res.stages[0]["status"] == "check_failed"
    assert "nan_or_inf_loss" in res.stages[0]["concern"]


def test_a_declaration_the_engine_cannot_read_leaves_the_refusal_standing(tmp_path):
    """Fail-closed. The declaration names no single epoch count, so the derivation has no opinion
    and the model's refusal is acted on exactly as before."""
    row = dict(V9_N0_REFUSED,
               declared_condition="all training epochs completed and the final model saved")
    res = _run(tmp_path, row)
    assert res.stages[0]["status"] == "check_failed"
    assert STAGE_CHECK_EPOCH_VETO_KEY not in res.stages[0]


def test_a_killed_training_on_a_reached_looking_epoch_is_still_refused(tmp_path):
    """The floor under "the declared work actually finished". Strip the trainer's end-of-training
    summary out of v9 node 0's own log and leave the epoch counter at 14.83 — a training killed at
    99 % looks exactly like this — and the refusal stands, because nothing says the loop returned."""
    tail = V9_N0_REFUSED["stdout_tail"]
    cut = tail[:tail.index("{'train_runtime'")]
    assert "14.83" in cut and "train_runtime" not in cut
    res = _run(tmp_path, dict(V9_N0_REFUSED, stdout_tail=cut))
    assert res.stages[0]["status"] == "check_failed"
    assert STAGE_CHECK_EPOCH_VETO_KEY not in res.stages[0]


def test_the_veto_never_makes_salvage_more_permissive():
    """docs/36 corollary: the DOING moves, the RECORDING does not. A vetoed row still carries
    `check_inconclusive`, which is the key `metric_salvage` scans for — so a node that later fails
    still cannot contribute a number nobody vouched for."""
    from looplab.engine.metric_salvage import VETO_STAGE_KEYS, _stage_carries_veto_key

    vetoed = [{"name": "train", "status": "ok",
               STAGE_CHECK_INCONCLUSIVE_KEY: "declared_condition_violated: …",
               STAGE_CHECK_EPOCH_VETO_KEY: "the stage declared 15 epochs …"},
              {"name": "score", "status": "fail"}]
    assert _stage_carries_veto_key(vetoed) is True
    assert VETO_STAGE_KEYS == {STAGE_CHECK_INCONCLUSIVE_KEY}, \
        "the new key is deliberately NOT a salvage veto of its own — the doubt already is one"


def test_the_kind_the_veto_is_scoped_to_is_a_real_member_of_the_vocabulary():
    """A typo'd literal here would silently make the veto unreachable — the false refusal comes
    back and nothing goes red. Resolved against the registry rather than spelled twice."""
    from looplab.runtime.command_eval import STAGE_CHECK_DECLARED_CONDITION
    assert STAGE_CHECK_DECLARED_CONDITION in STAGE_CHECK_HARD_KINDS
    assert STAGE_CHECK_DECLARED_CONDITION != STAGE_CHECK_INCONCLUSIVE
