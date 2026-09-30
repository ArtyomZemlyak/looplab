"""A reset charges the lifecycle it abandoned when no attempt was running it (doc 69 69.12a).

The attempt that sees a reset writes its lifecycle's stale-generation terminal itself
(`evaluate.py::_eval_record_superseded`) and that row charges what the lifecycle ran. A lifecycle NO
attempt was running got none: one a pause WITHHELD — its seconds on its `eval_attempt_withheld` row,
waiting for the lifecycle's next terminal — or one a dead process left mid-chain. A reset while the
run was paused abandoned it, the re-dispatch ran the NEW lifecycle, and the old seconds reached no
terminal at all: the reset refunded them to `max_eval_seconds` (critic 2026-09-30, driven — a 1.3 s
failed attempt withheld at DECIDE_REPAIR, then a reset, and `total_eval_seconds` read the new
lifecycle's seconds alone). The engine now charges such a lifecycle at entry, through the same
fold-budget-only row (`Engine._charge_abandoned_lifecycles`).
"""
from __future__ import annotations

from looplab.core.models import Event
from looplab.engine.evaluate import abandoned_lifecycle_charges
from looplab.events.replay import fold
from test_a_withheld_attempt_is_charged import _slow_script, _withheld
from test_eval_canary import _PAUSE, _Dev, _Researcher, _engine, _evaluate, _seed, _terminals


def _log(*rows):
    head = [("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"}),
            ("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": "seed"},
                              "code": "x"})]
    return [Event(seq=i + 1, type=t, ts=float(i), data=dict(d))
            for i, (t, d) in enumerate(head + list(rows))]


def _withhold(generation=0, seconds=1.5, **extra):
    return ("eval_attempt_withheld", {"node_id": 0, "generation": generation, "attempt": 0,
                                      "at": "decide_repair", "reason": "paused",
                                      "eval_seconds": seconds, **extra})


_RESET = ("node_reset", {"node_id": 0, "generation": 0})


def _charges(*rows):
    events = _log(*rows)
    return abandoned_lifecycle_charges(events, fold(events))


# ------------------------------------------------------------------------------------ the rule
def test_an_abandoned_lifecycle_with_spend_and_no_terminal_is_charged_its_durable_seconds():
    assert _charges(_withhold(), _RESET) == [(0, 0, 1.5)]
    # …the whole of what `_durable_prior_seconds` sums, repair and dependency rows included.
    repaired = ("node_repaired", {"node_id": 0, "generation": 0, "attempt": 1, "files": {},
                                  "eval_seconds": 2.0})
    deps = ("deps_installed", {"node_id": 0, "generation": 0, "packages": ["x"], "round": 1,
                               "eval_seconds": 0.25})
    assert _charges(repaired, deps, _withhold(), _RESET) == [(0, 0, 3.75)]
    # A dead process's chain names its lifecycle through its repair or dependency rows alone.
    assert _charges(repaired, _RESET) == [(0, 0, 2.0)]
    assert _charges(deps, _RESET) == [(0, 0, 0.25)]


def test_nothing_is_charged_that_something_else_charges_or_that_spent_nothing():
    # The CURRENT lifecycle: its re-dispatch's terminal charges it (MUTATION: `>=` -> `>`).
    assert _charges(_withhold()) == []
    # A lifecycle whose terminal exists — stamped, or through the terminals' `attempt` alias.
    for terminal in ({"node_id": 0, "generation": 0, "reason": "superseded", "eval_seconds": 1.5},
                     {"node_id": 0, "attempt": 0, "reason": "crash", "eval_seconds": 1.5}):
        assert _charges(_withhold(), ("node_failed", terminal), _RESET) == [], terminal
    # …and the `attempt` alias names ONE lifecycle: the NEW one's terminal leaves the old charged.
    assert _charges(_withhold(), _RESET,
                    ("node_evaluated", {"node_id": 0, "attempt": 1, "metric": 0.5})) == [(0, 0, 1.5)]
    # An UNSTAMPED legacy terminal binds every generation: it can only suppress a charge.
    assert _charges(_withhold(), ("node_failed", {"node_id": 0, "reason": "crash"}), _RESET) == []
    # No seconds, no row; and a stamp the fold would not bind is no lifecycle at all.
    assert _charges(_withhold(seconds=0.0), _RESET) == []
    assert _charges(_withhold(generation=-1), _RESET) == []
    assert _charges(_withhold(generation=True), _RESET) == []
    # A node the fold does not know.
    assert _charges(("eval_attempt_withheld", {"node_id": 7, "generation": 0,
                                               "eval_seconds": 1.0}), _RESET) == []


def test_the_engine_writes_the_charge_once_and_never_on_a_finished_run(tmp_path):
    """Idempotent through its own row, and a finalized total stays the one its report was written
    from. MUTATION: drop `if not state.finished` -> the finished run is charged at entry."""
    from tests.factories import make_engine

    engine = make_engine(tmp_path / "run")
    for row in _log(_withhold(), _RESET):
        engine.store.append(row.type, row.data)
    assert engine._charge_abandoned_lifecycles(fold(engine.store.read_all())) is True
    assert engine._charge_abandoned_lifecycles(fold(engine.store.read_all())) is False
    rows = [e for e in engine.store.read_all() if e.type == "node_failed"]
    assert [(r.data["generation"], r.data["reason"], r.data["eval_seconds"]) for r in rows] == \
        [(0, "superseded", 1.5)]
    assert fold(engine.store.read_all()).total_eval_seconds == 1.5

    finished = make_engine(tmp_path / "finished")
    for row in _log(_withhold(), _RESET, ("run_finished", {"reason": "done"})):
        finished.store.append(row.type, row.data)
    finished._enter_run()
    assert not [e for e in finished.store.read_all() if e.type == "node_failed"]


# ------------------------------------------------------------------------------------ driven
def test_a_withheld_attempt_reset_while_paused_is_charged_when_the_run_resumes(tmp_path):
    """The critic's scenario end to end: DECIDE_REPAIR withholds the failed attempt, the operator
    resets the node while paused, and the resumed engine enters and evaluates the new lifecycle.
    MUTATION: drop the entry call -> the total misses the withheld attempt."""
    ledger = tmp_path / "ledger.txt"
    code = _slow_script(ledger, full="raise", full_sleep=0.8)
    run_dir = tmp_path / "run"
    eng = _engine(run_dir, _Dev(code), eval_canary=False)
    _seed(eng, code)
    real_settle = eng._eval_settle_outcome

    async def _settle_then_pause(a):
        out = await real_settle(a)
        eng.store.append(*_PAUSE)
        return out

    eng._eval_settle_outcome = _settle_then_pause
    evs = _evaluate(eng)
    (row,) = _withheld(evs)
    withheld = row.data["eval_seconds"]
    assert withheld >= 0.8 and _terminals(evs) == []
    eng.store.append("node_reset", {"node_id": 0, "generation": 0})
    eng.store.append("resume", {})

    again = _engine(run_dir, _Dev(_slow_script(ledger)), eval_canary=False,
                    researcher=_Researcher(repairs=0))
    again._enter_run()
    evs = _evaluate(again)
    by_generation = {t.data["generation"]: t for t in _terminals(evs)}
    assert by_generation[0].data["reason"] == "superseded"
    assert by_generation[0].data["error"] == "superseded by node reset"
    assert by_generation[0].data["eval_seconds"] == withheld
    # The eval-type reset keeps the node's code, so the new lifecycle fails the same way — which is
    # beside the point: both lifecycles ran, and both are now charged.
    assert by_generation[1].data["reason"] != "superseded"
    total = fold(evs).total_eval_seconds
    assert abs(total - (withheld + by_generation[1].data["eval_seconds"])) < 1e-6, total

