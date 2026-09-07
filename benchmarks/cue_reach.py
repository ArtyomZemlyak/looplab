#!/usr/bin/env python3
"""How far a prompt cue actually reaches, per phase, measured on resolved prompts.

WHY THIS EXISTS. Twice now a sweep has answered "does the money cue reach `plan_step`?" by
grepping `attributes.input` and got 31.7 % when the true figure is 99.3 %. `input` is a SUFFIX
whenever `input_from` is set (`core/tracing.py`), so a phase whose prompts are chained looks blind
no matter how well it is served. `benchmarks/algotune/span_input.py` exists to prevent exactly this
and was written after the same mistake in 2026-08-28; having the fix in a library did not stop the
mistake being made again by hand. This file is the fix with a command line on it.

Usage:
    cue_reach.py <run-or-probe-dir>... [--pattern REGEX] [--naive]

`--naive` additionally prints the number a reader who stops at `attributes.input` would get, so
the gap between the two is on the page rather than in someone's memory.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "algotune"))
import span_input  # noqa: E402

# The three wordings the loop uses for the same fact. `Spend guidance` is
# `engine/proposal_cues.py::_cue_llm_budget`; the two `BUDGET: $` lines are
# `adapters/repo_developer.py::_REPO_DEV_BUDGET_LINE` and
# `agents/deep_research.py::_RESEARCH_BUDGET_LINE`. A pattern that names only the first one reports
# the Developer and the Researcher as blind when they are not -- that is a SECOND way to get this
# wrong, independent of the truncation, and it is why the default is a union.
MONEY = r"Spend guidance|BUDGET: \$[0-9]"


def spans_of(root: pathlib.Path):
    """Every `spans.jsonl` under `root`, whether it is a run dir, a probe dir or a tree of them."""
    if root.is_file():
        return [root]
    direct = root / "spans.jsonl"
    if direct.exists():
        return [direct]
    return sorted(root.rglob("spans.jsonl"))


def reach(paths, pattern: str, naive: bool = False):
    """(rows, totals) -- per-phase span count, hits, cost, and the naive hit count beside it."""
    pat = re.compile(pattern)
    tot = collections.Counter()
    hit = collections.Counter()
    cost = collections.Counter()
    blind = collections.Counter()
    naive_hit = collections.Counter()
    for path in paths:
        spans, by_id = span_input.load(str(path))
        for span in spans:
            if span.get("kind") != "generation":
                continue
            attrs = span.get("attributes") or {}
            phase = attrs.get("phase") or "?"
            try:
                usd = float(attrs.get("cost") or 0.0)
            except (TypeError, ValueError):
                usd = 0.0
            tot[phase] += 1
            cost[phase] += usd
            text = "\n".join(str(m.get("content"))
                             for m in span_input.resolve(span, by_id) if isinstance(m, dict))
            if pat.search(text):
                hit[phase] += 1
            else:
                blind[phase] += usd
            if naive:
                own = "\n".join(str(m.get("content"))
                                for m in (attrs.get("input") or []) if isinstance(m, dict))
                if pat.search(own):
                    naive_hit[phase] += 1
    rows = [(p, tot[p], hit[p], cost[p], naive_hit[p]) for p in sorted(cost, key=lambda k: -cost[k])]
    return rows, (sum(cost.values()), sum(blind.values()))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("roots", nargs="+", type=pathlib.Path)
    ap.add_argument("--pattern", default=MONEY, help="regex to look for (default: the money cue)")
    ap.add_argument("--naive", action="store_true",
                    help="also print the truncated-read figure, to show the gap")
    args = ap.parse_args(argv)

    paths = [p for root in args.roots for p in spans_of(root)]
    if not paths:
        print("no spans.jsonl under any of the given roots", file=sys.stderr)
        return 2
    rows, (grand, blind) = reach(paths, args.pattern, naive=args.naive)
    head = f'{"phase":22s} {"spans":>6s} {"sees":>6s} {"%":>6s} {"cost":>9s} {"share":>6s}'
    print(f"{len(paths)} span log(s), pattern /{args.pattern}/")
    print(head + ("   naive%" if args.naive else ""))
    for phase, n, h, usd, nh in rows:
        line = (f"{phase:22s} {n:6d} {h:6d} {100 * h / n:5.1f}% "
                f"${usd:8.4f} {100 * usd / grand if grand else 0:5.1f}%")
        if args.naive:
            line += f"   {100 * nh / n:5.1f}%"
        print(line)
    print(f"\nblind spend ${blind:.4f} of ${grand:.4f} = "
          f"{100 * blind / grand if grand else 0:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
