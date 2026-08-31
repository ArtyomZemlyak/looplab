"""A cited experiment that EXISTS and is still running is not a fabrication.

`_evidence_snapshot` admits a cited node only at a TERMINAL lifecycle (`evaluated` / `failed`,
non-tombstoned, non-aborted), which is right: a claim cannot be evidenced by an experiment that has
produced no number. But when NO cited id cleared that bar, `_check_claims` wrote ONE sentence —
"cited experiments do not exist" — for two facts with OPPOSITE remedies. Absent means the model
invented an id and must stop inventing them; still-running means the citation was ACCURATE and
merely premature.

MEASURED over every event log preserved on this box: 259 such notes carrying 397 cited ids, of which
**169 (42.6 %) named a node that existed and was mid-eval at that memo's own timestamp**, spread
across ALL ELEVEN runs that produced a memo (2 to 43 each). The other 228 are genuinely absent.

Live on `e5small-dr-unified-v11`: memo0 at 14:29:27 cited [1, 13] when the run had zero nodes;
memo2 at 16:55:43 cited [0, 1], created 15:23:37 and 16:47:07, both real and both mid-eval — the
memo's own summary says "both still pending drafts". One sentence called both fabrications.

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.trust.memo_verify import _cited_node_partition, _cited_nodes_note


def _node(nid, status, *, tombstoned=False):
    return Node(id=nid, parent_id=None, operator="draft", code="",
                idea=Idea(operator="draft", params={}, rationale="r"),
                status=status, tombstoned=tombstoned)


def _state(nodes, aborted=()):
    st = RunState(nodes={n.id: n for n in nodes})
    if aborted:
        st.aborted_nodes = list(aborted)
    return st


def test_a_PENDING_cited_node_is_not_reported_as_nonexistent():
    """The defect, and 42.6 % of the corpus. Mutation: send every non-terminal id down the absent
    branch and the Researcher is told a correct citation was invented."""
    st = _state([_node(0, NodeStatus.pending), _node(1, NodeStatus.pending)])
    absent, pending = _cited_node_partition([0, 1], st)
    assert absent == [] and pending == [0, 1]
    note = _cited_nodes_note([0, 1], st)
    assert "have no result yet: [0, 1]" in note
    assert "do not exist" not in note, (
        "the whole point: a node that exists must never be called nonexistent")


def test_a_TRULY_ABSENT_id_keeps_the_original_sentence():
    """The 57.4 % that were right all along. Mutation: route absent ids to the new wording and the
    model is told to WAIT for an experiment that will never appear."""
    st = _state([_node(0, NodeStatus.pending)])
    note = _cited_nodes_note([13], st)
    assert note == "cited experiments do not exist: [13]"
    assert "no result yet" not in note


def test_a_MIXED_citation_says_BOTH_and_keeps_the_ids_apart():
    """v11 memo0 vs memo2 in one claim. Mutation: emit only the first clause and half the citation's
    diagnosis is silently dropped."""
    st = _state([_node(0, NodeStatus.pending)])
    note = _cited_nodes_note([0, 13], st)
    assert "do not exist: [13]" in note and "have no result yet: [0]" in note, note


def test_a_TOMBSTONED_node_is_ABSENT_not_pending():
    """That lifecycle is gone, so telling the model to wait for it is the inverse error. Mutation:
    treat presence in `state.nodes` as enough and a deleted node reads as merely slow."""
    st = _state([_node(0, NodeStatus.pending, tombstoned=True)])
    absent, pending = _cited_node_partition([0], st)
    assert absent == [0] and pending == []


def test_an_ABORTED_node_is_ABSENT_not_pending():
    """Mirrors `_evidence_snapshot`'s own `nid in aborted` clause. Mutation: drop the aborted check
    here and the two disagree — a partition that contradicts the snapshot is as false as the note
    it replaces."""
    st = _state([_node(0, NodeStatus.pending)], aborted=(0,))
    absent, pending = _cited_node_partition([0], st)
    assert absent == [0] and pending == []


def test_an_EVALUATED_node_partitions_as_pending_and_never_reaches_this_note():
    """A terminal node populates `node_refs`, so `_check_claims` takes the `cited` branch and this
    sentence is never built for it. The partition still reports it as present — the note's job is
    only to explain ids that produced no evidence, and inventing a third bucket here would be a
    claim about a case this function is never handed."""
    st = _state([_node(0, NodeStatus.evaluated)])
    absent, pending = _cited_node_partition([0], st)
    assert absent == [] and pending == [0]


def test_an_EMPTY_partition_still_produces_a_sentence():
    """Defensive: `_check_claims` only calls this with a non-empty `nids`, but a note that could
    render as the empty string would print a verdict with no reason at all. Mutation: drop the
    fallback and the note collapses to ''."""
    st = _state([])
    assert _cited_nodes_note([], st) == "cited experiments do not exist: []"
