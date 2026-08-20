"""`python -m looplab.judgebench` — build the dataset, or score a candidate against it.

    python -m looplab.judgebench extract runs/*             -o tests/data/judge_bench/train_monitor.v1.jsonl.gz
    python -m looplab.judgebench score                       # the incumbent replaying itself
    python -m looplab.judgebench score --answers cand.jsonl  # a candidate's captured answers, offline

The FAILURE-CLASSIFIER bench (`engine/triage.py::_failure_reason` and whatever replaces it) is the
same two verbs with a `triage-` prefix, over its own dataset and its own labels:

    python -m looplab.judgebench extract-triage runs/*     -o tests/data/judge_bench/failure_triage.v1.jsonl.gz
    python -m looplab.judgebench score-triage              # the reason each run actually recorded
    python -m looplab.judgebench score-triage --arm head   # `_failure_reason` replayed at HEAD
    python -m looplab.judgebench score-triage --answers cand.jsonl

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
from looplab.judgebench import triage_corpus, triage_score


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

    tri_extract = sub.add_parser("extract-triage",
                                 help="rebuild the FAILURE-CLASSIFIER dataset from run directories")
    tri_extract.add_argument("run_dirs", nargs="+", type=Path)
    tri_extract.add_argument("-o", "--out", type=Path, default=triage_corpus.DEFAULT_DATASET)

    tri_score = sub.add_parser("score-triage", help="score a failure classifier offline")
    tri_score.add_argument("--dataset", type=Path, default=triage_corpus.DEFAULT_DATASET)
    tri_score.add_argument("--arm", choices=("recorded", "head", "head-widened"),
                           default="recorded",
                           help="'recorded' is the reason each run actually recorded (zero "
                                "reconstruction); 'head' replays `_failure_reason` today over the "
                                "durable stderr tail; 'head-widened' gives that replay the triage "
                                "agent's own log reads as well, which is what isolates how much of "
                                "the marker rule's win is the WINDOW rather than the rule")
    tri_score.add_argument("--answers", type=Path, default=None,
                           help="JSONL of {case_id,reason}; overrides --arm")
    tri_score.add_argument("--run", default=None, help="only rows from this run")
    tri_score.add_argument("--high-confidence-only", action="store_true",
                           help="score only rows whose label rests on a high-confidence basis")

    args = parser.parse_args(argv)
    if args.cmd == "extract-triage":
        dataset = triage_corpus.build_dataset(args.run_dirs)
        triage_corpus.write_dataset(dataset, args.out)
        head = dataset["header"]
        print("%s rows (%s labelled, %s unlabelled) from %d runs -> %s (%d bytes)"
              % (head["rows"], head["labelled"], head["unlabelled"], len(head["sources"]),
                 args.out, args.out.stat().st_size))
        for source in head["sources"]:
            print("  %-32s %3d rows  %3d labelled" % (source["run"], source["rows"],
                                                      source["labelled"]))
        return 0

    if args.cmd == "score-triage":
        dataset = triage_corpus.read_dataset(args.dataset)
        rows = dataset["rows"]
        if args.run:
            rows = [r for r in rows if r["provenance"]["run"] == args.run]
        if args.answers is not None:
            candidate, name = triage_score.jsonl_candidate(args.answers), str(args.answers)
        elif args.arm == "recorded":
            candidate, name = triage_score.recorded_candidate, "recorded (the incumbent as it ran)"
        else:
            widened = args.arm == "head-widened"
            candidate = triage_score.head_replay_candidate(widened=widened)
            name = "_failure_reason at HEAD (%s stderr)" % ("widened" if widened else "durable tail")
        report = triage_score.score_dataset(
            rows, candidate, name=name, high_confidence_only=args.high_confidence_only)
        sys.stdout.write(triage_score.format_report(
            report, limits=dataset["header"].get("limits", "")))
        return 0

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
