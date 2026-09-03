#!/usr/bin/env python3
"""What a "don't start a node you cannot finish" gate would have cost, at every threshold.

WHY. 7.7 % of corpus spend lands after the last node a run ever evaluates, and the money cue that
was supposed to prevent it reaches 99 % of the deciding generations and prevents nothing (§148,
§152). The obvious repair is a hard gate: refuse to open a node when `limit - spent` is below the
cost of a node cycle. The obvious threshold is the p75 cycle, $0.4481.

THAT THRESHOLD IS WRONG BY A FACTOR OF FOUR AND A HALF. Measured over the 76-run corpus on
2026-09-03, a gate at p75 would have stopped 74 cycles that produced nothing ($4.4743 redirected)
and **54 cycles that produced a real node** -- including the 277.23 that is the best
`edge_expansion` node in the corpus. At $0.10 the same gate stops 61 empty cycles, redirects
$1.5354, and the single real node it costs scored **0**.

    gate    empty cut   $ redirected   real nodes lost   best lost
    0.05           49         0.5816                 0        0.00
    0.10           61         1.5354                 1        0.00
    0.15           67         2.2990                 3      211.40
    0.25           70         2.9389                23      277.23
    0.4481         74         4.4743                54      277.23

The curve is the deliverable, not the number: the threshold has to be re-derived as the corpus
grows, and a number in a comment goes stale silently. This script recomputes it.

Usage:
    budget_gate_curve.py [PROBE_ROOT] [--exclude NAME ...] [--gates 0.05 0.10 …]

EXCLUDE LIVE PROBES. A run still in flight has spent everything "after its last evaluated node"
because it has no last node yet -- §148.1 was corrected for exactly that mistake, an hour after
making it.
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics

DEFAULT_ROOT = "/var/tmp/looplab-bench/model-probes"
DEFAULT_GATES = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.45)


def load(root: str, exclude=()) -> list[dict]:
    """One record per run: its budget, its priced calls, and when each node was evaluated."""
    out = []
    for path in sorted(glob.glob(f"{root}/*/runs/*/run/events.jsonl")):
        name = path.split("/model-probes/")[-1].split("/")[0] if "/model-probes/" in path \
            else path.split("/")[-5]
        if name in exclude:
            continue
        try:
            rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
        except (OSError, ValueError):
            continue
        if not rows:
            continue
        cost = [(r["ts"], float((r.get("data") or {}).get("cost") or 0.0))
                for r in rows if r.get("type") == "llm_usage"]
        total = sum(c for _, c in cost)
        # The budget is not in the event stream; every probe in this corpus is a $1 run and the
        # few larger ones round cleanly. A wrong budget only shifts that run's own curve.
        budget = 1.00 if total < 1.6 else round(total)
        evals = [(r["ts"], (r.get("data") or {}).get("metric"))
                 for r in rows if r.get("type") == "node_evaluated"]
        out.append({"name": name, "budget": budget, "cost": cost, "evals": evals,
                    "marks": [rows[0]["ts"]] + [t for t, _ in evals]})
    return out


def cycles(runs) -> list[float]:
    """Spend between one node evaluation and the next -- the price of a completed node."""
    got = []
    for run in runs:
        marks = run["marks"]
        for i in range(1, len(marks)):
            got.append(sum(c for ts, c in run["cost"] if marks[i - 1] < ts <= marks[i]))
    return got


def at_gate(runs, gate: float) -> dict:
    """What refusing to open a node below `gate` would have cut, and what it would have cost."""
    empty_cut = real_lost = 0
    redirected = 0.0
    best_lost = 0.0
    for run in runs:
        marks, evals = run["marks"], run["evals"]
        for i in range(1, len(marks) + 1):
            start = marks[i - 1]
            remaining = run["budget"] - sum(c for ts, c in run["cost"] if ts <= start)
            # A cent of float error decides a real node here: 1.00 - 0.92 is 0.07999999999999996,
            # which is "below" a $0.08 gate by 4e-17 and costs the run its 277.23. Compare with a
            # tolerance far below any real money and far above double-precision noise.
            if remaining >= gate - 1e-9:
                continue
            if i <= len(evals):
                real_lost += 1
                metric = evals[i - 1][1]
                if isinstance(metric, (int, float)):
                    best_lost = max(best_lost, float(metric))
            else:
                # A run that stopped cleanly still has a trailing "cycle" after its last node, and
                # if it spent nothing in it there was nothing to refuse. Counting those inflates
                # the gate's apparent yield with runs it would not have touched.
                spent_after = sum(c for ts, c in run["cost"] if ts > start)
                if spent_after <= 0:
                    continue
                empty_cut += 1
                redirected += spent_after
    return {"gate": gate, "empty_cut": empty_cut, "redirected": redirected,
            "real_lost": real_lost, "best_lost": best_lost}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default=DEFAULT_ROOT)
    ap.add_argument("--exclude", nargs="*", default=[],
                    help="probe names to leave out -- pass every run still in flight")
    ap.add_argument("--gates", nargs="*", type=float, default=list(DEFAULT_GATES))
    args = ap.parse_args(argv)

    runs = load(args.root, set(args.exclude))
    if not runs:
        print(f"no runs under {args.root}")
        return 2
    cyc = cycles(runs)
    if cyc:
        srt = sorted(cyc)
        print(f"completed node cycle: n={len(cyc)} median ${statistics.median(cyc):.4f} "
              f"p75 ${srt[int(0.75 * len(srt))]:.4f} p90 ${srt[int(0.90 * len(srt))]:.4f}")
    print(f"{len(runs)} run(s)" + (f", excluding {', '.join(sorted(args.exclude))}"
                                   if args.exclude else ""))
    print(f'{"gate":>8s} {"empty cut":>10s} {"$ redirected":>13s} {"real nodes lost":>16s} '
          f'{"best lost":>10s}')
    for gate in args.gates:
        row = at_gate(runs, gate)
        print(f"{row['gate']:8.4f} {row['empty_cut']:10d} {row['redirected']:13.4f} "
              f"{row['real_lost']:16d} {row['best_lost']:10.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
