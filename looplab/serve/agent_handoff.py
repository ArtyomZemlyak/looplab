"""Optional, addressed self-attested handoffs on LoopLab's existing durable outbox.

A handoff wakes a recipient; it does not validate readiness, grant authority or schedule work.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from fastapi import HTTPException
from looplab.core.atomicio import append_jsonl_bytes_locked
from looplab.core.redact import redact_persisted_text
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.serve.agent_events import (_lock, _rotate_if_large,
    _write_json, check_hook_name, events_dir, OUTBOX, STATE, _OUTBOX_MAX_BYTES)


def publish(root: Path, run_id: str, generation: str, action_id: str, recipient: str,
            reason: str, next_read: str, urgency: str) -> dict:
    from looplab.harness.journals import read_event_source, read_source
    rd = root / run_id
    events = read_event_source(rd)
    if run_generation_token(events) != generation:
        raise HTTPException(409, 'run generation changed')
    uid = fold(events).run_uid
    recipient = check_hook_name(recipient)
    reason = redact_persisted_text(reason, max_chars=300, entropy=False, single_line=True)
    if not re.fullmatch(r'/api/[A-Za-z0-9_./%:-]+', next_read) or '..' in next_read or '//' in next_read:
        raise HTTPException(400, 'next_read must be a relative API path')
    fields = {'run_id': run_id, 'run_uid': uid, 'generation': generation, 'action_id': action_id,
              'recipient_hook': recipient, 'reason': reason, 'next_read': next_read, 'urgency': urgency}
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    key = f'{uid}:handoff:{action_id}'
    d = events_dir(root)
    with _lock(root):
        if run_generation_token(read_event_source(rd)) != generation:
            raise HTTPException(409, 'run generation changed')
        state_path = d / STATE
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise HTTPException(503, 'outbox state unreadable; operator recovery required') from exc
            if not isinstance(state, dict):
                raise HTTPException(503, 'outbox state malformed; operator recovery required')
        else:
            state = {}
        actions = state.setdefault('handoff_actions', {})
        if not isinstance(actions, dict):
            raise HTTPException(503, 'handoff ledger malformed; operator recovery required')
        prior = actions.get(key)
        retained = []
        for path in (d / f'{OUTBOX}.1', d / OUTBOX):
            source, health = read_source(path)
            if not health.get('read_complete'):
                raise HTTPException(503, 'outbox incomplete; operator recovery required')
            retained.extend(source)
        existing = next((row for row in retained if row.get('key') == key), None)
        if prior or existing:
            known = prior or existing
            if known.get('request_sha256') != digest:
                raise HTTPException(409, 'handoff action_id already used with different content')
            if not prior:
                actions[key] = {'id': known.get('id'), 'request_sha256': digest}
                state['next_id'] = max(int(state.get('next_id', 0) or 0), known['id'] + 1)
                _write_json(d / STATE, state)
            return {'event_id': known.get('id'), 'request_sha256': digest, 'replayed': True}
        if len(actions) >= 20000:
            raise HTTPException(409, 'handoff idempotency ledger full; operator recovery required')
        max_id = max((r.get('id', -1) for r in retained if type(r.get('id')) is int), default=-1)
        eid = max(int(state.get('next_id', 0) or 0), max_id + 1)
        row = {'id': eid, 'ts': time.time(), 'key': key, 'kind': 'agent_handoff', **fields,
               'summary': reason, 'owner': recipient, 'authority': 'notification_only',
               'request_sha256': digest}
        _rotate_if_large(d / OUTBOX, _OUTBOX_MAX_BYTES)
        append_jsonl_bytes_locked(d / OUTBOX, json.dumps(row, sort_keys=True).encode() + b'\n')
        actions[key] = {'id': eid, 'request_sha256': digest}
        state['next_id'] = eid + 1
        _write_json(d / STATE, state)
        return {'event_id': eid, 'request_sha256': digest, 'replayed': False}
