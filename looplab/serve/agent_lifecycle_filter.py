"""Conservative delivery-time filtering of superseded agent-origin pauses.

The append-only outbox is unchanged. A pause can contain an operator's message, so an
unknown author or unavailable/corrupt source always remains deliverable. This is a
notification filter, not a run control, and cannot retract an already delivered message.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import HTTPException
from looplab.core.node_evidence import read_bounded_regular_file
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token

_COMMAND_ID = re.compile(r'^cmd_[0-9a-f]{32}$')
_MAX_RECEIPT_BYTES = 2 * 1024 * 1024


def _snapshot(rd: Path):
    from looplab.harness.journals import read_event_source
    try:
        events = read_event_source(rd)
        uid = fold(events).run_uid
        generation = run_generation_token(events)
        if not uid or not generation:
            return None
        by_seq = {event.seq: event for event in events if type(event.seq) is int}
        # A later explicit resume/reopen closes an earlier pause even when the run paused again.
        resumes = [event.seq for event in events if event.type in ('resume', 'run_reopened')]
        return uid, generation, by_seq, max(resumes, default=-1)
    except (HTTPException, OSError, ValueError, TypeError):
        return None


def superseded_agent_pause(rd: Path, row: dict, cache: dict) -> bool:
    """True only with same-run event, later resume and matching agent command receipt."""
    seq = row.get('seq')
    if row.get('kind') != 'run_paused' or type(seq) is not int:
        return False
    if rd not in cache:
        cache[rd] = _snapshot(rd)
    state = cache[rd]
    if state is None:
        return False
    uid, generation, by_seq, last_resume = state
    event = by_seq.get(seq)
    if uid != row.get('run_uid') or last_resume <= seq or event is None or event.type != 'pause':
        return False
    data = event.data if isinstance(event.data, dict) else {}
    command_id = data.get('_command_id')
    if not isinstance(command_id, str) or not _COMMAND_ID.fullmatch(command_id):
        return False
    directory = rd / '.commands'
    if directory.is_symlink():
        return False
    raw = read_bounded_regular_file(directory / f'{command_id}.json', _MAX_RECEIPT_BYTES + 1)
    if raw is None or len(raw) > _MAX_RECEIPT_BYTES:
        return False
    try:
        receipt = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    matched = (isinstance(receipt, dict) and receipt.get('id') == command_id
            and receipt.get('event_type') == 'pause' and receipt.get('event_seq') == seq
            and receipt.get('run_generation') == generation and receipt.get('status') == 'succeeded'
            and receipt.get('submitted_by') in ('agent_client', 'agent_token'))
    if not matched:
        return False
    # The run may have been replaced while its receipt was read. Never suppress on an
    # uncertain incarnation; this read-side fence does not claim atomicity with delivery.
    from looplab.harness.journals import read_event_source
    try:
        return run_generation_token(read_event_source(rd)) == generation
    except (HTTPException, OSError, ValueError, TypeError):
        return False
