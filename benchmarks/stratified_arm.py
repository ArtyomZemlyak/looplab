#!/usr/bin/env python3
"""The batch-stratified exact test for a two-arm probe comparison, computed rather than hand-rolled.

WHY THIS FILE EXISTS. §144 measured a between-batch component in the node-0 kernel rate as large as
the effect the arm is hunting: across nine batches of three or more probes the rates are 0.00, 0.67,
1.00, 0.67, 1.00, 0.67, 0.25, 0.50, 0.00, with a variance of 0.1424 against the 0.0724 a constant
rate would give. The probes are launched two per arm per batch precisely so that component cancels;
pooling the counts throws that away and lets it back into the denominator.

So the reading has to condition on the batch. And §143 is the record of what hand-rolling a
pre-registered statistic per sweep costs -- a mutation guard that guarded nothing, pushed as though
it had. A statistic that decides an arm belongs in a file with tests.

THE TEST. Each batch contributes a 2x2 table with its own margins. Conditioning on those margins,
the count in one cell is hypergeometric; the null distribution of the SUM across batches is the
convolution of those hypergeometrics, computed exactly here rather than approximated. That is the
conditional (network-algorithm) test, on counts small enough that the convolution is cheap.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys
from math import comb


def hypergeom_pmf(n1: int, n2: int, k: int) -> dict[int, float]:
    """P(a) for a 2x2 with row totals n1, n2 and first-column total k, under the null."""
    total = n1 + n2
    out: dict[int, float] = {}
    lo, hi = max(0, k - n2), min(n1, k)
    denom = comb(total, k)
    for a in range(lo, hi + 1):
        out[a] = comb(n1, a) * comb(n2, k - a) / denom
    return out


def stratified_p(strata: list[tuple[int, int, int, int]]) -> tuple[float, int, float]:
    """One-sided P(sum of arm-A successes >= observed), conditioning on each stratum's margins.

    `strata` is a list of (a, n1, b, n2): a successes of n1 in arm A, b of n2 in arm B.
    Returns (p, observed sum, expected sum). A stratum with no variability contributes a point mass
    and cannot move the p -- which is the honest treatment of a batch where both arms did the same
    thing, and the reason a design like this needs batches that disagree.
    """
    dist = {0: 1.0}
    obs = exp = 0
    for a, n1, b, n2 in strata:
        obs += a
        k = a + b
        pmf = hypergeom_pmf(n1, n2, k)
        exp += sum(v * p for v, p in pmf.items())
        nxt: dict[int, float] = collections.defaultdict(float)
        for s, ps in dist.items():
            for v, pv in pmf.items():
                nxt[s + v] += ps * pv
        dist = dict(nxt)
    p = sum(pr for s, pr in dist.items() if s >= obs)
    return p, obs, exp


def _first_node_kernel(run: str) -> bool | None:
    nid = None
    try:
        with open(os.path.join(run, "events.jsonl"), errors="replace") as fh:
            for line in fh:
                if '"node_evaluated"' not in line:
                    continue
                try:
                    nid = (json.loads(line).get("data") or {}).get("node_id")
                except ValueError:
                    return None
                break
    except OSError:
        return None
    if nid is None:
        return None
    nd = os.path.join(run, "nodes", f"node_{nid}")
    if not os.path.isdir(nd):
        return None
    return any(f.endswith((".pyx", ".pxd")) for f in os.listdir(nd))


def collect(root: str, task: str) -> list[tuple[str, str, bool]]:
    """(batch, card_args, kernel) per probe that has an instrument and an evaluated node."""
    rows = []
    for f in glob.glob(os.path.join(root, "model-probes/*/runs", task, "run/events.jsonl")):
        probe = f.split("/model-probes/")[1].split("/")[0]
        inst = os.path.join(root, "model-probes", probe, "INSTRUMENT.txt")
        started = card = ""
        try:
            for line in open(inst, errors="replace"):
                if line.startswith("started:"):
                    started = line.split(":", 1)[1].strip()
                elif line.startswith("card_args:"):
                    card = line.split(":", 1)[1].strip()
        except OSError:
            continue
        k = _first_node_kernel(os.path.dirname(f))
        if not started or k is None:
            continue
        rows.append((started[:13], card, k))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bench-root", default=os.environ.get("BENCH_ROOT", "/var/tmp/looplab-bench"))
    ap.add_argument("--task", default="edge_expansion")
    ap.add_argument("--arm-a", required=True, help="substring of card_args identifying arm A")
    ap.add_argument("--arm-b", required=True, help="substring of card_args identifying arm B, or "
                                                   "the literal `shipped` for the flagless card")
    a = ap.parse_args(argv)

    def which(card: str) -> str | None:
        shipped = "none" in card.lower()
        if a.arm_a != "shipped" and a.arm_a in card:
            return "A"
        if a.arm_a == "shipped" and shipped:
            return "A"
        if a.arm_b != "shipped" and a.arm_b in card:
            return "B"
        if a.arm_b == "shipped" and shipped:
            return "B"
        return None

    per = collections.defaultdict(lambda: {"A": [0, 0], "B": [0, 0]})
    for batch, card, k in collect(a.bench_root, a.task):
        arm = which(card)
        if arm is None:
            continue
        per[batch][arm][0] += int(k)
        per[batch][arm][1] += 1

    strata = []
    print(f"{'batch':16} {'A kernel/n':>12} {'B kernel/n':>12}")
    for batch in sorted(per):
        A, B = per[batch]["A"], per[batch]["B"]
        if not A[1] or not B[1]:
            print(f"  {batch:14} {A[0]}/{A[1]:<10} {B[0]}/{B[1]:<10}  (one arm absent -- dropped)")
            continue
        print(f"  {batch:14} {A[0]}/{A[1]:<10} {B[0]}/{B[1]:<10}")
        strata.append((A[0], A[1], B[0], B[1]))
    if not strata:
        print("no batch has both arms -- nothing to test", file=sys.stderr)
        return 2
    p, obs, exp = stratified_p(strata)
    ta = sum(s[0] for s in strata); na = sum(s[1] for s in strata)
    tb = sum(s[2] for s in strata); nb = sum(s[3] for s in strata)
    print(f"\n  strata used: {len(strata)}")
    print(f"  arm A {ta}/{na}, arm B {tb}/{nb}")
    print(f"  stratified exact one-sided p (A > B) = {p:.5f}   observed {obs}, expected {exp:.2f}")
    pooled, _, _ = stratified_p([(ta, na, tb, nb)])
    print(f"  pooled Fisher, for comparison         = {pooled:.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
