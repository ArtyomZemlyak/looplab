"""Bounded read-only access to an attempt's recorded pre-overlay seed archive.

No live checkout/workdir fallback, materialization, mount resolution or execution.
The archive verifier feeds the exact hashed bytes to the inventory/text projection.
"""
from __future__ import annotations

import hashlib

from looplab.engine.seed_archive import seed_archive_digest, verified_seed_archive
from looplab.events.replay import event_generation_binds

TEXT_LIMIT = 256 * 1024


class SeedFilesUnavailable(ValueError):
    """The recorded source cannot support this read."""


def seed_files(run_dir, node, events, *, offset=0, limit=100, path=None):
    receipt = (node.metric_provenance or {}).get("base_revision") if node else None
    digest = seed_archive_digest(receipt)
    if (node is None or node.tombstoned or node.status.value != "evaluated" or digest is None
            or any(type(receipt.get(key)) is not int or receipt[key] < 0
                   for key in ("node_id", "generation", "seed_event_seq"))
            or receipt["node_id"] != node.id or receipt["generation"] != node.attempt):
        raise SeedFilesUnavailable("This completed attempt has no usable recorded base archive.")
    seed = next((event for event in events if event.seq == receipt["seed_event_seq"]), None)
    seeded = seed.data.get("base_revision") if seed and isinstance(seed.data, dict) else None
    if (seed is None or seed.type != "workspace_seeded" or type(node.terminal_event_seq) is not int
            or seed.seq >= node.terminal_event_seq or type(seed.data.get("node_id")) is not int
            or seed.data["node_id"] != node.id
            or not event_generation_binds(seed.data, node.attempt)
            or seed_archive_digest(seeded) != digest
            or any(seeded[key] != receipt[key] for key in ("file_count", "bytes"))):
        raise SeedFilesUnavailable("The base archive is not bound to this attempt's measured evidence.")
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
