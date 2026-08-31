"""A card the operator reopened is retirable by the engine again.

`_drop_card_once` is idempotent by HISTORY: it refuses to plan a drop for a card any drop receipt
already names. That was right when a drop was terminal, and became wrong when `dccad06f` gave the
operator a reopen — the fold has resolved drop/reopen LAST-RECEIPT-WINS ever since, so after
drop -> reopen the board shows the card LIVE while this scan still saw the historical drop and
returned without appending.

WHAT THE SILENCE COSTS. Every later engine retirement no-ops while its caller believes the card
retired: `_retire_unclaimable_cards` resets its counters and re-enters the same refuse/retire cycle,
and the node-reset re-propose leaves the superseded twin live beside its replacement — the leak
`_exhausted` exists to refuse, made silent. `events/card_ledger.py` names the state in its own
comment ("permanently un-droppable by its owner") and blocks only the laundering path.

**THE ASYMMETRY IS THE DESIGN AND IT IS NOT SYMMETRIC.** A reopen may undo an OPERATOR's drop only.
An engine `card_auto_dropped` stands whatever follows, because `_record_node_less_card` mints a
rejected proposal and auto-drops it in ONE `append_many` precisely so the audit row is never live;
honouring a reopen there would launder it back onto the selectable board.

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

import pathlib
import tempfile

import pytest

from looplab.events.eventstore import EventStore
from looplab.events.types import (EV_CARD_ADDED, EV_CARD_AUTO_DROPPED, EV_CARD_DROPPED,
                                  EV_CARD_REOPENED)
from tests.factories import make_engine


@pytest.fixture()
def engine():
    with tempfile.TemporaryDirectory() as tmp:
        yield make_engine(pathlib.Path(tmp) / "run")


def _drops(engine) -> list[tuple[str, str]]:
    return [(e.type, (e.data or {}).get("reason") or "")
            for e in engine.store.read_all()
            if e.type in {EV_CARD_AUTO_DROPPED, EV_CARD_DROPPED}]


def test_an_OPERATOR_reopen_lets_the_engine_retire_the_card_again():
    """The defect. Mutation: key idempotence on "any drop receipt ever" again, and this engine drop
    silently no-ops while `_retire_unclaimable_cards` believes it retired the card."""
    with tempfile.TemporaryDirectory() as tmp:
        eng = make_engine(pathlib.Path(tmp) / "run")
        eng.store.append(EV_CARD_ADDED, {"id": "card-0", "statement": "s"})
        eng.store.append(EV_CARD_DROPPED, {"id": "card-0", "dropped_by": "operator",
                                           "reason": "operator stopped it"})
        eng.store.append(EV_CARD_REOPENED, {"id": "card-0"})
        eng._drop_card_once("card-0", reason="unclaimable")
        kinds = _drops(eng)
        assert len(kinds) == 2, f"the engine must be able to retire a reopened card, got {kinds}"
        assert kinds[-1][1] == "unclaimable"


def test_an_ENGINE_auto_drop_still_stands_after_a_reopen():
    """The laundering path, which must STAY closed. Mutation: honour any reopen regardless of who
    dropped, and a rejected proposal the engine auto-dropped for audit returns to the selectable
    board — `_record_node_less_card` mints and auto-drops in one append precisely to prevent that."""
    with tempfile.TemporaryDirectory() as tmp:
        eng = make_engine(pathlib.Path(tmp) / "run")
        eng.store.append(EV_CARD_ADDED, {"id": "card-0", "statement": "s"})
        eng.store.append(EV_CARD_AUTO_DROPPED, {"id": "card-0", "reason": "rejected proposal"})
        eng.store.append(EV_CARD_REOPENED, {"id": "card-0"})
        eng._drop_card_once("card-0", reason="second try")
        assert len(_drops(eng)) == 1, "an engine auto-drop is not undone by a reopen"


def test_a_STANDING_operator_drop_is_still_idempotent():
    """The property the scan was written for, which must survive the fix. Mutation: drop the
    `standing` test entirely and every retire pass appends another receipt for a card already
    dropped."""
    with tempfile.TemporaryDirectory() as tmp:
        eng = make_engine(pathlib.Path(tmp) / "run")
        eng.store.append(EV_CARD_ADDED, {"id": "card-0", "statement": "s"})
        eng.store.append(EV_CARD_DROPPED, {"id": "card-0", "dropped_by": "operator", "reason": "x"})
        eng._drop_card_once("card-0", reason="again")
        assert len(_drops(eng)) == 1


def test_drop_reopen_drop_reopen_leaves_the_card_retirable():
    """LAST RECEIPT WINS, replayed: the fold makes drop/reopen/drop expressible, so this scan must
    agree with it over the same log. Mutation: stop resetting `standing` on a reopen (or set it
    once), and the second reopen is ignored."""
    with tempfile.TemporaryDirectory() as tmp:
        eng = make_engine(pathlib.Path(tmp) / "run")
        eng.store.append(EV_CARD_ADDED, {"id": "card-0", "statement": "s"})
        for _ in range(2):
            eng.store.append(EV_CARD_DROPPED, {"id": "card-0", "dropped_by": "operator", "reason": "x"})
            eng.store.append(EV_CARD_REOPENED, {"id": "card-0"})
        eng._drop_card_once("card-0", reason="final")
        assert [k for k, _r in _drops(eng)].count(EV_CARD_AUTO_DROPPED) == 1


def test_a_reopen_of_a_DIFFERENT_card_changes_nothing():
    """Mutation: ignore the id when clearing `standing`, and one card's reopen revives every other
    card's drop — the canonical-id test is what keeps the scan about THIS card."""
    with tempfile.TemporaryDirectory() as tmp:
        eng = make_engine(pathlib.Path(tmp) / "run")
        eng.store.append(EV_CARD_ADDED, {"id": "card-0", "statement": "s"})
        eng.store.append(EV_CARD_DROPPED, {"id": "card-0", "dropped_by": "operator", "reason": "x"})
        eng.store.append(EV_CARD_REOPENED, {"id": "card-9"})
        eng._drop_card_once("card-0", reason="again")
        assert len(_drops(eng)) == 1, "a reopen must only clear the card it names"


def test_a_reopen_BEFORE_the_drop_does_not_clear_it():
    """Order is the log's own. Mutation: clear `standing` on any reopen anywhere, and a reopen that
    predates the drop revives it — which is what `_event_index` guards against in the fold."""
    with tempfile.TemporaryDirectory() as tmp:
        eng = make_engine(pathlib.Path(tmp) / "run")
        eng.store.append(EV_CARD_ADDED, {"id": "card-0", "statement": "s"})
        eng.store.append(EV_CARD_REOPENED, {"id": "card-0"})
        eng.store.append(EV_CARD_DROPPED, {"id": "card-0", "dropped_by": "operator", "reason": "x"})
        eng._drop_card_once("card-0", reason="again")
        assert len(_drops(eng)) == 1
