#!/usr/bin/env python3
"""Whether the arm's LABEL and the arm's LANE are the same variable.

WHY. §266 found, at batch 9 of 12, that 17 of 18 treated probes had run on lanes 0-10,48-58 and
11-21,59-69 and 18 of 19 controls on 22-32,70-80 and 33-43,81-91. §190's test permutes LABELS within
a batch, which is valid only if the four probes in a batch are exchangeable -- and if one label
always lands on the same pair of lanes, "treated" and "ran on lane A or B" are one variable wearing
two names. Nothing checked it for nine batches because nothing was looking.

Six sittings of the reference-against-itself ruler could not show the lanes differ (per-sitting
contrast positive in 4 of 6, sign test p = 0.34) and could not exclude a ~3 % lane effect either, so
the confound was never demonstrably harmful. That is not the point. The point is that it went nine
batches unmeasured, and the check costs one file read per probe.

WHAT IT DELIBERATELY DOES NOT READ is any score, any metric, any champion. Membership comes from the
`BATCHES` list in `arm_readout.py` -- parsed, not imported, so nothing in that module runs -- and the
lane comes from the probe's own `INSTRUMENT.txt`. Both are facts about the ASSIGNMENT, fixed before
a probe makes its first call, so this is readable at any time without touching §190's embargo.

Usage:
    lane_balance.py [--root DIR] [--readout FILE] [--share 0.9] [--min-probes 4]
"""
from __future__ import annotations

import argparse
import ast
import collections
import re
import sys
from pathlib import Path

DEFAULT_ROOT = "/var/tmp/looplab-bench/model-probes"
DEFAULT_READOUT = str(Path(__file__).resolve().parent / "arm_readout.py")
LANE = re.compile(r"^lane:\s+(\S+)", re.M)


def batches(readout_path: str) -> list:
    """(treat, control) per batch, PARSED out of `arm_readout.py` rather than imported.

    Importing would run that module, and that module's job is reading the outcome. Membership is a
    list of names; taking it by AST keeps this tool unable to reach anything else in there even by
    accident.
    """
    tree = ast.parse(Path(readout_path).read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(getattr(t, "id", "") == "BATCHES" for t in node.targets):
            continue
        out = []
        for pair in node.value.elts:
            treat, control = ([ast.literal_eval(x) for x in side.elts] for side in pair.elts)
            out.append((treat, control))
        return out
    return []


def lane_of(root: str, name: str):
    """The lane a probe actually ran on, from the instrument it wrote at launch."""
    try:
        text = (Path(root) / name / "INSTRUMENT.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    got = LANE.search(text)
    return got.group(1) if got else None


def table(root: str, batch_list) -> collections.Counter:
    counts: collections.Counter = collections.Counter()
    for treat, control in batch_list:
        for label, names in (("treat", treat), ("control", control)):
            for name in names:
                lane = lane_of(root, name)
                if lane:
                    counts[(label, lane)] += 1
    return counts


def imbalance(counts: collections.Counter, share: float = 0.9, min_probes: int = 4) -> list:
    """Lanes that carry one label almost exclusively, as sentences.

    A lane with one or two probes on it says nothing -- early batches and crossovers both look like
    that -- so `min_probes` keeps a thin lane from raising an alarm it cannot support.
    """
    said = []
    lanes = sorted({lane for _, lane in counts})
    for lane in lanes:
        by_label = {label: counts[(label, lane)] for label in ("treat", "control")}
        total = sum(by_label.values())
        if total < min_probes:
            continue
        top, n = max(by_label.items(), key=lambda kv: kv[1])
        if n / total >= share:
            said.append(f"lane {lane}: {n} of {total} probes are {top} "
                        f"({100 * n / total:.0f} %) -- on this lane the label is not a free variable")
    return said


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--readout", default=DEFAULT_READOUT)
    ap.add_argument("--share", type=float, default=0.9)
    ap.add_argument("--min-probes", type=int, default=4)
    args = ap.parse_args(argv)

    batch_list = batches(args.readout)
    if not batch_list:
        print(f"no BATCHES list in {args.readout}", file=sys.stderr)
        return 2
    counts = table(args.root, batch_list)
    if not counts:
        print("no probe on any batch has an INSTRUMENT.txt to read a lane from", file=sys.stderr)
        return 2
    lanes = sorted({lane for _, lane in counts})
    print(f'{"lane":14s} {"treat":>6s} {"control":>8s}')
    for lane in lanes:
        print(f'{lane:14s} {counts[("treat", lane)]:6d} {counts[("control", lane)]:8d}')
    bad = imbalance(counts, args.share, args.min_probes)
    for line in bad:
        print(f"  CONFOUNDED: {line}")
    if bad:
        print("  §190 permutes the LABEL within a batch; that is only a test of the label if the "
              "lanes are exchangeable. Cross the mapping in the remaining batches -- it costs "
              "nothing and turns an unmeasurable confound into an estimable one.")
    else:
        print(f"  no lane carries one label at {100 * args.share:.0f} % or more "
              f"over {sum(counts.values())} assigned probe(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
