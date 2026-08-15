"""Training-log monitor — a per-eval background observer of the LIVE training log (I-series watchdog
family, sibling of `runtime/sandbox._StageHealthMonitor`).

A repo eval's declared training stage runs for a long time (often multi-hour) while the engine's async
loop is otherwise idle — `_evaluate` runs the eval in a worker thread. This mixin adds a periodic task
in that same task group (alongside the mid-eval intervention `_watch`) that tails the stage's live log
and implements the complete bounded phase stack:

- Phase 0: read the live-log tail on a timer and emit a `train_monitor` TRACE span;
- Phase 1: when a client is available, classify the bounded digest and append fold-ignored
  `train_monitor_alert` diagnostics for non-healthy verdicts;
- Phase 2: self-pace later observations from the run budget, healthy streak, and bounded model hint;
- Phase 3: only when `train_monitor_kill` is explicitly enabled, claim a CONFIRMED, sufficiently
  confident `broken` verdict about an IDENTIFIED training stage — one the engine's own MEASURED loss
  trajectory does not contradict — and reuse the evaluation cancel/tree-kill path. The node still
  terminates once with `reason=monitor_broken`.

WHO OWNS WHICH QUESTION. The judge is asked what a TAIL can answer — is anything anomalous, what is
this run saying about itself — and the engine owns "is it still descending", because that is a
statement about the whole curve and the tail is ~30 seconds of it (see the trajectory section
below, measured on `runs/rubertlite-dr-unified-v7`). The measurement is derived from the
candidate's own log text, so it is held to `engine/metric_salvage.py`'s rule: it may REFUSE an
intervention and may never authorize one, and it reaches no metric, champion or selection record.

Which log is judged is part of the contract, not an implementation detail: the eval writes one
`<stage>.log` per stage plus `setup.log` (dep install) and, on the single-command path, `eval.log`.
Only a stage that runs the candidate's own work can be judged by a TRAINING-health prompt — see
`eval_log_plan` / `resolve_stage_log`.

With intervention off, the monitor never changes the metric champion or node lifecycle. Diagnostics can
still feed the separately configured watchdog-reflection prompt cue on a later proposal.

Design constraints this file must keep (engine invariants):
- **The runtime never calls the LLM** (layering: `runtime` imports nothing above itself), so an LLM-driven
  watchdog CANNOT live in the sandbox — it lives here, in the engine, reading the log FILE the sandbox
  already writes (`_tee_drain(log_path=…)`).
- Verdicts are DIAGNOSTIC events (fold-ignored), so their thread-dependent splice position never changes
  folded state. Replay reads the ordinary terminal event and NEVER re-invokes the monitor LLM.
"""
from __future__ import annotations

import math
import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from looplab.core.llm_broker import in_llm_lane
# The log-role vocabulary is stamped on the DURABLE `EV_TRAIN_MONITOR_ALERT` row, so it lives in
# `events/types.py` where readers below the engine (`events/digest.py`'s `watchdog_reflection`) can
# name a role without importing the engine (layering: `events` imports only `core`). Imported here
# and re-exported, because `train_monitor.LOG_ROLE_*` is the spelling engine code and tests use.
# `events.types` imports nothing, so this module-level import cannot become a cycle.
from looplab.events.types import (
    LOG_ROLE_AMBIGUOUS,
    LOG_ROLE_SCORE,
    LOG_ROLE_SETUP,
    LOG_ROLE_TRAINING,
    LOG_ROLE_UNKNOWN,
    LOG_ROLE_WORK,
)

# The verdict schema the log observer returns. `status` drives everything downstream: a non-"healthy"
# verdict becomes an EV_TRAIN_MONITOR_ALERT (Phase 1) and, later, a gated early kill (Phase 3, "broken"
# only). Field descriptions are part of the schema the model sees — they ARE the classification contract.
class TrainingVerdict(BaseModel):
    status: Literal["healthy", "watch", "broken"] = Field(
        description="healthy = training is progressing normally (loss decreasing or stable, no errors); "
                    "watch = something looks off but not necessarily fatal (slow, plateauing, warnings); "
                    "broken = clear evidence the run is WASTED and cannot recover — diverged loss, a silent "
                    "CPU fallback while a GPU was expected, data-loader errors repeating in the loop, loss "
                    "stuck at its initialization value (not learning), or an uncaught exception.")
    reason: str = Field(description="One short sentence naming the SPECIFIC log evidence for the status.")
    confidence: float = Field(default=0.5, description="Confidence in the status, 0.0 to 1.0.")
    recheck_after_s: Optional[float] = Field(
        default=None,
        description="Optional: how many seconds until you want to look again. Use a LARGER value when the "
                    "run is healthy and steady, so a boring, well-behaved run is not watched closely. You "
                    "cannot look sooner than the automatic cadence (values below it are ignored); the "
                    "automatic pace already tightens on shorter runs. Omit to keep the default cadence.")


# The observer's framing. A contract: it fixes the observer's role (the engineer who wrote THIS loop),
# what it may rely on (log evidence, NOT the unknown final metric), and its bias (flag EARLY, before the
# whole budget is burned — but do not cry wolf on a normal slow-but-progressing run).
_MONITOR_SYSTEM = (
    "You are the ML engineer who wrote this training script, watching its LIVE log during a long run to "
    "catch a wasted run EARLY — before its whole (often multi-hour) time budget is spent. Judge ONLY from "
    "the log evidence; the final metric is not known yet, so do not guess it. A run that is merely slow or "
    "plateauing but still progressing is 'watch', not 'broken' — reserve 'broken' for clear, cannot-recover "
    "evidence. Be concise and specific about the evidence you saw.")


# The sentence that tells the judge the tail is not all it may have. Spliced ONLY when tools are
# actually wired (`monitor_log_tools`), at the same position pattern as `stage_context` and
# `trajectory_text`, so `train_monitor_tools=false` restores the historical message byte for byte.
#
# It NAMES the failure it exists to end, because a model handed both a tail and a tool still reasons
# from the tail: on v7 the spliced tail was ten loss values ~30 seconds apart on a five-hour run, and
# the verdict "pinned at ~23.0 … no learning trend from its initialization value" was a correct
# reading of exactly that. Telling it the window is small is not the same as telling it the window is
# *too small to answer the question it is being asked* — so this says the second thing.
_LOOK_INVITATION = (
    "YOU CAN LOOK FURTHER. The tail below is a SHORT window — often under a minute of a run that has "
    "been going for hours — and 'converged', 'stuck at initialization' and 'still descending slowly' "
    "are indistinguishable inside it, because the difference is smaller than the step-to-step noise. "
    "Before you call anything broken, USE YOUR TOOLS: `metric_series` for what the loss has actually "
    "done over the whole run at a granularity you choose, and `read_log` to tail further back, read "
    "the run's start, or search for a traceback. If the tools disagree with the tail, the tools have "
    "more evidence.")

# The tool-loop turn budget for ONE monitor tick. Deliberately well below `trust.judge.JUDGE_MAX_TURNS`
# (15): this judge fires up to `_MAX_MONITOR_LLM_CALLS` times per node on a timer, so a turn budget is
# multiplied by ~200 in a way the two one-shot verifiers' never is. Six is enough for the shape the
# invitation asks for — a whole-run series, a narrower one, a search, and the emit — and a loop that
# spends it degrades to `parse_structured` on the same messages rather than to nothing.
_MONITOR_LOOK_TURNS = 6


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

# An observed value at least this many times the run's own opening scale is an EXPLOSION, not a
# reading of the same curve. Deliberately generous: the point is to notice `1.2e25` beside `63.8`,
# never to adjudicate a 3x spike, and its only effect is to withdraw the veto (see `_anomaly_of`).
_TRAJECTORY_EXPLOSION_RATIO = 100.0
# A net drop must clear BOTH the measured step-to-step noise floor and this fraction of the opening
# level. The noise floor is the real test; the relative floor only stops a numerically-tiny drift on
# a quiet log from reading as progress. Node 1's 5.8% and node 0's 30% clear it by three orders.
_TRAJECTORY_MIN_RELATIVE_DROP = 0.001
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
      noise floor AND more than `_TRAJECTORY_MIN_RELATIVE_DROP` of the opening level, so the loss is
      demonstrably not stuck at its initialization value;
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

    @property
    def anomalous(self) -> bool:
        """Whether the numbers themselves carry evidence a TAIL can legitimately act on — a
        non-finite loss/grad-norm or an explosion. Such a run is not 'descending' in any sense the
        veto should protect, so the veto stands down and the model's `broken` verdict is left to
        act (`runs/rubertlite-dr-unified-v6` node 5 is the worked case)."""
        return bool(self.anomaly)


def _median(values) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


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
    floor = max(common["noise"], abs(numeric[0].median) * _TRAJECTORY_MIN_RELATIVE_DROP)
    direction = "descending" if net > floor else ("rising" if net < -floor else "flat")
    return LossTrajectory(net=net, direction=direction, **common)


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
    for key in ("first", "last", "minimum", "noise", "net"):
        value = getattr(trajectory, key)
        if isinstance(value, float) and math.isfinite(value):
            row[key] = round(value, 6)
    if trajectory.anomaly:
        row["anomaly"] = trajectory.anomaly[:64]
    if trajectory.progress_done is not None and trajectory.progress_total:
        row["progress"] = f"{trajectory.progress_done}/{trajectory.progress_total}"
    return row


# Phase 2 self-pacing constants. After this many CONSECUTIVE healthy verdicts (and no explicit
# agent-requested recheck), the monitor geometrically backs OFF — a steadily-healthy run does not need
# close watching — capped so it never fully stops (a late failure is still caught, just cheaply).
_HEALTHY_BACKOFF_K = 3
_MONITOR_CADENCE_CAP_S = 3600.0     # never wait more than an hour between checks (stays safe on late failures)
# Per-node LLM-call backstop. The adaptive cadence + healthy backoff already bound calls to ~budget/base
# (≈150 even for a 24h eval); this is a never-normally-hit ceiling for a pathological always-changing,
# never-healthy log. Past it the monitor keeps observing (trace) but stops spending on the LLM.
_MAX_MONITOR_LLM_CALLS = 200

# Phase 3 confirmation window. A kill needs this many CONSECUTIVE confident `broken` verdicts about the
# SAME stage log. EVERYTHING else re-arms the gate from zero, and the list below is exhaustive on
# purpose — the previous wording said "anything else" while the code only ever touched the streak
# inside `if verdict is not None`, so a tick the model ANSWERED but whose answer did not validate left
# the arm standing:
#   • a parseable 'watch'/'healthy' verdict;
#   • a switch to another stage's log (`armed_key`);
#   • a tick that produced NO parseable verdict — an endpoint failure, model output that fails schema
#     validation, or the per-node LLM cap. That second case was the commit's own cited mitigation:
#     told the stage was a scorer, `deepseek-v4-flash` answers `unknown`, which is not in
#     `TrainingVerdict.status`'s `Literal[healthy|watch|broken]`, so `parse_structured` returns None.
#     The model DECLINING to confirm must not read the same as never having been asked. Reproduced:
#     arm, six `unknown` ticks, one `broken` -> killed;
#   • the arm outliving `_MONITOR_ARM_TTL_S` or spending `_MONITOR_ARM_MAX_LOOKS` (below).
# The sibling ASHA watchdog will not stop a node on one observation either — it wants a grace
# window, a min-siblings floor and an LLM judge — and the loss here is the same multi-hour training with
# no repair and no retry, so one sampled verdict must not be the whole gate. Deliberately small: the
# watchdog's whole point is to catch a wasted run EARLY, and a second look costs one extra cadence.
_MONITOR_KILL_CONFIRM_TICKS = 2
# The confirmation look is scheduled SOONER than the ordinary cadence (which is up to 30 min on a long
# budget) so arming the gate delays a real kill by seconds, not by another full watch interval.
_MONITOR_CONFIRM_DELAY_S = 30.0
# BOUNDS ON THE ARM. The arming tick bypasses the changed-digest gate so "diverged, then stopped
# printing" cannot survive by saying nothing — but that bypass held for as long as the arm did, and the
# arm had no bound at all. On a frozen log with a dead endpoint the monitor issued one billable call per
# cadence, every one carrying a BYTE-IDENTICAL prompt, until `_MAX_MONITOR_LLM_CALLS` (~100 minutes of
# re-asking the same question; measured: 25 calls, 1 distinct prompt, log bytes unchanged). Two
# independent bounds, both of which simply disarm:
#   • looks: the bypass buys exactly ONE re-ask of an unchanged digest. A second identical prompt is
#     not additional evidence — the confirmation defends against sampling noise, and on a frozen log
#     there is no new sample to take. A tick that DOES see new bytes was never a bypass, so a live log
#     is unaffected;
#   • wall clock: an arm older than this is stale regardless of why, so a slow or hanging endpoint
#     cannot leave a kill primed indefinitely. Sized well above one confirmation delay plus a full
#     provider timeout, and far below the runaway.
_MONITOR_ARM_MAX_LOOKS = 1
_MONITOR_ARM_TTL_S = 300.0
# The SAME digest is re-asked at most this many times before it is retired as judged. `last_digest` is
# deliberately committed only when a verdict parses (a transient endpoint failure must not permanently
# skip judging the current digest — for a slow-logging stage that is a long window to be blind in), but
# "only on success" and "forever" are different promises: against an endpoint that never answers, an
# unchanged log was re-sent every cadence. After this many failures the digest is retired and the
# monitor goes quiet until the log actually changes, which is the pre-arm behaviour it was protecting.
_MONITOR_SAME_DIGEST_RETRIES = 2

# Which eval phase a log belongs to. The eval writes one log per phase into the node workdir
# (`runtime/command_eval.py`): `setup.log` for the dep install, `<stage>.log` for every resolved
# pipeline stage, and `eval.log` for the single-command path. Those three shapes are the WHOLE naming
# contract, and these roles say which of them a TRAINING-health verdict may be formed about at all
# (see `eval_log_plan` / `resolve_stage_log`, and the `log_role` conjunct of `should_monitor_kill`).
# The vocabulary itself is defined in `events/types.py` and imported at the top of this module (see
# the note there); only what each role MEANS to this watchdog is decided here.
#
# Roles a training-health prompt can say nothing useful about, so they produce no tick at all. Feeding
# one of these to the judge is not merely low-value: a short CPU-only scorer that prints framework
# warnings and has no loss trajectory hits three separate clauses of `TrainingVerdict.status`'s
# `broken` contract at once. `LOG_ROLE_AMBIGUOUS` joins them because a filename with two possible
# writers cannot be attributed at all (see `eval_log_plan`), and "we do not know whose bytes these
# are" is the same answer as "these are not training bytes" for a prompt that must name evidence.
_NON_TRAINING_ROLES = frozenset({LOG_ROLE_SETUP, LOG_ROLE_SCORE, LOG_ROLE_AMBIGUOUS})
# The ONLY role that may end a node. A set, so the kill conjunct reads as membership rather than an
# equality that silently widens when a role is added: `LOG_ROLE_WORK` is judged like training but is
# deliberately NOT here.
_KILL_ELIGIBLE_ROLES = frozenset({LOG_ROLE_TRAINING})


def next_monitor_sleep(base: float, *, status: Optional[str] = None,
                       recheck_after_s: Optional[float] = None, healthy_streak: int = 0,
                       backoff_after: int = _HEALTHY_BACKOFF_K, cap: float = _MONITOR_CADENCE_CAP_S) -> float:
    """The delay until the NEXT check, given the base cadence and the latest verdict. Pure/deterministic.

    Precedence: an explicit agent-requested `recheck_after_s` wins (the observer self-paces — but never
    faster than `base`, to bound LLM cost, and never slower than `cap`); otherwise a run that has been
    healthy for `backoff_after`+ consecutive checks backs off geometrically (×2 each extra healthy tick,
    bounded); everything else keeps the base cadence."""
    if isinstance(recheck_after_s, (int, float)) and not isinstance(recheck_after_s, bool) and recheck_after_s > 0:
        return min(cap, max(base, float(recheck_after_s)))
    if status == "healthy" and healthy_streak >= backoff_after:
        return min(cap, base * (2.0 ** min(healthy_streak - backoff_after + 1, 6)))
    return base


def _normalize_monitor_confidence(value: object) -> tuple[float, bool]:
    """Return a bounded confidence plus whether the input was finite.

    The safe numeric fallback keeps alerts and traces serializable/observable; callers making an
    intervention decision must additionally require the validity bit.
    """
    try:
        confidence = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0, False
    # max/min is not a validity check: ``min(1.0, NaN)`` evaluates to 1.0 in Python.
    # Treat every non-finite model value as invalid and observable-at-zero, never as kill authority.
    if not math.isfinite(confidence):
        return 0.0, False
    return max(0.0, min(1.0, confidence)), True


def should_monitor_kill(verdict: Optional["TrainingVerdict"], *, enabled: bool, threshold: float,
                        log_role: str = LOG_ROLE_UNKNOWN, broken_streak: int = 0,
                        confirm_ticks: int = _MONITOR_KILL_CONFIRM_TICKS,
                        trajectory: Optional["LossTrajectory"] = None) -> bool:
    """Whether a verdict warrants an EARLY KILL (Phase 3). Pure/deterministic — the WHOLE kill decision
    surface, so what it takes to end a node is one testable expression rather than a scatter of loop
    state.

    Five independent conjuncts, every one fail-closed on its default:

    - `enabled`: the opt-in (`train_monitor_kill`).
    - a `broken` verdict at confidence >= `threshold`. The prompt makes a slow/plateauing-but-progressing
      run 'watch', never 'broken'; 'watch'/'healthy' stay advisory.
    - `log_role` is in `_KILL_ELIGIBLE_ROLES`, i.e. `LOG_ROLE_TRAINING`: the tail is provably the run's
      own training (`eval_log_plan` grants that role only to a log that is the WHOLE eval). A verdict
      about `setup.log`, the pipeline's scorer, an unattributable filename, a pipeline WORK stage
      (`LOG_ROLE_WORK` — judged, but the plan cannot prove it is the training step) or a log nothing
      could attribute at all (`LOG_ROLE_UNKNOWN`, the default) is advisory evidence and never
      authority — the monitor must not act on a stage it cannot identify.
    - `broken_streak >= confirm_ticks`: the verdict has been REPEATED. One confident tick used to be the
      whole gate, which is out of step with every sibling control in this family — the ASHA watchdog
      needs a grace window, a min-siblings floor AND an LLM judge before it may stop a node, and the
      cost of being wrong here is identical (a multi-hour training discarded with no repair, no retry
      and no refund of its `max_nodes` slot). `confirm_ticks=1` restores single-tick behaviour for a
      caller that wants it; 0 or less cannot disable the requirement, because `broken_streak` counts
      the current tick and is therefore always >= 1 at a real call site. What COUNTS toward that
      streak is the caller's business and is spelled out on `_MONITOR_KILL_CONFIRM_TICKS`.
    - the engine's own MEASURED `trajectory` does not contradict the verdict
      (`trajectory_vetoes_kill`). The other four conjuncts all ask who is speaking and how often;
      this one asks whether the question was answerable from what the speaker was shown. On v7 the
      judge saw ten loss values spanning half a minute of a five-hour run and called a run that had
      gone 24.28 -> 22.90 "pinned at ~23.0 ... showing no learning trend from its initialization
      value" at confidence 0.82. `None` (no measurement) never vetoes, so a run that prints no
      parseable loss at all is exactly as killable as it was before.
    """
    if not enabled or verdict is None or verdict.status != "broken":
        return False
    if log_role not in _KILL_ELIGIBLE_ROLES:
        return False
    if trajectory_vetoes_kill(trajectory):
        return False
    try:
        needed = int(confirm_ticks)
    except (TypeError, ValueError, OverflowError):
        return False
    if broken_streak < max(1, needed):
        return False
    confidence, confidence_valid = _normalize_monitor_confidence(verdict.confidence)
    return confidence_valid and confidence >= threshold


def claim_watchdog_kill(kill_signal: dict, cancel, *, reason: str, terminal_reason: str,
                        confidence: Optional[float] = None) -> bool:
    """Atomically claim the shared per-eval watchdog terminal in the cooperative event loop.

    The training-health and ASHA-rank monitors are sibling tasks and can reach a kill decision on the
    same tick. Only the first decision may own the persisted failure explanation; a later sibling still
    exits because the winner has already set ``cancel``. Returns whether this caller won the claim.
    """
    # there is deliberately no await between this guard and the one-shot dict update. Both
    # watchdogs run on the same cooperative event loop, so the first writer owns reason + terminal_reason
    # as one indivisible decision instead of a later task producing a mixed/overwritten terminal record.
    if kill_signal.get("kill"):
        return False
    payload = {
        "kill": True,
        "reason": str(reason),
        "terminal_reason": str(terminal_reason),
    }
    if confidence is not None:
        payload["confidence"] = confidence
    kill_signal.update(payload)
    cancel.set()
    return True


def last_lifecycle_row(rows, event_type: str, node_id: int, generation: int) -> Optional[dict]:
    """The NEWEST row of `event_type` belonging to exactly this `(node_id, generation)`, as its data
    dict — or None when the watchdog never spoke for this lifecycle.

    Both watchdogs need this on re-entry: `resume` can restart an observer inside the same node
    generation, and without recovering the last durable row the first healthy verdict looks like a
    first observation, so a pre-crash warning is lost instead of being closed (doc 25 EC-04). Three
    sites hand-rolled the scan — both resume recoveries and `asha_monitor.latest_train_verdict` — with
    the bool-guarded field validation copied verbatim.

    That guard is the point, and it is easy to get subtly wrong: rows are UNTRUSTED append-only data,
    and `isinstance(True, int)` is True in Python, so a payload carrying `node_id: true` matches a
    plain `== node_id` test against node 1 and hands a watchdog another lifecycle's history. The
    generation half matters the same way.

    Returns the newest MATCHING row even when its contents are unusable — callers decide what an
    unreadable payload means, and every one of them treats it as "no history", never as a reason to
    keep scanning backwards into an older row that would answer for a stale tick.

    Pure; safe on an empty or None row list.
    """
    for event in reversed(list(rows or ())):
        if getattr(event, "type", None) != event_type:
            continue
        data = getattr(event, "data", None) or {}
        nid, gen = data.get("node_id"), data.get("generation")
        if isinstance(nid, bool) or not isinstance(nid, int) or nid != node_id:
            continue
        if isinstance(gen, bool) or not isinstance(gen, int) or gen != generation:
            continue
        return data
    return None


# ------------------------------------------------------------------ which log belongs to which stage
_SETUP_LOG = "setup.log"
_SINGLE_COMMAND_LOG = "eval.log"
# `score` is RESERVED for the engine-appended protected scoring stage (`engine/eval_stages.py`
# appends it to every Developer manifest, and `command_eval.validate_stages(reserved=("score",))`
# refuses it to the agent). An operator-declared pipeline MAY own the name, and there it means the
# same thing. This is a BACKSTOP, not the primary test — the structural "last stage" rule in
# `_is_scorer_stage` is — and it is spelled to match `validate_stages` EXACTLY: that validator refuses
# the name case-INSENSITIVELY (`nm.lower() in reserved`), so anything it would have refused to the
# agent must read as "scorer" here too. Comparing `name == "score"` instead was live on every platform
# we run (`os.path.normcase` is identity outside Windows): two byte-identical runs differing only in
# capitalisation ended `node_evaluated metric=0.7` and `node_failed monitor_broken` — an operator
# pipeline spelled `Score` handed a scorer's tail to a training-health judge holding kill authority.
_RESERVED_SCORER_NAMES = frozenset({"score"})


def _log_name_key(name: str) -> str:
    """Case-folded log basename, matching `_log_path_key`'s Windows handling."""
    return os.path.normcase(str(name))


def _is_scorer_stage(name: str, *, index: int, total: int) -> bool:
    """Whether a resolved pipeline stage is the SCORER — structurally first, by name only as a backstop.

    STRUCTURAL: `command_eval.run_command`'s staged branch states the contract verbatim — "The LAST
    stage's stdout carries the metric" — and that stage's output is the only one `read_metric` reads.
    It holds for BOTH shapes `_resolve_stages` produces: the engine appends its protected `score`
    stage last to a Developer manifest, and an operator-declared pipeline scores in its own final
    stage. So POSITION, not spelling, is what the engine actually knows. `total == 1` is excluded
    because a one-stage pipeline is the single-command shape wearing a stage name: that one command
    both trains and scores, exactly like `eval.log`.

    NAME: any spelling `command_eval.validate_stages` would have RESERVED (case-folded `score`) also
    reads as the scorer wherever it appears, so a mid-pipeline stage the agent could never have been
    allowed to name cannot acquire training authority by sitting in an operator's list."""
    if total > 1 and index == total - 1:
        return True
    return str(name).strip().lower() in _RESERVED_SCORER_NAMES


@dataclass(frozen=True)
class EvalLogPlan:
    """Every log file ONE eval attempt can write, mapped to the stage that writes it and its role.

    Built by the engine from the SAME resolved stage list the eval runs (`_resolved_stages`), so the
    watchdogs stop guessing which phase produced the bytes they are reading.
    """

    roles: dict                  # case-folded basename -> (stage name or None, LOG_ROLE_*)
    stage_names: tuple = ()      # the resolved pipeline order; () for a single-command eval


def eval_log_plan(stages) -> EvalLogPlan:
    """The log plan for a resolved eval pipeline. Pure/deterministic — no I/O — so what the watchdogs
    are allowed to judge is unit-testable without a filesystem.

    `stages` is `Engine._resolved_stages`' output: the ordered pipeline, or `[]`/None for the classic
    single-command eval (whose one command trains AND scores in one process — see the
    `command_eval.py` comment on that branch — so its `eval.log` IS a training log).

    WHICH STAGES MAY KILL. Only `LOG_ROLE_TRAINING` carries kill authority, and this plan grants it
    only where the log is PROVABLY the run's own training:

    - the single-command `eval.log`, and a ONE-stage pipeline (the same shape wearing a stage name):
      that command is the whole eval, so there is nothing else its output could be. `command_eval`
      says as much on that branch — "A single-command RepoTask eval IS the training (train->eval in
      one process, often multi-hour)" — and it is also the only path the runtime leaves WITHOUT its
      own deterministic divergence kill (`health_check=True` is passed for every declared stage and
      omitted here), so the LLM watchdog is that path's only early stop;
    - every other pipeline stage is `LOG_ROLE_WORK`: still read, still judged, still alerting — but
      ADVISORY.

    That last line is the substantive narrowing, and it is deliberate. The previous rule — "every
    stage whose name is not the exact string `score` is training" — was justified in this docstring by
    a claim about `command_eval` that is false for the staged path: `run_argv` is called with
    `health_check=True` for EVERY declared stage, the appended scorer included, so the runtime draws
    no train/not-train line there at all. Nothing else draws one either: `validate_stages` accepts any
    filesystem-safe slug, the manifest carries no role field, and the appended `score` stage is the
    operator's `cmd` — which the operator is explicitly invited to point at an entrypoint "the agent
    must BUILD", i.e. one that may itself train. So a pipeline's work stages cannot even be argued to
    CONTAIN the training by elimination.

    Measured, not theorised: driving the real `_evaluate` over `data_prep -> train -> score` with a
    `data_prep` stage printing framework warnings, `CUDA not available - falling back to CPU` and a
    flat `loss: 0.6931`, `deepseek-v4-flash` answered `broken` at confidence 0.9 — while being told,
    in the prompt, that it was looking at stage `data_prep` of that pipeline, and it armed the kill
    gate. The stage identity is a mitigation, not a guarantee, so a stage the plan cannot PROVE is
    training must not hold the authority to discard a multi-hour run with no repair, no retry and no
    refunded `max_nodes` slot. A `LOG_ROLE_WORK` verdict still reaches the alert row,
    `watchdog_reflection`, the attention feed and the audit trail — the watchdog keeps its whole
    advisory job on those stages, only not the gun.

    AMBIGUOUS FILENAMES. A log basename with more than one possible writer cannot be attributed to a
    phase, so it produces no tick. That is ONE rule covering two real collisions: a pipeline stage
    named `setup` shadows the dep install's `setup.log` (`command_eval` writes pip output there
    regardless of any stage), and on Windows two stage names differing only in case fold onto one
    file. Previously the shadowing stage "won the name" and inherited kill authority over pip output.
    """
    raw = list(stages or [])
    names = tuple(str(s.get("name")) for s in raw
                  if isinstance(s, dict) and s.get("name") is not None)
    # A row this cannot name is a broken resolved pipeline (`_resolve_stages` only ever returns
    # `validate_stages`-cleaned dicts), and dropping it would RENUMBER the rest: a 3-stage list with
    # two unusable rows would otherwise collapse to a "one-stage pipeline" and hand the survivor kill
    # authority. Position-derived SCORE stays (marking more logs unjudged is the safe direction);
    # only the TRAINING grant is withheld.
    complete = len(names) == len(raw)
    roles: dict = {}

    def _claim(key: str, value: tuple) -> None:
        # A second writer for the same basename -> unattributable, and unattributable is not judged.
        roles[key] = value if roles.get(key, value) == value else (None, LOG_ROLE_AMBIGUOUS)

    _claim(_log_name_key(_SETUP_LOG), (None, LOG_ROLE_SETUP))
    if names:
        for index, name in enumerate(names):
            if _is_scorer_stage(name, index=index, total=len(names)):
                role = LOG_ROLE_SCORE
            elif len(names) == 1 and complete:
                role = LOG_ROLE_TRAINING     # a one-stage pipeline IS the single-command shape
            else:
                role = LOG_ROLE_WORK
            _claim(_log_name_key(f"{name}.log"), (name, role))
    else:
        _claim(_log_name_key(_SINGLE_COMMAND_LOG), (None, LOG_ROLE_TRAINING))
    return EvalLogPlan(roles=roles, stage_names=names)


@dataclass(frozen=True)
class ActiveStageLog:
    """The log a watchdog tick is looking at, plus WHICH eval phase wrote it."""

    path: Path
    stage: Optional[str]
    role: str


def resolve_stage_log(workdir, plan: Optional[EvalLogPlan] = None) -> Optional[ActiveStageLog]:
    """The workdir's live log, ATTRIBUTED to the eval phase that writes it. None when there is nothing
    the caller may read (no `*.log` yet, or none the plan can name).

    Freshest-mtime still tracks the moving active stage — that part of the old heuristic was right, and
    the sandbox's live stage cursor genuinely is unobservable from here. What was wrong was acting on
    the answer without knowing WHICH stage it named:

    REVIEW NOTE (superseded): this glob used to be deliberately broad ("the failure is benign — a
    slightly-less-relevant tail feeds an ADVISORY verdict"). That premise died when `train_monitor_kill`
    became the default: the freshest `*.log` is `setup.log` during a minutes-long pip install and
    `score.log` during the ALWAYS-appended final score stage (`engine/eval_stages.py`), and both were
    fed to a training-health judge holding kill authority, with the changed-digest gate guaranteeing a
    fresh LLM call on every file switch. A `score.log` verdict killed the training that had just
    SUCCEEDED. The engine knows the resolved stage list; pass it in (`eval_log_plan`) and the answer is
    named instead of guessed.

    With a plan, logs the plan cannot name are IGNORED rather than read: a stray `*.log` the candidate's
    own code drops is at best a duplicate of the stage log (which captures the subprocess's whole
    stdout/stderr), and silently judging unattributable bytes is the exact defect above. Without a plan
    the old freshest-file answer stands, tagged `LOG_ROLE_UNKNOWN` so callers can degrade to advisory.
    """
    try:
        logs = list(Path(workdir).glob("*.log"))
    except OSError:
        return None
    if plan is not None:
        logs = [p for p in logs if _log_name_key(p.name) in plan.roles]
    if not logs:
        return None
    try:
        newest = max(logs, key=lambda f: f.stat().st_mtime)
    except OSError:
        return None
    if plan is None:
        return ActiveStageLog(path=newest, stage=None, role=LOG_ROLE_UNKNOWN)
    stage, role = plan.roles[_log_name_key(newest.name)]
    return ActiveStageLog(path=newest, stage=stage, role=role)


def monitor_stage_context(resolved: Optional[ActiveStageLog],
                          plan: Optional[EvalLogPlan] = None) -> str:
    """One line telling the observer WHICH stage's log it is about to read. Pure/deterministic.

    The judge used to receive a tail headed "LIVE TRAINING LOG" with nothing saying which phase of the
    eval produced it, so it had no way to notice it was being shown a scorer. This rides in the
    caller-supplied `context` (the system prompt and the log header stay verbatim — prompt text is a
    contract), and it is the only thing that makes an UNKNOWN attribution visible to the model at all.
    """
    if resolved is None:
        return ""
    if resolved.role == LOG_ROLE_UNKNOWN or plan is None:
        return ("NOTE: this log could not be attributed to a named eval stage, so it may not be the "
                "training stage's output. Judge only what the lines themselves support.")
    if not plan.stage_names:
        return ("This eval runs ONE command that both trains and scores in a single process; the log "
                "below is that command's complete live output.")
    order = " -> ".join(plan.stage_names)
    if resolved.stage in plan.stage_names:
        position = plan.stage_names.index(resolved.stage) + 1
        line = (f"This is the live log of pipeline stage {resolved.stage!r} "
                f"(stage {position} of {len(plan.stage_names)}; the pipeline is {order}). "
                "Judge THIS stage's output only.")
        if resolved.role == LOG_ROLE_WORK:
            # A WORK stage is one the plan cannot prove is the training step (see `eval_log_plan`), and
            # it is exactly where a confident wrong `broken` was measured: a `data_prep` stage printing
            # framework warnings and a flat loss drew `broken` 0.9 WITH the sentence above present.
            # The role carries the safety (no kill authority); this only helps the model answer the
            # right question. Additive — the sentence above is unchanged, and every other attribution
            # still renders byte-identically to before.
            line += (" This stage may be data preparation, export or another non-training step rather "
                     "than the training loop itself; if these lines are not a training run, say that "
                     "instead of judging them as one.")
        return line
    return f"This eval runs the pipeline {order}. Judge only the stage output shown below."


def active_training_log(workdir, plan: Optional[EvalLogPlan] = None) -> Optional[Path]:
    """The live log an observer may read, or None.

    A thin role filter over `resolve_stage_log`, and the filter is READABILITY, never kill authority:
    `setup.log` (dep install), the pipeline's scorer and an unattributable filename
    (`_NON_TRAINING_ROLES`) carry no training signal at all, so they are not returned — a tick during
    those phases has nothing to observe, exactly like a tick before the first log exists. A
    `LOG_ROLE_WORK` stage IS returned: its verdict is advisory (`should_monitor_kill` is where that is
    enforced) but it is the candidate's own running code, and it is also where the sibling ASHA
    watchdog finds the live metric curve. Without a plan this is the historical freshest-`*.log`
    answer (see `resolve_stage_log`'s superseded review note).
    """
    resolved = resolve_stage_log(workdir, plan)
    if resolved is None or resolved.role in _NON_TRAINING_ROLES:
        return None
    return resolved.path


@dataclass(frozen=True)
class TrainingLogCursor:
    """The immutable boundary between two eval attempts for one existing log file."""

    offset: Optional[int]
    identity: Optional[tuple[int, int]]
    probe_start: int = 0
    probe: Optional[bytes] = None


@dataclass(frozen=True)
class TrainingLogSnapshot:
    """Best-effort cursors for every ``*.log`` that existed before an eval attempt started."""

    cursors: dict[str, TrainingLogCursor]
    complete: bool = True


_CURSOR_PROBE_BYTES = 64


def _log_path_key(path: Path) -> str:
    """Stable-enough process-local path identity, including Windows case folding."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _file_identity(stat_result) -> Optional[tuple[int, int]]:
    """Return an OS file identity when the filesystem exposes one (Windows does via ``st_ino`` too).

    Deliberately a SUBSET of `core/atomicio.file_identity`: this asks only "is this the same file?",
    never "is it unchanged?" — the monitor tails a log that is expected to grow between reads, so
    including size/mtime would report a rotation on every ordinary append. It also returns None when
    the filesystem cannot prove identity (inode 0), which the canonical tuple has no way to express.
    """
    try:
        device = int(stat_result.st_dev)
        inode = int(stat_result.st_ino)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return (device, inode) if inode else None


def snapshot_training_logs(workdir) -> TrainingLogSnapshot:
    """Capture attempt-start byte cursors for the workdir's existing stage logs.

    The small boundary probe distinguishes append from truncate-and-regrow even when a filesystem does
    not expose a useful inode. A path that cannot be snapshotted is retained as an unreadable cursor so
    a transient permission/stat failure cannot make a later monitor consume prior-attempt bytes.
    """
    try:
        paths = list(Path(workdir).glob("*.log"))
    except OSError:
        return TrainingLogSnapshot({}, complete=False)
    cursors: dict[str, TrainingLogCursor] = {}
    for path in paths:
        key = _log_path_key(path)
        try:
            with open(path, "rb") as fh:
                stat_result = os.fstat(fh.fileno())
                size = max(0, int(stat_result.st_size))
                probe_start = max(0, size - _CURSOR_PROBE_BYTES)
                fh.seek(probe_start)
                probe = fh.read(size - probe_start)
            cursors[key] = TrainingLogCursor(
                offset=size,
                identity=_file_identity(stat_result),
                probe_start=probe_start,
                probe=probe,
            )
        except (OSError, TypeError, ValueError, OverflowError):
            cursors[key] = TrainingLogCursor(offset=None, identity=None, probe=None)
    return TrainingLogSnapshot(cursors)


def read_training_tail_raw(workdir, *, max_read_bytes: int = 131_072,
                           snapshot: Optional[TrainingLogSnapshot] = None,
                           plan: Optional[EvalLogPlan] = None) -> str:
    """The RAW (un-digested) utf-8 tail of the active stage log — the last `max_read_bytes`. Bounded
    seek-to-tail read so a multi-GB log never loads into memory; a torn leading line is dropped by the
    'replace' decode. '' when there is no log yet. Used by the ASHA watchdog, which feeds it to the
    eval's own metric reader (digesting first would collapse the very metric lines it must parse).

    With an attempt-start ``snapshot``, bytes that predate the current eval are excluded. Replacement,
    rotation, and truncation start a fresh file at byte zero; an unreadable/ambiguous old boundary fails
    closed to an empty tail rather than reusing a stale metric.

    With an eval ``plan`` the read is confined to the stage logs that can carry training output —
    `setup.log` and the protected scorer return '' rather than a tail, so neither watchdog classifies
    (or ranks) another phase's bytes as this training's.
    """
    path = active_training_log(workdir, plan)
    if path is None:
        return ""
    limit = max(0, int(max_read_bytes))
    if limit == 0:
        return ""
    try:
        with open(path, "rb") as fh:
            size = max(0, int(os.fstat(fh.fileno()).st_size))
            floor = attempt_byte_floor(fh, path, snapshot)
            if floor is None:
                return ""
            fh.seek(max(floor, size - limit))
            raw = fh.read(limit)
    except (OSError, TypeError, ValueError, OverflowError):
        return ""
    return raw.decode("utf-8", "replace")


def attempt_byte_floor(fh, path, snapshot: Optional[TrainingLogSnapshot]) -> Optional[int]:
    """The byte offset at or above which THIS eval attempt's bytes begin in the already-open `fh`.

    `None` means the boundary cannot be established and the caller must return nothing: a transient
    permission/stat failure at snapshot time must not let a monitor consume prior-attempt bytes.
    `0` (no snapshot, or a fresh file) means the whole file is this attempt's.

    Extracted from `read_training_tail_raw` so the SECOND reader of these bytes — the log tools the
    judge queries (`tools/log_tools.py`, wired in `monitor_log_sources`) — cannot come to a different
    conclusion about where the previous attempt ended. One boundary, two readers; the alternative is a
    role that seeks past a floor the digest respects and reads a dead attempt's curve as the live one's.
    Leaves `fh`'s position undefined — every caller seeks before reading.
    """
    if snapshot is not None and not snapshot.complete:
        return None
    stat_result = os.fstat(fh.fileno())
    size = max(0, int(stat_result.st_size))
    current_identity = _file_identity(stat_result)
    cursor = snapshot.cursors.get(_log_path_key(Path(path))) if snapshot is not None else None
    if cursor is None and snapshot is not None and current_identity is not None:
        # A rotation can rename the old file to another ``*.log`` path. Follow identity across
        # that rename so the renamed prior-attempt bytes are not mistaken for a brand-new log.
        cursor = next((old for old in snapshot.cursors.values()
                       if old.identity == current_identity), None)
    if cursor is None and snapshot is not None:
        # Some filesystems expose no stable inode. A matching old EOF probe is sufficient to
        # classify an unknown path as a renamed old log; a false match only suppresses advisory
        # evidence (safe), whereas treating it as new could resurrect a stale kill metric.
        for old in snapshot.cursors.values():
            if old.offset is None or old.probe is None or size < old.offset:
                continue
            fh.seek(old.probe_start)
            if fh.read(len(old.probe)) == old.probe:
                cursor = old
                break
    if cursor is None:
        return 0
    if cursor.offset is None or cursor.probe is None:
        return None
    replaced = (cursor.identity is not None and current_identity is not None
                and cursor.identity != current_identity)
    truncated = size < cursor.offset
    boundary_changed = False
    if not replaced and not truncated:
        fh.seek(cursor.probe_start)
        boundary_changed = fh.read(len(cursor.probe)) != cursor.probe
    # only a proven append may inherit the old EOF. Rotation/replacement or a
    # truncate-and-regrow (including past the old size before the first watchdog tick) starts
    # at zero; if identity is unavailable, the boundary probe supplies the same protection.
    return 0 if (replaced or truncated or boundary_changed) else cursor.offset


def monitor_log_sources(workdir, plan: Optional[EvalLogPlan] = None,
                        snapshot: Optional[TrainingLogSnapshot] = None) -> list:
    """The `tools/log_tools.LogSource` map for ONE eval: every stage log the plan can NAME, each with
    the eval phase that writes it and this attempt's byte floor.

    This is the whole of rule 2 in `tools/log_tools.py`'s boundary. What a role may read is exactly
    what `eval_log_plan` derived from the resolved pipeline — the node's own workdir output — so the
    tool never constructs a path from model input and there is nothing outside the workdir to name.
    A log the plan cannot attribute (`LOG_ROLE_AMBIGUOUS`) is left OUT for the same reason
    `resolve_stage_log` refuses to judge it: bytes nobody can attribute to a phase are not evidence.

    Deliberately WIDER than `read_training_tail_raw`'s single active log and deliberately NOT wider
    than the plan: `setup.log` and the scorer carry no TRAINING-health authority (`_NON_TRAINING_ROLES`,
    enforced in `should_monitor_kill`), but a judge that can read them can answer "did the dep install
    actually get the CUDA build" and "has the scorer started yet", which is the question the tail's
    absence of an answer used to be mistaken for evidence about. The ROLE rides on every source, so
    the model is always told which phase it is reading — the same fix `monitor_stage_context` makes
    for the spliced tail.

    Returns [] when there is no log yet. Import is function-local: `tools/` sits BELOW `engine/`, so a
    module-level import here would be the wrong direction for a module `tools` must never import back.
    """
    from looplab.tools.log_tools import LogSource
    try:
        candidates = sorted(Path(workdir).glob("*.log"))
    except OSError:
        return []
    sources: list = []
    for path in candidates:
        key = _log_name_key(path.name)
        if plan is not None:
            if key not in plan.roles:
                continue
            stage, role = plan.roles[key]
            if role == LOG_ROLE_AMBIGUOUS:
                continue
        else:
            role = LOG_ROLE_UNKNOWN
        floor = 0
        try:
            with open(path, "rb") as fh:
                boundary = attempt_byte_floor(fh, path, snapshot)
            if boundary is None:
                continue          # fail closed — the same direction `read_training_tail_raw` fails
            floor = boundary
        except (OSError, TypeError, ValueError, OverflowError):
            continue
        sources.append(LogSource(name=path.name, path=path, role=role, floor=floor))
    return sources


def read_training_tail(workdir, *, max_read_bytes: int = 131_072,
                       max_lines: int = 40, max_chars: int = 4000,
                       snapshot: Optional[TrainingLogSnapshot] = None,
                       plan: Optional[EvalLogPlan] = None) -> str:
    """Read only the LAST `max_read_bytes` of the active stage log and digest it (collapse tqdm
    re-renders, keep the recent trajectory). '' when there is no log yet.

    REVIEW NOTE (accepted, not fixed): this bounded seek-to-tail pattern (stat size → seek → read) also
    appears inline in `serve/routers/runs.py::_tail` and `events/eventstore.py::_disk_last_seq`. Each copy
    differs in its line-boundary handling (and there is no existing shared helper — `sandbox._clamp_tail_bytes`
    clamps an in-memory STRING, not a file), so a 3-call-site extraction is deferred as not worth the churn."""
    raw = read_training_tail_raw(workdir, max_read_bytes=max_read_bytes, snapshot=snapshot, plan=plan)
    if not raw:
        return ""
    return training_log_digest(raw, max_lines=max_lines, max_chars=max_chars)


def needs_log_snapshot(engine, eval_spec) -> bool:
    """Must this attempt take the pre-attempt log snapshot and resolve its log plan?

    A NAMED RULE because it has three independent readers with different lifetimes and the third one
    cannot take its own: the two watchdogs read the logs WHILE the attempt runs, and the repair
    triage reads them AFTER it has died. A snapshot is a "before" — deriving it lazily at the failure
    is too late, because by then the attempt's own bytes are in the file and there is nothing left to
    take a before OF, so every `LogSource.floor` would be 0 and a repairer would read its
    predecessor's curve as its own. So the decision has to be made where the attempt STARTS, on
    behalf of a reader that has not been asked for anything yet.

    It was an inline `or` of the first two clauses in `engine/evaluate.py::_evaluate`, and this
    change would have made it three — i.e. a rule no test could reach without driving a whole
    sandboxed eval. Driven on a throwaway copy of the tree, deleting the repair clause from that
    inline form left every guard in the suite green, which is why it is a function now. Every disjunct is gated on `eval_spec` because only the
    command-eval path writes the per-stage `<stage>.log` any of this is about; the `solution.py`
    paths (toy/dataset) have no such log and must keep paying nothing.
    """
    if not eval_spec:
        return False
    if getattr(engine, "_train_monitor", False):
        return True
    if getattr(engine, "_asha_live", False) and isinstance(eval_spec, dict):
        return True
    return bool(getattr(engine, "_repair_log_tools", False))


def _log_query_tools(workdir, log_plan, log_snapshot):
    """The `LogQueryTools` provider over THIS eval's stage logs, or None when there is no nameable
    log yet. The ONE construction both LOOK paths use — the gate is the caller's.

    It exists as its own function because there are now two roles that may look and they must not be
    able to disagree about WHAT is lookable. The source map is `monitor_log_sources` in both cases —
    one derivation of rule 2's boundary (`eval_log_plan` over the resolved pipeline) and one reading
    of rule 1's floor (`attempt_byte_floor`), so a repairer diagnosing attempt N reaches exactly the
    bytes the watchdog was reading while attempt N ran, and no byte of attempt N-1.

    The map stays a CALLABLE even on the repair path, where the eval is already over and it cannot
    move. That is deliberate: a second, frozen construction here would be a second answer to "which
    logs exist", and the whole point of this function is that there is one.
    """
    from looplab.tools.log_tools import LogQueryTools
    if not monitor_log_sources(workdir, log_plan, log_snapshot):
        return None
    return LogQueryTools(lambda: monitor_log_sources(workdir, log_plan, log_snapshot))


def monitor_log_tools(engine, workdir, log_plan=None, log_snapshot=None):
    """The log tools this tick's judge may LOOK with, or None to keep the historical one-shot call.

    A FREE FUNCTION taking the engine, not a mixin method, and that is the point — the same reason
    `engine/speculation_gate.py`'s envelope is one. BOTH watchdogs need it, so as a method it has to
    live on a mixin one of them does not have: on `TrainingMonitorMixin` an ASHA-only object raises
    AttributeError, and `_monitor_asha`'s own per-tick containment `except` swallows that into a
    watchdog that silently stops producing verdicts for the rest of a multi-hour eval.
    `tests/test_asha_monitor.py`'s `_AshaStub` is exactly that object and is what caught it. Moving it
    to `SharedEngineMixin` only moves which stub breaks; a free function depends on no MRO at all, and
    `getattr` below is total over a partially-built engine.

    None when `Settings.train_monitor_tools` is off or the eval has written no nameable log yet — and
    `None` is exactly what `structured_judge` treats as "no tools", so the off path is the plain
    `parse_structured` both judges have always made, byte for byte.

    The provider is built PER TICK and its source map is a CALLABLE, because both halves move: a new
    stage log appears when the pipeline advances, and the log the judge is being asked about changes
    with it. A provider frozen at eval start would answer a question about `train.log` while the model
    read `setup.log`.
    """
    if not getattr(engine, "_train_monitor_tools", False):
        return None
    return _log_query_tools(workdir, log_plan, log_snapshot)


def repair_log_tools(engine, workdir, log_plan=None, log_snapshot=None):
    """The log tools the CRASH/TIMEOUT TRIAGE judge may LOOK with, or None to keep the historical
    stderr-tail-only ask.

    WHY THE REPAIR PATH NEEDED THIS TOO (measured, `runs/rubertlite-dr-unified-v8` node 3, attempt 5)
    ---------------------------------------------------------------------------------------------
    The watchdogs got `read_log`/`metric_series` because a bounded tail cannot answer the question
    they are asked. Triage is asked a HARDER version of the same question — "why did this stage die"
    — and it was still working from a slice, and a much smaller one: `_eval_failure_text` hands it
    `res.stderr[-500:]`, five hundred CHARACTERS, where the watchdog's digest at least came off a
    128 KiB read.

    Node 3 declared a 22,000 s ceiling and was SIGKILLed at 22,003 s. Its `train.log` holds two
    progress bars with different totals: the training bar reached `10590/10590 [5:29:35]` and the
    stage then printed `{'train_runtime': 19775.3, …, 'epoch': 14.98}` — all 15 epochs, done — after
    which a RETRIEVAL bar started and was killed at `223/361 [31:29<19:50]`, about twenty minutes
    from finishing. The durable `node_repaired.error_in` for that attempt is 522 characters and
    contains ONLY the last two renders of the second bar. The triage verdict read the second bar's
    elapsed field as training progress — *"node 3 is still in epoch 1 at 31:20"*, and `31:20` is
    verbatim the `222/361` render — and prescribed halving the batch AND cutting `n_epochs` 15 -> 8.
    The evidence that refutes it was 83,697 characters back in the same file, on a plain
    non-progress-bar line. 6.1 GPU-hours were discarded — and then spent AGAIN: the epochs cut never
    landed (`repair_verify` stamped `unmet: ['grad_accum', 'n_epochs']` on that row, no repaired file
    sets it, `config.yaml` still reads 15), so attempt 6 re-ran the SAME 10,590 steps and, measured
    live at `1928/10590 [57:46]` = 1.798 s/step, projects 19,038 s of training + ~3,058 s of
    retrieval = 22,096 s against the same 22,000 s ceiling. A wrong diagnosis bought a fix that was
    inert against the actual failure, which is the cost this widening is meant to stop.

    So the fix is the same one, one role over: let it ASK the log instead of being handed the end of
    it. Everything else — the boundary, the floor, the source map — is `monitor_log_tools`' and is
    shared through `_log_query_tools` rather than written a second time.

    A FREE FUNCTION for the same reason its sibling is (see above), and gated on its OWN setting
    (`Settings.repair_log_tools`) rather than on `train_monitor_tools`: the two are different paid
    surfaces on different cadences, and an operator who turned the timer-fired watchdog's tools off
    to stop ~200 agentic ticks per node has said nothing about the one look that happens when a
    multi-hour node has already died. `getattr` is total over a partially-built engine.

    None also when the eval wrote no nameable log — which is every non-command eval (`solution.py`,
    toy, dataset) and every failure before the first stage opened its log. `UnifiedAgent.triage_crash`
    reads `None` as "no extra tools" and splices no invitation, so the off path is the historical
    prompt and the historical toolset, byte for byte.
    """
    if not getattr(engine, "_repair_log_tools", False):
        return None
    return _log_query_tools(workdir, log_plan, log_snapshot)


class TrainingMonitorMixin:
    """The engine's training-log monitor cluster. `self` IS the Engine (mixin convention — see
    orchestrator.py). Gated on `self._train_monitor`; started as a sibling task in `_evaluate`'s task
    group so it lives exactly as long as the eval and is cancelled with it."""

    def _monitor_cadence(self) -> float:
        """The BASE check interval, derived from the per-experiment time budget so a short training is
        watched often and a multi-hour one sparsely (a fixed 600s would miss a 5-minute run entirely and
        over-watch a 5-hour one). ~10% of the budget, clamped to [30s, 30min], then floored by the config
        `train_monitor_interval_s` so the user can force MORE-frequent checks. Falls back to the config
        interval when no budget is known (solution.py path / no eval_spec)."""
        cfg = max(0.02, float(getattr(self, "_train_monitor_interval_s", 600.0) or 600.0))
        budget = None
        fn = getattr(self, "_experiment_time_budget", None)
        if callable(fn):
            try:
                budget = fn()
            except Exception:  # noqa: BLE001 — cadence is advisory; a budget hiccup just uses the config
                budget = None
        if isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0:
            derived = min(1800.0, max(30.0, float(budget) * 0.1))
            return min(cfg, derived)         # config is an upper bound: the user can only tighten it
        return cfg

    def _training_verdict(self, digest: str, context: str, stage_context: str = "",
                          trajectory_text: str = "", tools=None) -> Optional[TrainingVerdict]:
        """One-shot LLM judgment of the live log (SYNC — the caller runs it in a worker thread). Uses the
        Developer's client (the Developer wrote the loop, so it knows what its own logs should look like)
        with a fresh, STATELESS structured call — it never mutates the shared role object, so it is safe to
        fire while the eval thread runs. The client records its own usage/cost. Returns None when there is
        no client (offline / toy path) or the model output can't be parsed — advisory, never fatal.

        `stage_context` (from `monitor_stage_context`) NAMES the eval phase the digest came from and sits
        immediately above the log header, where the model reads it as part of the evidence. This is the
        second, independent layer of the mis-scoped-verdict fix: scoping decides which log is read, and
        this decides whether a model shown the wrong one can NOTICE. Measured on the live endpoint with a
        flat-loss + `CUDA not available` scorer tail: with the stage identity present, `deepseek-v4-flash`
        answers "cannot determine, the stage is 'score' not 'train'" and `qwen3.5-122b` answers healthy —
        neither says `broken`. Without it the same tail reads as three separate `broken` clauses at once.
        `trajectory_text` (from `trajectory_context`) is the third such layer and the one that made the
        question ANSWERABLE: scoping decides which log is read, the stage identity decides whether a
        model shown the wrong one can notice, and this decides whether a model shown the right one has
        enough of it. Measured, the digest the judge received was ~10 loss values over ~30 seconds of a
        multi-hour run — see the trajectory section above for the two live cases where that produced a
        confident "not learning" about a run that had descended 4.73 and 1.41 respectively.

        `tools` (from `monitor_log_tools`) is the FOURTH such layer and the one that stops the engine
        choosing for the judge what it is allowed to see. The three above are all still fixed slices;
        this one lets the judge ASK — tail the log further back, read its start, search it for a
        traceback, or query the loss series over the whole run at a granularity it picks. The digest
        still arrives spliced, so a model that ignores the tools answers exactly as it did before;
        `_LOOK_INVITATION` is what tells it they are there, and it is spliced at the SAME position
        pattern as the three above (empty when there are no tools, reproducing the historical message
        byte for byte).

        Additive by construction: `_MONITOR_SYSTEM` and the log header are unchanged (prompt strings are
        contracts), and an empty `stage_context`/`trajectory_text` reproduces the historical message byte
        for byte."""
        client = getattr(getattr(self, "developer", None), "client", None)
        if client is None:
            return None
        messages = [
            {"role": "system", "content": _MONITOR_SYSTEM},
            {"role": "user", "content": ((context + "\n\n") if context else "")
             + ((stage_context + "\n\n") if stage_context else "")
             + ((trajectory_text + "\n\n") if trajectory_text else "")
             + ((_LOOK_INVITATION + "\n\n") if tools is not None else "")
             + "LIVE TRAINING LOG (recent tail):\n" + digest
             + "\n\nClassify this run's health from the log evidence above."},
        ]
        try:
            from looplab.trust.judge import structured_judge
            # `parser="tool_call"` is what the two other judges in this repo use, and `structured_judge`
            # falls back to the plain `parse_structured` whenever the tool loop yields nothing valid —
            # so an agentic hiccup degrades to the historical verdict rather than to no verdict.
            return structured_judge(client, messages, TrainingVerdict, parser="tool_call",
                                    tools=tools, max_turns=_MONITOR_LOOK_TURNS)
        except Exception:  # noqa: BLE001 — a parser/endpoint failure means "no verdict this tick", not a crash
            return None

    @in_llm_lane("enrichment")
    async def _monitor_training(self, node_id: int, generation: int, workdir, cancel,
                                context: str = "", kill_signal: Optional[dict] = None,
                                log_snapshot: Optional[TrainingLogSnapshot] = None,
                                log_plan: Optional[EvalLogPlan] = None) -> None:
        """Tail the live training log every `train_monitor_interval_s`, ask the Developer to judge its
        health, record the verdict, and (opt-in) kill a broken run early.

        WHICH log (`log_plan`, from `eval_log_plan`): the monitor lives across the WHOLE eval — setup,
        every stage, the always-appended score stage — so "the freshest `*.log`" is not a synonym for
        "the training". With a plan it reads only the stage logs that can carry the candidate's own
        output and the judge is TOLD which stage it is looking at; `setup.log`, the pipeline's scorer
        and an unattributable filename produce no tick at all. Which of the logs it DOES read may kill
        is a second, stricter question, answered by `eval_log_plan` (only a log that is the whole eval)
        and enforced in `should_monitor_kill`. Without a plan it keeps the historical freshest-file
        read but can never kill — the monitor must not act on a stage it cannot identify.

        Advisory (always): every tick with a CHANGED digest emits a `train_monitor` trace span carrying
        the verdict; a NON-healthy verdict additionally appends an EV_TRAIN_MONITOR_ALERT diagnostic event
        (fold-ignored, so it cannot directly change lifecycle/champion/replay). The raw
        diagnostic can still steer a later Researcher prompt when watchdog_reflection is enabled, and it
        also feeds the owner attention view + audit.
        Healthy verdicts stay trace-only except for a healthy transition after an alert; that explicit
        recovery row lets every lifecycle projection clear the earlier warning.

        Intervention (Phase 3, only when `_train_monitor_kill` is on): a confident 'broken' verdict about
        an identified training stage ARMS the gate and schedules a prompt re-look; the second consecutive
        such verdict claims the kill — the monitor records the reason into `kill_signal` and sets `cancel`
        (the SAME tree-kill path an operator abort uses), then stops. `_evaluate` sees the killed eval and
        writes the node's single terminal `node_failed` (reason='monitor_broken'); replay reconstructs the
        node from that terminal and never re-invokes the LLM. A plateau is 'watch', never 'broken', so it
        is never killed — and a `broken` verdict the engine's own measured trajectory contradicts is not
        killed either: it neither arms the gate nor claims, it records the measurement beside the verdict
        and keeps watching. The alert row records whether this monitor actually OWNED that terminal, so an
        audit of "which watchdog stopped what" reads the durable log instead of guessing.

        With no LLM client wired it degrades to trace-only observation. Exits when the eval finishes
        (`cancel`, or the task group is cancelled); a per-tick hiccup skips the tick and never disables the
        watcher for the rest of a long eval."""
        # Local: `evaluate` imports this module, so a module-level import would be a cycle.
        from looplab.engine.evaluate import _watch_limiter
        import anyio

        from looplab.events.types import DIAGNOSTIC_EVENTS, EV_TRAIN_MONITOR_ALERT
        # Base cadence derived from the per-experiment time budget (Phase 2): a short training is watched
        # often, a multi-hour one sparsely. The next delay adapts per verdict — the observer self-paces
        # (LLM `recheck_after_s`) and a steadily-healthy run backs off — via `next_monitor_sleep`.
        base = self._monitor_cadence()
        next_sleep = base
        last_digest: Optional[str] = None
        healthy_streak = 0
        last_event_status: Optional[str] = None
        # resume may restart the observer inside the same node generation. Recover its last
        # durable state so the first healthy verdict can close a pre-crash warning instead of losing it.
        try:
            prior_rows = await anyio.to_thread.run_sync(
                self.store.read_all, limiter=_watch_limiter())
            prior = last_lifecycle_row(
                prior_rows, EV_TRAIN_MONITOR_ALERT, node_id, generation)
            if prior is not None:
                status = str(prior.get("status") or "").strip().lower()
                last_event_status = status if status in ("healthy", "watch", "broken") else None
        except Exception:  # noqa: BLE001 - advisory history lookup; the live monitor still proceeds
            pass
        llm_calls = 0
        # Phase 3 arming state. `broken_streak` counts CONSECUTIVE confident-broken verdicts about the
        # same stage log; `armed_key` is the log they were about, so a stage change (train.log ->
        # score.log) can never let two different subjects confirm each other. `armed_at` is set only
        # when the KILL GATE actually arms (a broken verdict about a kill-eligible log) and is what
        # licenses the changed-digest bypass; with `arm_looks` it bounds how long and how expensively
        # an arm may stand (`_MONITOR_ARM_TTL_S` / `_MONITOR_ARM_MAX_LOOKS`). `disarm()` is the ONE
        # spelling of "re-arm from zero", so every path listed on `_MONITOR_KILL_CONFIRM_TICKS`
        # provably resets the same three variables.
        broken_streak = 0
        armed_key: Optional[str] = None
        armed_at: Optional[float] = None
        arm_looks = 0
        # Bounded retry of one unchanged digest whose verdict never parsed (`_MONITOR_SAME_DIGEST_RETRIES`).
        failed_digest: Optional[str] = None
        failed_digest_tries = 0
        # The run-scale loss curve, accumulated one reduction per tick from the tails this loop
        # already reads (see `LossTrajectoryTracker`). It is what the judge is shown besides the
        # tail and what `should_monitor_kill` consults; it is reset when the active stage log
        # changes, alongside the kill gate, because two stages are two curves.
        tracker = LossTrajectoryTracker()
        # Tracked separately from `last_digest`, which is committed only on a PARSED verdict: an
        # endpoint failure must not make the same window be counted twice as two readings.
        last_tracked: Optional[str] = None

        def disarm() -> None:
            nonlocal broken_streak, armed_at, arm_looks
            broken_streak, armed_at, arm_looks = 0, None, 0

        while True:
            await anyio.sleep(next_sleep)    # only cancellation (eval finished) unwinds the task, from here
            if cancel.is_set():
                return
            try:
                def _observe_log():
                    """ONE attributed read per tick, in the worker thread: which stage log is live, and
                    (only when that phase can carry the candidate's own output) its digested tail.
                    `setup.log`, the pipeline's scorer and an unattributable filename return no tail at
                    all — there is nothing for a training-health prompt to say about a dep install or
                    about a scorer running after the training already finished, and asking anyway is
                    what produced a confident wrong verdict."""
                    resolved = resolve_stage_log(workdir, log_plan)
                    if resolved is None or resolved.role in _NON_TRAINING_ROLES:
                        return resolved, ""
                    return resolved, read_training_tail(workdir, snapshot=log_snapshot, plan=log_plan)

                resolved, tail = await anyio.to_thread.run_sync(
                    _observe_log, limiter=_watch_limiter())
                log_role = resolved.role if resolved is not None else LOG_ROLE_UNKNOWN
                log_key = _log_path_key(resolved.path) if resolved is not None else None
                if log_key != armed_key:
                    armed_key = log_key
                    disarm()                 # a different subject re-arms from zero
                    tracker.reset()          # ...and a different subject is a different curve
                    last_tracked = None
                elif armed_at is not None and (
                        anyio.current_time() - armed_at) > _MONITOR_ARM_TTL_S:
                    disarm()                 # a stale arm is not evidence — see _MONITOR_ARM_TTL_S
                # KNOWN BLIND SPOT of this changed-digest gate: a HUNG training (process alive, no
                # new log output) holds the digest constant forever, so the LLM is never consulted
                # again and the hang is never judged here. The STALL watchdog in `run_argv` is what
                # catches that case — it is output-based and tree-kills on silence — so this monitor
                # deliberately stays a judge of what the run SAYS, not of whether it says anything.
                #
                # ONE exception: once the kill gate is ARMED, the confirming look must happen even if
                # the log has gone quiet since. "Diverged, then stopped printing" is precisely the run
                # the confirmation is meant to end, and skipping it there would turn the confirmation
                # window into a way for a broken run to survive by saying nothing.
                if not tail:
                    continue                 # no live log yet (or none this watchdog may read)
                # MEASURE BEFORE ASKING, and measure on EVERY tick with new bytes — including the
                # ones the changed-digest gate below then declines to spend an LLM call on, and the
                # ones whose verdict never parses. The trajectory's value is its span, so a window
                # skipped here is a hole in the run's history that no later tick can refill.
                if tail != last_tracked:
                    tracker.observe(tail, at=anyio.current_time())
                    last_tracked = tail
                trajectory = tracker.summary()
                unchanged = tail == last_digest
                if unchanged:
                    if armed_at is None:
                        continue             # nothing new since last tick -> no LLM call
                    if arm_looks >= _MONITOR_ARM_MAX_LOOKS:
                        # The arm has already bought its one re-ask of this identical digest. Asking
                        # again buys a byte-identical prompt, not a second sample, so disarm and let
                        # the ordinary changed-digest gate take over (see _MONITOR_ARM_MAX_LOOKS).
                        disarm()
                        continue
                    arm_looks += 1
                # Open the span BEFORE the LLM call so the observer's LLM turn bands under `train_monitor`
                # (not the enclosing `evaluate`) — the same trace-attribution fix `_triage_crash` uses.
                with self.tracer.span("train_monitor", node_id=node_id) as sp:
                    sp.set_many(generation=generation, log_role=log_role,
                                digest_lines=tail.count("\n") + 1, digest_chars=len(tail))
                    if resolved is not None and resolved.stage:
                        sp.set("stage", resolved.stage)
                    if trajectory.windows:
                        # The measured curve on the span too, so "why did (or didn't) it act" is
                        # answerable from the trace and not only from the durable alert row.
                        sp.set_many(trajectory=trajectory.direction,
                                    trajectory_windows=trajectory.windows,
                                    trajectory_points=trajectory.points)
                    if unchanged:
                        # The confirming look at a FROZEN log re-asks a byte-identical question. It
                        # defends against sampling noise, never against a systematic misread — say so
                        # on the span rather than letting "two verdicts" imply two observations.
                        sp.set("confirm_digest_unchanged", True)
                    # Per-node backstop on LLM cost (the adaptive cadence + healthy-backoff are the primary
                    # budget control; this only bounds a pathological run whose digest keeps changing while
                    # staying non-healthy). Past the cap we keep OBSERVING (trace-only) but stop calling the
                    # LLM. Surfaced on the span — a silent cap would read as "all healthy" when it isn't.
                    verdict = None
                    if llm_calls >= _MAX_MONITOR_LLM_CALLS:
                        sp.set("llm_capped", True)
                    elif cancel.is_set():
                        # The eval ended (finished, operator abort, or the SIBLING ASHA watchdog already
                        # claimed the kill) while this tick was reading the log. Starting the call now
                        # would buy a verdict about a node that no longer exists AND — because the call
                        # is deliberately un-abandonable below — hold node teardown open for a whole
                        # endpoint timeout to pay for it. Checked here, immediately before the spend.
                        sp.set("cancelled_before_call", True)
                        return
                    else:
                        # the verdict is advisory, but its client usage is billable and is
                        # recorded on shared run state. Join an in-flight call on eval cancellation so
                        # no detached worker can emit cost after the node/run has finalized. Endpoint
                        # timeouts remain the upper bound for this ownership hand-off.
                        verdict = await anyio.to_thread.run_sync(
                            self._training_verdict, tail, context,
                            monitor_stage_context(resolved, log_plan),
                            trajectory_context(trajectory),
                            # Built HERE, per tick, in the same thread hand-off as the call it serves:
                            # the tool reads the live log while the judge is thinking, so it must see
                            # the file as it is now, not as it was when the eval started.
                            monitor_log_tools(self, workdir, log_plan, log_snapshot),
                            abandon_on_cancel=False)
                        llm_calls += 1
                    if verdict is None:
                        # NO PARSEABLE ANSWER this tick — an endpoint failure, model output that failed
                        # schema validation (`unknown` is not a `TrainingVerdict.status`), or the
                        # per-node LLM cap. Under `_MONITOR_KILL_CONFIRM_TICKS` that is "anything
                        # else", so it re-arms the gate from zero: the streak used to be touched only
                        # inside this branch's `else`, which left an arm standing across six `unknown`
                        # ticks and let the NEXT `broken` kill as though the two were consecutive.
                        if armed_at is not None:
                            next_sleep = base   # drop the shortened confirmation cadence with the arm
                        disarm()
                        sp.set("verdict_unparsed", True)
                        # Bounded same-digest retry (see `_MONITOR_SAME_DIGEST_RETRIES`): `last_digest`
                        # is otherwise committed only on a usable verdict, so an endpoint that never
                        # answers re-sent a byte-identical prompt every cadence until the LLM cap.
                        if tail == failed_digest:
                            failed_digest_tries += 1
                        else:
                            failed_digest, failed_digest_tries = tail, 1
                        if failed_digest_tries >= _MONITOR_SAME_DIGEST_RETRIES:
                            last_digest = tail   # retire it: quiet until the log actually changes
                            sp.set("digest_retired", True)
                    else:
                        conf, confidence_valid = _normalize_monitor_confidence(verdict.confidence)
                        # The reason is LLM text derived from the raw log; redact it before it lands in the
                        # trace / event log / attention feed, matching how `_evaluate` stores stderr tails.
                        _redact = getattr(self, "_redact", None)
                        reason = (verdict.reason or "")
                        reason = (_redact(reason) if callable(_redact) else reason)[:300]
                        # The durable event keeps the fuller reason (300); the trace span carries a shorter
                        # preview (200) — spans are a high-volume sidecar, the event is the authoritative record.
                        sp.set_many(status=verdict.status, confidence=round(conf, 3), reason=reason[:200])
                        if not confidence_valid:
                            sp.set("confidence_valid", False)
                        failed_digest, failed_digest_tries = None, 0   # this digest WAS judged
                        healthy_streak = healthy_streak + 1 if verdict.status == "healthy" else 0
                        if verdict.status == "broken":
                            broken_streak += 1
                        else:
                            disarm()         # a parseable non-broken verdict re-arms from zero
                        next_sleep = next_monitor_sleep(
                            base, status=verdict.status, recheck_after_s=verdict.recheck_after_s,
                            healthy_streak=healthy_streak)
                        # Phase 3 intervention (opt-in): a CONFIRMED, confident 'broken' verdict about an
                        # identified training stage is tree-killed EARLY. Hand the reason to `_evaluate`
                        # via `kill_signal`, set `cancel` (same path as an operator abort), and stop
                        # watching — `_evaluate` writes the single terminal node_failed.
                        # No `or`-coercion on the confidence bar. `x or 0.0` turns an unset/None/0.0
                        # knob into a ZERO threshold — i.e. EVERY `broken` verdict kills — which is
                        # the wrong direction to fail in, and it matters much more now that
                        # `train_monitor_kill` defaults to True. Mirrors the identical rule in
                        # `asha_monitor.py`; a non-numeric knob falls back to the schema default.
                        _kc = getattr(self, "_train_monitor_kill_confidence", 0.8)
                        threshold = (float(_kc) if isinstance(_kc, (int, float))
                                     and not isinstance(_kc, bool) else 0.8)
                        stop_decided = kill_signal is not None and should_monitor_kill(
                            verdict, enabled=getattr(self, "_train_monitor_kill", False),
                            threshold=threshold, log_role=log_role, broken_streak=broken_streak,
                            trajectory=trajectory)
                        # The COUNTERFACTUAL, evaluated only when the measurement is what refused.
                        # Pure and cheap, and it is what makes "the monitor would have ended this
                        # node but for the curve it measured" a durable fact rather than something
                        # an auditor has to re-derive from a log that has since grown.
                        trajectory_veto = (not stop_decided and kill_signal is not None
                                           and trajectory_vetoes_kill(trajectory)
                                           and should_monitor_kill(
                                               verdict,
                                               enabled=getattr(self, "_train_monitor_kill", False),
                                               threshold=threshold, log_role=log_role,
                                               broken_streak=broken_streak))
                        if trajectory_veto:
                            sp.set("trajectory_veto", True)
                        # CLAIM BEFORE RECORDING. The sibling ASHA watchdog can decide on the same tick,
                        # and only one of them owns the node's terminal — so the alert must state what
                        # actually happened to the node, not what this monitor wanted. The guard->update
                        # inside `claim_watchdog_kill` is await-free, so the answer is exact.
                        claimed = stop_decided and claim_watchdog_kill(
                            kill_signal, cancel, reason=reason, terminal_reason="monitor_broken",
                            confidence=round(conf, 3))
                        if stop_decided:
                            sp.set_many(stop_decided=True, kill=bool(claimed))
                        elif (verdict.status == "broken" and broken_streak == 1
                              and log_role in _KILL_ELIGIBLE_ROLES and kill_signal is not None
                              and getattr(self, "_train_monitor_kill", False)
                              and not trajectory_vetoes_kill(trajectory)):
                            # ARMED, not acting: re-look promptly instead of after another full cadence
                            # (up to 30 min on a long budget), so confirmation costs seconds of a
                            # multi-hour budget rather than a meaningful slice of it. `armed_at` starts
                            # the TTL at the TRANSITION, never on a later broken tick — otherwise a
                            # flapping log could renew the arm indefinitely. It is also what licenses
                            # the changed-digest bypass, so a `LOG_ROLE_WORK` stage (advisory, this
                            # branch not taken) never buys a re-look it could not act on. The
                            # measured-trajectory veto is a conjunct here for exactly that reason:
                            # a confirmation that `should_monitor_kill` will refuse anyway is a
                            # billable re-ask and a changed-digest bypass bought for nothing.
                            next_sleep = min(next_sleep, _MONITOR_CONFIRM_DELAY_S)
                            armed_at, arm_looks = anyio.current_time(), 0
                            sp.set("kill_armed", True)
                        sp.set("next_check_s", round(next_sleep, 2))
                        if (verdict.status != "healthy"
                                or last_event_status in ("watch", "broken")):
                            # healthy is normally trace-only, but the transition from an alert
                            # is a durable recovery edge. Without it, projections can only ever discover the
                            # old bad verdict and keep warning after the live curve has recovered.
                            assert EV_TRAIN_MONITOR_ALERT in DIAGNOSTIC_EVENTS
                            alert = {
                                "node_id": node_id, "generation": generation,
                                "status": verdict.status, "reason": reason,
                                "confidence": round(conf, 3),
                                # STAGE ATTRIBUTION on the DURABLE row, not only on the trace span.
                                # The span is a self-described high-volume sidecar; the alert is the
                                # authoritative record and the one every projection reads, so without
                                # these `watchdog_reflection` told the next Researcher "node 0:
                                # training flagged broken ... loss is stuck at its initialization
                                # value" about a node whose training had not started. Additive and
                                # fold-ignored; readers default an absent role to "unknown".
                                "log_role": log_role}
                            if resolved is not None and resolved.stage:
                                alert["stage"] = str(resolved.stage)[:64]
                            # THE MEASUREMENT beside the verdict. `watchdog_reflection` narrates
                            # this row to the next Researcher, and on v7 that meant carrying "loss
                            # pinned at ~23.0 ... no learning trend" into the next proposal about a
                            # run that had gone 24.28 -> 22.90. Additive and fold-ignored; an
                            # absent `trajectory` means the engine measured nothing (an old row, or
                            # a log printing no parseable loss), NEVER that the loss was flat.
                            measured = trajectory_row(trajectory)
                            if measured is not None:
                                alert["trajectory"] = measured
                            if trajectory_veto:
                                alert["trajectory_veto"] = True
                            if not confidence_valid:
                                alert["confidence_valid"] = False
                            if stop_decided:
                                # Attribution, additive and fold-ignored, using the SAME vocabulary as
                                # the sibling EV_ASHA_VERDICT row: `stop_decided` is what this monitor
                                # decided, `kill` whether it then WON the shared per-eval claim. Neither
                                # says the node stopped — `_evaluate` terminalizes a claim only
                                # `if kill_signal.get("kill") and not ok`, so a claim against an eval
                                # that already produced a usable result still ends `node_evaluated`. The
                                # node's single terminal remains the authority on the outcome.
                                alert["stop_decided"] = True
                                alert["kill"] = bool(claimed)
                                if not claimed:
                                    alert["kill_superseded_by"] = str(
                                        kill_signal.get("terminal_reason") or "")[:64]
                            # Once claimed, `_evaluate` cancels this task group; `self._write_lock` is
                            # the next checkpoint, so an unshielded append would be preempted and the
                            # kill would leave NO diagnostic behind. Bounded (one append), and the same
                            # shielding `_evaluate` uses for its own promised terminal.
                            with anyio.CancelScope(shield=stop_decided):
                                async with self._write_lock:
                                    self.store.append(EV_TRAIN_MONITOR_ALERT, alert)
                        last_event_status = verdict.status
                        # Committed only once a USABLE verdict came back. Setting it before the call
                        # meant a transient endpoint failure (verdict None) permanently skipped
                        # judging THIS digest: the monitor went quiet until the log changed again,
                        # which for a slow-logging stage is a long window to be blind in. That
                        # protection is now BOUNDED rather than unconditional — after
                        # `_MONITOR_SAME_DIGEST_RETRIES` failed attempts the None branch above commits
                        # the same digest itself, so an endpoint that never answers stops re-sending a
                        # byte-identical prompt every cadence.
                        last_digest = tail
                        if stop_decided:
                            return           # won or lost, this node is ending — stop watching it
            except anyio.get_cancelled_exc_class():
                raise                        # cooperative cancellation — must propagate, never be swallowed
            except Exception:  # noqa: BLE001 — a transient per-tick hiccup (disk/LLM/tracer) SKIPS this tick;
                continue                     # it must never disable the watcher for the rest of a long eval
