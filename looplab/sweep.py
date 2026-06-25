"""Intra-node sweep runtime (stdlib only).

A *sweep* node runs many configurations in ONE process — load the data once, warm the GPU once,
and evaluate every grid point — then reports them all back in a single `node_evaluated` event.
This helper is the prompted default for the Developer's generated code:

    from looplab.sweep import run_sweep

    def train(params, seed):
        # ... build/fit a model with these params + seed, return a metric ...
        return validation_score          # or {"metric": score, "auc": ..., "size": ...}

    run_sweep({"lr": [0.1, 0.01, 0.001], "depth": [3, 5]}, train, direction="max")

It prints a final ``{"trials": [...]}`` line that the sandbox parses (`_json_line_trials`). A
bring-your-own-library script (Optuna / GridSearchCV / joblib) doesn't need this helper — it only
needs to emit that same JSON line. Everything here is dependency-free.

Determinism (replay-critical): the grid is enumerated via ``itertools.product`` over SORTED keys,
so trial order is stable across runs; each trial's seed is derived from ``LOOPLAB_EVAL_SEED`` (the
env var the engine injects, which the confirm phase varies) so a re-run — or a different confirm
seed — is reproducible.
"""
from __future__ import annotations

import itertools
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

__all__ = ["enumerate_grid", "run_sweep"]


def enumerate_grid(space: dict[str, list]) -> list[dict]:
    """Cartesian product of a discrete grid {name: [values...]} as a list of param dicts, in a
    deterministic order (keys sorted, values in the given order). Empty space -> one empty config
    (a single default run), so callers never special-case the no-grid case."""
    if not space:
        return [{}]
    keys = sorted(space)
    combos = itertools.product(*(space[k] for k in keys))
    return [dict(zip(keys, vals)) for vals in combos]


def _base_seed(seed: int) -> int:
    """The sweep's base seed: honor LOOPLAB_EVAL_SEED (engine-injected, varied by the confirm
    phase) so multi-seed confirmation flows through to every trial; fall back to the caller's seed."""
    raw = os.environ.get("LOOPLAB_EVAL_SEED")
    if raw is None:
        return int(seed)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(seed)


def _normalize(result: Any) -> tuple[Optional[float], dict]:
    """train_fn may return a bare metric or a dict {"metric": x, **extras}. -> (metric, extras)."""
    if isinstance(result, dict):
        metric = result.get("metric")
        extras = {k: v for k, v in result.items() if k != "metric"}
        return (None if metric is None else float(metric)), extras
    return (None if result is None else float(result)), {}


def run_sweep(
    space: dict[str, list],
    train_fn: Callable[[dict, int], Any],
    *,
    n_jobs: int = 1,
    max_trials: Optional[int] = None,
    seed: int = 0,
    direction: str = "min",
    emit: bool = True,
) -> list[dict]:
    """Evaluate every point of a discrete grid in-process and report the trials.

    space:     {name: [values...]} discrete grid (cartesian product, deterministic order).
               Random search = pass a pre-sampled space of singleton lists (one code path).
    train_fn:  (params, seed) -> metric | {"metric": metric, **extra_metrics}. Exceptions are
               caught per-trial (that trial gets metric=None + the error message) so one bad
               config never sinks the whole sweep.
    n_jobs:    1 = sequential (default). >1 uses a thread pool — appropriate for CPU/sklearn
               grids that release the GIL or for I/O-bound work. GPU sweeps should stay n_jobs=1:
               the win there is amortizing the one-time data load / imports / device warm-up, and
               the device serializes the actual compute anyway. (Process-pool parallelism is out
               of scope for v1 — it would discard the shared in-process state that is the point.)
    max_trials: truncate the grid to at most this many points (a per-node trial cap).
    seed:      fallback base seed when LOOPLAB_EVAL_SEED is unset.
    direction: "min" | "max" — accepted for caller convenience but intentionally NOT used to
               reorder trials: emitted order stays the deterministic grid order (replay-critical,
               per the module docstring), and the engine independently recomputes the node's
               best metric regardless of order.
    emit:      print the final `{"trials": [...]}` line (set False to only get the return value).

    Returns the list of trial dicts (also printed when emit=True).
    """
    base = _base_seed(seed)
    configs = enumerate_grid(space)
    if max_trials is not None and max_trials >= 0:
        configs = configs[:max_trials]

    def _one(i_params: tuple[int, dict]) -> dict:
        i, params = i_params
        trial_seed = base * 1_000_003 + i
        t0 = time.perf_counter()
        try:
            metric, extras = _normalize(train_fn(params, trial_seed))
            err = ""
        except Exception as exc:  # isolate: a failed trial must not abort the sweep
            metric, extras, err = None, {}, f"{type(exc).__name__}: {exc}"
        return {
            "params": params,
            "metric": metric,
            "seconds": round(time.perf_counter() - t0, 4),
            "extra_metrics": extras,
            "error": err,
        }

    indexed = list(enumerate(configs))
    if n_jobs and n_jobs > 1 and len(indexed) > 1:
        with ThreadPoolExecutor(max_workers=n_jobs) as pool:
            # map preserves input order, so the result list stays in deterministic grid order
            trials = list(pool.map(_one, indexed))
    else:
        trials = [_one(ip) for ip in indexed]

    if emit:
        # Final line the sandbox scans for. Keep it last and on its own line.
        print(json.dumps({"trials": trials}))
    return trials
