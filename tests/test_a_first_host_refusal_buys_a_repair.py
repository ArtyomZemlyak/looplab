"""A node's FIRST refusal by the operator's host scorer can buy one repair before the idea is judged.

MEASURED 2026-09-26 on MiniOneRec inf13: node 5 (a ragged single pass, 4.4x) and node 6 (the same
pass under a CUDA graph, 3.9x, full-width users byte-identical) were refused by the host scorer's
`expect.numeric` contract (`refused == 0`) and the triage judge answered `reject_idea` at their first
refusal — node 6's rationale resting on "the recall gate demands byte-exact answers", which the task
states the opposite of. Reset with the diagnosis in hand, node 5 reached 4.17x inside the gate in two
repairs. Node 9 (would-be 2.11x under a 4.17x champion) was rejected, rightly.

`eval_attempt_rules.deferred_triage_verdict` is the rule (behind `Settings.host_refusal_deferral`,
OFF by default); the two critics' findings it answers are each pinned below: the FIRST host refusal,
not "no repair yet" (node 6 had a crash repair first); a VALUE gate on the scorer's declared would-be
number (node 9); cap headroom for a second judged attempt; and a HELD verdict that settles onto the
terminal whenever the chain ends without a second ruling (stuck, floors, non-answers).

Driven through the REAL `Engine._evaluate` with the evaluator's results, the Developer and the judge
scripted (the `tests/test_repair_loop_golden.py` harness), plus the rule's truth table.
"""
from __future__ import annotations

import itertools
import math
import sys

import anyio
import pytest

from looplab.core.models import DEVELOPER_STUCK_PREFIX
from looplab.engine import evaluate as ev
from looplab.engine.crash_repair import _format_repair_log
from looplab.engine.eval_attempt_rules import (FIRST_HOST_REFUSAL_DEFERRAL, DeferredVerdict,
                                               coerce_judge_deferred, deferred_triage_verdict)
from looplab.engine.repair_judgment import format_repair_trajectory
from looplab.events.eventstore import EventStore
from looplab.runtime.command_eval import HOST_STAGE_KEY, NUMERIC_DECLARED_KEY
from looplab.runtime.sandbox import RunResult
from tests.test_repair_loop_golden import _GOOD, _Dev, _Judge, _engine

_CONTRACT = [{"key": "refused", "op": "==", "value": 0.0}]
_REFUSAL = ("the host scorer REFUSED this candidate (refused == 0 — the stage printed refused = 1). "
            "Do NOT edit the score stage: it is the operator's, protected. Repair the candidate's "
            "own code.\nThe scorer said:\nREFUSED: recall@50 -0.062 (207 lost/82 gained of 2000)")
_HELD = DeferredVerdict("reject_idea", FIRST_HOST_REFUSAL_DEFERRAL)


# ------------------------------------------------------------------------------ the rule's table

def _rule(**kw):
    base = dict(enabled=True, engine_reason="expect_failed", host_contract_refused=True,
                first_host_refusal=True, cap_headroom=True, would_be=4.27, champion=1.37,
                direction="max")
    base.update(kw)
    return deferred_triage_verdict("reject_idea", **base)


@pytest.mark.parametrize("action, engine_reason, host, first, headroom, enabled", list(
    itertools.product(("reject_idea", "abandon", "repair", "unanswerable", "unreadable"),
                      ("expect_failed", "crash", "needs_failed"), (True, False), (True, False),
                      (True, False), (True, False))))
def test_every_conjunct_is_required(action, engine_reason, host, first, headroom, enabled):
    got = deferred_triage_verdict(action, enabled=enabled, engine_reason=engine_reason,
                                  host_contract_refused=host, first_host_refusal=first,
                                  cap_headroom=headroom, would_be=4.27, champion=1.37)
    held = (enabled and action == "reject_idea" and engine_reason == "expect_failed" and host
            and first and headroom)
    assert got == (_HELD if held else None)


@pytest.mark.parametrize("would_be, champion, direction, held", [
    (4.27, 1.37, "max", True),        # inf13 node 5
    (3.89, 1.37, "max", True),        # inf13 node 6
    (2.11, 4.17, "max", False),       # inf13 node 9: could not have won
    (4.17, 4.17, "max", False),       # a tie does not beat the champion
    (4.27, None, "max", True),        # no champion yet: nothing to beat
    (0.2, 0.3, "min", True),          # direction honoured
    (0.4, 0.3, "min", False),
    (None, 1.37, "max", False),       # no declared would-be number: no deferral
    (math.nan, 1.37, "max", False),
    (math.inf, 1.37, "max", False),
    (True, 0.5, "max", False),        # a bool is not a number
    ("4.2", 1.37, "max", True),       # a numeric string reads as its number
])
def test_the_value_gate(would_be, champion, direction, held):
    assert _rule(would_be=would_be, champion=champion, direction=direction) == (
        _HELD if held else None)


def test_the_deferral_row_is_two_keys_and_nothing_else_survives_the_ledger():
    assert _HELD.as_row() == {"action": "reject_idea", "rule": FIRST_HOST_REFUSAL_DEFERRAL}
    assert coerce_judge_deferred(_HELD.as_row()) == _HELD.as_row()
    for foreign in ({"action": "abandon", "rule": FIRST_HOST_REFUSAL_DEFERRAL},
                    {"action": "reject_idea", "rule": "made_up"}, "reject_idea", None, {}):
        assert coerce_judge_deferred(foreign) is None, foreign
    row = ev.repair_ledger_row({"attempt": 1, "error_in": "e", "rationale": "r",
                                "judge_deferred": {"action": "repair", "rule": "x"}}, attempts=0)
    assert "judge_deferred" not in row


# ------------------------------------------------------------ what counts as a host refusal

_HOST_PIPELINE = [
    {"name": "work", "command": [sys.executable, "-c", "print(1)"], "timeout": 60.0},
    {"name": "score", "command": [sys.executable, "/opt/host/score.py"], "timeout": 60.0,
     HOST_STAGE_KEY: True},
]


def _refused(status="expect_failed", numeric=True, failed="score", would_be=4.27,
             diagnosis=None) -> RunResult:
    row = {"name": "score", "status": status, "exit_code": 0, "seconds": 1.0}
    if numeric:
        row[NUMERIC_DECLARED_KEY] = list(_CONTRACT)
        row["numeric_values"] = {"refused": 1.0}
    return RunResult(exit_code=0, stdout='{"speedup": 0.0, "refused": 1}\n', stderr=_REFUSAL,
                     metric=None, timed_out=False,
                     stages=[{"name": "work", "status": "ok", "exit_code": 0, "seconds": 0.1}, row],
                     failed_stage=failed, host_would_be=would_be, host_diagnosis=diagnosis)


def test_the_host_contract_refusal_is_recognized():
    assert ev._host_contract_refused(_refused(), _HOST_PIPELINE) is True


@pytest.mark.parametrize("res, stages, why", [
    (_refused(numeric=False), _HOST_PIPELINE, "an artifact contract, not the numeric one"),
    (_refused(status="fail"), _HOST_PIPELINE, "a scorer that crashed is not a refusal"),
    (_refused(), [dict(s, **{HOST_STAGE_KEY: False}) for s in _HOST_PIPELINE],
     "a stage named `score` the engine did not stamp"),
    (_refused(failed=None), _HOST_PIPELINE, "no failed stage"),
    (_refused(), [], "no pipeline resolved"),
])
def test_anything_else_is_not(res, stages, why):
    assert ev._host_contract_refused(res, stages) is False, why


# ------------------------------------------------- "first host refusal", read off the durable log

class _E:
    def __init__(self, seq, type_, data):
        self.seq, self.type, self.data = seq, type_, data


def _claim(seq, attempt, gen=0):
    return _E(seq, "eval_invocation_claimed", {"node_id": 6, "generation": gen, "attempt": attempt,
                                               "invocation_id": f"i{attempt}"})


def _stage(seq, status, gen=0, numeric=True, name="score"):
    d = {"node_id": 6, "name": name, "status": status, "generation": gen}
    if numeric:
        d[NUMERIC_DECLARED_KEY] = list(_CONTRACT)
    return _E(seq, "stage_finished", d)


def test_node_6_shape_a_crash_repair_first_does_not_disqualify_the_first_refusal():
    events = [_claim(1, 0), _stage(2, "fail", numeric=False), _claim(5, 1), _stage(6, "expect_failed")]
    assert ev._earlier_host_refusals(events, 6, 0, 1, "score") == 0


def test_a_second_refusal_counts_the_first():
    events = [_claim(1, 0), _stage(2, "expect_failed"), _claim(5, 1), _stage(6, "expect_failed")]
    assert ev._earlier_host_refusals(events, 6, 0, 1, "score") == 1
    assert ev._earlier_host_refusals(events, 6, 0, 0, "score") == 0


def test_another_lifecycle_another_stage_or_no_contract_does_not_count():
    events = [_stage(1, "expect_failed", gen=0), _stage(2, "expect_failed", name="train"),
              _stage(3, "expect_failed", numeric=False), _claim(4, 0, gen=1),
              _stage(5, "expect_failed", gen=1)]
    assert ev._earlier_host_refusals(events, 6, 1, 0, "score") == 0


def test_a_resumed_attempts_own_earlier_rows_do_not_count():
    # A process died after attempt 1's stage rows and before its terminal; the resume re-claims
    # attempt 1. Those rows are that attempt's own, not an earlier refusal.
    events = [_claim(1, 0), _stage(2, "fail", numeric=False), _claim(5, 1),
              _stage(6, "expect_failed"), _claim(9, 1), _stage(10, "expect_failed")]
    assert ev._earlier_host_refusals(events, 6, 0, 1, "score") == 0


def test_without_a_claim_row_the_newest_refusal_is_this_attempts_own():
    assert ev._earlier_host_refusals([_stage(1, "expect_failed")], 6, 0, 0, "score") == 0
    assert ev._earlier_host_refusals([_stage(1, "expect_failed"), _stage(2, "expect_failed")],
                                     6, 0, 1, "score") == 1


def test_a_hold_is_open_only_while_the_newest_repair_row_is_the_deferred_one():
    held = {"attempt": 1, "fix": "the idea cannot meet the gate", "judge_deferred": _HELD.as_row()}
    later = {"attempt": 2, "fix": "fix the pages"}
    assert ev._held_reject_from_log([held]) == "the idea cannot meet the gate"
    assert ev._held_reject_from_log([held, later]) is None
    assert ev._held_reject_from_log([]) is None
    assert ev._held_reject_from_log([{"attempt": 1, "fix": "", "judge_deferred": _HELD.as_row()}])


# ---------------------------------------------------------- driven through the real attempt loop

class _SeqJudge(_Judge):
    """Answers `verdicts` in order (the last one repeats) and keeps each history it was shown."""

    def __init__(self, verdicts):
        super().__init__(verdicts[0])
        self.verdicts = list(verdicts)
        self.histories: list[str] = []

    def triage_crash(self, node, error, attempt, *, state=None, brief="", history="",
                     stages_passed=None, attempts_left=None):
        self.histories.append(history)
        self.verdict = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        return super().triage_crash(node, error, attempt, state=state, brief=brief,
                                    history=history, stages_passed=stages_passed,
                                    attempts_left=attempts_left)


class _RecDev(_Dev):
    """A Developer that keeps the error text each repair was handed."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.errors: list[str] = []

    def repair(self, idea, code, error):
        self.errors.append(str(error))
        return super().repair(idea, code, error)


_REJECT = {"action": "reject_idea", "rationale": "the idea cannot meet the gate",
           "summary": "decode positions use the batch max M+s instead of each request's L+s"}


def _run(tmp_path, results, *, pipeline=_HOST_PIPELINE, verdicts=(_REJECT,), deferral=True,
         lead=False, champion=None, cap=None, reasons=None, dev=None, direction="max"):
    judge = _SeqJudge(list(verdicts))
    dev = dev or _RecDev(_GOOD, tail=_GOOD + "# repaired\n")
    eng = _engine(tmp_path / "run", dev=dev, judge=judge)
    eng._host_refusal_deferral = deferral
    eng._host_refusal_repair_lead = lead
    if cap is not None:
        eng._inline_repair_attempts = cap
    if reasons is not None:
        eng._inline_repair_reasons = tuple(reasons)
    queue = list(results)

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    eng._run_eval = fake_run_eval
    eng._resolved_stages = lambda node, workdir, profile=None: [dict(s) for s in pipeline]
    eng.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g",
                                     "direction": direction})
    idea = {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"}
    nid = 0
    if champion is not None:          # an evaluated champion the would-be number is held against
        eng.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                          "idea": idea, "code": _GOOD})
        eng.store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": champion})
        nid = 1
    eng.store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": idea, "code": _GOOD})

    async def _bounded() -> bool:
        with anyio.move_on_after(300) as scope:
            await eng._evaluate(nid, anyio.CapacityLimiter(1), None)
        return scope.cancelled_caught

    assert not anyio.run(_bounded), "the attempt loop did not terminate"
    events = EventStore(tmp_path / "run" / "events.jsonl").read_all()
    return events, judge, dev, nid


def _rows(events, kind, nid=None):
    return [e.data for e in events if e.type == kind and (nid is None or e.data.get("node_id") == nid)]


def _passed() -> RunResult:
    return RunResult(exit_code=0, stdout='{"metric": 3.9}\n', stderr="", metric=3.9,
                     timed_out=False)


def _crash() -> RunResult:
    return RunResult(exit_code=1, stdout="", stderr="Traceback (most recent call last):\n"
                     "RuntimeError: an illegal memory access was encountered", metric=None,
                     timed_out=False)


def test_off_by_default_a_first_refusal_is_rejected_at_once(tmp_path):
    events, *_ = _run(tmp_path, [_refused()], deferral=False)
    assert not _rows(events, "node_repaired")
    (failed,) = _rows(events, "node_failed")
    assert failed["reason"] == "idea_rejected"


def test_the_first_refusal_is_repaired_and_the_repair_is_scored(tmp_path):
    events, judge, dev, nid = _run(tmp_path, [_refused(), _passed()])
    (repaired,) = _rows(events, "node_repaired")
    assert repaired["triage_action"] == "repair"
    assert repaired["judge_deferred"] == _HELD.as_row()
    assert repaired["rationale"] == "the idea cannot meet the gate"   # the judge's own words, kept
    assert [r["metric"] for r in _rows(events, "node_evaluated", nid)] == [3.9]
    assert not _rows(events, "node_failed")
    # THE REPAIR WAS TOLD what it was repairing over, and that "stuck" settles it.
    assert "`reject_idea`" in dev.errors[0] and "the idea cannot meet the gate" in dev.errors[0]
    assert "declare that you are stuck" in dev.errors[0]


def test_the_second_refusal_is_the_judges_to_reject(tmp_path):
    events, judge, _dev, _nid = _run(tmp_path, [_refused(), _refused()])
    (repaired,) = _rows(events, "node_repaired")
    assert repaired["judge_deferred"]["action"] == "reject_idea"
    (failed,) = _rows(events, "node_failed")
    assert failed["reason"] == "idea_rejected" and failed["triage_action"] == "reject_idea"
    assert "stands: the chain ended" not in failed["error"]   # a second ruling, not a held one
    assert len(judge.histories) == 2 and judge.histories[0] == ""
    assert "THE JUDGE ANSWERED `reject_idea` ON THIS FAILURE" in judge.histories[1]


def test_node_9_a_refusal_that_could_not_have_won_is_rejected_at_once(tmp_path):
    events, *_ = _run(tmp_path, [_refused(would_be=2.11)], champion=4.17)
    assert not _rows(events, "node_repaired")
    (failed,) = _rows(events, "node_failed")
    assert failed["reason"] == "idea_rejected"


def test_a_refusal_that_would_beat_the_champion_is_held(tmp_path):
    events, *_ = _run(tmp_path, [_refused(would_be=4.27), _passed()], champion=1.37)
    (repaired,) = _rows(events, "node_repaired")
    assert repaired["judge_deferred"] == _HELD.as_row()


def test_no_declared_would_be_number_no_deferral(tmp_path):
    events, *_ = _run(tmp_path, [_refused(would_be=None)])
    assert not _rows(events, "node_repaired")


def test_node_6_shape_a_crash_repair_before_the_first_refusal(tmp_path):
    verdicts = [{"action": "repair", "rationale": "fix the illegal access"}, _REJECT]
    events, *_ = _run(tmp_path, [_crash(), _refused(), _passed()], verdicts=verdicts)
    repaired = _rows(events, "node_repaired")
    assert len(repaired) == 2
    assert "judge_deferred" not in repaired[0] and repaired[1]["judge_deferred"] == _HELD.as_row()
    assert not _rows(events, "node_failed")


def test_no_cap_headroom_no_deferral(tmp_path):
    # With one repair allowed, a deferral would spend it and the floor would end the node before
    # the judge could rule again — so the judge's rejection is taken now.
    events, *_ = _run(tmp_path, [_refused(), _passed()], cap=1)
    assert not _rows(events, "node_repaired")
    (failed,) = _rows(events, "node_failed")
    assert failed["reason"] == "idea_rejected"


def test_a_stuck_developer_on_the_deferred_repair_settles_the_held_rejection(tmp_path):
    dev = _RecDev(_GOOD, answers=[f"{DEVELOPER_STUCK_PREFIX} the gate cannot be met this way)"])
    events, *_ = _run(tmp_path, [_refused()], dev=dev)
    assert not _rows(events, "node_repaired")
    (failed,) = _rows(events, "node_failed")
    assert failed["reason"] == "idea_rejected" and failed["triage_action"] == "reject_idea"
    assert failed["triage_rationale"] == "the idea cannot meet the gate"
    assert "stands: the chain ended before a second verdict" in failed["error"]
    assert "does not know how to fix" in failed["error"]


def test_a_chain_that_ends_on_a_floor_before_a_second_ruling_settles_the_held_rejection(tmp_path):
    # The repaired build then crashes with a reason the operator narrowed out of inline repair: the
    # gate buys nothing and no judge is asked — the held verdict is the last word.
    events, judge, *_ = _run(tmp_path, [_refused(), _crash()], reasons=["expect_failed"])
    (repaired,) = _rows(events, "node_repaired")
    assert repaired["judge_deferred"] == _HELD.as_row()
    (failed,) = _rows(events, "node_failed")
    assert failed["reason"] == "idea_rejected" and failed["triage_action"] == "reject_idea"
    assert "stands: the chain ended" in failed["error"]
    assert len(judge.histories) == 1


def test_a_non_answer_on_the_second_attempt_does_not_close_the_hold(tmp_path):
    verdicts = [_REJECT, {"action": "no-such-verdict", "rationale": "?"}]
    events, *_ = _run(tmp_path, [_refused(), _crash()], verdicts=verdicts)
    (failed,) = _rows(events, "node_failed")
    assert failed["reason"] == "idea_rejected" and failed["triage_action"] == "reject_idea"


def test_a_real_second_verdict_closes_the_hold_and_stands(tmp_path):
    verdicts = [_REJECT, {"action": "abandon", "rationale": "nothing left to change"}]
    events, *_ = _run(tmp_path, [_refused(), _crash()], verdicts=verdicts)
    (failed,) = _rows(events, "node_failed")
    assert failed["triage_action"] == "abandon"
    assert failed["reason"] != "idea_rejected"


def test_a_refusal_by_a_stage_the_engine_did_not_build_is_rejected_at_once(tmp_path):
    unstamped = [dict(s, **{HOST_STAGE_KEY: False}) for s in _HOST_PIPELINE]
    events, *_ = _run(tmp_path, [_refused()], pipeline=unstamped)
    assert not _rows(events, "node_repaired")
    (failed,) = _rows(events, "node_failed")
    assert failed["reason"] == "idea_rejected"


def test_abandon_on_the_first_refusal_stands(tmp_path):
    events, *_ = _run(tmp_path, [_refused()], verdicts=[{"action": "abandon",
                                                         "rationale": "nothing to change"}])
    assert not _rows(events, "node_repaired")
    (failed,) = _rows(events, "node_failed")
    assert failed["triage_action"] == "abandon" and "judge_deferred" not in failed


def test_the_deferred_row_is_not_graded_against_the_rejection_text(tmp_path):
    # `verify_repair` would read "the idea cannot meet the gate" as a prescription the diff did not
    # carry out; the deferred row is graded on its bytes alone.
    events, *_ = _run(tmp_path, [_refused(), _passed()])
    (repaired,) = _rows(events, "node_repaired")
    verified = repaired.get("verified")
    assert not (isinstance(verified, dict) and verified.get("verdict") == "unmet"), verified


# ------------------------------------------------------- the history row, rendered and resumed

def test_the_row_survives_the_ledger_and_renders_only_when_present():
    base = {"attempt": 1, "error_in": "e", "rationale": "the idea cannot meet the gate",
            "stages_passed": 1}
    held = ev.repair_ledger_row({**base, "judge_deferred": _HELD.as_row()}, attempts=0)
    plain = ev.repair_ledger_row(base, attempts=0)
    assert held["judge_deferred"] == _HELD.as_row()
    assert "judge_deferred" not in plain
    assert "THE JUDGE ANSWERED" in _format_repair_log([held])
    assert "THE JUDGE ANSWERED" not in _format_repair_log([plain])
    # …and the critic's trajectory labels the held verdict rather than calling it a fix.
    assert "HELD verdict" in format_repair_trajectory([held])
    assert "HELD verdict" not in format_repair_trajectory([plain])
    assert "the fix claimed" in format_repair_trajectory([plain])
