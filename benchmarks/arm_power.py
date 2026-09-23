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
import posixpath
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
        # POSIX FORM FIRST (WIN-SEPS). A Windows glob answer joins every matched component with
        # "\\", so this containment test found no "/runs/<task>/" in any of them and every champion
        # list came back EMPTY on the Windows CI leg (review 2026-09-22 round 2, run 35804658308:
        # 8 rows here and in test_the_null_is_the_control_population). The round-1 guard covered
        # `split`s only; this is the same defect spelled as `in`.
        posix = path.replace(os.sep, "/")
        if f"/runs/{task}/" not in posix:
            continue
        # THE NAME RELATIVE TO `root`, not a split on a magic path segment. Splitting on
        # "/model-probes/" silently matched nothing for any root not called that -- including every
        # test fixture, which is how the mutation that removes this filter first came back green.
        probe = posixpath.relpath(posix, Path(root).as_posix()).split("/")[0]
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


def rate_power(control_rate: float, treat_rate: float, per_probe: int, n_batches: int,
               trials: int, alpha: float, seed: int = 20260918):
    """Power for a BEHAVIOUR outcome -- a per-probe RATE rather than a score.

    WHY THIS MODE EXISTS (doc 56 §421). §137 measured the same treatment two ways over the same
    nine-against-nine runs: the behaviour outcome reached p = 0.0013 and the SCORE outcome only
    p = 0.0567. On a bimodal score distribution (§108: two clusters, sd 79.7 over the corpus) an
    affordable arm cannot see an effect the size the loop plausibly produces -- §420's table --
    while the behaviour it is supposed to change is a proportion with far less spread. So an arm
    whose primary outcome is "did the run keep the regime of its strong node" needs ITS power
    computed, not the score table's borrowed.

    The test is the same one `power()` uses and the same one the arm will use: each probe
    contributes ONE number and `stratified_p` permutes within batches. The only change is what the
    number IS -- here a probe's own share of its own transitions, drawn as `Binomial(k, p) / k`.

    `per_probe` is how many transitions one probe offers. It is a PARAMETER and not a corpus
    reading on purpose: `node_created` records `parent_ids` in only 20 of 119 archived runs
    (§422), so the corpus cannot say, and a preregistration that quoted a number the corpus does
    not hold would be exactly the borrowed-table mistake §187 was written about.
    """
    rnd = random.Random(seed + n_batches * 100 + per_probe)
    hits = 0
    for _ in range(trials):
        batches = []
        for _b in range(n_batches):
            control = [sum(rnd.random() < control_rate for _ in range(per_probe)) / per_probe
                       for _ in range(2)]
            treat = [sum(rnd.random() < treat_rate for _ in range(per_probe)) / per_probe
                     for _ in range(2)]
            batches.append((treat, control))
        if stratified_p(batches) <= alpha:
            hits += 1
    return hits / trials


def fisher_one_sided(a: int, b: int, c: int, d: int) -> float:
    """One-sided Fisher exact p for the 2x2 `[[a, b], [c, d]]`, treatment-is-better.

    Exact and dependency-free (`math.comb`): the bench has no scipy and a power tool that cannot run
    where the arm runs is not a power tool.
    """
    from math import comb
    n = a + b + c + d
    row1, col1 = a + b, a + c
    total = comb(n, row1)
    return sum(comb(col1, k) * comb(n - col1, row1 - k)
               for k in range(a, min(row1, col1) + 1)) / total


def pooled_rate_power(control_rate: float, treat_rate: float, per_arm: int, trials: int,
                      alpha: float, seed: int = 20260918):
    """Power for a POOLED behaviour outcome: one 2x2 over every transition both arms produced.

    §137 measured exactly this way -- 15 of 16 against 20 of 41, p = 0.0013 -- and THAT is the
    reason to use it: a number computed by the same test lands beside the published one, and the
    programme's question is whether the policy does what the card clause did not.

    IT IS NOT MORE POWERFUL, and the first version of this docstring claimed it was. Measured, at
    twelve batches and one transition per probe: pooled 0.89 against per-probe 0.90 at a 0.5 -> 0.9
    gap, and 0.43 against 0.39 at 0.7 -> 0.9. The two tests see the same thing at this size, and the
    binding constraint is neither of them -- it is that a probe offers about ONE transition (§422:
    with the floor at three the gate fires at most once a run), so only a LARGE rate gap is
    visible at any affordable size. A rationale the measurement does not support is worse than no
    rationale, which is why this paragraph replaced it rather than standing beside it.

    What pooling costs is the batch stratification, this bench's control for lane and time
    confounds. Affordable HERE and only here: the outcome is a property of the run's own
    transitions, not a timing, so a slow lane cannot move it the way it moves a speedup.
    """
    rnd = random.Random(seed + per_arm)
    hits = 0
    for _ in range(trials):
        t = sum(rnd.random() < treat_rate for _ in range(per_arm))
        c = sum(rnd.random() < control_rate for _ in range(per_arm))
        if fisher_one_sided(t, per_arm - t, c, per_arm - c) <= alpha:
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
    # THE BEHAVIOUR MODE (§421). `--outcome rate` sizes an arm whose primary outcome is a per-probe
    # proportion; the three rate flags are its parameters and the corpus is not consulted, because
    # the corpus cannot answer them (§422).
    ap.add_argument("--outcome", choices=("score", "rate"), default="score")
    ap.add_argument("--control-rate", type=float, default=0.5,
                    help="the share a CONTROL probe is assumed to reach (§108's coin flip)")
    ap.add_argument("--treat-rate", type=float, default=0.9,
                    help="the share a TREATED probe is assumed to reach")
    ap.add_argument("--per-probe", type=int, default=2,
                    help="how many transitions one probe offers the outcome")
    ap.add_argument("--pooled", action="store_true",
                    help="with --outcome rate: ONE 2x2 over every transition, Fisher exact (§137's "
                         "own test) instead of a per-probe rate permuted within batches")
    args = ap.parse_args(argv)

    if args.outcome == "rate":
        # THE CORPUS IS NOT READ IN THIS MODE, and that is the honest thing rather than a shortcut:
        # the rates are assumptions the preregistration states, and `parent_ids` reaches only 20 of
        # 119 archived runs (§422), so a corpus reading here would be a number the corpus does not
        # hold dressed as a measurement.
        print(f"outcome: a per-probe RATE. control {args.control_rate:.2f} against treated "
              f"{args.treat_rate:.2f}, {args.per_probe} transition(s) per probe, "
              f"alpha {args.alpha}, {args.trials} trials per row")
        print("these rates are ASSUMPTIONS the preregistration must state; the corpus cannot "
              "supply them (§422)\n")
        print(f'{"batches":>8s} {"probes":>7s} {"$":>6s} {"trans/arm":>10s} {"power":>7s}')
        for nb in args.batches:
            per_arm = nb * 2 * args.per_probe          # two probes an arm per batch
            pw = (pooled_rate_power(args.control_rate, args.treat_rate, per_arm, args.trials,
                                    args.alpha) if args.pooled
                  else rate_power(args.control_rate, args.treat_rate, args.per_probe, nb,
                                  args.trials, args.alpha))
            print(f"{nb:8d} {nb * 4:7d} {nb * 4:6d} {per_arm:10d} {pw:7.2f}")
        return 0

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
