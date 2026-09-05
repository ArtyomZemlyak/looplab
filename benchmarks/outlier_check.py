#!/usr/bin/env python3
"""Where a RUNNING probe sits in the corpus, on the variables the sweep keeps asking about by hand.

WHY. Three times in three sweeps the same question came up and was answered with an ad-hoc query:
is a 47 s evaluation unusual (§226 — no, it is the weak-node tail), is 52 % of spend in
`deep_research` unusual (§256 — yes, the corpus maximum and the only run above 40 %), is a first node
at 62 % of budget unusual (§256 — yes, past a corpus maximum of 53 %). Two of the three turned out to
be ordinary and one did not, and the difference was never guessable — it needed the distribution.

So the distribution comes to the sweep instead. For each running probe this reports its **percentile
within the finished corpus** on a few process variables, and names only what falls outside p5..p95.

WHAT IT DELIBERATELY DOES NOT COMPARE is the score. Node COUNT and spend are process; a probe's
metric is the outcome, and §190 forbids reading the arm's outcome in flight. Adding "and its node is
in the 12th percentile" would turn a hygiene tool into an interim read, which is exactly how
`arm_fidelity` nearly went wrong twice (§209, §212).

Usage:
    outlier_check.py [--task edge_expansion] [--root DIR]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events_read  # noqa: E402
import lanes  # noqa: E402

DEFAULT_ROOT = "/var/tmp/looplab-bench/model-probes"
BENCH = "/var/tmp/looplab-bench"


def phase_series(spans_path: str) -> list:
    """(cost, phase) for every generation span, in file order -- which is append order, which is
    time order. Kept separate from `measure` so a corpus run can be replayed only as far as a
    running probe has got."""
    out = []
    try:
        fh = open(spans_path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            if not line.startswith("{"):
                continue
            try:
                span = json.loads(line)
            except ValueError:
                continue
            if span.get("name") != "generation":
                continue
            attrs = span.get("attributes") or {}
            try:
                out.append((float(attrs.get("cost") or 0.0), attrs.get("phase") or "?"))
            except (TypeError, ValueError):
                pass
    return out


def measure(events_path: str, spans_path: str, cap: float | None = None) -> dict:
    """Process variables for one probe. No scores: counts, money and shares only.

    `cap` truncates the phase shares to the first `cap` dollars of generation spend, so a finished
    run can be read as it looked when it was as young as the probe being placed."""
    spend = 0.0
    first_node_at = None
    nodes = 0
    for event in events_read.iter_events(events_path):
        kind = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if kind == "llm_usage":
            try:
                spend += max(0.0, float(data.get("cost") or 0.0))
            except (TypeError, ValueError):
                pass
        elif kind == "node_evaluated":
            metric = data.get("metric")
            if isinstance(metric, (int, float)) and metric > 0:
                nodes += 1
                if first_node_at is None:
                    first_node_at = spend
    phases: collections.Counter = collections.Counter()
    seen = 0.0
    for cost, phase in phase_series(spans_path):
        if cap is not None and seen >= cap:
            break
        seen += cost
        phases[phase] += cost
    # ABSOLUTE DOLLARS, NOT A SHARE OF SPEND-SO-FAR. The first version reported
    # `first_node_at / spend`, and its very first run flagged three healthy probes: a running probe's
    # denominator is the money it has spent SO FAR, so the same node reads 54 % at $0.59 and 32 % at
    # its eventual $1.01. Comparing that against a corpus of FINAL shares is §209's mistake wearing
    # different clothes -- a partial quantity held against a complete one. Dollars are the same
    # number whenever they are read.
    out = {"spend": spend, "nodes": nodes,
           "first_node_usd": first_node_at, "gen_spend": seen}
    total = sum(phases.values())
    for phase in ("deep_research", "plan_step", "propose", "repropose", "plan"):
        out[f"share_{phase}"] = (100 * phases.get(phase, 0.0) / total) if total else None
    return out


def corpus(root: str, task: str, min_spend: float = 0.9, cap: float | None = None) -> dict:
    """The finished-run distribution of each variable.

    With `cap`, each finished run's phase shares are read at the point it had spent `cap` dollars on
    generation. Membership still needs a FINISHED run (`min_spend`, measured on the whole run) --
    the truncation changes how a corpus run is read, never which runs are in the corpus.
    """
    values: dict = collections.defaultdict(list)
    for events_path in sorted(glob.glob(f"{root}/*/runs/{task}/run/events.jsonl")):
        spans_path = events_path.replace("events.jsonl", "spans.jsonl")
        whole = measure(events_path, spans_path)
        if whole["spend"] < min_spend:
            continue
        got = measure(events_path, spans_path, cap=cap) if cap is not None else whole
        for key, value in got.items():
            if isinstance(value, (int, float)):
                values[key].append(float(value))
    return dict(values)


def percentile(sample, value) -> float:
    """Midrank percentile: ties count half.

    The first version counted values `<= value`, so a probe sitting exactly ON the corpus median
    read as the **100th** percentile when every corpus run happened to share that value -- and got
    flagged as an outlier for being typical. Ties are not evidence of extremity; the midrank
    convention puts a value equal to everything else at 50, where it belongs.
    """
    if not sample:
        return float("nan")
    below = sum(1 for x in sample if x < value)
    equal = sum(1 for x in sample if x == value)
    return 100.0 * (below + 0.5 * equal) / len(sample)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--task", default="edge_expansion")
    ap.add_argument("--low", type=float, default=5.0)
    ap.add_argument("--high", type=float, default=95.0)
    args = ap.parse_args(argv)

    whole_dist = corpus(args.root, args.task)
    dist = whole_dist
    if not dist:
        print(f"no finished {args.task} runs to compare against", file=sys.stderr)
        return 2
    live = [r["probe"] for r in lanes.probes(BENCH) if r["probe"]]
    if not live:
        print("no bench probe running")
        return 0
    n = len(next(iter(dist.values())))
    print(f"{len(live)} running probe(s) against {n} finished {args.task} runs")
    cache: dict = {}
    flagged = 0
    for name in sorted(live):
        found = sorted(glob.glob(f"{args.root}/{name}/runs/{args.task}/run/events.jsonl"))
        if not found:
            continue
        got = measure(found[0], found[0].replace("events.jsonl", "spans.jsonl"))
        # READ THE CORPUS AT THE PROBE'S AGE, NOT AT ITS OWN END. A share of generation spend is a
        # PARTIAL quantity for a running probe and a COMPLETE one for a corpus run, and the phases
        # are not evenly spread over a run: measured 2026-09-05, the corpus median `share_propose`
        # is 31.1 % over the first $0.567 and 25.3 % over the whole run, so a young probe reads high
        # on it for no reason but its age. That mismatch flagged two of three healthy probes --
        # `freeB11` fell from the 98th percentile to the 79th and `capB10` from the 98th to the
        # 91st once each corpus run was replayed only as far as the probe had got. It is §209's
        # mistake for the third time: a partial quantity held against a complete one.
        near = cache.get(round(got["gen_spend"], 6))
        if near is None:
            near = corpus(args.root, args.task, cap=got["gen_spend"])
            cache[round(got["gen_spend"], 6)] = near
        said = []
        for key, value in got.items():
            if not isinstance(value, (int, float)) or key not in dist:
                continue
            # A RUNNING PROBE HAS SPENT LESS AND BUILT FEWER NODES THAN A FINISHED ONE, BY
            # DEFINITION. Comparing those two against the finished corpus flags every live probe
            # every time, which is an alarm that means nothing and trains its reader to skip the
            # ones that do. Only quantities that are already final mid-run are compared.
            if key in ("spend", "nodes", "gen_spend"):
                continue
            # Shares are read against the age-matched corpus; everything else against the whole run.
            dist = near if key.startswith("share_") else whole_dist
            # A VARIABLE WITH NO SPREAD CANNOT PLACE ANYTHING. When every corpus run records the
            # same value -- a phase no run on this task ever entered, say -- any reading is
            # simultaneously the minimum and the maximum, and flagging it is noise dressed as a
            # finding. Only when the probe MATCHES that constant, though: a value far outside a
            # corpus that never varies is the most notable thing such a corpus can produce, and the
            # first version of this guard silently swallowed exactly that case.
            if min(dist[key]) == max(dist[key]) == value:
                continue
            pct = percentile(dist[key], value)
            if pct <= args.low or pct >= args.high:
                said.append(f"{key}={value:.4g} at the {pct:.0f}th pct "
                            f"(corpus median {statistics.median(dist[key]):.4g})")
        print(f"  {name:10s} ${got['spend']:.4f} {got['nodes']} node(s)"
              + ("" if said else "  -- nothing outside p%g..p%g" % (args.low, args.high)))
        for line in said:
            flagged += 1
            print(f"      OUTSIDE: {line}")
    if not flagged:
        print("  no probe is outside the corpus on any process variable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
