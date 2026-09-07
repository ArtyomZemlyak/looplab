#!/usr/bin/env python3
"""Live status of a running campaign: what has finished, what SCORED, and what merely finished.

`compare_arms.py` is the end-of-campaign report; this is the one to run while it is going.

THE DISTINCTION THIS EXISTS FOR
-------------------------------
A finished task-arm is not a scored one. AlgoTuner writes the WORDS `N/A` and `Error` into
`agent_summary.json` when it has no number, and an earlier version of this script counted those as
scores -- reporting "3/3 scored" for a set that contained one import failure.

`N/A` is also not a low score, and must never be read as one. Measured 2026-08-20:
`set_cover_conflicts` ended in 78 SECONDS with `reason: import_error` -- the agent wrote
`from scipy.optimize import integrality` (a parameter of `milp`, not an importable name), every
evaluation died instantly on that import so no 100-instance pass ever ran, and it burned the whole
$0.02 on model round-trips. That is "drowned in its own typo", not "solved it badly".

It also shows what a SPEND budget buys, which is worth stating in any methods note: the same $0.02
bought 178 minutes of work on a task whose code ran, and 78 seconds on one whose code did not.
Both arms pay that identically, so parity holds -- but "same budget" is not "same amount of work".

    python campaign_status.py --algotune-root /srv/AlgoTune [--out /root/benchmarks/campaign]
    python campaign_status.py --algotune-root /srv/AlgoTune --arm B      # reads arm B's own files

ARM B WAS READ OUT OF ARM A'S FILE, AND THAT IS WORSE THAN READING NOTHING
-------------------------------------------------------------------------
Until 2026-08-23 this script read `<algotune>/reports/agent_summary.json` for BOTH arms. Only arm A
writes that file -- `AlgoTuner/main.py::update_summary_json` -- so `--arm B` was looking up arm B's
task names in arm A's results. The rows are keyed by TASK and by the model name, and both arms run
the SAME model, so the lookup did not miss: it HIT, on the other arm's number.

Measured on `campaign-paired/` 2026-08-23, `--arm B` printed:

    kcenters               5.9454      # arm A's. Arm B scored 4.3635.
    edge_expansion         0.9852      # arm A's. Arm B scored 24.1928.
    integer_factorization  5.8271      # arm A's. Arm B scored 1.0025.
    discrete_log           0.9926      # arm A's. Arm B scored 1.0118.
    ... 14 more rows as "no number", every one of which HAS one in B-<task>.final.json

So the headline "arm B: 18 finished -> 4 SCORED" was four of arm A's scores under arm B's banner
and fourteen real arm-B scores discarded -- including the arm's best result, off by 24x in the
direction that loses arm B the comparison. A report that silently prints the other arm's number is
not a smaller version of a report that prints nothing; it is the only failure here that a reader
cannot detect by looking at it.

Arm B's number lives in `<out>/B-<task>.final.json` (the champion's TEST score, with a
`no_speedup.reason` block when there is none) and its run in `<runs-root>/<task>/run/events.jsonl`.
Those are read here, through `compare_arms.py`'s own readers rather than through a second copy of
them -- see `_compare_arms()`.

A MISSING `agent_summary.json` IS PRINTED, NOT RAISED. It used to be an unguarded `read_text()`, so
running this before arm A had finished its first task -- or on a box that only ever runs arm B --
ended in a `FileNotFoundError` traceback instead of the status the operator asked for.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _compare_arms():
    """`compare_arms.py`, imported by path — `benchmarks/` holds scripts, not a package.

    Three decisions live there and must not be re-made here, because a second copy of a rule is the
    drift: `_arm_b_final` (a zero the ARENA is responsible for is not a score -- it reads
    `no_speedup.reason` beside `speedup`), `marker_state` (what `campaign.sh` says about a task-arm,
    including the `wall_cut` a `.done` alone cannot express), and `_arm_a` (the "N/A"/"Error" words,
    the NaN/inf rejection, and the LAST-WRITER-DOES-NOT-WIN rule for a fragment that matches two
    model names). This file used to hold its own thinner spelling of the first and third, and the
    thinner spelling is how `--arm B` came to print arm A's numbers at all.
    """
    spec = importlib.util.spec_from_file_location("compare_arms_for_status", HERE / "compare_arms.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CA = _compare_arms()


def _tasks(out: Path, arm: str) -> list[str]:
    """Every task-arm this campaign has STARTED, not only the ones that reached a marker.

    A live campaign's interesting rows are the ones still in flight and the ones that stopped
    without a verdict; keying off `.done` alone hid both. `campaign.sh` opens `<arm>-<task>.log`
    before it runs anything, so the log is the evidence that a task-arm was attempted.
    """
    names: set[str] = set()
    for pattern, suffix in ((f"{arm}-*.done", ".done"), (f"{arm}-*.log", ".log"),
                            (f"{arm}-*.final.json", ".final.json")):
        for path in out.glob(pattern):
            names.add(path.name[len(arm) + 1:-len(suffix)])
    return sorted(names)


def _wall_minutes(out: Path, arm: str, task: str) -> int | None:
    """The wall clock out of the marker, or None when there is no marker to read it from."""
    marker = out / f"{arm}-{task}.done"
    try:
        fields = marker.read_text(encoding="utf-8").split()
    except OSError:
        return None
    for field in fields:
        if field.startswith("wall="):
            try:
                return int(field.split("=", 1)[1]) // 60
            except ValueError:
                return None
    return None


class _AsFile:
    """A `Path`-shaped stand-in that hands `compare_arms`' readers an ALREADY-PARSED payload.

    Both readers there take a path and `json.loads` it, which is right for a tool that runs once;
    this one polls and reads each file once per tick. The shim is four lines and keeps the RULES on
    one side of the fence — the alternative is re-implementing them here, which is the drift that
    made this file print arm A's numbers under arm B's banner in the first place.
    """

    def __init__(self, payload):
        self._payload = payload

    def read_text(self, *_a, **_k):
        return json.dumps(self._payload)

    def __truediv__(self, _other):
        return self          # `arm_a_failure` builds `root / "reports" / "agent_failures.json"`

    @classmethod
    def root_for(cls, payload):
        return cls(payload)


def _arm_a_number(summary: dict, failures: dict, task: str, fragment: str):
    """`(speedup, reason)` for arm A out of AlgoTuner's own summary. Arm A's file, arm A only.

    THE LOOKUP ITSELF IS `compare_arms`', not a second copy of it. This used to re-spell it as a
    first-match `next()` over the fragment-matching model rows, and the two then DISAGREED on the
    routine input: `agent_summary.json` holds several names for one model (`deepseek-v4-flash` and
    `deepseek-v4-flash-0731` both match the default fragment), and `_arm_a`'s documented
    LAST-WRITER-DOES-NOT-WIN rule keeps a real number that a trailing "N/A" row would otherwise
    overwrite. With an "N/A" under the short key and a real 1.42 under the long one, the end-of-
    campaign report scored the task and this live status printed it as failed.

    `_compare_arms`'s own docstring lists this among the decisions that "must not be re-made here,
    because a second copy of a rule is the drift" — so it is made there. The dict is still accepted
    (the caller reads the file once for every task) and `_arm_a` is fed through a tiny shim rather
    than a re-read, because its rule is over the ROWS and not over the file.
    """
    value = CA._arm_a(_AsFile({task: summary.get(task) or {}}), fragment).get(task)
    if value is not None:
        return value, ""
    raw = next((v.get("final_speedup") for k, v in (summary.get(task) or {}).items()
                if isinstance(v, dict) and fragment.lower() in str(k).lower()), None)
    # And the REASON through the same reader as the report's, so a merge-target file holding an old
    # campaign's row beside this one's answers with the newest match rather than with dict order.
    _fault, reason, _stamp = CA.arm_a_failure(_AsFile.root_for(failures), task, fragment)
    return None, str(reason or raw or "no record in agent_summary.json")


def _arm_b_number(out: Path, runs_root: Path, task: str):
    """`(speedup, reason)` for arm B out of arm B's own files.

    The TEST score first (`B-<task>.final.json`, through `compare_arms._arm_b_final` so a zero the
    arena is responsible for stays a non-number and keeps its reason). When there is none -- a
    task-arm still running, or one whose scoring pass has not happened yet -- the run's own champion
    metric is offered as CONTEXT and labelled `train`, never as the number. It is a TRAIN metric
    (every LoopLab node is evaluated on TRAIN, mirroring AlgoTuner's agent loop) and arm A's column
    is a TEST score, so putting the two in one column would compare two different splits.
    """
    final = out / f"B-{task}.final.json"
    if final.exists():
        value, why = CA._arm_b_final(final)
        if value is not None:
            return value, why
        return None, why or "no speedup in B-%s.final.json" % task
    train = CA._arm_b_train(runs_root / task / "run")
    if train is not None:
        return None, f"not scored yet; the run's own TRAIN champion is {train:.4f} (other split)"
    return None, "no B-%s.final.json and no run to read" % task


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--algotune-root", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=Path("/root/benchmarks/campaign"))
    ap.add_argument("--arm", choices=("A", "B"), default="A")
    ap.add_argument("--runs-root", type=Path, default=None,
                    help="Arm B's <task>/run/ directories. Defaults to <out>/../runs-B, the layout "
                         "compare_arms.py already assumes.")
    ap.add_argument("--model-fragment", default="v4-flash")
    args = ap.parse_args()

    runs_root = args.runs_root or (args.out.parent / f"runs-{args.arm}")
    summary: dict = {}
    failures: dict = {}
    if args.arm == "A":
        # ONLY ARM A. `agent_summary.json` is AlgoTuner's file and arm B never writes a row into it,
        # so reading it for arm B does not fail -- it silently answers with arm A's score for the
        # same task and the same model. See this module's docstring for the four rows that hit.
        reports = args.algotune_root.resolve() / "reports"
        summary_file = reports / "agent_summary.json"
        try:
            summary = json.loads(summary_file.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            # PRINTED, NOT RAISED: an arm A that has not finished its first task has not written
            # this file yet, and a traceback is not the status the operator asked for.
            print(f"no {summary_file} yet -- arm A has written no results. Every row below will "
                  f"read as 'no number'; that is the FILE missing, not the tasks failing.\n")
        except (OSError, ValueError) as exc:
            print(f"could not read {summary_file}: {exc}\n")
        failures_file = reports / "agent_failures.json"
        try:
            failures = json.loads(failures_file.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            failures = {}

    tasks = _tasks(args.out, args.arm)
    if not tasks:
        print(f"no {args.arm}-*.log, {args.arm}-*.done or {args.arm}-*.final.json under {args.out}")
        return 1

    scored, blank = [], []
    wall_cut, owed = [], []
    for task in tasks:
        if args.arm == "A":
            value, reason = _arm_a_number(summary, failures, task, args.model_fragment)
        else:
            value, reason = _arm_b_number(args.out, runs_root, task)
        # The SAME runs root this file already resolved for `_arm_b_number`, handed to the marker
        # reader rather than left to its sibling-name guess: `marker_state`'s ceiling rescue reads a
        # run's own event log, and pointing it at a directory the shipped `CAMPAIGN_RUNS` default
        # never produces made that rescue dead code.
        state = CA.marker_state(args.out, args.arm, task, runs_root)
        wall = _wall_minutes(args.out, args.arm, task)
        if state in CA.NOT_AT_BUDGET_STATES:
            # Delegated, not re-tested: `compare_arms.NOT_AT_BUDGET_STATES` is the one place that
            # says which states mean "terminal, but not a measurement at the budget" -- the two
            # harness cuts and, since 2026-09-06, an immediate exit. A membership test spelled here
            # would be the fourth copy of that rule, and the copies are what come apart.
            wall_cut.append(task)
        elif state in ("unfinished", "refused"):
            owed.append(task)
        if value is None:
            blank.append((task, wall, reason or "?", state))
        else:
            scored.append((task, value, wall, state))

    def _wall(minutes) -> str:
        return "  no marker" if minutes is None else f"{minutes:>6} min"

    print(f"arm {args.arm}: {len(tasks)} task-arm(s) attempted -> {len(scored)} SCORED, "
          f"{len(blank)} no number\n")
    for task, value, minutes, state in sorted(scored, key=lambda r: -r[1]):
        tag = "" if state == "done" else f"   [{state}]"
        print(f"  {task:<32}{value:>10.4f}{_wall(minutes)}{tag}")
    for task, minutes, reason, state in blank:
        tag = "" if state == "done" else f"{state}: "
        print(f"  {task:<32}{'--':>10}{_wall(minutes)}   ({tag}{reason})")

    if scored:
        # THE MEANS EXCLUDE WHAT THEY MUST, on the same rule `compare_arms.py` applies: a wall-cut
        # task-arm was stopped by a clock rather than by the budget every other row is compared at,
        # so it is shown and not averaged.
        comparable = [(t, v, w) for t, v, w, st in scored if st not in CA.NOT_AT_BUDGET_STATES]
        if comparable:
            values = sorted(v for _, v, _ in comparable)
            walls = [w for _, _, w in comparable if w is not None]
            print(f"\nscored: median {statistics.median(values):.4f}  "
                  f"range {values[0]:.4f}-{values[-1]:.4f}  (over {len(comparable)} at the budget)")
            if walls:
                print(f"wall per scored task: median {int(statistics.median(walls))} min")
    if blank:
        print(f"\n{len(blank)} task-arm(s) produced no number. That is NOT a zero and NOT a low "
              f"score -- see this file's docstring.")
    if wall_cut:
        print(f"{len(wall_cut)} task-arm(s) were STOPPED BY THE HARNESS (a wall cut or a stall cut) "
              f"or EXITED IMMEDIATELY, i.e. not at the budget, so they are shown and not averaged: "
              + ", ".join(sorted(wall_cut)))
    if owed:
        print(f"{len(owed)} task-arm(s) have no .done marker from campaign.sh and are still OWED: "
              + ", ".join(sorted(owed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
