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



# -------------------------------------------------------------------------------- critic 2026-09-30
# The first cut charged a RESET's older generation off repair, dependency and withheld rows alone.
# Three lifecycles it left refunded, each driven by the critic: a node DELETED while paused (its
# current generation, which nothing re-dispatches), an invocation a dead process SETTLED and never
# carried (its seconds on the settle row only), and a reset landing after this process's entry on a
# run that then FINISHED in it (never charged: a finished run is skipped at every later entry).

def _settle(seconds, *, attempt=0, outcome="failed", generation=0):
    return ("eval_invocation_settled", {"node_id": 0, "generation": generation, "attempt": attempt,
                                        "invocation_id": f"inv-{attempt}", "outcome": outcome,
                                        "eval_seconds": seconds})


def _repaired(seconds, attempt=1):
    return ("node_repaired", {"node_id": 0, "generation": 0, "attempt": attempt, "files": {},
                              "eval_seconds": seconds})


def test_a_settle_no_row_carried_is_charged_and_a_carried_one_is_not():
    """The critic's `d3`: a 26,830 s `ok` settle, the process dies, a reset — [] before. MUTATION:
    drop the orphan term from `_durable_prior_seconds` -> []."""
    assert _charges(_settle(26830.0, outcome="ok"), _RESET) == [(0, 0, 26830.0)]
    # A repair row carries the attempt it answers: the settle before it is not charged twice.
    assert _charges(_settle(5.0), _repaired(5.0), _RESET) == [(0, 0, 5.0)]
    # …and a DECIDE_REPAIR withhold carries it too; a withhold at any earlier point carries nothing
    # of a settle (MUTATION: drop the `at` test -> the admit row swallows the 26,830 s).
    assert _charges(_settle(5.0), _withhold(seconds=5.0), _RESET) == [(0, 0, 5.0)]
    admit = _withhold(seconds=0.0, at="admit")
    assert _charges(_settle(26830.0, outcome="ok"), admit, _RESET) == [(0, 0, 26830.0)]
    # A dead process's settle, then a resumed re-run of the same attempt that a repair carried: the
    # first settle's window closed with no carrying row (MUTATION: charge the last settle only).
    assert _charges(_settle(4.0), _settle(6.0), _repaired(6.0), _RESET) == [(0, 0, 10.0)]
    # …and a dependency round carries the attempt it re-runs.
    deps = ("deps_installed", {"node_id": 0, "generation": 0, "packages": ["x"], "round": 1,
                               "eval_seconds": 3.0})
    assert _charges(_settle(3.0), deps, _settle(2.0, outcome="ok"), _RESET) == [(0, 0, 5.0)]
    # Another lifecycle's rows never close this one's window; a malformed number is no seconds.
    other = ("node_repaired", {"node_id": 0, "generation": 1, "attempt": 1, "files": {},
                               "eval_seconds": 1.0})
    assert _charges(_settle(7.0), other, _RESET) == [(0, 0, 7.0)]
    assert _charges(_settle(True), _RESET) == [] and _charges(_settle("nan"), _RESET) == []


def test_the_recovered_terminal_is_priced_off_the_sum_once():
    """`_eval_recover_settled` finalizes from the orphan `ok` settle, which the sum already holds:
    the terminal is `prior + 0`, never the settle twice (the driven twin is
    `tests/test_settled_eval_recovery.py`)."""
    from looplab.engine.evaluate import _durable_prior_seconds
    events = _log(_settle(2.0), _repaired(2.0), _settle(9.5, attempt=1, outcome="ok"))
    assert _durable_prior_seconds(events, 0, 0) == 11.5


def test_a_deleted_node_s_current_lifecycle_is_charged_and_named_a_delete(tmp_path):
    """The critic's `d2`: a withheld attempt, a delete while paused, a resume — the tombstone is out
    of `pending_nodes`, so nothing re-dispatched it and the total read 0.0. MUTATION: drop the
    tombstone clause -> []."""
    from tests.factories import make_engine

    tomb = ("node_tombstoned", {"node_ids": [0]})
    assert _charges(_withhold(), tomb) == [(0, 0, 1.5)]
    # An evaluated node that is deleted afterwards is not charged again.
    done = ("node_evaluated", {"node_id": 0, "generation": 0, "metric": 0.5, "eval_seconds": 1.5})
    assert _charges(_withhold(), done, tomb) == []
    # A reset THEN a delete: both lifecycles, each named for what abandoned it.
    gen1 = _withhold(generation=1, seconds=2.0)
    engine = make_engine(tmp_path / "run")
    for row in _log(_withhold(), _RESET, gen1, tomb):
        engine.store.append(row.type, row.data)
    assert engine._charge_abandoned_lifecycles(fold(engine.store.read_all())) is True
    rows = [e.data for e in engine.store.read_all() if e.type == "node_failed"]
    assert [(r["generation"], r["reason"], r["error"], r["eval_seconds"]) for r in rows] == [
        (0, "superseded", "superseded by node reset", 1.5),
        (1, "superseded", "abandoned by node delete", 2.0)]
    state = fold(engine.store.read_all())
    assert state.total_eval_seconds == 3.5 and state.nodes[0].tombstoned
    assert engine._charge_abandoned_lifecycles(state) is False


def test_an_orphan_settle_is_its_own_lifecycle_s_and_never_negative_or_infinite():
    """crit_v46 survivors n1-11/n1-13/n1-14 (`evaluate.py::_durable_orphan_settle_seconds`).
    MUTATIONS: match the settle on the node alone -> the other lifecycle's 10 s is charged too; drop
    `seconds > 0` -> a clock stepped back charges -3 s; drop `isfinite` -> an `inf` settle charges
    inf."""
    from looplab.engine.evaluate import _durable_orphan_settle_seconds

    def settle(generation, seconds):
        return ("eval_invocation_settled", {"node_id": 0, "generation": generation, "attempt": 0,
                                            "outcome": "failed", "eval_seconds": seconds})

    events = _log(settle(0, 10.0), settle(1, 5.0))
    assert _durable_orphan_settle_seconds(events, 0, 1) == 5.0
    assert _durable_orphan_settle_seconds(events, 0, 0) == 10.0
    assert _durable_orphan_settle_seconds(_log(settle(0, -3.0)), 0, 0) == 0.0
    assert _durable_orphan_settle_seconds(_log(settle(0, float("inf"))), 0, 0) == 0.0


def test_a_deleted_node_s_charge_is_not_a_failure_or_an_eval_to_the_strategist(tmp_path):
    """crit_v46 L2: the delete's charge-only `failed` terminal read as a search FAILURE and its
    partial seconds as an EVAL's cost in the Strategist's context — driven, one deleted node moved
    `failure_rate` 0.0 -> 0.5 and `avg_eval_seconds` 30.0 -> 15.4. The budget still counts them.
    MUTATIONS: drop the tombstone filter from `failure_rate` / from `avg_eval_seconds` -> red."""
    from tests.factories import make_engine

    engine = make_engine(tmp_path / "run", strategist_budget_brief=True)
    rows = [("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})]
    for nid in (0, 1):
        rows.append(("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {"x": float(nid)}}}))
    rows += [("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.0, "eval_seconds": 30.0}),
             ("eval_attempt_withheld", {"node_id": 1, "generation": 0, "attempt": 0,
                                        "at": "decide_repair", "reason": "paused",
                                        "eval_seconds": 0.873}),
             ("node_tombstoned", {"node_ids": [1]})]
    for kind, data in rows:
        engine.store.append(kind, data)
    assert engine._charge_abandoned_lifecycles(fold(engine.store.read_all())) is True
    state = fold(engine.store.read_all())
    assert state.nodes[1].status.value == "failed" and state.total_eval_seconds == 30.873
    ctx = engine._strategy_ctx(state)
    assert (ctx.failure_rate, ctx.avg_eval_seconds) == (0.0, 30.0)


def test_two_abandoned_lifecycles_are_both_charged_each_under_its_own_generation(tmp_path):
    """MUTATIONS: charge only the first sorted lifecycle; stamp the row with the node id instead of
    the generation (the fold then charges nothing, and every entry would append it again)."""
    from tests.factories import make_engine

    node_2 = ("node_created", {"node_id": 2, "parent_ids": [], "operator": "draft",
                               "idea": {"operator": "draft", "params": {}, "rationale": "seed"},
                               "code": "y"})
    withheld_2 = ("eval_attempt_withheld", {"node_id": 2, "generation": 0, "attempt": 0,
                                            "at": "decide_repair", "reason": "paused",
                                            "eval_seconds": 4.0})
    reset_2 = ("node_reset", {"node_id": 2, "generation": 0})
    engine = make_engine(tmp_path / "run")
    for row in _log(_withhold(), _RESET, node_2, withheld_2, reset_2):
        engine.store.append(row.type, row.data)
    assert engine._charge_abandoned_lifecycles(fold(engine.store.read_all())) is True
    rows = [e.data for e in engine.store.read_all() if e.type == "node_failed"]
    assert sorted((r["node_id"], r["generation"], r["eval_seconds"]) for r in rows) == [
        (0, 0, 1.5), (2, 0, 4.0)]
    assert fold(engine.store.read_all()).total_eval_seconds == 5.5
    assert engine._charge_abandoned_lifecycles(fold(engine.store.read_all())) is False


def test_the_finish_gate_charges_a_reset_that_landed_after_entry(tmp_path):
    """The critic's `d5`: entry charged nothing, the reset came after it, and the run finished in the
    same process — its total never held the withheld seconds. Both finish gates refuse ONCE over the
    charge and then finish over the charged log. MUTATIONS: drop the refusal from either gate."""
    from tests.factories import make_engine

    engine = make_engine(tmp_path / "run")
    for row in _log(_withhold(), _RESET):
        engine.store.append(row.type, row.data)
    tail = engine.store.read_all()[-1].seq
    assert engine._finish_if_quiescent({"reason": "done"}, after_seq=tail) is False
    events = engine.store.read_all()
    assert [e.type for e in events if e.type in ("node_failed", "run_finished")] == ["node_failed"]
    assert engine._finish_if_quiescent({"reason": "done"}, after_seq=events[-1].seq) is True
    state = fold(engine.store.read_all())
    assert state.finished and state.total_eval_seconds == 1.5

    # The report gate asks BEFORE it claims a finalize scope (and before it buys the report).
    reporting = make_engine(tmp_path / "report")
    for row in _log(_withhold(), _RESET):
        reporting.store.append(row.type, row.data)
    reporting.report_writer, reporting.report_every, reporting.external_harness = object(), 1, False
    begun: list = []
    reporting._begin_finalize = lambda *a, **k: begun.append(a) or "scope"
    tail = reporting.store.read_all()[-1].seq
    assert reporting._finish_with_report_if_quiescent(
        fold(reporting.store.read_all()), {"reason": "done"}, after_seq=tail) is False
    assert begun == []
    assert [e.data["eval_seconds"] for e in reporting.store.read_all()
            if e.type == "node_failed"] == [1.5]


# --------------------------------------------- the canary's seconds and the salvage row (crit_v46 L1)
def _canary(seconds, passed, *, retry=0, attempt=0):
    extra = {"retry": retry} if retry else {}
    return [("eval_canary_started", {"node_id": 0, "generation": 0, "attempt": attempt,
                                     "code_digest": "d", "timeout": 900, **extra}),
            ("eval_canary_finished", {"node_id": 0, "generation": 0, "attempt": attempt,
                                      "code_digest": "d", "passed": passed, "eval_seconds": seconds,
                                      "log_dir": "/x", **extra})]


def _claim(attempt=0, **extra):
    return ("eval_invocation_claimed", {"node_id": 0, "generation": 0, "attempt": attempt,
                                        "invocation_id": f"inv-{attempt}", **extra})


def test_a_new_attempt_s_canary_ends_an_older_orphan_settle_s_window():
    """crit_v46 L1 A, driven: a dead process's 100 s settle, then the resumed attempt's canary fails
    (2 s, no invocation claimed) and a pause withholds it at DECIDE_REPAIR carrying those 2 s — the
    withhold closed the OLDER window, and the reset charged 2 s. MUTATION: drop the canary-start
    clause -> 2.0 again."""
    assert _charges(_claim(), _settle(100.0), *_canary(2.0, False), _withhold(seconds=2.0),
                    _RESET) == [(0, 0, 102.0)]
    assert _charges(_claim(), _settle(100.0), *_canary(2.0, False), _repaired(2.0),
                    _RESET) == [(0, 0, 102.0)]


def test_a_passed_canary_nothing_carried_is_charged():
    """crit_v46 L1 B, driven: a passed 900 s canary, a death mid full-eval, a reset — nothing, because
    the resume skips a passed canary by code digest. MUTATIONS: drop the canary rows from the sum or
    from `abandoned_lifecycle_charges`' spend rows -> []."""
    assert _charges(*_canary(900.0, True), _claim(), _RESET) == [(0, 0, 900.0)]
    # A resumed process repeating that invocation: its settle is its own clock, not the canary's.
    # MUTATION: let the resumed settle carry the dead process's canary -> 50.0.
    assert _charges(*_canary(900.0, True), _claim(), _claim(after_interrupted_attempt=True),
                    _settle(50.0), _RESET) == [(0, 0, 950.0)]


def test_a_canary_is_charged_exactly_once_when_its_attempt_carries_it():
    """The settle, an `after_canary` withhold, a repair row and a terminal each carry the canary that
    ran inside their attempt's clock — never a second time here. A canary RETRY is the same attempt:
    both canaries ride on its settle. MUTATIONS: charge the canary at its own settle; let a retry's
    start end the window; replace instead of accumulate across a retry."""
    assert _charges(*_canary(9.0, True), _claim(), _settle(20.0), _RESET) == [(0, 0, 20.0)]
    after = _withhold(seconds=9.0, at="after_canary")
    assert _charges(*_canary(9.0, True), after, _RESET) == [(0, 0, 9.0)]
    assert _charges(*_canary(2.0, False), _repaired(2.0), _RESET) == [(0, 0, 2.0)]
    done = ("node_evaluated", {"node_id": 0, "generation": 0, "metric": 0.5, "eval_seconds": 20.0})
    assert _charges(*_canary(9.0, True), _claim(), _settle(20.0, outcome="ok"), done, _RESET) == []
    retried = [*_canary(3.0, False), *_canary(5.0, True, retry=1)]
    assert _charges(*retried, _claim(), _settle(12.0), _RESET) == [(0, 0, 12.0)]
    assert _charges(*retried, _claim(), _RESET) == [(0, 0, 8.0)]


def test_a_salvage_cause_row_does_not_close_the_window_it_carries_nothing_for():
    """crit_v46 L1 C, driven: a 4,560 s settle, the salvage-cause fix's row (no seconds), a death and
    a reset charged nothing — and a resumed chain's seed was 0. MUTATION: let the salvage row carry."""
    from looplab.engine.evaluate import _durable_prior_seconds
    salvage = ("node_repaired", {"node_id": 0, "generation": 0, "attempt": 0, "files": {},
                                 "deleted": [], "triage_action": "salvage_cause_fix", "changed": [],
                                 "salvaged_metric": 0.7})
    assert _charges(_claim(), _settle(4560.0), salvage, _RESET) == [(0, 0, 4560.0)]
    assert _durable_prior_seconds(_log(_claim(), _settle(4560.0), salvage), 0, 0) == 4560.0
