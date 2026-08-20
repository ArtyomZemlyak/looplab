#!/usr/bin/env python3
"""Rank AlgoTune's 154 tasks by how much ONE evaluation pass costs, and print the cheapest N.

This is how the campaign's task list was chosen, and it is committed so the choice is reproducible
rather than a list somebody once pasted into a script.

WHY COST AND NOT TOPIC
----------------------
The first 20-task list was picked for subject coverage and could not finish. Arm A on `svm` ran
4 h 24 m, was cut by the safety net at 12 of ~20 messages, and wrote no result at all -- roughly
22 minutes per agent message, ~7 h to spend one arm's budget, ~280 h for 40 task-arms.

The cost was knowable in advance and free: `reports/generation.json` carries
`baseline_runs[*].eval_duration_ms` for every task -- one full evaluation pass on the authors'
machine. Absolute numbers do not transfer (this box measured 14-19x slower: `svm` 65 s there vs
~15 min here, `discrete_log` 35 s vs 668 s cold) but the RANKING does.

**The mean is not the story, the worst case is.** The old list carried `btsp` at 772 s per pass,
i.e. ~3 h for ONE evaluation here, so that task-arm could never finish inside any sane timeout and
no budget change would have fixed it. The chosen 20 top out at 60 s.

Cost is the ONLY axis this applies, and it is applied identically to both arms, so it cannot favour
either. The resulting set still spans number theory, combinatorial optimisation, geometry,
interpolation, graphs, clustering, sparse linear algebra and PDEs.

    python pick_tasks.py --algotune-root /srv/AlgoTune [--count 20]
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def task_costs(generation_file: Path) -> list[tuple[float, str]]:
    """`[(median seconds for one evaluation pass, task)]`, cheapest first."""
    data = json.loads(generation_file.read_text(encoding="utf-8"))
    rows: list[tuple[float, str]] = []
    for task, meta in (data or {}).items():
        runs = (meta or {}).get("baseline_runs") or {}
        durations = [r["eval_duration_ms"] for r in runs.values()
                     if isinstance(r, dict) and r.get("success") and r.get("eval_duration_ms")]
        if durations:
            rows.append((statistics.median(durations) / 1000.0, str(task)))
    rows.sort()
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path)
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--quiet", action="store_true", help="Print only the space-separated task list.")
    args = ap.parse_args()

    generation = args.algotune_root.resolve() / "reports" / "generation.json"
    if not generation.exists():
        print(f"no generation.json at {generation}")
        return 1
    rows = task_costs(generation)
    if not rows:
        print("no task carries usable timing data")
        return 1
    picked = rows[:args.count]

    if args.quiet:
        print(" ".join(task for _, task in picked))
        return 0

    print(f"{len(rows)} tasks with timing data; cheapest {len(picked)}:\n")
    print(f"{'task':<34}{'one eval pass (s)':>18}")
    for cost, task in picked:
        print(f"{task:<34}{cost:>18.1f}")
    total = sum(c for c, _ in picked)
    worst = max(picked)
    allcost = [c for c, _ in rows]
    print(f"\nselected total {total:.0f}s   worst single task {worst[1]} at {worst[0]:.0f}s")
    print(f"whole corpus:  median {statistics.median(allcost):.0f}s   max "
          f"{allcost[-1]:.0f}s ({rows[-1][1]})")
    print("\nThese are the authors' machine's numbers. The ranking transfers; the absolute values do"
          "\nnot -- this box measured 14-19x slower. Feed the list to campaign.sh via TASKS=...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
