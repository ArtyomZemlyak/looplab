"""A node's FIRST refusal by the operator's host scorer buys one repair before the idea is judged.

MEASURED 2026-09-26 on MiniOneRec inf13: node 5 (a ragged single pass, 4.4x) and node 6 (the same
pass under a CUDA graph, 3.9x, full-width users byte-identical) were refused by the host scorer's
`expect.numeric` contract (`refused == 0`) and the triage judge answered `reject_idea` at ZERO
repairs both times — node 6's rationale resting on "the recall gate demands byte-exact answers",
which the task states the opposite of. A refusal measures THIS BUILD; judging the IDEA should take
at least one repair made with the refusal in hand. `eval_attempt_rules.deferred_triage_verdict` is
the rule, `evaluate._host_contract_refused` the one input it needs from a result.

Driven through the REAL `Engine._evaluate` with the evaluator's results, the Developer and the judge
scripted (the `tests/test_repair_loop_golden.py` harness), plus the rule's truth table.
"""
from __future__ import annotations

import itertools
import sys

import pytest

from looplab.engine import evaluate as ev
from looplab.engine.eval_attempt_rules import (FIRST_HOST_REFUSAL_DEFERRAL, DeferredVerdict,
                                               deferred_triage_verdict)
from looplab.engine.crash_repair import _format_repair_log
from looplab.events.eventstore import EventStore
from looplab.runtime.command_eval import HOST_STAGE_KEY, NUMERIC_DECLARED_KEY
from looplab.runtime.sandbox import RunResult
from tests.test_repair_loop_golden import _GOOD, _Dev, _Judge, _engine, _seed_and_evaluate

_CONTRACT = [{"key": "refused", "op": "==", "value": 0.0}]
_REFUSAL = ("the host scorer REFUSED this candidate (refused == 0 — the stage printed refused = 1). "
            "Do NOT edit the score stage: it is the operator's, protected. Repair the candidate's "
            "own code.\nThe scorer said:\nREFUSED: recall@50 -0.062 (207 lost/82 gained of 2000)")


# ------------------------------------------------------------------------------ the rule's table

@pytest.mark.parametrize("action, engine_reason, host, repairs", list(itertools.product(
    ("reject_idea", "abandon", "repair", "unanswerable", "unreadable"),
    ("expect_failed", "crash", "no_metric", "needs_failed"),
    (True, False),
    (0, 1, 2))))
def test_only_a_first_host_contract_refusal_holds_a_rejection(action, engine_reason, host, repairs):
    got = deferred_triage_verdict(action, engine_reason=engine_reason, host_contract_refused=host,
                                  repairs_done=repairs)
    held = (action == "reject_idea" and engine_reason == "expect_failed" and host and repairs == 0)
    assert got == (DeferredVerdict("reject_idea", FIRST_HOST_REFUSAL_DEFERRAL) if held else None)


def test_the_deferral_row_is_two_keys():
    assert DeferredVerdict("reject_idea", FIRST_HOST_REFUSAL_DEFERRAL).as_row() == {
        "action": "reject_idea", "rule": FIRST_HOST_REFUSAL_DEFERRAL}


# ------------------------------------------------------------ what counts as a host refusal

_HOST_PIPELINE = [
    {"name": "work", "command": [sys.executable, "-c", "print(1)"], "timeout": 60.0},
    {"name": "score", "command": [sys.executable, "/opt/host/score.py"], "timeout": 60.0,
     HOST_STAGE_KEY: True},
]


def _refused(status="expect_failed", numeric=True, failed="score") -> RunResult:
    row = {"name": "score", "status": status, "exit_code": 0, "seconds": 1.0}
    if numeric:
        row[NUMERIC_DECLARED_KEY] = list(_CONTRACT)
        row["numeric_values"] = {"refused": 1.0}
    return RunResult(exit_code=0, stdout='{"speedup": 0.0, "refused": 1}\n', stderr=_REFUSAL,
                     metric=None, timed_out=False,
                     stages=[{"name": "work", "status": "ok", "exit_code": 0, "seconds": 0.1}, row],
                     failed_stage=failed)


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


# ---------------------------------------------------------- driven through the real attempt loop

class _RecordingJudge(_Judge):
    """Answers the same verdict every time and keeps the repair history it was shown."""

    def __init__(self, verdict):
        super().__init__(verdict)
        self.histories: list[str] = []

    def triage_crash(self, node, error, attempt, *, state=None, brief="", history="",
                     stages_passed=None, attempts_left=None):
        self.histories.append(history)
        return super().triage_crash(node, error, attempt, state=state, brief=brief,
                                    history=history, stages_passed=stages_passed,
                                    attempts_left=attempts_left)


def _run(tmp_path, results, *, pipeline=_HOST_PIPELINE, verdict=None):
    judge = _RecordingJudge(verdict or {"action": "reject_idea",
                                        "rationale": "the idea cannot meet the gate"})
    eng = _engine(tmp_path / "run", dev=_Dev(_GOOD, tail=_GOOD + "# repaired\n"), judge=judge)
    queue = list(results)

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    eng._run_eval = fake_run_eval
    eng._resolved_stages = lambda node, workdir, profile=None: [dict(s) for s in pipeline]
    _seed_and_evaluate(eng, code=_GOOD, direction="max")
    events = EventStore(tmp_path / "run" / "events.jsonl").read_all()
    return events, judge


def _rows(events, kind):
    return [e.data for e in events if e.type == kind]


def _passed() -> RunResult:
    return RunResult(exit_code=0, stdout='{"metric": 3.9}\n', stderr="", metric=3.9,
                     timed_out=False)


def test_the_first_refusal_is_repaired_and_the_repair_is_scored(tmp_path):
    events, judge = _run(tmp_path, [_refused(), _passed()])
    (repaired,) = _rows(events, "node_repaired")
    assert repaired["triage_action"] == "repair"
    assert repaired["judge_deferred"] == {"action": "reject_idea",
                                          "rule": FIRST_HOST_REFUSAL_DEFERRAL}
    assert repaired["rationale"] == "the idea cannot meet the gate"   # the judge's own words, kept
    assert [r["metric"] for r in _rows(events, "node_evaluated")] == [3.9]
    assert not _rows(events, "node_failed")
    assert len(judge.histories) == 1


def test_the_second_refusal_is_the_judges_to_reject(tmp_path):
    events, judge = _run(tmp_path, [_refused(), _refused()])
    (repaired,) = _rows(events, "node_repaired")
    assert repaired["judge_deferred"]["action"] == "reject_idea"
    (failed,) = _rows(events, "node_failed")
    assert failed["reason"] == "idea_rejected" and failed["triage_action"] == "reject_idea"
    # …and the judge that rejected it was TOLD the first rejection had been held, not shown it as
    # a fix that failed.
    assert len(judge.histories) == 2 and judge.histories[0] == ""
    assert "THE JUDGE ANSWERED `reject_idea` ON THIS FAILURE" in judge.histories[1]
    assert FIRST_HOST_REFUSAL_DEFERRAL in judge.histories[1]


def test_a_refusal_by_a_stage_the_engine_did_not_build_is_rejected_at_once(tmp_path):
    unstamped = [dict(s, **{HOST_STAGE_KEY: False}) for s in _HOST_PIPELINE]
    events, _judge = _run(tmp_path, [_refused()], pipeline=unstamped)
    assert not _rows(events, "node_repaired")
    (failed,) = _rows(events, "node_failed")
    assert failed["reason"] == "idea_rejected"


def test_abandon_on_the_first_refusal_stands(tmp_path):
    events, _judge = _run(tmp_path, [_refused()], verdict={"action": "abandon",
                                                          "rationale": "nothing to change"})
    assert not _rows(events, "node_repaired")
    (failed,) = _rows(events, "node_failed")
    assert failed["triage_action"] == "abandon" and "judge_deferred" not in failed


# ------------------------------------------------------- the history row, rendered and resumed

def test_the_row_survives_the_ledger_and_renders_only_when_present():
    base = {"attempt": 1, "error_in": "e", "rationale": "the idea cannot meet the gate",
            "stages_passed": 1}
    held = ev.repair_ledger_row({**base, "judge_deferred": {"action": "reject_idea",
                                                            "rule": FIRST_HOST_REFUSAL_DEFERRAL}},
                                attempts=0)
    plain = ev.repair_ledger_row(base, attempts=0)
    assert held["judge_deferred"] == {"action": "reject_idea", "rule": FIRST_HOST_REFUSAL_DEFERRAL}
    assert "judge_deferred" not in plain
    assert "THE JUDGE ANSWERED" in _format_repair_log([held])
    assert "THE JUDGE ANSWERED" not in _format_repair_log([plain])
