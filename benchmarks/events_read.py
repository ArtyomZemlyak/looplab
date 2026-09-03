#!/usr/bin/env python3
"""A LINE IN `events.jsonl` IS NOT AN EVENT.

The engine writes a crash-atomic packet whose `type` is the sentinel
`__looplab_event_batch_v1__` and whose real events live in `data.events`. A reader that keys on
`type` sees the packet and not its contents.

MEASURED over the 80 run logs in the probe corpus on 2026-09-03: 29,571 physical rows, of which
**11 are packets, in 11 different runs, and every one of them holds exactly one `node_failed` and
one `pause`**. So every node failure the corpus has ever recorded is invisible to a naive reader —
including the sweep line that has been reporting `fails=[]` for weeks, and six of the eight tools
under `benchmarks/` that read this file. `algotune/plot_corpus_v2.py::iter_events` is the one that
got it right, and its docstring is where this one starts.

Two spellings exist. The corpus writes `"type": ["__looplab_event_batch_v1__"]` — a one-element
LIST — and the engine's own tests exercise the bare STRING. Handle both, or the next writer change
silently re-hides the failures.
"""
from __future__ import annotations

import json

SENTINEL = "__looplab_event_batch_v1__"


def is_packet(row) -> bool:
    """True for a batch container, false for an ordinary event.

    A row is a packet only if it BOTH carries the sentinel and actually holds events: a row typed
    with the sentinel but with no `data.events` is something else -- a chat message quoting it, a
    truncated tail -- and swallowing it would lose a row rather than expand one.
    """
    if not isinstance(row, dict):
        return False
    kind = row.get("type")
    named = (kind == SENTINEL) or (isinstance(kind, list) and SENTINEL in kind)
    if not named:
        return False
    return isinstance((row.get("data") or {}).get("events"), list)


def iter_events(path):
    """Every logical event in `path`, packets expanded, torn lines skipped."""
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:      # a torn last line is normal on a LIVE run
                continue
            if is_packet(row):
                for inner in (row.get("data") or {}).get("events") or []:
                    if isinstance(inner, dict):
                        yield inner
            else:
                yield row


def read(path) -> list[dict]:
    """`iter_events` as a list, for callers that need to walk it more than once."""
    return list(iter_events(path))
