"""The Researcher works per CARD; nothing recorded that until now.

The product model is that the Researcher proposes a hypothesis (a Card) while the Developer builds
and the sandbox evaluates NODES, and one card can carry several nodes. Measured on
`runs/rubert-dr-0807` (2026-08-11) before this existed: all 15 `propose` spans carry an EMPTY
attribute map, no span anywhere in the run carries a card id, and every `card_added` /
`card_build_requested` / `card_build_done` / `card_enriched` event has `trace_id: None`. So "show me
the research behind this card" had no join to answer it, through spans or events — the only reason
it went unnoticed is that no surface had ever asked.
"""
from __future__ import annotations

from looplab.core.models import Idea
from looplab.engine.orchestrator import stamp_proposal_span


class _Span:
    def __init__(self):
        self.attributes = {}

    def set(self, key, value):
        self.attributes[key] = value
        return self


def test_the_card_id_reaches_the_span_so_the_join_exists_at_all():
    span = _Span()
    stamp_proposal_span(span, Idea(operator="improve", params={}, card_id="card-3"), node_id=7)
    assert span.attributes["card_id"] == "card-3"
    assert span.attributes["operator"] == "improve"


def test_the_node_is_recorded_as_CONTEXT_and_never_as_node_id():
    """`node_id` is the key `traceview.effective_node_id` projects a whole trace by. Spelling it
    here would move every Researcher trace into ONE node's per-node view, and a card's research
    belongs to all of its nodes — not to whichever was prepared first."""
    span = _Span()
    stamp_proposal_span(span, Idea(operator="draft", params={}, card_id="card-0"), node_id=4)
    assert span.attributes["proposed_for_node"] == 4
    assert "node_id" not in span.attributes


def test_an_idea_with_no_card_stamps_nothing_rather_than_inventing_a_link():
    span = _Span()
    stamp_proposal_span(span, Idea(operator="merge", params={}), node_id=2)
    assert "card_id" not in span.attributes
    assert span.attributes["proposed_for_node"] == 2


def test_junk_never_reaches_a_durable_span():
    span = _Span()
    stamp_proposal_span(span, Idea(operator="draft", params={}, card_id="   "), node_id=True)
    assert "card_id" not in span.attributes
    assert "proposed_for_node" not in span.attributes, "a bool is an int; True must not mean node 1"

    span = _Span()
    stamp_proposal_span(span, Idea(operator="draft", params={}, card_id="card-1"), node_id=-1)
    assert "proposed_for_node" not in span.attributes
    assert span.attributes["card_id"] == "card-1"


def test_a_missing_span_is_a_no_op_not_a_crash():
    """This runs inside the proposal path; a stamp must never be able to fail a run."""
    stamp_proposal_span(None, Idea(operator="draft", params={}, card_id="card-1"))


def test_no_idea_still_records_the_node_and_never_invents_a_card():
    """The re-proposal path passes None ON PURPOSE. Its `node.idea.card_id` is the card that path is
    about to DROP (it is handed to `_plan_native_card` as `superseded_card_id`), so stamping it would
    file the research that REPLACED a card as the research that produced it — the one mis-attribution
    a card trace must never make."""
    span = _Span()
    stamp_proposal_span(span, None, node_id=6)
    assert span.attributes == {"proposed_for_node": 6}


def test_the_re_proposal_site_passes_no_idea():
    import inspect

    from looplab.engine import orchestrator

    source = inspect.getsource(orchestrator)
    assert "stamp_proposal_span(_span, None, node_id=node.id)" in source
    assert "stamp_proposal_span(_span, node.idea" not in source, (
        "stamping the node's CURRENT idea there files a re-proposal under the card it replaced")


def test_every_propose_span_site_stamps():
    """FOUR call sites open a `propose` span — draft, debug, improve, and the re-proposal a node
    reset performs. A site that opens one and forgets the stamp is a Researcher trace that silently
    loses its card, which is the whole defect this file exists for, and no behavioural test would
    catch it on the other three paths."""
    import inspect

    from looplab.engine import orchestrator

    source = inspect.getsource(orchestrator)
    opened = source.count('self.tracer.span("propose") as _span')
    unbound = source.count('self.tracer.span("propose"):')
    assert unbound == 0, "a `propose` span opened without binding its handle cannot be stamped"
    assert opened == 4, f"{opened} propose spans found; every one must bind its handle"
    assert source.count("stamp_proposal_span(_span,") == 4, "a site opened a span and forgot to stamp"
    # Three sites know the card while the span is open; the RE-PROPOSAL site must pass NO idea —
    # the one it holds names the card this path is about to drop. Its card stays derivable through
    # the `node_created` event that shares its trace.
    assert source.count("stamp_proposal_span(_span, idea") == 3
    assert source.count("stamp_proposal_span(_span, None, node_id=node.id)") == 1
