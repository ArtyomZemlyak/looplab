"""Optional recipient acknowledgement of an existing outbox event.

An acknowledgement tracks a handoff only. It does not resolve a checkpoint, change
an admission gate, or establish that any execution or scientific action occurred.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from fastapi import HTTPException
from looplab.core.atomicio import append_jsonl_bytes_locked
from looplab.events.run_generation import run_generation_token
from looplab.serve.agent_events import _lock, events_dir, read_outbox

ACKS = 'acknowledgements.jsonl'


def acknowledge(root: Path, run_id: str, generation: str, event_id: int,
                recipient: str, action_id: str, disposition: str) -> dict:
    from looplab.harness.journals import read_event_source, read_source
    rd = root / run_id
    events = read_event_source(rd)
    if run_generation_token(events) != generation:
        raise HTTPException(409, 'run generation changed')
    from looplab.events.replay import fold
    uid = fold(events).run_uid
    body = {'run_id': run_id, 'run_uid': uid, 'generation': generation, 'event_id': event_id,
            'recipient': recipient, 'action_id': action_id, 'disposition': disposition}
    request_sha = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    with _lock(root):
        path = events_dir(root) / ACKS
        rows, health = read_source(path)
        if not health.get('read_complete'):
            raise HTTPException(503, 'acknowledgement journal incomplete; repair before retry')
        previous = [r for r in rows if r.get('run_id') == run_id and r.get('run_uid') == uid
                    and r.get('generation') == generation and r.get('recipient') == recipient
                    and r.get('action_id') == action_id]
        if previous:
            if previous[-1].get('request_sha256') != request_sha:
                raise HTTPException(409, 'action_id already used for a different acknowledgement')
            return previous[-1]
        page = read_outbox(root, after=event_id - 1, run_id=run_id, limit=1)
        event = next((e for e in page['events'] if e['id'] == event_id), None)
        if event is None or event.get('run_id') != run_id or event.get('run_uid') != uid:
            raise HTTPException(404, 'event not retained for this run incarnation')
        if event.get('recipient_hook') and event.get('recipient_hook') != recipient:
            raise HTTPException(409, 'event is addressed to another recipient')
        # Recheck identity while holding the acknowledgement lock; it does not lock the event log.
        if run_generation_token(read_event_source(rd)) != generation:
            raise HTTPException(409, 'run generation changed')
        receipt = {**body, 'request_sha256': request_sha, 'recorded_at': time.time(),
                   'meaning': 'handoff acknowledgement only; underlying action remains separately governed'}
        append_jsonl_bytes_locked(path, json.dumps(receipt, sort_keys=True).encode() + b'\n')
        return receipt


def acknowledgements(root: Path, run_id: str, generation: str, recipient: str | None = None) -> list[dict]:
    from looplab.harness.journals import read_source
    rows, health = read_source(events_dir(root) / ACKS)
    if not health.get('read_complete'):
        raise HTTPException(503, 'acknowledgement journal incomplete')
    return [r for r in rows if r.get('run_id') == run_id and r.get('generation') == generation
            and (recipient is None or r.get('recipient') == recipient)][-100:]
