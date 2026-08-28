#!/usr/bin/env python3
"""LoopLab <-> AlgoTune evaluation bridge.

Scores a LoopLab-produced ``solver.py`` with **AlgoTune's own evaluator**, on this
machine, and prints the speedup as JSON on stdout for LoopLab's ``stdout_json``
metric reader.

No scoring logic lives here. The bridge moves one file into the layout
``evaluate_results.py`` expects (``results/<model>/<task>/solver.py``), invokes that
script for exactly one ``(model, task)`` pair, and reads the number back. Whatever
AlgoTune computes is what LoopLab optimises:

    speedup = baseline_ms / optimized_ms      (100 % instance validity required)

Both times are measured locally in the same pass, so the ratio self-normalises
against hardware — which is what makes re-timing other agents' shipped solvers on
this box a fair comparison rather than a cross-machine guess.

PARITY — the reason ``--baseline-cache`` exists
-----------------------------------------------
AlgoTune's own agent loop pays the reference ("oracle") timing **once per run**:
``BaselineManager`` holds it in-process and every later ``eval`` command is cheap.
Measured on an RTX 5090 box (2026-08-19), that one-time cost is ~150 instances at
~3 s each, i.e. **about 30 minutes for a single task**.

A bridge that shells out once per candidate re-pays that on *every* node. The
resulting slowdown would be a property of this wiring, not of the agent under test,
and it cannot be corrected by granting more wall-clock because the overhead scales
with node count.

So the bridge caches the per-task baseline on first measurement and reuses it, which
is precisely what ``BaselineManager`` does inside an AlgoTuner run. This is parity
restoration, not a protocol deviation — but it is a deviation from a naive reading
of the harness, so it must appear in any methods note that accompanies published
numbers. Pass ``--no-cache`` to force a full re-measurement.

WHY BESIDE THE NUMBER — the ``no_speedup`` block
------------------------------------------------
A zero here is not one fact, it is at least six, and until 2026-08-22 they all printed the same
line. Measured on the arm-B run ``runs-armb/spectral_clustering`` (node_2's own ``score.log``)::

    {"speedup": 0.0, "eval_seconds": 110.5, "subset": "train",
     "baseline_source": "in-harness (record exposes no baseline_time_ms to cache)"}

while the evaluator's stderr — captured, then DISCARDED by this file — said::

    WARNING - Failed evaluations: 1
    WARNING -   spectral_clustering/diag2: Speedup N/A due to invalid results: 95/100 valid (95.0%)

The agent saw the bare ``0.0`` three times, concluded "replicating the reference is ANSWERED and
FAILED", threw the approach away and spent the rest of a $1.00 budget on that lesson. It was five
edge cases from a working solver, and the harness had said so.

So every line this bridge prints whose ``speedup`` is not a positive number now carries a
``no_speedup`` object -- enforced at the one exit, ``_emit``, not at each branch. The single
exception is the ``--enforce-rules`` refusal, which was already a complete explanation in its own
channel (``rules_violation`` + ``looplab_failure_reason``) and which this change leaves untouched.
The ``no_speedup`` keys are STABLE; add to them, do not rename them:

``reason``          one of ``NO_SPEEDUP_REASONS`` below — the machine-readable class.
``evaluator_verdict`` AlgoTune's own ``EvaluationResult.error_message``, verbatim, lifted off the
                    "Failed evaluations" block on its stderr. Absent when it printed none.
``speedup_reported`` the RAW value in ``evaluate_summary.json`` (a STRING: ``"N/A"``, ``"0.0000"``).
                    This is what separates "the harness refused to score" from "it scored zero".
``instances_total`` / ``instances_valid`` / ``instances_invalid`` / ``validity_pct``
                    parsed from the verdict. 94/100 and 0/100 are different experiments.
``is_solution_errors`` up to 3 ``{"message", "count"}`` rows: the DISTINCT ``ERROR:`` log lines the
                    evaluator's worker processes emitted, most frequent first. On a task whose
                    ``is_solution`` logs its rejections (139 of AlgoTune's 155 task modules call
                    ``logging.error``) these ARE the rejection reasons, verbatim.
``is_solution_error_lines`` / ``is_solution_errors_distinct``
                    how many such lines there were in total, so "3 shown" is never mistaken for
                    "3 happened".
``stderr_tail``     stays at the TOP level, unchanged, as on the other failure paths.

It is one NESTED object and not a set of top-level keys, which is a deliberate choice about a
different subsystem: `runtime/sandbox.py::json_line_extras` sweeps every other NUMERIC key on this
line into the node's `extra_metrics` with no declaration, so a top-level `instances_valid` would
enter the operator's metrics table, the Pareto front, the MLflow export and the reviewer projection
as an `auto`-channel measurement — which is exactly the population CLAUDE.md's `extra_metrics` rule
was written about. A dict is not a number, so nesting keeps the diagnosis in the TEXT the agent
reads and out of the metric plumbing. Verified 2026-08-22: on the line below, `json_line_metric`
still returns the speedup and `json_line_extras` returns `{"eval_seconds": ...}` and nothing else —
i.e. exactly what it returned before this change.

WHAT IS NOT REACHABLE, established rather than assumed. AlgoTune builds a richer
``invalid_solution_analysis`` — a per-instance code-context of the ``is_solution`` line that
rejected the solution, which arm A's own agent is shown up to three of
(``AlgoTuner/utils/message_writer.py:726-750``). It cannot get here:
``AlgoTuner/utils/evaluator/main.py:1160`` attaches it to the returned list ONLY when a
``baseline_manager`` was passed, and ``scripts/evaluate_results.py`` does not pass one; that script
then reads only aggregate fields off the per-instance dicts and writes a summary whose entire
payload is ``{"final_speedup": "<str>"}`` (``update_single_result``, line 852). Nothing under
``results/<model>/<task>/`` is written by the evaluator at all — that directory is our INPUT. The
``ERROR:`` lines above are the residual that IS reachable, and they are reachable for an accidental
reason worth writing down: the ``ProcessPoolExecutor`` children are started with ``forkserver``, so
they never run ``setup_logging`` and their root logger falls through to ``logging.lastResort``,
which prints ``levelname:name:message`` to the inherited stderr at WARNING and above. INFO from a
child is therefore lost (that is why "Validation stats: 94/100" never appears) and ERROR is not.

Two things those lines are NOT, and the keys are named so as not to claim otherwise: they are not
attributed to an instance, and they do not COUNT instances. The recorded run below is 6 invalid
instances against 17 ``ERROR:`` lines, because one rejection can log several checks.

Usage
-----
    python looplab_eval.py --algotune-root /path/to/AlgoTune \\
                           --task svm --model LoopLab --solver solver.py

See ``benchmarks/algotune/README.md`` for the full workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Where the per-task baseline timings are remembered between invocations. Kept beside
# the bridge rather than inside the AlgoTune checkout so a `git clean` of the upstream
# tree cannot silently discard the parity cache mid-campaign.
DEFAULT_CACHE = Path(__file__).resolve().parent / ".baseline_cache.json"
# The per-INSTANCE reference timings (the cache that saves the WALL CLOCK), as opposed to
# DEFAULT_CACHE above, which holds the aggregate (the cache that stabilises the DENOMINATOR).
# The per-instance one is written by `patch_baseline_cache.py`, which patches AlgoTune itself.
DEFAULT_TIMES_DIR = Path(__file__).resolve().parent / ".baseline_times"


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a missing or corrupt cache must never fail a run
        return {}


def _save_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:  # noqa: BLE001 - the measurement already succeeded; caching is best-effort
        pass


_SPEEDUP_KEYS = ("final_speedup", "speedup", "avg_speedup")


def _coerce_speedup(value) -> float | None:
    """`evaluate_results.py` writes the number as a STRING, and writes words for a non-number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None          # "N/A" / "Error" are the harness's own words for "no number"


def _find_result(node, task: str, model: str) -> dict | None:
    """Locate this (task, model) result anywhere in `evaluate_summary.json`.

    THE SHAPE THIS ACTUALLY HAS, measured 2026-08-19 against a real run:

        {"discrete_log": {"BV4": {"final_speedup": "0.9963"}}}

    Note what is NOT there: no `task_name`, no `speedup`, no `baseline_time_ms`, and the value is a
    STRING. The first version of this function looked for `node.get("task_name") == task and
    "speedup" in node` — fields nothing writes — so it returned None on a summary that had just been
    written successfully, and the bridge reported `speedup: 0.0` for a solver the evaluator had
    scored at 0.9963. It could never have returned a number for any task, on any node, for the whole
    campaign, and the failure looked exactly like a wrong solver.

    So the documented shape is read FIRST and explicitly. The tolerant walk stays underneath it
    because upstream has reshaped this file before — but a walk is a fallback, never the primary
    reader: a reader that only ever guesses cannot go red when the guess stops matching.
    """
    if isinstance(node, dict):
        entry = node.get(task)
        if isinstance(entry, dict):
            row = entry.get(model)
            if isinstance(row, dict):
                for key in _SPEEDUP_KEYS:
                    if key in row:
                        return dict(row)
            # A model key we did not predict (upstream normalizes names): accept a UNIQUE row.
            rows = [dict(v) for v in entry.values()
                    if isinstance(v, dict) and any(k in v for k in _SPEEDUP_KEYS)]
            if len(rows) == 1:
                return rows[0]
    return _walk_for_result(node, task)


def _walk_for_result(node, task: str) -> dict | None:
    """Fallback for a reshaped summary: any dict that names this task and carries a speedup."""
    if isinstance(node, dict):
        if node.get("task_name") == task and any(k in node for k in _SPEEDUP_KEYS):
            return node
        for value in node.values():
            found = _walk_for_result(value, task)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _walk_for_result(value, task)
            if found is not None:
                return found
    return None


# ------------------------------------------------------------------------------------------------
# The WHY beside the number. See the module docstring for the emitted key contract.
# ------------------------------------------------------------------------------------------------

# THE VOCABULARY. Every `no_speedup.reason` this file can print, and nothing else may be printed:
# `_emit` refuses an unregistered one down to `unknown` rather than shipping a word no reader knows.
# A bare string in a JSON line is exactly the duck-typed seam CLAUDE.md's registry rule is about — a
# typo'd literal here does not fail, it silently teaches the next proposer a class that does not
# exist. `tests/test_algotune_bridge_says_why.py` derives the emitted set from this module's own AST
# and asserts both directions, so a reason added at a call site and forgotten here goes red.
NO_SPEEDUP_REASONS = (
    "invalid_results",     # some instances failed `is_solution`; AlgoTune refuses a partial speedup
    "critical_error",      # solver_exception / import_error / memory_error — stopped mid-dataset
    "no_valid_speedups",   # nothing was timed at all
    "baseline_measured_in_pass",  # the arena timed the REFERENCE, not the candidate — see below
    "solver_unloadable",   # their import of our solver.py failed
    "compilation_failed",  # a Cython/pythran/DaCe build step failed
    "no_problems",         # the requested dataset half was empty
    "evaluator_error",     # the evaluator itself raised, or produced a shape it did not expect
    "evaluator_timeout",   # OUR --timeout fired; the evaluator never finished
    "no_summary",          # the evaluator exited without writing evaluate_summary.json
    "no_record",           # it wrote one, and this (task, model) is not in it
    "unreadable_summary",  # it wrote one and we could not parse it
    "reported_zero",       # the harness scored this solver and the number really is 0
    "no_solver",           # nothing to score: --solver names no file
    "no_evaluator",        # nothing to score with: no scripts/evaluate_results.py under the root
    "unknown",             # a shape we do not recognise. The stderr tail is the evidence; say so.
)
# NOT a member: `rules_violation`. That path never prints a zero — it prints `"speedup": null` with
# `rules_violation` and `looplab_failure_reason` already on the line, which is a COMPLETE
# explanation in its own channel, and it is a path this change was asked to leave alone.

# Their nine `result.error_message = ...` sites in `scripts/evaluate_results.py` (read 2026-08-22,
# lines 409/415/430/460/546/607/624/626/636), each mapped to one of ours. Longest-first is not
# required — no prefix here is a prefix of another — but FIRST MATCH WINS, so keep them disjoint.
# A verdict matching none of these is `unknown` and travels verbatim in `evaluator_verdict`: this
# table decides the CLASS, it never decides what the operator gets to read.
_VERDICT_REASONS = (
    ("Speedup N/A due to invalid results:", "invalid_results"),
    ("Critical error:",                     "critical_error"),
    ("No valid speedup calculations",       "no_valid_speedups"),
    ("Compilation failed:",                 "compilation_failed"),
    ("No test problems found",              "no_problems"),
    ("Failed to import optimized solver:",  "solver_unloadable"),
    ("Could not load optimized solver",     "solver_unloadable"),
    ("Unexpected results format",           "evaluator_error"),
    ("Agent-compatible evaluation error:",  "evaluator_error"),
)

# `logging.warning(f"  {result.task_name}/{result.display_model_name}: {result.error_message}")`,
# under the `Failed evaluations:` header (`evaluate_results.py:1170-1173`). Matched WITHOUT the
# `%(asctime)s - %(levelname)s - ` prefix `setup_logging` puts in front of it, because that format
# is theirs to change and the pair we need is the one after it. The model is matched loosely (`\S+`)
# rather than against our own `model_dir`: `normalize_model_name` may rewrite it, and a verdict for
# the one task we asked for is ours by construction — this process runs exactly one (task, model).
_VERDICT_RE = re.compile(r"(?:^|\s)(?P<task>[A-Za-z0-9_.+-]+)/(?P<model>\S+?): (?P<msg>\S.*)$")

# The header the verdict lines hang under, and their count — `for result in failed[:5]`. The scan is
# CONFINED to that block AND takes the FIRST match in it, and it needs both: `<task>/<model>: <text>`
# is not a rare shape on this stream. TWO lines below the verdict the evaluator logs
# `Updated summary for spectral_clustering/REC-90409: N/A`, which matches the same regex on the same
# task, inside the same five-line window. Measured against the recorded fixture while writing this
# (2026-08-22): a "last match wins" scan reported `evaluator_verdict: "N/A"` and `reason: unknown`
# for the exact run this whole change was built from — the defect it fixes, one layer in.
_FAILED_HEADER = "Failed evaluations:"
_MAX_VERDICT_LINES = 5

# The counts inside the verdict AlgoTune builds at `evaluate_results.py:607`.
_COUNTS_RE = re.compile(r"(?P<valid>\d+)/(?P<total>\d+) valid \((?P<pct>[\d.]+)%\)")

# `logging.lastResort`'s format — `%(levelname)s:%(name)s:%(message)s` — which is what an
# unconfigured root logger in a forkserver child prints. WARNING is deliberately excluded: the one
# WARNING every run of every task emits is `CODE_DIR not set when initializing DaCe`, i.e. pure
# noise, while ERROR/CRITICAL is where a task's `is_solution` states its rejection.
_WORKER_ERROR_RE = re.compile(r"^(?:ERROR|CRITICAL):(?P<logger>[\w.]*):(?P<msg>\S.*)$")

_MAX_IS_SOLUTION_EXAMPLES = 3      # what arm A's own agent is shown (message_writer.py:744)
_MAX_IS_SOLUTION_CHARS = 400       # one rejection line, not a pasted traceback
# THE COST OF RANKING BY FREQUENCY, measured on the real verification run (2026-08-22, node_2 of
# `runs-armb/spectral_clustering`): a HARNESS-internal error can outnumber every task rejection and
# take the top slot. That run logged
# `get_fresh_solve_callable_with_module_reload: Class 'Solver' not found in solver module` exactly
# 100 times — once per instance, out of `isolated_benchmark.py`'s daemonic in-process fallback while
# the REFERENCE was being timed — against 8 + 5 + 4 for the three real `is_solution` rejections, so
# 4 distinct kinds went into 3 slots and one real rejection was dropped.
#
# The cap is still 3, deliberately. That line is NOT noise in general: on a candidate that really
# ships no `Solver` class it is THE diagnosis, and no rule this side of the boundary can tell "the
# harness could not load a class" from "the task rejected the answer" — both arrive as one
# `logging.error` string through the same accidental channel. `is_solution_errors_distinct` is what
# keeps the omission visible (4 shown as 3), and raising the cap is a one-constant change the
# operator can make on evidence rather than a guess made here.


def _verdict_from_stderr(stderr: str, task: str) -> str:
    """AlgoTune's own `error_message` for THIS task, off its stderr, or "".

    The evaluator never puts this in the summary file — `update_single_result` writes
    `{"final_speedup": "N/A"}` and drops everything that explains it (see the module docstring) — so
    stderr is the only channel it survives on, and this bridge captured it and threw it away.

    Within one block the FIRST match for our task wins (see `_FAILED_HEADER`); across blocks the
    LAST block wins, since a second one could only come from a later evaluator run on this stream
    and the newest verdict is the one about the solver we just copied in.
    """
    found = ""
    remaining = 0
    for line in (stderr or "").splitlines():
        line = line.rstrip()
        if _FAILED_HEADER in line:
            remaining = _MAX_VERDICT_LINES
            continue
        if remaining <= 0:
            continue
        match = _VERDICT_RE.search(line)
        if match is None:
            remaining = 0          # the block ended; anything after it is other logging
            continue
        remaining -= 1
        if match.group("task") == task:
            found = match.group("msg").strip()
            remaining = 0
    return found


def _is_solution_errors(stderr: str) -> tuple[list[dict], int, int]:
    """The distinct `ERROR:` lines the evaluator's workers logged, most frequent first.

    Returns `(top rows, total lines, distinct count)`. Deduplicated by message and COUNTED, because
    the same rejection fires on many instances and three copies of one string is not three findings.
    Frequency order, ties broken by first appearance, so the answer is deterministic for a fixture.

    These are NOT per-instance and they are NOT a count of invalid instances — the recorded run has
    6 invalid instances and 17 of these lines. `instances_invalid` is the count; this is the reason.
    """
    counts: dict[str, int] = {}
    total = 0
    for line in (stderr or "").splitlines():
        match = _WORKER_ERROR_RE.match(line.rstrip())
        if match is None:
            continue
        total += 1
        message = match.group("msg").strip()[:_MAX_IS_SOLUTION_CHARS]
        counts[message] = counts.get(message, 0) + 1
    order = list(counts)
    top = sorted(order, key=lambda m: (-counts[m], order.index(m)))[:_MAX_IS_SOLUTION_EXAMPLES]
    return [{"message": m, "count": counts[m]} for m in top], total, len(counts)


def _no_speedup(reason: str, *, stderr: str = "", task: str = "",
                reported: Any = None) -> dict[str, Any]:
    """Build the `no_speedup` block: the class, the harness's own words, and the counts.

    `reason` is what the CALL SITE knows (it timed out; there was no summary). A verdict recovered
    from stderr OVERRIDES it, and that is the point rather than a fallback: the call site knows only
    that the number is missing, while the verdict is the evaluator saying which of the ways it
    is missing. The one exception is `evaluator_timeout` — there the call site's fact is the newer
    one, since any verdict on that stderr is about an earlier, completed phase.
    """
    verdict = _verdict_from_stderr(stderr, task) if task else ""
    out: dict[str, Any] = {"reason": reason}
    if verdict:
        out["evaluator_verdict"] = verdict
        if reason != "evaluator_timeout":
            out["reason"] = next((r for prefix, r in _VERDICT_REASONS
                                  if verdict.startswith(prefix)), "unknown")
        counts = _COUNTS_RE.search(verdict)
        if counts:
            valid, total = int(counts.group("valid")), int(counts.group("total"))
            out["instances_total"] = total
            out["instances_valid"] = valid
            out["instances_invalid"] = total - valid
            out["validity_pct"] = float(counts.group("pct"))
    if reported is not None:
        # The RAW summary value, as a string, deliberately unparsed: "N/A" is the harness refusing
        # to score and "0.0000" is the harness scoring a zero, and `float()` maps both to the same
        # 0.0 that started this.
        out["speedup_reported"] = str(reported)
    rows, lines, distinct = _is_solution_errors(stderr)
    if rows:
        out["is_solution_errors"] = rows
        out["is_solution_error_lines"] = lines
        out["is_solution_errors_distinct"] = distinct
    return out


# ------------------------------------------------------------------------------------------------
# THE SPLIT IS A CLAIM ABOUT A THIRD-PARTY FILE, so it is read off that file and never asserted.
# ------------------------------------------------------------------------------------------------
# `--subset train` does not do anything by itself. `scripts/evaluate_results.py` HARDCODES the test
# half at four sites and has no argument for the split; `patch_eval_subset.py` rewrites those sites
# to consult `ALGOTUNE_EVAL_SUBSET`. On an UNPATCHED checkout the environment variable is inert and
# the evaluator scores on TEST -- which is upstream's own behaviour and therefore silent.
#
# Until 2026-08-23 this file printed `"subset": args.subset` unconditionally. So a checkout that had
# been reverted (`git checkout scripts/`, a re-clone, `patch_eval_subset.py --revert`, or simply a
# `setup_algotune.sh` that ran the patches in a different order) would have scored EVERY node of
# arm B on the graded split while every `score.log` said `"subset": "train"`. That is the train/test
# leak `patch_eval_subset.py`'s own docstring calls "the exact class of thing this whole comparison
# exists to exclude", wearing a record that denies it -- one arm optimising against the set it is
# graded on, with nothing anywhere to notice.
#
# WHY THE MARKER AND NOT THE EVALUATOR'S OWN LOG LINE. The patch logs
# `LOOPLAB scoring on the 'train' split (N problems)`, which would be the better evidence -- the
# thing that RAN saying what it did. It cannot be read: that statement is inside
# `evaluate_single_model_task`, which runs in a `ProcessPoolExecutor` child started under
# `forkserver`, so `setup_logging` never ran there and `logging.lastResort` prints WARNING and above
# only. Measured on the 104 KB recording in `tests/fixtures/algotune_eval_invalid_results_stderr.txt`
# (a real 458 s evaluator run): ZERO occurrences of `LOOPLAB scoring`, and zero of
# `test problems for`, the INFO line four lines below it -- while the PARENT's INFO lines
# (`Evaluation complete`, `Updated summary for`) are all there. Raising that one call to WARNING
# would make the stronger evidence reachable and is a change to `patch_eval_subset.py`, not to this
# file; until a checkout carries it, the marker in the source is what decides.
_SUBSET_PATCH_MARKER = "# --- LOOPLAB EVAL SUBSET (benchmarks/algotune/patch_eval_subset.py) ---"
# What an unpatched `evaluate_results.py` scores on, whatever it was asked for.
_UPSTREAM_SUBSET = "test"


def subset_actually_scored(evaluator: Path, asked: str) -> tuple[str, dict[str, Any]]:
    """`(the split that will really be scored, the evidence for saying so)`.

    Reads the evaluator this bridge is about to invoke. The evidence travels as ONE NESTED object
    for the same reason `no_speedup` does: `runtime/sandbox.py::json_line_extras` sweeps every
    top-level NUMERIC key on this line into the node's `extra_metrics` as an undeclared `auto`
    measurement, so `subset_problems`-shaped facts must not sit at the top level. (Booleans it
    skips -- `isinstance(v, bool)` -- but a dict keeps the whole family out by construction.)
    """
    try:
        source = evaluator.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # Unreadable is NOT "unpatched": one is a fact about the arena, the other about this box.
        return asked, {"asked": asked, "verified": False, "reason": "evaluator_unreadable",
                       "detail": f"{type(exc).__name__}: {exc}"}
    if _SUBSET_PATCH_MARKER in source:
        return asked, {"asked": asked, "verified": True, "reason": "patch_marker_present",
                       "marker": _SUBSET_PATCH_MARKER}
    return _UPSTREAM_SUBSET, {
        "asked": asked, "verified": False, "reason": "evaluator_not_patched",
        "scored": _UPSTREAM_SUBSET,
        "detail": ("scripts/evaluate_results.py carries no patch_eval_subset.py marker, so "
                   "ALGOTUNE_EVAL_SUBSET is inert and this score is on the upstream default "
                   f"({_UPSTREAM_SUBSET!r}), not on {asked!r}"),
    }


# The line the patched evaluator logs from INSIDE the worker that chose the split (see
# `patch_eval_subset.py`, which emits it at WARNING precisely so it survives `logging.lastResort`).
# This is the STRONGER evidence -- the process that did the thing saying what it did, with the
# instance count beside it -- and it is read off the same stderr the verdict is read off.
_SUBSET_SCORED_RE = re.compile(
    r"LOOPLAB scoring on the '(?P<subset>train|test)' split \((?P<n>\d+) problems\)")


def subset_from_stderr(stderr: str, evidence: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Upgrade `evidence` with what the evaluator SAID, or leave it exactly as it was.

    Three outcomes and they are not the same fact:
      * the line is absent          -- unchanged; the marker check above is all there is;
      * it names the asked-for half -- `verified` on the strongest evidence there is;
      * it names the OTHER half     -- `subset_mismatch`, which is the leak actually happening, and
        the returned split is the one the evaluator named, never the one we asked for.
    The LAST match wins: a stream can carry more than one evaluator phase and the newest statement
    is about the scoring pass we just ran.
    """
    matches = list(_SUBSET_SCORED_RE.finditer(stderr or ""))
    if not matches:
        return None, evidence
    said = matches[-1].group("subset")
    out = dict(evidence)
    out["scored"] = said
    out["problems"] = int(matches[-1].group("n"))
    asked = evidence.get("asked")
    if said == asked:
        out["verified"] = True
        out["reason"] = "evaluator_said_so"
        out.pop("detail", None)
    else:
        out["verified"] = False
        out["reason"] = "subset_mismatch"
        out["detail"] = (f"asked for {asked!r} and the evaluator logged that it scored {said!r} -- "
                         "this score is on a different half of the dataset than the record claimed")
    return said, out


# The per-invocation artefacts this bridge creates in the THIRD-PARTY checkout, removed on the way
# out. They exist because LoopLab evaluates nodes concurrently and a fixed `--model LoopLab` made
# two nodes overwrite each other's solver and summary (see `main`), but nothing ever removed them:
# measured 2026-08-22, ONE campaign left 77 `results/LoopLab-<pid>/` directories and 79
# `reports/evaluate_summary.<pid>.json` files, and `benchmarks/snapshot.sh` copies `reports/` into
# every snapshot. A directory named after a dead process is also indistinguishable from a live
# one for anybody reading that tree later.
#
# Set `ALGOTUNE_KEEP_EVAL_ARTEFACTS=1` to keep them: when a score is disputed, the copied solver
# and the raw summary are the evidence, and deleting evidence by default in a DEBUGGING session is
# worse than the clutter this removes.
_ARTEFACTS: list = []


def _sweep_artefacts() -> None:
    """Best-effort. A cleanup failure must never change the line this bridge prints."""
    if os.environ.get("ALGOTUNE_KEEP_EVAL_ARTEFACTS") == "1":
        return
    for path in _ARTEFACTS:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        except Exception:                       # noqa: BLE001 - see the docstring
            pass
    _ARTEFACTS.clear()


def _emit(out: dict[str, Any]) -> None:
    """Print the ONE JSON line LoopLab reads — the single exit through which every path leaves.

    It exists to hold one invariant that a scattered `print(json.dumps(...))` could not: **a
    non-positive speedup never ships without a `no_speedup.reason`.** That is the whole defect —
    four different outcomes ("wrong on every instance", "wrong on 5 of 100", "crashed", "timed out")
    printed one indistinguishable `0.0` — so it is enforced HERE, at the boundary, rather than
    trusted to every branch above.

    A reason outside `NO_SPEEDUP_REASONS` is downgraded to `unknown` and the original is preserved
    under `reason_unregistered`. Refusing to print at all would turn a vocabulary slip into a node
    with no metric, which `metric_salvage` DISCARDS — strictly worse than a scored zero with a
    slightly wrong label, and the failure mode the timeout branch below was already fixed for once.
    """
    speedup = out.get("speedup")
    if not isinstance(speedup, (int, float)) or speedup <= 0:
        block = out.get("no_speedup")
        if not isinstance(block, dict):
            block = out["no_speedup"] = {"reason": "unknown"}
        reason = block.get("reason")
        if reason not in NO_SPEEDUP_REASONS:
            block["reason_unregistered"] = str(reason)
            block["reason"] = "unknown"
    print(json.dumps(out))
    # After the print, never before: the line LoopLab reads is the product, and a sweep that
    # raised before it would turn clutter into a node with no metric.
    _sweep_artefacts()


# The operator's PLANTED files, which are not part of any candidate's submission. Declared by
# `make_task.py::main` as the task's `protect` list (`[f"reference_{task}.py", "description.txt"]`)
# and repeated here as a DEFAULT only -- `--protect` lets the caller pass the declaration itself, so
# renaming the planted reference does not silently start copying the grader's own source into the
# scored directory beside the candidate.
DEFAULT_PROTECT = ("description.txt",)
DEFAULT_PROTECT_PREFIXES = ("reference_",)


def submission_files(solver: Path, protect: tuple = DEFAULT_PROTECT,
                     protect_prefixes: tuple = DEFAULT_PROTECT_PREFIXES) -> list:
    """Every file this candidate SUBMITS beside its solver, in a stable order.

    ONE enumeration, because two callers ask the same question for opposite reasons and a
    disagreement between them is a hole: the copy loop decides what gets SCORED, and the rules check
    decides what gets VALIDATED. While the rules check read only `solver.py`, a candidate could put
    a banned primitive in a helper module -- `edit_surface` grants `*.py`, `*.pyx`, `*.pxd`,
    `setup.py` and `pyproject.toml` -- and submit it unvalidated. Arm A cannot do that: AlgoTune
    validates at EDIT time, on every file its editor writes.
    """
    out = []
    for extra in sorted(solver.parent.iterdir()):
        if extra.name == solver.name or extra.is_dir():
            continue
        if extra.name in protect or extra.name.startswith(tuple(protect_prefixes)):
            continue
        out.append(extra)
    return out


def _rules_violation(root: Path, solver: Path, extras: list = ()) -> str:
    """AlgoTune's own verdict on this candidate, or "" when it is clean.

    Their validator, imported from their checkout — not a reimplementation and not a copied list.
    The rule set is theirs to change; a second copy here would go stale in the direction that
    silently permits, which is the direction that produces an incomparable number.

    A validator that cannot be imported returns "" and says so on stderr: refusing to score every
    node because their layout moved would turn an arena change into a campaign of zeros, and this
    check is a fairness guarantee, not a safety boundary (the safety boundary is the probe's kernel
    rung, which does not depend on it).
    """
    sys.path.insert(0, str(root))
    try:
        from AlgoTuner.security.code_validator import check_code_for_tampering
    except Exception as exc:                    # noqa: BLE001 — see the docstring
        print(f"looplab_eval: --enforce-rules asked for, but AlgoTune's validator did not import "
              f"({exc!r}); scoring WITHOUT the rule check", file=sys.stderr)
        return ""
    finally:
        if sys.path and sys.path[0] == str(root):
            sys.path.pop(0)
    # EVERY submitted Python source, not just `solver.py`. The rule is a FAIRNESS guarantee and it
    # is only as wide as the submission: validating one file while copying five means a speedup can
    # be obtained with a primitive the other arm was forbidden, which is precisely the incomparable
    # number this flag exists to refuse. `.pyx`/`.pxd` are deliberately NOT fed to the validator --
    # it parses Python with `ast` and Cython is not Python, so it would raise SyntaxError on every
    # legitimate Cython submission and the arm below would report the evaluator's failure instead.
    # That residual is real and is stated rather than papered over.
    checked = [solver] + [e for e in extras if e.suffix == ".py"]
    try:
        for path in checked:
            verdict = str(check_code_for_tampering(path.read_text(encoding="utf-8")) or "")
            if verdict:
                return verdict if path is solver else f"{path.name}: {verdict}"
        return ""
    except SyntaxError as exc:
        # Their validator re-raises a syntax error rather than reporting it, and a solver that does
        # not parse is the EVALUATOR's failure to report, not a rules violation.
        print(f"looplab_eval: solver does not parse ({exc}); leaving it to the evaluator",
              file=sys.stderr)
        return ""



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path,
                    help="Path to the AlgoTune checkout (the dir holding scripts/ and results/).")
    ap.add_argument("--task", required=True, help="AlgoTune task name, e.g. 'svm'.")
    ap.add_argument("--model", default="LoopLab",
                    help="Directory name under results/ this candidate is filed as.")
    ap.add_argument("--solver", default="solver.py", help="Candidate solver to score.")
    ap.add_argument("--baseline-cache", type=Path, default=DEFAULT_CACHE,
                    help=f"Per-task baseline cache (default: {DEFAULT_CACHE}).")
    ap.add_argument("--no-cache", action="store_true",
                    help="Ignore and do not write the aggregate baseline cache; recompute the ratio "
                         "from each run's freshly measured baseline.")
    ap.add_argument("--baseline-times-dir", type=Path, default=DEFAULT_TIMES_DIR,
                    help="Where patch_baseline_cache.py keeps the per-instance reference timings. "
                         "Informational here; the patch owns that path.")
    ap.add_argument("--subset", choices=("train", "test"), default="train",
                    help="Dataset half to score on. Default TRAIN, mirroring AlgoTuner's own agent, "
                         "which iterates on train and touches test only for its final number — "
                         "scoring every node on test would let this arm optimise against the graded "
                         "split while the other arm does not. Requires patch_eval_subset.py -- and "
                         "the emitted `subset` is what the evaluator will ACTUALLY score, with the "
                         "evidence for it under `subset_evidence` (see subset_actually_scored).")
    ap.add_argument("--timeout", type=int, default=7200, help="Seconds to allow the evaluator.")
    ap.add_argument("--protect", default="",
                    help="Comma-separated files the OPERATOR planted, which are not part of any "
                         "candidate's submission and must never be copied into the scored "
                         "directory. This is the task's own `protect` declaration "
                         "(make_task.py writes it into the task JSON); passing it keeps ONE "
                         "declaration instead of two spellings that drift. A name ending in `*` is "
                         "a prefix. Empty = the historical default "
                         "(`description.txt` and `reference_*`).")
    ap.add_argument("--solver-file-only", action="store_true",
                    help="Submit ONLY --solver, the way this bridge behaved before 2026-08-24. "
                         "Default is to submit every file the node wrote beside it and to run the "
                         "arena's own `setup.py build_ext --inplace` over them, which is the "
                         "editing surface the other arm has always had.")
    ap.add_argument("--enforce-rules", action="store_true",
                    help="Run AlgoTune's OWN solver validator (AlgoTuner.security.code_validator) "
                         "on the candidate before scoring, and refuse to score a violation. OFF by "
                         "default: these are THIS arena's rules, not LoopLab's, and a LoopLab task "
                         "that is not an AlgoTune arm must not inherit them.")
    args = ap.parse_args()

    root: Path = args.algotune_root.resolve()
    evaluator = root / "scripts" / "evaluate_results.py"
    if not evaluator.exists():
        _emit({"speedup": 0.0, "error": f"no evaluate_results.py under {root}",
               "no_speedup": _no_speedup("no_evaluator")})
        return 0

    # THE SPLIT, decided by the file that will honour it (see `subset_actually_scored`). Everything
    # below reports `subset`, never `args.subset`: what was ASKED for is on the evidence object and
    # what was SCORED is on the line.
    subset, subset_evidence = subset_actually_scored(evaluator, args.subset)
    if not subset_evidence.get("verified"):
        print(f"looplab_eval: {subset_evidence.get('detail', subset_evidence)}", file=sys.stderr)

    src = Path(args.solver).resolve()
    if not src.exists():
        _emit({"speedup": 0.0, "error": f"no solver at {src}",
               "no_speedup": _no_speedup("no_solver")})
        return 0

    # The declaration wins where it is given; the literals below are only a fallback for a caller
    # that has no task JSON to read (a hand invocation, an older campaign).
    _protect, _protect_prefixes = DEFAULT_PROTECT, DEFAULT_PROTECT_PREFIXES
    if args.protect.strip():
        _declared = [x.strip() for x in args.protect.split(",") if x.strip()]
        _protect = tuple(x for x in _declared if not x.endswith("*"))
        _protect_prefixes = tuple(x[:-1] for x in _declared if x.endswith("*")) or ("\0",)

    if args.enforce_rules:
        violation = _rules_violation(root, src, submission_files(src, _protect, _protect_prefixes))
        if violation:
            # NOT a score of zero, and not a crash to repair: this candidate was never eligible.
            # Arm A cannot produce such a solver at all — AlgoTune validates at EDIT time and simply
            # refuses to write the file (`editor/editor_functions.py:1377`), so its agent learns the
            # rule and loses nothing. Our Developer writes through LoopLab's own tools, which that
            # validator never sees, so without this the two arms would be playing by different rules
            # about what may be SUBMITTED — and a speedup obtained with a primitive the other arm was
            # forbidden is not a comparable number.
            # `looplab_failure_reason` is the DECLARED-reason channel
            # (`runtime/command_eval.py::declared_failure_reason` ->
            # `engine/triage.py::DECLARABLE_REASONS`): the engine ends the node with this reason and
            # does NOT spend a repair on it, because there is nothing here a repair could fix that
            # would not be a way around the arena's rule. The full text rides along so the next
            # proposer reads WHAT was refused, not just that something was.
            print(json.dumps({"speedup": None, "rules_violation": violation,
                              "looplab_failure_reason": "rules_violation",
                              "error": f"rules_violation: {violation}"}))
            return 2

    # A PER-INVOCATION model name, because LoopLab evaluates nodes CONCURRENTLY (`eval_parallel`).
    # With the fixed `--model LoopLab`, two nodes copied their solvers over one another in
    # `results/LoopLab/<task>/`, and each `summary.unlink()` deleted the summary the other was about
    # to read -- so a node could record its sibling's speedup as its own, or find no summary and
    # score 0.0. The suffix keeps the runs apart; `--model` still names the family for reporting.
    model_dir = f"{args.model}-{os.getpid()}"
    dest_dir = root / "results" / model_dir / args.task
    dest_dir.mkdir(parents=True, exist_ok=True)
    _ARTEFACTS.append(root / "results" / model_dir)
    shutil.copy2(src, dest_dir / "solver.py")

    # THE WHOLE SUBMISSION, not one file — and the build the arena runs for its own agent.
    #
    # Arm A's editing surface is a DIRECTORY: it may write `.pyx`, Pythran and DaCe sources and a
    # `setup.py`, and `editor_functions.py::_verify_and_recompile_if_needed` compiles them with
    # `python setup.py build_ext --inplace`. On AlgoTune those compiled paths are a primary source
    # of the large speedups in the published table. This bridge copied ONE file, so arm B could not
    # reach any of them — a capability difference our harness invented, not the arena.
    #
    # The build is invoked HERE rather than by importing the arena's editor, for the reason the
    # comment below already gives about `BaselineManager`: importing AlgoTuner in this process
    # before the evaluator builds its worker pool crashes every evaluation. Same command, same
    # `--inplace`, same 1800 s ceiling the editor uses.
    _submitted = []
    if not args.solver_file_only:
        # Same enumeration the rules check above ran on, so what is SCORED is what was VALIDATED.
        for extra in submission_files(src, _protect, _protect_prefixes):
            shutil.copy2(extra, dest_dir / extra.name)
            _submitted.append(extra.name)
    build_note = ""
    # `.pxd`, not `.pyi`. A `.pyi` is a TYPE STUB — `setup.py build_ext` has nothing to do with one
    # — while `.pxd` is Cython's own header and is exactly what a `.pyx` submission brings with it.
    # The typo made the trigger fire on a stub (spawning a build with no `setup.py`, whose rc=2 was
    # then recorded as a "failed" build) and miss a `.pxd`-only submission.
    if any(n.endswith((".pyx", ".pxd")) for n in _submitted) or "setup.py" in _submitted \
            or "pyproject.toml" in _submitted:
        cmd = [sys.executable, "setup.py", "build_ext", "--inplace"]
        try:
            built = subprocess.run(cmd, cwd=str(dest_dir), capture_output=True, text=True,
                                   timeout=1800)
            build_note = ("ok" if built.returncode == 0
                          else f"failed rc={built.returncode}: {(built.stderr or '')[-400:]}")
        except subprocess.TimeoutExpired:
            build_note = "timeout after 1800s"
        except OSError as exc:                      # no setup.py, no compiler, no python
            build_note = f"not run: {type(exc).__name__}: {exc}"
        print(f"looplab_eval: build_ext {build_note}", file=sys.stderr)

    # The summary path is per-invocation for the same reason: `evaluate_results.py` writes one
    # shared `reports/evaluate_summary.json`, so concurrent bridges would read each other's.
    summary = root / "reports" / f"evaluate_summary.{os.getpid()}.json"
    _ARTEFACTS.append(summary)
    if summary.exists():
        summary.unlink()

    # The evaluator is invoked DIRECTLY. The persistent baseline cache is applied by patching
    # `BaselineManager` on disk (`patch_baseline_cache.py`), NOT by wrapping it from here: measured
    # 2026-08-19, importing AlgoTuner in this process before the evaluator builds its worker pool
    # crashed every evaluation ("A process in the process pool was terminated abruptly"), while the
    # identical run invoked directly returned 0.9963x.
    argv = [sys.executable, str(evaluator), "--models", model_dir, "--tasks", args.task,
            "--output", str(summary)]

    # The split is carried in the ENVIRONMENT rather than as a flag: `evaluate_results.py` hardcodes
    # it at three sites and has no argument for it, so `patch_eval_subset.py` reads this name. An
    # unpatched checkout ignores it and scores on test, which is upstream's own behaviour.
    # What was ASKED for, deliberately -- a patched checkout honours it and an unpatched one ignores
    # it, and `subset_evidence` above has already recorded which of those this checkout is.
    env = dict(os.environ, ALGOTUNE_EVAL_SUBSET=args.subset)

    # THE REFERENCE TIMINGS MUST ALREADY EXIST, and this snapshot is how we know they did.
    #
    # When AlgoTune has no cached per-instance baseline for this (task, subset, lane) it measures one
    # in the same pass -- and in that pass THE CANDIDATE IS NEVER TIMED. The evaluator reports the
    # reference against itself: `final_speedup` comes back ~1.0 and every instance validates,
    # whatever was submitted. Demonstrated 2026-08-25 with a solver whose `solve()` returns `[]` for
    # every instance: it scored 1.0009 with 100/100 valid. The same champion scored 0.0 (98/100
    # valid) once the cache was warm, and `edge_expansion`'s scored 0.9996 cold against 24.68 warm.
    #
    # It cost eight of this campaign's twenty final numbers, all of them plausible: 1.146, 1.069,
    # 1.0646, 1.0362, 1.0308, 1.0243, 0.9865. Nothing in the output said they were not measurements,
    # and they were read for hours as a real train/test collapse -- the tell was only that those
    # eight evaluations each ran ~330 s against ~50 s for the eleven that had a warm cache, the extra
    # ~210 s being the reference pass itself.
    #
    # So: glob the timings this run could use BEFORE it runs, and compare after. A file that appears
    # or changes during the run means the reference was measured here, which means the number below
    # is not about the candidate. `_emit` is told to refuse it rather than print it.
    #
    # OPEN[baseline-guard-watches-the-wrong-clone] in the documented two-clone workflow this
    # fingerprints a `.baseline_times` the arena never writes.
    # proof:absent:--baseline-times-dir@benchmarks/algotune/campaign.sh
    # REVIEW 2026-08-25 (correctness): `patch_baseline_cache.py` bakes its cache dir at PATCH time
    # (default: beside the patch script that ran, docs/52 SS6 runs it from the working clone) while
    # `DEFAULT_TIMES_DIR` here resolves beside THIS file at RUN time (docs/51 SS7 runs the campaign
    # from the pinned `looplab-armb` clone). Nothing passes `--baseline-times-dir` and nothing checks
    # the pairing, so the guard can fingerprint a directory nothing writes and stay silent. The glob
    # and the reassigned-`subset` closure were fixed 2026-08-25; this half needs the cache dir read
    # out of the patched `baseline_manager.py` (or a refusal when the watched dir does not match the
    # patch's embedded one). Its companion: `campaign.sh` has no re-score path for a refused
    # champion pass -- `record_done` writes the marker regardless and `already_measured` never
    # re-runs it -- and the refusal BRANCH itself is executed by no test (see the annotation in
    # tests/test_algotune_refuses_baseline_measured_in_pass.py).
    # ONE KEY, FIXED BEFORE THE RUN, and a glob that matches what the patch really writes.
    #
    # `patch_baseline_cache.py` names a per-instance cache `<task>__<subset>[__<regime>].json`, and
    # its regime segment is the EMPTY string whenever workers <= 1 — which docs/51 SS10 mandates and
    # `campaign.sh` never overrides, so on every campaign run to date the file is the bare
    # `<task>__<subset>.json`. The old pattern demanded a literal `__` plus a third segment, so it
    # matched NOTHING in the serial regime: both fingerprints were `{}`, compared equal, and the
    # reference-timed-in-pass refusal — the whole point of this block — could not fire on the one
    # configuration the campaign actually runs. `*` after the subset matches both spellings.
    #
    # And `_times_key` is bound HERE rather than read out of `subset` at call time: `subset` is
    # REASSIGNED after the run by `subset_from_stderr` (the `subset_mismatch` case), so a closure
    # over it made the before-glob and the after-glob cover DIFFERENT splits and produced a refusal
    # whose `timings_written` listed files that had existed all along. Refusing on a mismatch is
    # right; explaining it with a fabricated cause is not, and `subset_evidence` already states the
    # real one.
    _times_key = f"{args.task}__{subset}"

    def _baseline_fingerprint() -> dict:
        try:
            return {f.name: (f.stat().st_mtime_ns, f.stat().st_size)
                    for f in args.baseline_times_dir.glob(f"{_times_key}*.json")}
        except OSError:
            return {}

    _baseline_before = _baseline_fingerprint()

    started = time.time()
    try:
        proc = subprocess.run(argv, cwd=str(root), capture_output=True, text=True,
                              timeout=args.timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        # EVERY other failure path here prints `{"speedup": 0.0, "error": ...}` so LoopLab's
        # `stdout_json` reader gets a number. This one used to let the exception escape, so the one
        # failure the `--timeout` flag exists for was the only one that printed NOTHING -- the node
        # then recorded "no metric" rather than a scored-zero, which `metric_salvage` DISCARDS
        # instead of counting as a failed solver. A timed-out solver is a wrong solver, not a
        # missing measurement.
        # `TimeoutExpired.stderr` is bytes under `text=False` and str under `text=True`; the
        # decode below is what the tail already did, hoisted so the explanation can read the SAME
        # bytes. A partial stderr is still worth mining: the ERROR lines a task's `is_solution`
        # logged before the wall clock ran out say what it was rejecting while it hung.
        partial = exc.stderr or ("" if isinstance(exc.stderr, str) else b"")
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        # A partial stderr can already carry the split statement -- the evaluator logs it before
        # it starts timing anything -- so the timed-out line says which half it was timing.
        said, subset_evidence = subset_from_stderr(partial, subset_evidence)
        _emit({"speedup": 0.0, "eval_seconds": round(time.time() - started, 1),
               "subset": said or subset, "subset_evidence": subset_evidence,
               "error": f"evaluator exceeded --timeout {args.timeout}s",
               "stderr_tail": partial[-1000:],
               "no_speedup": _no_speedup("evaluator_timeout", stderr=partial, task=args.task)})
        return 0
    elapsed = round(time.time() - started, 1)

    # The evaluator's own statement outranks the marker: one is what the file CAN do, the other is
    # what this invocation DID. A mismatch is reported, never silently preferred away.
    said, subset_evidence = subset_from_stderr(proc.stderr, subset_evidence)
    if said is not None:
        subset = said
        if subset_evidence.get("reason") == "subset_mismatch":
            print(f"looplab_eval: {subset_evidence['detail']}", file=sys.stderr)

    out: dict[str, Any] = {"speedup": 0.0, "eval_seconds": elapsed, "subset": subset,
                           "subset_evidence": subset_evidence}
    # WHAT WAS SUBMITTED AND WHETHER IT BUILT, on the record rather than only on stderr.
    #
    # `build_note` used to be printed and then dropped, so a `setup.py build_ext` that FAILED was
    # invisible in the JSON and the run continued to the arena, where the solver's `import fast`
    # died and the verdict came back `solver_unloadable` — a reason `compare_arms.py` classifies as
    # SOLVERS_FAULT and averages into arm B's mean as a real 0.0. The build is OUR step (this bridge
    # invokes it), so a reader has to be able to tell "the candidate's extension would not compile"
    # from "the candidate's python would not import", and one of those is upstream of the other.
    # Nested for `json_line_extras`' reason: a top-level numeric key here would be swept into the
    # node's `extra_metrics` as an undeclared `auto` measurement.
    # Only when there is something to say: a one-file submission that needed no build adds no key,
    # so the single-solver path keeps the exact shape `test_a_real_speedup_is_untouched` pins.
    if _submitted or build_note:
        out["submission"] = {"files": _submitted, "build_ran": bool(build_note),
                             "build": build_note or "not needed (no .pyx/.pxd/setup.py submitted)"}

    if not summary.exists():
        out["error"] = "evaluate_summary.json not produced"
        out["stderr_tail"] = proc.stderr[-1000:]
        out["no_speedup"] = _no_speedup("no_summary", stderr=proc.stderr, task=args.task)
        _emit(out)
        return 0

    try:
        record = _find_result(json.loads(summary.read_text(encoding="utf-8")),
                              args.task, model_dir)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"unreadable summary: {type(exc).__name__}: {exc}"
        out["no_speedup"] = _no_speedup("unreadable_summary", stderr=proc.stderr, task=args.task)
        _emit(out)
        return 0

    if record is None:
        out["error"] = f"no record for task {args.task!r} in evaluate_summary.json"
        out["stderr_tail"] = proc.stderr[-1000:]
        out["no_speedup"] = _no_speedup("no_record", stderr=proc.stderr, task=args.task)
        _emit(out)
        return 0

    speedup = next((_coerce_speedup(record[k]) for k in _SPEEDUP_KEYS if k in record), None)
    # These two are absent from the shape this file actually has; they are read defensively so the
    # aggregate cache below still works if upstream restores them.
    baseline_ms = record.get("baseline_time_ms")
    optimized_ms = record.get("optimized_time_ms")

    # Parity: remember this task's baseline the first time it is measured, and report the
    # cached value on later calls so an out-of-process bridge does not re-pay a cost the
    # in-process reference loop pays once. The SOLVER time is always freshly measured.
    # THE SUBSET IS PART OF THE KEY. The train and test halves are DIFFERENT REFERENCE SETS, and
    # `patch_baseline_cache.py`'s own docstring says so in as many words -- "It never caches across
    # TASKS or across the train/test SUBSET split ... one standing in for another would corrupt
    # every speedup computed from it". That is true of the per-INSTANCE cache it patches, whose key
    # is `<task>__<subset>...`, and it was NOT true of this aggregate one, whose key was the task
    # alone. The campaign scores every node on train and the champion once on test through this same
    # default cache file, so the first train baseline would have become the denominator of the
    # graded test score.
    #
    # It has never fired: `evaluate_summary.json`'s entire payload is `{"final_speedup": "<str>"}`
    # (see `_find_result`), so `baseline_time_ms` is absent, the `else` branch below runs every time
    # and `.baseline_cache.json` does not exist on this box at all. That is why this is a repair and
    # not an incident -- and it is exactly the shape that becomes one the day upstream restores the
    # field, which is the reason those two reads are there in the first place.
    cache_key = f"{args.task}__{subset}"
    if not args.no_cache:
        cache = _load_cache(args.baseline_cache)
        cached = cache.get(cache_key)
        if baseline_ms is not None and cached is None:
            cache[cache_key] = {"baseline_time_ms": baseline_ms, "measured_at": elapsed}
            _save_cache(args.baseline_cache, cache)
            out["baseline_source"] = "measured (now cached)"
        elif cached is not None:
            out["baseline_source"] = "cache"
            out["baseline_cached_ms"] = cached["baseline_time_ms"]
            # Recompute against the cached baseline so every node is scored on the same
            # denominator; a denominator that drifts per node is not a comparable metric.
            if optimized_ms:
                speedup = float(cached["baseline_time_ms"]) / float(optimized_ms)
        else:
            # NOT "the baseline is missing" -- the speedup above came through fine and is the
            # harness's own. This branch means only that the RECORD carries no `baseline_time_ms`
            # for this bridge to cache, which is the normal shape (see the note above the reads),
            # so the aggregate cache never engages. The old label read as a failure and cost a real
            # investigation on 2026-08-20: a node scoring 0.0 was diagnosed as "no baseline" when
            # the actual cause was an empty working set, three layers away.
            out["baseline_source"] = "in-harness (record exposes no baseline_time_ms to cache)"

    # The other half of the fingerprint taken before the evaluator ran. If the reference timings for
    # this (task, subset) appeared or changed while it ran, the reference was measured in this pass
    # and the candidate was not timed -- so whatever `final_speedup` says is about the reference.
    # Refuse it. A null speedup with a reason is a result the campaign can act on; a plausible 1.0
    # is one it cannot even doubt.
    _baseline_after = _baseline_fingerprint()
    if _baseline_after != _baseline_before:
        appeared = sorted(set(_baseline_after) - set(_baseline_before))
        # Through `_no_speedup`, not a dict literal: `tests/test_algotune_bridge_says_why.py`
        # derives the producible set from the AST of THIS call and would have called the new
        # reason registered-but-dead. It caught exactly that, which is what it is for.
        out["speedup"] = None
        block = _no_speedup("baseline_measured_in_pass", reported=speedup)
        block["evaluator_verdict"] = (
            "the per-instance reference timings for this task/subset were written during this "
            "evaluation, so the arena timed the reference and not the candidate")
        block["timings_written"] = appeared or ["(existing file changed)"]
        block["remedy"] = "re-run this scoring now that the timings are cached"
        out["no_speedup"] = block
        out["baseline_source"] = "measured in this pass — NOT A MEASUREMENT OF THE CANDIDATE"
        _emit(out)
        return 0

    out["speedup"] = float(speedup) if speedup is not None else 0.0
    if baseline_ms is not None:
        out["baseline_time_ms"] = baseline_ms
    if optimized_ms is not None:
        out["optimized_time_ms"] = optimized_ms
    for key in ("is_valid", "success", "error_message"):
        if record.get(key) is not None:
            out[key] = record[key]

    # THE PATH THIS CHANGE IS ABOUT. A record was found, and until 2026-08-22 that was treated as
    # success even when the record said `"final_speedup": "N/A"` -- `_coerce_speedup` returned None,
    # the line above printed 0.0, and `proc.stderr`, which held the evaluator's own sentence about
    # WHY, was dropped on the floor. The reason starts as `reported_zero` (what THIS side knows: a
    # record exists and it did not yield a positive number) and `_no_speedup` upgrades it to the
    # evaluator's own class when its verdict is on the stderr we just captured.
    if out["speedup"] <= 0:
        reported = next((record[k] for k in _SPEEDUP_KEYS if k in record), None)
        out["stderr_tail"] = proc.stderr[-1000:]
        out["no_speedup"] = _no_speedup("reported_zero", stderr=proc.stderr, task=args.task,
                                        reported=reported)

    _emit(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
