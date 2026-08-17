#!/usr/bin/env python3
"""Model Inspector full-span reads over an S3/FUSE range-request cost boundary.

The benchmark uses the production coalescing planner and deterministic synthetic JSONL offsets. It
does not require an S3 bucket: request latency and sequential throughput are explicit inputs, making
the trade-off between fewer GETs and gap-byte amplification reproducible in CI or on a laptop.
"""
from __future__ import annotations

import argparse
import math
import random
from dataclasses import dataclass

from looplab.events.span_index import (
    _FULL_READ_BATCH_MAX_BYTES, _FULL_READ_COALESCE_GAP_BYTES,
    _coalesced_full_read_plan)

MIB = 1024 * 1024


@dataclass(frozen=True)
class Result:
    name: str
    requests: int
    read_bytes: int
    selected_bytes: int
    modeled_ms: float

    @property
    def amplification(self) -> float:
        return self.read_bytes / self.selected_bytes if self.selected_bytes else 0.0


def synthetic_offsets(selected_rows: int, interleave: int, seed: int):
    """Return file metadata plus every ``interleave``-th row as one node's selection."""
    rng = random.Random(seed + selected_rows * 131 + interleave * 17)
    total_rows = selected_rows * interleave
    meta = []
    offset = 0
    for _ in range(total_rows):
        # A mix of compact operation/tool rows and prompt-heavy generation rows. The hard bounds are
        # intentionally below the production 8 MiB row ceiling; layout, not JSON parsing, is under
        # test here.
        length = max(600, min(90 * 1024, int(rng.lognormvariate(math.log(9 * 1024), 1.0))))
        meta.append((offset, length))
        offset += length + 1  # JSONL newline: adjacent rows are one byte apart in indexed extents.
    return meta, list(range(0, total_rows, interleave))


def result(name, plan, selected_bytes, latency_ms, throughput_mib_s):
    read_bytes = sum(length for _offset, length, _slices in plan)
    transfer_ms = read_bytes / (throughput_mib_s * MIB) * 1000
    return Result(name, len(plan), read_bytes, selected_bytes,
                  len(plan) * latency_ms + transfer_ms)


def run_case(selected_rows, interleave, seed, latency_ms, throughput_mib_s):
    meta, rows = synthetic_offsets(selected_rows, interleave, seed)
    selected_bytes = sum(meta[row][1] for row in rows)
    baseline = [(meta[row][0], meta[row][1], [(row, 0, meta[row][1])]) for row in rows]
    variants = [("per-row", baseline)]
    for name, gap in (("4 KiB", 4 * 1024), ("64 KiB", 64 * 1024),
                      ("256 KiB (prod)", _FULL_READ_COALESCE_GAP_BYTES),
                      ("1 MiB", MIB)):
        variants.append((name, _coalesced_full_read_plan(
            rows, meta, max_gap_bytes=gap, max_batch_bytes=_FULL_READ_BATCH_MAX_BYTES)))
    variants.append(("single cover", _coalesced_full_read_plan(
        rows, meta, max_gap_bytes=1 << 60, max_batch_bytes=1 << 60)))
    return [result(name, plan, selected_bytes, latency_ms, throughput_mib_s)
            for name, plan in variants]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", nargs="+", type=int, default=[512, 4096])
    parser.add_argument("--interleave", nargs="+", type=int, default=[1, 4, 32],
                        help="physical row stride for the selected node")
    parser.add_argument("--latency-ms", type=float, default=3.4,
                        help="fixed cost of one range read (measured geesefs default: 3.4)")
    parser.add_argument("--throughput-mib-s", type=float, default=100.0)
    parser.add_argument("--seed", type=int, default=20260817)
    args = parser.parse_args()
    if (any(value < 1 for value in args.rows + args.interleave)
            or args.latency_ms < 0 or args.throughput_mib_s <= 0):
        parser.error("rows/interleave/throughput must be positive and latency non-negative")

    print(f"latency={args.latency_ms:g} ms/request  throughput={args.throughput_mib_s:g} MiB/s")
    print("rows  stride  strategy           requests  read MiB  amp     modeled ms")
    for rows in args.rows:
        for stride in args.interleave:
            for item in run_case(rows, stride, args.seed, args.latency_ms, args.throughput_mib_s):
                print(f"{rows:4d}  {stride:6d}  {item.name:17s}  {item.requests:8d}  "
                      f"{item.read_bytes / MIB:8.2f}  {item.amplification:5.2f}x  "
                      f"{item.modeled_ms:10.1f}")


if __name__ == "__main__":
    main()
