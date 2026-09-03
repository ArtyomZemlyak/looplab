"""Whether the process serving this payload is still running the code that is on disk.

A long-lived `looplab ui` process PINS ITS OWN CODE exactly the way a running engine does: every
module was read once, at import, and no merge afterwards reaches it. That is invisible from the
browser, because a stale server does not fail — it answers 200 with a payload built by last week's
fold, and every downstream layer faithfully renders the smaller truth it was handed.

MEASURED, NOT HYPOTHESISED. On 2026-09-03 the operator reported the question ladder showing twelve
questions with nothing attached and every one of them "not measured yet". The fold on disk gave 17
`parent_card_id` edges and 7 questions with children; the wire built in-process from the same log
gave the same 17; the shipped bundle contained the fixed reader. The payload the SERVER returned
carried `parent_card_id` on 0 of 34 cards, `child_rollup` on 0, and no `child_concept_tags` field at
all — a DTO of 30 fields against the tree's 55. That process had been up 9 days 5 hours, since
before the fold learned to keep the edge. Restarting it restored all three numbers at once.

WHAT THIS REPORTS, AND WHAT IT DOES NOT. It compares the mtimes of the package's `.py` files now
against the snapshot taken when this module was imported — which is boot, since `serve.appstate`
imports it. It therefore reports THE TREE MOVED UNDER A RUNNING PROCESS, not "a module this process
actually uses changed": a file the server never imports still counts. That over-report is deliberate
and is the honest direction to err, because the server cannot know which module a later request will
import lazily, and because the remedy is the same either way — restart. It is a NOTICE and never a
refusal: a stale server still serves, and deciding when to restart is the operator's call.

It cannot see a change that leaves mtime untouched (a restored copy), and it says nothing about
whether the engine — a different process, with its own pinned code — is current.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import looplab

# The package the server runs out of. Resolved once: the answer cannot change inside a process, and
# `__file__` is the only thing that knows where this code was actually loaded from.
PACKAGE_ROOT = Path(looplab.__file__).resolve().parent

# A bound on the walk, so a tree that has grown a vendored subtree cannot make a per-request check
# unbounded work. Reaching it is reported rather than silently truncating the comparison.
MAX_TRACKED_FILES = 5_000
# How many changed paths ride on the wire. The COUNT is exact; the list is a sample for the notice.
MAX_REPORTED_CHANGES = 8
# The check costs a stat per file (335 files, 12 ms measured on this box). Cheap, but it runs on a
# 2.5 s poll per open browser, so answer from a short cache rather than walking every time.
CACHE_SECONDS = 30.0


def snapshot(root: Path | None = None) -> dict[str, int]:
    """Map every tracked `.py` under `root` to its mtime in nanoseconds, keyed by relative path."""
    base = Path(root) if root is not None else PACKAGE_ROOT
    out: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(base):
        # `__pycache__` holds COMPILED copies whose mtimes move on import alone — including this
        # process's own imports. Tracking them would make every server report itself stale.
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            if len(out) >= MAX_TRACKED_FILES:
                return out
            full = Path(dirpath) / name
            try:
                out[str(full.relative_to(base))] = full.stat().st_mtime_ns
            except OSError:
                # A file that vanished mid-walk is a change, and recording it as missing is what
                # makes the next comparison see one.
                continue
    return out


BOOT_SNAPSHOT: dict[str, int] = snapshot()


def code_freshness(root: Path | None = None, boot: dict[str, int] | None = None) -> dict:
    """Compare the tree now against the snapshot taken when this process imported the package."""
    base = BOOT_SNAPSHOT if boot is None else boot
    now = snapshot(root)
    changed = sorted(
        set(base) ^ set(now)                                      # added or removed
        | {path for path in set(base) & set(now) if base[path] != now[path]})  # rewritten
    return {
        "stale": bool(changed),
        "changed_count": len(changed),
        "changed": changed[:MAX_REPORTED_CHANGES],
        "changed_truncated": len(changed) > MAX_REPORTED_CHANGES,
        "files_at_boot": len(base),
        "files_now": len(now),
        # A walk that hit the bound compared a PREFIX of the tree, so `stale: false` from it means
        # "nothing changed in the part I looked at" and must not be read as a clean bill.
        "complete": len(now) < MAX_TRACKED_FILES and len(base) < MAX_TRACKED_FILES,
    }


_cache_lock = threading.Lock()
_cached: tuple[float, dict] | None = None


def cached_code_freshness(clock=time.monotonic) -> dict:
    """`code_freshness()` answered from a short cache — the per-request entry point."""
    global _cached
    with _cache_lock:
        if _cached is not None and clock() - _cached[0] < CACHE_SECONDS:
            return _cached[1]
    fresh = code_freshness()
    with _cache_lock:
        _cached = (clock(), fresh)
    return fresh
