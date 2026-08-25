"""A question carries the concepts it is about, and says who authored them.

THE GAP, measured on `runs/e5small-dr-unified-v5`: all five questions registered by the run's
opening memo carried `concept_tags=[]` while the run's single experiment carried four
(`loss/contrastive/dcl/hard-negative`, `regularization/rdrop`, …). The concept hierarchy and the
question board were two disjoint taxonomies over one run, so an operator grouping the board by
concept saw the experiments and none of the questions they answer.

TWO PLACES DROPPED THEM. `replay._on_hypothesis_added` kept only statement/id/source/rationale/
at_node, and `card_ledger`'s constructor for a non-native row is handed an EMPTY `snapshot` by
construction — `_card_added_snapshot` runs for native rows only.

THE PROVENANCE IS ITS OWN KIND. `hypothesis_added` is not a synonym for `card_added`: a memo
authored these tags, not a card mint, and there is no ownership receipt or action digest behind
them. Filing them under `card_added` would mint a receipt nobody issued; leaving `concept_source`
None would make an authored membership indistinguishable from one nobody claimed.
"""
from __future__ import annotations

from looplab.core.models import Event
from looplab.events.replay import fold


def _fold(*rows):
    base = [Event(seq=0, ts=0.0, type="run_started",
                  data={"run_id": "r", "task_id": "t", "direction": "max"})]
    return fold(base + [Event(seq=i + 1, ts=0.0, type=t, data=d)
                        for i, (t, d) in enumerate(rows)])


def _question(**extra):
    return ("hypothesis_added",
            {"statement": "does distillation help here", "source": "deep_research", **extra})


def test_a_question_carries_its_concepts_and_names_their_author():
    st = _fold(_question(concepts=["loss/contrastive", "training/distillation"]))
    card = next(iter(st.cards.values()))
    assert card.card_kind == "direction", "a question owns no action"
    assert card.concept_tags == ["loss/contrastive", "training/distillation"]
    assert card.concept_source is not None and card.concept_source.kind == "hypothesis_added", (
        "a memo authored these, not a card mint — `card_added` would be a receipt nobody issued")


def test_an_EXPLICIT_empty_list_is_not_an_authored_membership_either():
    """"Nobody said" must stay distinguishable from "said none".

    A MUTATION SURVIVED HERE and this is the case it needed: the first version of this test passed
    a question with NO `concepts` key, so the `isinstance(..., list)` gate short-circuited and the
    mutation that writes the receipt UNCONDITIONALLY never bit. The list has to be PRESENT and
    empty for the difference to exist at all.
    """
    st = _fold(_question(concepts=[]))
    assert "concepts" not in st.hypotheses_added[0], (
        "an explicit empty list must not become a stored membership")
    card = next(iter(st.cards.values()))
    assert card.concept_tags == [] and card.concept_source is None


def test_a_question_with_no_concepts_key_at_all_is_untouched():
    st = _fold(_question())
    card = next(iter(st.cards.values()))
    assert card.concept_tags == []
    assert card.concept_source is None


def test_malformed_concepts_are_dropped_rather_than_carried():
    for bad in ("loss/contrastive", 7, {"a": 1}, None):
        st = _fold(_question(concepts=bad))
        card = next(iter(st.cards.values()))
        assert card.concept_tags == [], f"{bad!r} is not a concept list"
        assert card.concept_source is None


def test_an_invalid_slug_inside_a_valid_list_does_not_poison_the_rest():
    """The SAME bound every other concept membership goes through — a question cannot introduce a
    slug shape a node could not."""
    st = _fold(_question(concepts=["loss/contrastive", "NOT A SLUG!!", "training/distillation"]))
    card = next(iter(st.cards.values()))
    assert "loss/contrastive" in card.concept_tags
    assert "NOT A SLUG!!" not in card.concept_tags


def test_the_receipt_survives_into_the_folded_journal():
    """The handler's own half: `st.hypotheses_added` is what the card ledger reads."""
    st = _fold(_question(concepts=["loss/contrastive"]))
    assert st.hypotheses_added[0].get("concepts") == ["loss/contrastive"]
    assert "concepts" not in fold([
        Event(seq=0, ts=0.0, type="run_started",
              data={"run_id": "r", "task_id": "t", "direction": "max"}),
        Event(seq=1, ts=0.0, type="hypothesis_added", data={"statement": "s"}),
    ]).hypotheses_added[0]


def test_a_card_added_ROW_never_takes_the_memo_provenance():
    """`native_row` means "came from `cards_added`", not "has a valid receipt" — so EVERY
    `card_added` row is fenced off from the question path, receipt or no receipt. A mint's own
    membership can never be overwritten with a memo's.

    HONEST LIMIT, stated rather than papered over: this property is protected TWICE — by the
    `native_row` guard and by the kwarg order (`**question_source` before `**snapshot`, so a
    native row's own snapshot wins) — and my mutation harness could not make either observable,
    including with both removed at once. I am recording that the mutation was NOT caught rather
    than claiming it was; what the assertion below does hold is the shipped behaviour, driven
    through the real fold.
    """
    st = _fold(("card_added", {"id": "card-0", "statement": "a card row",
                               "concepts": ["loss/contrastive"], "idea": {"operator": "draft"}}))
    card = st.cards["card-0"]
    assert card.concept_tags == [], "a top-level `concepts` on a card row is not a memo membership"
    assert card.concept_source is None or card.concept_source.kind != "hypothesis_added"
