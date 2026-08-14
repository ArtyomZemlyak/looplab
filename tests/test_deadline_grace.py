"""The one-shot, judge-granted deadline extension (doc 36 / doc 39 site #2).

What is being held, in both directions:
  * a stage that is two seconds from finishing is not killed by a number that cannot see its
    progress bar — and the extension is real, i.e. the child actually keeps running and its output
    is still captured;
  * nothing a judge (or the candidate's log it reads) can say buys more than the OPERATOR'S cap, a
    second grace, or a reprieve from an operator cancel;
  * with the feature off — the default — the behaviour is the historical unconditional tree-kill.

Why it exists: all four `stage_finished.status == "timeout"` rows in `runs/` land exactly on their
4 h / 6 h / 8 h wall and discarded 22.0 GPU-hours between them. The entire captured record of
`rubertlite-dense-retrieval` node 72 — a 12.45-hour node, five repairs, 6.00 h on its last attempt —
ends `100%|##########| 664/664 [00:17<00:00, 38.13it/s]`.

These drive the real `run_argv` against real subprocesses. A source pin could not tell "the judge was
consulted" from "the child got more time", and the second is the property.
"""
from __future__ import annotations

import sys
import threading
import time

import pytest

from looplab.engine.eval_stages import parse_deadline_reply
from looplab.runtime.sandbox import _granted_grace, run_argv

# Prints a line, then keeps printing for well past any deadline these tests set.
_CHATTY = ("import sys,time\n"
           "for i in range(400):\n"
           "    print(f'step {i}/400', flush=True)\n"
           "    time.sleep(0.05)\n"
           "print('DONE')\n")


def _script(tmp_path, body, name="w.py"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return [sys.executable, str(p)]


# --------------------------------------------------------------------------------------------
# The clamp, as a truth table. This is the whole of the enforcement.
# --------------------------------------------------------------------------------------------

def test_the_runtime_clamps_the_judge_and_every_non_answer_is_zero():
    assert _granted_grace(lambda t: 5.0, "", 2.0) == 2.0, "the operator's cap wins"
    assert _granted_grace(lambda t: 1.0, "", 2.0) == 1.0
    assert _granted_grace(lambda t: float("inf"), "", 2.0) == 2.0
    assert _granted_grace(lambda t: float("nan"), "", 2.0) == 0.0
    assert _granted_grace(lambda t: -9.0, "", 2.0) == 0.0
    assert _granted_grace(lambda t: "lots", "", 2.0) == 0.0
    assert _granted_grace(lambda t: None, "", 2.0) == 0.0
    assert _granted_grace(lambda t: 1 / 0, "", 2.0) == 0.0, "a raising judge must not crash the eval"
    assert _granted_grace(None, "", 2.0) == 0.0, "no judge wired"
    assert _granted_grace(lambda t: 5.0, "", 0) == 0.0, "feature off"
    assert _granted_grace(lambda t: 5.0, "", None) == 0.0


def test_the_deadline_verdict_is_one_word_and_fails_closed():
    """Fail-CLOSED here, unlike the stage checker: there an unreadable answer saves a node, here it
    would SPEND. The unreadable case must resolve to whichever answer is cheap."""
    assert parse_deadline_reply("FINISHING") is True
    assert parse_deadline_reply("  finishing\n") is True
    assert parse_deadline_reply("NOT_FINISHING") is False
    assert parse_deadline_reply("It looks like it is finishing") is False
    assert parse_deadline_reply("") is False
    assert parse_deadline_reply(None) is False


# --------------------------------------------------------------------------------------------
# The effect, against real subprocesses
# --------------------------------------------------------------------------------------------

def test_off_by_default_the_deadline_is_still_an_unconditional_kill(tmp_path):
    t0 = time.monotonic()
    rc, out, err, timed_out = run_argv(_script(tmp_path, _CHATTY), str(tmp_path), 1.0)
    assert timed_out is True and time.monotonic() - t0 < 8
    assert "DONE" not in out


def test_a_judge_that_declines_changes_nothing(tmp_path):
    asked = []
    rc, out, err, timed_out = run_argv(
        _script(tmp_path, _CHATTY), str(tmp_path), 1.0,
        on_deadline=lambda tail: asked.append(tail) or 0.0, deadline_grace_max_s=5.0)
    assert timed_out is True
    assert len(asked) == 1, "asked ONCE, not re-asked every poll for the rest of the wait"
    assert "step" in asked[0], "the judge is shown the live tail it is supposed to read"


def test_a_granted_grace_actually_lets_the_child_finish(tmp_path):
    """THE PROPERTY. A child that needs ~1.2 s dies at a 0.6 s deadline and survives it when the
    judge buys more — and its full output, including the last line, is still captured."""
    body = ("import time\n"
            "for i in range(12):\n"
            "    print(f'{i}/12', flush=True); time.sleep(0.1)\n"
            "print('DONE')\n")
    rc, out, _e, timed_out = run_argv(_script(tmp_path, body), str(tmp_path), 0.6)
    assert timed_out is True and "DONE" not in out

    sig: dict = {}
    rc, out, _e, timed_out = run_argv(_script(tmp_path, body), str(tmp_path), 0.6,
                                      signals=sig, on_deadline=lambda tail: 30.0,
                                      deadline_grace_max_s=10.0)
    assert timed_out is False and rc == 0
    assert "DONE" in out
    assert sig["deadline_grace_s"] == 10.0, "clamped to the operator's cap and reported out-of-band"


def test_the_grace_is_one_shot_and_cannot_be_renewed(tmp_path):
    """A judge that could be re-asked at every wall is an unbounded spend wearing a bound."""
    calls = []
    t0 = time.monotonic()
    rc, out, err, timed_out = run_argv(
        _script(tmp_path, _CHATTY), str(tmp_path), 0.5,
        on_deadline=lambda tail: calls.append(1) or 1.0, deadline_grace_max_s=1.0)
    elapsed = time.monotonic() - t0
    assert timed_out is True
    assert len(calls) == 1, "the judge was consulted more than once"
    assert elapsed < 8, f"the child outlived one grace ({elapsed:.1f}s)"


def test_an_operator_cancel_is_never_graced(tmp_path):
    """`cancel` is the operator's own `node_abort`. A judge must not be able to sit on top of it."""
    asked = []
    ev = threading.Event()
    threading.Timer(0.4, ev.set).start()
    t0 = time.monotonic()
    rc, out, err, timed_out = run_argv(
        _script(tmp_path, _CHATTY), str(tmp_path), 60.0, cancel=ev,
        on_deadline=lambda tail: asked.append(1) or 300.0, deadline_grace_max_s=300.0)
    assert timed_out is True
    assert asked == [], "the deadline judge was consulted on an operator cancel"
    assert time.monotonic() - t0 < 15


def test_the_stage_row_reports_the_seconds_that_were_bought(tmp_path):
    """A stage that ran past its declared budget must SAY why, or a later reader reconstructing where
    the compute went (which is how the 22.0 GPU-hours were found) sees only a longer stage."""
    from looplab.runtime.command_eval import run_command_eval
    (tmp_path / "slow.py").write_text(
        "import json,time\n"
        "for i in range(12):\n"
        "    print(i, flush=True); time.sleep(0.1)\n"
        "print(json.dumps({'metric': 0.5}))\n", encoding="utf-8")
    stages = [{"name": "train", "command": [sys.executable, "slow.py"]}]
    res = run_command_eval([sys.executable, "slow.py"], str(tmp_path), 0.6,
                           {"kind": "stdout_json", "key": "metric"}, stages=stages,
                           on_deadline=lambda tail: 30.0, deadline_grace_max_s=8.0)
    assert res.stages[0]["status"] == "ok"
    assert res.stages[0]["deadline_grace_s"] == pytest.approx(8.0)
    assert res.metric == pytest.approx(0.5)


# --------------------------------------------------------------------------------------------
# THE SILENT CHILD. The docstring above names it — a progress bar at 664/664 and then nothing —
# and until 2026-08-14 nothing in this file drove it: every case above keeps a CHATTY child
# printing throughout, which keeps the stall watchdog's clock permanently reset and hides the
# whole interaction between the two clocks. The reviewer's reproduction, verbatim: one bar line,
# then silence, a short budget, a granted grace. Measured on the pre-fix tree it returned
# `wall-clock actually got 4.26 s` for a 600 s extension and a durable row that claimed it.
# --------------------------------------------------------------------------------------------

# Prints ONE progress-bar line — node 72's last line — and then goes quiet, alive, forever.
_SILENT_AFTER_ONE_BAR = ("import time\n"
                         "print('100%|##########| 664/664 [00:17<00:00, 38.13it/s]', flush=True)\n"
                         "time.sleep(3600)\n")


def test_a_grace_granted_to_a_SILENT_child_is_actually_served(tmp_path):
    """THE PROPERTY THE ROW CLAIMS. The stall watchdog and the deadline are two clocks measuring
    different things — total wall time and silence — and a grace moves only the first. The silence
    kill is DEFERRED for exactly the window bought, so the seconds `deadline_grace_s` reports are
    seconds the process really received; then it dies, because a grace is not a reprieve."""
    (tmp_path / "quiet.py").write_text(_SILENT_AFTER_ONE_BAR, encoding="utf-8")
    sig: dict = {}
    t0 = time.monotonic()
    rc, out, err, timed_out = run_argv(
        [sys.executable, "quiet.py"], str(tmp_path), 1.0,
        stall_timeout=1.0,                      # == the budget: what `_stall_window` derives here
        signals=sig, on_deadline=lambda tail: 30.0, deadline_grace_max_s=2.0)
    elapsed = time.monotonic() - t0
    assert sig["deadline_grace_s"] == 2.0
    # The whole finding in one assertion: the wall clock must SHOW the extension the row claims.
    assert elapsed >= 2.5, f"the 2.0s grace bought {elapsed - 1.0:.2f}s of child ({elapsed:.2f}s wall)"
    assert elapsed < 12, "a grace is bounded — the child must not outlive budget + grace"
    assert "664/664" in out, "the graced child's output is still captured"
    assert sig["stalled"] is True, "it never spoke again, and the row must say THAT killed it"


def test_the_durable_row_describes_what_actually_happened_to_a_silent_stage(tmp_path):
    """The RECORD side (doc 36). A row reading `{"deadline_grace_s": 600.0, "seconds": 4.26}` for a
    4 s budget asserts an extension the run did not get — worse than losing the GPU-hours, because a
    later reader reconstructing where the compute went believes it. `seconds` and `deadline_grace_s`
    must be consistent with each other on the SAME row."""
    from looplab.runtime.command_eval import run_command_eval
    (tmp_path / "quiet.py").write_text(_SILENT_AFTER_ONE_BAR, encoding="utf-8")
    stages = [{"name": "train", "command": [sys.executable, "quiet.py"]}]
    res = run_command_eval([sys.executable, "quiet.py"], str(tmp_path), 1.0,
                           {"kind": "stdout_json", "key": "metric"}, stages=stages,
                           stall_cap=1800.0,
                           on_deadline=lambda tail: 600.0, deadline_grace_max_s=2.0)
    row = res.stages[0]
    assert row["deadline_grace_s"] == pytest.approx(2.0)
    assert row["seconds"] >= 1.0 + row["deadline_grace_s"] * 0.75, (
        f"the row claims {row['deadline_grace_s']}s of grace but only ran {row['seconds']}s: {row}")
    assert res.stalled is True and res.timed_out is False


def test_a_deadlocked_child_still_dies_and_the_grace_is_the_only_extra_time_it_gets(tmp_path):
    """The watchdog's own comment — a real deadlock must die in minutes — survives the grace. The
    deferral is bounded by the granted window and never renewed, so the ceiling on a hung stage is
    budget + ONE clamped grace, and it is reported as a stall rather than a clean anything."""
    body = ("import threading\n"                       # a real deadlock, not a sleep
            "print('start', flush=True)\n"
            "lock = threading.Lock(); lock.acquire(); lock.acquire()\n")
    (tmp_path / "dead.py").write_text(body, encoding="utf-8")
    sig: dict = {}
    t0 = time.monotonic()
    rc, out, err, timed_out = run_argv(
        [sys.executable, "dead.py"], str(tmp_path), 1.0, stall_timeout=1.0,
        signals=sig, on_deadline=lambda tail: 900.0, deadline_grace_max_s=1.5)
    elapsed = time.monotonic() - t0
    assert rc != 0 and sig["stalled"] is True
    assert elapsed < 12, f"a deadlocked child outlived budget + one grace ({elapsed:.1f}s)"
    assert "STALLED" in err


def test_the_stall_watchdog_still_precedes_the_judge_when_the_window_is_the_shorter_clock(tmp_path):
    """THE SECOND REGIME, and it is the watchdog working rather than the feature being unreachable.
    A window SHORTER than the budget fires first, so `on_deadline` is never called on a stage that
    has said nothing for a whole window — correctly: the judge's whole input is a live log tail."""
    (tmp_path / "quiet.py").write_text(_SILENT_AFTER_ONE_BAR, encoding="utf-8")
    asked = []
    sig: dict = {}
    rc, out, err, timed_out = run_argv(
        [sys.executable, "quiet.py"], str(tmp_path), 30.0, stall_timeout=1.0, signals=sig,
        on_deadline=lambda tail: asked.append(tail) or 600.0, deadline_grace_max_s=600.0)
    assert asked == [], "a stage silent for a whole stall window has nothing for a judge to read"
    assert sig["stalled"] is True
    assert "deadline_grace_s" not in sig, "no grace was asked for, so none may be claimed"


def test_a_stage_that_goes_quiet_NEAR_its_wall_is_judged_and_keeps_the_whole_grace(tmp_path):
    """The PRODUCTION shape (`stall_cap` 1800 under a multi-hour budget, node 72's own shape): the
    stage talks until close to its wall, so the judge IS reached — and then goes quiet inside the
    grace, which is where the silence window it had already half-spent used to kill it early."""
    body = ("import time\n"
            "for i in range(6):\n"
            "    print(f'{i}/6', flush=True); time.sleep(0.1)\n"
            "print('100%|##########| 664/664', flush=True)\n"
            "time.sleep(3600)\n")
    (tmp_path / "late.py").write_text(body, encoding="utf-8")
    asked = []
    sig: dict = {}
    t0 = time.monotonic()
    rc, out, err, timed_out = run_argv(
        [sys.executable, "late.py"], str(tmp_path), 1.2,
        stall_timeout=0.9,                      # SHORTER than the budget, and already half-spent
        signals=sig, on_deadline=lambda tail: asked.append(tail) or 30.0,
        deadline_grace_max_s=2.0)
    elapsed = time.monotonic() - t0
    assert len(asked) == 1 and "664/664" in asked[0], "the judge reads the bar it is there to read"
    assert sig["deadline_grace_s"] == 2.0
    assert elapsed >= 2.6, (
        f"the grace was served for {elapsed - 1.2:.2f}s of its 2.0s (stall window 0.9s): {elapsed:.2f}s")
