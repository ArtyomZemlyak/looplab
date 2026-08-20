#!/usr/bin/env python3
"""Live status of a running campaign: what has finished, what SCORED, and what merely finished.

`compare_arms.py` is the end-of-campaign report; this is the one to run while it is going.

THE DISTINCTION THIS EXISTS FOR
-------------------------------
A finished task-arm is not a scored one. AlgoTuner writes the WORDS `N/A` and `Error` into
`agent_summary.json` when it has no number, and an earlier version of this script counted those as
scores -- reporting "3/3 scored" for a set that contained one import failure.

`N/A` is also not a low score, and must never be read as one. Measured 2026-08-20:
`set_cover_conflicts` ended in 78 SECONDS with `reason: import_error` -- the agent wrote
`from scipy.optimize import integrality` (a parameter of `milp`, not an importable name), every
evaluation died instantly on that import so no 100-instance pass ever ran, and it burned the whole
$0.02 on model round-trips. That is "drowned in its own typo", not "solved it badly".

It also shows what a SPEND budget buys, which is worth stating in any methods note: the same $0.02
bought 178 minutes of work on a task whose code ran, and 78 seconds on one whose code did not.
Both arms pay that identically, so parity holds -- but "same budget" is not "same amount of work".

    python campaign_status.py --algotune-root /srv/AlgoTune [--out /root/benchmarks/campaign]
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=Path("/root/benchmarks/campaign"))
    ap.add_argument("--arm", choices=("A", "B"), default="A")
    ap.add_argument("--model-fragment", default="v4-flash")
    args = ap.parse_args()

    reports = args.algotune_root.resolve() / "reports"
    summary = json.loads((reports / "agent_summary.json").read_text(encoding="utf-8"))
    failures_file = reports / "agent_failures.json"
    failures = (json.loads(failures_file.read_text(encoding="utf-8"))
                if failures_file.exists() else {})

    done = sorted(p.name[2:-5] for p in args.out.glob(f"{args.arm}-*.done"))
    scored, blank = [], []
    for task in done:
        raw = next((v.get("final_speedup") for k, v in (summary.get(task) or {}).items()
                    if args.model_fragment.lower() in str(k).lower()), None)
        marker = (args.out / f"{args.arm}-{task}.done").read_text(encoding="utf-8").split()
        wall = int(marker[0].split("=")[1]) if marker else 0
        value = _float(raw)
        if value is None:
            reason = next((v.get("reason") for v in (failures.get(task) or {}).values()), "?")
            blank.append((task, raw, wall, reason))
        else:
            scored.append((task, value, wall))

    print(f"arm {args.arm}: {len(done)} finished -> {len(scored)} SCORED, {len(blank)} no number\n")
    for task, value, wall in sorted(scored, key=lambda r: -r[1]):
        print(f"  {task:<32}{value:>10.4f}{wall // 60:>6} min")
    for task, raw, wall, reason in blank:
        print(f"  {task:<32}{str(raw):>10}{wall // 60:>6} min   ({reason})")

    if scored:
        values = sorted(v for _, v, _ in scored)
        walls = [w for _, _, w in scored]
        print(f"\nscored: median {statistics.median(values):.4f}  "
              f"range {values[0]:.4f}-{values[-1]:.4f}")
        print(f"wall per scored task: median {statistics.median(walls) // 60} min")
    if blank:
        print(f"\n{len(blank)} task(s) produced no number. That is NOT a zero and NOT a low score "
              f"-- see this file's docstring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
