"""`python -m looplab.judgebench` — build the dataset, or score a candidate against it.

    python -m looplab.judgebench extract runs/*             -o tests/data/judge_bench/train_monitor.v1.jsonl.gz
    python -m looplab.judgebench score                       # the incumbent replaying itself
    python -m looplab.judgebench score --answers cand.jsonl  # a candidate's captured answers, offline

Deliberately not a `looplab` subcommand (see `looplab/judgebench/__init__.py`). `extract` reads `runs/`
and writes ONLY the output file; `score` makes no network call at all.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from looplab.judgebench.judge_corpus import (
    DEFAULT_DATASET, build_dataset, read_dataset, write_dataset)
from looplab.judgebench.score import (
    attempt_totals, format_report, jsonl_candidate, per_attempt_report, recorded_candidate,
    score_dataset)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m looplab.judgebench", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    extract = sub.add_parser("extract", help="rebuild the dataset from run directories")
    extract.add_argument("run_dirs", nargs="+", type=Path)
    extract.add_argument("-o", "--out", type=Path, default=DEFAULT_DATASET)

    scorer = sub.add_parser("score", help="score a candidate offline")
    scorer.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    scorer.add_argument("--answers", type=Path, default=None,
                        help="JSONL of {case_id,status}; omit to replay the recorded verdict")
    scorer.add_argument("--stage", default=None, help="only decisions that watched this stage")
    scorer.add_argument("--tools", choices=("with", "without"), default=None,
                        help="only decisions the judge had (or lacked) log tools for")
    scorer.add_argument("--exclude-basis", default=None,
                        help="comma-separated label_basis values to drop, e.g. "
                             "'stage_failed' to cut the failures the engine decided over artifacts "
                             "AFTER the stage exited, which the judged log could not show")

    args = parser.parse_args(argv)
    if args.cmd == "extract":
        dataset = build_dataset(args.run_dirs)
        write_dataset(dataset, args.out)
        print("%s rows from %d runs -> %s (%d bytes)"
              % (dataset["header"]["rows"], len(dataset["header"]["sources"]), args.out,
                 args.out.stat().st_size))
        for source in dataset["header"]["sources"]:
            print("  %-32s %d" % (source["run"], source["rows"]))
        return 0

    dataset = read_dataset(args.dataset)
    rows = dataset["rows"]
    if args.stage:
        rows = [r for r in rows if (r.get("context") or {}).get("stage") == args.stage]
    if args.tools:
        want = args.tools == "with"
        rows = [r for r in rows
                if bool((r.get("provenance") or {}).get("tools_available")) is want]
    if args.exclude_basis:
        drop = {s.strip() for s in args.exclude_basis.split(",") if s.strip()}
        rows = [r for r in rows
                if not any((r.get("label") or {}).get("label_basis", "").startswith(d)
                           for d in drop)]
    candidate = recorded_candidate if args.answers is None else jsonl_candidate(args.answers)
    name = "recorded (incumbent)" if args.answers is None else str(args.answers)
    report = score_dataset(rows, candidate, name=name)
    sys.stdout.write(format_report(report, per_attempt_report(rows, candidate)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
