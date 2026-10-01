"""Read a saved command without reconciliation, leases, workers or an exclusive lock.

The existing command GET deliberately restarts nonterminal workers. Recovery needs
an observation before choosing that action, including when the POST response (and
command ID) was lost. A snapshot may lag the event log; it is not proof that a
candidate evaluated, that a process lives, or that a retry is safe.
"""
from __future__ import annotations

import hmac
import json
import re

from fastapi import HTTPException

from looplab.core.node_evidence import read_bounded_regular_file
from looplab.core.redact import redact_persisted_text
from looplab.serve.command_identity import command_identity
from looplab.serve.http import refusal
from looplab.serve.run_commands import CONTROL_SPECS, TERMINAL_STATUSES, _normalize_expected_generation

_MAX_RECORD_BYTES = 2 * 1024 * 1024


def snapshot(service, rd, expected_generation, *, command_id="", idempotency_key=""):
    expected = _normalize_expected_generation(expected_generation)
    if bool(command_id) == bool(idempotency_key):
        raise HTTPException(400, "Supply exactly one command_id or Idempotency-Key")
    digest = None
    if idempotency_key:
        command_id, digest = command_identity(idempotency_key)
    rd, generation = service.generation_fence(rd)
    if generation != expected:
        raise HTTPException(409, "run generation changed")
    try:
        path = service._path(rd, command_id)
        if not path.exists():
            raise HTTPException(404, "No saved receipt; absence does not prove the command was never applied")
        raw = read_bounded_regular_file(path, _MAX_RECORD_BYTES + 1)
        if raw is None or len(raw) > _MAX_RECORD_BYTES:
            raise HTTPException(503, "command receipt unavailable")
        record = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise HTTPException(503, "command receipt unavailable") from exc
    except OSError as exc:
        raise refusal("run_path_unreadable") from exc
    if (not isinstance(record, dict) or record.get("id") != command_id
            or not isinstance(record.get("status"), str)
            or record["status"] not in TERMINAL_STATUSES | {"accepted", "executing"}
            or not isinstance(record.get("event_type"), str)
            or record["event_type"] not in CONTROL_SPECS):
        raise HTTPException(503, "command receipt invalid")
    if digest is not None:
        stored = record.get("idempotency_key_digest")
        if (not isinstance(stored, str) or re.fullmatch(r"[0-9a-f]{64}", stored) is None
                or not hmac.compare_digest(stored, digest)):
            raise HTTPException(409, "idempotency command-id collision")
    if record.get("run_generation") != generation:
        raise HTTPException(409, "receipt belongs to another or unknown run generation")
    # Fence across the read; a reset cannot turn an old receipt into current evidence.
    if service.generation_fence(rd)[1] != generation:
        raise HTTPException(409, "run generation changed during receipt read")
    seq = record.get("event_seq")
    error = record.get("error") if isinstance(record.get("error"), dict) else {}
    return {"version": 1, "generation": generation, "terminal": record["status"] in TERMINAL_STATUSES,
            "command": {"id": command_id, "event_type": record["event_type"], "status": record["status"],
                        "event_seq": seq if type(seq) is int and seq >= 0 else None,
                        "error_code": redact_persisted_text(error.get("code", ""), max_chars=256,
                                                             entropy=False, single_line=True),
                        "retryable": error.get("retryable") is True},
            "meaning": "Saved receipt only; no reconciliation or worker restart. It may lag events. Check current state, node results and checkpoints before choosing recovery."}
