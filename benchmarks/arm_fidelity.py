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
import json
import statistics
import sys

DEFAULT_ROOT = "/var/tmp/looplab-bench/model-probes"


def probe_calls(root: str, name: str) -> dict:
    """`{executed, refused, spans}` for one probe tree. No scores are read."""
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
    return {"executed": executed, "refused": refused, "spans": len(spans)}


def report(root: str, treat, control) -> dict:
    rows = {n: probe_calls(root, n) for n in list(treat) + list(control)}
    t = [rows[n]["executed"] for n in treat if rows[n]["executed"] or rows[n]["refused"]]
    c = [rows[n]["executed"] for n in control if rows[n]["executed"] or rows[n]["refused"]]
    return {"rows": rows,
            "treat_median": statistics.median(t) if t else 0,
            "control_median": statistics.median(c) if c else 0,
            "contrast": (statistics.median(c) if c else 0) - (statistics.median(t) if t else 0)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--treat", nargs="+", required=True)
    ap.add_argument("--control", nargs="+", required=True)
    args = ap.parse_args(argv)

    got = report(args.root, args.treat, args.control)
    print(f'{"probe":10s} {"arm":>9s} {"executed":>9s} {"refused":>8s} {"phases":>7s}')
    for name in args.treat:
        r = got["rows"][name]
        print(f'{name:10s} {"treat":>9s} {r["executed"]:9d} {r["refused"]:8d} {r["spans"]:7d}')
    for name in args.control:
        r = got["rows"][name]
        print(f'{name:10s} {"control":>9s} {r["executed"]:9d} {r["refused"]:8d} {r["spans"]:7d}')
    print(f'\nmedian executed: treat {got["treat_median"]}, control {got["control_median"]}, '
          f'contrast {got["contrast"]:+g}')
    if got["contrast"] <= 0:
        print("  NO CONTRAST YET: the control has not out-probed the treatment, so nothing "
              "separates the arms so far")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
