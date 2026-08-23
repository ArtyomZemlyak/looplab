#!/usr/bin/env python3
"""Let ``scripts/evaluate_results.py`` score on the TRAIN split, so the LoopLab arm stops
optimising against the set it will be graded on.

THE DEFECT
----------
``evaluate_results.py`` hardcodes ``subset="test"`` / ``data_subset="test"`` at three sites and
loads only the test half of ``load_dataset()``. The LoopLab arm scores **every node** through that
script, so every experiment it ran was measured on the test set — while AlgoTuner's own agent
iterates on **train** (``dev_runs``) and touches test only for its final number.

Two consequences, both in LoopLab's favour and neither one asked for:

* **A train/test leak.** One arm optimises directly against the graded split; the other does not.
  Any speedup advantage that produced would be an artefact of the wiring, which is the exact class
  of thing this whole comparison exists to exclude.
* **A 3.7x cost asymmetry.** Arm A's loop runs ``1 warmup + dev_runs(2) = 3`` solver executions per
  instance; arm B's ran ``1 + runs(10) = 11``. (That second number comes from ``benchmark.runs``,
  which is absent upstream and defaults to 10 — NOT from ``eval_runs``, and NOT from the
  ``--num-runs`` flag, which only writes an environment name nothing reads.)

WHAT THIS PATCH DOES
--------------------
Adds an ``ALGOTUNE_EVAL_SUBSET`` environment variable, default ``"test"`` — so upstream behaviour
is unchanged unless something sets it. When it is ``"train"``, the script loads the train half and
passes ``subset="train"`` to the baseline manager and ``data_subset="train"`` to the evaluator.

The campaign then mirrors AlgoTuner exactly: the LoopLab arm evaluates each node on **train**, and
the champion is scored once on **test** at the end.

WHY AN ENVIRONMENT VARIABLE AND NOT A FLAG
-------------------------------------------
The bridge already shells out to this script; an env var needs no argparse plumbing threaded to
three call sites, and it cannot be silently dropped by a caller that forgets to forward an argument.
The default keeps the upstream contract, so a plain ``python scripts/evaluate_results.py`` still
scores on test exactly as before.

USAGE
-----
    python patch_eval_subset.py --algotune-root /path/to/AlgoTune [--revert]

Idempotent; keeps a ``.orig`` backup.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

MARKER = "# --- LOOPLAB EVAL SUBSET (benchmarks/algotune/patch_eval_subset.py) ---"

HELPER = '''

''' + MARKER + '''
# Which dataset half this script scores on. Default "test" keeps the upstream contract exactly;
# the campaign sets "train" for per-node evaluation so the LoopLab arm iterates on the same split
# AlgoTuner's own agent iterates on, and scores the champion on test once at the end.
def _looplab_eval_subset() -> str:
    import os
    value = (os.environ.get("ALGOTUNE_EVAL_SUBSET") or "test").strip().lower()
    return value if value in ("train", "test") else "test"
'''

DATASET_ANCHOR = """                _, test_iter = task_instance.load_dataset()
                test_problems = list(test_iter)"""

DATASET_PATCH = """                _ll_train_iter, _ll_test_iter = task_instance.load_dataset()
                _ll_subset = _looplab_eval_subset()
                test_iter = _ll_train_iter if _ll_subset == "train" else _ll_test_iter
                test_problems = list(test_iter)
                # WARNING and not INFO, and that is the whole point of the line. This function runs
                # in a `ProcessPoolExecutor` child started under `forkserver`; `setup_logging` never
                # runs there, so its root logger falls through to `logging.lastResort`, which prints
                # WARNING and above to the inherited stderr and DROPS INFO. Measured on a real 458 s
                # evaluator run (`tests/fixtures/algotune_eval_invalid_results_stderr.txt`, 104 KB):
                # zero occurrences of this line at INFO, and zero of the `test problems for` INFO
                # four lines below it, while the PARENT's INFO lines are all present.
                # It is the only channel on which the thing that ACTUALLY chose the split can say
                # which one it chose -- `looplab_eval.py::subset_actually_scored` reads it, and
                # without it the bridge can only infer the split from this marker being in the file.
                logging.warning(f"LOOPLAB scoring on the {_ll_subset!r} split "
                                f"({len(test_problems)} problems)")"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path)
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()

    target = args.algotune_root.resolve() / "scripts" / "evaluate_results.py"
    backup = target.with_suffix(".py.orig")
    if not target.exists():
        raise SystemExit(f"no evaluate_results.py at {target}")

    if args.revert:
        if backup.exists():
            shutil.copy2(backup, target)
            print(f"reverted {target.name}")
        else:
            print("nothing to revert")
        return 0

    source = target.read_text(encoding="utf-8")
    if MARKER in source:
        print("already patched (idempotent no-op)")
        return 0
    if DATASET_ANCHOR not in source:
        raise SystemExit("upstream evaluate_results.py has changed shape; re-derive the anchors")

    if not backup.exists():
        shutil.copy2(target, backup)

    patched = source.replace(DATASET_ANCHOR, DATASET_PATCH, 1)
    # The two baseline lookups and the evaluator call must follow the same split, or the numerator
    # and denominator would come from different halves of the dataset.
    patched = patched.replace('subset="test",\n', 'subset=_looplab_eval_subset(),\n')
    patched = patched.replace('subset="test", force_regenerate=False, test_mode=False',
                              'subset=_looplab_eval_subset(), force_regenerate=False, test_mode=False')
    patched = patched.replace('data_subset="test",', 'data_subset=_looplab_eval_subset(),')
    if 'subset="test"' in patched or 'data_subset="test"' in patched:
        raise SystemExit("a hardcoded subset survived the patch; refusing a half-applied change")

    # Define the helper after the imports so every call site can see it.
    lines = patched.splitlines(keepends=True)
    last_import = max(i for i, l in enumerate(lines)
                      if l.startswith("import ") or l.startswith("from "))
    lines.insert(last_import + 1, HELPER)
    target.write_text("".join(lines), encoding="utf-8")
    print(f"patched {target}\n  backup: {backup.name}\n  set ALGOTUNE_EVAL_SUBSET=train to score on train")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
