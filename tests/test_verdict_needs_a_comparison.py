"""A record set over nothing is not support, and 36% of every board's verdicts were unearned.

FOUND BY THE RUNNING ENGINE, which is the part worth keeping. `runs/e5small-dr-unified-v5`'s card-0
read `verdict='supported'` with `best_delta=None`, one node, no parents, one node in the whole run.
The Researcher read that on its proposal board, disbelieved it in its own recorded trace — "but wait,
the card says NODES=[0] and verdict=supported … but node 0's actual code didn't implement it … that's
odd" — and spent the next proposal re-implementing what the board claimed was already done. That is
card-1, a near-duplicate of card-0, and a whole Developer build.

THE MECHANISM. `_evidence_verdict` set `supported = True` for any node in `record_setters`, and
`_record_setter_ids` deliberately includes the node that ESTABLISHES the first SOTA — which on every
run is node 0, by being the only node. So the opening hypothesis of every run was declared borne out
against nothing.

MEASURED over every event log on the box: 14 of the 39 evaluated cards — 36% — read `supported`
ONLY because of a baseless record, every one of them a parentless draft (`evidence=[0]`, `[1]`,
`[2]`, `[3]`). Six cards are supported on a REAL improvement over a parent and do not move.
"""
from __future__ import annotations

from looplab.core.models import Idea, Node, NodeStatus
from looplab.events.card_ledger import _evidence_verdict, _record_setter_ids


def _node(nid: int, metric: float, parents=()) -> Node:
    return Node(id=nid, operator="draft", idea=Idea(operator="draft"), metric=metric,
                parent_ids=list(parents), status=NodeStatus.evaluated, feasible=True)


def _verdict(nodes, evidence, direction="max"):
    by_id = {n.id: n for n in nodes}
    return _evidence_verdict(evidence, by_id, direction,
                             _record_setter_ids(by_id, direction), False)


def test_the_first_node_of_a_run_is_TESTED_and_not_supported():
    """Supported against WHAT? There is no parent and no sibling. `tested` — "evaluated without
    improvement" — is exactly true of a first measurement."""
    best_delta, status, supported = _verdict([_node(0, 0.693)], [0])
    assert status == "tested"
    assert supported is False
    assert best_delta is None, "no baseline means no delta, not a delta of zero"


def test_a_node_that_BEAT_its_parent_is_still_supported():
    """The clause keeps its real job. This is the case the rule was written for."""
    nodes = [_node(0, 0.70), _node(1, 0.75, parents=[0])]
    best_delta, status, supported = _verdict(nodes, [1])
    assert status == "supported" and supported is True
    assert best_delta is not None and best_delta > 0


def test_the_record_stays_STICKY_after_something_overtakes_it():
    """The other half of the clause's job, and the board bug its own comment describes: a card must
    not flip supported->tested the moment a later node scores higher."""
    nodes = [_node(0, 0.70), _node(1, 0.75, parents=[0]), _node(2, 0.80, parents=[0])]
    _, status, supported = _verdict(nodes, [1])
    assert status == "supported" and supported is True, (
        "node 1 beat its parent and set the record at the time; node 2 overtaking it later does not "
        "un-make that")


def test_a_parentless_node_that_is_NOT_the_record_holder_is_also_tested():
    nodes = [_node(0, 0.90), _node(1, 0.50)]          # node 1 has no parent and beats nothing
    _, status, supported = _verdict(nodes, [1])
    assert status == "tested" and supported is False


def test_direction_min_reads_the_same_way():
    """The rule is direction-aware everywhere else; a first measurement is unearned in both."""
    _, status, supported = _verdict([_node(0, 0.10)], [0], direction="min")
    assert status == "tested" and supported is False
    nodes = [_node(0, 0.30), _node(1, 0.10, parents=[0])]
    _, status, supported = _verdict(nodes, [1], direction="min")
    assert status == "supported" and supported is True


def test_a_card_with_evidence_that_never_evaluated_stays_open():
    """Untouched by this change, and pinned so the tightening cannot have widened `open`."""
    pending = Node(id=0, operator="draft", idea=Idea(operator="draft"), status=NodeStatus.pending)
    _, status, supported = _evidence_verdict([0], {0: pending}, "max", set(), False)
    assert status == "testing" and supported is False
