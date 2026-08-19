"""The seed-boundary gate: an EQUALITY against a striding count is a phase that can never fire.

`cadence.py`'s header already states why `n % every == 0` is wrong — the node count advances in
strides of k > 1 under fan-out and speculative prefetch — but BOTH consumers of the gate still spelled
their first-ever firing as `n == self.n_seeds`, which has the identical hole. For the Strategist,
`strategist_every=3` covers a missed boundary within three nodes and nobody noticed. For concept
tagging, whose interval was longer than any run on this corpus, the boundary was the ONLY firing there
would ever be — so stepping over it cancelled the whole Part IV/V subsystem.

Measured 2026-08-11 (`node_concepts` events over whole runs): rubert-dr-0807 14 nodes / n_seeds 1 -> 1;
lt-recovery-0811 6 nodes / n_seeds 3 -> 0; rubertlite-dr-unified-v2 3 nodes / n_seeds 3 -> 0. The
6-node run consulted its Strategist 28 times over the same gate, so nothing else was blocking.
"""
from __future__ import annotations

from looplab.engine.cadence import cadence_due, cadence_marks, seed_boundary_due


def test_a_stride_over_the_seed_boundary_still_fires():
    # The exact shape that lost it: n_seeds=3 and a build that steps 2 -> 4.
    assert seed_boundary_due(4, 0, 3) is True
    assert seed_boundary_due(3, 0, 3) is True, "landing exactly on it must still work"
    assert seed_boundary_due(2, 0, 3) is False, "before the boundary is not due"


def test_it_fires_at_most_once_and_never_advances_a_running_consumer():
    # `last > 0` means the consumer has a durable record of firing; from then on the interval owns it.
    assert seed_boundary_due(9, 3, 3) is False
    assert seed_boundary_due(30, 1, 3) is False
    # …and the interval gate is what takes over, unchanged.
    assert cadence_due(9, 3, 5) is True
    assert cadence_due(7, 3, 5) is False


def test_an_empty_run_is_never_due():
    assert seed_boundary_due(0, 0, 3) is False
    assert seed_boundary_due(0, 0, 0) is False


def test_a_zero_or_junk_seed_count_settles_to_the_first_node():
    """`n_seeds` reaches this from a resumed snapshot and from partially built Engines in tests. A
    zero must mean "the first node", not "already past the boundary at n=0"."""
    assert seed_boundary_due(1, 0, 0) is True
    assert seed_boundary_due(1, 0, None) is True


def test_the_concept_gate_survives_a_stride_and_the_strategy_gate_still_agrees():
    """Both engine gates go through the one helper, driven here on their real call shape."""
    from looplab.engine.concept_cadence import ConceptCadenceMixin
    from looplab.engine.strategy import StrategyCadenceMixin

    class _Fake(ConceptCadenceMixin, StrategyCadenceMixin):
        n_seeds = 3
        strategist_every = 3
        concept_retag_every = 5

    class _State:
        def __init__(self, n, pending=False):
            self.nodes = {i: object() for i in range(n)}
            self._pending = pending

        def pending_nodes(self):
            return [object()] if self._pending else []

    engine = _Fake()
    # n stepped 2 -> 4 over the boundary; both gates must still fire their first time.
    assert engine._should_consult_concepts(_State(4), marks=[]) is True
    assert engine._should_consult(_State(4), marks=[]) is True
    # Once recorded, the boundary cannot re-fire; only the interval can.
    fired = [{"at_node": 4}]
    assert cadence_marks(fired) == 4
    assert engine._should_consult_concepts(_State(6), marks=fired) is False
    assert engine._should_consult_concepts(_State(9), marks=fired) is True   # 9 - 4 >= 5
    # An in-flight evaluation closes both gates ONLY under the historical predicate, which is what
    # `_Fake` gets by omitting `_cadence_while_evaluating` (the getattr default is False). That is
    # deliberate here: this file is about the seed boundary, so it pins the pre-F1i behaviour as a
    # negative control and `tests/test_cadence_while_evaluating.py` owns the other branch.
    assert engine._should_consult_concepts(_State(4, pending=True), marks=[]) is False
    engine._cadence_while_evaluating = True
    assert engine._should_consult_concepts(_State(4, pending=True), marks=[]) is True
    assert engine._should_consult(_State(4, pending=True), marks=[]) is True
    del engine._cadence_while_evaluating


def test_the_shipped_retag_interval_fires_more_than_once_on_a_real_run_length():
    """The other half of the defect: an interval longer than the run means one pass, ever.

    Run lengths on this corpus are 3, 6 and 14 nodes. Pinning the DEFAULT rather than a literal, so
    this fails if someone raises it back above a real run.
    """
    from looplab.core.config import Settings

    every = Settings().concept_retag_every
    assert every <= 7, f"concept_retag_every={every} is longer than a real run here"
    fired_at, n, last = [], 0, 0
    for n in range(1, 15):                      # a 14-node run, one node at a time
        if seed_boundary_due(n, last, 3) or cadence_due(n, last, every):
            fired_at.append(n)
            last = n
    assert len(fired_at) >= 3, f"only fired at {fired_at} over 14 nodes"
