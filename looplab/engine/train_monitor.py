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
# The median the trajectory reduces its windows with. It was a byte-identical private copy here and
# in `tools/log_tools.py`, which reduce the SAME log one trust tier apart (the deterministic veto's
# per-window median, and the judge-facing `metric_series` bucket median) — see `core/numeric.py`.
from looplab.core.numeric import median as _median
# The log-role vocabulary is stamped on the DURABLE `EV_TRAIN_MONITOR_ALERT` row, so it lives in
# `events/types.py` where readers below the engine (`events/digest.py`'s `watchdog_reflection`) can
# name a role without importing the engine (layering: `events` imports only `core`). Imported here
# and re-exported, because `train_monitor.LOG_ROLE_*` is the spelling engine code and tests use.
# `events.types` imports nothing, so this module-level import cannot become a cycle.
# The MANIFEST vocabulary (what a stage may declare) is defined once beside the validator that is
# the single definition of a valid stage; this module maps it to the LOG-ROLE vocabulary above.
# Two names deliberately, not one shared constant: the manifest key is a contract with the agent
# and the operator, the log role is a contract with every reader of the durable alert row, and a
# test pins that they still agree. `engine` imports `runtime` throughout, never the reverse.
from looplab.runtime.command_eval import STAGE_ROLE_TRAINING
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
    fault: Literal["implementation", "hypothesis", "environment", "unknown"] = Field(
        default="unknown",
        description="Only meaningful when status is 'broken': WHOSE fault it is, because the two get "
                    "opposite treatments. 'implementation' = the code or its configuration is wrong "
                    "and a fix is available — a parameter set to a value the log itself shows is "
                    "absurd, a loss that cannot descend as written, a normalization or reduction "
                    "applied to the wrong axis, a data loader feeding the same batch, a checkpoint "
                    "never saved. This STOPS the run and hands it back for REPAIR, so the experiment "
                    "is retried once the bug is fixed. 'hypothesis' = the code is doing exactly what "
                    "it was told and the IDEA is what failed; the run's poor result is a real "
                    "finding and must be recorded as one, not fixed away. 'environment' = neither "
                    "(a missing device, a broken mount, an OOM). 'unknown' = you cannot tell from "
                    "the evidence — say that rather than guessing, it is the safe answer. Prefer "
                    "'implementation' ONLY when you can name the specific thing to change; a "
                    "hypothesis wrongly called a bug costs a repair round, a bug wrongly called a "
                    "hypothesis records a verdict about an idea that was never actually tested.")
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
    "evidence. Be concise and specific about the evidence you saw. When you do call something broken, "
    "also say WHOSE fault it is (`fault`): a bug in the code or its configuration is REPAIRED and "
    "retried, while a sound implementation of a bad idea is a real result and is recorded as one. "
    "Look before you attribute — the run's own log is where its parameters, its device and its data "
    "shapes were echoed.")


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
    "more evidence. You can also READ THE CODE this run is executing — `list_dir`/`find_files` to "
    "see what is in the workdir, `read_file` for a whole file, `grep` to find where a symbol, flag "
    "or config key is set. That is what tells a BUG from a bad idea: a loss that is frozen because "
    "the objective cannot descend as written looks exactly like one frozen because the idea does "
    "not work, and only the source says which. Read before you attribute `fault`, and name what "
    "you found.")

# The tool-loop turn budget for ONE monitor tick. Deliberately well below `trust.judge.JUDGE_MAX_TURNS`
# (15): this judge fires up to `_MAX_MONITOR_LLM_CALLS` times per node on a timer, so a turn budget is
# multiplied by ~200 in a way the two one-shot verifiers' never is. Six was enough for the shape the
# invitation asked for when it was logs only — a whole-run series, a narrower one, a search, and
# the emit — and a loop that spends it degrades to `parse_structured` on the same messages rather
# than to nothing. NINE since 2026-08-18, because the invitation now asks for one more THING and
# that thing takes more than one turn: attributing `fault` means locating the file that sets the
# parameter (a `grep`), reading it (a `read_file`, possibly a second page), and doing that WITHOUT
# giving up the log evidence the verdict is primarily about. A budget that forces the judge to
# choose between looking at the curve and looking at the code produces exactly the guess the
# attribution exists to replace. Still far below `trust.judge.JUDGE_MAX_TURNS` (15) and still
# multiplied by ~200 ticks per node, which is why it moved by three and not by ten.
_MONITOR_LOOK_TURNS = 9


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
# `declared_condition_violated` — which are out of its reach BY NAME. The four genuinely diverged
# nodes in that corpus (n15 `loss=inf` for 20 epochs, n60 `nan`, n68 `-2e+10`, n74 `-2.35e+08`) name
# `nan_or_inf_loss` and are refused by the kind test before any curve is read; `anomalous` is the
# second, independent refusal for a diverged run the model happened to label the other way.
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
      own training — `eval_log_plan` grants that role to a log that is the WHOLE eval, or to the one
      stage a manifest DECLARES is the training loop (see that function on why a declaration is
      admissible where a stage name is not). A verdict
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


# What a repair-stop terminalizes as. In `FAILURE_REASONS`, therefore in the default
# `inline_repair_reasons`, therefore picked up by the inline repair loop with no new plumbing —
# which is the whole design: the engine already knows how to hand a failed node back to its
# Developer, and this only decides WHICH failures deserve that instead of a verdict.
MONITOR_REPAIR_REASON = "not_learning"


def should_monitor_repair(verdict: Optional["TrainingVerdict"], *, enabled: bool, threshold: float,
                          log_role: str = LOG_ROLE_UNKNOWN, broken_streak: int = 0,
                          confirm_ticks: int = _MONITOR_KILL_CONFIRM_TICKS,
                          trajectory: Optional["LossTrajectory"] = None) -> bool:
    """Whether a verdict warrants stopping this stage FOR REPAIR. Pure/deterministic.

    The sibling of `should_monitor_kill`, and the reason the role gate can finally open. Every
    conjunct there is about ONE cost: a kill discards a multi-hour training with no repair, no retry
    and no refunded `max_nodes` slot, so the monitor must not hold that gun over a stage it cannot
    identify. A repair-stop costs something else entirely — one restart of a run the judge has just
    said is wasted, with the diagnosis attached — so being wrong here is recoverable in the way
    being wrong there is not. That is why this admits EVERY role the judge is allowed to read:

    - a `mine` stage feeding empty negatives, a post-train stage exporting a broken checkpoint and a
      five-stage pipeline's third stage are all things the code can be wrong about, and none of them
      is the training loop. Refusing to act on them was never a judgement that they are healthy; it
      was a judgement that the only available action was too expensive to risk.
    - `_NON_TRAINING_ROLES` still cannot reach here, because they are not judged at all
      (`active_training_log` returns None for the dep install, the scorer and an unattributable
      filename), so there is no verdict about them to act on.

    `fault == "implementation"` is the load-bearing conjunct and it is the model's own attribution:
    the schema tells it that a bug is repaired and a bad idea is recorded, and that `unknown` is the
    safe answer. Everything else is the kill gate's arithmetic unchanged — the same confidence bar,
    the same repeated-verdict requirement, and the same measured-trajectory veto, because a curve
    that is still descending is not evidence of anything being wrong with the code either.
    """
    if not enabled or verdict is None or verdict.status != "broken":
        return False
    if getattr(verdict, "fault", "unknown") != "implementation":
        return False
    if log_role in _NON_TRAINING_ROLES:
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


def _confirmation_would_act(verdict: Optional["TrainingVerdict"], *, enabled: bool, threshold: float,
                            log_role: str = LOG_ROLE_UNKNOWN,
                            trajectory: Optional["LossTrajectory"] = None,
                            confirm_ticks: int = _MONITOR_KILL_CONFIRM_TICKS) -> bool:
    """Whether REPEATING this verdict would reach an intervention. Pure/deterministic.

    The arming question, and it is deliberately the same COUNTERFACTUAL shape as the `role_withheld`
    / `trajectory_veto` receipts in `_monitor_training`: ask the real predicates with the one thing
    the tick is missing — the streak — already satisfied, rather than re-listing their conjuncts at
    the arming site where the two copies can silently disagree. They did: see the comment at that
    call site for both directions of the drift the hand-written list carried.

    It grants no authority of its own. Both predicates are unchanged, this only decides WHEN the
    second look happens, and the second look is still judged on its own evidence — so this can never
    turn a verdict the gate refuses into one it accepts.
    """
    return bool(
        should_monitor_kill(verdict, enabled=enabled, threshold=threshold, log_role=log_role,
                            broken_streak=confirm_ticks, confirm_ticks=confirm_ticks,
                            trajectory=trajectory)
        or should_monitor_repair(verdict, enabled=enabled, threshold=threshold, log_role=log_role,
                                 broken_streak=confirm_ticks, confirm_ticks=confirm_ticks,
                                 trajectory=trajectory))


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
    # The DECLARED outputs of the stage that declared itself the training loop — the evidence
    # `training_authority_spent` uses to notice that training is already over. () whenever the
    # training role was not bought by a declaration (single command, one-stage pipeline), so that
    # path keeps its behaviour byte-for-byte.
    training_artifacts: tuple = ()


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
    - a stage the MANIFEST declares as the training loop (`role: "training"`, validated by
      `command_eval.validate_stages`, at most one per pipeline, never the positional scorer) AND
      that declares the `expect.files` its authority is spent against — see below;
    - every other pipeline stage is `LOG_ROLE_WORK`: still read, still judged, still alerting — but
      ADVISORY.

    WHY A DECLARATION IS ADMISSIBLE EVIDENCE. Everything below argues that a stage NAME proves
    nothing, and none of that changed — `train` is still just a slug. What the declaration adds is
    not a better guess but a different KIND of fact: the manifest is the same authenticated
    artifact the engine already trusts to say what runs, in what order, with what timeout and what
    output contract, and it can only ever be spent in one direction. Omit `role` and the stage
    keeps precisely the advisory role it has today; write it and the only thing bought is the power
    to have YOUR OWN stage stopped — a kill carries no repair, no retry and no refunded slot, so
    there is no reading under which a declarer profits. Compare the alternative that was rejected:
    admitting `LOG_ROLE_WORK` to the kill set whenever the measured trajectory corroborates. That
    fails on this function's own worked example — the `data_prep` stage printing a flat
    `loss: 0.6931` is exactly a frozen curve, so the corroboration fires hardest on the false
    positive it was meant to filter, and it would promote the deterministic half from VETO to
    CONFIRM, which is a widening of authority docs/36 reserves for evidence the record can
    authenticate.

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
    # The manifest's own answer to "which stage is the training loop", when it gave one — mapped to
    # the artifacts that can SPEND the authority again. `validate_stages` is the single definition of
    # a valid stage and admits exactly one such declaration, so this reads at most one name; anything
    # else is a manifest that never reached here. Read from the CLEANED dicts the engine resolved,
    # never from raw operator/agent text.
    #
    # A DECLARATION WITH NO `expect.files` BUYS NOTHING, and that is fail-closed rather than mean.
    # `training_authority_spent` is the entire price of admitting a declaration: the authority ends
    # the moment the stage's own promised artifact exists, because a stage that also scores
    # in-process (`e5small-dr-unified-v2`'s `train.log` ends `RECALL@100: 0.793344`) cannot be taken
    # at its word about which phase it is in. With no declared artifact there is nothing to observe
    # and the authority could never be handed back — so the gun would be held over the in-process
    # scoring phase too, which is the H-1 defect this whole mechanism exists to keep out. Granting
    # `LOG_ROLE_WORK` instead is exactly the behaviour the manifest had before it declared anything,
    # and it is VISIBLE: with no `LOG_ROLE_TRAINING` in the plan, every tick's span carries
    # `kill_reachable: false` from the first one, which is the same signal a pipeline that declared
    # nothing gets. (Not enforced in `validate_stages` on purpose: refusing the manifest would fail
    # a node over a permission it did not need, and the stage still runs exactly as declared.)
    declared_training: dict = {}
    for stage in raw:
        if not isinstance(stage, dict) or stage.get("name") is None:
            continue
        if str(stage.get("role") or "").strip().lower() != STAGE_ROLE_TRAINING:
            continue
        expect = stage.get("expect") or {}
        files = expect.get("files") if isinstance(expect, dict) else None
        promised = tuple(str(f) for f in (files or []) if isinstance(f, str))
        if promised:
            declared_training[str(stage.get("name"))] = promised
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
                # POSITION FIRST, always. The scorer is the operator's protected final stage and a
                # `score.log` verdict once killed a training that had just SUCCEEDED; a manifest
                # must not be able to buy that back by writing `role` on it.
                role = LOG_ROLE_SCORE
            elif len(names) == 1 and complete:
                role = LOG_ROLE_TRAINING     # a one-stage pipeline IS the single-command shape
            elif name in declared_training and complete:
                # DECLARED, not guessed. `complete` for the same reason the one-stage grant needs
                # it: a pipeline this cannot fully name is a broken resolution, and a broken
                # resolution must not hand out the one role that ends nodes.
                role = LOG_ROLE_TRAINING
            else:
                role = LOG_ROLE_WORK
            _claim(_log_name_key(f"{name}.log"), (name, role))
    else:
        _claim(_log_name_key(_SINGLE_COMMAND_LOG), (None, LOG_ROLE_TRAINING))
    # The artifacts belong to the declaration only if the declaration actually BOUGHT the role: the
    # positional scorer rule and the `complete` guard both refuse it, and a stage that was refused
    # must not carry a spend condition for an authority it does not hold.
    artifacts: tuple = ()
    for name, promised in declared_training.items():
        if roles.get(_log_name_key(f"{name}.log"), (None, None))[1] == LOG_ROLE_TRAINING:
            artifacts = promised
    return EvalLogPlan(roles=roles, stage_names=names, training_artifacts=artifacts)


def training_authority_spent(workdir, plan: Optional[EvalLogPlan]) -> bool:
    """Whether a DECLARED training stage has already written what it promised — i.e. whether the
    thing a kill would now destroy is a finished training rather than a running one.

    This is the price of admitting a declaration, paid in the same currency the rest of the file
    uses. `e5small-dr-unified-v2`'s `train` stage does not only train: its own log ends with the
    retrieval evaluation it runs in-process (`RECALL@100: 0.793344` is a line in `train.log`), which
    is the H-1 shape — a judge holding kill authority reading scorer output — moved INSIDE one
    stage, where no plan can split it by filename. A stage that declares `role: "training"` cannot
    be taken at its word about a phase it does not distinguish, so the authority is spent the moment
    its declared artifact exists: after that the verdict is advisory again, exactly as if the stage
    had never declared anything. Not a heuristic about the text — `expect.files` is the manifest's
    own output contract and the file is an exact filesystem fact.

    Fail-closed on I/O trouble: unreadable means the authority is treated as spent (advisory), never
    as live. `()` artifacts answer False here, and the reason that is safe is upstream rather than
    obvious: `eval_log_plan` only ever leaves this empty for a plan whose training role was NOT
    bought by a declaration — the single-command eval and the one-stage pipeline, which never
    promised anything whose arrival could end them. A declaration that named no `expect.files` does
    not reach this function at all, because it is refused `LOG_ROLE_TRAINING` in the first place; an
    authority with no spend condition is one that outlives the training it was granted over, which
    is the exact defect this function exists to prevent.
    """
    if plan is None or not plan.training_artifacts:
        return False
    for rel in plan.training_artifacts:
        try:
            if (Path(workdir) / rel).exists():
                return True
        except (OSError, ValueError):
            # ValueError is not hypothetical: an embedded NUL in a path raises it before any
            # syscall, so a `Path` this cannot even form must land on the same side as one it
            # cannot stat.
            return True
    return False


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


# How many windows a FINISHED stage log is reduced to. The monitor's tracker gets one window per
# tick because it reads a live file it can only ever see the tail of; a stage check runs after the
# stage has EXITED, so the whole of this attempt's bytes are on disk and the windowing is a choice
# rather than a constraint. 32 mirrors the tick granularity a multi-hour eval actually produces at
# `train_monitor_interval_s`, and the direction test only reads the first and last NUMERIC window's
# medians plus the median of the per-window noise floors, so it is not sensitive to the exact count —
# what it must not be is 1, which `summarize_trajectory` already refuses ("ONE window is a tail by
# another name").
STAGE_TRAJECTORY_WINDOWS = 32
# ...and the bound on what one window costs in memory, since the window size is derived from the
# file. A 53.6 MB stage log — the largest in `runs/` — reduces to 32 x 1.67 MB chunks; the floor
# stops a small log from being cut into 32 slivers that each hold one progress-bar render.
STAGE_TRAJECTORY_MIN_CHUNK = 65_536
STAGE_TRAJECTORY_MAX_CHUNK = 4 * 1024 * 1024


def read_stage_trajectory(path, *, floor: int = 0,
                          windows: int = STAGE_TRAJECTORY_WINDOWS) -> LossTrajectory:
    """Measure the loss trajectory over THIS attempt's bytes of a finished stage log.

    STREAMED, never slurped: the file is read from `floor` to EOF in record-aligned chunks and each
    chunk is reduced to one `LossWindow` on the way past, so peak memory is one chunk and every byte
    above the floor is covered. A head+tail read was the obvious cheaper alternative and is refused —
    `_anomaly_of`'s non-finite rung asks a question about EVERY window, and a `loss=nan` in the middle
    of a run that recovers is exactly the evidence a bounded read would drop. Measured on the largest
    stage log in `runs/` (53.6 MB, `e5small-dr-unified-v2` node 2): 2.70 s, ~19.8 MB/s, once per
    checked stage, on the eval worker thread that is about to block on an LLM call anyway.

    `floor` is `attempt_byte_floor`'s answer and is NOT optional in practice: stage logs are opened
    `"a"` (`sandbox._tee_drain`), so a repaired or re-run stage appends to its predecessor's bytes and
    a floorless read splices two curves into one — inventing both a jump and a direction. Driven in
    `tests/test_stage_trajectory.py`.

    Returns an empty `LossTrajectory` (`windows=0`, `direction="unknown"`) for every failure — no
    file, no permission, nothing above the floor, no loss value in the bytes. That is the value
    `trajectory_acquits_stage_check` refuses on, so an unreadable log leaves the checker's verdict
    exactly as it was."""
    try:
        want = max(2, int(windows))
    except (TypeError, ValueError):
        want = STAGE_TRAJECTORY_WINDOWS
    rows: list = []
    try:
        with open(path, "rb") as fh:
            size = max(0, int(os.fstat(fh.fileno()).st_size))
            start = max(0, int(floor or 0))
            region = size - start
            if region <= 0:
                return LossTrajectory()
            # ...and never so large that the region is ONE window. `summarize_trajectory` refuses a
            # direction on a single window ("ONE window is a tail by another name"), so a chunk floor
            # that swallowed a short log would answer `unknown` about a curve plainly visible in it —
            # the same silent narrowing as the tail, arriving by a different route. Driven: a 44 KB
            # eval log is 1 chunk at the bare floor and 2 with this clamp.
            chunk = max(1, min(max(region // want, STAGE_TRAJECTORY_MIN_CHUNK),
                               STAGE_TRAJECTORY_MAX_CHUNK, region // 2))
            fh.seek(start)
            carry = b""
            remaining = region
            while remaining > 0:
                raw = fh.read(min(chunk, remaining))
                if not raw:
                    break
                remaining -= len(raw)
                buf = carry + raw
                # Align on a record boundary — `\n` OR `\r`, because a tqdm bar writes its whole life
                # into one newline-delimited line (the same rule `tools/log_tools._RECORD_SPLIT`
                # states). Splitting mid-render would cut a `loss=13.3` in half and lose the point.
                cut = max(buf.rfind(b"\n"), buf.rfind(b"\r"))
                if cut < 0:
                    # No boundary anywhere in this chunk. Split it anyway once the buffer has reached
                    # a full chunk. A log that never writes `\n` or `\r` is not hypothetical (a
                    # script printing with `end=""`), and letting the carry grow is the slurp this
                    # function streams to avoid — worse, a carry bounded at the whole region yields
                    # ONE window, which `summarize_trajectory` refuses a direction on, so the reader
                    # would answer `unknown` about a curve it had just read every point of. The
                    # forced split can cut ONE render in half, costing one loss value per split out
                    # of thousands.
                    carry = buf
                    if len(carry) < chunk:
                        continue
                    cut = len(carry) - 1
                buf, carry = buf[:cut + 1], buf[cut + 1:]
                window = summarize_loss_window(buf.decode("utf-8", "replace"))
                if window is not None:
                    rows.append(window)
            if carry:
                window = summarize_loss_window(carry.decode("utf-8", "replace"))
                if window is not None:
                    rows.append(window)
    except (OSError, TypeError, ValueError, OverflowError):
        return LossTrajectory()
    return summarize_trajectory(rows)


def stage_check_trajectory(workdir, stage: str, *, plan: Optional[EvalLogPlan] = None,
                           snapshot: Optional[TrainingLogSnapshot] = None) -> LossTrajectory:
    """The trajectory of the stage the inter-stage checker is about to judge, or an empty one.

    The path is NEVER constructed from anything a model said: `stage` is the resolved pipeline's own
    stage name, the basename is the one `command_eval._run_stages` writes (`ex.log(f"{name}.log")`),
    and `plan` — the same `eval_log_plan` the watchdogs use — must agree that this basename belongs
    to THIS stage. A `LOG_ROLE_AMBIGUOUS` name (two stages folding onto one file, or a stage called
    `setup` shadowing the dep install's `setup.log`) is refused for the reason `monitor_log_sources`
    refuses it: bytes nobody can attribute to a phase are not evidence.

    `snapshot` is the pre-attempt `snapshot_training_logs`, taken before any stage of this eval ran,
    which is what makes `attempt_byte_floor` able to answer at all. With no snapshot the floor is 0
    and a repaired stage's earlier curve is in scope — so the caller that has one must pass it."""
    if not str(stage or "").strip():
        return LossTrajectory()
    name = f"{stage}.log"
    if plan is not None:
        claimed = plan.roles.get(_log_name_key(name))
        if claimed is None or claimed[0] != stage or claimed[1] == LOG_ROLE_AMBIGUOUS:
            return LossTrajectory()
    try:
        path = Path(workdir) / name
        with open(path, "rb") as fh:
            floor = attempt_byte_floor(fh, path, snapshot)
    except (OSError, TypeError, ValueError, OverflowError):
        return LossTrajectory()
    if floor is None:
        return LossTrajectory()     # fail closed — the boundary could not be established
    return read_stage_trajectory(path, floor=floor)


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

    The "is there anything to name" probe is the FIRST derivation and the provider is handed it, so
    the construction costs one derivation and not two. It matters because the derivation is not
    cheap: `monitor_log_sources` globs the workdir and opens + fstats every stage log, with
    `attempt_byte_floor` probe-READING each one, on mounts where a stat can cost most of a second.
    `LogQueryTools` holds it for the rest of THIS call and drops it again (`_one_derivation`), so a
    later tool call still re-derives — a new stage log appearing mid-eval is exactly what the
    callable is for.
    """
    from looplab.tools.log_tools import LogQueryTools
    first = monitor_log_sources(workdir, log_plan, log_snapshot)
    if not first:
        return None
    pending = [first]

    def resolve():
        # The probe's own answer serves the first read; every later one re-derives, because the
        # active stage log MOVES during an eval and a frozen map answers about the wrong phase.
        return pending.pop() if pending else monitor_log_sources(workdir, log_plan, log_snapshot)

    return LogQueryTools(resolve)


def monitor_tools(engine, workdir, log_plan=None, log_snapshot=None):
    """Everything this tick's judge may look with: the eval's own logs AND the code that wrote
    them. None when neither is available, which is what `structured_judge` reads as "no tools" and
    is the historical one-shot call byte for byte.

    Composed here rather than at the call site so the two watchdogs cannot come to disagree about
    what looking means — the same reason `_log_query_tools` exists as one function. `CompositeTools`
    de-dups by tool NAME with the first provider winning, and the logs go first deliberately: they
    are the evidence the verdict is primarily about, and a name collision must never silently
    shadow `read_log` with a general file reader that knows nothing about attempt floors.
    """
    providers = [p for p in (monitor_log_tools(engine, workdir, log_plan, log_snapshot),
                             monitor_code_tools(engine, workdir)) if p is not None]
    if not providers:
        return None
    if len(providers) == 1:
        return providers[0]
    from looplab.agents.tool_loop import CompositeTools
    return CompositeTools(providers)


def monitor_code_tools(engine, workdir):
    """The read-only CODE scouts the live judge may look with, rooted at the NODE WORKDIR — or None
    when the tools are off or there is no workdir to root them at.

    WHY THE JUDGE NEEDS THEM. It is asked a question the log alone often cannot answer. `fault`
    splits a `broken` verdict into "the code is wrong" and "the idea is wrong", and those get
    opposite treatments — one is repaired and retried, the other is recorded as a real negative
    result. A frozen loss looks identical either way from the outside; what tells them apart is
    whether something in the running code cannot work as written. Measured on
    `e5small-dr-unified-v2`: nodes 2 and 4 spent ~10 GPU-hours reaching 0.0 and 2e-05 under 48
    `broken` verdicts between them, every one correct about the symptom and none able to say
    whether a reduction, a normalization or the hypothesis was at fault — because the judge could
    read every byte of the log and not one line of the program that wrote it.

    ROOTED AT THE WORKDIR, and that is the whole safety argument as well as the accuracy one:

    - it is the code that is ACTUALLY RUNNING. The Developer's own scouts are rooted at the
      editable SOURCE, which is a different filesystem from the one the eval sees — a distinction
      that already cost a run (`runs/rubert-dr-0807` node 2 died on a missing
      `<workdir>/looplab_eval.py` while the repair session's `read_file` cheerfully returned it).
      A judge reading the source tree would be answering about a program that is not the one on
      trial.
    - it is the one region that provably holds only what THIS node produced, which is what
      `monitor_log_sources` already relies on and what `read_allowlist`/`read_fence` already grant.
      No other node's workspace, no operator secret outside it, and no engine source.
    - the direction of harm is favourable. Everything the judge reads here is the candidate's own
      text, exactly like the log it has always read — and the widest thing that text can now buy is
      the CHEAP action: `fault="implementation"` routes to a repair-stop, whose cost when wrong is
      one restart. It cannot mint a metric, a champion, a violation or a selection; the terminal
      kill keeps its own narrow gate, and `hypothesis` — the answer that ends a node — is the one
      the code cannot argue itself into, because refusing to blame the implementation is what
      leaves it standing.

    `RepoScoutTools` is reused rather than re-derived for the reason `_log_query_tools` gives about
    its own single construction: two answers to "what is readable" is how two roles come to
    disagree. It is already the right shape for this mount — path-safe, secret-filtered, bounded
    per page and per walk, and it already skips the gigabyte directories a trainer workdir carries
    (`ckpt`, `checkpoints`, `wandb`, `lightning_logs`), which on geesefs is the difference between
    a grep and a stall.
    """
    if not getattr(engine, "_train_monitor_tools", False):
        return None
    try:
        root = Path(workdir)
        if not root.is_dir():
            return None
    except OSError:
        return None
    from looplab.tools.reposcout import RepoScoutTools
    return RepoScoutTools(roots=[str(root)], default_root=str(root))


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
    sets it, `config.yaml` still reads 15), so attempt 6 re-ran the SAME 10,590 steps. This docstring
    projected that retry at 22,096 s into the same 22,000 s ceiling, extrapolated live at
    1.798 s/step. **THE RUN FALSIFIED THE PROJECTION AND IT IS RETRACTED HERE**: `train` passed in
    19,915.75 s, `score` ran 3,130.3 s, and the node recorded 0.762048 to become v8's champion. What
    bought the margin was a SECOND edit in the same repair — deleting the in-`train` `test_model()`
    call, so the full-index retrieval moved out of the train budget and into the protected `score`
    stage (verified in the durable change set: attempt 4's `train.py` CALLS `test_model(...)`,
    attempt 5's carries the import and a note that retrieval "is run independently").
    **What this rung exists for is UNCHANGED and is not the projection**: the verdict was drawn from
    a 522-character tail holding only the second progress bar, and it was wrong about where the time
    was going. A misdiagnosis that happens to be rescued by an unrelated edit in the same repair is
    not a diagnosis that worked.

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
                        return resolved, "", False
                    # Same thread as the rest of this tick's I/O, so the stat never touches the
                    # event loop. Read EVERY tick, not once: the artifact appears mid-eval, and it
                    # is the appearance that spends the authority.
                    spent = training_authority_spent(workdir, log_plan)
                    return (resolved,
                            read_training_tail(workdir, snapshot=log_snapshot, plan=log_plan),
                            spent)

                resolved, tail, authority_spent = await anyio.to_thread.run_sync(
                    _observe_log, limiter=_watch_limiter())
                log_role = resolved.role if resolved is not None else LOG_ROLE_UNKNOWN
                if authority_spent and log_role == LOG_ROLE_TRAINING:
                    # The declaration bought authority over the TRAINING; the training is over.
                    # Downgrading to the advisory role rather than going silent keeps the verdict,
                    # the alert row and the narration — only the gun is handed back.
                    log_role = LOG_ROLE_WORK
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
                    # WHETHER A KILL IS REACHABLE AT ALL for this eval, on every tick's span. The
                    # role gate is a property of the resolved PIPELINE, not of the run's health, so
                    # a pipeline that declared no training stage can be read as unstoppable from
                    # its first tick instead of after the hours it takes for a verdict to matter.
                    if log_plan is not None and LOG_ROLE_TRAINING not in {
                            role for _stage, role in log_plan.roles.values()}:
                        sp.set("kill_reachable", False)
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
                        stage_text = monitor_stage_context(resolved, log_plan)
                        trajectory_text = trajectory_context(trajectory)

                        def _judge():
                            """The paid call AND the source derivation it needs, both in the worker.

                            `monitor_log_tools` is FILESYSTEM work — it globs the workdir for
                            `*.log` and opens + fstats every stage log the plan names, and
                            `attempt_byte_floor` probe-READS each one. As an ARGUMENT to
                            `run_sync` it was evaluated on the EVENT-LOOP thread, which is the one
                            place in this tick that pays for a slow mount: on the geesefs/S3 mounts
                            `runs/` lives on, an `lstat` of a file that is NOT there costs
                            105-950 ms (`core/fence.py::_warm_directory_lookup` measured it), so
                            one blocking derivation per tick per running eval stalls the whole
                            engine loop. Every other filesystem touch in this loop is already
                            handed to a worker (`_observe_log` above); this one just looked like a
                            plain argument. Still built PER TICK, for the reason it always was: the
                            tool reads the live log while the judge is thinking, so it must see the
                            file as it is now and not as it was when the eval started.
                            """
                            return self._training_verdict(
                                tail, context, stage_text, trajectory_text,
                                monitor_tools(self, workdir, log_plan, log_snapshot))

                        verdict = await anyio.to_thread.run_sync(
                            _judge, abandon_on_cancel=False)
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
                        # REPAIR FIRST, and on every judged stage. A named bug is a thing to fix,
                        # not a verdict about an idea — so this is asked before the terminal kill
                        # and wins it: a collapsed training whose cause the judge can point at goes
                        # back to its Developer, and only an implementation the judge will NOT
                        # blame reaches the gun. See `should_monitor_repair` for why the role gate
                        # that guards the kill does not guard this.
                        repair_decided = kill_signal is not None and should_monitor_repair(
                            verdict, enabled=getattr(self, "_train_monitor_kill", False),
                            threshold=threshold, log_role=log_role, broken_streak=broken_streak,
                            trajectory=trajectory)
                        stop_decided = (not repair_decided) and kill_signal is not None \
                            and should_monitor_kill(
                                verdict, enabled=getattr(self, "_train_monitor_kill", False),
                                threshold=threshold, log_role=log_role,
                                broken_streak=broken_streak, trajectory=trajectory)
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
                        # The OTHER counterfactual, and the one that cost the most: every conjunct
                        # cleared except the role. `e5small-dr-unified-v2` node 2 sat in exactly
                        # this state 31 times over 7.3 hours and NOTHING said so — the alert rows
                        # read as ordinary `broken` verdicts, indistinguishable from ones the kill
                        # path had simply not confirmed yet, so the unreachability of the early
                        # stop for multi-stage pipelines stayed invisible until a node scored 0.0.
                        # Pure, cheap, and evaluated only when the role is what refused; asking the
                        # SAME predicate with the role swapped is what makes this a fact about the
                        # gate rather than a second opinion about the run.
                        role_withheld = (not stop_decided and kill_signal is not None
                                         and log_role not in _KILL_ELIGIBLE_ROLES
                                         and should_monitor_kill(
                                             verdict,
                                             enabled=getattr(self, "_train_monitor_kill", False),
                                             threshold=threshold, log_role=LOG_ROLE_TRAINING,
                                             broken_streak=broken_streak, trajectory=trajectory))
                        if role_withheld:
                            sp.set("kill_role_withheld", log_role)
                        # CLAIM BEFORE RECORDING. The sibling ASHA watchdog can decide on the same tick,
                        # and only one of them owns the node's terminal — so the alert must state what
                        # actually happened to the node, not what this monitor wanted. The guard->update
                        # inside `claim_watchdog_kill` is await-free, so the answer is exact.
                        claimed = (stop_decided or repair_decided) and claim_watchdog_kill(
                            kill_signal, cancel, reason=reason,
                            terminal_reason=(MONITOR_REPAIR_REASON if repair_decided
                                             else "monitor_broken"),
                            confidence=round(conf, 3))
                        if stop_decided or repair_decided:
                            sp.set_many(stop_decided=True, kill=bool(claimed),
                                        fault=str(getattr(verdict, "fault", "unknown")))
                        if repair_decided:
                            sp.set("repair_decided", True)
                        elif (verdict.status == "broken" and broken_streak == 1
                              and kill_signal is not None
                              and _confirmation_would_act(
                                  verdict, enabled=getattr(self, "_train_monitor_kill", False),
                                  threshold=threshold, log_role=log_role, trajectory=trajectory)):
                            # ARMED, not acting: re-look promptly instead of after another full cadence
                            # (up to 30 min on a long budget), so confirmation costs seconds of a
                            # multi-hour budget rather than a meaningful slice of it. `armed_at` starts
                            # the TTL at the TRANSITION, never on a later broken tick — otherwise a
                            # flapping log could renew the arm indefinitely. It is also what licenses
                            # the changed-digest bypass, so a tick that could not act on a confirmation
                            # never buys a re-look — a billable re-ask and a changed-digest bypass
                            # bought for nothing.
                            #
                            # WHICH tick that is, is asked as the COUNTERFACTUAL
                            # (`_confirmation_would_act`) rather than re-listed here, and that
                            # changed on 2026-08-20. The hand-written conjuncts were
                            # `log_role in _KILL_ELIGIBLE_ROLES` plus the
                            # trajectory veto, written when a KILL was the only action a confirmation
                            # could reach; `should_monitor_repair` then opened the repair-stop to
                            # EVERY judged role, so on exactly the roles it was opened for the second
                            # look cost a full cadence — and, on a log that diverged and then went
                            # silent, never arrived at all, because `unchanged and armed_at is None`
                            # `continue`s and only an arm bypasses it. The list was also missing the
                            # CONFIDENCE bar it claimed to be a proxy for: a `broken` at 0.3 on a
                            # training stage armed, re-asked at 30 s and bypassed the digest gate for
                            # a kill `should_monitor_kill` refuses on confidence. Asking the two real
                            # predicates with the streak already satisfied answers both directions at
                            # once and cannot drift from them.
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
                            if role_withheld:
                                # Additive and fold-ignored; readers default its absence to "the
                                # role was not what refused". It names the role that HELD, so the
                                # operator reads "this stage was never kill-eligible" instead of
                                # re-deriving it from a manifest that may since have changed.
                                alert["kill_role_withheld"] = str(log_role)[:32]
                            if not confidence_valid:
                                alert["confidence_valid"] = False
                            if verdict.status == "broken":
                                # The judge's own attribution, on the durable row whether or not it
                                # led anywhere: "the code is wrong" and "the idea is wrong" are the
                                # two answers the search must be able to tell apart afterwards, and
                                # a run that recorded only the second learned the wrong lesson from
                                # every bug. Additive and fold-ignored; absent reads as "unknown".
                                alert["fault"] = str(getattr(verdict, "fault", "unknown"))[:16]
                            if repair_decided:
                                alert["repair_decided"] = True
                            if stop_decided or repair_decided:
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
                            with anyio.CancelScope(shield=stop_decided or repair_decided):
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
                        if stop_decided or repair_decided:
                            return           # won or lost, this attempt is ending — stop watching it
            except anyio.get_cancelled_exc_class():
                raise                        # cooperative cancellation — must propagate, never be swallowed
            except Exception:  # noqa: BLE001 — a transient per-tick hiccup (disk/LLM/tracer) SKIPS this tick;
                continue                     # it must never disable the watcher for the rest of a long eval
