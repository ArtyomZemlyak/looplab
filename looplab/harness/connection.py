"""Bounded read-only identity discovery for an external client, independent of broad state."""
from pathlib import Path

from fastapi import HTTPException

from looplab.core.run_ref import run_address
from looplab.core.redact import redact_persisted_identity
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.journals import read_event_source
from looplab.harness.snapshot_settings import read_harness_settings


def _complete_tail(rd: Path):
    try:
        with (rd / "events.jsonl").open("rb") as stream:
            stream.seek(0, 2)
            if stream.tell():
                stream.seek(-1, 2)
                if stream.read(1) != b"\n":
                    raise HTTPException(503, {"code": "connection_source_incomplete"})
    except OSError as exc:
        raise HTTPException(503, {"code": "connection_source_unavailable"}) from exc


def snapshot(rd: Path, *, root: Path) -> dict:
    _complete_tail(rd)
    events = read_event_source(rd)
    _complete_tail(rd)
    generation = run_generation_token(events)
    state = fold(events)
    address = run_address(root, rd)
    uid = state.run_uid
    if (not generation or not isinstance(uid, str) or not uid or len(uid) > 256
            or len(address) > 256
            or redact_persisted_identity(uid, max_chars=256) != uid
            or redact_persisted_identity(address, max_chars=256) != address):
        raise HTTPException(409, {"code": "connection_identity_unavailable",
                                  "message": "Run lacks a supported durable identity."})
    try:
        settings = read_harness_settings(rd)
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(503, {"code": "connection_settings_unavailable"}) from exc
    if not settings.external_harness:
        raise HTTPException(409, {"code": "external_harness_required"})
    return {"version": 1, "run_id": address, "run_uid": uid, "generation": generation,
            "event_seq": events[-1].seq, "source_complete": True, "mode": "external_harness"}
