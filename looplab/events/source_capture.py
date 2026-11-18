"""Read-only complete/stable event capture for bounded offline/native diagnostics.

Uses the canonical decoder and dense sequence, without EventStore repairs, writes or prefix recovery.
Captured SHA identifies bytes, not authorship or remote storage flush.
"""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import orjson

from looplab.core.atomicio import file_identity

MAX_EVENT_CAPTURE_BYTES = 128 * 1024 * 1024


def capture_events(path: Path, *, max_bytes=MAX_EVENT_CAPTURE_BYTES):
    """Capture one unchanged complete source with canonical batch/sequence validation.

    No repair, private client access, source execution, engine startup, artifact/model payload reads or write.
    The byte digest identifies the captured input; this is not a remote storage-flush attestation.
    """
    from looplab.events.eventstore import decode_event_record, event_sequence_continues
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_EVENT_CAPTURE_BYTES:
        raise ValueError("event capture byte bound must be in1..128MiB")
    # A FIFO must be rejected as non-regular rather than blocking at open before fstat.
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ValueError('diagnostic requires a regular event source within 128 MiB')
        raw = stream.read(max_bytes + 1)
        after = os.fstat(stream.fileno())
    if file_identity(before) != file_identity(after) or len(raw) > max_bytes:
        raise ValueError('event source changed during capture; read a stable copy explicitly')
    if not raw or not raw.endswith(b'\n'):
        raise ValueError('diagnostic requires a complete event source, including its final newline')
    events, expected = [], 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            batch = decode_event_record(orjson.loads(line))
            if not event_sequence_continues(batch, expected):
                raise ValueError('invalid_sequence')
        except (ValueError, TypeError, orjson.JSONDecodeError) as exc:
            raise ValueError('invalid event source record, version, batch or dense sequence') from exc
        events.extend(batch)
        expected = batch[-1].seq + 1
    return events, {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
                    'logical_events': len(events), 'event_seq': expected - 1,
                    'observation': 'unchanged local descriptor during capture; remote flush not attested'}
