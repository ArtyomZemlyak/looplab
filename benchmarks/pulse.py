#!/usr/bin/env python3
"""Points 1, 2 and 4 of the sweep — liveness, evaluations and stall — WITHOUT reading a score.

WHY THIS FILE EXISTS. `arm_fidelity` was built so the fidelity question could be asked continuously
without an interim look at the outcome, and `test_arm_fidelity_reads_no_scores.py` holds it to that.
Meanwhile the sweep's own point 2 -- "new nodes, zeros and errors" -- was answered every half hour by
a heredoc that printed the node METRICS. §190 forbids reading the arm's outcome before twelve
batches; the tool obeyed and the operator did not. Same shape as everything else this month: not a
breakage, a quiet mismatch between what I thought I was doing and what I was doing.

Point 2 never needed the value. It needs: did new nodes arrive, were any of them ZERO, and if so is
that zero a ruler refusal or a solver failure. The discriminator WAS `eval_seconds` -- a zero in
under five seconds is the harness declining, a zero at 45 s is an evaluation that ran and failed --
and the twelve zeros the corpus held when that was written looked like the second kind.
RE-COUNTED 2026-09-07 with the reasons read instead of the seconds: thirteen zero nodes across
thirteen probes -- `no_valid_speedups` 6, `evaluator_error` 4, `invalid_results` 2,
`compilation_failed` 1 -- spanning 8.3 to 60.9 s. Only TWO are the kind that sentence described
(§324), and ten of the thirteen are the arena's rather than the solver's.

The bridge, though, says why by name: `looplab_eval` classifies every refusal
(`baseline_measured_in_pass`, `regime_not_scorable_for_task`, `evaluator_timeout`, twelve more) and
the reason travels in the node's `stdout_tail`. That is read now, and the stopwatch is the fallback
for a node whose stdout the record did not keep -- because the heuristic is exactly wrong about the
costliest refusal: `evaluator_timeout` returns its zero AFTER the full timeout, so a 900-second
arena failure read as "the evaluation ran and came back invalid".

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
import re
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


# THE BRIDGE SAYS WHY, AND THIS TOOL WAS GUESSING FROM A STOPWATCH. `looplab_eval` classifies every
# refusal by name -- `baseline_measured_in_pass`, `regime_not_scorable_for_task`,
# `evaluator_timeout`, twelve more -- and `compare_arms` keeps a partition of that vocabulary into
# the solver's fault and the arena's. None of it reached here: the reason travels in the node's
# `stdout_tail` (the bridge's own JSON line, kept to 4,000 chars by `evaluate.py`), and this file
# inferred "refusal" from `eval_seconds < 5`.
#
# That heuristic is right for the refusals that cost no time and WRONG for the one that costs the
# most: `evaluator_timeout` returns a zero after the full timeout, so the tool an operator watches
# live called an arena failure a solver's zero -- the same misclassification `compare_arms`'
# NOT_SOLVERS_FAULT list exists to prevent, arriving through the other door.
_REASON = re.compile(r'"no_speedup"\s*:\s*\{[^{}]*?"reason"\s*:\s*"([a-z_]+)"')


def refusal_reason(data: dict) -> str | None:
    """The bridge's own name for why this node has no speedup, or None if it did not say."""
    for field in ("stdout_tail", "stderr_tail", "error_evidence"):
        text = data.get(field)
        if not isinstance(text, str):
            continue
        got = _REASON.search(text)
        if got:
            return got.group(1)
    return None


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
                why = refusal_reason(data)
                bad.append({"node_id": data.get("node_id"), "eval_seconds": secs,
                            "violations": data.get("violations"), "reason": why,
                            # The stopwatch stays as the FALLBACK, for a node whose stdout the
                            # record did not keep; a reason that was said outranks it either way.
                            "refusal": bool(why) if why else
                            (isinstance(secs, (int, float)) and secs < REFUSAL_SECONDS)})
        elif kind in ("error", "developer_crash", "build_interrupted"):
            errors += 1
    return {"spend": spend, "nodes": nodes, "zeros": zeros, "errors": errors, "bad": bad}


def wchan(pid) -> str:
    try:
        return open(f"/proc/{pid}/wchan", encoding="utf-8").read().strip() or "-"
    except OSError:
        return "gone"


def timer_processes(table) -> dict:
    """The snapshot DAEMONS in a process table, told apart from the forks of a cycle in flight.

    Measured 2026-09-08: the sweep's own scan -- "every process whose cmdline holds
    `snapshot_timer.sh` and `_loop`" -- read **five** where there is one. Four were gone a second
    later: bash forks for a pipeline or a command substitution inherit the parent's cmdline, so a
    timer that is mid-cycle looks like five timers to a matcher that only reads `/proc/<pid>/cmdline`.
    A duplicate timer is a real hazard (two snapshots at once, which §313 drove: the loser exits 3
    having written nothing), and that is exactly why the count may not cry wolf every time a cycle
    happens to be running when the sweep looks.

    The discriminator is the PARENT. The daemon is started with `nohup ... &` and reparented to
    init; a fork of its cycle has the daemon as its parent. Neither is a matter of timing, and both
    are in the same table this reads once.

    `table` is a list of `{"pid", "ppid", "cmdline"}` so the rule can be tested against a fabricated
    process tree -- `/proc` cannot be arranged to hold two timers on demand.
    """
    daemons, forks = [], []
    for row in table:
        cmd = row.get("cmdline") or ""
        if "snapshot_timer.sh" not in cmd or "_loop" not in cmd:
            continue
        (daemons if str(row.get("ppid")) == "1" else forks).append(str(row.get("pid")))
    inside = {p for p in forks}
    return {"daemons": daemons, "forks": sorted(inside),
            "duplicate": len(daemons) > 1}


def process_table(root="/proc") -> list:
    """`/proc` as the list `timer_processes` reads. One pass, and a process that exits under us is
    skipped rather than raising -- the scan is a census, not a transaction."""
    out = []
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for pid in names:
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"{root}/{pid}/cmdline", "rb").read().decode("utf-8", "replace")
            status = open(f"{root}/{pid}/status", encoding="utf-8").read()
            ppid = next(l.split()[1] for l in status.splitlines() if l.startswith("PPid:"))
        except (OSError, StopIteration, IndexError):
            continue
        out.append({"pid": pid, "ppid": ppid, "cmdline": cmd.replace("\0", " ")})
    return out


BENCH_SCRIPTS = ("run_probe.sh", "campaign.sh", "ruler_selfcheck.py", "looplab_eval.py",
                 "snapshot.sh")


def stray_bench_processes(table) -> list:
    """Bench work whose parent is gone AND which is nobody's parent -- a leftover, not a daemon.

    §332. The wide orphan scan the sweep ran by hand counted two on a healthy box, and both were
    MY OWN launcher shells: `bash -c source …/shell-snapshots/… && nohup run_probe.sh …`, reparented
    to init the moment the tool call returned, each holding the live probe as a child. Matching the
    script name ANYWHERE in a command line is the `pkill -f` mistake the sweep list warns about, one
    layer up: the launcher's argv mentions the script it launched.

    Two discriminators, and both are needed:

      * THE PROCESS ITSELF, not a mention. `argv[0]` or `argv[1]` is the script; a wrapper that
        passes it to `bash -c` has it further along.
      * NO CHILDREN. A deliberately detached daemon (`nohup … &`) is reparented to init too -- that
        is what `nohup` is for -- and the thing that tells it from a leftover is whether anything is
        still running under it. The 23 forkservers of §313 had none; the probe launcher has one.

    `table` is `{"pid", "ppid", "cmdline"}` rows so the rule can be tested against a fabricated
    tree; `/proc` cannot be asked to hold an abandoned campaign on demand.
    """
    children: dict = {}
    for row in table:
        children.setdefault(str(row.get("ppid")), []).append(str(row.get("pid")))
    out = []
    for row in table:
        if str(row.get("ppid")) != "1":
            continue
        argv = (row.get("cmdline") or "").split()
        head = argv[:2]
        if not any(any(name in part for name in BENCH_SCRIPTS) for part in head):
            continue
        if children.get(str(row.get("pid"))):
            continue                       # something is running under it: detached, not abandoned
        out.append(str(row.get("pid")))
    return sorted(out)


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
    health = check_money.endpoint_health(ledger)
    newest = health["newest"]
    live = lanes.probes(args.bench)
    running = {r["probe"] for r in live if r["probe"]}
    # THE TIMER, ONCE, WITH ITS FORKS NAMED AS FORKS. Point 1 of the sweep asks whether the
    # snapshot timer is alive; a matcher on the cmdline alone answers "five" whenever a cycle is
    # running, and a DUPLICATE timer is a real thing to catch (two snapshots at once; §313 drove
    # the loser exiting 3 with nothing written).
    timers = timer_processes(process_table())
    if timers["duplicate"]:
        print(f'DUPLICATE snapshot timer: daemons {", ".join(timers["daemons"])} -- two timers mean '
              "two snapshots racing for one lock; kill all but one BY PID")
    elif timers["daemons"]:
        cycle = f', {len(timers["forks"])} fork(s) of a cycle in flight' if timers["forks"] else ""
        print(f'snapshot timer {timers["daemons"][0]} alive{cycle}')
    else:
        print("NO snapshot timer running")

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
            # THE STREAK, NOT THE LAST SAMPLE. §333: this line said "last call came back 503" while
            # the state was 60 consecutive 503s over 22 minutes -- the provider's own pool down
            # (`litellm.ServiceUnavailableError: No available workers`). The list's rule for the
            # other wall is a rule about a streak too ("three consecutive 504s at exactly 300 s =
            # the nginx ceiling, not a hang"), and one sample cannot express either.
            run = (health.get("streak") or {}).get(name)
            if run and str(run["status"]) == str(called[1]):
                mins = (now - run["since"]) / 60.0
                print(f'      {run["count"]} consecutive {run["status"]}s over {mins:.0f} min '
                      "-- the endpoint is refusing, not this probe stalling")
            else:
                print(f'      last call came back {called[1]}, not 200 -- check the endpoint before '
                      "the probe")
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
