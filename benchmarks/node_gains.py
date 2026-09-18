#!/usr/bin/env python3
"""Δk — what the k-th evaluated node added to a run's running best, over a tree of probes.

    python benchmarks/node_gains.py <probe root> [<probe root> ...]

WHY THIS IS A COMMITTED INSTRUMENT AND NOT AN ANALYSIS WRITTEN WHEN THE DATA ARRIVE. docs/56 §432
buys three $2.00 probes to measure Δ5, and states three readings BEFORE the money. An analysis
chosen afterwards can honour those readings word for word and still pick, say, the mean over the
median, or "evaluated nodes" over "feasible nodes", and land wherever it likes: §433's own table
moves from "decays gently" to "stops at the third node" on exactly that choice. So the arithmetic
is fixed here, in git, with its choices written down, and the reading is a command.

THE FOUR CHOICES, each of which could go the other way:

* **The running best, not the node's own score.** Δk = best(k) − best(k−1) over the first k
  evaluated nodes. It is never negative, and a k with Δk = 0 means "the k-th node did not beat what
  was already there" — which is the question phase 1 asks, not "was it any good".
* **Median beside mean, and the share that improved beside both.** §433 exists because the mean
  alone reads as a gently decaying curve while 82 % of champions stand at node 1 or 2. One number
  here would be a choice; three are a description.
* **Terminal order, not node id.** Nodes are built concurrently and ids are reserved before the
  work; the k-th node a run LEARNED about is the k-th terminal in the log.
* **Salvaged nodes are excluded**, matching `feasible_nodes`: a salvaged metric was not measured on
  the search's own protocol. The corpus has none, so this changes nothing today and says so.

It reads `node_evaluated` out of the event log directly rather than folding: the fold is
authoritative for `RunState`, this needs the ordered terminal metrics, and folding 161 runs costs
more I/O than a bench box has to spare while it is running probes.
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from pathlib import Path


def run_series(path: Path) -> tuple[list[float], int]:
    """`(metrics of the evaluated nodes in terminal order, salvaged nodes skipped)`."""
    out: list[float] = []
    salvaged = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"node_evaluated"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "node_evaluated":
                continue
            data = row.get("data") or {}
            if data.get("metric_salvaged"):
                salvaged += 1
                continue
            v = data.get("metric")
            if isinstance(v, (int, float)):
                out.append(float(v))
    return out, salvaged


def collect(roots: list[Path]) -> tuple[dict[str, list[list[float]]], int, int]:
    """`{task: [series, ...]}`, runs read, salvaged nodes skipped."""
    by_task: dict[str, list[list[float]]] = collections.defaultdict(list)
    runs = salvaged = 0
    for root in roots:
        for ev in sorted(root.glob("*/runs/*/run/events.jsonl")):
            runs += 1
            series, skipped = run_series(ev)
            salvaged += skipped
            if series:
                by_task[ev.parts[-3]].append(series)
    return by_task, runs, salvaged


def report(by_task, runs: int, salvaged: int, out=sys.stdout) -> None:
    print(f"прогонов прочитано: {runs}; с оценёнными узлами: "
          f"{sum(len(v) for v in by_task.values())}; спасённых узлов пропущено: {salvaged}",
          file=out)
    for task, series in sorted(by_task.items(), key=lambda kv: -len(kv[1])):
        lens = [len(s) for s in series]
        print(f"\n=== {task}: {len(series)} прогон(ов), узлов min {min(lens)} "
              f"медиана {statistics.median(lens):.0f} max {max(lens)} ===", file=out)
        print(f"{'k':>2} {'дошло':>6} {'улучшил':>8} {'доля':>6} {'Δk медиана':>11} "
              f"{'Δk среднее':>11} {'лучшее после k':>15}", file=out)
        for k in range(1, max(lens) + 1):
            reached = [s for s in series if len(s) >= k]
            best_k = [max(s[:k]) for s in reached]
            if k == 1:
                print(f"{k:>2} {len(reached):>6} {'—':>8} {'—':>6} {'—':>11} {'—':>11} "
                      f"{statistics.median(best_k):>15.4g}", file=out)
                continue
            gains = [max(s[:k]) - max(s[:k - 1]) for s in reached]
            improved = sum(1 for g in gains if g > 1e-9)
            print(f"{k:>2} {len(reached):>6} {improved:>8} {improved/len(gains):>5.0%} "
                  f"{statistics.median(gains):>11.4g} {statistics.fmean(gains):>11.4g} "
                  f"{statistics.median(best_k):>15.4g}", file=out)
        where = collections.Counter(s.index(max(s)) + 1 for s in series)
        line = ", ".join(f"узел {k}: {n} ({n/len(series):.0%})" for k, n in sorted(where.items()))
        print(f"чемпион стоит на — {line}", file=out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="+", type=Path,
                    help="probe roots; each is scanned for */runs/*/run/events.jsonl")
    args = ap.parse_args(argv)
    missing = [r for r in args.roots if not r.is_dir()]
    if missing:
        print("REFUSING: not a directory: " + ", ".join(str(m) for m in missing), file=sys.stderr)
        return 2
    by_task, runs, salvaged = collect(args.roots)
    if not by_task:
        # NOT AN EMPTY TABLE. A root with no runs in it is a mistyped path far more often than it is
        # a finding, and a table of zeros reads as a measurement.
        print(f"REFUSING: {runs} run(s) under those roots and not one evaluated node. "
              "A path with no data is not a result.", file=sys.stderr)
        return 2
    report(by_task, runs, salvaged)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
