#!/usr/bin/env python3
"""What the baseline cache holds, so point 5 of the sweep stops being a memorised number.

WHY. The sweep list says "seven entries in `.baseline_times`, all re-measured HERE". On 2026-09-04
re-timing arm A on `pagerank` and `spectral_clustering` (§193) legitimately wrote two more, and the
count became nine. A reader holding "seven" now has to decide between an alarm and a section they
may not have read. The count is not the invariant; the invariant is that **every entry is in one
regime, carries a full set of per-instance timings, and was written on this box** — and that is
checkable.

The regime key is in the filename (`<task>__<subset>__<key>.json`) and it is the thing that makes
two timings comparable: `looplab_eval.py::eval_regime` refuses a mismatch loudly, and §149 is the
record of a ruler reading 0.0 because the key came out `__lane22r3` instead of `__w22x1r3`.

Usage:
    ruler_check.py [DIR] [--expect-regime KEY] [--min-instances N]
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import statistics
import sys
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent / "algotune" / ".baseline_times"
NAME = re.compile(r"^(?P<task>.+?)__(?P<subset>train|test)__(?P<regime>[^.]+)\.json$")


def entries(directory) -> list[dict]:
    """One record per cache file: task, subset, regime, instance count, spread, mtime."""
    out = []
    for path in sorted(Path(directory).glob("*.json")):
        # A SIDECAR IS NOT AN ENTRY. §297 gave every baseline write a `.provenance.json` recording
        # the conditions it was taken under, and this loop immediately reported both of them as
        # malformed cache files -- a false alarm I created myself, in the tool whose whole job is
        # telling a real problem from a memorised number. Skipped here and READ below, which is
        # what it was written for.
        if path.name.endswith(".provenance.json"):
            continue
        got = NAME.match(path.name)
        row = {"file": path.name, "path": path, "ok_name": bool(got),
               "task": got.group("task") if got else "", "subset": got.group("subset") if got else "",
               "regime": got.group("regime") if got else "", "n": 0, "median": 0.0, "mtime": 0.0}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            times = [float(v) for v in data.values() if isinstance(v, (int, float))]
            row["n"] = len(times)
            row["median"] = statistics.median(times) if times else 0.0
        except (OSError, ValueError):
            pass
        try:
            row["mtime"] = os.path.getmtime(path)
        except OSError:
            pass
        # AND THE CONDITIONS IT WAS TAKEN UNDER, when the write left them. `pagerank`'s 46 % error
        # was undiagnosable for three sweeps precisely because no entry carried this.
        row["provenance"] = {}
        try:
            row["provenance"] = json.loads(
                Path(str(path) + ".provenance.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        out.append(row)
    return out


DRIFT_LOG = Path(__file__).resolve().parent / "algotune" / "ruler_selfcheck_log.jsonl"
DRIFT_TOLERANCE = 0.15      # of the cached value; see below for why it is not tighter


def latest_readings(path=DRIFT_LOG) -> dict:
    """The most recent reference-against-itself reading per task, from the recorded series."""
    out: dict = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            task, med, stamp = row.get("task"), row.get("median"), str(row.get("stamp") or "")
            if not isinstance(med, (int, float)) or not task:
                continue
            if task not in out or stamp > out[task][1]:
                out[task] = (float(med), stamp)
    return out


CPSAT_ROOT = "/var/tmp/looplab-bench/AlgoTune/AlgoTuneTasks"


def uses_cpsat(task: str, root: str | None = None) -> bool:
    """Does this task's reference solve with CP-SAT?

    IT DECIDES WHETHER A RULER IS POSSIBLE AT ALL. Measured 2026-09-06 across all 19 tasks whose
    self-check returned a number, three repeats each: the nine CP-SAT tasks read **1.1375 to 1.8545**
    and the ten others **0.9021 to 1.0670** -- no overlap. CP-SAT is multi-threaded and its search is
    nondeterministic, so the same solver timed twice does not give the same time: within one task the
    repeats swing 1.72/2.09/1.85 and 1.32/1.48/1.19, against 0.96/0.98/0.98 for `edge_expansion`.

    The reference-as-candidate reading exists to say "the cache and the box still agree". On these
    tasks it cannot: there is no stable time for the cache to hold. That is a property of the task,
    not a defect in the cache, and it must be said differently -- a 46 % "drift" on `pagerank` was
    worth a week (§292-§299); the same number here means only that CP-SAT was asked twice.
    """
    import os
    # READ AT CALL TIME, NOT BOUND AT DEF TIME. `root: str = CPSAT_ROOT` evaluates once at import,
    # so a test setting `ruler_check.CPSAT_ROOT` changed nothing and every call read the real
    # checkout: two tests passed for the wrong reason and a mutation dropping the tolerance guard
    # survived. (It survived twice, the second time because I restored the file from a backup taken
    # BEFORE this very fix -- a stale backup quietly undoing it.)
    path = f"{root if root is not None else CPSAT_ROOT}/{task}/{task}.py"
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return False
    return "cp_model" in src or "ortools" in src


def stale_entries(rows, readings, tolerance: float = DRIFT_TOLERANCE) -> list[str]:
    """Cache entries whose own recorded self-check says they no longer time this box.

    A reading of 1.0 means the cached baseline and a fresh timing agree. Measured 2026-09-06 by
    timing all four references into a scratch cache and dividing:

        task             cached/fresh   self-check reading
        edge_expansion       0.868          0.9007
        pde_heat1d           1.016          1.0676
        discrete_log         1.052          1.0830
        pagerank             1.463          1.4317

    The two columns are independent -- one re-times the reference, the other runs it as a candidate
    against the cache -- and they agree to within 0.04 on every task. So the reading IS a measure of
    how wrong the cache is, and `pagerank`'s cache is high by **46 %** while the other three sit
    within 13 %. Being "re-measured HERE", which is what the standing sweep says of every entry, is
    not the same as being still true.

    The tolerance is 15 % and not tighter on purpose: three of the four tasks disagree by 2-13 %
    and none of them is a problem worth an alarm every sweep. It is set to catch the one that is.
    """
    said = []
    for task, (median, stamp) in sorted(readings.items()):
        if not any(r["task"] == task for r in rows if r["ok_name"]):
            continue
        off = abs(median - 1.0)
        if off > tolerance and uses_cpsat(task):
            said.append(f"{task}: self-check reads {median:.4f} ({stamp[:10]}) -- but this task "
                        "solves with CP-SAT, whose runtime depends on how many cores it is given: "
                        "x2.2 between one core and a 22-cpu lane on the same instance (§304). The "
                        "reference asks for no `num_search_workers` and no seed, so it takes "
                        "whatever the process is allowed. NOT a drifting cache, and not mere noise "
                        "-- per-instance repeat spread is only x1.3 and averages away over a "
                        "hundred instances. THE FIX IS THE WORKER COUNT (§307): the same task read at "
                        "ALGOTUNE_EVAL_WORKERS=1 comes home -- max_common_subgraph 1.4820 -> "
                        "1.0141, queens_with_obstacles 1.2667 -> 1.0142, max_clique_cpsat 1.6028 "
                        "-> 0.9974 -- because twenty-two pinned single-core workers all running "
                        "CP-SAT contend differently in the two passes. A one-worker ruler is 22x "
                        "slower to build and keys __lane22r3, so it is a different cache")
            continue
        if off > tolerance:
            said.append(f"{task}: its own self-check reads {median:.4f} ({stamp[:10]}), so the "
                        f"cached baseline is {'high' if median > 1 else 'low'} by "
                        f"{100 * off:.0f} % -- every score on this task divides by it")
    return said


def problems(rows, expect_regime: str | None = None, min_instances: int = 100) -> list[str]:
    """What is WRONG with the cache, as sentences. Empty list = nothing to say.

    The count is deliberately not checked. Entries get added by legitimate work -- §193 added two by
    re-timing arm A -- and a tool that alarms on growth teaches the reader to ignore it.
    """
    said = []
    regimes = collections.Counter(r["regime"] for r in rows if r["ok_name"])
    for row in rows:
        if not row["ok_name"]:
            said.append(f"{row['file']}: not <task>__<subset>__<regime>.json, so its regime is unknown")
            continue
        if row["n"] < min_instances:
            said.append(f"{row['file']}: {row['n']} per-instance timings, fewer than {min_instances}")
    if expect_regime:
        for reg, n in regimes.items():
            if reg != expect_regime:
                said.append(f"{n} entr{'y' if n == 1 else 'ies'} in regime {reg}, not {expect_regime}"
                            f" -- timings from two regimes are not comparable (§149)")
    elif len(regimes) > 1:
        said.append("entries span more than one regime: "
                    + ", ".join(f"{r}x{n}" for r, n in regimes.most_common())
                    + " -- timings from two regimes are not comparable (§149)")
    return said


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("directory", nargs="?", default=str(DEFAULT_DIR))
    ap.add_argument("--expect-regime", default="w22x1r3")
    ap.add_argument("--min-instances", type=int, default=100)
    args = ap.parse_args(argv)

    rows = entries(args.directory)
    if not rows:
        print(f"no baseline entries under {args.directory}", file=sys.stderr)
        return 2
    print(f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'} in {args.directory}")
    print(f'{"task":22s} {"subset":>6s} {"regime":>10s} {"n":>4s} {"median ms":>10s}  written')
    for row in rows:
        when = (datetime.datetime.fromtimestamp(row["mtime"]).strftime("%m-%d %H:%M")
                if row["mtime"] else "?")
        prov = row.get("provenance") or {}
        note = ("" if not prov else
                f'   [{prov.get("eval_workers", "?")} workers, load '
                f'{(prov.get("loadavg") or ["?"])[0]:.1f}]'
                if isinstance((prov.get("loadavg") or [None])[0], (int, float))
                else f'   [{prov.get("eval_workers", "?")} workers]')
        print(f'{row["task"]:22s} {row["subset"]:>6s} {row["regime"]:>10s} {row["n"]:4d} '
              f'{row["median"]:10.2f}  {when}{note}')
    bad = problems(rows, args.expect_regime, args.min_instances)
    bad += stale_entries(rows, latest_readings())
    for line in bad:
        print(f"  PROBLEM: {line}")
    if not bad:
        print(f"  all {len(rows)} in regime {args.expect_regime}, "
              f"{args.min_instances}+ instances each")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
