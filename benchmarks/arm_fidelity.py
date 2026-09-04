#!/usr/bin/env python3
"""Did the treatment actually differ from the control? — without looking at the outcome.

§190's design forbids reading the arm before twelve batches are in, and §180 is the record of why.
But treatment FIDELITY is not the outcome: whether the capped probes really made fewer probe calls
than the uncapped ones is a property of the intervention, and finding out late that the two arms
did the same thing is how $48 becomes nothing. §195 is that failure caught four minutes in; this is
the same question asked continuously.

**This tool reads no scores, on purpose.** It counts `run_probe` calls and refusals and stops. A
version that also printed the champion would turn every fidelity check into an interim read of the
arm, which the design forbids and which no amount of discipline reliably prevents once the number
is on the screen.

WHAT TO WATCH. §196 measured that a cap of 12 bites 91 % of `edge_expansion` runs, so most control
probes should land ABOVE 12 and every treated probe at exactly 12. A batch where the control also
sits at nine or ten is a batch with little contrast — it dilutes the effect the power table assumed,
and the honest response is to say so, not to reinterpret it afterwards.

Usage:
    arm_fidelity.py --treat capA2 capB2 --control freeA2 freeB2 [--root DIR]
"""
from __future__ import annotations

import argparse
import glob
import os
import json
import statistics
import sys

DEFAULT_ROOT = "/var/tmp/looplab-bench/model-probes"


def probe_calls(root: str, name: str) -> dict:
    """`{executed, refused, spans, finished}` for one probe tree. No scores are read.

    `finished` is the EXISTENCE of `final.json`, never its contents. A running probe's count is a
    lower bound and a finished one's is the answer, and mixing them is how this tool spent three
    sweeps printing "NO CONTRAST YET" at a moment when the treatment had already stopped at its cap
    of 12 and the control was still climbing through 10, 11, 12. A negative contrast between a
    finished arm and an unfinished one is not evidence about the intervention; it is the clock.
    """
    executed = refused = 0
    spans: set = set()
    for path in sorted(glob.glob(f"{root}/{name}/runs/*/run/spans.jsonl")):
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"run_probe"' not in line:
                    continue
                try:
                    span = json.loads(line)
                except ValueError:
                    continue
                attrs = span.get("attributes") or {}
                if span.get("kind") != "tool" or attrs.get("tool") != "run_probe":
                    continue
                spans.add(attrs.get("phase_span"))
                if "run_probe refused" in str(attrs.get("output", "")):
                    refused += 1
                else:
                    executed += 1
    return {"executed": executed, "refused": refused, "spans": len(spans),
            "finished": os.path.exists(f"{root}/{name}/final.json")}


def report(root: str, treat, control) -> dict:
    """Contrast over FINISHED probes only, with the running ones counted but not compared."""
    rows = {n: probe_calls(root, n) for n in list(treat) + list(control)}

    def started(names):
        return [n for n in names if rows[n]["executed"] or rows[n]["refused"]]

    t = [rows[n]["executed"] for n in started(treat) if rows[n]["finished"]]
    c = [rows[n]["executed"] for n in started(control) if rows[n]["finished"]]
    running = [n for n in started(list(treat) + list(control)) if not rows[n]["finished"]]
    tm = statistics.median(t) if t else 0
    cm = statistics.median(c) if c else 0
    return {"rows": rows, "running": running,
            "treat_n": len(t), "control_n": len(c),
            "treat_median": tm, "control_median": cm,
            "contrast": (cm - tm) if (t and c) else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--treat", nargs="+", required=True)
    ap.add_argument("--control", nargs="+", required=True)
    args = ap.parse_args(argv)

    got = report(args.root, args.treat, args.control)
    print(f'{"probe":10s} {"arm":>9s} {"executed":>9s} {"refused":>8s} {"phases":>7s}  state')
    for arm, names in (("treat", args.treat), ("control", args.control)):
        for name in names:
            r = got["rows"][name]
            state = "finished" if r["finished"] else "running"
            print(f'{name:10s} {arm:>9s} {r["executed"]:9d} {r["refused"]:8d} {r["spans"]:7d}  '
                  f'{state}')
    if got["contrast"] is None:
        print(f'\nno contrast to report yet: {got["treat_n"]} finished treated probe(s) and '
              f'{got["control_n"]} finished control(s). A running probe\'s count is a LOWER BOUND, '
              "and the treated ones stop at their cap while the controls are still climbing -- "
              "comparing the two mid-flight measures the clock, not the intervention.")
        if got["running"]:
            print("  still running: " + ", ".join(got["running"]))
        return 0
    print(f'\nmedian executed over FINISHED probes: treat {got["treat_median"]} '
          f'(n={got["treat_n"]}), control {got["control_median"]} (n={got["control_n"]}), '
          f'contrast {got["contrast"]:+g}')
    if got["running"]:
        print("  still running (not counted): " + ", ".join(got["running"]))
    if got["contrast"] <= 0:
        print("  NO CONTRAST: the control did not out-probe the treatment in the probes that "
              "FINISHED, so nothing separates the arms so far")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
