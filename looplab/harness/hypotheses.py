"""External consolidation of the live pure-belief board.

The built-in hybrid merge writes ``hypothesis_merged``. Here the coding agent
can review the same board, merge aliases or explicitly keep distinct beliefs.
One atomic event batch couples a merge to its reviewed-board receipt.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import orjson
from fastapi import HTTPException

from looplab.core.config import read_config_snapshot
from looplab.events.eventstore import (EventStore, EventStoreConcurrencyError,
                                       EventStoreLockError)
from looplab.events.replay import fold
from looplab.events.types import EV_HYPOTHESIS_MERGED, EV_HYPOTHESIS_MERGE_REVIEWED
from looplab.harness.obligations import evidence_revision
from looplab.harness.journals import read_event_source
from looplab.events.run_generation import run_generation_token


def board_revision(state) -> str:
    board = sorted((card.id, card.statement) for card in state.open_pure_beliefs())
    return hashlib.sha256(orjson.dumps({"board": board,
                                        "outcomes": evidence_revision(state)},
                                       option=orjson.OPT_SORT_KEYS)).hexdigest()


def merge_due(settings, state, events) -> bool:
    if not (settings.external_harness and settings.track_hypotheses):
        return False
    if len(state.open_pure_beliefs()) < 4:
        return False
    revision = board_revision(state)
    return not any(event.type == EV_HYPOTHESIS_MERGE_REVIEWED
                   and event.data.get("board_sha256") == revision for event in events)


def board_status(rd: Path, expected_generation: str) -> dict:
    events = read_event_source(rd)
    if run_generation_token(events) != expected_generation.lower():
        raise HTTPException(409, "run generation changed")
    settings = read_config_snapshot(rd / "config.snapshot.json")
    state = fold(events)
    return {"due": merge_due(settings, state, events),
            "board_sha256": board_revision(state),
            "open_beliefs": [{"id": card.id, "statement": card.statement}
                             for card in state.open_pure_beliefs()]}


def review_board(srv, rd: Path, body) -> dict:
    try:
        with srv.commands.sequence(rd):
            settings = read_config_snapshot(rd / "config.snapshot.json")
            if not (settings.external_harness and settings.track_hypotheses):
                raise HTTPException(409, "external hypothesis review is not enabled")
            store = EventStore(rd / "events.jsonl")
            events = read_event_source(rd, store=store)
            generation = run_generation_token(events)
            if not generation or generation != body.expected_generation.lower():
                raise HTTPException(409, "run generation changed")
            state = fold(events)
            older = next((e for e in events if e.type == EV_HYPOTHESIS_MERGE_REVIEWED
                          and e.data.get("action_id") == body.action_id), None)
            chosen = {"decision": body.decision, "canonical": body.canonical,
                      "aliases": body.aliases, "statement": body.statement,
                      "reason": body.reason}
            if older is not None:
                if (older.data.get("board_sha256") != body.expected_board_sha256.lower()
                        or any(older.data.get(key) != value for key, value in chosen.items())):
                    raise HTTPException(409, "hypothesis review action_id was reused differently")
                return {"ok": True, "replayed": True, "review": older.data}
            if not merge_due(settings, state, events):
                raise HTTPException(409, "no new open-belief board review is due")
            if body.expected_board_sha256.lower() != board_revision(state):
                raise HTTPException(409, "open-belief board or measured outcomes changed")
            current = {card.id: card for card in state.open_pure_beliefs()}
            if body.decision == "merge":
                if (body.canonical not in current or not body.aliases
                        or len(set(body.aliases)) != len(body.aliases)
                        or body.canonical in body.aliases
                        or any(alias not in current for alias in body.aliases)):
                    raise HTTPException(400, "merge requires distinct live pure-belief IDs")
                if not body.statement.strip():
                    raise HTTPException(400, "merged statement is required")
            elif body.canonical or body.aliases or body.statement:
                raise HTTPException(400, "no_merge cannot claim a canonical or aliases")
            receipt = {"action_id": body.action_id, "board_sha256": board_revision(state),
                       **chosen}
            records = []
            if body.decision == "merge":
                records.append((EV_HYPOTHESIS_MERGED, {
                    "canonical": body.canonical, "aliases": body.aliases,
                    "statement": body.statement, "at_node": len(state.nodes)}))
            records.append((EV_HYPOTHESIS_MERGE_REVIEWED, receipt))
            store.append_many(records, expected_last_seq=events[-1].seq, require_lock=True)
            return {"ok": True, "replayed": False, "review": receipt}
    except HTTPException:
        raise
    except EventStoreConcurrencyError as exc:
        raise HTTPException(409, "hypothesis board changed; refresh and retry") from exc
    except (OSError, EventStoreLockError, ValueError, KeyError) as exc:
        raise HTTPException(503, "hypothesis review unavailable; refresh the board") from exc
