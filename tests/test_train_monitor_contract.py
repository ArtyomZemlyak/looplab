"""The DECLARED CONTRACT, read live: what the training-log watchdog is told about the promise the
stage it is watching made about itself.

THE MEASUREMENT THIS FILE EXISTS FOR. Re-derived 2026-08-20 from the committed 450-decision bench
(`tests/data/judge_bench/train_monitor.v1.jsonl.gz`), grouped by `label_basis` and by the stage row
each decision was actually about:

    trained fine, metric ~0 (node_metric_degenerate)     53 decisions   48 said broken   91 %
    stage exited 0, engine failed it on a CHECK          23 decisions    1 said broken    4 %
    stage exited 0, engine failed it on ARTIFACTS        15 decisions    1 said broken    7 %
    stage KILLED by signal -9                            39 decisions    2 said broken    5 %
    stage KILLED by signal -15                            2 decisions    0
    stage CRASHED with an exception (exit 1)             22 decisions    1 said broken    5 %

The judge is right when the failure is in the CURVE and appears blind everywhere else. Three of
those four "blind" rows are not blindness at all and are refused in the report rather than patched
here; the 38 exit-0 rows are, and the reason is one line long: **the engine has held the stage's own
promise since before the stage started and never showed it to the judge.** All four of the
`check_failed` attempts the bench records as missed are `declared_condition_violated`, and three
declared an epoch count the trainer's configuration could never reach — `"n_epochs": 8` against a
declared 15, `6` against 10, `1` against 50 — each echoed in the first 30 KB of a multi-hour log.
8.2 h of stage time to reach, at the end, a conclusion available at the start.

EVERY ASSERTION BELOW IS DRIVEN AND EVERY ONE HAS AN INPUT THAT MAKES IT FAIL — the negative
controls are named `..._negative_control` and each one is the real shape that broke the obvious
version of the rule, not an invented one.
"""
from __future__ import annotations

import pytest

from looplab.engine import train_monitor as _tm
from looplab.engine.train_monitor import (
    StageDeclaration, declared_schedule_shortfall, eval_log_plan, schedule_reading,
    stage_contract_context)


# One real record from `runs/rubertlite-dr-unified-v8` node 8's `train.log`: the HF Trainer's log
# dict rendered onto the SAME carriage-return record as the tqdm bar that carries the step counter.
# 11232 steps at epoch 1.52 by step 2130 is an 8.02-epoch schedule; the stage declared 15.
_SHORT = ("  19%|##        | 2130/11232 [12:41<58:12,  2.61it/s]"
          "{'loss': 5.1027, 'grad_norm': 1.44, 'learning_rate': 4.7e-05, 'epoch': 1.52}\r")
# The same shape from a schedule that IS 15 epochs (1695 steps, epoch 0.62 at step 70 -> 15.01).
_FULL = ("   4%|#         | 70/1695 [03:27<1:17:40,  2.87s/it]"
         "{'loss': 58.3918, 'grad_norm': 6.41, 'learning_rate': 0.0002, 'epoch': 0.62}\r")
_ASSERT_15 = "all 15 epochs completed and the final model saved to experiments/x/final"


def test_schedule_reading_derives_the_trainers_own_ceiling():
    reading = schedule_reading(_SHORT)
    assert reading is not None
    assert reading.done == 2130 and reading.total == 11232 and reading.epoch == pytest.approx(1.52)
    # 1.52 * 11232 / 2130 = 8.016..., i.e. the 8-epoch schedule the config echoed at second one.
    assert reading.epochs == pytest.approx(8.02, abs=0.02)


def test_schedule_reading_requires_one_record_negative_control():
    """The lane-crossing shape that made the loose version convict a CHAMPION.

    `rubertlite-dr-unified-v8` node 13 declared 10 epochs, ran all 10 and scored 0.716575. Its tail
    holds a FINISHED `313/313` dataloader bar and, on a later record, a training log dict reporting
    epoch 4.02. Pairing "the last counter seen" with "the last epoch seen" reads that as a
    4.02-epoch schedule and fires. Measured over all 450 bench decisions: the loose rule fires on 1
    productive decision and this rule on 0.
    """
    crossed = ("100%|##########| 313/313 [00:11<00:00, 27.9it/s]\r"
               "{'loss': 20.68, 'grad_norm': 0.9, 'epoch': 4.02}\r")
    assert schedule_reading(crossed) is None
    assert declared_schedule_shortfall("all 10 epochs completed", crossed) is None
    # ...and the SAME two numbers on one record are read, so the control is about the pairing and
    # not about the parser refusing the values.
    together = ("100%|##########| 313/313 [00:11<00:00, 27.9it/s]"
                "{'loss': 20.68, 'grad_norm': 0.9, 'epoch': 4.02}\r")
    assert schedule_reading(together) is not None


def test_schedule_reading_refuses_a_resolution_it_cannot_support():
    """`epoch` is logged to 2 decimals, and projecting it multiplies that rounding by total/done.

    At step 2 of 11232 an `epoch: 0.01` is anywhere from 28 to 84 epochs. Refusing is the whole
    difference between a reading and a guess.
    """
    too_early = "   0%| | 2/11232 [00:01<9:00:00,  0.35it/s]{'loss': 9.9, 'epoch': 0.01}\r"
    assert schedule_reading(too_early) is None
    # The same log a few thousand steps later answers.
    assert schedule_reading(too_early + _SHORT) is not None


def test_shortfall_fires_on_the_measured_case():
    found = declared_schedule_shortfall(_ASSERT_15, _SHORT)
    assert found is not None
    target, reading = found
    assert target == 15
    assert reading.epochs == pytest.approx(8.02, abs=0.02)


def test_shortfall_never_contradicts_the_end_of_stage_floor_negative_control():
    """`runs/rubertlite-dr-unified-v9` node 0: 14.87 of a declared 15, which
    `command_eval.epoch_floor_acquits` exists to ACQUIT (HF sizes `max_steps` from a floored
    updates-per-epoch, so a fractional final epoch is a step budget and not a shortened run).

    A live rung that called that stage short would contradict the engine's own end-of-run answer
    about the same stage. It shares `DECLARED_EPOCH_TOLERANCE` for exactly that reason, and this is
    the test that would go red if either side moved alone. Driven over that node's 11 recorded bench
    decisions the reading is 14.87-14.92 and it fires on none of them.
    """
    at_end = ("100%|##########| 1695/1695 [4:12:00<00:00,  8.9s/it]"
              "{'loss': 22.9, 'grad_norm': 1.1, 'epoch': 14.87}\r")
    assert schedule_reading(at_end).epochs == pytest.approx(14.87, abs=0.01)
    assert declared_schedule_shortfall(_ASSERT_15, at_end) is None
    # ...and a genuine whole-epoch shortfall on the identical shape still fires, so the tolerance is
    # a bar and not an off switch.
    assert declared_schedule_shortfall("all 20 epochs completed", at_end) is not None


def test_shortfall_refuses_an_assertion_with_no_single_target():
    """Two declared numbers mean the sentence has no target, and picking one would be choosing which
    half of the declaration to hold the stage to. Shares `declared_epoch_target`'s rule."""
    assert declared_schedule_shortfall("all 15 epochs then all 3 fine-tuning epochs", _SHORT) is None
    assert declared_schedule_shortfall("the model converges", _SHORT) is None
    # The positive control on the same text, so the refusal is about the assertion and not the log.
    assert declared_schedule_shortfall(_ASSERT_15, _SHORT) is not None


def test_contract_context_quotes_the_promise_and_reports_the_shortfall():
    declaration = StageDeclaration(assertion=_ASSERT_15, files=("experiments/x/final/model.safetensors",))
    text = stage_contract_context(declaration, _SHORT)
    assert _ASSERT_15 in text
    assert "experiments/x/final/model.safetensors" in text
    assert "ENGINE READING" in text and "2130/11232" in text and "15 this stage declared" in text


def test_contract_context_states_the_promise_even_with_no_shortfall():
    """The contract is evidence in its own right, not a wrapper for the shortfall: 15 of the 38
    exit-0 decisions were failed on a missing ARTIFACT, where no epoch reading has anything to say
    and the declared file is still the thing the stage is about to be judged on."""
    declaration = StageDeclaration(assertion=_ASSERT_15, files=("experiments/x/final/model.safetensors",))
    text = stage_contract_context(declaration, _FULL)
    assert _ASSERT_15 in text and "experiments/x/final/model.safetensors" in text
    assert "ENGINE READING" not in text


def test_contract_context_is_empty_when_the_stage_promised_nothing():
    """An empty return is what reproduces the historical message byte for byte — the same additive
    discipline `trajectory_context` and `_LOOK_INVITATION` keep. `test_contract_text_splices_...`
    below proves the message really is unchanged, which this alone does not."""
    assert stage_contract_context(None, _SHORT) == ""
    assert stage_contract_context(StageDeclaration(), _SHORT) == ""


def test_eval_log_plan_carries_every_stages_declaration():
    plan = eval_log_plan([
        {"name": "mine", "expect": {"files": ["neg.parquet"],
                                    "assert": "hard negatives mined for 90% of queries"}},
        {"name": "train", "expect": {"assert": _ASSERT_15}},
        {"name": "score"},
    ])
    assert set(plan.declarations) == {"mine", "train"}
    assert plan.declarations["mine"].files == ("neg.parquet",)
    assert plan.declarations["train"].assertion == _ASSERT_15
    # NOT the same map as the AUTHORITY grant: `mine` declared no `role: "training"`, so it carries
    # evidence and no kill authority. A `declarations` that inherited `training_artifacts`' refusals
    # would hide a check the engine is certainly going to run on `mine`.
    assert plan.training_artifacts == ()


def test_contract_text_splices_into_the_judge_message_and_is_additive():
    """Drive the real `_training_verdict` message assembly, capturing what the client is handed.

    A source pin would be satisfied by a comment; this observes the bytes. The two arms differ by
    exactly the contract block, which is what "additive" has to mean.
    """
    captured = {}

    class _Client:
        pass

    class _Engine(_tm.TrainingMonitorMixin):
        def __init__(self):
            self.developer = type("D", (), {"client": _Client()})()

    def _fake_judge(client, messages, schema, parser="tool_call", tools=None, max_turns=0):
        captured.setdefault("msgs", []).append(messages)
        return None

    import looplab.trust.judge as _judge_mod
    original = _judge_mod.structured_judge
    _judge_mod.structured_judge = _fake_judge
    try:
        engine = _Engine()
        engine._training_verdict("TAIL", "CTX", "STAGE", "TRAJ", None, contract_text="")
        engine._training_verdict("TAIL", "CTX", "STAGE", "TRAJ", None,
                                 contract_text="CONTRACT-BLOCK")
    finally:
        _judge_mod.structured_judge = original

    without, with_contract = (m[0][1]["content"] for m in
                              ([captured["msgs"][0]], [captured["msgs"][1]]))
    assert "CONTRACT-BLOCK" not in without
    assert "CONTRACT-BLOCK" in with_contract
    assert with_contract.replace("CONTRACT-BLOCK\n\n", "") == without
    # Position: below the stage identity (a fact about the same stage) and above the trajectory,
    # because a contract the stage cannot meet makes a healthy curve irrelevant.
    assert with_contract.index("STAGE") < with_contract.index("CONTRACT-BLOCK")
    assert with_contract.index("CONTRACT-BLOCK") < with_contract.index("TRAJ")


def test_the_switch_restores_the_historical_prompt():
    """`train_monitor_contract=false` must be a byte-for-byte restore, which is only true if the
    flag gates the DERIVATION and not merely the wording."""
    from looplab.core.config import Settings
    from looplab.engine.options import EngineOptions

    assert Settings().train_monitor_contract is True
    assert EngineOptions().train_monitor_contract is True
    # It is EVIDENCE, so it deliberately gets NO legacy-snapshot pin: a resumed run gains no paid
    # call, no intervention, no concurrency and no selection change. `train_monitor_tools` has one
    # because it DOES add paid round trips — that is the distinction, and a future field that spends
    # money must not copy this row by symmetry.
    from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS
    assert "train_monitor_contract" not in LEGACY_CONFIG_SNAPSHOT_DEFAULTS
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["train_monitor_tools"] is False
