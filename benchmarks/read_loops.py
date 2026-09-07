#!/usr/bin/env python3
"""One file, read to death: the same path fetched over and over inside one phase.

WHY THIS EXISTS. `oldCK8` (2026-09-03, batch 4 of §115's arm) spent **$0.9574 of its $1.00 in
`propose`** and produced no node at all. It read `reference_edge_expansion.py` **189 times** — a
248-line file — walking it ONE LINE AT A TIME:

    {"lines":1,"path":"reference_edge_expansion.py","start_line":25}
    {"lines":1,"path":"reference_edge_expansion.py","start_line":26}
    {"lines":1,"path":"reference_edge_expansion.py","start_line":27}

Each read returned 72-102 characters and cost a whole turn, with the entire growing conversation
re-sent (§152: 84.7 % of prompt tokens are a byte-identical re-send at a flat rate). The run ended
`abandoned / error_terminal` after 1333 s.

**Every existing net misses this shape.** `tool_loop.py`'s repeat note keys on identical
`(tool, canonical-args)` — the args differ on every call, so `repeat_streak` stayed 1 on 192 of 194
reads. The identical-result note keys on identical results — the results differ, one line each.
`agent_max_turns` is 0 (unlimited) in every probe's `config.snapshot.json`. And the "read a file
ONCE, don't re-read" sentence lives in the `plan` user message, not in `propose`.

So the signature has to be the PATH, not the arguments: N fetches of one path inside one phase,
however the ranges differ.

Usage:
    read_loops.py [PROBE_ROOT] [--threshold N]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import re

READ_TOOLS = {"repo_read", "read_file", "read_installed"}
DEFAULT_ROOT = "/var/tmp/looplab-bench/model-probes"
_PATH = re.compile(r'"(?:path|file)"\s*:\s*"([^"]+)"')


def loops(spans_path: str) -> list[tuple[str, str, int, float]]:
    """`(phase, path, reads, cost_of_that_phase)` for every (phase, path) pair in one run.

    Cost is the phase's whole generation spend, not the read's: a read costs a TURN, and the turn's
    price is the prompt it carried. Attributing per-read would understate it by the prefix.
    """
    reads: collections.Counter = collections.Counter()
    phase_cost: collections.Counter = collections.Counter()
    try:
        fh = open(spans_path, encoding="utf-8", errors="replace")
    except OSError:
        return []
    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                span = json.loads(line)
            except ValueError:
                continue
            attrs = span.get("attributes") or {}
            kind = span.get("kind")
            if kind == "generation":
                try:
                    phase_cost[attrs.get("phase") or "?"] += float(attrs.get("cost") or 0.0)
                except (TypeError, ValueError):
                    pass
            elif kind == "tool" and attrs.get("tool") in READ_TOOLS:
                found = _PATH.search(str(attrs.get("input", "")))
                if found:
                    reads[(attrs.get("phase") or "?", found.group(1))] += 1
    return [(ph, p, n, phase_cost[ph]) for (ph, p), n in reads.items()]


def scan(root: str, threshold: int = 25):
    """Every (run, phase, path) read at least `threshold` times, worst first."""
    out = []
    for spans in sorted(glob.glob(f"{root}/*/runs/*/run/spans.jsonl")):
        name = spans.split("/model-probes/")[-1].split("/")[0] if "/model-probes/" in spans \
            else spans.split("/")[-5]
        for phase, path, n, cost in loops(spans):
            if n >= threshold:
                out.append((n, name, phase, path, cost))
    out.sort(reverse=True)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default=DEFAULT_ROOT)
    ap.add_argument("--threshold", type=int, default=25,
                    help="report a (run, phase, path) read at least this many times (default 25)")
    args = ap.parse_args(argv)
    rows = scan(args.root, args.threshold)
    print(f"(run, phase, path) triples read >= {args.threshold} times:")
    if not rows:
        print("  none")
        return 0
    print(f'{"reads":>6s}  {"run":11s} {"phase":14s} {"$ of that phase":>15s}  path')
    for n, name, phase, path, cost in rows:
        print(f"{n:6d}  {name:11s} {phase:14s} {cost:15.4f}  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
