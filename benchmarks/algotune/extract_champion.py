#!/usr/bin/env python3
"""Write out the champion node's ``solver.py`` from a LoopLab run, for final scoring on TEST.

Read from the FOLD (`events.jsonl` -> `RunState`), never from a node workdir on disk. Two reasons,
and both have bitten this repo before:

* the fold is what DECIDES which node is champion, so a second reading of "what did this run
  produce" is how a report comes to disagree with the product;
* a repo-task run's on-disk layout is not a contract — an evaluation can rewrite a workdir, and a
  path guessed from a directory listing is a guess even when it happens to work.

Used by the campaign so the LoopLab arm mirrors AlgoTuner exactly: every node is evaluated on the
TRAIN split, and the champion is scored on TEST once, at the end.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path, help="Where to write the champion solver.")
    ap.add_argument("--filename", default="solver.py",
                    help="The file to extract from the champion's committed working set.")
    args = ap.parse_args()

    log = args.run_dir / "events.jsonl"
    if not log.exists():
        print(f"no event log at {args.run_dir}", file=sys.stderr)
        return 1
    try:
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        # `EventStore(path)` takes the LOG FILE, not the run directory. Passing the directory
        # raised IsADirectoryError, the broad `except` below turned it into "no champion", and
        # every task in the arm scored `speedup: null` -- a plumbing break wearing the costume of
        # a legitimate empty result. Same shape as the `_find_result` key bug this bridge already
        # had once: the check one line up already knew the filename and the call did not use it.
        state = fold(EventStore(str(log)).read_all())
    except OSError as exc:
        # A run whose log cannot be READ is a broken BRIDGE, not a run without a champion, and it
        # must not be reported as one. Only a fold-level failure below is "no champion".
        print(f"cannot read the event log at {log}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - a broken run is "no champion", not a crash
        print(f"could not fold {args.run_dir}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    best = state.best()
    if best is None:
        print("run has no champion", file=sys.stderr)
        return 1
    files = getattr(best, "files", None) or {}
    # A repo node's committed working set is keyed by repo-relative path; accept a suffix match so
    # a multi-editable layout (`<name>/solver.py`) resolves too.
    content = files.get(args.filename)
    if content is None:
        matches = [v for k, v in files.items() if str(k).replace("\\", "/").endswith(args.filename)]
        if len(matches) == 1:
            content = matches[0]
        elif len(matches) > 1:
            print(f"ambiguous: {len(matches)} files end with {args.filename}", file=sys.stderr)
            return 1
    if content is None:
        print(f"champion node {best.id} committed no {args.filename} "
              f"(has: {sorted(files)[:8]})", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(content, encoding="utf-8")
    print(f"champion node {best.id} (metric={best.metric}) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
