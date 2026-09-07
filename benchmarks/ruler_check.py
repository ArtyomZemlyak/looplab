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
        out.append(row)
    return out


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
        print(f'{row["task"]:22s} {row["subset"]:>6s} {row["regime"]:>10s} {row["n"]:4d} '
              f'{row["median"]:10.2f}  {when}')
    bad = problems(rows, args.expect_regime, args.min_instances)
    for line in bad:
        print(f"  PROBLEM: {line}")
    if not bad:
        print(f"  all {len(rows)} in regime {args.expect_regime}, "
              f"{args.min_instances}+ instances each")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
