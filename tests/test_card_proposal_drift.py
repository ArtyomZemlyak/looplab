"""The arbiter that says an experiment stopped being the one its card proposed — and consumes it.

`node_knob_delta` measured this and nothing decided on it, which made the proposal board quietly
dangerous. A card's `params` is the receipt-bound PROPOSAL; under `params_style: "none"` the engine
applies nothing and the Developer realises the idea by editing the repo, so a repair that fits a
training into memory moves the numbers while the card keeps the old ones. A proposer reading a row
under "already tried" and sizing its next idea one knob off THAT is sizing it off a recipe nothing
ever ran — the reading that cost `runs/e5small-dr-unified-v4` four days, and that put 8192 into the
v3 goal, where all three nodes then died of `torch.OutOfMemoryError`.

Measured through the fold on v4: six of the nine cards with an applied record disagree with their
own proposal, the run's CHAMPION among them.
"""
from __future__ import annotations

from looplab.core.models import (Card, CardSelectionProvenance, RunState,
                                 card_drift_brief, card_proposal_drift)
from looplab.agents.roles import board_prompt_lines


def _card(**kw) -> Card:
    return Card(id="card-1", statement="s", seed_statement="s", **kw)


def test_only_the_coordinates_BOTH_sides_name_are_compared():
    """A knob the card declared and the carrier never answered is not evidence of a move. The
    applied record answers what it could READ, and absence is `unknown` here exactly as it is
    everywhere else — so `moved` can never exceed `compared`, and `compared` is the honest
    denominator for how much of the proposal was actually checked."""
    drift = card_proposal_drift(_card(params={"a": 1.0, "b": 2.0, "unread": 9.0},
                                      applied_params={"a": 1.0, "b": 3.0}))
    assert drift == {"compared": 2, "moved": 1, "params": ["b"], "node": None}


def test_agreement_is_reported_as_zero_moved_and_never_as_silence():
    """A caller must be able to tell "they agree" from "nothing was comparable" — the same
    distinction `metric_provenance`'s checked-beside-diverged pair exists for one layer down."""
    agree = card_proposal_drift(_card(params={"a": 1.0}, applied_params={"a": 1.0}))
    assert agree["compared"] == 1 and agree["moved"] == 0


def test_nothing_comparable_is_None_and_never_an_empty_dict():
    assert card_proposal_drift(_card(params={"a": 1.0})) is None, "no applied record"
    assert card_proposal_drift(_card(applied_params={"a": 1.0})) is None, "no proposal"
    assert card_proposal_drift(_card(params={"a": 1.0}, applied_params={"z": 1.0})) is None, (
        "no shared coordinate — the two records are about different knobs")


def test_the_clause_is_SILENT_on_agreement():
    """A line reading "0 of 6 knobs moved" on every card trains a reader to skip the line, and the
    entire value of this signal is that the loud case stays loud."""
    assert card_drift_brief(_card(params={"a": 1.0}, applied_params={"a": 1.0})) == ""
    assert card_drift_brief(_card(params={"a": 1.0})) == ""


def test_the_clause_names_the_node_the_claim_is_about():
    brief = card_drift_brief(_card(params={"batch": 4096.0, "lr": 0.001},
                                   applied_params={"batch": 2048.0, "lr": 0.0005},
                                   applied_params_node=13))
    assert "node 13" in brief and "2 of 2" in brief and "batch" in brief and "lr" in brief


def test_a_wide_divergence_states_the_count_even_where_the_names_are_capped():
    card = _card(params={f"k{i}": 1.0 for i in range(20)},
                 applied_params={f"k{i}": 2.0 for i in range(20)})
    drift = card_proposal_drift(card)
    assert drift["moved"] == 20 and len(drift["params"]) == 12, "the NAME list is bounded"
    assert "20 of 20" in card_drift_brief(card), "the COUNT is never capped with the names"
    assert "+8 more" in card_drift_brief(card)


def test_the_proposal_board_CARRIES_the_clause_and_is_byte_identical_without_it():
    """The consumer, not the arbiter: measuring this and rendering nothing is where it was."""
    def _board(card):
        st = RunState(goal="g", direction="max")
        st.cards = {card.id: card}
        return "\\n".join(board_prompt_lines(st, None, [], for_proposal=True))

    owned = dict(status="evaluated", verdict="tested", evidence=[13], belief_id="b",
                 selection_provenance=CardSelectionProvenance(
                     action_source="card_added", action_owner_count=1))
    moved = _board(_card(params={"lr": 0.001}, applied_params={"lr": 0.0005},
                         applied_params_node=13, **owned))
    assert "RAN AT DIFFERENT COORDINATES on node 13" in moved

    agreed = _board(_card(params={"lr": 0.001}, applied_params={"lr": 0.001},
                          applied_params_node=13, **owned))
    silent = _board(_card(params={"lr": 0.001}, **owned))
    assert "RAN AT DIFFERENT COORDINATES" not in agreed
    assert agreed == silent, (
        "a card that ran as proposed must render byte-identically to one with no record at all")
