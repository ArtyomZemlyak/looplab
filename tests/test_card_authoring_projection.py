"""The in-flight AUTHORING projection: does a card reach the board when work STARTS?

Drives `events/authoring_projection.py::card_authoring` against real folded prefixes of a real
speculative-lane event sequence, rather than pinning its source. The property that matters is the
HANDOFF: between the durable build election and the durable `node_building` claim there is a window
— 2,128 s of it on `runs/rubertlite-dr-unified-v5` — in which the fold reports `status="proposed"`
("work item is open and has not started") while a Developer is writing the card's code. Exactly one
of {this projection, the fold} must own the card at every prefix in that window, and the switch must
happen at `node_building` with no gap and no double.
"""
from __future__ import annotations

from looplab.core.models import Event, card_ownership_receipt
from looplab.events.authoring_projection import AUTHORING_MAX_ROWS, card_authoring
from looplab.events.replay import fold

T0 = 1_700_000_000.0


def _events(rows):
    return [Event(seq=index, type=kind, data=data, ts=T0 + index)
            for index, (kind, data) in enumerate(rows)]


def _card_added(card_id="card-0", statement="switch the loss to cross-batch InfoNCE"):
    idea = {"operator": "draft", "params": {"lr": 0.2}, "eval_timeout": None}
    action = {
        "operator": "draft", "params": {"lr": 0.2}, "space": None, "eval_profile": None,
        "eval_timeout": None, "parent_id": None, "parent_ids": [], "parent_generations": {},
        "scored_against": None, "scored_against_generation": None, "scored_against_empty": True,
        "footprint": None,
    }
    receipt = card_ownership_receipt(card_id, statement, action)
    assert receipt is not None
    return ("card_added", {
        "id": card_id, "statement": statement, "source": "researcher", "idea": idea,
        "parent_id": None, "parent_ids": [], "parent_generations": {},
        "scored_against": None, "scored_against_generation": None, "scored_against_empty": True,
        "ownership_receipt": receipt,
    })


def _speculative_lane(card_id="card-0", node_id=0):
    """The exact durable sequence `engine/speculation.py` writes for one prefetched card."""
    return [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        _card_added(card_id),
        ("card_build_requested", {"card_id": card_id, "generation": 0}),
        ("card_build_attempted", {"card_id": card_id, "generation": 0, "index": 0}),
        # ... the Developer runs here, on a worker thread, for as long as it takes ...
        ("node_building", {"node_id": node_id, "operator": "draft", "parent_ids": [],
                           "card_id": card_id, "speculative": True, "card_build_generation": 0}),
        ("node_created", {"node_id": node_id, "operator": "draft", "parent_ids": [],
                          "idea": {"operator": "draft", "params": {"lr": 0.2}, "card_id": card_id},
                          "speculative": True, "card_build_generation": 0}),
        ("card_build_done", {"card_id": card_id, "generation": 0, "node_id": node_id,
                             "speculative": True}),
    ]


def _own(rows, upto):
    evs = _events(rows)[:upto]
    st = fold(evs)
    live = card_authoring(evs, st)
    card = st.cards.get("card-0")
    return live, (card.status if card is not None else None)


def test_exactly_one_of_the_projection_and_the_fold_owns_the_card_at_every_prefix():
    rows = _speculative_lane()
    seen_projection = seen_fold = 0
    for upto in range(1, len(rows) + 1):
        live, status = _own(rows, upto)
        if status is None:
            assert live == []          # no card yet: nothing to be provisional about
            continue
        by_projection = [row for row in live if row["card_id"] == "card-0"]
        by_fold = status == "building"
        # The whole point: never both (a doubled card on the board) and — once the build has been
        # elected and until it commits — never neither (the 35-minute lie this projection removes).
        assert not (by_projection and by_fold)
        seen_projection += bool(by_projection)
        seen_fold += bool(by_fold)
        if upto >= 3 and status == "proposed":
            assert by_projection, f"prefix {upto} shows a card as not-yet-started while it is being built"
    assert seen_projection >= 2 and seen_fold >= 1


def test_the_paid_attempt_receipt_is_what_promotes_speculating_to_building():
    rows = _speculative_lane()
    elected, _ = _own(rows, 3)
    assert [(row["phase"], row["started"]) for row in elected] == [("speculating", T0 + 2)]
    building, status = _own(rows, 4)
    # `card_build_attempted` is appended by `_start_head_producer` BEFORE the provider call, so it is
    # the honest boundary for "a Developer is writing this now" — and its own ts is the clock.
    assert [(row["phase"], row["started"], row["folded_status"]) for row in building] \
        == [("building", T0 + 3, "proposed")]
    assert status == "proposed"


def test_the_projection_lets_go_the_instant_node_building_lands():
    rows = _speculative_lane()
    assert _own(rows, 4)[0]                      # in flight
    live, status = _own(rows, 5)                 # node_building
    assert live == [] and status == "building"
    live, status = _own(rows, 6)                 # node_created
    assert live == [] and status == "running"


def test_a_closed_head_is_never_reported_again():
    rows = _speculative_lane()
    live, status = _own(rows, len(rows))         # card_build_done advanced the cursor
    assert live == [] and status == "running"


def test_a_crash_recovery_re_attempt_reports_the_producer_that_is_actually_running():
    rows = _speculative_lane()[:4] + [
        ("card_build_attempted", {"card_id": "card-0", "generation": 0, "index": 0}),
    ]
    live, _ = _own(rows, len(rows))
    # The first producer died; the operator's clock must measure the one that replaced it.
    assert [(row["phase"], row["started"]) for row in live] == [("building", T0 + 4)]


def test_a_dropped_card_leaves_the_board_rather_than_reappearing_as_in_flight():
    rows = _speculative_lane()[:4] + [
        ("card_dropped", {"id": "card-0", "reason": "operator stopped it", "dropped_by": "operator"}),
    ]
    live, status = _own(rows, len(rows))
    assert live == [] and status == "dropped"


def test_the_serial_lane_projects_nothing():
    # `card_reservation.py::_reserve_node_build` appends card_added and its node_building claim in ONE
    # batch before the Developer runs, so the fold is already honest and there is nothing to overlay.
    rows = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        _card_added(),
        ("node_building", {"node_id": 0, "operator": "draft", "parent_ids": [], "card_id": "card-0"}),
    ]
    live, status = _own(rows, len(rows))
    assert live == [] and status == "building"


def test_a_head_whose_card_the_projection_cannot_see_never_synthesizes_a_board_row():
    rows = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("card_build_requested", {"card_id": "card-ghost", "generation": 0}),
        ("card_build_attempted", {"card_id": "card-ghost", "generation": 0, "index": 0}),
    ]
    evs = _events(rows)
    assert card_authoring(evs, fold(evs)) == []


def test_the_row_count_is_bounded():
    rows = [("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})]
    for index in range(AUTHORING_MAX_ROWS + 8):
        card_id = f"card-{index}"
        rows.append(_card_added(card_id, f"direction number {index}"))
        rows.append(("card_build_requested", {"card_id": card_id, "generation": 0}))
        rows.append(("card_build_attempted", {"card_id": card_id, "generation": 0, "index": index}))
    evs = _events(rows)
    assert len(card_authoring(evs, fold(evs))) == AUTHORING_MAX_ROWS


def test_a_historical_prefix_reports_what_was_in_flight_then_not_now():
    # `state_payload(upto_seq=...)` folds a prefix; the projection reads the SAME list, so a replayed
    # moment shows the head that was open at that moment rather than the live one.
    rows = _speculative_lane()
    evs = _events(rows)
    prefix = evs[:4]
    assert [row["card_id"] for row in card_authoring(prefix, fold(prefix))] == ["card-0"]
    assert card_authoring(evs, fold(evs)) == []
