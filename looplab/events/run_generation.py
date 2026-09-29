"""The stable identity of one event-log GENERATION, from its first durable event.

A pure function of the log: `run_generation_token` hashes the first event's `(seq, ts, type,
run_id)`, so it is stable as the run grows and distinct across an in-place reset. It lived in
`serve/run_commands.py`, where the durable command lifecycle first needed it; the external-harness
package writes its sidecars under the same generation fence, and reaching it there pulled the whole
server into `harness/` — and, through `engine/external_watch.py`, into the engine (the edge
`tests/test_package_layering.py` forbids). `serve/run_commands.py` re-exports it as the SAME object.
"""
from __future__ import annotations

import hashlib
import json

from looplab.core.models import Event


def run_generation_token(events) -> str:
    """Return the stable lowercase token for one non-empty event-log generation.

    An in-place reset archives the entire log and a replacement engine writes a new first event.
    Basing the token on that durable event keeps it stable as the same run grows, while making the
    old and replacement logs distinct without a mutable sidecar that could drift from events.jsonl.
    Empty/startup logs deliberately have no token: accepting a mutation before there is durable
    generation identity would re-open the exact reset race this precondition closes.
    """
    iterator = iter(events)
    try:
        try:
            first = next(iterator, None)
        except OSError:
            # A concurrent delete/replace or transient filesystem read failure is not a trustworthy
            # generation. Match EventStore.read_all's fail-closed empty-prefix behavior.
            return ""
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
    if first is None:
        return ""
    if isinstance(first, dict):
        try:
            first = Event(**first)
        except Exception:  # noqa: BLE001 - match EventStore's fail-closed invalid-record boundary
            return ""
    seq = first.seq
    timestamp = first.ts
    event_type = first.type
    data = first.data or {}
    raw = json.dumps({
        "seq": seq, "ts": timestamp, "type": event_type,
        "run_id": data.get("run_id"),
    }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
