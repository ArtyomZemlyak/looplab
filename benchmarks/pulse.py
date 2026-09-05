#!/usr/bin/env python3
"""Points 1, 2 and 4 of the sweep — liveness, evaluations and stall — WITHOUT reading a score.

WHY THIS FILE EXISTS. `arm_fidelity` was built so the fidelity question could be asked continuously
without an interim look at the outcome, and `test_arm_fidelity_reads_no_scores.py` holds it to that.
Meanwhile the sweep's own point 2 -- "new nodes, zeros and errors" -- was answered every half hour by
a heredoc that printed the node METRICS. §190 forbids reading the arm's outcome before twelve
batches; the tool obeyed and the operator did not. Same shape as everything else this month: not a
breakage, a quiet mismatch between what I thought I was doing and what I was doing.

Point 2 never needed the value. It needs: did new nodes arrive, were any of them ZERO, and if so is
that zero a ruler refusal or a solver failure. The discriminator is `eval_seconds` -- a zero in under
five seconds is the harness declining, a zero at 45 s is an evaluation that ran and failed -- and all
12 zeros in the corpus are the second kind, at 41-47 s, carrying `violations`.

So this prints counts, seconds, violations, spend, log age and `wchan`, and never a metric. The
test that matters is behavioural: a probe whose node scores 123456.789 must not have that number
appear anywhere in the output.

Usage:
    pulse.py [--root DIR] [--bench DIR] [--stall 2400]
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events_read  # noqa: E402
import lanes  # noqa: E402

DEFAULT_BENCH = "/var/tmp/looplab-bench"
REFUSAL_SECONDS = 5.0     # a zero faster than this is the ruler declining, not the solver failing


def pulse(events_path: str) -> dict:
    """Counts and diagnostics for one probe's event log. The metric is CLASSIFIED, never carried."""
    spend = 0.0
    nodes = zeros = errors = 0
    bad = []
    for event in events_read.iter_events(events_path):
        kind = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if kind == "llm_usage":
            try:
                spend += max(0.0, float(data.get("cost") or 0.0))
            except (TypeError, ValueError):
                pass
        elif kind == "node_evaluated":
            scored = data.get("metric")
            if isinstance(scored, (int, float)) and scored > 0:
                nodes += 1
            else:
                zeros += 1
                secs = data.get("eval_seconds")
                bad.append({"node_id": data.get("node_id"), "eval_seconds": secs,
                            "violations": data.get("violations"),
                            "refusal": isinstance(secs, (int, float)) and secs < REFUSAL_SECONDS})
        elif kind in ("error", "developer_crash", "build_interrupted"):
            errors += 1
    return {"spend": spend, "nodes": nodes, "zeros": zeros, "errors": errors, "bad": bad}


def wchan(pid) -> str:
    try:
        return open(f"/proc/{pid}/wchan", encoding="utf-8").read().strip() or "-"
    except OSError:
        return "gone"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", default=DEFAULT_BENCH)
    ap.add_argument("--root", default=None)
    ap.add_argument("--stall", type=float, default=2400.0)
    ap.add_argument("--now", type=float, default=None)
    args = ap.parse_args(argv)
    root = args.root or f"{args.bench}/model-probes"
    now = args.now if args.now is not None else time.time()

    live = lanes.probes(args.bench)
    if not live:
        print("no bench probe running")
        return 0
    print(f'{"probe":10s} {"lane":12s} {"$":>8s} {"nodes":>5s} {"zeros":>5s} {"errs":>4s} '
          f'{"log age":>8s}  wchan')
    stalled = 0
    for row in sorted(live, key=lambda r: r["probe"] or ""):
        name = row["probe"]
        found = sorted(glob.glob(f"{root}/{name}/runs/*/run/events.jsonl"))
        if not found:
            print(f'{name:10s} {lanes._fmt(row["cpus"]):12s}  no events.jsonl yet')
            continue
        got = pulse(found[0])
        age = now - os.path.getmtime(found[0])
        print(f'{name:10s} {lanes._fmt(row["cpus"]):12s} {got["spend"]:8.4f} {got["nodes"]:5d} '
              f'{got["zeros"]:5d} {got["errors"]:4d} {age:7.0f}s  {wchan(row["pid"])}')
        for z in got["bad"]:
            # THE ZERO'S OWN SECONDS ARE THE DIAGNOSIS. A zero under five seconds means the harness
            # declined to measure -- a regime mismatch, an unloadable solver -- and blaming the
            # model for it sends the next hour in the wrong direction. All 12 corpus zeros are the
            # other kind: 41-47 s of real evaluation that came back invalid.
            what = ("RULER REFUSAL -- the harness declined, the solver was never the question"
                    if z["refusal"] else "the evaluation ran and came back invalid")
            print(f'      zero at node {z["node_id"]}: eval_seconds={z["eval_seconds"]}, '
                  f'violations={z["violations"]} -- {what}')
        if age > args.stall:
            stalled += 1
            print(f'      STALLED: {age:.0f}s since the log last grew, past the {args.stall:.0f}s '
                  "ceiling -- check the ledger's newest row and wchan before concluding it is hung")
    return 1 if stalled else 0


if __name__ == "__main__":
    raise SystemExit(main())
