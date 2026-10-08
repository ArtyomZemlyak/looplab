"""Bounded read-only access to an attempt's recorded pre-overlay seed archive.

No live checkout/workdir fallback, materialization, mount resolution or execution.
The archive verifier feeds the exact hashed bytes to the inventory/text projection.
"""
from __future__ import annotations

import hashlib

from looplab.core.seed_receipt import RECEIPT_UNUSABLE, bind_seed_receipt, node_base_receipt
from looplab.engine.seed_archive import verified_seed_archive
from looplab.events.replay import event_generation_binds

TEXT_LIMIT = 256 * 1024


class SeedFilesUnavailable(ValueError):
    """The recorded source cannot support this read."""


def seed_files(run_dir, node, events, *, offset=0, limit=100, path=None):
    # The receipt -> seed-event binding is `core/seed_receipt.py::bind_seed_receipt`, the rule the
    # upstream lane and `looplab export-git` share; the two refusals keep their two sentences.
    if node is None or node.tombstoned or node.status.value != "evaluated":
        raise SeedFilesUnavailable("This completed attempt has no usable recorded base archive.")
    bound = bind_seed_receipt(node_base_receipt(node), {event.seq: event for event in events},
                              node_id=node.id, generation=node.attempt,
                              terminal_event_seq=node.terminal_event_seq,
                              generation_binds=event_generation_binds)
    if bound.reason == RECEIPT_UNUSABLE:
        raise SeedFilesUnavailable("This completed attempt has no usable recorded base archive.")
    if bound.digest is None:
        raise SeedFilesUnavailable("The base archive is not bound to this attempt's measured evidence.")
    receipt, digest = bound.receipt, bound.digest
    rows, selected = [], None

    def capture(name, data, executable):
        nonlocal selected
        row = {"path": name, "bytes": len(data), "executable": bool(executable),
               "sha256": hashlib.sha256(data).hexdigest()}
        rows.append(row)
        if name != path:
            return
        text, status = None, "too_large"
        if len(data) <= TEXT_LIMIT:
            try:
                text = data.decode("utf-8")
                status = "binary" if "\x00" in text else "utf8"
                if status == "binary":
                    text = None
            except UnicodeDecodeError:
                status = "binary"
        selected = {**row, "text_status": status, "text": text}

    if verified_seed_archive(run_dir, receipt, on_file=capture) is None:
        raise SeedFilesUnavailable("The recorded base archive is missing, changed or unreadable.")
    rows.sort(key=lambda row: row["path"])
    if path is not None and selected is None:
        raise SeedFilesUnavailable("This file is not in the verified base archive.")
    page = rows[offset:offset + limit]
    return {"version": 1, "scope": "recorded_seed_before_mounts_and_overlay",
            "node_id": node.id, "attempt": node.attempt, "base_digest": digest,
            "offset": offset, "limit": limit, "total": len(rows), "files": page,
            "next_offset": offset + len(page) if offset + len(page) < len(rows) else None,
            "file": selected, "text_limit": TEXT_LIMIT}
