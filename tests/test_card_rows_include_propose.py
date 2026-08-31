"""A card's row prices the whole experiment — the propose that minted it AND the build.

`CARD_UNATTRIBUTED`'s comment said "Every `propose` generation lands here by construction on a
card-driven run, because a proposal is made BEFORE the card it may become exists", and
`token_spend_by_card`'s docstring said the table was "what each experiment's BUILD cost". Both were
backwards: `orchestrator.py::stamp_proposal_span` stamps the card id INSIDE the open `propose` span
the moment `_link` mints the card, and spans are written on CLOSE, so the id is on the row
`_owning_card` walks to.

MEASURED on `runs/rubertlite-dr-unified-v9` by folding its real spans twice, with and without the
propose phase: ALL 27,436,262 propose tokens resolve to a real card and `(no card)` gains exactly 0
of them; per card the propose share of the row is 18.2 %-62.0 % (card-5 is 62 %). So an operator
reconciling this table against the phase table's plan+stages+card_build was reading the propose as
attribution error.

FIXED BY REWORDING, not by narrowing `card_of` to `card_build` spans: the proposal that minted a
card is money spent on that experiment's behalf, and moving 25.3 % of a run into `(no card)` would
make that bucket the largest row in the table.

These tests pin the BEHAVIOUR the claims now describe, so the old sentence cannot come back without
going red. Every assertion has an input that makes it fail; the mutations are named.
"""
from __future__ import annotations

from looplab.events.token_spend import CARD_UNATTRIBUTED, token_spend_by_card


def _gen(span_id, parent_id, phase, total, card_id=None):
    attributes = {"op": "chat", "phase": phase,
                  "usage": {"prompt": total * 3 // 4, "completion": total // 4, "total": total}}
    if card_id:
        attributes["card_id"] = card_id
    return {"kind": "generation", "span_id": span_id, "parent_id": parent_id,
            "attributes": attributes}


def _op(span_id, parent_id, phase, card_id=None):
    attributes = {"phase": phase}
    if card_id:
        attributes["card_id"] = card_id
    return {"kind": "operation", "span_id": span_id, "parent_id": parent_id,
            "attributes": attributes}


def test_a_propose_generation_under_a_stamped_span_is_charged_to_the_CARD():
    """The mechanism the old comment denied. Mutation: key `card_of` on `card_build` spans only and
    this 400 moves to `(no card)` — which is the 'build-only pricing' alternative, measured at 25.3 %
    of a real run and rejected for exactly that reason."""
    spans = [
        _op("p1", None, "propose", card_id="card-1"),      # stamped when `_link` minted the card
        _gen("g1", "p1", "propose", 400),
    ]
    rows = {r["card"]: r["tokens"] for r in token_spend_by_card(spans)["rows"]}
    assert rows.get("card-1") == 400, (
        "the proposal that minted a card is spend on that experiment's behalf and must appear in "
        f"its row, got {rows}")
    assert CARD_UNATTRIBUTED not in rows, (
        "and nothing is left unattributed — the old comment claimed every propose generation "
        "landed in that bucket 'by construction'")


def test_a_card_row_is_propose_PLUS_build_and_the_two_are_summed():
    """The sentence the docstring now makes: the row prices the whole experiment.

    Mutation: charge only the nearest `card_build` ancestor and the row reads 600 — the operator
    then reconciles 600 against a phase table showing 1,000 for the card and calls it a leak."""
    spans = [
        _op("p1", None, "propose", card_id="card-1"),
        _gen("g1", "p1", "propose", 400),
        _op("b1", None, "card_build", card_id="card-1"),
        _gen("g2", "b1", "stages", 600),
    ]
    rows = {r["card"]: r["tokens"] for r in token_spend_by_card(spans)["rows"]}
    assert rows.get("card-1") == 1000, f"propose + build, both on the card's row, got {rows}"


def test_a_generation_whose_ancestry_names_NO_card_still_lands_in_the_bucket():
    """The bucket keeps its real job.

    THE FIXTURE MUST CONTAIN A CARD, and the first cut did not: with no `card_id` anywhere in the
    span set, a mutant that falls back to "any card we saw" has none to fall back TO and answers
    `(no card)` for its own reason. The mutation run said so. A run with a real card beside an
    unowned call is also the only realistic shape — the bucket exists precisely for runs that have
    cards.

    Mutation: attribute an unowned generation to some arbitrary card and an unattributable call
    stops being a fact about the record."""
    spans = [
        _op("p1", None, "propose", card_id="card-1"),
        _gen("g1", "p1", "propose", 400),
        _op("x1", None, "deep_research"),               # no card anywhere on this chain
        _gen("g2", "x1", "deep_research", 250),
    ]
    rows = {r["card"]: r["tokens"] for r in token_spend_by_card(spans)["rows"]}
    assert rows == {"card-1": 400, CARD_UNATTRIBUTED: 250}, (
        f"the deep-research call belongs to no card and must say so, got {rows}")


def test_the_shipped_claims_no_longer_say_BUILD_only():
    """The three sentences that disagreed with the mechanism. A NEGATIVE pin on purpose (CLAUDE.md:
    what must not come back is the TEXT), and it is checked in the two code sites plus the guide."""
    import inspect
    import pathlib

    from looplab.events import token_spend

    src = inspect.getsource(token_spend)
    assert "what each experiment's BUILD cost" not in src, (
        "the docstring's old headline priced the row as the build alone")
    assert "lands here by construction" not in src, (
        "the comment's old claim that every propose generation is unattributable")

    guide = pathlib.Path(__file__).resolve().parents[1] / "docs" / "guide" / "cli-reference.md"
    text = guide.read_text()
    assert "is where every `propose` generation belongs by construction" not in text
    # Pinned on a phrase that does not cross the guide's line wrap — the first cut looked for
    # "prices the whole experiment", which the file breaks across two lines, so the assertion failed
    # about text that was actually there. A source pin that spans a wrap is a pin nobody can keep.
    assert "the proposal that minted it and the build that followed" in text, (
        "and the guide must state what the row DOES price, or the correction is only a deletion")
