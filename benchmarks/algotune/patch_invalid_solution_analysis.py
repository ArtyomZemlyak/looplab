#!/usr/bin/env python3
"""Carry AlgoTune's per-instance ``invalid_solution_analysis`` out of the evaluator and into the
summary file the LoopLab bridge reads, so a wrong solver is told WHY and not only HOW OFTEN.

THE DEFECT
----------
When a candidate is wrong on some instances the harness refuses to score it and says so::

    Speedup N/A due to invalid results: 94/100 valid (94.0%)

AlgoTune builds much more than that sentence. ``ResultAggregator._extract_invalid_contexts``
formats, for up to three rejected instances, the CODE CONTEXT of the ``is_solution`` line that
rejected the solution, and ``AlgoTuner/utils/message_writer.py:726-750`` shows those three to
**AlgoTuner's own agent**. Arm A sees them. Arm B could not, for two independent reasons:

1. ``AlgoTuner/utils/evaluator/main.py`` gates the attachment on an argument that has nothing to do
   with it::

       if baseline_manager and all_invalid_analyses:
           attributed_results.invalid_solution_analysis = all_invalid_analyses[:3]

   ``baseline_manager`` selects where the REFERENCE TIMINGS come from (three uses, all of them in
   the first thirty lines of the function). ``scripts/evaluate_results.py`` resolves its baseline
   times itself and passes ``baseline_times=`` rather than ``baseline_manager=`` — so the argument
   is ``None``, the analysis is dropped on the floor, and the ``all_invalid_analyses`` list that was
   accumulated across every chunk is discarded unread.

2. Even attached, it had nowhere to go. ``update_single_result`` writes a summary whose entire
   payload per (task, model) is ``{"final_speedup": "<str>"}`` and drops everything that explains
   it. That file is the bridge's only structured channel (see ``looplab_eval.py::_find_result``).

The cost is measured and it is not small. ``tests/test_algotune_bridge_says_why.py`` records the
incident: arm B's agent was handed ``0.0`` three times for a solver that was correct on 95 of 100
instances, concluded "replicating the reference is ANSWERED and FAILED", and spent the rest of a
$1.00 budget on that conclusion. The bridge now recovers the aggregate verdict and the workers'
``ERROR:`` lines off stderr — but the CODE CONTEXT, the thing that names the failing check in the
reference's own source, stopped at the evaluator.

WHAT THIS PATCH CHANGES
-----------------------
``AlgoTuner/utils/evaluator/main.py``
    ``if baseline_manager and all_invalid_analyses:`` -> ``if all_invalid_analyses:``. The cap of
    three is upstream's and is left alone.

``scripts/evaluate_results.py``
    * ``EvaluationResult`` gains ``invalid_solution_analysis: list[str]`` (default empty), so the
      value survives the ``ProcessPoolExecutor`` hop back to the parent that writes the summary.
    * after ``evaluate_code_on_dataset`` returns, the attribute is read off the result list.
    * ``update_single_result`` writes it beside ``final_speedup`` when it is non-empty.

ADDITIVE AND OPTIONAL, BOTH DELIBERATE. ``final_speedup`` keeps its exact shape and type, and the
new key is ABSENT rather than empty when there is nothing to say — so every existing reader of
``evaluate_summary.json`` (ours reaches it through ``_find_result``, which keys on the speedup
fields) sees byte-identical output for a run that scored, and an unpatched checkout is simply a
checkout whose summaries never carry the key. Nothing downstream may require it.

WHY ON DISK RATHER THAN IN PROCESS
----------------------------------
The same reason as ``patch_baseline_cache.py``, and it was measured rather than assumed: importing
AlgoTuner in the parent process before ``evaluate_results.py`` builds its worker pool crashes every
evaluation ("A process in the process pool was terminated abruptly", 2026-08-19), so the bridge
invokes the script as a subprocess and can only change its behaviour by changing the file. The
analysis is built inside a ``ProcessPoolExecutor`` child under ``forkserver``, three module
boundaries away from anything the bridge can monkeypatch.

WHY NOT PASS ``baseline_manager=`` INSTEAD
------------------------------------------
That would also satisfy the gate, and it would change the measurement: ``evaluate_code_on_dataset``
prefers a passed manager and calls ``get_baseline_times()`` on it AGAIN, with its own arguments.
``evaluate_results.py`` has already resolved the timings for the configured subset and hands them
over as ``baseline_times=``. Re-deriving the denominator to unlock a diagnostic string is not a
trade this campaign may make. Deleting the gate moves no number at all.

USAGE
-----
    python patch_invalid_solution_analysis.py --algotune-root /path/to/AlgoTune [--revert]

Idempotent; keeps ``.analysis.orig`` backups. The backup suffix is NOT the plain ``.orig`` that
``patch_eval_subset.py`` uses on the same file: that one holds pristine upstream, and reverting this
patch through it would silently undo the train/test split patch as well — which is the leak
``patch_eval_subset.py``'s docstring calls "the exact class of thing this whole comparison exists to
exclude". Each patch reverts to the state it found, so the two are order-independent in both
directions.
"""
from __future__ import annotations

import argparse
import ast
import shutil
import sys
from pathlib import Path

MARKER = ("# --- LOOPLAB INVALID SOLUTION ANALYSIS "
          "(benchmarks/algotune/patch_invalid_solution_analysis.py) ---")

# ------------------------------------------------------------------------------------------------
# 1. AlgoTuner/utils/evaluator/main.py -- the gate
# ------------------------------------------------------------------------------------------------
MAIN_ANCHOR = """    # When using new architecture, attach all accumulated invalid solution analyses
    if baseline_manager and all_invalid_analyses:"""

MAIN_PATCH = """    """ + MARKER + """
    # `baseline_manager` chose where the REFERENCE TIMINGS come from, thirty lines above; it says
    # nothing about whether the rejections are worth reporting. `scripts/evaluate_results.py`
    # resolves the timings itself and passes `baseline_times=`, so under that caller this argument
    # is None and every accumulated analysis was discarded -- the whole reason arm B's bridge could
    # only ever report HOW OFTEN a solver was wrong and never WHY.
    # When using new architecture, attach all accumulated invalid solution analyses
    if all_invalid_analyses:"""

# ------------------------------------------------------------------------------------------------
# 2. scripts/evaluate_results.py -- carry it to the summary
# ------------------------------------------------------------------------------------------------
IMPORT_ANCHOR = "from dataclasses import dataclass\n"
IMPORT_PATCH = "from dataclasses import dataclass, field\n"

RESULT_ANCHOR = """    error_message: str | None = None
    compilation_needed: bool = False
    compilation_success: bool = True
"""

RESULT_PATCH = """    error_message: str | None = None
    compilation_needed: bool = False
    compilation_success: bool = True
    """ + MARKER + """
    # The formatted `is_solution` code contexts for up to three rejected instances, exactly the
    # list AlgoTuner's own agent is shown (`message_writer.py:726-750`). A FIELD and not a global,
    # because `evaluate_single_task` runs in a `ProcessPoolExecutor` child and this dataclass is
    # the only thing that comes back to the parent that writes the summary.
    invalid_solution_analysis: list[str] = field(default_factory=list)
"""

CAPTURE_ANCHOR = """                # Step 6: Extract speedups from results (same format as agent)
                logging.info(
                    f"Results type: {type(results)}, has aggregate_metrics: {hasattr(results, 'aggregate_metrics')}"
                )"""

CAPTURE_PATCH = """                """ + MARKER + """
                # Read it BEFORE the aggregation below, which may `break` out on a critical error
                # and return early. `getattr` and not an attribute access: `results` is an
                # `AttributedList` when the run produced results and a plain dict on the
                # early-exit error paths, and neither shape is required to carry this.
                _ll_analysis = getattr(results, "invalid_solution_analysis", None)
                if _ll_analysis:
                    result.invalid_solution_analysis = [str(_ll_ctx) for _ll_ctx in _ll_analysis]
                    logging.info(
                        f"LOOPLAB carrying {len(result.invalid_solution_analysis)} invalid "
                        f"solution analysis entries into the summary"
                    )

                # Step 6: Extract speedups from results (same format as agent)
                logging.info(
                    f"Results type: {type(results)}, has aggregate_metrics: {hasattr(results, 'aggregate_metrics')}"
                )"""

SUMMARY_ANCHOR = """                summary_data[result.task_name][result.display_model_name] = {
                    "final_speedup": speedup_str
                }"""

SUMMARY_PATCH = """                """ + MARKER + """
                # ADDITIVE and OPTIONAL. `final_speedup` keeps its exact key, shape and string
                # type, and the analysis key is absent rather than empty when there is nothing to
                # say -- so a run that scored writes a byte-identical summary and no reader may
                # come to require the new key.
                _ll_row = {"final_speedup": speedup_str}
                _ll_analysis = getattr(result, "invalid_solution_analysis", None)
                if _ll_analysis:
                    _ll_row["invalid_solution_analysis"] = list(_ll_analysis)
                summary_data[result.task_name][result.display_model_name] = _ll_row"""


def _apply(target: Path, edits: list[tuple[str, str]]) -> bool:
    """Apply anchor->patch edits to one file, backing it up first. Returns True if it changed."""
    backup = target.with_suffix(target.suffix + ".analysis.orig")
    if not target.exists():
        raise SystemExit(f"not an AlgoTune checkout: {target} missing")

    source = target.read_text(encoding="utf-8")
    if MARKER in source:
        print(f"   already patched: {target.name} (idempotent no-op)")
        return False

    patched = source
    for anchor, replacement in edits:
        if anchor not in patched:
            # LOUD, and before anything is written. `setup_algotune.sh` step 2 records what the
            # alternative costs: a patch that silently did not match printed nothing an operator
            # would read as a failure and left a 6.5x slowdown in place on a fresh machine.
            raise SystemExit(
                f"   FAILED: upstream {target.name} has changed shape; re-derive the anchor:\n"
                f"   {anchor.splitlines()[-1].strip()!r}")
        patched = patched.replace(anchor, replacement, 1)

    if patched == source:
        raise SystemExit(f"   FAILED: nothing matched in {target.name}")

    try:
        ast.parse(patched)
    except SyntaxError as exc:
        raise SystemExit(f"   FAILED: patched {target.name} does not parse ({exc})")

    if not backup.exists():
        shutil.copy2(target, backup)
    target.write_text(patched, encoding="utf-8")
    print(f"   patched {target}\n     backup: {backup.name}")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path)
    ap.add_argument("--revert", action="store_true",
                    help="Restore the .analysis.orig backups and exit.")
    args = ap.parse_args(argv)

    root = args.algotune_root.resolve()
    evaluator_main = root / "AlgoTuner" / "utils" / "evaluator" / "main.py"
    eval_script = root / "scripts" / "evaluate_results.py"

    if args.revert:
        for target in (evaluator_main, eval_script):
            backup = target.with_suffix(target.suffix + ".analysis.orig")
            if backup.exists():
                shutil.copy2(backup, target)
                print(f"   restored {target.name} from {backup.name}")
            else:
                print(f"   nothing to revert for {target.name}")
        return 0

    _apply(evaluator_main, [(MAIN_ANCHOR, MAIN_PATCH)])
    _apply(eval_script, [
        (IMPORT_ANCHOR, IMPORT_PATCH),
        (RESULT_ANCHOR, RESULT_PATCH),
        (CAPTURE_ANCHOR, CAPTURE_PATCH),
        (SUMMARY_ANCHOR, SUMMARY_PATCH),
    ])

    # Verify-or-fail, the lesson `setup_algotune.sh` step 2 records: a patch that prints nothing an
    # operator reads as a failure leaves the defect in place on a fresh machine.
    for target in (evaluator_main, eval_script):
        if MARKER not in target.read_text(encoding="utf-8"):
            print(f"   FAILED: the marker is not in {target.name} after patching", file=sys.stderr)
            return 1
    if "if baseline_manager and all_invalid_analyses:" in \
            evaluator_main.read_text(encoding="utf-8"):
        print("   FAILED: the gate survived the patch", file=sys.stderr)
        return 1
    print("   invalid_solution_analysis now reaches evaluate_summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
