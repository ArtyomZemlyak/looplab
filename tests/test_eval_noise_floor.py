"""The eval NOISE FLOOR (doc 52 row 11): a run CAN record the repeated-seed spread of one
candidate's metric, and does when asked.

DRIVEN, not pinned. Every assertion below comes from a REAL toy run's own `events.jsonl` — the
probe's repeats really re-materialize a workdir and really run the eval — because the marker this
closes is about what a RUN records, and a source scan cannot tell "the phase is wired into the
ladder" from "the phase is spelled in a comment above the ladder". The one exception is
`noise_floor_summary`, whose truth table is checked directly: it is the instrument's whole claim
and the one thing a reader must be able to check without an engine.

WHAT IS NOT HERE, deliberately: the NUMBER. The toy quadratic is deterministic, so its floor is
0.0 — a real property of that evaluation and a useless one for calibrating a champion's margin.
What the spread IS on a GPU-graded task is a box measurement (doc 52 row 11's arm), and no test
here may be read as having produced it.
"""
from __future__ import annotations

import shutil

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.config import Settings
from looplab.core.fitness import standard_error_difference
from looplab.engine.noise_floor import noise_floor_summary
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import (ALL_EVENT_TYPES, DIAGNOSTIC_EVENTS, EV_EVAL_NOISE_FLOOR,
                                  EV_EVAL_NOISE_SEED, EV_NODE_EVALUATED, EV_NODE_FAILED)
from tests.factories import TOY_TASK, make_engine


def _run(run_dir, **overrides):
    task = ToyTask.load(TOY_TASK)
    engine = make_engine(run_dir, task=task, n_seeds=3, max_nodes=6, **overrides)
    anyio.run(engine.run)
    return engine


def _rows(engine, etype):
    return [e.data for e in engine.store.read_all() if e.type == etype]


# --------------------------------------------------------------------------- the pure summary


def test_the_summary_is_the_gates_own_arithmetic():
    """`sem` must be the number `trust/gate.py::one_se_better` puts on one side of its comparison —
    otherwise the floor is recorded on a scale the gate does not use, which is the whole point of
    recording it. Checked against `standard_error_difference` itself, not against a re-derivation."""
    summary = noise_floor_summary([1.0, 2.0, 3.0])
    assert summary["n"] == 3
    assert summary["mean"] == pytest.approx(2.0)
    assert summary["std"] == pytest.approx(1.0)          # sample (Bessel) std, as confirm uses
    assert summary["spread"] == pytest.approx(2.0)
    assert summary["sem"] == pytest.approx(
        standard_error_difference(summary["std"], summary["n"], 0.0, 0))


def test_a_probe_that_measured_nothing_reports_no_floor_rather_than_zero():
    """A floor of 0.0 is the STRONGEST possible claim about an evaluation ("every margin is real").
    Fewer than two usable numbers must never mint it."""
    assert noise_floor_summary([])["std"] is None
    assert noise_floor_summary([])["n"] == 0
    assert noise_floor_summary([1.5])["std"] is None     # one number has no spread
    assert noise_floor_summary([1.5])["mean"] == pytest.approx(1.5)
    assert noise_floor_summary([None, None])["sem"] is None
    # A measured zero is a different answer from an unmeasured one, and both must be reachable.
    assert noise_floor_summary([2.0, 2.0])["std"] == pytest.approx(0.0)


def test_the_repeats_ignore_a_seed_that_produced_no_metric():
    """A failed repeat is not evidence of a small spread. It drops out of n, and the summary is
    the spread of what actually ran — the same rule the confirm phase applies to a failed seed."""
    assert noise_floor_summary([1.0, None, 3.0])["n"] == 2
    assert noise_floor_summary([1.0, None, 3.0])["spread"] == pytest.approx(2.0)


# ----------------------------------------------------------------- the mechanism, on a real run


def test_a_run_records_the_repeated_seed_spread_of_one_candidate(tmp_path):
    """THE MARKER'S OWN SENTENCE, driven: the run's log carries one repeat per seed and one summary
    that agrees with them."""
    engine = _run(tmp_path / "on", eval_noise_seeds=3)
    seeds = _rows(engine, EV_EVAL_NOISE_SEED)
    floors = _rows(engine, EV_EVAL_NOISE_FLOOR)

    assert len(floors) == 1, "the floor is measured once per run"
    floor = floors[0]
    assert [row["seed"] for row in seeds] == [0, 1, 2], (
        "the repeats run the SEARCH's own seeds (0 first: it re-measures the exact configuration "
        "the search scored), not confirm's disjoint base")
    assert {row["node_id"] for row in seeds} == {floor["node_id"]}, "one candidate, not top-k"
    state = fold(engine.store.read_all())
    assert floor["node_id"] == state.best_node_id, "the champion is what a margin is asked about"
    assert floor["search_metric"] == state.nodes[floor["node_id"]].metric

    measured = [row["metric"] for row in seeds if row["metric"] is not None]
    assert measured, "the toy eval must actually have run under the probe"
    assert floor["metrics"] == [row["metric"] for row in seeds]
    assert floor["n"] == len(measured)
    assert floor["mean"] == pytest.approx(noise_floor_summary(measured)["mean"])
    assert floor["sem"] == pytest.approx(noise_floor_summary(measured)["sem"])
    # The toy quadratic is deterministic, so this run's floor is a measured ZERO. That is the
    # mechanism working, not the number the marker still owes.
    assert floor["spread"] == pytest.approx(0.0)


def test_the_floor_is_folded_and_readable_back_off_the_log(tmp_path):
    """A record nothing can read back is not a record. `fold` is the only producer of `RunState`,
    so the probe's answer has to survive it — including the per-seed memo a resume reads."""
    engine = _run(tmp_path / "fold", eval_noise_seeds=3)
    state = fold(engine.store.read_all())
    floor = state.eval_noise_floor
    assert floor is not None
    assert floor["n"] == 3 and floor["std"] == pytest.approx(0.0)
    assert set(state.eval_noise_seed_results[floor["node_id"]]) == {0, 1, 2}
    # Order tolerance and idempotence (invariant 5): the same rows in a different order, and the
    # same rows twice, fold to the same answer — the probe charges its seconds exactly once.
    events = engine.store.read_all()
    assert fold(events).eval_noise_floor == floor
    doubled = fold(events + [e for e in events if e.type == EV_EVAL_NOISE_SEED])
    assert doubled.eval_seconds_by_kind == fold(events).eval_seconds_by_kind
    assert doubled.eval_noise_floor == floor


def test_the_probe_charges_its_seconds_to_its_own_budget_bucket(tmp_path):
    """The repeats are real evaluations and the run's budget must say so — under `noise`, beside
    `node` and `confirm`, so "where did the compute go" stays answerable."""
    engine = _run(tmp_path / "budget", eval_noise_seeds=3)
    state = fold(engine.store.read_all())
    assert state.eval_seconds_by_kind.get("noise", 0.0) > 0.0
    assert state.total_eval_seconds == pytest.approx(sum(state.eval_seconds_by_kind.values()))


def test_a_repeat_never_becomes_a_second_terminal_for_its_node(tmp_path):
    """INVARIANT 2, the reason the repeats live outside the node lifecycle: exactly one
    `node_evaluated`/`node_failed` per node, with the probe on."""
    engine = _run(tmp_path / "terminals", eval_noise_seeds=3)
    terminals: dict[int, int] = {}
    for event in engine.store.read_all():
        if event.type in (EV_NODE_EVALUATED, EV_NODE_FAILED):
            terminals[event.data["node_id"]] = terminals.get(event.data["node_id"], 0) + 1
    assert terminals, "the run evaluated nothing, so this proves nothing"
    assert set(terminals.values()) == {1}, f"a node got two terminals: {terminals}"


def test_off_is_the_default_and_records_nothing(tmp_path):
    """OFF must leave the run exactly as it was: `Settings` ships 0, and a default run's log carries
    no row of either type and no folded state."""
    assert Settings().eval_noise_seeds == 0
    engine = _run(tmp_path / "off")
    assert _rows(engine, EV_EVAL_NOISE_SEED) == []
    assert _rows(engine, EV_EVAL_NOISE_FLOOR) == []
    state = fold(engine.store.read_all())
    assert state.eval_noise_floor is None and state.eval_noise_seed_results == {}
    assert "noise" not in state.eval_seconds_by_kind


def test_one_repeat_is_off_too_because_one_number_has_no_spread(tmp_path):
    """The setting's own stated rule, held at the Engine rather than re-decided per caller."""
    engine = _run(tmp_path / "one", eval_noise_seeds=1)
    assert engine.eval_noise_seeds == 0
    assert _rows(engine, EV_EVAL_NOISE_FLOOR) == []


def test_a_completed_pass_gates_itself_and_is_never_bought_twice(tmp_path):
    """Invariant 3: the summary row IS the gate. Re-entering the phase on a folded state that
    already carries a floor must run no repeat and append no row — which is what a resume of a
    finished run does on every loop turn."""
    engine = _run(tmp_path / "gate", eval_noise_seeds=3)
    state = fold(engine.store.read_all())
    assert engine._noise_floor_due(state) is False, "the ladder would re-enter a finished probe"
    before = len(_rows(engine, EV_EVAL_NOISE_SEED))
    # Called anyway, because the gate is what makes the re-entry cheap and not what makes it safe:
    # a phase entered by mistake must still buy no second evaluation and leave one summary.
    anyio.run(engine._noise_floor_phase, state)
    assert len(_rows(engine, EV_EVAL_NOISE_SEED)) == before, "a re-entered probe paid again"
    assert len(_rows(engine, EV_EVAL_NOISE_FLOOR)) == 1, "a second floor row for one run"


def test_a_resumed_pass_does_not_re_run_the_repeats_it_already_paid_for(tmp_path):
    """The per-seed memo, driven: over a log that a crash left with two of three repeats and no
    summary, a fresh pass runs exactly the missing one. This is the difference between a crash
    costing one repeat and costing the whole probe — and a repeat is a full evaluation."""
    engine = _run(tmp_path / "resume", eval_noise_seeds=3)
    node_id = _rows(engine, EV_EVAL_NOISE_FLOOR)[0]["node_id"]

    # The exact log a crash between repeat 2 and repeat 3 leaves behind: everything except the
    # summary row and the last repeat, in its own copy of the run directory (the node workdirs
    # come with it, so `_materialize` has the same tree the first pass had).
    resumed = tmp_path / "resumed"
    shutil.copytree(tmp_path / "resume", resumed)
    kept = [e for e in engine.store.read_all()
            if not (e.type == EV_EVAL_NOISE_FLOOR
                    or (e.type == EV_EVAL_NOISE_SEED and e.data.get("seed") == 2))]
    # RE-APPENDED through the store rather than hand-edited: `seq` is contiguous by construction
    # and the corruption stop is a real guard, not something to write around.
    (resumed / "events.jsonl").unlink()
    replayed = EventStore(resumed / "events.jsonl")
    for event in kept:
        replayed.append(event.type, event.data)

    engine2 = make_engine(resumed, task=ToyTask.load(TOY_TASK), n_seeds=3, max_nodes=6,
                          eval_noise_seeds=3)
    state = fold(engine2.store.read_all())
    assert set(state.eval_noise_seed_results[node_id]) == {0, 1}
    assert engine2._noise_floor_due(state) is True
    anyio.run(engine2._noise_floor_phase, state)
    assert sorted(row["seed"] for row in _rows(engine2, EV_EVAL_NOISE_SEED)) == [0, 1, 2], (
        "the resumed pass re-ran a repeat it had already paid for")
    assert len(_rows(engine2, EV_EVAL_NOISE_FLOOR)) == 1


def test_both_types_are_registered_and_folded(tmp_path):
    """Invariant 7 and the fold/diagnostic partition, stated where the instrument is: these two
    are FOLDED (one is a resume memo, the other a completion gate), so neither may be diagnostic."""
    assert {EV_EVAL_NOISE_SEED, EV_EVAL_NOISE_FLOOR} <= ALL_EVENT_TYPES
    assert not ({EV_EVAL_NOISE_SEED, EV_EVAL_NOISE_FLOOR} & DIAGNOSTIC_EVENTS)
