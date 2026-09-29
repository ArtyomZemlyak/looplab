"""A pause that WITHHOLDS an attempt leaves a durable record of what it had spent (doc 69 69.12a).

A withheld attempt returns with no terminal — the node stays pending and the re-dispatch after the
pause lifts continues the chain — so two facts had nowhere to live:

  * the SECONDS it had consumed: a passed canary whose full eval the pause refused to start (the
    re-dispatch skips the passed canary), or the failed attempt DECIDE_REPAIR stopped repairing when
    it saw the pause. No terminal and no repair row carried them, so the chain's terminal charged
    nothing for them;
  * that the evaluation STOPPED: `events/eval_occupancy.py` pairs a lifecycle's first start with its
    first terminal, so the whole pause read as a running evaluation.

Every withhold now writes one diagnostic `eval_attempt_withheld` row: `eval_seconds` is what no other row
carries (the lifecycle's next terminal sums it, `_durable_withheld_seconds`), and the occupancy
closes the busy stretch at it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from looplab.engine.evaluate import _durable_repair_ledger, _durable_withheld_seconds
from looplab.events.eval_occupancy import eval_occupancy
from looplab.events.replay import fold
from looplab.events.types import (DIAGNOSTIC_EVENTS, EV_EVAL_ATTEMPT_WITHHELD, EVAL_WITHHELD_POINTS,
                                  EVENT_PAYLOAD_KEYS)
from test_eval_canary import (_PAUSE, _Dev, _PausingDev, _Researcher, _after_canary, _engine,
                              _evaluate, _of, _seed, _terminals)


def _slow_script(ledger: Path, *, canary_sleep: float = 0.0, full: str = "ok",
                 full_sleep: float = 0.0) -> str:
    """A node program: the canary sleeps `canary_sleep` and passes; the full eval sleeps
    `full_sleep` and then prints its metric (`ok`) or raises (`raise`)."""
    full_body = ("    raise KeyError('history_item_sid')\n" if full == "raise"
                 else "    print('METRIC: ' + str(8 / 10))\n")
    return ("import os, time\n"
            "c = os.environ.get('LOOPLAB_CANARY') == '1'\n"
            f"open({str(ledger)!r}, 'a').write(('canary' if c else 'full') + '\\n')\n"
            "if c:\n"
            f"    time.sleep({canary_sleep})\n"
            "    print('METRIC: ' + str(1 / 10))\n"
            "else:\n"
            f"    time.sleep({full_sleep})\n" + full_body)


def _withheld(evs):
    return _of(evs, EV_EVAL_ATTEMPT_WITHHELD)


# ------------------------------------------------------------------------------- the registry
def test_the_row_is_diagnostic_and_declares_what_it_carries():
    assert EV_EVAL_ATTEMPT_WITHHELD in DIAGNOSTIC_EVENTS, "a folded row would move the election's fence"
    contract = EVENT_PAYLOAD_KEYS[EV_EVAL_ATTEMPT_WITHHELD]
    assert set(contract.required) == {"at", "attempt", "eval_seconds", "generation", "node_id",
                                      "reason"}
    assert EVAL_WITHHELD_POINTS == ("admit", "before_launch", "after_canary", "decide_repair")


# ------------------------------------------------------------------ the four withhold points, driven
def test_a_canary_a_pause_withheld_is_charged_at_the_next_terminal(tmp_path):
    """The canary's seconds are the attempt's (`_t0`), and the re-dispatch skips the passed canary:
    before this row they were charged nowhere (the terminal read the full eval's seconds alone).
    MUTATION: drop `_durable_withheld_seconds` from the seed -> the terminal misses the canary."""
    ledger = tmp_path / "ledger.txt"
    code = _slow_script(ledger, canary_sleep=0.8)
    run_dir = tmp_path / "run"
    eng = _engine(run_dir, _Dev(code))
    _seed(eng, code)
    _after_canary(eng, _PAUSE)
    evs = _evaluate(eng)
    assert ledger.read_text().split() == ["canary"] and _terminals(evs) == []
    (row,) = _withheld(evs)
    assert row.data["at"] == "after_canary" and row.data["reason"] == "paused"
    assert row.data["generation"] == 0 and row.data["attempt"] == 0
    canary_seconds = row.data["eval_seconds"]
    assert canary_seconds >= 0.8, row.data
    eng.store.append("resume", {})
    evs = _evaluate(_engine(run_dir, _Dev(code)))
    assert ledger.read_text().split() == ["canary", "full"]
    (term,) = _terminals(evs)
    assert term.type == "node_evaluated"
    assert term.data["eval_seconds"] >= canary_seconds, term.data
    assert fold(evs).total_eval_seconds >= canary_seconds


def test_the_attempt_a_pause_stopped_repairing_is_charged_at_the_next_terminal(tmp_path):
    """DECIDE_REPAIR sees the pause and returns with no repair row: the attempt that just failed was
    charged nowhere. MUTATION: record 0.0 at `decide_repair` -> the terminal misses the first run."""
    ledger = tmp_path / "ledger.txt"
    code = _slow_script(ledger, full="raise", full_sleep=0.8)
    run_dir = tmp_path / "run"
    eng = _engine(run_dir, _Dev(code), eval_canary=False)
    _seed(eng, code)
    real_settle = eng._eval_settle_outcome

    async def _settle_then_pause(a):
        out = await real_settle(a)
        eng.store.append(*_PAUSE)                  # the operator pauses while the attempt died
        return out

    eng._eval_settle_outcome = _settle_then_pause
    evs = _evaluate(eng)
    assert _terminals(evs) == [] and not _of(evs, "node_repaired")
    (row,) = _withheld(evs)
    assert row.data["at"] == "decide_repair" and row.data["reason"] == "paused"
    first = row.data["eval_seconds"]
    assert first >= 0.8, row.data
    eng.store.append("resume", {})
    again = _engine(run_dir, _Dev(code), eval_canary=False, researcher=_Researcher(repairs=0))
    evs = _evaluate(again)
    (term,) = _terminals(evs)
    assert term.type == "node_failed"
    assert term.data["eval_seconds"] >= first + 0.8, term.data


def test_a_relaunch_withheld_at_its_head_records_no_seconds(tmp_path):
    """The attempt before a relaunch is on its `node_repaired` row already; the withheld row closes the
    interval and adds nothing, or the chain would be charged that attempt twice.
    MUTATION: record the attempt's seconds at `before_launch` -> the charge doubles."""
    ledger = tmp_path / "ledger.txt"
    broken = _slow_script(ledger, full="raise")
    fixed = _slow_script(ledger)
    dev = _PausingDev(broken, fixes=[fixed])
    run_dir = tmp_path / "run"
    eng = _engine(run_dir, dev, eval_canary=False)
    dev.store = eng.store
    _seed(eng, broken)
    evs = _evaluate(eng)
    (repaired,) = _of(evs, "node_repaired")
    (row,) = _withheld(evs)
    assert row.data["at"] == "before_launch" and row.data["eval_seconds"] == 0.0
    assert row.data["attempt"] == repaired.data["attempt"] == 1, "the repaired attempt was withheld"
    assert _durable_withheld_seconds(evs, 0, 0) == 0.0


def _receipted_seed(eng) -> None:
    """Node 0 with a PROMISED eval-start boundary and its receipt already durable — a Card session
    writes it at admission on the main task, before the worker's ADMIT folds."""
    eng.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft", "eval_start_boundary": True,
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": "print('unused')\n", "files": {"run.py": "print('METRIC: 0.5')\n"}})
    eng.store.append("node_eval_started", {"node_id": 0, "generation": 0})


@pytest.mark.parametrize(("control", "reason"), [
    (("pause", {"reason": "operator"}), "paused"),
    (("run_abort", {"reason": "finalized"}), "stopping"),
])
def test_admit_closes_a_receipt_it_refuses_to_run(tmp_path, control, reason):
    """ADMIT refuses a halted run before the sandbox; a lifecycle whose receipt is already durable
    read as EVALUATING until something closed it. MUTATION: drop the ADMIT row -> no row."""
    eng = _engine(tmp_path / "run", _Dev("print('METRIC: 0.5')\n"), eval_canary=False)
    _receipted_seed(eng)
    eng.store.append(*control)
    evs = _evaluate(eng)
    assert _terminals(evs) == []
    (row,) = _withheld(evs)
    assert row.data["at"] == "admit" and row.data["reason"] == reason
    assert row.data["eval_seconds"] == 0.0 and row.data["generation"] == 0


def test_admit_names_the_attempt_the_lifecycle_would_have_run(tmp_path):
    """A lifecycle with two durable repairs is withheld at ADMIT: its row names attempt 2, the index
    the ONE derivation (`_durable_repair_ledger`, reached through SEED_LEDGERS) gives the re-dispatch —
    not the record's pre-seed 0. MUTATION: drop the seed from ADMIT's branch -> the row says 0."""
    eng = _engine(tmp_path / "run", _Dev("print('METRIC: 0.5')\n"), eval_canary=False)
    _receipted_seed(eng)
    for n in (1, 2):
        eng.store.append("node_repaired", {
            "node_id": 0, "generation": 0, "attempt": n, "triage_action": "repair",
            "error_in": "crash", "rationale": f"fix {n}", "changed": ["run.py"], "deleted": [],
            "stages_passed": [], "files": {"run.py": "print('METRIC: 0.5')\n"}})
    eng.store.append(*_PAUSE)
    evs = _evaluate(eng)
    assert _terminals(evs) == []
    (row,) = _withheld(evs)
    assert row.data["at"] == "admit" and row.data["attempt"] == 2
    assert row.data["attempt"] == _durable_repair_ledger(evs, 0, 0)[0]


def test_admit_writes_nothing_for_a_lifecycle_with_no_receipt(tmp_path):
    """No receipt, nothing open: a row would only be noise. MUTATION: drop the receipt clause."""
    eng = _engine(tmp_path / "run", _Dev("print('METRIC: 0.5')\n"), eval_canary=False)
    _seed(eng, "print('METRIC: 0.5')\n")
    eng.store.append(*_PAUSE)
    assert _withheld(_evaluate(eng)) == []


# ------------------------------------------------------------------------- the durable reader
class _E:
    def __init__(self, kind, data):
        self.type, self.data = kind, data


def test_the_reader_sums_only_this_lifecycle_s_clean_seconds():
    rows = [_E(EV_EVAL_ATTEMPT_WITHHELD, {"node_id": 0, "generation": 0, "eval_seconds": 2.5}),
            _E(EV_EVAL_ATTEMPT_WITHHELD, {"node_id": 0, "generation": 0, "eval_seconds": 1.5}),
            _E(EV_EVAL_ATTEMPT_WITHHELD, {"node_id": 0, "generation": 1, "eval_seconds": 100.0}),
            _E(EV_EVAL_ATTEMPT_WITHHELD, {"node_id": 1, "generation": 0, "eval_seconds": 100.0}),
            _E(EV_EVAL_ATTEMPT_WITHHELD, {"node_id": 0, "generation": 0, "eval_seconds": True}),
            _E(EV_EVAL_ATTEMPT_WITHHELD, {"node_id": 0, "generation": 0, "eval_seconds": "x"}),
            _E(EV_EVAL_ATTEMPT_WITHHELD, {"node_id": 0, "generation": 0, "eval_seconds": float("inf")}),
            _E(EV_EVAL_ATTEMPT_WITHHELD, {"node_id": 0, "generation": 0, "eval_seconds": -9.0}),
            _E("deps_installed", {"node_id": 0, "generation": 0, "eval_seconds": 50.0})]
    assert _durable_withheld_seconds(rows, 0, 0) == 4.0
    assert _durable_withheld_seconds(rows, 0, 1) == 100.0
    assert _durable_withheld_seconds([], 0, 0) == 0.0


# ------------------------------------------------------------------------------- the occupancy
def _row(seq, ts, kind, node=0, generation=0):
    return {"seq": seq, "ts": ts, "type": kind, "data": {"node_id": node, "generation": generation}}


def test_a_withheld_row_ends_the_busy_stretch_and_a_restart_opens_the_next():
    """MUTATION: ignore `eval_attempt_withheld` -> one interval 10..100 and no dead window over the pause."""
    rows = [_row(0, 0.0, "run_started"), _row(1, 10.0, "node_eval_started"),
            _row(2, 20.0, EV_EVAL_ATTEMPT_WITHHELD), _row(3, 50.0, "resume"),
            _row(4, 60.0, "node_eval_started"), _row(5, 100.0, "node_evaluated")]
    out = eval_occupancy(rows)
    assert out["intervals"] == [(10.0, 20.0, 0), (60.0, 100.0, 0)]
    assert out["dead_windows"] == [(20.0, 60.0)] and out["dead_seconds"] == 40.0
    assert out["open_intervals"] == 0


def test_a_withheld_lifecycle_never_restarted_is_closed_not_open():
    rows = [_row(0, 0.0, "run_started"), _row(1, 10.0, "node_eval_started"),
            _row(2, 20.0, EV_EVAL_ATTEMPT_WITHHELD), _row(3, 90.0, "pause")]
    out = eval_occupancy(rows)
    assert out["intervals"] == [(10.0, 20.0, 0)] and out["open_intervals"] == 0


def test_a_restart_still_running_is_the_one_open_stretch():
    rows = [_row(0, 0.0, "run_started"), _row(1, 10.0, "node_eval_started"),
            _row(2, 20.0, EV_EVAL_ATTEMPT_WITHHELD), _row(3, 30.0, "node_eval_started"),
            _row(4, 40.0, "node_eval_started"), _row(5, 90.0, "phase_progress")]
    out = eval_occupancy(rows)
    assert out["intervals"] == [(10.0, 20.0, 0), (30.0, 90.0, 0)]
    assert out["open_intervals"] == 1


def test_rows_after_the_first_terminal_and_skewed_closes_are_ignored():
    rows = [_row(0, 0.0, "run_started"), _row(1, 10.0, "node_eval_started"),
            _row(2, 5.0, EV_EVAL_ATTEMPT_WITHHELD),                       # stamped before its open
            _row(3, 30.0, "node_eval_started"), _row(4, 40.0, "node_failed"),
            _row(5, 50.0, "node_eval_started"), _row(6, 60.0, EV_EVAL_ATTEMPT_WITHHELD)]
    out = eval_occupancy(rows)
    assert out["intervals"] == [(30.0, 40.0, 0)] and out["open_intervals"] == 0


def test_a_lifecycle_with_no_withheld_row_pairs_as_it_always_did():
    """Every older log: first start, first terminal, per lifecycle — a sibling's withheld row does not
    change another lifecycle's pairing."""
    rows = [_row(0, 0.0, "run_started"), _row(1, 10.0, "node_eval_started"),
            _row(2, 12.0, "node_eval_started"), _row(3, 30.0, "node_evaluated"),
            _row(4, 35.0, "node_evaluated"),
            _row(5, 40.0, "node_eval_started", node=1), _row(6, 45.0, EV_EVAL_ATTEMPT_WITHHELD, node=1),
            _row(7, 50.0, "node_eval_started", node=2)]
    out = eval_occupancy(rows)
    assert out["intervals"] == [(10.0, 30.0, 0), (40.0, 45.0, 1), (50.0, 50.0, 2)]
    assert out["open_intervals"] == 1
