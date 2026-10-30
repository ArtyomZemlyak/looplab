"""Optional, self-attested disposition of saved command intake notices.

No run admission or scientific review is affected. Legacy/transient warnings remain unknown.
"""
from __future__ import annotations
import hashlib
import json
import os
import stat
import re
import time
from pathlib import Path
from fastapi import HTTPException
from looplab.events.eventstore import interprocess_lock
from looplab.serve.command_receipt import snapshot as command_snapshot

JOURNAL = 'research_notice_dispositions.jsonl'
COMMAND_ID = re.compile(r'^cmd_[0-9a-f]{32}$')


def _dispositions(rd: Path, generation: str):
    from looplab.harness.journals import read_source
    if (rd / JOURNAL).is_symlink():
        raise HTTPException(409, 'notice journal must not be a symlink')
    rows, health = read_source(rd / JOURNAL)
    if not health.get('read_complete'):
        raise HTTPException(503, 'notice disposition history incomplete')
    return [r for r in rows if r.get('generation') == generation]


def _entries(srv, rd: Path, generation: str, command_id: str):
    if (rd / '.commands').is_symlink():
        raise HTTPException(409, 'command directory must not be a symlink')
    receipt = command_snapshot(srv.commands, rd, generation, command_id=command_id)
    result = []
    for index, notice in enumerate(receipt['intake_notices']):
        identifier = hashlib.sha256(json.dumps([generation, command_id, index, notice], sort_keys=True).encode()).hexdigest()
        result.append({'notice_id': identifier, 'command_id': command_id, 'event_seq': receipt['command']['event_seq'],
                       'notice': notice, 'command_status': receipt['command']['status']})
    return result


def snapshot(srv, rd: Path, generation: str, offset: int = 0, limit: int = 20):
    rd, current = srv.commands.generation_fence(rd)
    if current != generation:
        raise HTTPException(409, 'run generation changed')
    directory = rd / '.commands'
    try:
        paths = []
        if directory.is_symlink():
            raise HTTPException(409, 'command directory must not be a symlink')
        if directory.is_dir():
            for path in directory.iterdir():
                if COMMAND_ID.fullmatch(path.stem) and path.suffix == '.json' and not path.is_symlink():
                    paths.append(path.name)
                    if len(paths) > 10000:
                        raise HTTPException(413, 'too many command receipts for notice view')
        paths.sort()
    except OSError as exc:
        raise HTTPException(503, 'command receipt directory unavailable') from exc
    decisions = _dispositions(rd, generation)
    by_notice = {row.get('notice_id'): row for row in decisions}
    items = []
    unavailable = 0
    for name in paths[offset:offset + limit]:
        try:
            entries = _entries(srv, rd, generation, name[:-5])
        except HTTPException as exc:
            if exc.status_code == 409:
                continue  # historical generation, explicitly not current
            unavailable += 1
            continue
        for entry in entries:
            receipt = by_notice.get(entry['notice_id'])
            entry['disposition'] = receipt.get('disposition') if receipt else 'unreviewed'
            entry['reason'] = receipt.get('reason', '') if receipt else ''
            items.append(entry)
    if srv.commands.generation_fence(rd)[1] != generation:
        raise HTTPException(409, 'run generation changed during notice read')
    return {'generation': generation, 'items': items, 'offset': offset, 'limit': limit,
            'total_receipts': len(paths), 'has_more': offset + limit < len(paths), 'unavailable_receipts': unavailable,
            'meaning': 'Saved command intake notices only. Dispositions are self-attested, not proof of correction. Transient and pre-existing unreadable warnings remain unknown.'}


def record(srv, rd: Path, body):
    from looplab.core.redact import redact_persisted_text
    generation, command_id = body.expected_generation, body.command_id
    with srv.commands.sequence(rd):
        entries = _entries(srv, rd, generation, command_id)
        if not any(e['notice_id'] == body.notice_id for e in entries):
            raise HTTPException(404, 'notice not found in this command receipt')
        path = rd / JOURNAL
        with interprocess_lock(Path(str(path) + '.lock'), required=True):
            rows = _dispositions(rd, generation)
            request = {'generation': generation, 'notice_id': body.notice_id, 'command_id': command_id,
                       'action_id': body.action_id, 'disposition': body.disposition,
                       'reason': redact_persisted_text(body.reason, max_chars=500, entropy=False, single_line=True)}
            digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            old = next((r for r in rows if r.get('action_id') == body.action_id), None)
            if old:
                if old.get('request_sha256') != digest:
                    raise HTTPException(409, 'notice action_id reused with different content')
                return {'replayed': True, 'receipt': old}
            receipt = {**request, 'request_sha256': digest, 'recorded_at': time.time()}
            try:
                fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with os.fdopen(fd, 'a+b') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise HTTPException(409, 'notice journal must be a regular file')
                    stream.seek(0, os.SEEK_END)
                    if stream.tell():
                        stream.seek(-1, os.SEEK_END)
                        if stream.read(1) not in (b'\n', b'\r'):
                            stream.write(b'\n')
                    stream.write(json.dumps(receipt, sort_keys=True).encode() + b'\n')
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise HTTPException(503, 'notice journal unavailable') from exc
            return {'replayed': False, 'receipt': receipt}
