#!/usr/bin/env python3
"""Requests whose body had been sent before, and what the gateway did with them.

WHY. §122 stamped `req_sha` (16 hex of SHA-256 over the raw request body) on every meter row so
that "the engine re-sent the same request" could be told from "the meter double-counted". It can
now, and the answer is the first: 224 of 6,232 stamped rows repeat a body, $0.3932.

What the ledger alone does not say is what happens upstream, and that is the number that decides
whether this is worth fixing. The repeat's median latency is 25.1 ms against the original's
4,570.1 ms -- a 182x collapse, reproduced live on 2026-09-03 with two identical requests through
the meter: 236.4 ms then 17.7 ms, same `req_sha`, same 4,012 prompt tokens, same $0.00056240. The
gateway is serving a cached body and billing it in full.

There is no cache-hit token field to read, either: the upstream `usage` object carries exactly
`prompt_tokens`, `completion_tokens`, `total_tokens` (`cost`/`cost_basis`/`cost_source` are the
proxy's own additions), and a least-squares fit over 22,757 rows gives a flat $0.14/Mtok in and
$0.28/Mtok out with a maximum residual of 1.4e-17. So the campaign's dollar figures are not
secretly discounted -- and equally, no discount can be recovered by reading a field.

Usage:
    resent_bodies.py [LEDGER] [--collapse-ratio R]
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys

LEDGER = "/var/tmp/looplab-bench/meter/meter.jsonl"


def scan(path: str, collapse_ratio: float = 0.25) -> dict:
    """Group metered rows by request body.

    `skipped_no_sha` is returned rather than ignored: `req_sha` only exists from §122 onward, so a
    tool that quietly drops those rows reports a share of a corpus it did not name.
    """
    first: dict[str, dict] = {}
    repeats: list[tuple[dict, dict]] = []
    stamped = 0
    skipped = 0
    stamped_cost = 0.0
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        sha = row.get("req_sha")
        if not sha:
            skipped += 1
            continue
        stamped += 1
        stamped_cost += float(row.get("cost") or 0.0)
        if sha in first:
            repeats.append((row, first[sha]))
        else:
            first[sha] = row

    def _lat(row) -> float:
        try:
            return float(row.get("latency_ms") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    served_from_cache = [r for r, f in repeats if _lat(f) > 0 and _lat(r) < collapse_ratio * _lat(f)]
    by_arm: collections.Counter = collections.Counter()
    for r, _ in repeats:
        by_arm[r.get("arm")] += float(r.get("cost") or 0.0)
    return {
        "stamped": stamped, "skipped_no_sha": skipped, "stamped_cost": stamped_cost,
        "repeats": len(repeats),
        "repeat_cost": sum(float(r.get("cost") or 0.0) for r, _ in repeats),
        "repeat_latency_ms": statistics.median([_lat(r) for r, _ in repeats]) if repeats else 0.0,
        "first_latency_ms": statistics.median([_lat(f) for _, f in repeats]) if repeats else 0.0,
        "served_from_cache": len(served_from_cache),
        "cache_cost": sum(float(r.get("cost") or 0.0) for r in served_from_cache),
        "by_arm": by_arm.most_common(5),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ledger", nargs="?", default=LEDGER)
    ap.add_argument("--collapse-ratio", type=float, default=0.25,
                    help="a repeat this much faster than its original was not generated (default "
                         "0.25, i.e. 4x faster)")
    args = ap.parse_args(argv)
    got = scan(args.ledger, args.collapse_ratio)
    print(f"rows carrying req_sha: {got['stamped']}, ${got['stamped_cost']:.4f}"
          f"   (skipped, stamped before §122: {got['skipped_no_sha']})")
    if not got["stamped"]:
        return 0
    print(f"bodies sent more than once: {got['repeats']} "
          f"({100 * got['repeats'] / got['stamped']:.1f} %), ${got['repeat_cost']:.4f}")
    print(f"  median latency  repeat {got['repeat_latency_ms']:9.1f} ms"
          f"   original {got['first_latency_ms']:9.1f} ms")
    print(f"  of those, served too fast to have been generated: {got['served_from_cache']} "
          f"(${got['cache_cost']:.4f}) -- billed in full")
    print("  worst arms: " + ", ".join(f"{a} ${c:.4f}" for a, c in got["by_arm"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
