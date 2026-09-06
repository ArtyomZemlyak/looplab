#!/usr/bin/env python3
"""Which campaign tasks can be scored, under which regime — computed, not remembered.

WHY. §309 ended with a sorted list of twenty tasks in a documentation table. A campaign would have
to be told that table by hand, and a hand-typed list is the thing this bench keeps discovering it
cannot trust: §291 found six comparison figures quoted from probes that no longer exist, §300 found
fifteen tasks with no data, §303 found nine rulers that do not read unity. So the sorting is derived
from artefacts on disk every time it is asked.

THE RULE, and every number in it was measured (§303-§309):

  * a task with no baseline entry cannot be scored at all;
  * a task whose recorded self-check reads within `TOLERANCE` of 1.0 rules AS IT IS;
  * a CP-SAT task that misses only because of worker contention rules AT ONE WORKER -- measured for
    `max_common_subgraph` 1.4820 -> 1.0141, `queens_with_obstacles` 1.2667 -> 1.0142,
    `max_clique_cpsat` 1.6028 -> 0.9974, `rectanglepacking` -> 0.9871;
  * a CP-SAT task whose per-instance times are heavy-tailed rules under NEITHER regime, because the
    speedup is a sum and the tail is where a randomized solver's variance does not cancel. Every
    task above p90/p10 = 30 missed at one worker (51.9 -> 1.145, 45.2 -> 1.247, 32.2 -> 1.236) and
    every task below 15 came home. Nothing has ever landed between.

`discrete_log` is why the tail alone decides nothing: p90/p10 = 276, the heaviest on the box, and it
reads 0.997 because it is deterministic.

Usage:
    task_inventory.py [--tasks-from campaign.sh] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ruler_check  # noqa: E402

TOLERANCE = 0.10        # of unity; §309's ten plain tasks all sit inside 7 %
CAMPAIGN = Path(__file__).resolve().parent / "algotune" / "campaign.sh"


def campaign_tasks(path=CAMPAIGN) -> list:
    """The task list the campaign actually runs, read from its own default."""
    try:
        src = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    got = re.search(r'TASKS="\$\{TASKS:-(.*?)\}"', src, re.S)
    if not got:
        return []
    return [t for t in got.group(1).replace("\\\n", " ").split() if t != "\\"]


def classify(task: str, rows, readings) -> dict:
    """One task's verdict, from the cache, its own recorded reading, and its source."""
    entry = next((r for r in rows if r["task"] == task and r.get("subset") == "test"), None)
    reading = (readings.get(task) or (None, ""))[0]
    tail = ruler_check.tail_ratio(entry) if entry else None
    cpsat = ruler_check.uses_cpsat(task)
    if entry is None:
        return {"task": task, "verdict": "no baseline", "reading": reading, "tail": tail,
                "cpsat": cpsat, "why": "no test entry in the cache"}
    if reading is None:
        return {"task": task, "verdict": "unread", "reading": None, "tail": tail, "cpsat": cpsat,
                "why": "no self-check reading recorded; run ruler_selfcheck --record"}
    if abs(reading - 1.0) <= TOLERANCE:
        return {"task": task, "verdict": "rules as is", "reading": reading, "tail": tail,
                "cpsat": cpsat, "why": f"self-check {reading:.4f}"}
    if cpsat and tail is not None and tail > ruler_check.TAIL_RATIO_LIMIT:
        return {"task": task, "verdict": "unrulable", "reading": reading, "tail": tail,
                "cpsat": True,
                "why": f"CP-SAT with a heavy tail (p90/p10 = {tail:.0f}); the speedup is a sum and "
                       "the tail is where the variance does not cancel"}
    if cpsat:
        return {"task": task, "verdict": "rules at one worker", "reading": reading, "tail": tail,
                "cpsat": True,
                "why": f"CP-SAT, light tail (p90/p10 = {tail:.0f}); contention at 22 workers, not "
                       "the solver"}
    return {"task": task, "verdict": "off by more than the tolerance", "reading": reading,
            "tail": tail, "cpsat": False, "why": f"self-check {reading:.4f}, not CP-SAT"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tasks-from", default=str(CAMPAIGN))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    tasks = campaign_tasks(args.tasks_from)
    if not tasks:
        print(f"no task list in {args.tasks_from}", file=sys.stderr)
        return 2
    rows = ruler_check.entries(ruler_check.DEFAULT_DIR)
    readings = ruler_check.latest_readings()
    out = [classify(t, rows, readings) for t in tasks]
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    order = ["rules as is", "rules at one worker", "unrulable",
             "off by more than the tolerance", "unread", "no baseline"]
    print(f"{len(tasks)} campaign tasks")
    for verdict in order:
        group = [r for r in out if r["verdict"] == verdict]
        if not group:
            continue
        print(f"\n  {verdict.upper()}  ({len(group)})")
        for r in sorted(group, key=lambda x: -(x["reading"] or 0)):
            rd = f"{r['reading']:.4f}" if r["reading"] is not None else "  -   "
            tl = f"{r['tail']:6.1f}" if r["tail"] is not None else "     -"
            print(f"    {r['task']:30s} reading {rd}  tail {tl}  {r['why']}")
    scorable = sum(1 for r in out if r["verdict"] in ("rules as is", "rules at one worker"))
    print(f"\n  {scorable} of {len(tasks)} can be scored against a ruler that reads unity")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
