#!/usr/bin/env python3
"""The probe-cap arm's analysis, written down BEFORE the numbers and executable.

WHY THIS IS A FILE AND NOT A PARAGRAPH. §190 registered the design — twelve batches, two probes per
arm, exact stratified permutation, one-sided, α = 0.05 — and since then the rules for what counts as
a probe have accumulated one incident at a time: `freeB3` excluded at $1.1056 (§213.1), `capB4`
carrying the label with its cap unreached but recorded (§227, §243), pauses at the ceiling that are
really endings (§228), and the per-batch medians that turned out to be a fragile statistic (§254).
Every one of those was decided for a reason at the time, and every one is a degree of freedom that
could be re-decided afterwards to suit whatever number arrives. Written as code, run against a
corpus that is not yet complete, they cannot be.

THE POPULATION. A probe enters the arm if, and only if:
  * its own `config.snapshot.json` records the cap its label claims — 12 for treated, 0 for control
    (§243: `capB4` is in on this rule, and behaviour alone could never have said so);
  * it ended, meaning a `run_finished` event OR a pause at ≥ 99 % of its budget (§228: sixteen of the
    corpus's runs record a normal ending as a Developer crash, and the fix cannot reach the probes
    already recorded);
  * its metered spend is at most $1.05. This is the §213.1 criterion, written before any contrast was
    read, and `freeB3` at $1.1056 is the one probe it excludes.

THE STATISTIC is the sum over batches of (mean treated TEST − mean control TEST), and the test is the
exact permutation over within-batch relabellings — C(4,2) = 6 per batch, 6^12 enumerable — one-sided
in the direction the design predicts. `arm_power.py` computes the same null.

WHAT A NULL MEANS. §234 measured the power at **0.77 against a +44-point effect** on the corpus as it
stands (sd 63.7), so a p above 0.05 says "no effect of that size was detected at power 0.77". It does
NOT say capping does not help, and §190's own falsification clause says so in the same words.

WHAT THIS REFUSES TO DO. It will not read a partial arm. Fewer than `--batches` complete batches and
it prints what is missing and exits 2 — because an interim look at the outcome is the one thing the
design forbids, and a tool that will do it on request is a tool that will be asked.

Usage:
    arm_readout.py --batches 12 [--alpha 0.05] [--max-spend 1.05]
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from itertools import combinations, product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arm_fidelity  # noqa: E402
import events_read  # noqa: E402

ROOT = arm_fidelity.DEFAULT_ROOT
CEILING_SHARE = arm_fidelity.CEILING_SHARE

# The arm as launched, in order. Extending this list is how a batch joins the readout.
BATCHES = [
    (["capA2", "capB2"], ["freeA2", "freeB2"]),
    (["capA3", "capB3"], ["freeA3", "freeB4"]),
    (["capA4", "capB4"], ["freeA4", "freeB5"]),
    (["capA5", "capB5"], ["freeA5", "freeB6"]),
    (["capA6", "capB6"], ["freeA6", "freeB7"]),
    (["capA7", "capB7"], ["freeA7", "freeB8"]),
    (["capA8", "capB8"], ["freeA8", "freeB9"]),
    (["capA9", "capB9"], ["freeA9", "freeB10"]),
    (["capA10", "capB10"], ["freeA10", "freeB11"]),
    # FROM HERE THE LANE<->LABEL MAPPING IS SWAPPED (§266). Batches 1-9 put 17 of 18 treated probes
    # on lanes 0-10 and 11-21 and 18 of 19 controls on 22-32 and 33-43, which makes the label and
    # the lane the same variable. Six sittings of the reference-against-itself ruler could not show
    # the lanes differ (per-sitting contrast positive in 4 of 6, sign test p = 0.34) but also could
    # not exclude ~3 %, so batches 10-12 run treatment on 22-32 and 33-43 and control on 0-10 and
    # 11-21. Registered here, before any contrast was read, so the swap cannot be chosen by outcome.
    (["capA11", "capB11"], ["freeA11", "freeB12"]),
]


def spend(name: str) -> float:
    total = 0.0
    for path in sorted(glob.glob(f"{ROOT}/{name}/runs/*/run/events.jsonl")):
        for event in events_read.iter_events(path):
            if event.get("type") != "llm_usage":
                continue
            data = event.get("data")
            if isinstance(data, dict):
                try:
                    total += max(0.0, float(data.get("cost") or 0.0))
                except (TypeError, ValueError):
                    pass
    return total


def score(name: str):
    try:
        with open(f"{ROOT}/{name}/final.json", encoding="utf-8") as fh:
            value = json.load(fh).get("speedup")
    except (OSError, ValueError):
        return None
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def admit(name: str, arm: str, max_spend: float, budget: float = 1.0):
    """`(score, None)` if this probe enters the arm, else `(None, why not)`."""
    want = 12 if arm == "treat" else 0
    if arm_fidelity.assigned_cap(ROOT, name) != want:
        return None, f"config records {arm_fidelity.assigned_cap(ROOT, name)}, not {want}"
    paid = spend(name)
    ended = arm_fidelity._run_finished(ROOT, name) or paid >= budget * CEILING_SHARE
    if not ended:
        return None, f"has not ended (${paid:.4f})"
    if paid > max_spend:
        return None, f"spent ${paid:.4f}, over the ${max_spend:.2f} ceiling"
    got = score(name)
    if got is None:
        return None, "no usable score"
    return got, None


def stratified_p(batches, alternative_positive: bool = True) -> float:
    obs = sum(statistics.mean(t) - statistics.mean(c) for t, c in batches)
    per = []
    for t, c in batches:
        pool = list(t) + list(c)
        per.append([([pool[i] for i in idx], [x for j, x in enumerate(pool) if j not in idx])
                    for idx in combinations(range(len(pool)), len(t))])
    total = ge = 0
    for combo in product(*per):
        val = sum(statistics.mean(a) - statistics.mean(b) for a, b in combo)
        if (val >= obs) if alternative_positive else (val <= obs):
            ge += 1
        total += 1
    return ge / total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--batches", type=int, default=12)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--max-spend", type=float, default=1.05)
    args = ap.parse_args(argv)

    ready, missing = [], []
    for i, (treat, control) in enumerate(BATCHES, 1):
        rows, why = {}, []
        for name, arm in [(n, "treat") for n in treat] + [(n, "control") for n in control]:
            got, reason = admit(name, arm, args.max_spend)
            if reason:
                why.append(f"{name}: {reason}")
            else:
                rows[name] = got
        if len(rows) == 4:
            ready.append((i, [rows[n] for n in treat], [rows[n] for n in control]))
        else:
            missing.append((i, why))

    print(f"{len(ready)} complete batches of the {args.batches} the design registered")
    for i, why in missing:
        print(f"  batch {i} incomplete: " + "; ".join(why))
    if len(ready) < args.batches:
        print(f"\nREFUSING TO READ THE ARM at {len(ready)} of {args.batches} batches. An interim look "
              "at the outcome is the one thing §190 forbids, and this tool is not the exception.")
        return 2

    batches = [(t, c) for _, t, c in ready[:args.batches]]
    obs = sum(statistics.mean(t) - statistics.mean(c) for t, c in batches)
    p = stratified_p(batches)
    print(f"\nsum of within-batch mean differences: {obs:+.2f}")
    print(f"exact stratified one-sided permutation p = {p:.4f} (alpha {args.alpha})")
    print("REJECT the null" if p <= args.alpha else
          f"do NOT reject: no effect of the registered size was detected at power 0.77 (§234). "
          "That is not the same as 'capping does not help'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
