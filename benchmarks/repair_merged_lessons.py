#!/usr/bin/env python3
"""Split lesson rows that a fixed parser would never have written as one row.

WHY THIS EXISTS. `engine/memory.py::_INLINE_PAIR` splits an LLM's numbered verdicts (`P1 ... P2
... P3 ...`) into separate lessons. Before 6f6a0a1c it did not split when several arrived on ONE
line, so a distillation glued three verdicts into a single 861-character statement. The parser is
fixed; the rows it already wrote are not, and a store is append-only -- nothing re-reads and
re-splits them.

That merged row is not merely long. `tests/test_lesson_prior_render_budget.py` renders every
statement in the live store and asserts none arrives as a "redacted preview"; the 861-character row
fails it, and has failed it on every sweep since 2026-08-30. A permanent red trains a reader to
skim the file where a real regression would appear.

THE SPLIT IS THE SHIPPED ONE, IMPORTED. `_INLINE_PAIR` is the same regex the live parser uses, so
this tool cannot disagree with it -- a second spelling of the rule is how a repair comes to produce
rows the parser would not have. Every other field is copied unchanged: `fingerprint` is a token
list of the TASK (`memory.py::task_fingerprint`), not of the statement, and `evidence`/`evidence_sig`
describe the run the three verdicts came from together.

The rewrite is atomic (`os.replace`) and refuses to run without a backup, because this store is
SHARED -- the row repaired here belongs to another project.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from looplab.engine.memory import _INLINE_PAIR  # noqa: E402  the SHIPPED splitter, not a copy


def split_statement(statement: str) -> list[str]:
    """The parts the live parser would have made of this statement. One part = nothing to do."""
    parts = [p.strip() for p in _INLINE_PAIR.split(statement or "")]
    return [p for p in parts if p]


def repair(lines: list[str]) -> tuple[list[str], list[tuple[int, list[int]]]]:
    """Return (new_lines, report). `report` is (line index, part lengths) per row that was split."""
    out: list[str] = []
    report: list[tuple[int, list[int]]] = []
    for i, line in enumerate(lines):
        if not line.strip():
            out.append(line)
            continue
        try:
            row = json.loads(line)
        except ValueError:
            out.append(line)            # a torn line is not this tool's business
            continue
        if not isinstance(row, dict) or not isinstance(row.get("statement"), str):
            out.append(line)
            continue
        parts = split_statement(row["statement"])
        if len(parts) < 2:
            out.append(line)
            continue
        report.append((i, [len(p) for p in parts]))
        for part in parts:
            out.append(json.dumps({**row, "statement": part}, ensure_ascii=False))
    return out, report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", type=Path,
                    default=Path("/home/jovyan/data/looplab-memory/lessons.jsonl"))
    ap.add_argument("--backup", type=Path, default=None,
                    help="Where to copy the store before rewriting (default: <store>.premerge).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not args.store.exists():
        print(f"store not found: {args.store}", file=sys.stderr)
        return 1
    lines = args.store.read_text(encoding="utf-8").splitlines()
    new, report = repair(lines)
    for idx, lens in report:
        print(f"line {idx}: split into {len(lens)} statements {lens}")
    if not report:
        print("nothing to split")
        return 0
    print(f"{len(lines)} rows -> {len(new)}")
    if args.dry_run:
        return 0

    backup = args.backup or args.store.with_suffix(args.store.suffix + ".premerge")
    shutil.copy2(args.store, backup)
    print(f"backup: {backup}")
    tmp = args.store.with_suffix(args.store.suffix + ".tmp")
    tmp.write_text("\n".join(new) + "\n", encoding="utf-8")
    os.replace(tmp, args.store)         # atomic: a reader sees one store or the other, never half
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
