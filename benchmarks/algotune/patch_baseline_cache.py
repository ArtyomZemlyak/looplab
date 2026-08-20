#!/usr/bin/env python3
"""Give AlgoTune's ``BaselineManager`` a PERSISTENT baseline cache, by patching it on disk.

WHY ON DISK, AND NOT IN PROCESS
-------------------------------
The first version of this wrapped ``BaselineManager.get_baseline_times`` in the parent process and
then ran ``scripts/evaluate_results.py`` through ``runpy``. It broke every evaluation:

    ERROR:root:Evaluation failed for discrete_log/BV4: A process in the process pool was
    terminated abruptly while the future was running or pending.

Measured 2026-08-19, decisively: the same task, same config, same machine —
``evaluate_results.py`` run **directly** returned ``0.9963x``; run through the in-process wrapper it
crashed the pool. Importing AlgoTuner in the parent before the script creates its worker pool is
enough to do it. So the wrapper is gone and the change lives where it cannot affect process setup:
in the file itself, applied before anything imports it.

This matches what this arm already does for the upstream ``sys.modules`` bug (see the README) —
AlgoTune's own pins have rotted and local patches are the working recipe here.

WHAT IT CHANGES
---------------
``get_baseline_times(subset)`` consults ``<cache-dir>/<task>__<subset>.json`` before measuring, and
writes it after. Nothing else moves.

WHY THAT IS PARITY RESTORATION, NOT AN ADVANTAGE
------------------------------------------------
Inside an AlgoTuner run the reference pass is paid **once** — ``self._cache`` holds it for the life
of the process. ``looplab_eval.py`` shells out per candidate, so every node got a fresh interpreter
and re-measured the whole thing: ~15 minutes per node, paid by one arm and not the other, scaling
with node count. This makes the out-of-process bridge pay what the in-process loop pays: once. The
SOLVER is still timed fresh on every node and the ratio is unchanged.

It never caches across TASKS or across the train/test SUBSET split — those are different reference
sets and one standing in for another would corrupt every speedup computed from it — and
``force_regenerate=True`` still measures.

USAGE
-----
    python patch_baseline_cache.py --algotune-root /path/to/AlgoTune [--cache-dir DIR] [--revert]

Idempotent: re-running is a no-op, and ``--revert`` restores the ``.orig`` backup.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

_NO_KEY_CLASS = """class _LooplabNoCacheKey(Exception):
    \"\"\"No task name to key the baseline cache on -- measure, never share a key.\"\"\"


"""

MARKER = "# --- LOOPLAB PERSISTENT BASELINE CACHE (benchmarks/algotune/patch_baseline_cache.py) ---"

ANCHOR = """        with self._lock:
            # Check cache
            if not force_regenerate and self._cache[subset] is not None:"""

PATCH = '''        {marker}
        # A DISK cache in front of the in-process one. See that module's docstring for why this is
        # parity restoration (the in-process loop pays the reference pass once; an out-of-process
        # bridge otherwise re-pays it per node) and why it is applied here rather than by wrapping
        # the method from a parent process (doing that crashed the evaluation pool).
        #
        # Fail-OPEN in both directions: an unreadable cache re-measures, and an unwritable one still
        # returns a correct measurement. This must never be able to turn a good run into a bad one.
        _ll_cache_dir = {cache_dir!r}
        _ll_key = None
        if not force_regenerate and not test_mode and max_samples is None:
            try:
                import json as _ll_json, os as _ll_os
                _ll_task = getattr(getattr(self, "task_instance", None), "task_name", None)
                # FAIL CLOSED on an unknown task name. The fallback used to be the literal "task",
                # which collapses every such task onto ONE cache file -- so task B's reference
                # timings become the denominator of task A's speedup, silently, with the log
                # printing "cache HIT". That is exactly the cross-task reuse this module's docstring
                # forbids, and the only safe answer is to skip the cache and measure.
                if not _ll_task:
                    raise _LooplabNoCacheKey()
                _ll_key = _ll_os.path.join(_ll_cache_dir, f"{{_ll_task}}__{{subset}}.json")
                if _ll_os.path.exists(_ll_key):
                    with open(_ll_key, "r", encoding="utf-8") as _ll_fh:
                        _ll_times = _ll_json.load(_ll_fh)
                    if isinstance(_ll_times, dict) and _ll_times:
                        logging.info("LOOPLAB baseline cache HIT %s (%d entries)",
                                     _ll_key, len(_ll_times))
                        self._cache[subset] = _ll_times
                        return _ll_times
            except _LooplabNoCacheKey:
                logging.info("LOOPLAB baseline cache SKIPPED: no task name to key on")
                _ll_key = None
            except Exception as _ll_exc:            # noqa: BLE001 - a cache miss must never fail a run
                logging.warning("LOOPLAB baseline cache unreadable (%s); re-measuring", _ll_exc)
                _ll_key = None

        with self._lock:
            # Check cache
            if not force_regenerate and self._cache[subset] is not None:'''

WRITE_ANCHOR = """                    self._cache[subset] = baseline_times
                    logging.info(f"Successfully generated all {actual_count} baseline times")"""

WRITE_PATCH = '''                    self._cache[subset] = baseline_times
                    # Persist ONLY a complete set: upstream retries until the count matches the
                    # dataset exactly, and a partial one would make later nodes score against a
                    # denominator drawn from a different number of instances.
                    if _ll_key:
                        try:
                            import json as _ll_json2, os as _ll_os2
                            _ll_os2.makedirs(_ll_os2.path.dirname(_ll_key), exist_ok=True)
                            _ll_tmp = _ll_key + ".tmp"
                            with open(_ll_tmp, "w", encoding="utf-8") as _ll_fh2:
                                _ll_json2.dump(baseline_times, _ll_fh2)
                            _ll_os2.replace(_ll_tmp, _ll_key)
                            logging.info("LOOPLAB baseline cache WRITE %s (%d entries)",
                                         _ll_key, len(baseline_times))
                        except Exception as _ll_exc2:   # noqa: BLE001 - the measurement still stands
                            logging.warning("LOOPLAB baseline cache not persisted (%s)", _ll_exc2)
                    logging.info(f"Successfully generated all {actual_count} baseline times")'''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path)
    ap.add_argument("--cache-dir", type=Path,
                    default=Path(__file__).resolve().parent / ".baseline_times",
                    help="Where the per-(task, subset) reference timings are kept.")
    ap.add_argument("--revert", action="store_true", help="Restore the .orig backup and exit.")
    args = ap.parse_args()

    target = (args.algotune_root.resolve()
              / "AlgoTuner" / "utils" / "evaluator" / "baseline_manager.py")
    backup = target.with_suffix(".py.orig")
    if not target.exists():
        raise SystemExit(f"no baseline_manager.py at {target}")

    if args.revert:
        if backup.exists():
            shutil.copy2(backup, target)
            print(f"reverted {target} from {backup.name}")
        else:
            print("nothing to revert (no .orig backup)")
        return 0

    source = target.read_text(encoding="utf-8")
    if MARKER in source:
        print("already patched (idempotent no-op)")
        return 0
    if ANCHOR not in source or WRITE_ANCHOR not in source:
        raise SystemExit("upstream baseline_manager.py has changed shape; re-derive the anchors "
                         "rather than force-patching it")

    if not backup.exists():
        shutil.copy2(target, backup)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    patched = source.replace(
        ANCHOR, PATCH.format(marker=MARKER, cache_dir=str(args.cache_dir)), 1)
    patched = patched.replace("class BaselineManager", _NO_KEY_CLASS + "class BaselineManager", 1)
    patched = patched.replace(WRITE_ANCHOR, WRITE_PATCH, 1)
    target.write_text(patched, encoding="utf-8")
    print(f"patched {target}\n  cache dir: {args.cache_dir}\n  backup:    {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
