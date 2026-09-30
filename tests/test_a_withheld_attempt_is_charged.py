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


def _receipted_seed(eng, code: str = "print('METRIC: 0.5')\n") -> None:
    """Node 0 with a PROMISED eval-start boundary and its receipt already durable — a Card session
    writes it at admission on the main task, before the worker's ADMIT folds."""
    eng.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft", "eval_start_boundary": True,
        "idea": {"operator": "draft", "params": {"x": 1.0, "y": 1.0}, "rationale": "seed"},
        "code": "print('unused')\n", "files": {"run.py": code}})
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


# ------------------------------------------------------------ critic 2026-09-29 (a8774), driven
def test_a_same_owner_resume_reopens_the_busy_stretch_at_its_next_launch(tmp_path):
    """An owner that resumes its own pause writes no second `node_eval_started`
    (`Node.eval_activity_started` is still set), so a stretch that reopened only on a start row
    left the whole re-dispatched evaluation reading idle. It reopens at the lifecycle's next launch
    row. MUTATION: drop the relaunch rows from the pairing -> one stretch, the 1.5 s eval missing."""
    ledger = tmp_path / "ledger.txt"
    code = _slow_script(ledger, canary_sleep=0.3, full_sleep=1.5)
    run_dir = tmp_path / "run"
    eng = _engine(run_dir, _Dev(code))
    _receipted_seed(eng, code)
    _after_canary(eng, _PAUSE)
    evs = _evaluate(eng)
    assert _terminals(evs) == [] and len(_withheld(evs)) == 1
    eng.store.append("resume", {})
    evs = _evaluate(_engine(run_dir, _Dev(code)))
    (term,) = _terminals(evs)
    assert term.type == "node_evaluated"
    assert len(_of(evs, "node_eval_started")) == 1, "the same owner wrote no second start"
    occ = eval_occupancy(evs)
    assert occ["open_intervals"] == 0 and len(occ["intervals"]) == 2
    busy = sum(finish - begin for begin, finish, _node in occ["intervals"])
    assert busy >= 1.5 + 0.3 - 0.05, occ["intervals"]


def test_an_abort_after_a_withhold_charges_what_the_lifecycle_spent(tmp_path):
    """The abort's terminal is the lifecycle's last: it charged 0.0 for THIS dispatch, and the
    withheld attempt's seconds reached no terminal at all. MUTATION: write 0.0 again in
    `_skip_if_aborted` -> the terminal and the run's total read 0."""
    ledger = tmp_path / "ledger.txt"
    code = _slow_script(ledger, canary_sleep=0.8)
    run_dir = tmp_path / "run"
    eng = _engine(run_dir, _Dev(code))
    _seed(eng, code)
    _after_canary(eng, _PAUSE)
    evs = _evaluate(eng)
    (row,) = _withheld(evs)
    assert row.data["eval_seconds"] >= 0.8
    eng.store.append("node_abort", {"node_id": 0, "generation": 0})
    eng.store.append("resume", {})
    evs = _evaluate(_engine(run_dir, _Dev(code)))
    (term,) = _terminals(evs)
    assert term.data["reason"] == "aborted"
    assert term.data["eval_seconds"] == row.data["eval_seconds"]
    assert fold(evs).total_eval_seconds >= row.data["eval_seconds"]


def test_the_zero_compute_terminals_share_the_one_prior_sum():
    """Every zero-compute terminal charges `_durable_prior_seconds`: the repair, dependency-round
    and withheld rows of that lifecycle, and only of that lifecycle."""
    rows = [_E("node_repaired", {"node_id": 0, "generation": 0, "attempt": 1, "eval_seconds": 2.0}),
            _E("deps_installed", {"node_id": 0, "generation": 0, "eval_seconds": 1.0}),
            _E(EV_EVAL_ATTEMPT_WITHHELD, {"node_id": 0, "generation": 0, "eval_seconds": 0.5}),
            _E(EV_EVAL_ATTEMPT_WITHHELD, {"node_id": 0, "generation": 1, "eval_seconds": 9.0})]
    from looplab.engine.evaluate import _durable_prior_seconds
    assert _durable_prior_seconds(rows, 0, 0) == 3.5
    assert _durable_prior_seconds(rows, 0, 1) == 9.0


def test_the_per_node_eval_budget_counts_a_withheld_attempt(tmp_path):
    """The seconds a withheld attempt spent are this lifecycle's: the per-node eval budget stop reads
    them through the `prior_repair_seconds` seed, so a pause cannot refund the budget. MUTATION:
    leave `_durable_withheld_seconds` out of the seed -> the chain buys a repair past its budget."""
    import anyio

    ledger = tmp_path / "ledger.txt"
    code = _slow_script(ledger, full="raise")
    eng = _engine(tmp_path / "run", _Dev(code), eval_canary=False)
    _seed(eng, code)
    eng.store.append(EV_EVAL_ATTEMPT_WITHHELD, {
        "node_id": 0, "generation": 0, "attempt": 0, "at": "decide_repair", "reason": "paused",
        "eval_seconds": 50.0})

    async def _run():
        await eng._evaluate(0, anyio.CapacityLimiter(1), max_es=40.0)

    anyio.run(_run)
    evs = eng.store.read_all()
    (term,) = _terminals(evs)
    assert term.type == "node_failed" and not _of(evs, "node_repaired")
    assert "eval budget exhausted" in (term.data.get("triage_rationale") or term.data.get("error")
                                      or ""), term.data
    assert term.data["eval_seconds"] >= 50.0


def test_a_withhold_on_a_repaired_chain_carries_only_its_own_attempt(tmp_path):
    """DECIDE_REPAIR's row carries the attempt that just failed and nothing the chain already charged
    elsewhere: the repaired attempt before it is on its `node_repaired` row. MUTATION: record
    `charged_eval_seconds()` -> the earlier repair's seconds are counted twice."""
    ledger = tmp_path / "ledger.txt"
    broken = _slow_script(ledger, full="raise", full_sleep=0.4)
    still_broken = _slow_script(ledger, full="raise", full_sleep=0.4) + "# still\n"
    dev = _Dev(broken, fixes=[still_broken])
    eng = _engine(tmp_path / "run", dev, eval_canary=False)
    _seed(eng, broken)
    real_settle = eng._eval_settle_outcome

    async def _settle_then_pause_on_the_second(a):
        out = await real_settle(a)
        if a.attempt >= 1:
            eng.store.append(*_PAUSE)
        return out

    eng._eval_settle_outcome = _settle_then_pause_on_the_second
    evs = _evaluate(eng)
    (repaired,) = _of(evs, "node_repaired")
    (row,) = _withheld(evs)
    assert row.data["at"] == "decide_repair" and row.data["attempt"] == 1
    assert 0.4 <= row.data["eval_seconds"] < repaired.data["eval_seconds"] + 0.4, (row.data,
                                                                                   repaired.data)


def test_a_passed_canary_on_a_repaired_chain_carries_only_its_own_seconds(tmp_path):
    """After a repair, the canary a pause withholds the full eval of carries ITS seconds, not the
    chain's. MUTATION: record `charged_eval_seconds(...)` at `after_canary` -> double counted."""
    ledger = tmp_path / "ledger.txt"
    broken = _slow_script(ledger, canary_sleep=0.3, full="raise", full_sleep=0.6)
    fixed = _slow_script(ledger, canary_sleep=0.3) + "# fixed\n"
    dev = _Dev(broken, fixes=[fixed])
    eng = _engine(tmp_path / "run", dev)
    _seed(eng, broken)
    _after_canary(eng, _PAUSE, attempt=1)
    evs = _evaluate(eng)
    (repaired,) = _of(evs, "node_repaired")
    (row,) = _withheld(evs)
    assert row.data["at"] == "after_canary" and row.data["attempt"] == 1
    assert 0.3 <= row.data["eval_seconds"] < 0.3 + repaired.data["eval_seconds"], (row.data,
                                                                                   repaired.data)


def test_admit_names_a_finalize_beside_the_pause_stopping(tmp_path):
    """A paused run whose finalize is pending is STOPPING — the stop wins over the pause.
    MUTATION: `"paused" if a.state.paused else "stopping"` -> it reads paused."""
    eng = _engine(tmp_path / "run", _Dev("print('METRIC: 0.5')\n"), eval_canary=False)
    _receipted_seed(eng)
    eng.store.append(*_PAUSE)
    eng.store.append("run_abort", {"reason": "finalized"})
    (row,) = _withheld(_evaluate(eng))
    assert row.data["at"] == "admit" and row.data["reason"] == "stopping"


@pytest.mark.parametrize("seconds", [-3.0, float("nan"), float("inf"), "x"])
def test_the_writer_clamps_what_it_records(tmp_path, seconds):
    """A clock that stepped back, or a value that is no number, must not refund the chain: the row
    carries 0.0. MUTATION: keep the sign -> a negative row."""
    import anyio

    from looplab.engine.evaluate import EvalAttempt

    eng = _engine(tmp_path / "run", _Dev("print('METRIC: 0.5')\n"), eval_canary=False)
    _seed(eng, "print('METRIC: 0.5')\n")
    a = EvalAttempt(node_id=0)
    a.generation = 0

    async def _write():
        await eng._record_eval_withheld(a, "before_launch", seconds)

    anyio.run(_write)
    (row,) = _withheld(eng.store.read_all())
    assert row.data["eval_seconds"] == 0.0


@pytest.mark.parametrize("control", [("node_abort", {"node_id": 0, "generation": 0}),
                                     ("node_reset", {"node_id": 0})])
def test_an_intervention_after_the_attempt_s_watcher_owns_the_terminal(tmp_path, control):
    """On a paused run, a reset or abort recorded between SETTLE_OUTCOME's verdict and DECIDE_REPAIR
    owns the lifecycle: the abort a terminal with the chain's seconds, the reset its stale-generation
    one — never a withheld return that leaves the reset's old generation with none. MUTATION: drop
    the intervention check from DECIDE_REPAIR's pause branch -> a withheld row, no terminal."""
    ledger = tmp_path / "ledger.txt"
    code = _slow_script(ledger, full="raise", full_sleep=0.3)
    eng = _engine(tmp_path / "run", _Dev(code), eval_canary=False)
    _seed(eng, code)
    real_settle = eng._eval_settle_outcome

    async def _settle_then_pause_and_intervene(a):
        out = await real_settle(a)
        eng.store.append(*_PAUSE)
        eng.store.append(*control)
        return out

    eng._eval_settle_outcome = _settle_then_pause_and_intervene
    evs = _evaluate(eng)
    assert _withheld(evs) == []
    (term,) = [e for e in evs if e.type == "node_failed" and e.data.get("node_id") == 0
               and e.data.get("generation") == 0]
    expected = "aborted" if control[0] == "node_abort" else "superseded"
    assert term.data["reason"] == expected
    assert term.data["eval_seconds"] >= 0.3



class _KillingProxy:
    """A proxy scorer that predicts every candidate doomed — ADMIT's proxy kill, on demand."""

    def score_with_uncertainty(self, state, node):
        return 0.1, 0.0

    def abstains(self, state, node, nearest):
        return False

    def should_skip(self, state, node, pred, nearest):
        return True


@pytest.mark.parametrize("terminal", ["gpu_unavailable", "proxy_skipped"])
def test_admit_s_zero_compute_terminals_charge_the_lifecycle_s_prior_seconds(tmp_path, terminal):
    """ADMIT's own zero-compute terminals — a GPU pin it cannot enforce, the proxy's kill — close a
    lifecycle a pause may already have charged, and each is its LAST terminal: it carries the
    durable seconds no other terminal will. MUTATION: write 0.0 again at either -> 0."""
    from looplab.runtime.sandbox import GpuPinUnenforceable

    code = "print('METRIC: 0.5')\n"
    eng = _engine(tmp_path / "run", _Dev(code), eval_canary=False)
    _seed(eng, code)
    eng.store.append(EV_EVAL_ATTEMPT_WITHHELD, {
        "node_id": 0, "generation": 0, "attempt": 0, "at": "after_canary", "reason": "paused",
        "eval_seconds": 7.25})
    if terminal == "gpu_unavailable":
        def _refuse(*_args, **_kwargs):
            raise GpuPinUnenforceable("a positive GPU declaration on a zero-device inventory")

        eng._resource_eval_env = _refuse
    else:
        eng.proxy_scorer = _KillingProxy()
        eng.proxy_kill_fraction = 0.5
    evs = _evaluate(eng)
    (term,) = _terminals(evs)
    assert term.data["reason"] == terminal
    assert term.data["eval_seconds"] == 7.25
    assert fold(evs).total_eval_seconds == 7.25


def test_an_abort_the_watcher_sees_while_the_withheld_row_is_written_owns_the_terminal(tmp_path):
    """A canary passed on a paused run, and the withheld row is written while the attempt's watcher
    is still live: an abort it sees in that window owns the lifecycle — a terminal charging the
    canary's seconds, never a withheld return the abort then closes at 0 s. MUTATION: drop the
    check after the `after_canary` row -> no terminal."""
    import anyio

    ledger = tmp_path / "ledger.txt"
    code = _slow_script(ledger, canary_sleep=0.4)
    eng = _engine(tmp_path / "run", _Dev(code))
    _seed(eng, code)
    _after_canary(eng, _PAUSE)
    real_record = eng._record_eval_withheld

    async def _record_then_abort(a, at, seconds):
        await real_record(a, at, seconds)
        eng.store.append("node_abort", {"node_id": 0, "generation": 0})
        for _ in range(40):                         # the watcher polls every 0.3 s
            if a._seen.get("kind"):
                break
            await anyio.sleep(0.05)
        assert a._seen.get("kind") == "abort"

    eng._record_eval_withheld = _record_then_abort
    evs = _evaluate(eng)
    (term,) = _terminals(evs)
    assert term.type == "node_failed" and term.data["reason"] == "aborted"
    assert term.data["eval_seconds"] >= 0.4
    assert "while its canary ran" in term.data["error"]


def test_a_launch_row_reopens_a_withheld_stretch_only_after_the_lifecycle_started():
    """A relaunch row (`eval_canary_started` / `eval_invocation_claimed`) reopens the stretch a
    withhold closed, and a withheld stretch only: one stamped BEFORE the lifecycle's start (written
    by another writer, landing ahead of the folded start) opens nothing early. MUTATION: drop the
    `started` guard -> the stretch opens at the stray row, 5 s early."""
    rows = [_row(0, 0.0, "run_started"), _row(1, 5.0, "eval_canary_started"),
            _row(2, 10.0, "node_eval_started"), _row(3, 20.0, EV_EVAL_ATTEMPT_WITHHELD),
            _row(4, 30.0, "eval_invocation_claimed"), _row(5, 40.0, "node_evaluated")]
    out = eval_occupancy(rows)
    assert out["intervals"] == [(10.0, 20.0, 0), (30.0, 40.0, 0)]
    assert out["dead_windows"] == [(20.0, 30.0)] and out["open_intervals"] == 0


def test_a_withheld_row_that_cannot_be_written_leaves_the_lifecycle_pending(tmp_path):
    """The row is diagnostic: a disk that refuses it loses the record, never the node. MUTATION:
    let the append's `OSError` propagate -> `_evaluate`'s containment writes an `engine_error`
    terminal for an attempt that never ran."""
    eng = _engine(tmp_path / "run", _Dev("print('METRIC: 0.5')\n"), eval_canary=False)
    _receipted_seed(eng)
    eng.store.append(*_PAUSE)
    real_append = eng.store.append

    def _refusing(kind, data, *args, **kwargs):
        if kind == EV_EVAL_ATTEMPT_WITHHELD:
            raise OSError(28, "No space left on device")
        return real_append(kind, data, *args, **kwargs)

    eng.store.append = _refusing
    evs = _evaluate(eng)
    assert _terminals(evs) == [] and _withheld(evs) == []
    assert fold(evs).nodes[0].status.value == "pending"


def test_a_late_intervention_writes_its_own_kind_of_terminal_charging_the_whole_lifecycle(tmp_path):
    """`_eval_record_late_intervention`'s three answers, each charging `charged_eval_seconds()` —
    the durable prior a dead process paid PLUS this process's — never this process's alone (critic
    2026-09-30: every driven late-intervention test started from a prior of 0). MUTATIONS: charge
    `total_eval`; treat a Card drop as no intervention; call a Card drop an abort."""
    import anyio

    class _Attempt:
        node_id, generation, total_eval = 0, 0, 1.0
        marked = False

        def charged_eval_seconds(self):
            return 7.5                                  # a 6.5 s durable prior + 1.0 s of this one

        def mark_superseded_workdir(self):
            self.marked = True

    for kind, reason, error in (
            ("abort", "aborted", "aborted by operator (on a paused run)"),
            ("card_drop", "card_dropped", "Card dropped by operator (on a paused run)"),
            ("reset", "superseded", "superseded by node reset")):
        eng = _engine(tmp_path / kind, _Dev("print('METRIC: 0.5')\n"), eval_canary=False)
        _seed(eng, "print('METRIC: 0.5')\n")
        attempt = _Attempt()
        wrote = anyio.run(eng._eval_record_late_intervention, attempt, kind, "on a paused run")
        (term,) = _terminals(list(eng.store.read_all()))
        assert wrote is True and term.type == "node_failed", kind
        assert (term.data["reason"], term.data["error"]) == (reason, error), kind
        assert term.data["eval_seconds"] == 7.5, kind
        assert attempt.marked is (kind == "reset"), kind
    eng = _engine(tmp_path / "none", _Dev("print('METRIC: 0.5')\n"), eval_canary=False)
    _seed(eng, "print('METRIC: 0.5')\n")
    assert anyio.run(eng._eval_record_late_intervention, _Attempt(), None, "x") is False
    assert _terminals(list(eng.store.read_all())) == []
