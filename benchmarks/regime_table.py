#!/usr/bin/env python3
"""§108's table, computed instead of read: what each IMPLEMENTATION REGIME scored, per task.

    regime_table.py [PROBE_ROOT] [--min-nodes 10] [--json]

WHY IT IS A FILE. §108 grouped `edge_expansion`'s nodes by hand into Cython / numba / pure Python
and found a six-fold median gap; §110 then corrected how far that sentence reaches. Both were hand
counts over a corpus that has since quadrupled, and the sentence they support -- "the score is very
nearly one binary choice" -- is the premise under docs/60 §60.9 B2. A premise that expensive should
be one command, not a memory of an afternoon.

The classifier is `looplab/engine/regime_contrast.py`, the same one a run uses on itself, so the
archive reading and the run-time reading can never drift into two different definitions of "wrote a
kernel".

IT READS THE EVENT LOG DIRECTLY and does not fold. `node_created` carries the files, `node_evaluated`
carries the metric, and folding 161 runs to join two payload types would be slower and no truer --
the fold's other work (status, terminals, cards) decides nothing here.

`BENCH_ROOT` points it at a live stand; the default is the persistent archive, because that is the
corpus that survives a container restart (doc 56 §414-§416).
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from looplab.engine.regime_contrast import REGIMES, contrast, node_regime  # noqa: E402

DEFAULT_ROOT = (os.environ.get("BENCH_ROOT")
                or "/home/jovyan/data/looplab-bench/runs-archive") + "/model-probes"


def read_probe(path: str) -> tuple:
    """`(task_id, [(regime, metric), ...])` for one probe's event log."""
    files_by_id: dict = {}
    scored: list = []
    task = ""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind, data = event.get("type"), (event.get("data") or {})
            if kind == "run_started":
                task = str(data.get("task_id") or data.get("task") or "")
            elif kind == "node_created":
                files = data.get("files")
                if isinstance(files, dict):
                    files_by_id[data.get("node_id", data.get("id"))] = files
            elif kind == "node_evaluated":
                metric = data.get("metric")
                if isinstance(metric, (int, float)):
                    scored.append((data.get("node_id", data.get("id")), float(metric)))
    return task, [(node_regime(files_by_id.get(nid) or {}), m) for nid, m in scored]


def collect(root: str) -> tuple:
    by_task: dict = collections.defaultdict(list)
    probes = 0
    for path in sorted(glob.glob(f"{root}/*/runs/*/run/events.jsonl")):
        probes += 1
        task, rows = read_probe(path)
        by_task[task].extend(rows)
    return probes, by_task


def render(probes: int, by_task: dict, min_nodes: int) -> str:
    out = [f"{probes} probe(s) under the root; tasks with at least {min_nodes} evaluated nodes:"]
    for task, rows in sorted(by_task.items(), key=lambda kv: -len(kv[1])):
        if len(rows) < min_nodes:
            continue
        got = contrast(rows)
        out.append("")
        out.append(f"{task}  ({len(rows)} evaluated nodes)")
        out.append(f"  {'regime':<10} {'n':>5} {'median':>10} {'max':>10} {'min':>10}")
        for regime in REGIMES:
            stats = (got or {}).get("regimes", {}).get(regime)
            if not stats:
                continue
            out.append(f"  {regime:<10} {stats['n']:>5} {stats['median']:>10.2f} "
                       f"{stats['max']:>10.2f} {stats['min']:>10.2f}")
        if got:
            ratio = "n/a (the worst median is not positive)" if got["ratio"] is None \
                else f"{got['ratio']:.2f}x"
            out.append(f"  best {got['best']} over worst {got['worst']}: {ratio}")
        else:
            # A TASK WITH ONE REGIME IS THE INTERESTING CASE, not a gap in the table: it is a task
            # where the loop never tried the alternative, which is the question B2 is about.
            only = sorted({r for r, _m in rows})
            out.append(f"  one regime only ({', '.join(only)}) -- nothing was compared here")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", nargs="?", default=DEFAULT_ROOT)
    ap.add_argument("--min-nodes", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    probes, by_task = collect(args.root)
    if args.json:
        print(json.dumps({"probes": probes, "root": args.root,
                          "tasks": {t: contrast(r) for t, r in by_task.items()
                                    if len(r) >= args.min_nodes}}, indent=1))
        return 0
    print(render(probes, by_task, args.min_nodes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
