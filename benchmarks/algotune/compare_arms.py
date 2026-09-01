#!/usr/bin/env python3
"""Summarise an AlgoTune campaign: arm A (AlgoTuner) vs arm B (LoopLab), same model.

Reads each arm from the place that arm actually writes, and refuses to invent the rest:

* **arm A** -> ``<algotune>/reports/agent_summary.json``, which ``AlgoTuner/main.py`` updates as
  ``<task>.<normalized-model>.final_speedup`` at the end of a run. That file also carries the 17
  SHIPPED reference models, so their numbers come along for free as context.
* **arm B** -> ``B-<task>.final.json``, the champion's score on the TEST split, produced once after
  the run. Every LoopLab node is evaluated on TRAIN (mirroring AlgoTuner's own agent loop), so the
  run's internal champion metric is a TRAIN number and belongs in the same column as arm A's test
  result only by mistake. Without ``--final-dir`` the tool falls back to that train metric AND says
  so in a warning, because a silently mixed-split comparison is worse than no comparison.
  The champion itself is identified from the FOLD (`extract_champion.py`), never from a directory
  listing: the fold is what decides a run's champion.

WHAT THE COLUMNS MEAN, AND THE TWO THINGS THEY DO NOT
-----------------------------------------------------
``speedup = baseline_ms / optimized_ms``, both timed on this machine in the same pass, so the ratio
self-normalises against hardware. **100 % instance validity is required for any speedup at all** --
a solver wrong on one instance scores 0, not partial credit -- so a `0.0` here means "wrong
somewhere", not "no faster".

It does NOT isolate the loop from the model: the 17 reference arms were produced by *AlgoTuner's*
loop driving other models, so re-timing them compares ARTIFACTS. The only controlled row is
A-vs-B on the same model, produced here. They are printed together and labelled apart.

A MISSING arm is printed as ``--`` and never as ``0``. "Did not finish" and "finished wrong" are
different facts, and averaging the first as a zero is how a campaign that half-ran reports a
result. Means are taken over the PAIRS that have both arms, and the count is stated.

TWO WAYS THAT PROMISE WAS BROKEN BY THIS FILE, both fixed 2026-08-23 and both demonstrable against
the campaign under ``/var/tmp/looplab-bench``:

1. **A zero the HARNESS is responsible for was averaged as a wrong solver.** ``_arm_b_final`` read
   ``speedup`` and nothing else out of a file that also carries ``no_speedup.reason`` -- the class
   the bridge adds precisely so a zero is not six facts at once (see ``looplab_eval.py``). Live:
   ``B-rectanglepacking.final.json`` is ``{"speedup": 0.0, "no_speedup": {"reason":
   "no_valid_speedups", "evaluator_verdict": "No valid speedup calculations from agent
   evaluation"}}`` with ``Evaluation complete: 0/1 successful`` on the captured stderr. NOTHING WAS
   TIMED. This tool printed ``0.0000``, and its own footer said that means "the solver was WRONG
   somewhere" -- a sentence that is false about that row, under a number that would have pulled
   arm B's mean down and handed arm A a win. ``NOT_SOLVERS_FAULT`` below splits the vocabulary:
   a zero a SOLVER earned is a zero; a zero that means the arena never produced a measurement is
   ``--``, exactly as a missing arm is, and the reason is printed beside it either way.
2. **A task the campaign says is still owed was printed as a result.** ``campaign.sh`` writes
   ``B-<task>.final.json`` BEFORE it decides whether the task-arm reached a terminal state, so an
   interrupted run (rc 130/137/143 -- "NO marker; the task is still owed") leaves a full-looking
   score behind. Live: ``B-pagerank.final.json`` = 15.6726 and ``B-rbf_interpolation.final.json`` =
   1.1852, neither with a ``.done`` marker and neither with a ``.refused`` one. The campaign driver
   knows those two are unfinished and this table could not tell them from the other seventeen. The
   markers are that driver's own verdict, so with ``--final-dir`` they are consulted: a score whose
   task-arm has no marker is reported as UNFINISHED and kept out of the means.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

# FIND OUR OWN PACKAGE. Every driver script runs this file with ALGOTUNE's interpreter -- seven of
# them do, because the summary reads the arena's reports and that is the venv nearest to hand -- and
# `looplab` is not installed there. The result was `ModuleNotFoundError: No module named 'looplab'`
# at the last step of a campaign that had already run for hours: the comparison table, the whole
# point of the exercise, was never printed, and the arms had to be compared by hand.
#
# Fixing the seven call sites would leave the eighth to get it wrong. This file sits at
# `<repo>/benchmarks/algotune/`, so it can say where its own package lives and does. `parents[2]`
# resolves against the tree this copy belongs to, which matters: run from the pinned `looplab-armb`
# checkout it must import THAT tree's `looplab`, not the working clone's.
_REPO = Path(__file__).resolve().parents[2]
if (_REPO / "looplab" / "__init__.py").exists() and str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _to_float(value):
    """`looplab.core.parse.to_float(finite=True)` — "the one spelling of COERCING scalar parsing",
    which also rejects NaN/inf. Four hand-rolled copies of this lived here; `float("inf")` parses
    fine and would have been printed as a speedup and averaged into the arm means.

    THE BOOTSTRAP ABOVE FINDS THE PACKAGE; IT CANNOT INSTALL ITS DEPENDENCIES. `looplab.core.parse`
    imports pydantic, so under ALGOTUNE's interpreter — the one seven driver scripts actually use —
    `sys.path` alone still ended the campaign's last step in a `ModuleNotFoundError`, one import
    deeper than the one that was fixed. So the shared spelling is TRIED and a two-line equivalent
    stands in when the environment cannot supply it. The fallback is deliberately the whole of
    `to_float(v, finite=True)` and nothing else: it is a copy, which this file's history says to
    avoid, and it is a smaller cost than a comparison table that cannot print. Keep them identical.
    """
    try:
        from looplab.core.parse import to_float
    except Exception:                      # noqa: BLE001 - no package, or no pydantic in THIS venv
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return None if not math.isfinite(f) else f
    return to_float(value, finite=True)


def _arm_a(summary_path: Path, model_fragment: str) -> dict[str, float | None]:
    """`{task: final_speedup}` for the one model whose name contains `model_fragment`."""
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, float | None] = {}
    for task, models in (data or {}).items():
        if not isinstance(models, dict):
            continue
        for name, row in models.items():
            if model_fragment.lower() not in str(name).lower():
                continue
            # `row` is not guaranteed to be a dict: the harness writes the WORDS "N/A"/"Error"
            # for a non-number, and `("N/A" or {}).get(...)` raises AttributeError, which the
            # float guard below does not catch -- it killed the whole comparison with a traceback
            # instead of printing the `--` this tool promises for a missing arm.
            raw = row.get("final_speedup") if isinstance(row, dict) else None
            # "N/A" and "Error" are the harness's own words for "no number", and they are not zero.
            # `finite=True` additionally rejects NaN/inf, which would otherwise print as a speedup
            # and poison the mean.
            value = _to_float(raw)
            # LAST WRITER DOES NOT WIN. The fragment matches by SUBSTRING, and this file routinely
            # holds several names for one model -- `deepseek-v4-flash` and
            # `deepseek-v4-flash-0731` both match the default fragment. Assigning unconditionally
            # let a trailing "N/A"/"Error" row overwrite an earlier real number, so the task
            # printed `--`, counted as "missing an arm" and dropped out of the means: the silent
            # exclusion this module's docstring promises not to do, arriving through the door it
            # was watching. A number, once found for a task, is never replaced by a non-number.
            if value is not None or task not in out:
                out[task] = value
    return out


def _reference_models(summary_path: Path, task: str) -> dict[str, float]:
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for name, row in (data.get(task) or {}).items():
        value = _to_float(row.get("final_speedup") if isinstance(row, dict) else None)
        if value is not None:
            out[str(name)] = value
    return out


# THE ZEROS THE SOLVER DID NOT EARN. Every member of `looplab_eval.py::NO_SPEEDUP_REASONS` that
# means "no measurement was produced" rather than "this solver was wrong". Derived by reading that
# tuple's own comments, and pinned against it by
# `tests/test_algotune_campaign_report.py::test_every_no_speedup_reason_is_classified`, so a reason
# added there and forgotten here goes red instead of being silently treated as a solver's zero.
#
# The two that STAY solver zeros are the interesting half:
#   `invalid_results`   the harness scored it and refused a partial credit -- a wrong solver, and
#                       the whole arena rule ("100 % validity or nothing"). Live: spectral_clustering
#                       at 95/100 valid is a real 0.0 and belongs in the mean.
#   `reported_zero`     the harness scored this solver and the number really is 0.
#   `compilation_failed`/`solver_unloadable` are the candidate's own build and import, so they are
#                       the candidate's failure, not the arena's -- and arm A pays for exactly the
#                       same mistakes.
NOT_SOLVERS_FAULT = frozenset({
    "critical_error",      # stopped mid-dataset; no complete pass over the instances happened
    "no_valid_speedups",   # nothing was timed at all
    "baseline_measured_in_pass", "baseline_regime_mismatch",  # the arena timed the REFERENCE and never the candidate: the
                           # number it returned (~1.0, and ~1.0 even for a solver that returns
                           # nothing) is about the reference, so it is not evidence about a solver
                           # in either direction
    "no_problems",         # the requested dataset half was empty -- an arena/config fault
    "evaluator_error",     # the evaluator itself raised, or produced a shape it did not expect
    "evaluator_timeout",   # OUR --timeout fired; "did not finish", which the docstring forbids
                           # averaging as a zero
    "no_summary",          # the evaluator exited without writing evaluate_summary.json
    "no_record",           # it wrote one and this (task, model) is not in it
    "unreadable_summary",  # it wrote one and we could not parse it
    "no_solver",           # nothing to score: the champion extraction produced no file
    "no_evaluator",        # nothing to score with
    "unknown",             # a shape nobody recognises is not evidence about a solver
})

# The other half, spelled out so the PARTITION is total. Without it a reason added to
# `NO_SPEEDUP_REASONS` and forgotten here would default to "the solver's fault" and be averaged into
# arm B's mean as a zero -- silently, in the direction that costs score, which is the whole class of
# defect this file was corrected for. The test asserts
# `SOLVERS_FAULT | NOT_SOLVERS_FAULT == set(NO_SPEEDUP_REASONS)` in BOTH directions, so a new member
# on either side goes red and has to be classified deliberately.
SOLVERS_FAULT = frozenset({
    "invalid_results",     # scored, and refused a partial credit -- the arena's own 100 % rule
    "reported_zero",       # scored, and the number really is 0
    "solver_unloadable",   # their import of OUR solver.py failed: the candidate's file
    "compilation_failed",  # the candidate's own Cython/pythran/DaCe build step
})


def _arm_b_final(final_json: Path) -> tuple[float | None, str]:
    """`(arm B's TEST score, the reason it is not a number)` — the champion on the graded split.

    This — not the run's own champion metric — is what compares to arm A. Every LoopLab node is
    evaluated on TRAIN (mirroring AlgoTuner's agent loop), so `state.best().metric` is a TRAIN
    number and putting it in the same column as arm A's test result would compare two different
    splits.

    A non-positive speedup is read TOGETHER WITH the `no_speedup.reason` the bridge puts beside it,
    because those are two different facts and this file used to print only the first. A zero whose
    reason is in `NOT_SOLVERS_FAULT` is not a score: nothing was measured, so it leaves as `None`
    and is excluded from the means exactly as a missing arm is. See this module's docstring for the
    live row that made the difference.
    """
    try:
        row = json.loads(final_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, ""
    if not isinstance(row, dict):
        return None, ""
    value = _to_float(row.get("speedup"))
    block = row.get("no_speedup")
    reason = str(block.get("reason")) if isinstance(block, dict) else ""
    if value is None:
        # `{"speedup": null, "error": ...}` — campaign.sh's own rows for "no champion to score" and
        # "the run refused to start". Already a `--`; carry the sentence so the table says which.
        return None, reason or str(row.get("error") or "")
    if value <= 0 and reason in NOT_SOLVERS_FAULT:
        return None, reason
    return value, reason


def _arm_b_regime(final_json: Path) -> str | None:
    """The RULER this row was measured on, if the row says.

    A speedup is a ratio and its denominator is a reference measured under a regime: `__lane<N>r3`
    when the evaluation pool is bypassed, `__w<W>x<C>r3` otherwise. On this box those two references
    sum to 3898 ms and 2976 ms over the same hundred instances, so a table that averages rows from
    both is averaging numbers off two instruments. Until 2026-08-30 no result carried its regime at
    all, which is why this reads defensively: a row from an older campaign simply says nothing, and
    "unstated" is reported as itself rather than folded into whichever key happens to be present.
    """
    try:
        row = json.loads(final_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    block = row.get("eval_regime") if isinstance(row, dict) else None
    if not isinstance(block, dict):
        return None
    key = block.get("key")
    return str(key) if key else None


def _arm_b_train(run_dir: Path) -> float | None:
    """The LoopLab run's own champion metric — a TRAIN number, reported only as context."""
    if not (run_dir / "events.jsonl").exists():
        return None
    try:
        from looplab.events.eventstore import EventStore
        from looplab.events.replay import fold
        # The LOG FILE, not the run directory -- see extract_champion.py: the directory form
        # raised IsADirectoryError and every arm-B row read as "no champion".
        state = fold(EventStore(str(run_dir / "events.jsonl")).read_all())
    except Exception:                       # noqa: BLE001 - a broken run is "no number", not a crash
        return None
    best = state.best()
    return _to_float(best.metric) if best is not None else None


def _fmt(value: float | None) -> str:
    return "--" if value is None else f"{value:.4f}"


# ARM A'S OWN WORD FOR "THE SOLVER WAS WRONG SOMEWHERE".
#
# The two arms enforce the same rule — 100% instance validity or no speedup — but they RECORD the
# outcome differently, and until 2026-08-24 this file turned that into a scoring asymmetry. Arm B
# writes `{"speedup": 0.0, "no_speedup": {"reason": "invalid_results"}}`, which is a measured zero
# and was averaged in. Arm A writes `final_speedup: "N/A"` into the summary and the REASON into
# `reports/agent_failures.json`, which nothing here read — so its zero printed as `--` and dropped
# out of the means. Live on one task: `A-spectral_clustering` 98/100 valid, `B-spectral_clustering`
# 95/100 valid; the first vanished, the second scored 0.0. Both directions favoured arm A.
#
# The choice is to COUNT BOTH AS ZERO rather than to exclude both. The arena's rule is that a solver
# wrong on any instance is not scored, and a framework that submits one has failed the task — that
# is a result, not a missing measurement. Excluding it would let an arm escape the consequence of
# its own wrong answers, which is the larger distortion of the two.
#
# `missing_metrics` with `final_eval_success=True` is the shape that means it: the evaluation RAN
# and produced no valid speedup. A reason that does not mean that leaves the row `--`, because a
# task-arm that never got to submit is genuinely unmeasured.
_ARM_A_SOLVER_FAULT = frozenset({"missing_metrics"})


def arm_a_failure(algotune_root: Path, task: str, model_fragment: str = "") -> tuple:
    """`(is_solver_fault, reason, stamp)` for arm A on this task, from the arena's own record.

    The file is a MERGE TARGET — `AlgoTuner/main.py::update_failure_json` loads it and `setdefault`s
    into it — so rows from earlier campaigns survive a rerun. The timestamp is returned with the
    reason so a caller can say which campaign it belongs to instead of assuming this one.

    WHICH RECORD, and it is NOT "the first one". Iterating and returning on the first dict answered
    with whatever model happened to be inserted FIRST — in a merge target that is routinely an OLD
    campaign's row sitting beside this one's, so the current `missing_metrics` +
    `final_eval_success=True` record was never read and arm A's real zero printed `--` again: the
    exact asymmetry the caller was corrected for. So the same two rules `_arm_a` uses, for the same
    reasons: filter by `model_fragment` (empty = every model, the historical behaviour when the
    caller has no fragment to give), and among the matches prefer the NEWEST `timestamp_utc` rather
    than dict order, which carries no meaning here at all. A record with no timestamp sorts oldest,
    so a stamped row always outranks an unstamped one.
    """
    try:
        raw = json.loads((algotune_root / "reports" / "agent_failures.json").read_text("utf-8"))
    except (OSError, ValueError):
        return (False, "", "")
    row = raw.get(task) or {}
    if not isinstance(row, dict):
        return (False, "", "")
    matches = [rec for name, rec in row.items()
               if isinstance(rec, dict)
               and (not model_fragment or model_fragment.lower() in str(name).lower())]
    if not matches:
        return (False, "", "")
    rec = max(matches, key=lambda r: str(r.get("timestamp_utc") or ""))
    reason = str(rec.get("reason") or "")
    stamp = str(rec.get("timestamp_utc") or "")
    if reason in _ARM_A_SOLVER_FAULT and "final_eval_success=True" in str(rec.get("details") or ""):
        return (True, reason, stamp)
    return (False, reason, stamp)


# A HARNESS CUT: the run was stopped by this harness rather than on its own terms. Both members are
# TERMINAL (a `.done`, so a resume does not re-run them) and both are REPORTED-BUT-NEVER-AVERAGED,
# because the mean may only hold task-arms that ran to the ceiling they are compared at.
#
# One tuple rather than a second copy of the rule: the wall-cut test was already spelled three times
# in two languages (here, `already_measured` and `final_banner` in campaign.sh) and adding a state by
# editing every copy is how the copies come apart -- a resume then skips a task the banner reports as
# owed, or averages a clock-killed run the report excludes.
#   wall_cut  - `timeout` fired (rc=124). The clock, not the budget.
#   stall_cut - the stall guard fired: the run's own log stopped growing for STALL_TIMEOUT. Added
#               2026-08-25; before it, a stall kill arrived as rc=143, indistinguishable from an
#               operator's Ctrl-C, so it got NO marker and the task was re-run from scratch for
#               ever, spending a fresh full budget each time to stall in the same place.
HARNESS_CUT_STATES: tuple[str, ...] = ("wall_cut", "stall_cut")
# (row phrase, footer phrase) — one row is about ONE arm and the footer counts many, so they cannot
# be one string without the grammar going wrong in whichever place it was not written for.
_CUT_PHRASES = {
    "wall_cut": ("cut at the wall clock (rc=124)", "were cut at the WALL CLOCK (rc=124)"),
    "stall_cut": ("stall-cut (its own log stopped growing)",
                  "were STALL-CUT (the run's own log stopped growing)"),
}


def _cut_phrase(state: str, *, footer: bool = False) -> str:
    """What to call a harness cut in a sentence."""
    pair = _CUT_PHRASES.get(state)
    if pair is None:
        return f"were stopped by the harness ({state})" if footer else f"stopped by the harness ({state})"
    return pair[1] if footer else pair[0]


def marker_state(final_dir: Path | None, arm: str, task: str, runs_root: Path | None = None) -> str:
    """What `campaign.sh` says about this task-arm: `"done"`, `"refused"` or `"unfinished"`.

    Read from the driver's own markers rather than re-derived, because they encode a decision this
    file cannot make. `record_done` writes `<arm>-<task>.done` ONLY for a terminal state; a refusal
    to start gets `.refused` and no marker; and rc 130/137/143 gets NEITHER, which is the driver
    saying "interrupted -- the task is still owed". The `.final.json` this tool reads is written
    BEFORE that decision, so a full-looking score survives all three cases.

    No marker directory (`--final-dir` absent, or a campaign whose markers were not kept) is
    `"unknown"` and changes nothing: a missing verdict is not a bad one.
    """
    if final_dir is None:
        return "unknown"
    marker = final_dir / f"{arm}-{task}.done"
    if marker.exists():
        # A WALL-CUT TASK IS NOT A FINISHED ONE. `record_done` writes a `.done` for rc=124 too, so a
        # task-arm the `timeout` killed looks exactly like one that ended on its own terms. Measured
        # 2026-08-23: five task-arms across both arms were cut at the 4 h wall, and three of them had
        # not spent the budget they are compared at — `A-count_riemann_zeta_zeros` reached $0.14 of
        # $1.00 because forty gateway timeouts at 300 s each ate three and a half of its four hours.
        # Averaging that beside a task that spent its dollar answers a question nobody asked.
        #
        # `state=wall_cut` IS ASKED FIRST AND `rc=124` SECOND, and both are kept. The driver now
        # names the state in words (`record_done`), which is the field to read: recovering "a clock
        # killed this" from an integer is what left every other reader wrong about it. But the 27
        # markers of the campaign under `/var/tmp/looplab-bench` were written before that field
        # existed and carry only `rc=124`, so dropping the integer would silently reclassify five
        # real wall cuts as clean finishes — the exact defect, arriving through the fix for it.
        try:
            text = marker.read_text(encoding="utf-8")
        except OSError:
            return "done"
        if "state=wall_cut" in text or "rc=124" in text:
            return "wall_cut"
        if "state=stall_cut" in text:
            return "stall_cut"
        # A TASK-ARM THE OPERATOR SKIPPED IS NOT ONE THAT FINISHED, and the difference has to
        # survive into the table. `campaign.sh::already_measured` skips on ANY non-empty marker
        # that is not a wall cut, so writing one is how a running campaign is told to stop taking
        # new work without touching the driver a live bash is reading. That mechanism is right; it
        # just leaves a marker that looks exactly like a completed run to every later reader.
        # Measured need, 2026-08-26: five CP-SAT task-arms were skipped by decision, and without
        # this branch the summary would have counted them among the finished and listed none of
        # them as absent. Restored at the 2026-08-29 merge, where resolving this hunk in favour of
        # the other side's `stall_cut` dropped it.
        if "state=operator_skip" in text:
            return "skipped"
        return "done"
    if (final_dir / f"{arm}-{task}.refused").exists():
        return "refused"
    if not any(final_dir.glob(f"{arm}-*.done")):
        return "unknown"        # nothing in this directory carries markers at all
    # A MISSING MARKER IS NOT THE SAME AS AN UNFINISHED RUN, and the difference is decidable from
    # the run's own terminal evidence rather than from the driver's bookkeeping. Measured
    # 2026-08-23: an operator drained the campaign at a task boundary and killed the driver while
    # two task-arms were between "champion scored" and `record_done`. Both had spent their whole
    # ceiling ($1.003 and $1.002 of $1.00) and produced three nodes each; their numbers are as good
    # as any other's, and excluding them would have dropped two real results — the same silent
    # exclusion this module's docstring promises not to do, arriving from the other side.
    #
    # So the marker is asked first and the RUN is asked second. Only a task with neither a marker
    # nor a run that reached its own ceiling is owed.
    if _run_reached_its_ceiling(final_dir, arm, task, runs_root):
        return "done"
    return "unfinished"


def _run_reached_its_ceiling(final_dir: Path, arm: str, task: str,
                             runs_root: Path | None = None) -> bool:
    """Did this task-arm stop because it spent its budget, rather than because it was killed?

    The evidence is the run's own event log: a `run_finished` naming the spend ceiling, or a metered
    total at or above the ceiling the snapshot records. Both are written by the run, not by the
    driver, so a killed driver cannot forge either. Absent or unreadable evidence answers False —
    a task with no record of finishing is owed, which is the safe direction.

    `runs_root` IS PASSED IN, never guessed. This used to derive `<final-dir>/../runs-<arm>`, a
    sibling layout the shipped defaults do not produce: `campaign.sh` defaults `CAMPAIGN_RUNS` to
    `camp-runs` and `box-jhub-l40s.sh` keeps that name, so on the documented setup the path never
    existed, this always answered False, and the drained-at-a-task-boundary rescue the docstring
    above argues for ($1.003 and $1.002 of $1.00, two real results) was excluded anyway. `main()`
    already takes `--runs-root` and `campaign.sh` prints the correct invocation — the table honoured
    it and this helper did not. The old sibling guess is kept only as the FALLBACK for a caller that
    has no runs root to give (`campaign_status.py` passes its own), because every miss here fails
    toward "less credit", which is the safe direction and is why nobody noticed.
    """
    runs = Path(runs_root) if runs_root is not None else final_dir.parent / f"runs-{arm}"
    events = runs / task / "run" / "events.jsonl"
    snapshot = runs / task / "run" / "config.snapshot.json"
    if not events.exists():
        return False
    try:
        ceiling = float(json.loads(snapshot.read_text(encoding="utf-8")).get("llm_budget_usd") or 0)
    except Exception:                       # noqa: BLE001 - no snapshot, no ceiling to compare to
        ceiling = 0.0
    spent = 0.0
    for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"llm_usage"' in line:
            try:
                spent += float((json.loads(line).get("data") or {}).get("cost") or 0)
            except ValueError:
                continue
        elif '"run_finished"' in line and "spend ceiling reached" in line:
            return True
    return bool(ceiling) and spent >= ceiling


def _scored_attempt(final_dir: Path | None, arm: str, task: str) -> str | None:
    """Which attempt produced the number this table prints, per campaign.sh's own marker.

    The marker records `attempt=aN`. Only that attempt's spend is the price of the reported score;
    the rest is the cost of the outages, which is a different question and is not asked here.
    """
    if final_dir is None:
        return None
    marker = final_dir / f"{arm}-{task}.done"
    try:
        text = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"\battempt=(\S+)", text)
    return m.group(1) if m else None


def metered_spend(meter: Path) -> dict[tuple[str, str, str], tuple[float, float]]:
    """What each task-arm ACTUALLY cost, from the metering proxy's ledger.

    Returns {(arm, task, attempt): (spent, uncounted)}. KEYED BY ATTEMPT, and that is not a
    detail: two gateway outages on 2026-08-25 forced whole task-arms to be re-run, and their
    abandoned attempts are still in the ledger with their numbers already thrown away. Summing a
    task across attempts turns a legitimate relaunch into an overspend -- it reported nine
    offenders against the five that a per-attempt count finds, inventing `convex_hull` ($2.077
    across two attempts, neither over $1.02) out of nothing. The caller pairs this with the
    attempt named in the `.done` marker, which is the only attempt whose score is reported.

    `uncounted` is the part spent on streams the
    upstream ended WITHOUT a usage frame. That distinction is the whole reason this exists:

    AlgoTuner (arm A) prices a call from the response's `usage` block, so a stream that dies
    before the usage frame costs it NOTHING in its own ledger while the gateway charges for every
    forwarded delta. Its `spend_limit` therefore fires late. Measured on 2026-08-26: five of arm
    A's fifteen finished task-arms crossed the $1.00 ceiling, `rbf_interpolation` reaching $2.009
    with $1.006 of it invisible -- seventeen consecutive ~1808 s runaway streams of ~220k tokens
    each, over nine hours, that its ledger recorded as free. Its own log closes the case: "Spend
    limit of $1.0000 reached. Current spend: $1.0025".

    LoopLab (arm B) prices from THIS ledger, so aborted streams consume its budget like any other
    call: 20 of 20 task-arms landed at or under $1.011, none over. The asymmetry is not a
    difference between the two loops -- it is a difference in what each one is able to see.

    A pair where one arm silently drew twice the budget is not a fair pair, and the table must not
    present it as one. An unreadable or absent ledger yields {} and the table simply makes no such
    claim, which is the safe direction.
    """
    out: dict[tuple[str, str], list[float]] = {}
    try:
        lines = meter.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        arm, task, attempt = d.get("arm"), d.get("task"), d.get("attempt") or "?"
        if not arm or not task:
            continue
        try:
            cost = float(d.get("cost") or 0.0)
        except (TypeError, ValueError):
            continue
        slot = out.setdefault((arm, task, attempt), [0.0, 0.0])
        slot[0] += cost
        if d.get("stream_aborted"):
            slot[1] += cost
    return {k: (v[0], v[1]) for k, v in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path)
    ap.add_argument("--runs-root", required=True, type=Path,
                    help="Directory holding <task>/run/ for the LoopLab arm.")
    ap.add_argument("--final-dir", type=Path, default=None,
                    help="Directory holding B-<task>.final.json (the champion's TEST score). "
                         "Without it, arm B's column is its TRAIN metric and is NOT comparable to "
                         "arm A -- the header says so.")
    ap.add_argument("--model-fragment", default="v4-flash",
                    help="Substring identifying OUR model inside agent_summary.json.")
    ap.add_argument("--meter", type=Path, default=None,
                    help="The metering proxy's ledger (meter.jsonl). Defaults to "
                         "<final-dir>/../meter/meter.jsonl. Used to report what each task-arm "
                         "ACTUALLY cost, which is not what either loop's own ledger believes.")
    ap.add_argument("--budget-usd", type=float, default=1.0,
                    help="The per-task spend ceiling both arms were given. A task-arm whose "
                         "METERED spend exceeds it by more than 5%% is named under the table.")
    ap.add_argument("--reference", action="store_true",
                    help="Also print the shipped reference models per task (context, not controls).")
    args = ap.parse_args()

    summary = args.algotune_root.resolve() / "reports" / "agent_summary.json"
    a = _arm_a(summary, args.model_fragment)
    tasks = sorted({p.name for p in args.runs_root.glob("*") if p.is_dir()} | set(a))
    if not tasks:
        print("no campaign output found"); return 1

    rows, paired = [], []
    for task in tasks:
        va = a.get(task)
        if args.final_dir:
            vb, why = _arm_b_final(args.final_dir / f"B-{task}.final.json")
        else:
            vb, why = _arm_b_train(args.runs_root / task / "run"), ""
        # WHOSE SENTENCE IS THIS? `_arm_b_final` answers with a bare reason SLUG
        # (`no_valid_speedups`), which names no arm and therefore needs the `B:` label; every
        # sentence the fallbacks below write already opens with "arm A" or "arm B". Labelling both
        # the same way is what printed `[B: arm A cut at the wall clock]` in the one report an
        # operator reads the arms' health from.
        why_is_a_bare_reason = bool(why)
        # BOTH ARMS' MARKERS, and the same words for both. Until 2026-08-24 this line read only
        # arm B's, so the wall-cut exclusion, the WALL-CUT banner and the "still owed" line were
        # arm-B-only — and in the campaign of 2026-08-23 arm A was wall-cut on 13 of 19 tasks
        # against arm B's 3. All thirteen printed as `--`, indistinguishable from "arm A was never
        # run", and the mean a reader takes away said nothing about one arm hitting the clock on two
        # thirds of the suite.
        state = marker_state(args.final_dir, "B", task, args.runs_root)
        state_a = marker_state(args.final_dir, "A", task, args.runs_root)
        if va is None:
            fault, a_reason, a_stamp = arm_a_failure(args.algotune_root.resolve(), task,
                                                     args.model_fragment)
            if fault:
                # Scored, not dropped — the same treatment arm B's `invalid_results` already gets.
                va = 0.0
                why = why or f"arm A {a_reason} ({a_stamp[:10]}) — wrong on some instances, scored 0"
        if va is not None and state_a in ("unfinished", "refused"):
            why = why or f"arm A {state_a} (campaign.sh wrote no .done marker)"
            va = None
        if vb is not None and state in ("unfinished", "refused"):
            # The driver's verdict outranks the leftover file. A score written before the task-arm
            # was interrupted is not a measurement of a $1.00 run, and printing it beside seventeen
            # that are is the mixed-population failure this whole module is written against.
            why = why or f"arm B {state} (campaign.sh wrote no .done marker)"
            vb = None
        elif vb is not None and state in HARNESS_CUT_STATES:
            # A HARNESS CUT IS NOT A BUDGET. The number is real — the champion was scored — but the
            # run it came from was stopped by this harness rather than by the ceiling every other row
            # is compared at, so it is REPORTED and not AVERAGED. Kept visible rather than dropped
            # because the operator needs to see that the bound is binding at all: three of the five
            # cuts measured on 2026-08-23 had not reached the budget, one of them at $0.14 of $1.00.
            why = why or f"arm B {_cut_phrase(state)}, not by the budget"
        if va is not None and state_a in HARNESS_CUT_STATES:
            why = why or f"arm A {_cut_phrase(state_a)}, not by the budget"
        cut = next((st for st in (state, state_a) if st in HARNESS_CUT_STATES), None)
        # THE ROW'S STATE HAS TO SPEAK FOR BOTH ARMS. It carries arm B's marker, with arm A's
        # consulted only for the cut flag above -- so an arm-A `operator_skip` reached the table as
        # arm B's `done` and the "skipped" line never printed. A harness cut still outranks it (a
        # clock beats bookkeeping); a skip on EITHER side is named when neither arm was cut. Lost
        # and restored at the 2026-08-29 merge, where `HARNESS_CUT_STATES` replaced the single
        # `wall_cut` this precedence was originally written around.
        skipped = "skipped" in (state, state_a)
        rows.append((task, va, vb, why,
                     cut or ("skipped" if skipped else state), why_is_a_bare_reason))
        # A wall-cut row on EITHER side is PRINTED (above) and not PAIRED: the mean is the one number
        # a reader takes away, and it may only contain task-arms that ran to the ceiling they are
        # compared at. Which arm hit the clock does not change that.
        # OPEN[skip-with-stale-final-still-pairs] an operator-skipped task-arm with values left by
        # an EARLIER campaign is still averaged into the paired means while the footer says it
        # never ran.
        # proof:`present:and vb is not None and not cut:@benchmarks/algotune/compare_arms.py`
        # REVIEW 2026-08-30 (correctness): `skipped` renames the row's state and drives the
        # "SKIPPED ... never ran" footer, and does not exclude the pair — only `cut` does.
        # `agent_summary.json` is a documented MERGE TARGET (rows from earlier campaigns survive a
        # rerun) and `B-<task>.final.json` files persist across campaigns (only `.refused` is
        # cleared), so a task skipped THIS campaign with a matching stale row is the
        # mixed-population mean this module's docstring promises against — restored through the
        # skip branch at the 2026-08-29 merge. Exclude `skipped` from pairing like `cut`.
        if va is not None and vb is not None and not cut:
            paired.append((va, vb))

    if args.final_dir is None:
        print("WARNING: --final-dir not given, so arm B's column is its TRAIN metric while "
              "arm A's is a TEST score. Those are different splits and must not be read as "
              "a comparison.")
    width = max(len(t) for t, _, _, _, _, _ in rows)
    print(f"{'task':<{width}}  {'A: AlgoTuner':>13}  {'B: LoopLab':>11}   winner")
    print("-" * (width + 42))
    for task, va, vb, why, _state, bare_reason in rows:
        if va is None or vb is None:
            winner = "(incomplete)"
        elif va == vb:
            winner = "tie"
        else:
            winner = "A" if va > vb else "B"
        # The REASON travels with the row. A `--` and a `0.0000` both used to be bare, and the whole
        # point of the split above is that the operator can see which of the two this is and why.
        # The label is carried on the ROW, not read out of the building loop's last iteration:
        # `why_is_a_bare_reason` is set per task above and this is a separate pass over `rows`.
        note = (f"   [B: {why}]" if bare_reason else f"   [{why}]") if why else ""
        print(f"{task:<{width}}  {_fmt(va):>13}  {_fmt(vb):>11}   {winner}{note}")

    print("-" * (width + 42))
    if paired:
        mean_a = sum(x for x, _ in paired) / len(paired)
        mean_b = sum(y for _, y in paired) / len(paired)
        wins_a = sum(1 for x, y in paired if x > y)
        wins_b = sum(1 for x, y in paired if y > x)
        print(f"{'mean over ' + str(len(paired)) + ' complete pairs':<{width}}  "
              f"{mean_a:>13.4f}  {mean_b:>11.4f}")
        print(f"{'wins':<{width}}  {wins_a:>13}  {wins_b:>11}   "
              f"({len(paired) - wins_a - wins_b} tied)")
    # OPEN[compare-arms-prints-its-tail-twice] the closing block — the still-OWED list, the
    # speedup footer and the whole `--reference` table — is printed TWICE on every invocation.
    # proof:`present:for task, _, _, _, _, _ in rows:@benchmarks/algotune/compare_arms.py`
    # REVIEW 2026-08-30 (merge-integrity): this copy (through the reference table below) and the
    # second one after the money block are both sides of a 2026-08-29 merge conflict kept at once;
    # this copy lacks the `skipped` footer the second has, so the two have already diverged, which
    # is how the next merge loses one. The operator's one end-of-campaign deliverable duplicates
    # its own caveats and rankings. Fold into ONE tail after the money block (this copy's
    # incomplete/unmeasured/harness-cut lines are the unique half to keep from here).
    incomplete = sum(1 for _, va, vb, _, _, _ in rows if va is None or vb is None)
    if incomplete:
        print(f"\n{incomplete} of {len(rows)} tasks are missing an arm and are EXCLUDED from the "
              f"means above — a missing run is not a zero.")
    unmeasured = sorted(t for t, _, _, why, _, _ in rows if why in NOT_SOLVERS_FAULT)
    if unmeasured:
        print(f"of those, {len(unmeasured)} arm-B row(s) are the ARENA producing no measurement "
              f"(not a wrong solver): " + ", ".join(unmeasured))
    for _st in HARNESS_CUT_STATES:
        _cut = sorted(t for t, _, _, _, st, _ in rows if st == _st)
        if _cut:
            print(f"{len(_cut)} task-arm(s) {_cut_phrase(_st, footer=True)} rather than by the budget, so their "
                  f"scores are shown but left out of the mean: " + ", ".join(_cut))
    unfinished = sorted(t for t, _, _, _, st, _ in rows if st in ("unfinished", "refused"))
    if unfinished:
        print(f"{len(unfinished)} task-arm(s) have no .done marker from campaign.sh and are still "
              f"OWED, so any score left behind for them is not reported: " + ", ".join(unfinished))
    print("\nspeedup = baseline_ms / optimized_ms, both timed here. 100% instance validity is "
          "required:\na 0.0000 means the solver was WRONG somewhere, not that it was not faster.\n"
          "That sentence is only true because a zero the ARENA is responsible for is printed as "
          "`--`\nwith its reason (see NOT_SOLVERS_FAULT); it was false about every such row before "
          "2026-08-23.")

    if args.reference:
        print("\nShipped reference models (AlgoTuner's loop driving OTHER models — context, not a "
              "control:\nthose rows differ from ours in the model AND were produced elsewhere).")
        for task, _, _, _, _, _ in rows:
            ref = _reference_models(summary, task)
            ours = {k: v for k, v in ref.items() if args.model_fragment.lower() in k.lower()}
            others = {k: v for k, v in ref.items() if k not in ours}
            if not others:
                continue
            best = max(others.items(), key=lambda kv: kv[1])
            print(f"  {task:<{width}}  best reference {best[0]} = {best[1]:.4f}  "
                  f"(median {sorted(others.values())[len(others) // 2]:.4f} over {len(others)})")
    # WHAT THE PAIR ACTUALLY COST. Printed for every run, not only when something is wrong: a
    # comparison at "the same $1 budget" is a claim about money, and a claim about money that is
    # never checked against the meter is a claim about a config file.
    meter_path = args.meter or (args.final_dir.parent / "meter" / "meter.jsonl"
                                if args.final_dir else None)
    spend = metered_spend(meter_path) if meter_path else {}
    if spend:
        ceiling = args.budget_usd
        over = []
        for task, *_rest in rows:
            for arm in ("A", "B"):
                attempt = _scored_attempt(args.final_dir, arm, task)
                if attempt is None:            # no marker -> no reported score -> nothing to judge
                    continue
                spent, uncounted = spend.get((arm, task, attempt), (0.0, 0.0))
                if ceiling > 0 and spent > ceiling * 1.05:
                    over.append((task, arm, spent, uncounted))
        totals = {arm: sum(v[0] for (a, _, _), v in spend.items() if a == arm)
                  for arm in ("A", "B")}
        print(f"\nmetered spend: arm A ${totals.get('A', 0.0):.2f}, arm B "
              f"${totals.get('B', 0.0):.2f} (the proxy's ledger, not either loop's own).")
        if over:
            over.sort(key=lambda r: -r[2])
            print(f"{len(over)} task-arm(s) drew MORE than the ${ceiling:.2f} ceiling they were "
                  f"given, so those pairs are NOT matched on budget:")
            for task, arm, spent, uncounted in over:
                tail = (f", ${uncounted:.3f} of it on streams that ended with no usage frame and "
                        f"so cost that loop's own ledger nothing"
                        if uncounted > 0.05 * ceiling else "")
                print(f"  {task:<{width - 2}} arm {arm}  ${spent:.3f}{tail}")
        else:
            print(f"every task-arm stayed within 5% of its ${ceiling:.2f} ceiling.")

    skipped = sorted(t for t, _, _, _, st, *_ in rows if st == "skipped")
    if skipped:
        print(f"{len(skipped)} task-arm(s) were SKIPPED by the operator and never ran, so this "
              f"table is over {len(rows) - len(skipped)} of {len(rows)} tasks: "
              + ", ".join(skipped))
    # ONE TABLE, ONE INSTRUMENT -- or say so. `looplab_eval.py::_emit` stamps `eval_regime` on
    # every line it prints, so a mean taken across two baseline regimes is now visible instead of
    # being a property of the environment two scripts happened to run under. Rows written before
    # 2026-08-30 carry no regime and are counted as "unstated" rather than assumed to match.
    # OVER THE ROWS THAT CARRY A NUMBER, and only those. Iterating ALL rows folded every task-arm
    # that never ran -- operator-skipped, refused, still owed, or whose `.final.json` is absent --
    # into "unstated", and the sentences below then describe them as rows that WERE measured
    # ("do not state the baseline regime they were measured on", "whose result predates the regime
    # being recorded at all"). On a campaign with three refusals and two skips that is a false
    # claim about five rows, printed six lines under the block that has just excluded the skipped
    # ones from the table, inside the one paragraph whose whole subject is comparability.
    seen_regimes: dict[str, list[str]] = {}
    for task, _va, vb, *_rest in rows:
        if vb is None:
            continue
        key = _arm_b_regime(args.final_dir / f"B-{task}.final.json") if args.final_dir else None
        seen_regimes.setdefault(key or "unstated", []).append(task)
    measured_n = sum(len(v) for v in seen_regimes.values())
    stated = {k: v for k, v in seen_regimes.items() if k != "unstated"}
    if len(stated) > 1:
        print("ARM B'S ROWS WERE MEASURED ON MORE THAN ONE BASELINE REGIME, so the mean above is "
              "taken\nacross two instruments and is not one number:")
        for key in sorted(stated):
            print(f"  {key:<14} {len(stated[key])} task(s): " + ", ".join(sorted(stated[key])))
        if "unstated" in seen_regimes:
            print(f"  {'unstated':<14} {len(seen_regimes['unstated'])} task(s) whose result "
                  f"predates the regime being recorded at all")
    elif stated and "unstated" in seen_regimes:
        print(f"{len(seen_regimes['unstated'])} of {measured_n} scored arm-B rows do not state the "
              f"baseline regime they were measured on;\nthe rest say {next(iter(stated))}. A row "
              f"that does not say cannot be shown to be comparable.")

    unfinished = sorted(t for t, _, _, _, st, *_ in rows if st in ("unfinished", "refused"))
    if unfinished:
        print(f"{len(unfinished)} task-arm(s) have no .done marker from campaign.sh and are still "
              f"OWED, so any score left behind for them is not reported: " + ", ".join(unfinished))
    print("\nspeedup = baseline_ms / optimized_ms, both timed here. 100% instance validity is "
          "required:\na 0.0000 means the solver was WRONG somewhere, not that it was not faster.\n"
          "That sentence is only true because a zero the ARENA is responsible for is printed as "
          "`--`\nwith its reason (see NOT_SOLVERS_FAULT); it was false about every such row before "
          "2026-08-23.")

    if args.reference:
        print("\nShipped reference models (AlgoTuner's loop driving OTHER models — context, not a "
              "control:\nthose rows differ from ours in the model AND were produced elsewhere).")
        for task, *_rest in rows:
            ref = _reference_models(summary, task)
            ours = {k: v for k, v in ref.items() if args.model_fragment.lower() in k.lower()}
            others = {k: v for k, v in ref.items() if k not in ours}
            if not others:
                continue
            best = max(others.items(), key=lambda kv: kv[1])
            print(f"  {task:<{width}}  best reference {best[0]} = {best[1]:.4f}  "
                  f"(median {sorted(others.values())[len(others) // 2]:.4f} over {len(others)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
