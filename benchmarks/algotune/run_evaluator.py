#!/usr/bin/env python3
"""Run AlgoTune's ``scripts/evaluate_results.py`` with a PERSISTENT baseline cache.

WHY THIS EXISTS — the parity trap, in its second and worse form
---------------------------------------------------------------
``AlgoTuner.utils.evaluator.baseline_manager.BaselineManager`` caches the reference ("oracle")
timings in ``self._cache``, i.e. **in process memory**. Inside an AlgoTuner run that is enough:
one process lives for the whole task, so the reference pass is paid once and every later ``eval``
command is cheap.

``looplab_eval.py`` scores a candidate by shelling out to ``evaluate_results.py``, which builds a
fresh ``BaselineManager`` in a fresh interpreter — so that cache starts empty **every node** and
the whole reference pass is re-measured from scratch. Measured on this box (RTX 5090, 2026-08-19),
that pass is ~2.4 s per instance over the task's test set: minutes per node, paid by one arm and
not the other, growing linearly with node count.

``looplab_eval.py``'s own ``.baseline_cache.json`` does NOT fix this. It caches the aggregate
*number* so every node is scored against one stable denominator; the subprocess still measures the
whole thing before handing that number over. Caching the value fixed the metric. This fixes the
cost.

WHAT IT DOES
------------
Installs a disk-backed cache around ``BaselineManager.get_baseline_times`` and then runs the
upstream script unmodified through ``runpy``. First call measures and writes; later calls load.
The cache is keyed by ``(task, subset)`` and stored beside the bridge, so a ``git clean`` of the
AlgoTune checkout cannot silently discard it mid-campaign.

WHY THIS IS PARITY RESTORATION AND NOT AN ADVANTAGE
---------------------------------------------------
It makes the out-of-process bridge pay what the in-process reference loop pays: **once**. It does
not make the candidate faster, does not touch the solver timing (always measured fresh, every
node), and does not change the ratio — the same reference numbers are used, just not re-derived.

It IS a departure from a naive reading of the harness, so it must be stated in any methods note
alongside published numbers. ``--no-baseline-cache`` on ``looplab_eval.py`` turns it off.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It never caches across TASKS and never across the train/test SUBSET split: those are different
reference sets, and one standing in for another would silently corrupt every speedup computed from
it. ``force_regenerate=True`` is honoured and bypasses the cache, because a caller asking for a
re-measurement is asking for exactly that.
"""
from __future__ import annotations

import json
import logging
import os
import runpy
import sys
from pathlib import Path

_LOG = logging.getLogger("looplab.algotune.baseline_cache")


def _cache_path(root: Path, task: str, subset: str) -> Path:
    return root / f"{task}__{subset}.json"


def install(cache_dir: Path, task: str) -> None:
    """Wrap ``BaselineManager.get_baseline_times`` with a disk cache under ``cache_dir``."""
    from AlgoTuner.utils.evaluator.baseline_manager import BaselineManager

    cache_dir.mkdir(parents=True, exist_ok=True)
    original = BaselineManager.get_baseline_times

    def cached(self, subset, force_regenerate=False, test_mode=False, max_samples=None):
        # A caller that explicitly asks to regenerate gets a real measurement, and a bounded
        # (`max_samples`) or `test_mode` request is a DIFFERENT reference set from the full one --
        # serving either from a cache written for the other is the corruption this must not commit.
        if force_regenerate or test_mode or max_samples is not None:
            return original(self, subset, force_regenerate, test_mode, max_samples)

        path = _cache_path(cache_dir, task, str(subset))
        if path.exists():
            try:
                times = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(times, dict) and times:
                    _LOG.info("baseline cache HIT %s (%d entries)", path.name, len(times))
                    # Seed the in-process cache too, so a second call in this same interpreter
                    # behaves exactly as upstream does.
                    self._cache[subset] = times
                    return times
            except (OSError, ValueError) as exc:
                _LOG.warning("baseline cache unreadable (%s); re-measuring", exc)

        times = original(self, subset, force_regenerate, test_mode, max_samples)
        # Only a COMPLETE set is worth persisting: upstream retries until the count matches the
        # dataset exactly, and caching a partial set would make every later node score against a
        # denominator drawn from a different number of instances.
        if isinstance(times, dict) and times:
            tmp = path.with_suffix(".json.tmp")
            try:
                tmp.write_text(json.dumps(times), encoding="utf-8")
                os.replace(tmp, path)
                _LOG.info("baseline cache WRITE %s (%d entries)", path.name, len(times))
            except OSError as exc:
                _LOG.warning("could not persist baseline cache (%s); measurement still valid", exc)
        return times

    BaselineManager.get_baseline_times = cached


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: run_evaluator.py <cache-dir> <task> <evaluator.py> [args...]",
              file=sys.stderr)
        return 2
    cache_dir, task, evaluator = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    install(cache_dir, task)
    # Hand the upstream script exactly the argv it expects, then run it as __main__ so its own
    # `if __name__ == "__main__"` entry point fires. Nothing else about it is modified.
    sys.argv = [evaluator] + sys.argv[4:]
    runpy.run_path(evaluator, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
