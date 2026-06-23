"""Atomic write helper (I1, ADR-17): temp file -> fsync -> os.replace.

Used for derived/snapshot files (config snapshot, HTML, SQLite is its own thing).
The event log itself uses append+fsync (see eventstore) — torn final lines are
tolerated on read, which is the durability contract we actually test.
"""
from __future__ import annotations

import os
from pathlib import Path


def atomic_write_bytes(path: str | os.PathLike, data: bytes) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)  # atomic on Win + POSIX


def atomic_write_text(path: str | os.PathLike, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))
