"""What the engine MEASURES about a running stage from the text its training script prints.

The live-log watchdog (`engine/train_monitor.py`) asks its judge what a short TAIL can answer and
keeps "is it still descending" for the engine; this module is that engine half, shared by every
reader of the same bytes: the tail digest the judge is handed (`training_log_digest`), the
run-scale loss trajectory (`LossWindow` -> `LossTrajectory`, `LossTrajectoryTracker`,
`trajectory_context`, `trajectory_vetoes_kill`, `trajectory_row`), the SAME trajectory under the
inter-stage check (`trajectory_acquits_stage_check`), the stage's declared contract read live
(`schedule_reading`, `declared_schedule_shortfall`, `stage_contract_context`) and the projection
against the stage's own wall (`projected_overrun_s`, `stamp_projected_overrun`, `wall_unreachable`).

Every number here comes from text the CANDIDATE wrote, so the measurement may REFUSE an
intervention and may never authorize one — the trust note above `_LOSS_POINT_RE` is the rule, and
nothing here reaches a metric, a champion or a selection record. Pure: no engine, no model call,
no filesystem.

Split out of `engine/train_monitor.py` by review 2026-09-22 (ENG3-13, doc 50 EM-06), with
`engine/monitor_gates.py` (the decisions) and `engine/eval_log_plan.py` (which log is whose, and
the attempt-bounded readers). Moved VERBATIM, comments included. `train_monitor` re-exports every
name as the SAME object, so each existing spelling keeps resolving — but a patch aimed at
`train_monitor` does not reach a call made in here: patch the module that READS the name
(`tests/test_train_monitor_split.py` holds the suite to that).
"""
from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

# The median the trajectory reduces its windows with. It was a byte-identical private copy here and
# in `tools/log_tools.py`, which reduce the SAME log one trust tier apart (the deterministic veto's
# per-window median, and the judge-facing `metric_series` bucket median) — see `core/numeric.py`.
from looplab.core.numeric import median as _median
# The declared-contract reader below reads the stage's promise with `command_eval`'s OWN parser and
# tolerance, the ones `epoch_floor_acquits` applies at the end (see its SHORTFALL note).
from looplab.runtime.command_eval import DECLARED_EPOCH_TOLERANCE, declared_epoch_target

if TYPE_CHECKING:                  # an annotation only: the plan module imports this one
    from looplab.engine.eval_log_plan import StageDeclaration


def training_log_digest(text: str, *, max_lines: int = 40, max_chars: int = 4000) -> str:
    """Reduce a raw training-log tail to a compact digest that preserves the recent TRAJECTORY (the LLM
    context in Phase 1).

    Two kinds of repetition, handled differently:
    - A tqdm/epoch bar overwrites ONE line in place with carriage returns (no newline until it finishes),
      so within a newline-delimited record we keep only the LAST `\\r` segment — the bar's final rendered
      state — collapsing thousands of snapshots to one.
    - Distinct per-step log LINES ("step 1 loss: 0.5", "step 2 loss: 0.4", …) are separate newline
      records and are KEPT: their sequence IS the loss trajectory the monitor must reason over. We keep
      the last `max_lines` of them (the recent trend), then bound to `max_chars`.
    Pure and deterministic — no I/O — so it is unit-testable and safe to reuse anywhere."""
    if not text:
        return ""
    records: list[str] = []
    # normalize only the platform newline pair. Splitting each Windows `\r\n` record on
    # bare `\r` first made its final segment empty, silently disabling both watchdogs on Windows;
    # genuine standalone carriage-return progress redraws remain intact for the collapse below.
    for rec in text.replace("\r\n", "\n").split("\n"):
        seg = rec.split("\r")[-1].rstrip()   # in-place re-renders: keep the final rendered segment only
        if seg.strip():
            records.append(seg)
    out = "\n".join(records[-max_lines:])
    return out[-max_chars:] if len(out) > max_chars else out


# ------------------------------------------------------------- the TRAJECTORY the tail cannot carry
# WHY THIS EXISTS, measured on the live run `runs/rubertlite-dr-unified-v7` (2026-08-14).
#
# The digest above preserves the recent trajectory, and "recent" is far shorter than it reads. A
# tqdm bar line is ~330 characters, so `max_chars=4000` truncates the 40 kept records to about TEN,
# and those ten are the last ~30 seconds of a multi-hour run. Replayed against the real logs the
# judge received exactly this:
#
#   node 0: 11.0197 11.0355 11.0296 11.0316 11.0278 11.0399 11.0236 11.0552 11.0445 11.0410
#   node 1: 22.8906 22.9009 22.8881 22.8904 22.9011 22.9118 22.9024 22.8631
#
# and answered, correctly for what it was shown, "flat, no downward trend, possible plateau" — one
# of them `broken` at confidence 0.82, "pinned at ~23.0 ... showing no learning trend from its
# initialization value". Both nodes were learning: over their whole logs node 0 ran 15.73 -> 11.03
# and node 1 24.28 -> 22.90. The curves DECELERATE, so inside any short window the movement is
# below the step-to-step noise floor and "converged/stuck" is observationally identical to "still
# descending slowly". No reader of the last N lines can tell them apart — the question was
# unanswerable from the evidence, which is worse than an ambiguous verdict because it produces a
# CONFIDENT wrong answer.
#
# So the engine measures the trajectory itself, deterministically, and two things follow:
#   • the judge is HANDED that measurement (`trajectory_context`) alongside the tail, so its verdict
#     is formed over the run rather than over half a minute of it;
#   • "is it still descending" stops being the model's question at all — `trajectory_direction`
#     owns it, and `should_monitor_kill` refuses a kill the measurement contradicts.
#
# WHAT THIS MEASUREMENT IS AND IS NOT (docs/36 line). The numbers come from text the CANDIDATE
# wrote: the agent authors the training script and therefore authors the loss values an extractor
# reads. That is the same route around a protected stage `engine/metric_salvage.py` refuses to open,
# so this rung is held to the same rule — it may VETO an intervention and may never authorize one.
# `trajectory_vetoes_kill` returns only True/False for "refuse", nothing here can raise a verdict to
# `broken`, no value reaches the metric/champion/selection record, and the alert row carries the
# measurement as observation, not as authority. A candidate that forges a descending loss buys
# itself the right not to be killed early — which is precisely the pre-2026-08-14 behaviour for
# every log the plan could not prove was training, and the direction that costs GPU hours instead of
# discarding a healthy multi-hour run with no repair, no retry and no refunded `max_nodes` slot.

# The `loss` KEY only, never `eval_loss`/`train_loss`/`val_loss`: those are different series and
# interleaving them into one trajectory would manufacture the jumps this rule exists to distinguish
# from real movement. The negative lookbehind is what excludes them (`_` precedes the `loss` in
# `eval_loss`); an optional quote covers the `{'loss': 11.03, 'grad_norm': ...}` dict a HF Trainer
# prints and the bare `loss=0.5` / `loss: 0.5` a hand-rolled loop prints.
_LOSS_VALUE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[-+]?nan|[-+]?inf(?:inity)?"
_LOSS_POINT_RE = re.compile(
    r"(?<![A-Za-z0-9_])['\"]?loss['\"]?\s*[:=]\s*(" + _LOSS_VALUE + r")", re.IGNORECASE)
# The gradient norm is read for ONE purpose: `grad_norm: nan` is the earliest honest sign of a
# blown-up run and it appears while the printed loss is still a finite-looking `0.0` (measured on
# `runs/rubertlite-dr-unified-v6` node 5, the positive control — `{'loss': 0.0, 'grad_norm': nan}`
# four steps before `{'loss': 1.2217858750118953e+25}`). It is never treated as a loss point.
_GRAD_NORM_NONFINITE_RE = re.compile(
    r"(?<![A-Za-z0-9_])['\"]?grad_norm['\"]?\s*[:=]\s*([-+]?nan|[-+]?inf(?:inity)?)", re.IGNORECASE)
# A tqdm/Keras-style `done/total` counter. Only used to report HOW FAR THROUGH the run this is, which
# is the one thing a plateau reading most needs and the tail states nowhere: "flat at epoch 21 of 50"
# and "flat at epoch 49 of 50" are different facts about the same numbers.
_PROGRESS_RE = re.compile(r"(?<![\d./])(\d{1,9})\s*/\s*(\d{1,9})(?![\d.])")
# ONE record, whichever way the writer ended it. A tqdm bar rewrites its line with `\r` and writes
# no `\n` for hours, so splitting on newlines alone puts a whole multi-hour run in one record —
# the same rule `tools/log_tools.py` and `runtime/sandbox._StageHealthMonitor` already keep, and
# the reason `schedule_reading` can pair a step counter with the epoch printed beside it.
_BREAK_RE = re.compile(r"[\r\n]")

# An observed value at least this many times the run's own opening scale is an EXPLOSION, not a
# reading of the same curve. Deliberately generous: the point is to notice `1.2e25` beside `63.8`,
# never to adjudicate a 3x spike, and its only effect is to withdraw the veto (see `_anomaly_of`).
_TRAJECTORY_EXPLOSION_RATIO = 100.0
# A net drop must clear BOTH the measured step-to-step noise floor and this fraction of the opening
# level. The noise floor is the real test; the relative floor only stops a numerically-tiny drift on
# a quiet log from reading as progress. Node 1's 5.8% and node 0's 30% clear it by three orders.
_TRAJECTORY_MIN_RELATIVE_DROP = 0.001
# ...and the floor BOTH of the other two collapse to zero at, which is not a hypothetical: a window
# of `loss: 0.0` values has a masd of 0.0 (the noise floor) AND an opening scale of 0.0 (the relative
# floor), so `net > floor` became `net > 0` and an epsilon of drift read as `descending`. Driven:
# windows of `loss: 0.0` then `loss: -1e-9` answered `direction='descending', net=1e-09, noise=0.0`,
# and a descending trajectory VETOES every `broken` verdict for the rest of the node — so a
# degenerate window bought a multi-hour node permanent immunity from the kill it exists to allow.
# `{'loss': 0.0, 'grad_norm': nan}` is the exact shape of this module's own positive control
# (`runs/rubertlite-dr-unified-v6` node 5); only the `grad_norm: nan` beside it rescued that case,
# through `_anomaly_of`, and a run that prints the zero without the nan had nothing.
#
# 1e-6 is chosen so it is INERT wherever either real floor has anything to say: the relative floor is
# already >= 1e-6 for any opening scale >= 1e-3, i.e. for every run in `runs/` and for any loss a
# 4-decimal logger (the HF Trainer rounds its logged loss to 4 places) can even express a movement
# in. It binds only where the opening median is below 1e-3 AND the within-window noise is below 1e-6
# — a curve at that scale has no legible direction, which is what `flat` means.
#
# Raising the floor can only ever turn `descending`/`rising` into `flat`, i.e. WITHDRAW a veto and
# never mint one, which is the only direction this measurement is allowed to move in (see the trust
# note above `_LOSS_POINT_RE`): `flat` is not evidence FOR a kill, it is the absence of evidence
# against one, so the judge's own verdict decides again exactly as it did before the veto existed.
_TRAJECTORY_MIN_ABSOLUTE_DROP = 1e-6
# Bounded history: one window per tick, and the cadence + `_MAX_MONITOR_LLM_CALLS` already bound a
# node to ~200 ticks. Retained as a deque so a pathological run cannot grow this without limit; the
# summary reads only the FIRST and LAST windows plus a median, so dropping the middle of an
# overlong history would change nothing that matters, and dropping the oldest would.
_MAX_TRAJECTORY_WINDOWS = 512


@dataclass(frozen=True)
class LossWindow:
    """ONE tick's tail, reduced to the four facts a trajectory needs. Immutable and JSON-safe.

    `masd` is the median absolute successive difference WITHIN this window — the step-to-step noise
    floor, i.e. exactly how much the loss moves between adjacent logged steps for no reason. It is
    measured per window and never across windows, because consecutive windows are ~10 minutes apart
    and the jump between them is signal, not noise.

    Every numeric field is Optional and `count` may be 0: a window whose only loss values are
    non-finite has no numbers to summarize and MUST still exist, because its `nonfinite` count is
    the anomaly signal. Dropping it (as this dataclass did before its non-finite-only case was
    driven) silently withdrew the positive control — a log printing nothing but `loss: nan`
    contributed no window at all, so no anomaly was ever seen.
    """

    median: Optional[float]
    masd: Optional[float]
    count: int
    first: Optional[float]
    last: Optional[float]
    minimum: Optional[float]
    maximum: Optional[float]
    nonfinite: int = 0
    progress_done: Optional[int] = None
    progress_total: Optional[int] = None
    at: Optional[float] = None


@dataclass(frozen=True)
class LossTrajectory:
    """What the engine MEASURED about the loss over the whole observed run, handed to the judge as
    context and consulted by `should_monitor_kill` as a veto. Pure data; every field derived.

    `direction` is the answer to the question the tail cannot answer:

    - ``"descending"`` — the run's opening window sits above its latest by more than the measured
      noise floor, more than `_TRAJECTORY_MIN_RELATIVE_DROP` of the opening level, AND more than
      `_TRAJECTORY_MIN_ABSOLUTE_DROP` (the floor the first two both collapse to zero at, on a window
      of `loss: 0.0`), so the loss is demonstrably not stuck at its initialization value;
    - ``"rising"`` — the same test in the other direction (divergence);
    - ``"flat"`` — the net movement does not clear the floor: genuinely converged, genuinely stuck,
      or too early to tell apart. This rule deliberately does not choose between those three, which
      is why `flat` is not evidence FOR a kill, only the absence of evidence against one;
    - ``"unknown"`` — fewer than two windows, or no numeric loss in the log at all.
    """

    windows: int = 0
    points: int = 0
    first: Optional[float] = None
    last: Optional[float] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    noise: Optional[float] = None
    net: Optional[float] = None
    direction: str = "unknown"
    anomaly: str = ""
    progress_done: Optional[int] = None
    progress_total: Optional[int] = None
    span_s: Optional[float] = None
    # HOW LONG THIS STAGE STILL HAS, in seconds — the run's own step rate extrapolated to its own
    # declared total. `None` whenever the log has not said enough to answer (see `_eta_of`).
    #
    # WHY IT IS WORTH RECORDING AT ALL. Nothing in this engine knew how long anything would take:
    # `_resource_envelope` carries a GPU count and memory and no time at all, and a search for
    # `eta` / `predicted_duration` / `estimated_seconds` across `engine/` and `search/` finds
    # nothing. Every scheduling question an operator asks — "can a second experiment fit beside this
    # one?" — needs this number and could not be asked.
    #
    # MEASURED on the two e5 nodes that finished under this monitor: the step-rate figure settles
    # almost immediately (node 3 predicted 6.90 h at step 20 and 6.94 h at step 936; node 4 gave
    # 8.74 h and 8.76 h at the same points) and UNDER-states the truth by 4-5 % (actuals 7.28 h and
    # 9.13 h), because it counts training steps and not the tail — the in-process test, the
    # checkpoint write, the score stage. That bias is one-directional and therefore correctable, but
    # NOT from two samples: this field records the RAW extrapolation, `node_evaluated.eval_seconds`
    # already records the truth, and the pair accumulates until the correction can be measured
    # instead of guessed.
    eta_s: Optional[float] = None

    @property
    def anomalous(self) -> bool:
        """Whether the numbers themselves carry evidence a TAIL can legitimately act on — a
        non-finite loss/grad-norm or an explosion. Such a run is not 'descending' in any sense the
        veto should protect, so the veto stands down and the model's `broken` verdict is left to
        act (`runs/rubertlite-dr-unified-v6` node 5 is the worked case)."""
        return bool(self.anomaly)


def parse_loss_points(text: str) -> tuple[list[float], int]:
    """Every `loss:`/`loss=` value in `text`, in order, split into the FINITE ones and a count of
    the non-finite ones. Pure/deterministic — no I/O.

    Non-finite values are counted rather than kept: `nan` poisons every comparison it touches (see
    `_normalize_monitor_confidence` for the same trap one field over), and their presence is itself
    the signal — one is enough, their magnitude means nothing."""
    finite: list[float] = []
    nonfinite = 0
    for match in _LOSS_POINT_RE.finditer(text or ""):
        try:
            value = float(match.group(1))
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value):
            finite.append(value)
        else:
            nonfinite += 1
    if _GRAD_NORM_NONFINITE_RE.search(text or ""):
        nonfinite += 1
    return finite, nonfinite


def _latest_progress(text: str) -> tuple[Optional[int], Optional[int]]:
    """The LAST plausible `done/total` counter in the text, or (None, None).

    Last, not first, because the digest ends at the newest rendered progress bar. `done <= total`
    and a non-zero total are required so a date, a version or a ratio elsewhere in the line cannot
    be reported to the judge as the run's position."""
    done = total = None
    for match in _PROGRESS_RE.finditer(text or ""):
        try:
            a, b = int(match.group(1)), int(match.group(2))
        except (TypeError, ValueError):
            continue
        if b > 0 and 0 <= a <= b:
            done, total = a, b
    return done, total


def summarize_loss_window(text: str, *, at: Optional[float] = None) -> Optional[LossWindow]:
    """Reduce ONE tick's digest to a `LossWindow`, or None when it names no loss at all.
    Pure/deterministic.

    A window whose loss values are ALL non-finite is kept with `count=0` and no numbers — see
    `LossWindow`: it carries the anomaly, which is the one thing a tail can decide by itself."""
    values, nonfinite = parse_loss_points(text)
    if not values and not nonfinite:
        return None
    done, total = _latest_progress(text)
    if not values:
        return LossWindow(median=None, masd=None, count=0, first=None, last=None,
                          minimum=None, maximum=None, nonfinite=nonfinite,
                          progress_done=done, progress_total=total, at=at)
    diffs = [abs(b - a) for a, b in zip(values, values[1:])]
    return LossWindow(
        median=_median(values), masd=_median(diffs) if diffs else 0.0, count=len(values),
        first=values[0], last=values[-1], minimum=min(values), maximum=max(values),
        nonfinite=nonfinite, progress_done=done, progress_total=total, at=at)


def _anomaly_of(rows, numeric) -> str:
    """The one-phrase reason the numbers are not a curve to be protected, or ''. Pure.

    Two rungs, both decided on VALUES rather than on prose: any non-finite loss or grad-norm
    (over EVERY window, numeric or not), and a value at least `_TRAJECTORY_EXPLOSION_RATIO` times
    the run's opening scale. Both are evidence a single tail genuinely carries, which is the whole
    point — the veto exists because a tail cannot see a slow descent, not because a tail can see
    nothing."""
    if any(window.nonfinite for window in rows):
        return "non-finite loss or grad_norm"
    if not numeric:
        return ""
    opening = abs(numeric[0].median)
    scale = opening if opening > 0 else 1.0
    if any(abs(window.maximum) >= _TRAJECTORY_EXPLOSION_RATIO * scale for window in numeric):
        return "loss exploded far beyond its opening scale"
    return ""


def _eta_of(rows) -> Optional[float]:
    """Seconds of stage remaining, from the run's OWN observed step rate. `None` when unanswerable.

    Derived from the two windows the tracker already keeps rather than from a new parse:
    `progress_done` / `progress_total` / `at` are collected on every tick for the progress line, so
    this costs nothing and cannot drift from what the trajectory reports.

    Refuses rather than guesses, in FOUR ways, because a wrong ETA is worse than none for anything
    that would schedule on it: a missing progress pair, a non-positive total, no forward motion
    between the ends (a stalled or restarted counter), or a non-positive span. `done >= total`
    answers 0.0 rather than a negative — a bar at or past its own total is finishing, not overdue.

    THERE IS NO SEPARATE "FEWER THAN TWO WINDOWS" CHECK, and its absence is deliberate. One was
    written here first, to mirror the rule `summarize_trajectory` applies to `direction`. Mutation
    testing showed it could not fail: with a single window `first` and `last` are the same object,
    so `advanced == 0` and the forward-motion guard below already answers None. A guard that cannot
    fail is the vacuous shape this repo keeps finding, so it is gone rather than propped up by a
    test that could not discriminate it either.
    """
    if not rows:
        return None
    last = rows[-1]
    # THE TWO ENDS OF THIS RATE MUST COME FROM ONE PROGRESS BAR, and until 2026-08-30 nothing
    # checked it: `done_a` was taken from `rows[0]` and `total` from `rows[-1]`, so a tick that
    # landed during an in-epoch validation ended on the val bar (total ~361) while its neighbours
    # ended on the train bar (total ~10,590), and `advanced` was a difference between two unrelated
    # counters. `_latest_progress` reports the LAST counter in a tick's tail, whichever lane
    # rendered it, and 109 of the 109 stage logs above 200 KB on this box carry more than one lane —
    # so the mixed pairing was the ROUTINE case, not a corner. `schedule_reading` below already
    # refuses exactly this by its ONE-RECORD rule and quotes the same measurement.
    #
    # MOST MIXES ONLY DEFLATE the ETA, which is the conservative direction, but the dangerous one is
    # real: a first window ending on a near-complete eval-on-start / sanity-check bar and a last
    # window on a young train bar yields a small positive `advanced` over a real span, overstating
    # the per-step time. That now matters more than when the note was written — since `ac189252` a
    # beyond-grace `projected_overrun_s` OPENS the durable write gate on its own, so an inflated ETA
    # no longer merely decorates an existing row, it can mint one about a stage that fits.
    #
    # THE ANCHOR IS THE LATEST EARLIER WINDOW ON THE SAME LANE, and windows on OTHER lanes in
    # between are SKIPPED rather than treated as a wall. A first cut walked back only through the
    # contiguous same-lane run and stopped at the first foreign total, which is same-lane but
    # measurably too strict: an in-epoch validation interlude is the ordinary shape here, so that
    # rule answers None for most real runs and would withdraw the very coverage `projected_overrun_s`
    # now leans on. Skipping the interlude keeps the pair on one bar AND keeps the span long; the
    # train bar merely pauses while the val bar renders, so the wall time between two train-bar
    # windows includes the validation and the rate comes out DEFLATED — the conservative direction
    # this function already prefers.
    #
    # A COUNTER THAT MOVED BACKWARD IS A RESTART, not a rate. An epoch bar re-rendering from 0
    # shares its total with the bar before it, so equal totals alone do not prove one continuous
    # count; when the nearest same-lane window is AHEAD of the last one, everything older belongs to
    # a previous cycle and the honest answer is None rather than a scan further back.
    #
    # ANSWERS None WHEN NO EARLIER WINDOW SHARES THE LANE, rather than falling back to a mixed pair:
    # a wrong ETA is worse than none for anything that schedules on it, which is this function's
    # stated rule and, since `ac189252`, its gate's too.
    total, done_b = last.progress_total, last.progress_done
    if not (type(done_b) is int and type(total) is int and total > 0):
        return None
    anchor = None
    for window in reversed(rows[:-1]):
        if type(window.progress_total) is not int or window.progress_total != total:
            continue                   # a different bar rendered this tick's tail — skip it
        if type(window.progress_done) is not int or window.progress_done > done_b:
            return None                # the counter restarted: this is not one continuous count
        anchor = window
        break
    if anchor is None:
        return None
    done_a = anchor.progress_done
    if anchor.at is None or last.at is None:
        return None
    span, advanced = last.at - anchor.at, done_b - done_a
    if span <= 0 or advanced <= 0:
        return None
    remaining = total - done_b
    if remaining <= 0:
        return 0.0
    return remaining * (span / advanced)


def summarize_trajectory(windows) -> LossTrajectory:
    """Reduce the observed windows to the run-scale trajectory. Pure/deterministic — the whole
    "is it still descending" decision is this one function plus `_anomaly_of`, so it has a truth
    table (`tests/test_train_monitor_trajectory.py`) instead of being reachable only through a
    simulated multi-hour eval."""
    rows = [w for w in windows if isinstance(w, LossWindow)]
    if not rows:
        return LossTrajectory()
    # Only windows that actually carry NUMBERS can state a direction; a non-finite-only window still
    # counts as an observation and still carries its anomaly (see `summarize_loss_window`).
    numeric = [w for w in rows if w.count]
    points = sum(w.count for w in rows)
    anomaly = _anomaly_of(rows, numeric)
    last_seen = rows[-1]
    common = dict(
        windows=len(rows), points=points, anomaly=anomaly,
        progress_done=last_seen.progress_done, progress_total=last_seen.progress_total,
        span_s=((last_seen.at - rows[0].at)
                if last_seen.at is not None and rows[0].at is not None else None),
        eta_s=_eta_of(rows),
    )
    if not numeric:
        return LossTrajectory(direction="unknown", **common)
    last = numeric[-1]
    common.update(
        first=numeric[0].first, last=last.last,
        minimum=min(w.minimum for w in numeric), maximum=max(w.maximum for w in numeric),
        noise=_median([w.masd for w in numeric]),
    )
    if len(numeric) < 2:
        # ONE window is a tail by another name — the exact evidence this module exists because the
        # judge cannot decide on. Report the numbers, refuse the direction.
        return LossTrajectory(net=None, direction="unknown", **common)
    # Window MEDIANS, not their endpoints: an endpoint is one sample and carries the full
    # step-to-step scatter, while the median of ~10 samples is what makes a sub-noise drift legible.
    net = numeric[0].median - last.median
    # THREE floors, and the third is the one that keeps the other two from both being 0.0 — see
    # `_TRAJECTORY_MIN_ABSOLUTE_DROP` for the degenerate `loss: 0.0` window that made `net > floor`
    # into `net > 0` and handed a node a permanent veto over its own kill.
    floor = max(common["noise"], abs(numeric[0].median) * _TRAJECTORY_MIN_RELATIVE_DROP,
                _TRAJECTORY_MIN_ABSOLUTE_DROP)
    direction = "descending" if net > floor else ("rising" if net < -floor else "flat")
    return LossTrajectory(net=net, direction=direction, **common)


# ------------------------------------------- the SAME trajectory, under the inter-stage stage check
# WHY THIS EXISTS, re-derived on `runs/rubertlite-dense-retrieval` (2026-08-20).
#
# The stage check (`eval_stages.py::_stage_check_fn`, decided in `command_eval.py::_run_stages`) is
# asked whether a `check`-flagged stage physically succeeded, and one member of its closed verdict
# vocabulary is `loss_unchanged_from_first_step` — "a loss LITERALLY UNCHANGED from the first
# training step (genuinely no learning)". What it is HANDED to answer that is `run.out[-4000:]`, and
# `run.out` is ITSELF already `sandbox._clamp_tail_bytes(out, 64_000)`. Two nested tail clamps: the
# first training step is not in the window and structurally cannot be, so the question the
# vocabulary asks is not answerable from the evidence the engine supplies.
#
# The corpus says what that costs. Sixteen `node_failed` rows in that run carry `reason: no_metric`
# from a stage check; TEN of those nodes were later reset by the operator, came back with the train
# stage `reused` at `seconds 0.0` — the very checkpoint the checker had condemned — and SCORED
# 0.805, 0.8412, 0.8424, 0.8379, 0.8606, 0.8265, 0.8376, 0.8662, 0.8531, 0.8147 against a run best
# of 0.8835, i.e. 0.91x-0.98x of best. Node 1's own `train.log` runs `loss=33.9` -> `loss=13.3` over
# 11,248 logged points in 1,214,400 bytes; its last 4,000 characters contain THREE of those points
# and all three read `13.3`, which is what "Loss stagnant at 13.3 throughout epoch 19, indicating no
# learning progress" is a correct reading of. A converged curve's tail is flat, and flat-at-the-end
# is indistinguishable from never-moved when the end is all you are shown.
#
# So this is the same defect `_LOSS_POINT_RE`'s block above describes for the live monitor, one
# decision over, and it gets the same answer: the engine measures the trajectory itself over the
# whole of THIS attempt's stage log and the measurement VETOES the refusal. Reused wholesale —
# `summarize_loss_window`, `summarize_trajectory`, `_anomaly_of`, `attempt_byte_floor`,
# `eval_log_plan` — because a second reader of these bytes that reduced them differently could
# disagree with the monitor about the same curve, which is the failure `core/numeric.median` was
# extracted to prevent.
#
# WHY THE PREDICATE IS "MOVED" AND NOT "DESCENDING". The kind names ONE property — unchanged from
# the first step — so what refutes it is MOVEMENT, in either direction. `rubertlite-dense-retrieval`
# node 22 is the case that decides it: its loss runs 18.9 -> 17.6 and then climbs smoothly to 32.6
# and plateaus, so the tail reads `32.0` for thousands of points and the checker wrote "Loss remains
# constant at 32 across all epochs". The trajectory reads `rising` — emphatically not unchanged —
# and that node scored 0.8147. A `descending`-only rule keeps 9 of the 10; this keeps all 10.
#
# WHAT IT MAY AND MAY NOT DO, the docs/36 line, identical to the epoch floor's. The numbers come
# from text the candidate's own training script wrote, so this may only ever ACQUIT: it moves the
# verdict DOWN to `inconclusive` and can never raise one, never fail a stage, and never touch the
# other hard kinds — `nan_or_inf_loss`, `crash`, `no_artifact_written`, `silent_fallback`,
# `declared_condition_violated` — which are out of its reach BY NAME. The four nodes in that corpus
# the run condemned as diverged (n15 `loss=inf` for 20 epochs, n60 `nan`, n68 `-2e+10`, n74
# `-2.35e+08`) name `nan_or_inf_loss` and are refused by the kind test before any curve is read;
# `anomalous` is the second, independent refusal for a diverged run the model happened to label the
# other way.
#
# **"THE FOUR GENUINELY DIVERGED NODES" IS THE PART OF THIS THAT WAS WRONG, and the correction is
# what decides the paragraph below** (re-derived 2026-08-20 from the preserved logs). n74 is not
# diverged. Sampled at the same fractions of its own log, its curve and the curve of n48 — the run's
# CHAMPION at 0.8835 — agree to three significant figures at every point: both open at
# `loss=-2.44e+06` on step 1, pass -2.72e+07/-2.73e+07 at 2 %, -1.81e+08/-1.80e+08 at 25 %, and both
# END at -2.32e+08. That family's loss legitimately runs to ~2.5e8: measured over all 249 stage logs
# on this box, 28 reach |loss| >= 1e8 and 26 of them produced a metric, sixteen of those above 0.87.
# n39 opens at a friendly `10.2`, ends at `-9.74e+08`, and scored 0.8654. What condemned n74 was an
# end-of-stage LLM checker reading a big negative number, i.e. the same heuristic a magnitude rung
# would be.
#
# DECLINED[explosion-rung-cannot-be-magnitude-symmetric] measured: n74 5.64x / n48 (champion,
# 0.8835) 5.65x, peaks 2.54e+08 vs 2.53e+08; n39 127,626,459x and scored 0.8654; n68 1.00x —
# docs/47-early-stop-blind-classes-2026-08-20.md
#
# Those ratios are `_anomaly_of`'s own arithmetic over one 20-tick windowing of each whole log, and
# 28 of the 249 stage logs on this box reach |loss| >= 1e8 with 26 of them producing a metric, 16
# of those above 0.87.
#
# Making the explosion rung read the MAGNITUDE at both ends of a window, or adding an absolute
# |loss| bar beside the ratio, is refused permanently rather than postponed. The BUG IS REAL:
# `abs(window.maximum)` is the SIGNED max, so for an all-negative loss it inspects the value
# NEAREST zero, and driven on the preserved logs n74 measures `direction=descending, anomaly=''`,
# so `trajectory_vetoes_kill` returns True and that node was IMMUNE to every `broken` verdict for
# the rest of its life. Correcting it is measurably worse than leaving it, and every variant fails
# on a DIFFERENT node of the same four:
#   * a MAGNITUDE bar cannot separate n74 (peak 2.54e+08) from n48 (2.53e+08) at any value;
#   * the RATIO cannot either (5.64x vs 5.65x, and the champion is higher) — and it already fires
#     on n39, which runs 7.71 -> -9.84e+08 and scored 0.8654, i.e. the shipped 100x boundary is
#     ALREADY a false positive waiting on that node's shape;
#   * neither can ever see n68, the one node that is plausibly broken on its own terms
#     (`rdrop_loss` collapses to 0.000), because it opened at -1.5e+10 and never moved: 1.00x.
# An `anomaly` can only make things END — it withdraws `trajectory_vetoes_kill`'s protection AND
# blocks `trajectory_acquits_stage_check` — so a bar that catches n74 kills n48. The ratios above
# are windowing-dependent (a different tick schedule gives different window medians), which is why
# they are quoted WITH their derivation and why the older `96x` figure a few lines up is not
# reproducible from any stated method; what does not move is the ORDERING, and the ordering is what
# refuses the rung.
#
# WHERE THAT SECOND REFUSAL DOES NOT REACH, measured rather than assumed. Replaying the veto with
# EVERY loss concern in that corpus forced to `loss_unchanged_from_first_step` — the maximum
# exposure, i.e. the model naming the wrong kind for a diverged run — n74 is acquitted: its loss
# runs -2.44e+06 -> -2.35e+08, which is `descending` in signed terms, and `_anomaly_of`'s explosion
# rung reads `window.maximum` (the least-negative value) so a run diverging NEGATIVE measures 96x its
# opening scale against a 100x boundary. Left as it is on purpose: lowering
# `_TRAJECTORY_EXPLOSION_RATIO` or making the rung symmetric would move the LIVE monitor's veto —
# a different decision, with its own corpus — and 96x is under the boundary either way. What a wrong
# acquittal costs here is the `score` stage on a model whose metric is then recorded near zero, which
# is the same cost the paragraph below prices, and the kind test already refuses this row for real.
#
# THE ASYMMETRY IT IS CHOSEN ON, from the same corpus and stated as a cost rather than a preference:
# a wrong "no progress" ended TEN nodes, one of them within 2 % of the run's best, with no repair, no
# retry and no refunded `max_nodes` slot, at 1,570-4,344 stage seconds each. A wrong "keep going"
# runs the remaining stages and is caught by the real metric — 65-67 s of `score` on those same
# nodes, after which the number the search ranks on is the operator's own reader over the protected
# `score` stage. Two orders of magnitude, and only one of the two is recoverable, so the uncertain
# case may not be a kill.
#
# THE ONE CASE NO LOSS-ONLY RULE CATCHES, named rather than papered over. Node 12 of that run is the
# single genuine `not_learning` in the whole 122-row `failure_triage.v1` corpus: its loss fell
# 0.986 -> 0.0195 while validation recall@100 stayed at 0.0028. The loss MOVED, so this rule acquits
# it, and correctly — it is answering "was the loss unchanged", which is false. "The loss fell and
# the model still did not learn" is a different question in kind: it needs the OBJECTIVE METRIC, and
# the stage check runs BEFORE the protected `score` stage that produces it, so the evidence does not
# exist yet at this decision. Acquitting node 12 costs one `score` stage and the metric then records
# 0.0028, which is exactly the reader the record is supposed to rest on. That is not a rule this
# function is missing; it is a rule that belongs downstream of it.
STAGE_CHECK_TRAJECTORY_KIND = "loss_unchanged_from_first_step"
# The directions that REFUTE "unchanged from the first step". `flat` does not (it is the absence of
# evidence either way — see `LossTrajectory.direction`) and neither does `unknown`, so both leave the
# refusal standing, which is the pre-veto behaviour exactly.
TRAJECTORY_MOVED_DIRECTIONS = ("descending", "rising")


# ---------------------------------------------------------------------------------------------
# THE DECLARED CONTRACT, READ LIVE.
#
# Everything above this line watches the CURVE, and measured over the committed 450-decision bench
# that is the only failure the judge sees: on the 53 decisions whose node trained fine and scored
# ~0 (`node_metric_degenerate`) the recorded judge said `broken` 48 times (91 %), and on the 38
# whose stage EXITED 0 and was then failed by the engine on its own declared contract it said
# `broken` twice (5 %). Those 38 are not a prompt problem. The stage really did train; what failed
# was the promise it made about itself — and the engine has held that promise, in
# `stage.expect.assert`, since before the stage started, and never showed it to the judge.
#
# Re-derived 2026-08-20 over the four `check_failed` attempts the bench records as missed, ALL of
# them `declared_condition_violated`: three declared an epoch count the trainer's own configuration
# could never reach, and each one ECHOED that configuration in the first 30 KB of a multi-hour log —
# `"n_epochs": 8` against a declared 15 (v8 node 8, 14,105 s), `"n_epochs": 6` against 10 (v8 node
# 9, 8,355 s), `"n_epochs": 1` against 50 (v9 node 1, 3,933 s and again 4,001 s). The engine paid
# 8.2 h to reach, at the end, a conclusion its own manifest made available at the start.
#
# WHAT THIS IS AND IS NOT. It is EVIDENCE: two more sentences in the user message of a call the
# monitor already makes, at the same splice position as `stage_context` and `trajectory_context`,
# costing zero extra provider calls. It is NOT authority — `should_monitor_kill`'s conjuncts,
# `should_monitor_repair`'s, the kill-eligible roles and the deterministic trajectory veto are all
# untouched, and nothing here can mint a metric, a champion, a violation or a selection (docs/36).
#
# THE CLASS THIS DOES NOT REACH, named rather than papered over. 4.96 h of that 20.1 h sits in
# the 11 attempts that CRASHED (exit 1), and the reason the judge sees ~5 % of them is NOT that
# the traceback is in another file — `runtime/sandbox.py::_tee_drain` runs one pump per stream
# into ONE `<stage>.log`, and all six crash logs checked (v9 n0/n6, v2 n2/n7, v8 n3 `mine.log`,
# v8 n10) hold the traceback where the judge already reads. A crash is SELF-TERMINATING: the
# process is dead by the time the traceback exists and `_triage_crash` acts on it at exit, so
# that 4.96 h is only reachable if the crash was PREDICTABLE from earlier output.
# OPEN[crash-predictability-unmeasured] nobody has measured whether it was. The measurement is
# where in each attempt's OWN byte range (`attempt_byte_floor` to the end) the first traceback
# lands: a traceback in the last 1 % means nothing was saveable and this class closes as a
# refusal, while a broad distribution means there is a rung here. It belongs in the bench
# corpus builder beside the labels it would qualify, because a claim about lead time has to be
# re-derivable from the same dataset as the claim about the miss.
# proof:absent:crash_lead_time@looplab/judgebench/judge_corpus.py
#
# The SHORTFALL reading below shares `command_eval`'s parser and `command_eval`'s tolerance on
# purpose. `epoch_floor_acquits` is the same fact read at the END, and the pair must not be able to
# disagree: a stage the floor would ACQUIT (`rubertlite-dr-unified-v9` node 0, whose trainer
# reported 14.87 of a declared 15 because HF sizes `max_steps` from a floored updates-per-epoch)
# must not be reported live as short. Driven: over that node's 11 recorded decisions this reads a
# ceiling of 14.87-14.92 and says nothing, on all 11.
_SCHEDULE_EPOCH_RE = re.compile(
    r"(?<![A-Za-z0-9_])['\"]?epoch['\"]?\s*[:=]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
# The reported epoch is rounded (the HF Trainer logs 2 decimal places), and projecting it to the end
# of the schedule multiplies that rounding by `total/done`. Refuse to read a ceiling whose own
# half-width is at or above half an epoch: at that resolution "short by one epoch" is not a
# statement the numbers support. Half an epoch and not a tenth because the SHORTFALL bar is a whole
# declared epoch (`DECLARED_EPOCH_TOLERANCE`), so this is the point at which the two can collide.
_SCHEDULE_MAX_HALF_WIDTH = 0.5


@dataclass(frozen=True)
class ScheduleReading:
    """What the trainer's own progress records say its FULL schedule is, in epochs.

    `epochs` is `epoch * total / done` — the trainer reports both halves itself, and their ratio is
    the only thing here that is derived. Immutable and JSON-safe.
    """

    epochs: float
    done: int
    total: int
    epoch: float


def schedule_reading(text: str) -> Optional[ScheduleReading]:
    """The epoch ceiling of the schedule the trainer actually configured, or None. Pure.

    THE PAIR MUST COME FROM ONE RECORD, and that is the whole subtlety. Measured 2026-08-15, 109 of
    the 109 stage logs in `runs/` above 200 KB carry more than one progress-bar lane, so taking "the
    last `k/N` seen" and "the last `epoch:` seen" independently pairs a step counter with an epoch
    from a DIFFERENT bar. Driven on the committed bench: the loose pairing reads
    `rubertlite-dr-unified-v8` node 13 — a completed 10-epoch training that scored 0.716575 — as a
    4.02-epoch schedule, by pairing a finished `313/313` dataloader bar with a training log dict.
    That is one false stop against a run whose champion is worth ~9 h, and the same-record rule
    takes it to zero: over all 450 recorded decisions this fires on 12, all of them `wasted`.

    Everything it reads is text the candidate's own training script wrote, exactly like the loss
    trajectory one section up, and like that measurement it is REPORTED, never acted on.
    """
    best = None
    for record in _BREAK_RE.split(text or ""):
        match = None
        for match in _PROGRESS_RE.finditer(record):
            pass                                  # the LAST counter in the record wins
        if match is None:
            continue
        done, total = int(match.group(1)), int(match.group(2))
        if not (total > 0 and 0 < done <= total):
            continue
        found = _SCHEDULE_EPOCH_RE.search(record)
        if found is None:
            continue
        try:
            epoch = float(found.group(1))
        except (TypeError, ValueError):            # pragma: no cover — the group is a float literal
            continue
        if epoch <= 0:
            # `epoch: 0.0` is the first few logged steps of ANY schedule and divides to nothing.
            continue
        if 0.005 * total / done > _SCHEDULE_MAX_HALF_WIDTH:
            continue
        best = ScheduleReading(epochs=epoch * total / done, done=done, total=total, epoch=epoch)
    return best


def declared_schedule_shortfall(assertion: str, text: str) -> Optional[tuple]:
    """`(target, reading)` when the stage's DECLARED epoch count is a whole epoch or more above the
    schedule the trainer configured, else None. Pure/deterministic.

    Fail-closed four ways, each its own conjunct: no single declared target (`declared_epoch_target`
    returns None for zero or two, for the reason stated there), no readable schedule, a schedule
    whose rounding cannot support the claim (`schedule_reading`), and a shortfall inside
    `DECLARED_EPOCH_TOLERANCE` — the SAME bar `declared_epoch_completion` applies at the end, so the
    live rung can never say `short` about a stage the end-of-run floor would acquit.
    """
    target = declared_epoch_target(assertion)
    if target is None:
        return None
    reading = schedule_reading(text)
    if reading is None:
        return None
    if reading.epochs > target - DECLARED_EPOCH_TOLERANCE:
        return None
    return target, reading


def stage_contract_context(declaration: Optional["StageDeclaration"], text: str) -> str:
    """The watched stage's own declared contract, plus the live schedule reading, as prompt text.

    "" when the stage declared nothing, which reproduces the historical message byte for byte — the
    same additive discipline `trajectory_context` and `_LOOK_INVITATION` keep, and the reason
    `train_monitor_contract=false` is a byte-for-byte restore rather than a behaviour flag.

    The declaration is quoted, not summarised, and it is labelled as the CANDIDATE's own promise:
    the judge must be able to tell "the engine will check this" from "the engine believes this".
    A judge told only the shortfall would be told an answer; told the promise and the reading, it
    can see that a training which is otherwise perfectly healthy is nonetheless going to be thrown
    away, which is precisely the state the 38 exit-0 decisions were in.
    """
    if declaration is None or not (declaration.assertion or declaration.files):
        return ""
    # The header is UNCONDITIONAL and its literal is `judgebench.judge_corpus.CONTRACT_PREFIX`, so
    # this block is a NAMED ingredient of the recorded prompt rather than something that silently
    # becomes part of `trajectory` when the bench splits a future run's message. A block whose first
    # line depended on which half of the declaration was present would need two prefixes, and a
    # splitter with two prefixes for one ingredient is one rewording away from finding neither.
    lines = ["THIS STAGE'S OWN DECLARED CONTRACT (from its manifest; the engine CHECKS it after the "
             "stage exits, and a stage that exits 0 still FAILS if it is not met):"]
    if declaration.assertion:
        lines.append("  it must be true that: %r" % declaration.assertion)
    if declaration.files:
        lines.append("  it must also produce: " + ", ".join(declaration.files))
    shortfall = declared_schedule_shortfall(declaration.assertion, text)
    if shortfall is not None:
        target, reading = shortfall
        lines.append(
            f"  ENGINE READING: at step {reading.done}/{reading.total} the trainer reports epoch "
            f"{reading.epoch:g}, so its configured schedule is about {reading.epochs:.2f} epochs "
            f"in total — a whole epoch or more below the {target} this stage declared. If that is "
            "right, this stage will be failed on its own declaration however well it trains, and "
            "the remaining hours buy nothing. Check it against the run's own configuration (the "
            "log's first page usually echoes it) before you call it.")
    lines.append("A run that is training perfectly can still be WASTED because it cannot meet what "
                 "it promised; that is a different judgement from the curve, and both are yours.")
    return "\n".join(lines)


def stage_trajectory_note(trajectory: Optional[LossTrajectory]) -> str:
    """The engine's one-sentence reading of a measured trajectory, for the stage row. Pure.

    Deliberately carries the NUMBERS and not just the direction: a row saying "the engine disagreed"
    is unreviewable, and the whole reason this rung exists is that a reader given a reduction instead
    of a measurement cannot check it.

    BUDGETED, and that is why it is terse. `command_eval` caps `check_inconclusive` at 300
    characters, and the row must hold BOTH readings — so this half is kept near 170 so the model's
    own claim (60-110 characters across the corpus) is still there after the clamp. The full
    measurement, unbounded, is what the MODEL is shown (`trajectory_context`); this is the receipt.
    The reason the engine could see what the checker could not is in the code and the guide, not
    repeated on every row."""
    if trajectory is None or trajectory.windows <= 0:
        return ""
    net = "" if trajectory.net is None else f", net {-trajectory.net:+.4g}"
    noise = "" if trajectory.noise is None else f" vs noise {_fmt_loss(trajectory.noise)}"
    return (f"the engine read this attempt's whole stage log — {trajectory.points} loss values, "
            f"{_fmt_loss(trajectory.first)} -> {_fmt_loss(trajectory.last)}{net}{noise}: DIRECTION "
            f"{trajectory.direction}, so the loss is not unchanged from the first step")


def trajectory_acquits_stage_check(kind: str, trajectory: Optional[LossTrajectory]) -> tuple:
    """`(acquitted, note)` for ONE stage-check verdict. The whole veto, in one statable place.

    FOUR conjuncts, each a separate way to fail closed and leave the refusal exactly as it was:
      1. the verdict is `loss_unchanged_from_first_step` — every other hard kind is a claim about
         mechanism that no curve contradicts, and this must never reach them;
      2. something was measured at all (`windows > 0`): no readable log, an unreadable one, or a
         stage that logged no loss leaves the model's verdict alone;
      3. the numbers are not ANOMALOUS — a non-finite loss or grad_norm anywhere in the attempt, or
         a value 100x the run's opening scale, is evidence a tail genuinely does carry, and a run in
         that state is not a curve this veto should protect (`_anomaly_of`);
      4. the loss MOVED (`TRAJECTORY_MOVED_DIRECTIONS`) — the direct refutation of the kind's claim.

    Returns the ENGINE's sentence when it acquits, so the record can say what contradicted the model
    rather than only that something did."""
    if str(kind or "") != STAGE_CHECK_TRAJECTORY_KIND:
        return False, ""
    if trajectory is None or trajectory.windows <= 0:
        return False, ""
    if trajectory.anomalous:
        return False, ""
    if trajectory.direction not in TRAJECTORY_MOVED_DIRECTIONS:
        return False, ""
    return True, stage_trajectory_note(trajectory)


class LossTrajectoryTracker:
    """Accumulates one `LossWindow` per monitor tick and reports the run-scale trajectory.

    WHY AN ACCUMULATOR RATHER THAN A WIDER READ. `read_training_tail_raw` reads the last 128 KiB,
    which at ~435 B/s of tqdm output is about five minutes; the monitor's cadence is up to thirty,
    so consecutive tails do not even overlap and no single read can span the run. Re-reading the
    whole file would (a) reintroduce the multi-GB load the bounded seek-to-tail read exists to
    prevent and (b) still be a per-tick cost paid on a worker thread. The monitor already reads a
    tail every tick from the first one onward, so keeping each tick's reduction costs nothing and
    covers the run from its start at tick granularity — gaps between windows and all, which is
    exactly why the noise floor is measured WITHIN a window and never across the gaps.

    Per eval attempt and per LOG: `reset()` is called when the active stage log changes, because
    two stages' losses are two different curves and splicing them would invent both a jump and a
    trajectory. The attempt boundary is already handled upstream by `snapshot_training_logs`.
    """

    def __init__(self, max_windows: int = _MAX_TRAJECTORY_WINDOWS) -> None:
        self._windows: deque = deque(maxlen=max(2, int(max_windows)))

    def reset(self) -> None:
        self._windows.clear()

    def observe(self, text: str, *, at: Optional[float] = None) -> Optional[LossWindow]:
        """Record one tick's digest. Returns the window kept, or None when the text carried no loss
        value (a setup-ish or silent tick contributes nothing rather than an empty window)."""
        window = summarize_loss_window(text, at=at)
        if window is not None:
            self._windows.append(window)
        return window

    def summary(self) -> LossTrajectory:
        return summarize_trajectory(self._windows)


def _fmt_loss(value: Optional[float]) -> str:
    return "?" if value is None else f"{value:.6g}"


def trajectory_context(trajectory: Optional[LossTrajectory]) -> str:
    """The measured trajectory as prompt text, or "" when there is nothing measured yet.
    Pure/deterministic.

    Rides in the user message beside `monitor_stage_context`, above the log header, and is ADDITIVE
    by construction: `_MONITOR_SYSTEM`, the stage line and the `LIVE TRAINING LOG (recent tail):`
    header are unchanged (prompt strings are contracts), and an empty return reproduces the
    historical message byte for byte.

    It says what the tail is, which is the half that was missing: the model was reading ten lines as
    though they were the run. Naming the noise floor beside the net change is what lets it tell
    "flat" from "descending under the resolution of this window" without being told the answer."""
    if trajectory is None or trajectory.windows <= 0:
        return ""
    lines = ["TRAJECTORY MEASURED BY THE ENGINE (not from the tail below — the tail is only this "
             "stage's last few seconds; these numbers are read from the whole log this eval has "
             "written so far, one reading per check):"]
    span = ""
    if trajectory.span_s and trajectory.span_s > 0:
        span = f" spanning {trajectory.span_s / 60.0:.0f} min"
    lines.append(f"  loss {_fmt_loss(trajectory.first)} -> {_fmt_loss(trajectory.last)} "
                 f"(lowest seen {_fmt_loss(trajectory.minimum)}) over {trajectory.windows} "
                 f"readings / {trajectory.points} logged points{span}")
    if trajectory.net is not None and trajectory.noise is not None:
        floor = trajectory.noise if trajectory.noise > 0 else None
        ratio = f", {abs(trajectory.net) / floor:.0f}x the noise floor" if floor else ""
        lines.append(f"  net change {-trajectory.net:+.6g}; step-to-step noise floor "
                     f"{_fmt_loss(trajectory.noise)}{ratio}")
    if (trajectory.progress_done is not None and trajectory.progress_total):
        pct = 100.0 * trajectory.progress_done / trajectory.progress_total
        lines.append(f"  position {trajectory.progress_done}/{trajectory.progress_total} "
                     f"({pct:.0f}% of the reported total)")
    verdict = {
        "descending": "the loss IS still going down at run scale, even where a short window looks flat",
        "rising": "the loss is going UP at run scale",
        "flat": "no net movement beyond the noise floor at run scale",
        "unknown": "not enough readings yet to state a direction",
    }[trajectory.direction]
    lines.append(f"  DIRECTION: {trajectory.direction} — {verdict}")
    if trajectory.anomaly:
        lines.append(f"  ANOMALY: {trajectory.anomaly}")
    lines.append("These numbers are extracted from the log the training script itself wrote, so "
                 "read them as the run's own report, and use them for the trend; use the tail "
                 "below for anything the numbers cannot show (errors, warnings, stalls, what the "
                 "run says about its own device and data).")
    return "\n".join(lines)


def trajectory_vetoes_kill(trajectory: Optional[LossTrajectory]) -> bool:
    """Whether the MEASURED trajectory contradicts ending this run. Pure/deterministic.

    True only for a demonstrably descending, non-anomalous curve. This is the deterministic half of
    the split: the model keeps its verdict and its alert row, and this owns "is it still
    descending". It can only ever REFUSE a kill — there is no return path from here that ends a
    node — which is what keeps a rung built on the candidate's own log text on the right side of
    docs/36: a wider action space, never a wider trusted set."""
    return (trajectory is not None and trajectory.direction == "descending"
            and not trajectory.anomalous)


def projected_overrun_s(span_s, eta_s, wall_s) -> Optional[float]:
    """Seconds by which a stage is projected to MISS its own wall, or None when unanswerable.

    `span_s + eta_s` is where this stage is heading; `wall_s` is where it will be killed. The whole
    point is WHEN the comparison can be made: at the wall, `eval_deadline_grace_s` asks a judge for
    a one-shot rescue and that judge is right to refuse a run two hours short — 30 minutes cannot
    close a 2.2-hour gap, which is exactly what it correctly refused for node 6. Seven hours EARLIER
    the same overrun was already computable, while it was still cheap to act on.

    Total and fail-CLOSED: any missing, non-finite or non-positive input answers None, and a stage
    that fits answers None as well — the row exists only when there is something to say.

    NOTE THE BIAS, and note its direction. `LossTrajectory.eta_s` counts training steps and not the
    tail (the in-process test, the checkpoint write), so on the two e5 nodes measured it UNDER-stated
    the truth by 4-5%. That makes this figure CONSERVATIVE: it will under-report an overrun and never
    invent one. A caller may treat a positive answer as real; it may not treat None as "fits"."""
    try:
        span, eta, wall = float(span_s), float(eta_s), float(wall_s)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (span, eta, wall)):
        return None
    if span < 0 or eta < 0 or wall <= 0:
        return None
    overrun = (span + eta) - wall
    return overrun if overrun > 0 else None


# The bar an attention item must clear, as a fraction of the stage's DECLARED wall and an absolute
# floor under it. Derived from the corpus in `stamp_projected_overrun`: 1 % admits every true
# positive on this box (the smallest is 2.7 %) and rejects the stated noise case (40 s on a ten-hour
# stage, 0.11 %) by an order of magnitude. The absolute floor is what keeps a SHORT stage from
# waking an operator over a few seconds, where 1 % is not yet a meaningful quantity.
_OVERRUN_ALERT_FLOOR_FRACTION = 0.01
_OVERRUN_ALERT_FLOOR_S = 60.0

# THE FIELD THE BEYOND-BAR CLEARANCE IS STAMPED UNDER, and the spellings a preserved row may carry.
# Every reader asks through `wall_unreachable`/`OVERRUN_BEYOND_BAR_KEYS` and never through a literal,
# because a literal is what broke this: the field was renamed here on 2026-09-03
# (`overrun_beyond_grace_s` -> `overrun_beyond_noise_s`, the bar moved from the deadline-grace
# ceiling to the projection's own resolution) and the healthy-verdict gate 1,900 lines below went on
# testing the OLD name. `_wall_unreachable` was then unconditionally False, which silently reverted
# the 2026-08-30 fix: a stage judged healthy whose measured ETA cannot fit its declared wall recorded
# nothing again — the `e5small-dr-unified-v11` node 2 shape, SIGKILLed at 84 % of a 36000 s wall,
# 10.0 GPU-hours, then charged a full retrain. A NAME is not a rule. The gate reads the predicate,
# `serve/attention.py::_beyond_bar` reads the tuple, and a fourth rename moves all three at once.
#
# The legacy key rides the TUPLE and not the writer (invariant #5 is additive-only): nothing stamps
# it any more, so a freshly stamped dict can only ever carry the current one, and a preserved row
# keeps the meaning it was written with.
OVERRUN_BEYOND_BAR_KEY = "overrun_beyond_noise_s"
OVERRUN_BEYOND_BAR_KEYS = (OVERRUN_BEYOND_BAR_KEY, "overrun_beyond_grace_s")


def wall_unreachable(fields) -> bool:
    """True when `stamp_projected_overrun` recorded an overrun that CLEARED its own alert bar.

    The gate's question, asked of the scratch dict the stamp filled — never of the raw projection,
    which would be a second and louder spelling of the noise the bar exists to swallow.
    """
    return any(key in (fields or {}) for key in OVERRUN_BEYOND_BAR_KEYS)



def stamp_projected_overrun(alert: dict, trajectory, resolved, log_plan,
                            *, grace_cap=None) -> None:
    """Put the projection beside the verdict on the durable alert row, when there is one to put.

    A separate function for the same reason `trajectory_row` is one: the alert is assembled inside a
    long async loop, and a fact that can only be tested by driving that loop is a fact nobody tests.
    Mutates `alert` in place and returns nothing — it is a stamp, not a decision.

    Silent unless ALL of it is knowable: a measured span, an answerable ETA, a resolved stage, and a
    wall that stage's own manifest declared. Every one of those absences means "the engine cannot
    say", and none of them means "it fits"."""
    wall = None
    if resolved is not None and getattr(resolved, "stage", None) and log_plan is not None:
        wall = (getattr(log_plan, "timeouts", None) or {}).get(str(resolved.stage))
    over = projected_overrun_s(getattr(trajectory, "span_s", None),
                               getattr(trajectory, "eta_s", None), wall)
    if over is None:
        return
    alert["projected_overrun_s"] = round(over, 1)
    alert["stage_wall_s"] = round(float(wall), 1)
    # …AND WHETHER ANYONE SHOULD BE WOKEN, decided HERE because this is the only place that holds
    # both facts. `projected_overrun_s` is deliberately unfiltered — it is the engine's record that
    # it knew — but a 40-second overrun on a ten-hour stage is not a thing to interrupt an operator
    # about, so there is a bar.
    #
    # THE BAR IS THE PROJECTION'S OWN RESOLUTION, NOT THE DEADLINE GRACE (2026-08-30 measurement,
    # changed 2026-09-03). It used to be `over - resolve_deadline_grace(...)`, and the argument for
    # that read well: an overrun the grace absorbs needs no human. Two things are wrong with it and
    # the corpus settled both.
    #
    # (1) IT SUBTRACTS A RESCUE THAT MAY NEVER BE GRANTED. `resolve_deadline_grace` answers the MOST
    #     a stage could ever be given; the seconds actually granted are an LLM deadline judge's
    #     one-shot answer at the wall, clamped by that ceiling, and 0.0 for every way of not
    #     answering. So a real projected overrun inside the ceiling opened nothing, and if the judge
    #     then declined, the node died on its wall with the operator never told — in the window where
    #     acting was still cheap.
    #
    # (2) IT SUBTRACTS FROM A NUMBER THAT ALREADY UNDER-STATES. `e5small-dr-unified-v11` node 3 drew
    #     TWELVE consecutive projections (977.2 -> 1120.6 s over 9h51m) and then died:
    #     `stage_finished train status=timeout exit=-9 seconds=36008.207` against a declared 36000 s,
    #     at 2948/3150 steps. The projection was RIGHT that the stage would not fit and its magnitude
    #     under-reported — at the last tick the true remaining overrun was ~1884 s against a stamped
    #     1120.6, 59 % of it. Every one of those twelve rows carries `projected_overrun_s` and NOT
    #     the beyond-bar field: the AUTO ceiling on a 36000 s wall is min(10 %, 30 min) = 1800 s and
    #     every projection sat under it. The engine knew 9h12m before the kill, wrote it down twelve
    #     times, and opened nothing. 10.0 GPU-hours.
    #
    # So the bar is keyed on what the MEASUREMENT can distinguish. `_OVERRUN_ALERT_FLOOR_FRACTION`
    # (1 % of the declared wall) with `_OVERRUN_ALERT_FLOOR_S` under it admits every true positive
    # the corpus holds — v11 node 3's projections are 2.7-3.1 % of its wall, v8 node 4's larger
    # still — and rejects the stated noise case (40 s on a ten-hour stage is 0.11 %) by an order of
    # magnitude. It is deliberately NOT centred on the projection: a bar keyed on precision must be
    # keyed on the projection being an UNDER-estimate, which is what (2) measured, so the floor
    # bounds the SMALLEST overrun worth saying and nothing about the largest.
    #
    # The GRACE is still recorded (`stage_grace_s`) and is real information — how much rescue could
    # exist — but it no longer decides. That asymmetry is the point: the ceiling is a fact about what
    # MIGHT happen at the wall, and an attention item is a claim about what the engine KNOWS now.
    #
    # THE FIELD IS RENAMED with the meaning. `overrun_beyond_grace_s` said what it subtracted, and
    # keeping the name over a different subtraction is the recorded-fact-pinned-to-the-wrong-site
    # disease this repo tracks. Additive-only (invariant #5): preserved rows keep the old key with
    # its old meaning and `serve/attention.py` reads both, so no historical episode changes.
    try:
        from looplab.runtime.sandbox import resolve_deadline_grace
        grace = float(resolve_deadline_grace(grace_cap, wall))
    except Exception:  # noqa: BLE001 — an unreadable cap is recorded as no grace, never as "it fits"
        grace = 0.0
    floor = max(_OVERRUN_ALERT_FLOOR_S, float(wall) * _OVERRUN_ALERT_FLOOR_FRACTION)
    beyond = over - floor
    if beyond > 0:
        alert[OVERRUN_BEYOND_BAR_KEY] = round(beyond, 1)
        alert["overrun_alert_floor_s"] = round(floor, 1)
        alert["stage_grace_s"] = round(max(0.0, grace), 1)


def trajectory_row(trajectory: Optional[LossTrajectory]) -> Optional[dict]:
    """The compact, JSON-safe form stamped on `EV_TRAIN_MONITOR_ALERT`, or None.

    Additive and fold-ignored; readers default an absent `trajectory` to "the engine measured
    nothing", never to "flat". It is deliberately the MEASUREMENT and not a judgement: the row
    carries what the loss did, so an audit of "was this verdict answerable?" reads the durable log
    instead of re-deriving it from a log that has since grown."""
    if trajectory is None or trajectory.windows <= 0:
        return None
    row = {"direction": trajectory.direction, "windows": trajectory.windows,
           "points": trajectory.points}
    # The ETA rides the row the trajectory already stamps, so it reaches the durable log, the judge's
    # context and the UI through ONE seam instead of three. Absent when unanswerable — a reader must
    # treat a missing `eta_s` as "the engine cannot say", never as "soon".
    if isinstance(trajectory.eta_s, float) and math.isfinite(trajectory.eta_s):
        row["eta_s"] = round(trajectory.eta_s, 1)
    for key in ("first", "last", "minimum", "noise", "net"):
        value = getattr(trajectory, key)
        if isinstance(value, float) and math.isfinite(value):
            row[key] = round(value, 6)
    if trajectory.anomaly:
        row["anomaly"] = trajectory.anomaly[:64]
    if trajectory.progress_done is not None and trajectory.progress_total:
        row["progress"] = f"{trajectory.progress_done}/{trajectory.progress_total}"
    return row
