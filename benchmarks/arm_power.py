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
import os
import re
import json
import random
import statistics
import sys
from itertools import combinations, product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events_read
import lanes  # noqa: E402

DEFAULT_ROOT = "/var/tmp/looplab-bench/model-probes"


SHIPPED_CARD = "(none -- the shipped card)"


def node0_kernel_effect(root: str, task: str, min_nodes: int = 2):
    """`(effect, n_kernel, n_none)` for "node 0 carried a kernel" -- measured, not quoted.

    §380. `--effect` defaults to 23.5, the figure §186 measured over 69 edge_expansion runs with two
    or more nodes. The corpus is 118 such runs now, and the same split gives **+29.86**: the number
    that sizes every arm is stale by a quarter, and nothing said so because it was a default rather
    than a reading.

    Recomputed with §377's distinction, which turns out NOT to matter here:

        node 0 cython  n=50  node0 median 170.07  champion median 220.75
        node 0 numba   n=16  node0 median  27.67  champion median 205.47
        node 0 plain   n=52  node0 median  22.97  champion median 189.78

    Merged as §186 did (any kernel against none) the difference is +29.86; splitting Cython out
    gives +30.96, about one point apart. So §377's eightfold gap is real between CHAMPION kinds and
    does not carry into the node-0 effect -- a run that opens with numba usually ships Cython later.
    That correction belongs beside the number, because I raised the suspicion in §377 and it was
    wrong.
    """
    kernel, none = [], []
    for run in sorted(glob.glob(f"{root}/*/runs/{task}/run")):
        metrics = [(e.get("data") or {}).get("metric")
                   for e in events_read.iter_events(os.path.join(run, "events.jsonl"))
                   if e.get("type") == "node_evaluated"]
        metrics = [m for m in metrics if isinstance(m, (int, float))]
        if len(metrics) < min_nodes:
            continue
        node0 = os.path.join(run, "nodes", "node_0")
        body = ""
        solver = os.path.join(node0, "solver.py")
        if os.path.exists(solver):
            body = Path(solver).read_text(encoding="utf-8", errors="replace")
        has = bool(glob.glob(node0 + "/*.pyx")) or bool(
            re.search(r"cimport|import cython|@njit|import numba|from numba", body))
        (kernel if has else none).append(max(metrics))
    if len(kernel) < 2 or len(none) < 2:
        return None, len(kernel), len(none)
    return statistics.median(kernel) - statistics.median(none), len(kernel), len(none)


def probe_card_args(root: str, name: str):
    """What `card_args` this probe was launched with, or None when it predates the instrument.

    §374. The population an arm draws from is the CONTROL population -- the shipped card with no
    flags. Simulating the null from every run of the task mixes in the deliberate arms, and measured
    2026-09-09 that is not conservative, it is optimistic:

        all edge_expansion champions   n=118  median 216.44  sd 68.28
        shipped card only              n= 70  median 216.66  sd 78.11

    Every treatment arm is TIGHTER than the control (sd 29-52 against 78), so pooling them shrinks
    the spread the power divides by and the table asks for fewer probes than the arm needs.

    `card_sha256` cannot answer this on its own: `--checker …` leaves the card identical and changes
    what grades it, so twelve runs share the control's sha with a different treatment. The flags are
    what name the population.
    """
    try:
        body = Path(f"{root}/{name}/INSTRUMENT.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    got = re.search(r"^card_args:\s*(.*)$", body, re.M)
    return got.group(1).strip() if got else None


def champions(root: str, task: str = "edge_expansion", min_spend: float = 0.9,
              live=None, control_only: bool = True) -> list[float]:
    """The final champion of every FINISHED run of `task` -- the arm's own primary outcome (§146).

    §373. This used to take `max(metric)` from every run with any evaluated node, running ones
    included. Measured 2026-09-09 with four pagerank probes in flight: the tool reported "10
    pagerank champions" over six finished runs and four still working, whose champion is whatever
    their FIRST node happened to score. The spread it hands the simulation was sd **11.8** with them
    and **9.8** without -- a fifth more variance, and variance is what the power divides by. A
    partial quantity pooled with complete ones, deciding money: §360 in the tool that sizes the
    arm.

    Two filters, because they answer different questions and this box has both kinds of run. A live
    process is the reading §360 settled on -- `arm_fidelity.is_finished` calls `freeB3` and `remDL`
    unfinished though they stopped weeks ago. `min_spend` is `outlier_check.corpus`'s rule, and
    using the same number is the point: two tools reading one corpus should not disagree about
    which runs are in it.
    """
    live = {p.get("probe") for p in (lanes.probes() if live is None else live) if p.get("probe")}
    out = []
    for path in sorted(glob.glob(f"{root}/*/runs/*/run/events.jsonl")):
        if f"/runs/{task}/" not in path:
            continue
        # THE NAME RELATIVE TO `root`, not a split on a magic path segment. Splitting on
        # "/model-probes/" silently matched nothing for any root not called that -- including every
        # test fixture, which is how the mutation that removes this filter first came back green.
        probe = os.path.relpath(path, root).split(os.sep)[0]
        if probe in live:
            continue
        # THE CONTROL POPULATION, NOT EVERY RUN (§374). A probe with no instrument predates the
        # field and its card is unknown -- unknown is not "the shipped one", so it is left out
        # rather than assumed in.
        if control_only and probe_card_args(root, probe) != SHIPPED_CARD:
            continue
        spend, metrics = 0.0, []
        for event in events_read.iter_events(path):
            kind = event.get("type")
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if kind == "llm_usage":
                try:
                    spend += max(0.0, float(data.get("cost") or 0.0))
                except (TypeError, ValueError):
                    pass
            elif kind == "node_evaluated":
                got = data.get("metric")
                if isinstance(got, (int, float)):
                    metrics.append(float(got))
        if metrics and spend >= min_spend:
            out.append(max(metrics))
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
    # AND WHAT THE CORPUS SAYS THE EFFECT IS TODAY (§380). The default is a reading from a corpus
    # that has since grown; a default nobody re-measures is a quoted number wearing a flag's clothes.
    measured, n_k, n_n = node0_kernel_effect(args.root, args.task)
    if measured is not None and abs(measured - args.effect) > 2.0:
        print(f"NOTE: this corpus now gives +{measured:.2f} points for a kernel at node 0 "
              f"({n_k} runs against {n_n}); --effect is simulating +{args.effect:.1f}. Sizing an "
              "arm on the smaller figure asks for more probes than the effect needs.")
    print(f"effect simulated: +{args.effect:.1f} points, alpha {args.alpha}, "
          f"{args.trials} trials per row\n")
    print(f'{"batches":>8s} {"probes":>7s} {"$":>6s} {"power":>7s}')
    for nb in args.batches:
        pw = power(scores, args.effect, nb, args.trials, args.alpha)
        print(f"{nb:8d} {nb * 4:7d} {nb * 4:6d} {pw:7.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
