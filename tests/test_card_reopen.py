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
