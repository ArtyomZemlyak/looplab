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
import arm_fidelity  # noqa: E402  (score-free by its own test; used only for finished/paused)
import check_money  # noqa: E402  (for the ledger's newest row per arm; reads money, never a score)
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


def orphans(bench: str) -> dict:
    """Bench workers still pinned to a lane whose parent is gone (ppid 1).

    THE HYGIENE CHECK LOOKS FOR STATE Z AND THESE ARE STATE S. Measured 2026-09-06 on a box the
    sweep had just called clean -- zero zombies, no probe running: 23 orphaned `multiprocessing`
    forkservers and resource trackers from runs four to six hours dead, holding 652 MiB and still
    carrying the affinity mask of the lane they were born on. Nothing reported them, because
    "zombie" was the only word the hygiene step knew.

    They cost more than the memory. `busy_cpus_outside_lane` counted them as occupancy, so the
    ruler's own contention field read 22 on an idle box and 22 under a full lane -- the same number
    for both answers. That is fixed at the counting end (§313); this is the other end, the one that
    says out loud that they are there.

    Only what belongs to the bench's own interpreter is counted. An orphan of the host's making is
    not this tool's business, and claiming it would make the number unactionable.
    """
    try:
        hz = os.sysconf("SC_CLK_TCK")
        boot = float(open("/proc/uptime", encoding="utf-8").read().split()[0])
    except (OSError, ValueError):
        return {"count": 0, "rss_mib": 0.0, "oldest_h": 0.0, "cpus": 0}
    mark = f"{bench}/AlgoTune/.venv/bin/python"
    seen: set[int] = set()
    count, rss, oldest = 0, 0.0, 0.0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "replace")
            if mark not in cmd:
                continue
            fields = open(f"/proc/{pid}/stat", encoding="utf-8").read().split(") ")[-1].split()
            if fields[1] != "1":                       # still has a parent: someone owns it
                continue
            aff = os.sched_getaffinity(int(pid))
            if not aff or len(aff) >= (os.cpu_count() or 0):
                continue                               # unpinned: not a lane worker
            rss += int(open(f"/proc/{pid}/statm", encoding="utf-8").read().split()[1]) * 4096 / 2 ** 20
            oldest = max(oldest, boot - int(fields[19]) / hz)
            seen |= aff
            count += 1
        except (OSError, ValueError, IndexError):
            continue
    return {"count": count, "rss_mib": round(rss), "oldest_h": round(oldest / 3600, 2),
            "cpus": len(seen)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", default=DEFAULT_BENCH)
    ap.add_argument("--root", default=None)
    ap.add_argument("--stall", type=float, default=2400.0)
    # THE LIST ASKS FOR TWO CLOCKS AND THIS TOOL WAS SHOWING ONE. Point 4 says to look at the age of
    # `events.jsonl` AND at the age of the last CALL in the ledger. They come apart, and the way
    # they come apart is the diagnosis: a fresh ledger beside a stale log is a probe that is calling
    # and producing nothing -- the retry storm §175 recorded, where three consecutive 504s at
    # exactly 300 s are the nginx ceiling rather than a hang. A stale ledger beside a fresh log is
    # the opposite and much rarer. Reading only the log cannot tell either from an idle probe.
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--now", type=float, default=None)
    # A PROBE THAT LEAVES THE LANES LOOKS EXACTLY LIKE ONE THAT FINISHED. `pulse` lists what is
    # RUNNING, so a probe killed by a stray signal or an OOM simply stops appearing -- and the only
    # reason I have ever caught that is `check_money`, which found FIVE such probes (capA1, capB1,
    # freeA1, freeB1, svcCacheCheck) as money in the meter with no tree on disk. The liveness tool
    # should not need the money tool to notice a missing probe. Name the batch and it will say, for
    # each absentee, whether it ENDED or VANISHED.
    ap.add_argument("--expect", nargs="*", default=[])
    args = ap.parse_args(argv)
    root = args.root or f"{args.bench}/model-probes"
    now = args.now if args.now is not None else time.time()

    ledger = args.ledger or os.path.join(args.bench, "meter", "meter.jsonl")
    newest = check_money.endpoint_health(ledger)["newest"]
    live = lanes.probes(args.bench)
    running = {r["probe"] for r in live if r["probe"]}
    orph = orphans(args.bench)
    if orph["count"]:
        # PRINTED BEFORE THE EARLY RETURN, because an idle box is exactly when nothing else would
        # mention them and exactly when they are pure waste.
        print(f'{orph["count"]} orphaned bench worker(s) pinned across {orph["cpus"]} cpu(s), '
              f'{orph["rss_mib"]} MiB, oldest {orph["oldest_h"]} h -- parent gone (ppid 1), '
              f'state S so the zombie check does not see them')
    if not live and not args.expect:
        print("no bench probe running")
        return 0
    print(f'{"probe":10s} {"lane":12s} {"$":>8s} {"nodes":>5s} {"zeros":>5s} {"errs":>4s} '
          f'{"log age":>8s} {"call age":>9s}  wchan')
    stalled = 0
    for row in sorted(live, key=lambda r: r["probe"] or ""):
        name = row["probe"]
        found = sorted(glob.glob(f"{root}/{name}/runs/*/run/events.jsonl"))
        if not found:
            print(f'{name:10s} {lanes._fmt(row["cpus"]):12s}  no events.jsonl yet')
            continue
        got = pulse(found[0])
        age = now - os.path.getmtime(found[0])
        called = newest.get(name)
        call_age = (now - called[0]) if called else None
        print(f'{name:10s} {lanes._fmt(row["cpus"]):12s} {got["spend"]:8.4f} {got["nodes"]:5d} '
              f'{got["zeros"]:5d} {got["errors"]:4d} {age:7.0f}s '
              f'{(f"{call_age:8.0f}s" if call_age is not None else "       -"):>9s}  '
              f'{wchan(row["pid"])}')
        if called and called[1] != "200":
            print(f'      last call came back {called[1]}, not 200 -- check the endpoint before '
                  "the probe")
        if call_age is not None and age > args.stall / 4 and call_age < age / 4:
            print(f'      CALLING BUT NOT PRODUCING: last call {call_age:.0f}s ago, log last grew '
                  f'{age:.0f}s ago. Three consecutive 504s at exactly 300 s are the nginx ceiling, '
                  "not a hang (§175); check the ledger's statuses before the process")
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
    vanished = 0
    for name in sorted(set(args.expect) - running):
        got = arm_fidelity.probe_calls(root, name)
        found = sorted(glob.glob(f"{root}/{name}/runs/*/run/events.jsonl"))
        spend = pulse(found[0])["spend"] if found else 0.0
        if got["finished"]:
            print(f'{name:10s} {"(off the lanes)":12s} {spend:8.4f}      ended')
        elif got["paused"]:
            print(f'{name:10s} {"(off the lanes)":12s} {spend:8.4f}      PAUSED and owed work -- '
                  "resume it or the batch is short a probe")
            vanished += 1
        else:
            vanished += 1
            print(f'{name:10s} {"(off the lanes)":12s} {spend:8.4f}      VANISHED: no process, no '
                  "ending event, and not at its ceiling -- it did not finish, it stopped")
    return 1 if (stalled or vanished) else 0


if __name__ == "__main__":
    raise SystemExit(main())
