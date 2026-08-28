#!/usr/bin/env python3
"""Cheap per-instance CORRECTNESS check for the developer's writing sessions.

WHY. The score is zero unless every instance validates, and the writing sessions were checking the
wrong thing. Measured over three independent kcenters probes on 2026-08-28, of the agent's own
`run_probe` self-checks:

    probe        probes   compared to reference solve()   called is_solution()   timed something
    dsKcCtl        55                 3                           1                    23
    dsFBKc         42                 3                           1                    13
    fxKcenters     55                 3                           4                    28

It times things because timing is cheap and validating was not: the only correctness-checking
command pinned on the card is `eval_train`, a ~40 s full pass. dsKcCtl's node 1 is what that costs
-- it knowingly traded exactness for speed ("the decision oracle is monotone in r, so we can replace
most probes"), self-checked 55 times, and the engine's evaluation was the FIRST thing to say
`Solution is not optimal. Found value: 33.955, Optimal value: 33.408`. The node scored 0.0 and its
work was discarded, which at 2-4 nodes per run is a quarter of the budget.

The reference agent does not have this hole: `reference [1,2,3]` and `eval_input [1,2,3]` are
first-class commands, so its model never writes a validation harness by hand.

WHAT THIS IS NOT. It is not the ruler. It generates its OWN instances from the reference task's
generator and reports validity only -- no speedup, no baseline, nothing that could be confused with
`engine/evaluate.py`'s number. Timing that appears here is wall-clock convenience, not a score.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

_REQUIRED = ("generate_problem", "solve", "is_solution")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"looplab_check: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def find_task(mod) -> Any:
    """The reference task, found by DUCK TYPING rather than `isinstance(..., Task)`.

    The arena's own base class is not importable everywhere this runs (and importing it would drag
    the whole harness in), so the rule is the three methods that matter. Deterministic: sorted by
    name, so a module holding two candidates always yields the same one.
    """
    best = None
    for attr in sorted(dir(mod)):
        obj = getattr(mod, attr)
        if not isinstance(obj, type):
            continue
        if all(callable(getattr(obj, m, None)) for m in _REQUIRED):
            if best is None or attr < best[0]:
                best = (attr, obj)
    if best is None:
        raise SystemExit("looplab_check: no class with generate_problem/solve/is_solution in the reference")
    return best[1]


def check(reference: Path, solver: Path, n: int, size: int, seed: int) -> dict:
    task_cls = find_task(_load_module(reference, "_looplab_reference"))
    try:
        task = task_cls()
    except TypeError:
        task = task_cls(n=size)

    solver_mod = _load_module(solver, "_looplab_candidate")
    solver_cls = getattr(solver_mod, "Solver", None)
    if solver_cls is None:
        return {"ok": False, "error": f"{solver.name} defines no `Solver` class"}
    instance = solver_cls()

    rows = []
    for i in range(n):
        problem = task.generate_problem(size, random_seed=seed + i)
        # `is_solution` states its rejection through `logging.error`; capture it so the agent is told
        # WHY and not merely that something was wrong.
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.ERROR)
        root = logging.getLogger()
        root.addHandler(handler)
        prev = root.level
        root.setLevel(logging.ERROR)
        t0 = time.perf_counter()
        try:
            answer = instance.solve(problem)
            elapsed = time.perf_counter() - t0
            valid = bool(task.is_solution(problem, answer))
            row = {"instance": i, "seed": seed + i, "valid": valid, "seconds": round(elapsed, 4)}
        except Exception as exc:  # noqa: BLE001 — a candidate that raises is a FAILED instance, not a crashed check
            row = {"instance": i, "seed": seed + i, "valid": False,
                   "seconds": round(time.perf_counter() - t0, 4),
                   "raised": f"{type(exc).__name__}: {exc}"[:300]}
        finally:
            root.removeHandler(handler)
            root.setLevel(prev)
        why = " ".join(buf.getvalue().split())[:300]
        if why and not row["valid"]:
            row["reason"] = why
        rows.append(row)

    n_valid = sum(1 for r in rows if r["valid"])
    return {"ok": n_valid == len(rows), "instances": len(rows), "valid": n_valid,
            "invalid": len(rows) - n_valid, "rows": rows,
            "note": ("ALL VALID on these instances. This is a correctness check on freshly generated "
                     "instances, NOT the score -- the run's number still comes from the engine's "
                     "evaluation." if n_valid == len(rows) else
                     "INVALID INSTANCES PRESENT. The score is 0 unless every instance validates, so "
                     "fix this before optimising further.")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Per-instance correctness check against the reference task.")
    ap.add_argument("--reference", required=True, type=Path)
    ap.add_argument("--solver", default=Path("solver.py"), type=Path)
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--size", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    if not args.reference.exists():
        print(json.dumps({"ok": False, "error": f"reference not found: {args.reference}"}))
        return 1
    if not args.solver.exists():
        print(json.dumps({"ok": False, "error": f"solver not found: {args.solver}"}))
        return 1
    print(json.dumps(check(args.reference, args.solver, args.n, args.size, args.seed), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
