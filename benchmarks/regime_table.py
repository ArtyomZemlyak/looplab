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

IT READS THE EVENT LOG THROUGH `events_read.iter_events` and does not fold. `node_created` carries
the files, `node_evaluated` carries the metric, and folding 161 runs to join two payload types would
be slower and no truer -- the fold's other work (status, terminals, cards) decides nothing here.

THE SHARED READER IS NOT A STYLE RULE. A line is not an event: a PACKET row carries a list of them
inside its payload, and a naive `for line in open(...)` sees the packet and none of its contents.
The first version of this file had exactly that loop and `tests/test_a_line_is_not_an_event_-
everywhere.py` caught it -- the guard exists because the same omission has shipped before, and its
cost is silent undercounting rather than an error.

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import events_read  # noqa: E402  (the shared reader: a line is not an event)
from looplab.engine.regime_contrast import REGIMES, contrast, node_regime  # noqa: E402

DEFAULT_ROOT = (os.environ.get("BENCH_ROOT")
                or "/home/jovyan/data/looplab-bench/runs-archive") + "/model-probes"


def read_probe(path: str) -> tuple:
    """`(task_id, [(regime, metric), ...])` for one probe's event log."""
    files_by_id: dict = {}
    scored: list = []
    task = ""
    for event in events_read.iter_events(path):
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


def seed_rows(root: str) -> list:
    """One ledger row per archived probe -- what each of those runs WOULD have written at finalize.

    WHY SEEDING IS PART OF THE TOOL AND NOT A SCRIPT SOMEBODY REMEMBERS (docs/audit/prior-injection-
    hit-rate.md). The citation audit of 2026-09-18 read 161 probes, 439 proposals and 32 prior
    injections, and found ZERO (prior, proposal) pairs -- not because the priors were ignored but
    because every one of them fired EMPTY: `rows: 0`, since each probe read its own fresh memory
    dir. `regime_prior` reads `<memory_dir>/regime_contrast.jsonl`, written at finalize, so on a
    fresh stand its first sentence would be "none yet" and the treatment of doc 60 60.9 B2 would be
    empty for exactly that reason -- the same shape as doc 56 422, where B1's floor sat above what
    87 % of runs produce.

    The seed is DERIVED, never invented: each row is `contrast()` over one archived probe's own
    nodes, stamped with that probe's task and run, which is byte-for-byte the shape
    `engine/regime_contrast.py::run_contrast` emits. A row is skipped when that probe tried one
    regime only -- the same refusal the live path makes, for the same reason.
    """
    out = []
    for path in sorted(glob.glob(f"{root}/*/runs/*/run/events.jsonl")):
        task, rows = read_probe(path)
        if not task or not rows:
            continue
        stamp = {"task_id": task, "direction": "max",
                 "run_id": path.split("/model-probes/")[1].split("/")[0], "seeded_from": path}
        got = contrast(rows)
        if got is not None:
            out.append({**got, **stamp})
            continue
        # A ONE-REGIME PROBE STILL SAYS WHAT IT MEASURED -- the same rule the live writer follows
        # (`engine/regime_contrast.py::run_contrast`), and the reason is in its comment: dropping
        # these rows loses `pde_heat1d` almost entirely and REVERSES `discrete_log`.
        by: dict = {}
        for regime, metric in rows:
            if regime in REGIMES and isinstance(metric, (int, float)):
                by.setdefault(regime, []).append(float(metric))
        if by:
            out.append({"regimes": {r: {"n": len(v), "median": round(statistics.median(v), 6),
                                        "max": round(max(v), 6), "min": round(min(v), 6)}
                                    for r, v in sorted(by.items())},
                        "nodes": sum(len(v) for v in by.values()), **stamp})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", nargs="?", default=DEFAULT_ROOT)
    ap.add_argument("--min-nodes", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--seed-ledger", metavar="PATH",
                    help="append one `regime_contrast.jsonl` row per archived probe to PATH, so an "
                         "arm's first probe reads a prior that has something true to say")
    args = ap.parse_args(argv)
    if args.seed_ledger:
        rows = seed_rows(args.root)
        # APPEND, and say how many. Not truncate: the ledger is shared and a live run may already
        # have written to it, and a seed that silently replaced a real measurement would be worse
        # than no seed. `seeded_from` marks every row this path wrote, so a reader can tell the
        # corpus's history from this arm's own runs.
        with open(args.seed_ledger, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        tasks = sorted({r["task_id"] for r in rows})
        print(f"seeded {len(rows)} row(s) from {len(tasks)} task(s) into {args.seed_ledger}")
        for t in tasks:
            n = sum(1 for r in rows if r["task_id"] == t)
            print(f"  {t:<32} {n:>3} run(s)")
        return 0
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
