"""A native Card row outranks its frozen hypothesis twin for the same statement (doc 25 EV-13).

`st.cards` subsumes the removed hypothesis board, so every card event has a hypothesis twin that old
logs still replay, and two phases of `card_ledger.derive_cards` walk BOTH families. Dedup is first-wins, so the
concatenation ORDER is the whole invariant: put the shadow family first and the twin claims the id,
after which the real `card_added`'s own row is skipped by `if cid in cards: continue` and its receipt
— rationale, snapshot, footprint, action ownership — is silently gone.

That ordering was written out as a literal list concatenation at each of the two sites: one
invariant, two places to drift. It is now `_native_first`, and this file is what makes the ordering
falsifiable, because nothing else in the suite did: flipping the two concatenation arms left every
existing replay and card test green.
"""
from __future__ import annotations

import ast
import inspect

from looplab.events import card_ledger
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

STATEMENT = "dropout beats weight decay on this task"


def _store(tmp_path, rows):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    for kind, data in rows:
        store.append(kind, data)
    return store


def _card_for(state, statement):
    matches = [card for card in state.cards.values() if card.statement == statement]
    assert len(matches) == 1, f"expected one card for the statement, got {len(matches)}"
    return matches[0]


# ------------------------------------------------------------------ the native row wins the id

def test_the_native_card_owns_the_id_when_its_twin_is_logged_first(tmp_path):
    """Log order puts the hypothesis first; the derivation must still take the native receipt.

    The two families live in separate `RunState` lists, so log order cannot decide this — only the
    concatenation order in `_native_first` can. That is exactly why it is worth pinning.
    """
    store = _store(tmp_path, [
        ("hypothesis_added", {"statement": STATEMENT, "rationale": "the shadow's rationale",
                              "source": "human"}),
        ("card_added", {"id": "card-native", "statement": STATEMENT,
                        "rationale": "the native receipt's rationale", "source": "researcher"}),
    ])

    card = _card_for(fold(store.read_all()), STATEMENT)

    assert card.rationale == "the native receipt's rationale", (
        "the hypothesis twin claimed the id first, so the real card_added row was skipped and its "
        "receipt was lost")
    assert card.source == "researcher"


def test_the_native_card_still_wins_when_it_is_logged_first(tmp_path):
    """The other log order, so the test above cannot pass merely because later rows win."""
    store = _store(tmp_path, [
        ("card_added", {"id": "card-native", "statement": STATEMENT,
                        "rationale": "the native receipt's rationale", "source": "researcher"}),
        ("hypothesis_added", {"statement": STATEMENT, "rationale": "the shadow's rationale",
                              "source": "human"}),
    ])

    assert _card_for(fold(store.read_all()), STATEMENT).rationale == (
        "the native receipt's rationale")


def test_a_hypothesis_with_no_native_twin_still_becomes_a_card(tmp_path):
    """Native-first must not mean native-only: a node-less deep-research direction has no card event
    and is exactly what the compatibility projection exists to keep on the board."""
    store = _store(tmp_path, [
        ("hypothesis_added", {"statement": "an untwinned direction", "rationale": "from research",
                              "source": "human"}),
    ])

    card = _card_for(fold(store.read_all()), "an untwinned direction")
    assert card.rationale == "from research"


# ------------------------------------------------------------------ one home for the ordering

def test_both_phases_take_their_row_order_from_the_one_helper():
    """The de-duplication itself. Two phases walk both families; a second hand-written concatenation
    is the drift this replaces.

    Scanned over the WHOLE ledger module rather than one function since doc 25 EV-01 split the
    derivation into its numbered phases: the seeding phase and the merge-alias phase are separate
    functions now, and a per-function pin would go vacuously green the moment one of them moved
    again."""
    tree = ast.parse(inspect.getsource(card_ledger))
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "_native_first"]
    assert len(calls) == 2, f"card_ledger calls _native_first {len(calls)} times, expected 2"
    for call in calls:
        assert len(call.args) == 2 and not call.keywords, "positional (native, shadow) only"
        native, shadow = call.args
        assert isinstance(native, ast.Attribute) and native.attr.startswith("cards_"), (
            "the FIRST argument must be the native family — the argument order IS the precedence")
        assert isinstance(shadow, ast.Attribute) and shadow.attr.startswith("hypotheses_")
    # …and the two calls are in DIFFERENT phase functions, which is what "both phases" means once
    # the derivation is decomposed. One function calling it twice would satisfy the count above.
    owners = {
        fn.name
        for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_native_first"
    }
    assert len(owners) == 2, f"both _native_first calls live in {owners}"


def test_the_helper_puts_native_rows_first():
    """A unit check on the rule itself, so the two source pins above are anchored to a behaviour."""
    paired = card_ledger._native_first([{"n": 1}, {"n": 2}], [{"h": 1}])

    assert [flag for flag, _row in paired] == [True, True, False]
    assert [row for _flag, row in paired] == [{"n": 1}, {"n": 2}, {"h": 1}]


def test_the_helper_is_total_on_empty_families():
    assert card_ledger._native_first([], []) == []
    assert card_ledger._native_first([], [{"h": 1}]) == [(False, {"h": 1})]
    assert card_ledger._native_first([{"n": 1}], []) == [(True, {"n": 1})]
