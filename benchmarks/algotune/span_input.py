#!/usr/bin/env python3
"""Rebuild the FULL prompt a `generation` span was given, from a compressed `spans.jsonl`.

WHY THIS FILE EXISTS. Spans do not store the whole message list per turn. `core/tracing.py:634`
writes `h.set_many(input=cur[np:], input_carry=np, input_from=prev[0])` -- so when `input_from` is
set, `input` holds only the SUFFIX (this turn's new messages) and `input_carry` is an INTEGER, the
number of messages carried from the parent span's reconstructed list. A fresh sub-loop stores a full
base instead (`input_carry=0, input_from=None`), and an old log with no `input_carry` at all means
`input` IS the complete list (tracing.py:619).

Reading `input` as if it were the whole prompt is not a small error. On 2026-08-28, dsBud had 67 of
its 81 generations chained; a reader that stopped at `input` saw only the delta and reported the
budget line in 3 of 32 step prompts when the true figure is 35 of 35. The same bug undercounted the
step-feedback block in dsFB (49 of 125 reported, 90 of 125 true) and the invalid-solution reason in
gpt56luna (155 of 868 reported, 402 of 868 true). Every conclusion drawn from those counts was
directionally right and numerically wrong, which is the worst kind of wrong to leave in a document.
"""
from __future__ import annotations

import json
import sys


def load(path):
    """Return (spans, by_id). Malformed lines are skipped, not fatal -- a live log has a torn tail."""
    spans, by_id = [], {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                span = json.loads(line)
            except ValueError:
                continue
            spans.append(span)
            if span.get("span_id"):
                by_id[span["span_id"]] = span
    return spans, by_id


def resolve(span, by_id, _depth=0, _seen=None):
    """The complete message list this span was called with.

    Falls back to the span's own `input` for every reason a chain can fail -- no parent, a parent
    that is not in this file, a non-integer carry from an older writer, a cycle, or a chain deeper
    than any real conversation -- because a partial answer beats an exception in a measuring tool.
    """
    attrs = span.get("attributes") or {}
    own = list(attrs.get("input") or [])
    src = attrs.get("input_from")
    carry = attrs.get("input_carry")
    if not src or src not in by_id or not isinstance(carry, int) or carry <= 0 or _depth > 500:
        return own
    _seen = _seen or set()
    if src in _seen:
        return own
    _seen.add(src)
    return list(resolve(by_id[src], by_id, _depth + 1, _seen))[:carry] + own


def text(span, by_id, roles=None):
    """The resolved prompt flattened to one searchable string, optionally filtered by role."""
    return " ".join(str((m or {}).get("content") or "")
                    for m in resolve(span, by_id)
                    if isinstance(m, dict) and (roles is None or m.get("role") in roles))


def main(argv):
    if len(argv) < 3:
        print("usage: span_input.py <spans.jsonl> <needle> [phase]", file=sys.stderr)
        return 2
    path, needle = argv[1], argv[2]
    phase = argv[3] if len(argv) > 3 else None
    spans, by_id = load(path)
    total = hits = 0
    for span in spans:
        attrs = span.get("attributes") or {}
        if span.get("name") != "generation":
            continue
        if phase and attrs.get("phase") != phase:
            continue
        total += 1
        hits += needle in text(span, by_id)
    print(f"{hits}/{total} generations" + (f" in phase {phase}" if phase else "") + f" contain {needle!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
