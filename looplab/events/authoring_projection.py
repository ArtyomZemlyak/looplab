"""The IN-FLIGHT AUTHORING view over the card board — a derived projection, not folded state.

Sibling of `belief_projection.py`: it reads the folded `RunState` (plus the log the fold consumed,
for timestamps) and returns plain dicts. Nothing here is folded, nothing here is written, and no
engine decision reads it.

WHY IT EXISTS. On the speculative lane a Card is authored in two long phases, and until the second
one COMMITS the board says the work has not started:

    card_added ─┬─ card_build_requested ── card_build_attempted ══════════════╗ node_building ── node_created
                │  (main task, ~0.3 s apart)  │                              ║  (main task)
                └─ Card.status = "proposed" ──┴── Developer writes the code ──╝
                   "work item is open and has not started"     (worker thread)

`node_building` is what `events/card_ledger.py::_card_building_ids` stamps ownership from, and it is
appended by `engine/speculation.py::_claim_requested_card_build` only AFTER the producer thread has
returned. Measured on `runs/rubertlite-dr-unified-v5` by folding the log at every prefix boundary:
card-0's build ran 2,128 s and the fold reported `status="proposed"` for 2,130.7 s of it — the
`building` lane was occupied for 0.3 s, between `node_building` and `node_created`.

The serial lane does not have this gap (`engine/card_reservation.py::_reserve_node_build` appends
`card_added` and its `node_building` claim in ONE batch, BEFORE the Developer runs), so this
projection is empty for a run with speculation off. It exists to make the speculative lane no less
observable than the serial lane it replaced.

WHY IT IS NOT A FOLD CHANGE, and why it is not `card_build_requested` folding to `status="building"`.
That exact proposal was tried and rejected inside the fold — see the long comment in
`events/card_ledger.py::_card_building_ids`. `_prepare_existing_card_claim` requires
`card.selection_ready`, and both `_producer_card_reservation` and `_commit_card_build` re-fold AFTER
the request is durable, so making the request its own ownership blocker means the producer can never
claim the card it was asked to build: every speculative build returns "stale". The request is a
LIVENESS fact about a process that is running right now; `Card.status` is a REPLAY fact about the
log. Publishing the first through the second is what would put liveness into `RunState`, which
resume-by-replay would then have to trust.

WHY THE TIMESTAMP COMES FROM THE EVENTS AND NOT FROM `RunState`. The obvious cheaper shape is to
stamp `e.ts` onto the folded `card_build_requests` / `card_build_attempts` rows the way
`_on_node_building` stamps `started` onto its marker. Do not: `search/speculation_quality.py` reads
those rows (lines ~1563/1601/1781/2210) into the calibration receipt, whose identity is
`canonical_json(body)` — a changed derivation revokes every issued receipt and costs six GPU runs.
The rows stay byte-identical; the timestamps are re-read from the log, which the caller already has
in hand.
"""
from __future__ import annotations

from typing import Mapping

from looplab.events.types import EV_CARD_BUILD_ATTEMPTED, EV_CARD_BUILD_REQUESTED

# The two lanes a not-yet-owned head can be in. They are deliberately the SAME strings as
# `ui/src/cardBoardModel.js::CARD_COLUMNS`' optional `speculating` lane and its always-present
# `building` lane — that table has carried both since the board shipped, with a comment saying the
# speculative lanes are "unreachable from the production Card projection" and asking for exactly this
# bounded owner state before advertising them. Adding a phase here means adding its column there.
AUTHORING_PHASES = ("speculating", "building")
# The open head is normally ONE row. The cap is against a corrupt/hand-edited prefix, not a workload.
AUTHORING_MAX_ROWS = 32


def card_authoring(events, st) -> list[dict]:
    """The open card-build head(s) as bounded provisional board rows, newest request last.

    ``events`` is the SAME list the caller folded into ``st`` — for a historical ``upto_seq`` fold
    that is the prefix, so this reports what was in flight at that point rather than what is in
    flight now. One row per open request the fold does not already own:

        {"card_id", "generation", "index", "phase", "started", "folded_status"}

    ``phase`` is ``"building"`` once a paid producer attempt exists for this exact head position
    (``card_build_attempted`` — appended by `_start_head_producer` BEFORE the provider call, so it is
    the honest boundary for "a Developer is writing this now"), else ``"speculating"``. ``started``
    is that receipt's own event timestamp, or None when the row predates it. ``folded_status`` is the
    fold's own answer, carried alongside so a consumer can always recover the replay truth and a
    reviewer can see exactly what this projection overlays.

    THE FOLD WINS whenever it has anything to say: only a card the fold still calls ``proposed`` is
    reported. That single rule is deliberately used instead of "skip cards named by a `node_building`
    marker", which it subsumes and which is not enough on its own — `node_created` CLEARS the marker
    while `card_builds_done` only advances at `card_build_done`, so a marker-only check re-reported
    the card as still-building for the whole window between them (0.4 s on
    `runs/rubertlite-dr-unified-v5`, i.e. a visible flicker back out of Running). Found by
    `tests/test_card_authoring_projection.py::test_the_projection_lets_go_the_instant_node_building_lands`.
    """
    requests = list(getattr(st, "card_build_requests", None) or ())
    try:
        done = int(getattr(st, "card_builds_done", 0) or 0)
    except (TypeError, ValueError):
        return []
    done = max(0, min(done, len(requests)))
    open_heads = [(index, request) for index, request in enumerate(requests)
                  if index >= done and isinstance(request, Mapping)]
    if not open_heads:
        return []

    attempts: set[tuple] = set()
    for attempt in (getattr(st, "card_build_attempts", None) or ()):
        if isinstance(attempt, Mapping):
            attempts.add((attempt.get("card_id"), attempt.get("generation"), attempt.get("index")))

    # LAST matching receipt wins: a crash-recovery re-entry re-attempts the same head position, and
    # the operator's clock must measure the producer that is running, not the one that died.
    requested_at: dict[tuple, float] = {}
    attempted_at: dict[tuple, float] = {}
    for event in events:
        data = getattr(event, "data", None)
        if not isinstance(data, Mapping):
            continue
        ts = getattr(event, "ts", None)
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        if event.type == EV_CARD_BUILD_REQUESTED:
            requested_at[(data.get("card_id"), data.get("generation"))] = float(ts)
        elif event.type == EV_CARD_BUILD_ATTEMPTED:
            attempted_at[(data.get("card_id"), data.get("generation"), data.get("index"))] = float(ts)

    cards = getattr(st, "cards", None) or {}
    rows: list[dict] = []
    for index, request in open_heads:
        card_id = request.get("card_id")
        generation = request.get("generation")
        if not isinstance(card_id, str):
            continue
        card = cards.get(card_id)
        # A head whose card the projection cannot see is not a board row. Synthesizing one would put a
        # statement-less card on the board, which is the failure `_card_building_ids` also refuses.
        # `status != "proposed"` then covers dropped/merged/building/running/evaluated/gated in one
        # rule — see the docstring for why the marker check alone was not enough.
        if card is None or getattr(card, "status", None) != "proposed":
            continue
        key = (card_id, generation, index)
        if key in attempts:
            phase, started = "building", attempted_at.get(key)
        else:
            phase, started = "speculating", requested_at.get((card_id, generation))
        rows.append({
            "card_id": card_id,
            "generation": generation if type(generation) is int else None,
            "index": index,
            "phase": phase,
            "started": started,
            "folded_status": getattr(card, "status", None),
        })
        if len(rows) >= AUTHORING_MAX_ROWS:
            break
    return rows
