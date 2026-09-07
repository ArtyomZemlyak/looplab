#!/usr/bin/env python3
"""How many probes an arm needs, simulated against the corpus's own score distribution.

WHY THIS EXISTS. §115's arm was sized at twelve a side from a power table (§83) computed on a
different outcome, a different task and a smaller corpus, and it closed at p = 0.1341 having
answered nothing (§180). §186 then measured a real effect worth designing an arm around — node 0
carrying a kernel moves the final champion by 23.5 points — and the only honest next step is to
compute the probe count BEFORE the money, against the spread this corpus actually has.

THE MODEL. Scores are resampled from the corpus's own `edge_expansion` champions rather than from a
normal: the distribution is left-skewed with a hard floor near 1 and a ceiling near 277, and a
normal approximation understates how often two arms of four look different by luck. The treatment
arm is the same empirical distribution shifted by `--effect`. The test is the one the arm would
actually use -- the exact stratified permutation over within-batch relabellings (§146, §180) -- run
on simulated batches of four, two per arm.

Measured on 2026-09-04 over 69 `edge_expansion` champions (median 202.70, p10 106.69, p90 267.73):
a 23.5-point effect needs far more probes than any arm run so far. The table this prints is the
answer to "can we afford to ask?", and it is meant to be printed before, not after.

Usage:
    arm_power.py [--effect POINTS] [--batches N ...] [--trials N] [--alpha A]
"""
from __future__ import annotations

import argparse
import glob
import json
import random
import statistics
import sys
from itertools import combinations, product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events_read  # noqa: E402

DEFAULT_ROOT = "/var/tmp/looplab-bench/model-probes"


def champions(root: str, task: str = "edge_expansion") -> list[float]:
    """The final champion of every run of `task` -- the arm's own primary outcome (§146)."""
    out = []
    for path in sorted(glob.glob(f"{root}/*/runs/*/run/events.jsonl")):
        if f"/runs/{task}/" not in path:
            continue
        metrics = [(e.get("data") or {}).get("metric")
                   for e in events_read.iter_events(path) if e.get("type") == "node_evaluated"]
        metrics = [m for m in metrics if isinstance(m, (int, float))]
        if metrics:
            out.append(float(max(metrics)))
    return out


EXACT_NULL_CAP = 4096      # 6**4 = 1296 enumerates instantly; 6**6 = 46,656 x 300 trials does not


def stratified_p(batches, draws: int = 2000, rnd: random.Random | None = None) -> float:
    """One-sided permutation p over within-batch relabellings, treatment-is-better.

    Exact while the null is small; SAMPLED above `EXACT_NULL_CAP`. The first version of this file
    always enumerated, and six batches x 300 trials is 14 million relabellings -- it ran for ten
    minutes without printing a row and had to be killed by pid. A power tool that cannot be run is
    not a power tool.
    """
    obs = sum(statistics.mean(t) - statistics.mean(c) for t, c in batches)
    per = []
    for t, c in batches:
        pool = list(t) + list(c)
        per.append([([pool[i] for i in idx], [x for j, x in enumerate(pool) if j not in idx])
                    for idx in combinations(range(4), 2)])
    if 6 ** len(batches) <= EXACT_NULL_CAP:
        total = ge = 0
        for combo in product(*per):
            if sum(statistics.mean(a) - statistics.mean(b) for a, b in combo) >= obs:
                ge += 1
            total += 1
        return ge / total
    rnd = rnd or random.Random(1)
    ge = 0
    for _ in range(draws):
        val = sum(statistics.mean(a) - statistics.mean(b)
                  for a, b in (rnd.choice(opts) for opts in per))
        if val >= obs:
            ge += 1
    return ge / draws


def power(scores, effect: float, n_batches: int, trials: int, alpha: float, seed: int = 20260904):
    """Share of simulated arms that would reach `alpha`, resampling from `scores`."""
    rnd = random.Random(seed + n_batches)
    hits = 0
    for _ in range(trials):
        batches = []
        for _b in range(n_batches):
            control = [rnd.choice(scores) for _ in range(2)]
            treat = [rnd.choice(scores) + effect for _ in range(2)]
            batches.append((treat, control))
        if stratified_p(batches) <= alpha:
            hits += 1
    return hits / trials


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--task", default="edge_expansion")
    ap.add_argument("--effect", type=float, default=23.5,
                    help="points the treatment adds (default: §186's kernel-first effect)")
    ap.add_argument("--batches", type=int, nargs="*", default=[3, 6, 9, 12],
                    help="paired batches of four to simulate (2 per arm each)")
    ap.add_argument("--trials", type=int, default=400)
    ap.add_argument("--alpha", type=float, default=0.05)
    args = ap.parse_args(argv)

    scores = champions(args.root, args.task)
    if len(scores) < 10:
        print(f"only {len(scores)} champions for {args.task}; refusing to simulate from that",
              file=sys.stderr)
        return 2
    srt = sorted(scores)
    print(f"{len(scores)} {args.task} champions: median {statistics.median(scores):.2f}, "
          f"p10 {srt[int(0.1 * len(srt))]:.2f}, p90 {srt[int(0.9 * len(srt))]:.2f}, "
          f"sd {statistics.pstdev(scores):.1f}")
    print(f"effect simulated: +{args.effect:.1f} points, alpha {args.alpha}, "
          f"{args.trials} trials per row\n")
    print(f'{"batches":>8s} {"probes":>7s} {"$":>6s} {"power":>7s}')
    for nb in args.batches:
        pw = power(scores, args.effect, nb, args.trials, args.alpha)
        print(f"{nb:8d} {nb * 4:7d} {nb * 4:6d} {pw:7.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
