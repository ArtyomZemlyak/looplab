"""A dropped card can be put back on the board. The operator asked; the vocabulary had no word.

THE GAP, verified against the tree before any of this was written: `serve/protocol.py`'s
CONTROL_EVENTS carried `EV_CARD_DROPPED` and NO counterpart; `card_ledger.py`'s `st.cards_dropped` is
an ACCUMULATING list where nothing ever removed an entry, so an operator stop was TERMINAL — the card
stayed visible in the `dropped` lane, `actionable=False`, excluded from selection, and no event in
the vocabulary could return it. `EV_CARD_EDITED` could not stand in: `replay.py::_on_card_edited` is
"a display-only edit; the immutable seed/action receipt remains untouched" and folds `statement`
alone, so reopening through it would make a DISPLAY edit change SELECTION.

RESOLUTION IS LAST RECEIPT WINS by `_event_index`, which the fold stamps on every drop and reopen
receipt. That makes drop / reopen / drop expressible and replay-deterministic, and it is why a reopen
can never revive a card the operator has since stopped again.

THE DROP RECEIPT IS NOT DELETED. The log is append-only and who stopped the work and why is history
the reopened card still owes its reader — the same rule `Card.discarded_nodes` keeps for nodes that
never ran. Only whether the drop is APPLIED changes.
"""
from __future__ import annotations

from looplab.core.models import Event
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_DROPPED, EV_CARD_REOPENED


def _seed():
    evs = [Event(type="run_started", data={"run_id": "r", "task_id": "t", "direction": "max"}),
           Event(type="hypothesis_added", data={"statement": "s0"})]
    return evs, list(fold(evs).cards)[0]


def _status(evs):
    card = next(iter(fold(evs).cards.values()))
    return card.status, card.dropped_reason


def test_a_reopen_puts_a_dropped_card_back():
    evs, cid = _seed()
    drop = Event(type=EV_CARD_DROPPED, data={"id": cid, "reason": "not worth it", "by": "operator"})
    reopen = Event(type=EV_CARD_REOPENED, data={"id": cid, "reason": "second thoughts", "by": "operator"})

    assert _status(evs + [drop]) == ("dropped", "not worth it"), "the drop alone still stops it"
    assert _status(evs + [drop, reopen])[0] == "proposed", (
        "MUTATION: delete the reopen loop in `_apply_card_drops` and this stays `dropped` — the "
        "terminal state the operator asked to be rid of")


def test_the_ORDER_decides_and_a_reopen_never_supersedes_a_LATER_drop():
    """The property that makes last-receipt-wins safe rather than merely convenient."""
    evs, cid = _seed()
    drop = Event(type=EV_CARD_DROPPED, data={"id": cid, "reason": "not worth it", "by": "operator"})
    reopen = Event(type=EV_CARD_REOPENED, data={"id": cid, "reason": "second thoughts", "by": "operator"})

    assert _status(evs + [drop, reopen, drop])[0] == "dropped", (
        "MUTATION: resolve by PRESENCE of a reopen instead of by `_event_index` and this reads "
        "`proposed` — a stale reopen would revive a card the operator has since stopped again")
    assert _status(evs + [reopen, drop])[0] == "dropped", (
        "and a reopen that PRECEDES the drop supersedes nothing")


def test_a_reopen_with_nothing_to_supersede_is_a_no_op():
    evs, cid = _seed()
    reopen = Event(type=EV_CARD_REOPENED, data={"id": cid, "reason": "?", "by": "operator"})
    assert _status(evs + [reopen])[0] == "proposed", "an undropped card is unchanged"


def test_an_UNORDERED_reopen_leaves_the_drop_standing():
    """Fail-closed on a receipt that cannot claim to be later than anything.

    The fold stamps `_event_index` on every receipt it writes, so a missing one means a hand-written
    or pre-upgrade row. Reviving an operator's stop on an unordered claim is the wrong direction to
    be wrong in.
    """
    from looplab.core.models import RunState
    from looplab.events.card_ledger import derive_cards

    evs, cid = _seed()
    st = fold(evs + [Event(type=EV_CARD_DROPPED,
                           data={"id": cid, "reason": "stop", "by": "operator"})])
    st.cards_reopened.append({"id": cid, "reason": "no index"})     # no `_event_index`
    derive_cards(st)
    assert next(iter(st.cards.values())).status == "dropped", (
        "MUTATION: accept a receipt with no `_event_index` and an unordered row silently overrides "
        "an operator's stop")
    assert isinstance(st, RunState)


def test_the_drop_receipt_SURVIVES_a_reopen():
    """History is not rewritten: `_apply_card_drops` stops APPLYING the drop, it does not erase it."""
    evs, cid = _seed()
    st = fold(evs + [
        Event(type=EV_CARD_DROPPED, data={"id": cid, "reason": "not worth it", "by": "operator"}),
        Event(type=EV_CARD_REOPENED, data={"id": cid, "reason": "second thoughts", "by": "operator"})])

    assert len(st.cards_dropped) == 1, (
        "MUTATION: implement the reopen by REMOVING the drop entry and this goes red — who stopped "
        "the work and why is history the reopened card still owes its reader")
    assert st.cards_dropped[0].get("reason") == "not worth it"
    assert len(st.cards_reopened) == 1


def test_the_control_surface_registers_it_in_every_table():
    """`control_validation.py` asserts all five tables equal CONTROL_EVENTS AT IMPORT, so a missing
    row refuses the import rather than inheriting a neighbour's handler. Importing is the check."""
    from looplab.serve import control_validation as cv
    from looplab.serve.protocol import COLLABORATION_EVENTS, CONTROL_EVENTS

    assert EV_CARD_REOPENED in CONTROL_EVENTS
    assert EV_CARD_REOPENED in COLLABORATION_EVENTS, (
        "command-only and generation-fenced like every other card control — the legacy /control "
        "route must not let a write formed against an old generation land on a replacement run")
    for table in (cv.CONTROL_DATA_FIELDS, cv._CONTROL_NORMALIZERS, cv._CONTROL_PRECONDITIONS,
                  cv._CONTROL_DECISIONS, cv._CONTROL_POLICIES):
        assert EV_CARD_REOPENED in table


def test_the_normalizer_mirrors_the_drops_shape():
    """One lifecycle switch, one receipt shape — `_on_card_reopened` reuses the drop's bounded
    receipt, so a second subtly-different bound is how the two halves come to disagree."""
    from looplab.serve.control_validation import _normalize_card_reopened

    class _Ctx:
        def card(self):
            return "card-3", None

        def text(self, key, required=False, limit=0):
            return "second thoughts" if key == "reason" else ""

    out = _normalize_card_reopened(_Ctx())
    assert out["id"] == "card-3" and out["reason"] == "second thoughts"
    assert out.get("by") == "operator", (
        "`by`, not `dropped_by`: the shared receipt reads either, and `dropped_by` on a REOPEN row "
        "would be a lie to whoever reads the log")


def _board(rows):
    """Fold a minimal card log and return the single card it builds."""
    import pathlib
    import tempfile

    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    run_dir = pathlib.Path(tempfile.mkdtemp())
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})
    store.append("card_added", {"id": "card-x", "statement": "s", "source": "researcher"})
    for event_type, payload in rows:
        store.append(event_type, payload)
    return fold(store.read_all()).cards["card-x"]


_OPERATOR_DROP = ("card_dropped", {"id": "card-x", "reason": "not now", "dropped_by": "operator"})
_ENGINE_DROP = ("card_auto_dropped", {"id": "card-x", "reason": "rejected", "dropped_by": "engine"})
_REOPEN = ("card_reopened", {"id": "card-x", "reason": "back", "by": "operator"})


def test_a_reopen_may_not_undo_the_ENGINES_own_retirement():
    """`st.cards_dropped` holds TWO authorities and one handler folds both into it.

    `card_reservation._record_node_less_card` mints a Card and auto-drops it in a single
    `append_many` precisely so a REJECTED proposal is retained for audit and never live. An
    unqualified supersede put that proposal back on the selectable board — and because
    `_drop_card_once` is idempotent by HISTORY (it refuses to re-plan a drop for a card any drop
    receipt already names), the engine could then never retire it again: permanently un-droppable
    by its own owner.
    """
    card = _board([_ENGINE_DROP, _REOPEN])
    assert str(card.status) == "dropped" and card.dropped_by == "engine"


def test_a_reopen_still_undoes_the_operators_own_drop():
    """The counter-assertion — the fix must not cost the control the operator asked for by name."""
    card = _board([_OPERATOR_DROP, _REOPEN])
    assert str(card.status) != "dropped" and card.dropped_by is None


def test_an_UNATTRIBUTED_drop_fails_closed():
    """A receipt with no authority renders as the engine's (`dropped_by` defaults to "engine" in
    the same function), so it must not be reopenable either — a hand-written or pre-stamping row is
    not evidence that an operator stopped the work."""
    card = _board([("card_dropped", {"id": "card-x", "reason": "?"}), _REOPEN])
    assert str(card.status) == "dropped"


def test_drop_reopen_drop_is_still_expressible():
    """Last receipt wins by `_event_index`, and the fix must not have made the switch one-way."""
    card = _board([_OPERATOR_DROP, _REOPEN,
                   ("card_dropped", {"id": "card-x", "reason": "again", "dropped_by": "operator"})])
    assert str(card.status) == "dropped" and card.dropped_by == "operator"


def test_an_operator_drop_OVER_an_engine_drop_does_not_launder_it_reopenable():
    """The authority gate may not be read off the HEAD receipt, because the operator can write it.

    `dropped` is last-receipt-wins across BOTH authorities, and
    `control_validation._precondition_card` deliberately EXCLUDES `EV_CARD_DROPPED` from its
    terminal-lifecycle refusal so "an operator keeps authority over the DROP itself on a terminal
    Card". Those two facts compose: the operator appends their own `card_dropped` over the engine's
    `card_auto_dropped`, the head receipt now reads `dropped_by: "operator"`, and a gate that
    consults only the head lets the very next `card_reopened` pop the entry — putting a REJECTED
    proposal back on the selectable board, permanently, since `_drop_card_once` is idempotent by
    history and can never retire it again.

    Two API calls, both individually legitimate. The gate has to read the drop HISTORY.
    """
    card = _board([_ENGINE_DROP, _OPERATOR_DROP, _REOPEN])
    assert str(card.status) == "dropped", (
        "MUTATION: gate the reopen on `dropped[cid]`'s own `dropped_by` instead of on whether ANY "
        "engine-authored receipt precedes the reopen, and this reads `proposed` — an engine "
        "retirement laundered into a reopenable one by writing a drop over it")


def test_the_engine_drop_still_blocks_when_it_lands_BETWEEN_an_operator_drop_and_a_reopen():
    """Ordering, not merely presence: the engine's retirement precedes this reopen and stands."""
    card = _board([_OPERATOR_DROP, _ENGINE_DROP, _REOPEN])
    assert str(card.status) == "dropped"


def test_an_engine_drop_AFTER_a_reopen_does_not_retroactively_refuse_it():
    """The complement, so the gate is an ordering rule and not a blanket ban on the card.

    The reopen at index N is judged against the receipts that PRECEDE it; a later engine drop is a
    later drop, and last-receipt-wins already applies it.
    """
    card = _board([_OPERATOR_DROP, _REOPEN])
    assert str(card.status) != "dropped", "the reopen itself is unaffected"
    later = _board([_OPERATOR_DROP, _REOPEN, _ENGINE_DROP])
    assert str(later.status) == "dropped" and later.dropped_by == "engine", (
        "and the engine drop that follows it lands normally")
