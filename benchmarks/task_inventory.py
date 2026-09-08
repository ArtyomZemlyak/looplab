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


REFERENCE_INVALID = {
    # task -> what its own checker says. Measured, not assumed; §311 for the one entry here.
    "spectral_clustering":
        "its own is_solution rejects 7 of its 100 reference solutions "
        "(instances 1, 26, 55, 61, 62, 64, 84) as `argmax over a k-column subset (suspicious)`; "
        "100 % validity is required, so nothing on this box can score it (§311)",
}


def _reference_invalid(task: str):
    return REFERENCE_INVALID.get(task)


def _tail(tail) -> str:
    """`{tail:.0f}` on a None crashed the verdict for any entry whose cache row has under 20
    per-instance times -- a formatting hole in a branch that only fires for CP-SAT tasks, which is
    why it survived: every CP-SAT entry on this box has 100."""
    return "unknown" if tail is None else f"{tail:.0f}"


CAMPAIGN_REGIME = "w22x1r3"     # what a campaign's candidates actually run under
SERIAL_REGIME = "lane22r3"      # ALGOTUNE_EVAL_WORKERS=1 on a full lane


def classify(task: str, rows, readings, serial=None) -> dict:
    """One task's verdict, from the cache, its own recorded reading, and its source.

    `serial` holds readings taken with one evaluation worker. A CP-SAT task is called RULABLE AT ONE
    WORKER only when such a reading exists and reads unity -- §314 measured max_clique_cpsat at
    0.9922 there against 1.5291 at twenty-two, so the verdict is real, but it was being ASSERTED
    from "CP-SAT and a light tail" with no one-worker reading behind it. A task with no serial
    reading is now reported as needing one rather than passed.
    """
    # THE CAMPAIGN'S REGIME, EXPLICITLY. Both keys now live in the cache for the CP-SAT tasks, and
    # the moment they did, this line -- first match wins -- started reading whichever file sorted
    # first, so `max_common_subgraph`'s tail fell from 15.0 to 1.4 without anything being measured.
    # The same trap the section above is about, one level up, inside its own instrument.
    wide = [r for r in rows if r["task"] == task and r.get("subset") == "test"
            and r.get("regime") == CAMPAIGN_REGIME]
    entry = wide[0] if wide else next(
        (r for r in rows if r["task"] == task and r.get("subset") == "test"), None)
    reading = (readings.get(task) or (None, ""))[0]
    tail = ruler_check.tail_ratio(entry) if entry else None
    cpsat = ruler_check.uses_cpsat(task)
    if entry is None:
        return {"task": task, "verdict": "no baseline", "reading": reading, "tail": tail,
                "cpsat": cpsat, "why": "no test entry in the cache"}
    if reading is None:
        # A TASK WHOSE OWN REFERENCE FAILS ITS OWN CHECKER IS NOT "NOT YET READ". §311 measured
        # `spectral_clustering`: 7 of its 100 reference solutions are rejected by its own
        # `is_solution`, which flags them as `argmax over a k-column subset (suspicious)` -- an
        # anti-reward-hack heuristic firing on the reference. 100 % validity is required, so no
        # solver can score it, and telling an operator to "run ruler_selfcheck --record" sends them
        # to repeat a refusal.
        why = _reference_invalid(task)
        if why:
            return {"task": task, "verdict": "unscorable reference", "reading": None, "tail": tail,
                    "cpsat": cpsat, "why": why}
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
        # AND THE OLD REASON WAS WRONG, measured 2026-09-06 (§314): re-timing the baseline on an
        # IDLE box moved it 2 % (max_clique_cpsat) and 11 % (min_dominating_set), and reading it
        # again on that idle box still gave 1.5291. Contention is not what puts these above unity.
        # What does is an asymmetry between the baseline pass and the candidate pass that only
        # exists at twenty-two workers: the same code, timed both ways, reads 0.9922 serially.
        got = (serial or {}).get(task)
        if got is None:
            return {"task": task, "verdict": "unread at one worker", "reading": reading,
                    "tail": tail, "cpsat": True,
                    "why": f"CP-SAT, light tail (p90/p10 = {_tail(tail)}); reads {reading:.4f} at 22 "
                           "workers, and no one-worker reading has been taken -- run "
                           "ALGOTUNE_EVAL_WORKERS=1 ruler_selfcheck --record before scoring it"}
        # AND THE SERIAL READING HAS TO READ UNITY, not merely exist. The first cut accepted any
        # serial row, which would have called `max_clique_cpsat` rulable on a sitting that measured
        # 1.0967 -- outside the same tolerance every other task is held to. Measured the same
        # evening: 0.9922 in one sitting and 1.0967 in the next, both serial, both on an idle box,
        # so even the serial regime needs its number looked at rather than its existence.
        if abs(got[0] - 1.0) > TOLERANCE:
            return {"task": task, "verdict": "unrulable", "reading": reading, "tail": tail,
                    "cpsat": True, "serial": got[0],
                    "why": f"CP-SAT; {reading:.4f} at 22 workers and {got[0]:.4f} at one "
                           f"(measured {got[1][:10]}) -- serial removes most of the excess but not "
                           "enough to price a candidate"}
        return {"task": task, "verdict": "rules at one worker", "reading": reading, "tail": tail,
                "cpsat": True, "serial": got[0],
                "why": f"CP-SAT, light tail (p90/p10 = {_tail(tail)}); {reading:.4f} at 22 workers "
                       f"but {got[0]:.4f} at one, measured {got[1][:10]} -- the pass asymmetry is "
                       "concurrency, not the solver"}
    return {"task": task, "verdict": "off by more than the tolerance", "reading": reading,
            "tail": tail, "cpsat": False, "why": f"self-check {reading:.4f}, not CP-SAT"}


def inventory(tasks, rows=None) -> list[dict]:
    """The classified list, with the regime selection done HERE and nowhere else.

    §314: `main` did this wiring inline and the end-to-end test re-did it with a plain
    `latest_readings()`. The moment a second regime existed the two disagreed -- the test saw the
    six CP-SAT tasks' SERIAL readings in the twenty-two-wide column and called them "rules as is",
    while the tool reported them correctly. Duplicated wiring is how a test comes to check a
    different program than the one that ships.
    """
    rows = ruler_check.entries(ruler_check.DEFAULT_DIR) if rows is None else rows
    readings = ruler_check.latest_readings(regime=CAMPAIGN_REGIME, accept_unstamped=True)
    serial = ruler_check.latest_readings(regime=SERIAL_REGIME)
    return [classify(t, rows, readings, serial) for t in tasks]


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
    # Rows written before the regime was recorded carry no key; they were all taken twenty-two
    # wide, so they are the fallback for a task with no regime-stamped reading yet -- but a serial
    # row must never fill this slot, which is what an unfiltered "latest" did the hour it existed.
    out = inventory(tasks, rows)
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    order = ["rules as is", "rules at one worker", "unread at one worker", "unrulable",
             "unscorable reference",
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
