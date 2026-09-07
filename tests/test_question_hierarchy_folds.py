"""A question filed under a broader question survives the carrier, the fold and the forest.

Step 2-4 of the hierarchy. `question_parent_rows` (already guarded in
`test_question_parent_join.py`) resolves the edge; these drive what happens to it afterwards:
`hypothesis_added` carries `parent_belief_id`, `_on_hypothesis_added` folds it under the same bound
as `id`, the card ledger sets `parent_card_id` on the synthesized direction card, and the existing
DIRECTION -> EXPERIMENT forest fills the parent's `child_card_ids` with no further help.

MEASURED before this shipped, on e5small-dr-unified-v12: all 11 `hypothesis_added` rows carried
exactly `['at_node', 'concepts', 'source', 'statement']`, so a question could only ever be a leaf
while `Card` had carried `parent_card_id` and `child_card_ids` the whole time.
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from looplab.agents.deep_research import ResearchMemo, _MemoOut
from looplab.core.cards import hypothesis_id
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

BROAD = "Does hard-negative mining help?"
NARROW = "Does a stronger teacher help mining?"


def _fold_rows(*rows):
    store = EventStore(pathlib.Path(tempfile.mkdtemp()) / "events.jsonl")
    store.append("run_started", {"eval_parallel": 1})
    for payload in rows:
        store.append("hypothesis_added", payload)
    return fold(store.read_all()).cards


def _question(statement, **extra):
    return {"statement": statement, "source": "deep_research", "at_node": 0, **extra}


def test_the_edge_survives_the_carrier_and_the_fold():
    cards = _fold_rows(_question(BROAD),
                       _question(NARROW, parent_belief_id=hypothesis_id(BROAD)))
    parent, child = cards[hypothesis_id(BROAD)], cards[hypothesis_id(NARROW)]
    assert child.parent_card_id == parent.id
    # The forest fills the other direction with no extra plumbing — this is the assertion that
    # fails if the edge lands on a row the lineage pass does not walk.
    assert parent.child_card_ids == [child.id]
    assert parent.card_kind == child.card_kind == "direction"


def test_a_question_with_no_parent_is_exactly_what_it_was_before():
    # Byte-identical folding of every log on disk: absence must leave the field alone, never
    # become an authored "no parent".
    cards = _fold_rows(_question(BROAD), _question(NARROW))
    assert cards[hypothesis_id(NARROW)].parent_card_id is None
    assert cards[hypothesis_id(BROAD)].child_card_ids == []


def test_a_question_cannot_be_folded_as_its_own_parent():
    # Refused at the fold as well as at the append site: the two see different things, and a row
    # that is its own ancestor makes the rollup recurse.
    cards = _fold_rows(_question(BROAD, parent_belief_id=hypothesis_id(BROAD)))
    assert cards[hypothesis_id(BROAD)].parent_card_id is None


@pytest.mark.parametrize("bad", ["", "   ", None, 17, ["a"], "x" * 300])
def test_a_malformed_parent_id_is_dropped_not_folded(bad):
    cards = _fold_rows(_question(BROAD), _question(NARROW, parent_belief_id=bad))
    assert cards[hypothesis_id(NARROW)].parent_card_id is None


def test_an_unresolvable_parent_leaves_the_child_on_the_board():
    # A parent id naming nothing must not delete the question — a lost row is worse than a
    # missing edge, and the child is still a real open question.
    cards = _fold_rows(_question(NARROW, parent_belief_id="does-not-exist-000000"))
    assert hypothesis_id(NARROW) in cards


def test_a_chain_of_three_questions_folds_as_a_chain():
    third = "Does teacher SIZE or teacher TRAINING drive the gain?"
    cards = _fold_rows(_question(BROAD),
                       _question(NARROW, parent_belief_id=hypothesis_id(BROAD)),
                       _question(third, parent_belief_id=hypothesis_id(NARROW)))
    assert cards[hypothesis_id(third)].parent_card_id == hypothesis_id(NARROW)
    assert cards[hypothesis_id(NARROW)].parent_card_id == hypothesis_id(BROAD)
    assert cards[hypothesis_id(NARROW)].child_card_ids == [hypothesis_id(third)]


def test_the_memo_schema_carries_the_field_the_append_site_reads():
    # A prompt/append site that names a field the schema lacks ships INERT — that has happened on
    # this repo and cost a whole run's memos, which is why `question_concepts` has its own guard.
    assert "question_parents" in _MemoOut.model_fields, "the emit schema cannot express a parent"
    assert "question_parents" in ResearchMemo.model_fields, "the carrier would drop it"
    assert ResearchMemo().question_parents == []


def test_the_parent_survives_the_sanitizer_that_builds_the_durable_payload():
    # `sanitize_research_memo_payload` builds its OWN dict, so a key it does not know about is
    # dropped no matter what the schema and the carrier say — that is precisely how
    # `question_concepts` came to record "no concepts" about memos structurally unable to hold any.
    from looplab.core.advisory_payloads import sanitize_research_memo_payload
    clean = sanitize_research_memo_payload({
        "open_questions": [BROAD, NARROW], "question_parents": ["", BROAD]})
    assert clean["question_parents"] == ["", BROAD]
    # And a memo that says nothing still gets the key, so the append site never KeyErrors.
    assert sanitize_research_memo_payload({})["question_parents"] == []


def test_the_field_tells_the_model_the_alignment_rule_and_how_to_say_nothing():
    # The description is the only channel in front of the model when the emit call is constructed
    # (measured: on v6 the instruction was read and 25 concept calls were made, and the memo still
    # came back with an empty list). It must carry the positional rule AND an explicit way to
    # decline, or a model with nothing to say pads the field and files questions under lineages
    # they do not belong to.
    # On `_MemoOut`, not `ResearchMemo`: `_emit_spec` hands _MemoOut's json schema to the provider
    # as the tool's parameters, so that is the class whose `description` reaches the model.
    # `ResearchMemo` is the internal carrier and carries no descriptions at all.
    text = (_MemoOut.model_fields["question_parents"].description or "").lower()
    assert "same" in text and "position" in text
    assert "empty" in text or "top-level" in text
