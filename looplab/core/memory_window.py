"""Canonical bounded JSONL snapshot shared by human and agent memory readers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

MEMORY_SOURCE_BYTES = 2 * 1024 * 1024
MEMORY_SOURCE_ROWS = 1000
MEMORY_SOURCE_ROW_BYTES = 128 * 1024


def read_memory_jsonl_window(
    path: str | Path, *, loads=json.loads, max_bytes: int = MEMORY_SOURCE_BYTES,
    max_rows: int = MEMORY_SOURCE_ROWS, max_row_bytes: int = MEMORY_SOURCE_ROW_BYTES,
) -> tuple[list[tuple[int, object]], dict]:
    """Read one newline-aligned recent snapshot with an exact, portable receipt.

    The returned integer is the row's index inside this captured window.  Consumers that need a
    narrower schema account for rejected decoded values on top of ``skipped``.  A missing ledger is a
    healthy empty source; an unreadable ledger is explicitly unavailable.
    """
    receipt = {
        "source_window_truncated": False,
        "source_rows": 0,
        "skipped": 0,
        "unavailable": False,
        "source_size": 0,
        "window_digest": hashlib.sha256(b"").hexdigest(),
    }
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            start = max(0, end - max_bytes)
            preceding = b"\n"
            if start:
                handle.seek(start - 1)
                preceding = handle.read(1)
            handle.seek(start)
            raw = handle.read(end - start)
    except FileNotFoundError:
        return [], receipt
    except OSError:
        receipt["unavailable"] = True
        return [], receipt

    receipt["source_size"] = end
    receipt["source_window_truncated"] = start > 0
    if start and preceding != b"\n":
        boundary = raw.find(b"\n")
        receipt["skipped"] += 1
        if boundary < 0:
            receipt["window_digest"] = hashlib.sha256(raw).hexdigest()
            return [], receipt
        raw = raw[boundary + 1:]

    # Split ONLY on the JSONL record delimiter, the rule `core/jsonlio.py` states and this copy
    # broke: `bytes.splitlines` also splits bare CR, form feed and vertical tab, which are bytes
    # INSIDE one poisoned record. The same stores are read both ways — the Researcher's priors and
    # `tools/memory_tools.py` come through this window, while `serve/memory_cascade.py` and
    # `engine/lesson_hygiene.py` come through `read_jsonl_lenient` — so a row containing a bare CR
    # was two rows here and one row there, and one of those readers DELETES from the store.
    encoded = raw.split(b"\n")
    if raw.endswith(b"\n"):
        encoded.pop()
    if len(encoded) > max_rows:
        encoded = encoded[-max_rows:]
        receipt["source_window_truncated"] = True
    receipt["source_rows"] = len(encoded)
    receipt["window_digest"] = hashlib.sha256(b"\n".join(encoded)).hexdigest()

    rows: list[tuple[int, object]] = []
    for index, line in enumerate(encoded):
        if not line.strip():
            continue
        if len(line) > max_row_bytes:
            receipt["skipped"] += 1
            continue
        try:
            rows.append((index, loads(line)))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
            receipt["skipped"] += 1
    return rows, receipt
