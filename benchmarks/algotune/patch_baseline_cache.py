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

# THE CACHE-FILE NAME NOW HAS ONE AUTHORITY, and until 2026-09-01 it had none.
#
# This template named files `<task>__<subset>.json` (serial) or `__w{W}x{C}` (parallel) while every
# reader expected `__lane{N}r3` / `__w{W}x{C}r3`: `looplab_eval.py::eval_regime` builds that key and
# `_regime_mismatch` refuses any regime not ending in it, `check_leaks.sh` section 3 counts every
# file WITHOUT `r3` as a stale leak, and both plot scripts glob for it. On a checkout built by
# `setup_algotune.sh` (which applies THIS patch and not the parallel one) that was not a cosmetic
# split: serial cache files were invisible to `_regime_mismatch`'s two-underscore `__*` glob, so the
# guard was inert; and with `ALGOTUNE_EVAL_WORKERS>1` the first eval wrote `__wNxM.json` and EVERY
# later eval was refused `baseline_regime_mismatch` -- a whole campaign of `speedup: null` -- with a
# remedy no setting could satisfy. `campaign.sh::declare_baseline_ruler` is what made that live: it
# exports `ALGOTUNE_BASELINE_CACHE_DIR` (the condition `_regime_mismatch` returns early on) and
# `ALGOTUNE_EVAL_WORKERS=auto` (the `>1` half), the two preconditions together.
#
# The emitted key below is now the reader's key, character for character, and the lane width is in
# it. Keep them that way: the name is defined here and READ in `looplab_eval.py::eval_regime`,
# `check_leaks.sh` and the two plot scripts, so a change to the scheme is a change to all of them
# AND an invalidation of every reference on disk.
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
        # READ FROM THE ENVIRONMENT, defaulting to the path this script was pointed at. The first
        # version baked the path in, and the file DEPLOYED on this box does not -- somebody
        # improved the arena's copy without updating the generator, and re-running this script
        # would have silently undone it. Found 2026-08-31 by diffing the two.
        _ll_cache_dir = os.environ.get(
            'ALGOTUNE_BASELINE_CACHE_DIR',
            {cache_dir!r})
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
                # The REGIME belongs in the key. A baseline measured while nothing else ran is
                # not the same number as one measured while 24 other instances were being timed,
                # and the speedup only cancels the difference when BOTH halves come from the same
                # regime. Sharing one cache file across regimes silently divides an in-regime
                # solver time by an out-of-regime reference -- measured on this box as a swing
                # from 1.46x to 1.04x on the identical solver.
                try:
                    from AlgoTuner.utils.evaluator import looplab_parallel as _ll_par
                    _ll_w, _ll_c = _ll_par.resolve_workers()
                except Exception:
                    _ll_w, _ll_c = 1, 1
                # THE LANE WIDTH IS PART OF THE MEASUREMENT, and the serial key used to hide
                # it. At `workers <= 1` the pool is bypassed entirely: solver and reference both
                # run in the LANE's whole cpuset, so a reference taken by a 22-core lane and
                # reused by an 8-core one puts numerator and denominator on different machines.
                #
                # `r3`, not `r2`: from 2026-08-24 a lane is built out of WHOLE PHYSICAL CORES
                # (sibling pairs) rather than a contiguous CPU-number range, which changed which
                # silicon a lane owns -- so every reference taken before it is from another
                # instrument and must never be reused.
                #
                # Both were present in the DEPLOYED patch and absent here. Re-running the old
                # generator would have renamed every key on disk (`__w22x1r3` -> `__w22x1`),
                # making the whole existing ruler unreachable and silently re-measuring a new one.
                try:
                    _ll_lane = len(os.sched_getaffinity(0))
                except (AttributeError, OSError):
                    _ll_lane = 0
                _ll_regime = (f"__lane{{_ll_lane}}r3" if _ll_w <= 1
                              else f"__w{{_ll_w}}x{{_ll_c}}r3")
                _ll_key = _ll_os.path.join(
                    _ll_cache_dir, f"{{_ll_task}}__{{subset}}{{_ll_regime}}.json")
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
                    # ONLY WRITE INTO A CACHE SOMEBODY DELIBERATELY POINTED AT -- the same
                    # rule `looplab_eval.py::_regime_mismatch` already applies to REFUSING a run,
                    # and the two sides disagreed. That guard returns None (off) when neither
                    # ALGOTUNE_BASELINE_CACHE_DIR nor --baseline-times-dir was given, on the good
                    # ground that it must not police a directory nobody asked about. The write
                    # side had no such restraint and defaulted straight into the live ruler.
                    #
                    # So an invocation with the variable unset escaped the guard AND minted a new
                    # regime in the repo's own `.baseline_times`. Measured 2026-08-31: a
                    # reference-against-itself diagnostic run without the variable added
                    # `edge_expansion__train__lane22r3.json` and `pde_heat1d__train__lane22r3.json`
                    # beside the campaign's `__w22x1r3` set -- a SECOND ruler, 28.2 ms against
                    # 44.6 ms on the same instances, waiting for the next run at workers <= 1.
                    # Reads still fall back to the default; only minting does not.
                    if _ll_key and not os.environ.get('ALGOTUNE_BASELINE_CACHE_DIR'):
                        logging.warning(
                            "LOOPLAB baseline cache NOT WRITTEN: no ALGOTUNE_BASELINE_CACHE_DIR, "
                            "so this run may not mint a ruler in %s", _ll_cache_dir)
                        _ll_key = None
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
