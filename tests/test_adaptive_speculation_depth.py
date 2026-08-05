"""AUTO speculation depth adapts to the run's OWN measured evaluation duration.

`speculation_depth = -1` (AUTO, the shipped default) used to resolve once, at startup, from the
settled eval width — how many experiments can run at ONCE. That answers a capacity question, not the
one that decides whether a prefetch pays: a prefetch exists to overlap the Developer's PROVIDER
latency with a RUNNING evaluation, and when the evaluation takes 0.1 s there is nothing to overlap.

Measured on `examples/classification_task.json` AS IT SHIPPED BEFORE 2026-08-05 (the flat two-blob
variant; that example is now the concentric-rings task, which evaluates in 0.05-0.6 s — still far
under provider latency, so the conclusion carries), same command, both arms 8/8 nodes and the
identical champion: AUTO -> depth 1 cost **109 LLM calls / 1,265,911 tokens / 2348.8 s** against
`speculation_depth=0`'s **75 calls / 817,201 tokens / 2448.6 s** — 45% more calls and 55% more tokens
for a 4% wall-clock saving, and not from wrong predictions (one stale prefetch in nine requests).

What these tests pin is the mechanism: the depth may move, every move is a durable event, the fold
READS that event rather than re-deriving anything from the box, and the movement is a one-way ratchet
that cannot thrash.
"""
from __future__ import annotations

import json

import pytest

from looplab.core.models import NodeStatus, RunState
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.events.types import EV_SPECULATION_DEPTH_SETTLED
from tests.factories import make_engine


def _log(run_dir, *, depth: int, evals: list[float], builds: list[float]) -> None:
    """Write a run log whose build spans and eval seconds are exactly what this test measures.

    The event `ts` is what `_measured_build_seconds` reads, and `EventStore.append` stamps it from
    the clock — so the log is written directly here rather than through the store. That is also the
    honest shape for this property: the rule must be a pure function of a LOG, including one written
    by another process on another day.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = [{"v": 1, "seq": 0, "ts": 0.0, "type": "run_started",
             "data": {"run_id": run_dir.name, "task_id": "toy", "goal": "g", "direction": "min",
                      "card_driven_selection": True, "speculation_depth": depth}}]
    seq = 1
    for index, span in enumerate(builds):
        base = 1000.0 + index * 1000.0
        rows.append({"v": 1, "seq": seq, "ts": base, "type": "node_building",
                     "data": {"node_id": index, "operator": "draft", "parent_ids": []}})
        seq += 1
        rows.append({"v": 1, "seq": seq, "ts": base + span, "type": "node_created",
                     "data": {"node_id": index, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}}, "code": "print(1)"}})
        seq += 1
    for index, seconds in enumerate(evals):
        rows.append({"v": 1, "seq": seq, "ts": 9000.0 + index,
                     "type": "node_evaluated",
                     "data": {"node_id": index, "generation": 0, "metric": 1.0,
                              "eval_seconds": seconds}})
        seq += 1
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows))


def _engine(tmp_path, name, *, auto: bool, depth: int = 1,
            evals: list[float] | None = None, builds: list[float] | None = None):
    run_dir = tmp_path / name
    _log(run_dir, depth=depth, evals=evals or [], builds=builds or [])
    engine = make_engine(run_dir, card_driven_selection=True, max_nodes=8)
    engine.speculation_depth = depth
    engine._speculation_depth_auto = auto
    return engine


def _measured(engine, **_ignored) -> RunState:
    return fold(engine.store.read_all())


def test_a_spelled_depth_is_never_settled_away(tmp_path):
    """AUTO-only, like every other AUTO settling rule: an operator who SPELLED a depth gets it."""
    engine = _engine(tmp_path, "spelled", auto=False,
                     evals=[0.1, 0.1, 0.1], builds=[120.0, 130.0, 125.0])
    state = _measured(engine)
    assert engine._settle_speculation_depth(state) is False
    assert engine.speculation_depth == 1
    assert not [e for e in engine.store.read_all() if e.type == EV_SPECULATION_DEPTH_SETTLED]


def test_one_fast_node_cannot_switch_off_the_treatment(tmp_path):
    engine = _engine(tmp_path, "one-sample", auto=True, evals=[0.1], builds=[120.0])
    state = _measured(engine)
    assert engine._settle_speculation_depth(state) is False
    assert engine.speculation_depth == 1


def test_evals_too_short_to_hide_a_build_ratchet_the_depth_to_zero(tmp_path):
    # The live fast side: ~0.1 s evals against builds measured in minutes.
    engine = _engine(tmp_path, "fast-evals", auto=True,
                     evals=[0.11, 0.09, 0.12], builds=[131.0, 147.0, 122.0])
    state = _measured(engine)
    assert engine._settle_speculation_depth(state) is True
    assert engine.speculation_depth == 0

    rows = [e for e in engine.store.read_all() if e.type == EV_SPECULATION_DEPTH_SETTLED]
    assert len(rows) == 1
    data = rows[0].data
    assert data["depth"] == 0 and data["previous"] == 1
    # The evidence must be complete enough that the fold never has to re-measure anything.
    evidence = data["evidence"]
    assert evidence["eval_samples"] == 3 and evidence["build_samples"] == 3
    assert evidence["median_eval_seconds"] == pytest.approx(0.11)
    assert evidence["median_build_seconds"] == pytest.approx(131.0)
    assert evidence["eval_fraction_of_build"] < evidence["min_eval_fraction"]
    # …and the fold adopts it.
    assert fold(engine.store.read_all()).speculation_depth == 0


def test_evals_long_enough_to_hide_a_build_keep_the_prefetch(tmp_path):
    """The slow side. A prefetch here overlaps real latency, which is what AUTO turned it on for."""
    engine = _engine(tmp_path, "slow-evals", auto=True,
                     evals=[600.0, 540.0, 660.0], builds=[131.0, 147.0, 122.0])
    state = _measured(engine)
    assert engine._settle_speculation_depth(state) is False
    assert engine.speculation_depth == 1
    assert fold(engine.store.read_all()).speculation_depth == 1


def test_the_threshold_is_a_ratio_not_an_absolute_number_of_seconds(tmp_path):
    """A run whose builds are slow because the ENDPOINT is slow should keep prefetching at eval
    durations a fast endpoint would not justify — "fast" only means anything relative to the provider
    latency the overlap is supposed to hide."""
    quick = _engine(tmp_path, "quick-endpoint", auto=True,
                    evals=[2.0, 2.0], builds=[5.0, 5.0])
    assert quick._settle_speculation_depth(_measured(quick)) is False   # 40% of a build
    slow = _engine(tmp_path, "slow-endpoint", auto=True,
                   evals=[2.0, 2.0], builds=[900.0, 900.0])
    assert slow._settle_speculation_depth(_measured(slow)) is True      # 0.2% of a build


def test_the_fold_takes_the_minimum_so_it_is_order_tolerant_and_idempotent(tmp_path):
    """Invariant #5. A ratchet folded as last-write-wins would land on a different treatment when two
    rows are spliced in the other order, and a duplicated stale row could raise the depth back up."""
    engine = _engine(tmp_path, "fold-order", auto=True, depth=4)
    engine.store.append(EV_SPECULATION_DEPTH_SETTLED, {"depth": 2, "previous": 4})
    engine.store.append(EV_SPECULATION_DEPTH_SETTLED, {"depth": 1, "previous": 2})
    assert fold(engine.store.read_all()).speculation_depth == 1
    engine.store.append(EV_SPECULATION_DEPTH_SETTLED, {"depth": 2, "previous": 4})   # replayed/stale
    assert fold(engine.store.read_all()).speculation_depth == 1
    # A malformed row cannot change the search treatment at all.
    for bad in ({"depth": True}, {"depth": "0"}, {"depth": -1}, {"depth": 65}, {}):
        engine.store.append(EV_SPECULATION_DEPTH_SETTLED, bad)
    assert fold(engine.store.read_all()).speculation_depth == 1


def test_a_log_with_no_settle_rows_keeps_exactly_its_pinned_depth(tmp_path):
    """Reader-side default (invariant #5): every pre-existing log behaves as it always did."""
    engine = _engine(tmp_path, "legacy", auto=True, depth=3)
    assert fold(engine.store.read_all()).speculation_depth == 3


def test_a_resume_adopts_the_last_recorded_depth_not_the_box(tmp_path):
    """The property the startup pin was protecting, kept — by reading the LOG rather than re-deriving.

    A resume on a box with a different eval width must continue this run's own search treatment.
    `_require_pinned_speculation_receipt` compares against the FOLDED depth, which now carries every
    settle row, so the second process adopts 0 rather than re-resolving 1 off its own hardware.
    """
    engine = _engine(tmp_path, "resume", auto=True, evals=[0.1, 0.1], builds=[120.0, 120.0])
    state = _measured(engine)
    assert engine._settle_speculation_depth(state) is True

    resumed = make_engine(tmp_path / "resume", card_driven_selection=True, max_nodes=8)
    resumed.speculation_depth = 1          # what a fresh AUTO resolution off this box would give
    resumed._speculation_depth_auto = True
    entry = fold(resumed.store.read_all())
    assert entry.speculation_depth == 0
    resumed.speculation_depth = entry.speculation_depth   # `_reentry_repin`'s adoption
    assert resumed._speculation_enabled() is False


def test_the_ratchet_only_ever_goes_down(tmp_path):
    """A second settle can never re-enable speculation, in the engine or in the fold."""
    engine = _engine(tmp_path, "ratchet", auto=True, evals=[0.1, 0.1], builds=[120.0, 120.0])
    state = _measured(engine)
    assert engine._settle_speculation_depth(state) is True
    # Now the evals look slow (a genuinely long node lands later). The depth must NOT come back.
    engine.speculation_depth = 0
    slow = fold(engine.store.read_all())
    assert engine._settle_speculation_depth(slow) is False
    assert fold(engine.store.read_all()).speculation_depth == 0


def test_the_ratchet_never_fires_while_a_prefetch_is_outstanding(tmp_path):
    """Turning the depth to 0 also turns off the lane that SERVES an open request.

    `_speculation_enabled()` gates `_run_card_session`, `_serve_card_builds` and the whole request
    queue. Settling with a head request open would leave that request holding a physical node
    reservation with nothing left able to close it — the budget leaks and the run stalls, which is the
    same shape as the defect this sits beside. The ratchet is therefore quiescent-only.
    """
    engine = _engine(tmp_path, "quiescent", auto=True, evals=[0.1, 0.1], builds=[120.0, 120.0])
    state = _measured(engine)

    engine.store.append("card_added", {"id": "card-0", "statement": "s", "source": "researcher",
                                       "at_node": 0})
    engine.store.append("card_build_requested", {"card_id": "card-0", "generation": 0})
    with_request = fold(engine.store.read_all())
    assert engine._head_request(with_request) is not None
    assert engine._settle_speculation_depth(with_request) is False
    assert engine.speculation_depth == 1

    # …and an in-flight producer in THIS process blocks it too, even with the queue closed.
    engine.store.append("card_build_done", {"card_id": "card-0", "generation": 0,
                                            "skipped": "producer_failed"})
    closed = fold(engine.store.read_all())
    engine._ensure_speculation_state()
    engine._spec_build_inflight.add(("card-0", 0))
    assert engine._settle_speculation_depth(closed) is False
    engine._spec_build_inflight.discard(("card-0", 0))
    assert engine._settle_speculation_depth(closed) is True
