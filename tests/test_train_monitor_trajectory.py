"""The training monitor's run-scale LOSS TRAJECTORY: who owns "is it still descending".

The defect this file pins, measured on `runs/rubertlite-dr-unified-v7` while it was live: the judge
receives `training_log_digest`'s output, and a tqdm bar line is ~330 characters, so `max_chars=4000`
leaves about TEN records — roughly thirty seconds of a five-hour run. Replayed against the real
logs it was shown

    node 0: 11.0197 11.0355 11.0296 11.0316 11.0278 11.0399 11.0236 11.0552 11.0445 11.0410
    node 1: 22.8906 22.9009 22.8881 22.8904 22.9011 22.9118 22.9024 22.8631

and answered "flat", "plateau", and for node 1 `broken` at confidence 0.82 — "pinned at ~23.0 ...
showing no learning trend from its initialization value". Over their whole logs those nodes ran
15.73 -> 11.03 and 24.28 -> 22.90. A decelerating curve is BELOW the step-to-step noise floor inside
any short window, so "converged/stuck" and "still descending slowly" are observationally identical
there and no reader of the tail can separate them. The question was unanswerable from the evidence.

So the split: the model keeps the questions a tail can answer, and these deterministic rules own the
trajectory. They may only ever REFUSE a kill — the numbers come from text the candidate's own
training script wrote, which is `engine/metric_salvage.py`'s rule one file over, so a wider action
space must not become a wider trusted set (docs/36).

The corpus figures quoted below come from replaying the rule over every historical
`train_monitor_alert` row in `runs/` (150 rows across four runs on 2026-08-14; v7 was still
running, so its count grows). 71 rows have a loss-bearing log; all 71 measure as descending, and
exactly TWO kill decisions change — the two `broken` verdicts at or above the 0.8 confidence bar
about a curve that had already fallen. The other 79 rows are `runs/rubert-dr-0807`, whose log
contains no parseable loss at all, and nothing about them changes.
"""
from __future__ import annotations

import json
import threading
import time

import anyio
import pytest

from looplab.core.tracing import JsonlSpanExporter, Tracer
from looplab.engine import train_monitor as _tm
from looplab.engine.train_monitor import (
    LOG_ROLE_TRAINING,
    LOG_ROLE_WORK,
    LossTrajectory,
    LossTrajectoryTracker,
    TrainingVerdict,
    parse_loss_points,
    should_monitor_kill,
    summarize_loss_window,
    summarize_trajectory,
    trajectory_context,
    trajectory_row,
    trajectory_vetoes_kill,
)
from looplab.events.types import EV_TRAIN_MONITOR_ALERT

from tests.test_train_monitor import (
    _TRAIN_PLAN,
    _FakeClient,
    _FakeDeveloper,
    _run_verdict_monitor,
)


def _window(text: str):
    return summarize_loss_window(text)


def _windows(*series):
    """One `LossWindow` per tick, each from a rendered mini-digest of that tick's loss values."""
    return [summarize_loss_window("\n".join(f"{{'loss': {v}, 'epoch': 1.0}}" for v in values))
            for values in series]


# ----------------------------------------------------------------- what counts as a loss point
def test_only_the_loss_key_is_a_loss_point():
    # `eval_loss`/`train_loss`/`val_loss` are DIFFERENT series measured on different data. Splicing
    # them into one trajectory manufactures exactly the jumps this rule exists to tell from real
    # movement, so the negative lookbehind that excludes them is load-bearing, not tidiness.
    text = ("{'loss': 1.5, 'grad_norm': 0.9, 'learning_rate': 1e-4, 'epoch': 2.0}\n"
            "{'eval_loss': 90.0, 'eval_runtime': 3.0}\n"
            "train_loss=77.0 val_loss: 88.0\n"
            "step 3 loss: 1.25\n"
            "loss=1.0\n")
    values, nonfinite = parse_loss_points(text)
    assert values == [1.5, 1.25, 1.0]
    assert nonfinite == 0


@pytest.mark.parametrize("text", [
    "{'loss': nan, 'grad_norm': 1.0}",
    "{'loss': inf, 'grad_norm': 1.0}",
    "loss: -Infinity",
    # the earliest honest sign of a blown-up run, and it appears while the printed loss still looks
    # finite — `runs/rubertlite-dr-unified-v6` node 5 printed `{'loss': 0.0, 'grad_norm': nan}` four
    # steps before `{'loss': 1.2217858750118953e+25}`.
    "{'loss': 0.0, 'grad_norm': nan, 'epoch': 0.03}",
])
def test_non_finite_values_are_counted_not_kept(text):
    values, nonfinite = parse_loss_points(text)
    assert nonfinite >= 1
    assert all(v == v for v in values), "a nan must never enter the numeric series it would poison"


def test_a_window_reduces_to_median_noise_floor_and_endpoints():
    window = _window("\n".join(f"loss: {v}" for v in (1.00, 1.02, 0.99, 1.01, 1.00)))
    assert window is not None
    assert window.count == 5 and window.first == 1.00 and window.last == 1.00
    assert window.masd > 0, "the step-to-step scatter IS the noise floor and must be measured"
    assert _window("no numbers here at all") is None


def test_progress_counter_is_read_only_when_it_is_plausible():
    # "flat at step 15300 of 35300" and "flat at 34900 of 35300" are different facts about the same
    # numbers, and the tail states the position nowhere the model can rely on.
    window = _window("| 15300/35300 [3:17:30<4:12:36,  1.32it/s]{'loss': 11.03, 'epoch': 21.6}")
    assert window is not None and (window.progress_done, window.progress_total) == (15300, 35300)
    # done > total, and a zero total, are not positions in a run.
    assert _window("v1.5/0 loss: 1.0").progress_total is None
    assert _window("99/7 loss: 1.0").progress_total is None


# ------------------------------------------------------------------------ the direction truth table
def test_a_single_window_refuses_to_state_a_direction():
    # ONE window is a tail by another name — the exact evidence the judge already could not decide
    # on. Report the numbers, refuse the direction, and therefore never veto.
    trajectory = summarize_trajectory(_windows([5.0, 5.1, 4.9]))
    assert trajectory.windows == 1 and trajectory.direction == "unknown"
    assert trajectory.net is None
    assert trajectory_vetoes_kill(trajectory) is False


def test_no_numeric_loss_at_all_is_unknown_and_never_vetoes():
    # `runs/rubert-dr-0807` is the live case: 45 MB of Lightning rich-progress output with ZERO
    # occurrences of the string "loss". All 79 of that run's alert rows measure nothing, so all 17
    # of its `broken` verdicts keep exactly the authority they had. Fail-open is the point — this
    # rung takes nothing away from a log it cannot read.
    trajectory = summarize_trajectory([])
    assert trajectory.direction == "unknown" and trajectory.windows == 0
    assert trajectory_vetoes_kill(trajectory) is False
    assert trajectory_row(trajectory) is None
    assert trajectory_context(trajectory) == ""


def test_the_v7_node_1_shape_reads_as_descending():
    # The real numbers, at the two ends of what the monitor accumulated by the time it answered
    # `broken` 0.82: window medians 27.69 -> 22.92 against a step-to-step noise floor of 0.017.
    # Every window here is INDIVIDUALLY flat to three decimals; that is the whole difficulty.
    series = [[27.69 - 0.4 * k + d for d in (0.0, 0.02, -0.02, 0.01, -0.01)] for k in range(12)]
    trajectory = summarize_trajectory(_windows(*series))
    assert trajectory.direction == "descending"
    assert trajectory.windows == 12 and trajectory.net > 0
    assert trajectory_vetoes_kill(trajectory) is True


def test_a_genuinely_pinned_loss_reads_as_flat_and_never_vetoes():
    # The other half of the same rule, and the reason this is not a blanket "never kill": a run
    # whose windows do not move beyond their own noise floor gets NO protection, so the model's
    # `broken` keeps every bit of the authority it had.
    trajectory = summarize_trajectory(_windows(*[[5.001, 5.003, 4.999, 5.002]] * 8))
    assert trajectory.direction == "flat"
    assert trajectory_vetoes_kill(trajectory) is False


def test_a_drop_under_the_measured_noise_floor_is_not_a_direction():
    # 0.0005 of net movement against a ~0.02 noise floor is not evidence of learning; calling it
    # "descending" would hand a veto to any run whose loss wobbles downward by chance.
    trajectory = summarize_trajectory(_windows(*[
        [5.0000 - 0.0001 * k, 5.02 - 0.0001 * k, 4.98 - 0.0001 * k] for k in range(5)]))
    assert trajectory.direction == "flat"


def test_a_zero_scale_curve_cannot_manufacture_a_direction_out_of_an_epsilon():
    """The floor used to be `max(noise, |opening| * _TRAJECTORY_MIN_RELATIVE_DROP)`, and BOTH terms
    collapse to 0.0 on a window of `loss: 0.0`: the masd of identical values is 0, and 0.001 x 0 is
    0. So `net > floor` became `net > 0` and an epsilon of drift read as `descending` — which then
    VETOES every `broken` verdict for the rest of the node, i.e. a degenerate window bought a
    multi-hour run permanent immunity from the kill this rule exists to allow.

    `{'loss': 0.0, 'grad_norm': nan}` is this module's own positive control
    (`runs/rubertlite-dr-unified-v6` node 5) — but only the `grad_norm: nan` beside it rescued that
    case, through `_anomaly_of`. A run that prints the zero WITHOUT the nan had nothing, which is
    what `_TRAJECTORY_MIN_ABSOLUTE_DROP` is for.
    """
    trajectory = summarize_trajectory(_windows([0.0, 0.0], [0.0, 0.0], [-1e-9, -1e-9],
                                               [-1e-9, -1e-9]))
    assert trajectory.noise == 0.0, "the degenerate window this is about: no measurable scatter"
    assert trajectory.net and 0 < trajectory.net < 1e-6
    assert trajectory.direction == "flat"
    assert trajectory_vetoes_kill(trajectory) is False
    # ...and the same in the other direction: an epsilon UP is not divergence either.
    rising = summarize_trajectory(_windows([0.0, 0.0], [0.0, 0.0], [1e-9, 1e-9], [1e-9, 1e-9]))
    assert rising.direction == "flat"


def test_the_absolute_floor_is_inert_wherever_the_relative_one_has_anything_to_say():
    """It may only ever WITHDRAW a veto, never mint one, so the thing to prove is that it changes
    nothing at any scale a real run works at: the relative floor is already >= 1e-6 for any opening
    above 1e-3, so every curve the corpus contains is decided exactly as before."""
    assert _tm._TRAJECTORY_MIN_ABSOLUTE_DROP <= 1e-3 * _tm._TRAJECTORY_MIN_RELATIVE_DROP
    # A tiny but honest descent well inside the relative floor's reach still reads as descending.
    trajectory = summarize_trajectory(_windows(*[[1.0 - 0.01 * k, 1.0 - 0.01 * k] for k in range(5)]))
    assert trajectory.direction == "descending"
    assert trajectory_vetoes_kill(trajectory) is True


def test_divergence_reads_as_rising():
    trajectory = summarize_trajectory(_windows(*[[1.0 + k, 1.02 + k, 0.98 + k] for k in range(6)]))
    assert trajectory.direction == "rising"
    assert trajectory_vetoes_kill(trajectory) is False


# --------------------------------------------------------------- the anomaly override (POSITIVE CONTROL)
def test_non_finite_values_withdraw_the_veto_even_from_a_descending_curve():
    # `runs/rubertlite-dr-unified-v6` node 5, the positive control: `{'loss': 0.0, 'grad_norm': nan}`
    # then `{'loss': 1.2217858750118953e+25}`. Replayed at the run's own cadence the rule reads
    # `rising` with anomaly "non-finite loss or grad_norm" and does NOT veto. The override matters
    # independently of the direction, because a run can descend for an hour and THEN blow up.
    windows = _windows(*[[9.0 - k, 8.9 - k, 9.1 - k] for k in range(5)])
    windows.append(summarize_loss_window("{'loss': 0.0, 'grad_norm': nan, 'epoch': 0.03}"))
    trajectory = summarize_trajectory(windows)
    assert trajectory.direction == "descending", "the curve itself really was going down"
    assert trajectory.anomalous and "non-finite" in trajectory.anomaly
    assert trajectory_vetoes_kill(trajectory) is False


def test_an_explosion_withdraws_the_veto():
    windows = _windows(*[[63.8, 63.9, 63.7] for _ in range(3)])
    windows.append(summarize_loss_window("{'loss': 1.2217858750118953e+25, 'epoch': 0.01}"))
    trajectory = summarize_trajectory(windows)
    assert "exploded" in trajectory.anomaly
    assert trajectory_vetoes_kill(trajectory) is False


# ------------------------------------------------------------------------------- the kill conjunct
def _broken(confidence: float = 0.9) -> TrainingVerdict:
    return TrainingVerdict(status="broken", reason="not learning", confidence=confidence)


_KILLABLE = dict(enabled=True, threshold=0.8, log_role=LOG_ROLE_TRAINING, broken_streak=2)


def test_the_measured_trajectory_is_a_fifth_conjunct_of_the_kill():
    assert should_monitor_kill(_broken(), **_KILLABLE) is True          # every other conjunct held
    descending = summarize_trajectory(_windows(*[[9.0 - k, 9.1 - k, 8.9 - k] for k in range(6)]))
    assert should_monitor_kill(_broken(), **_KILLABLE, trajectory=descending) is False


@pytest.mark.parametrize("trajectory", [
    None,
    LossTrajectory(),
    summarize_trajectory(_windows(*[[5.0, 5.01, 4.99]] * 6)),                        # flat
    summarize_trajectory(_windows(*[[1.0 + k, 1.1 + k] for k in range(6)])),         # rising
])
def test_everything_that_is_not_a_measured_descent_leaves_the_kill_exactly_as_it_was(trajectory):
    # A measurement that says nothing must cost nothing: absent, unmeasured, flat and rising all
    # reproduce the pre-2026-08-14 decision byte for byte.
    assert should_monitor_kill(_broken(), **_KILLABLE, trajectory=trajectory) is True


@pytest.mark.parametrize("kwargs", [
    {"enabled": False},
    {"log_role": LOG_ROLE_WORK},
    {"broken_streak": 1},
    {"threshold": 0.99},
])
def test_the_veto_can_only_ever_refuse_never_authorize(kwargs):
    # The direction of this rung is the whole reason it is allowed to read the candidate's own log
    # text at all (docs/36 corollary 2: a wider action space must not widen the trusted set). No
    # trajectory of any shape may turn a refusal into a kill.
    for trajectory in (None, LossTrajectory(),
                       summarize_trajectory(_windows(*[[9.0 - k, 8.9 - k] for k in range(6)])),
                       summarize_trajectory(_windows(*[[5.0, 5.01]] * 6))):
        assert should_monitor_kill(_broken(0.85), **{**_KILLABLE, **kwargs},
                                   trajectory=trajectory) is False


def test_a_non_broken_verdict_is_still_never_a_kill_whatever_the_curve():
    for status in ("healthy", "watch"):
        verdict = TrainingVerdict(status=status, reason="", confidence=1.0)
        rising = summarize_trajectory(_windows(*[[1.0 + k, 1.2 + k] for k in range(6)]))
        assert should_monitor_kill(verdict, **_KILLABLE, trajectory=rising) is False


# --------------------------------------------------------------------------- what the model is handed
def test_the_trajectory_block_states_the_span_the_direction_and_what_the_tail_is():
    trajectory = summarize_trajectory(_windows(*[
        [15.7 - 0.4 * k, 15.72 - 0.4 * k, 15.68 - 0.4 * k] for k in range(12)]))
    text = trajectory_context(trajectory)
    assert "TRAJECTORY MEASURED BY THE ENGINE" in text
    assert "DIRECTION: descending" in text
    assert "noise floor" in text, "the floor is what makes a sub-window drift legible"
    assert "12 readings" in text


def test_an_empty_trajectory_reproduces_the_historical_prompt_byte_for_byte():
    # Prompt strings are contracts: `_MONITOR_SYSTEM`, the stage line and the log header are
    # unchanged, and this block is SPLICED beside them. With nothing measured the user message must
    # be exactly what it was before this rung existed.
    class _Recorder:
        def __init__(self):
            self.messages = None

        def complete_tool(self, messages, schema):
            self.messages = messages
            return {"status": "healthy", "reason": "ok", "confidence": 0.6}

    class _Host(_tm.TrainingMonitorMixin):
        def __init__(self, client):
            self.developer = _FakeDeveloper(client)

    client = _Recorder()
    host = _Host(client)
    host._training_verdict("step 1 loss: 0.5", "ctx", "stage line", "")
    without = client.messages[1]["content"]
    host._training_verdict("step 1 loss: 0.5", "ctx", "stage line", "MEASURED")
    with_block = client.messages[1]["content"]
    assert without == ("ctx\n\nstage line\n\nLIVE TRAINING LOG (recent tail):\nstep 1 loss: 0.5"
                       "\n\nClassify this run's health from the log evidence above.")
    assert with_block == without.replace("LIVE TRAINING LOG", "MEASURED\n\nLIVE TRAINING LOG", 1)


def test_the_durable_row_carries_the_measurement_not_a_judgement():
    trajectory = summarize_trajectory(_windows(*[[9.0 - k, 9.1 - k] for k in range(6)]))
    row = trajectory_row(trajectory)
    assert row["direction"] == "descending" and row["windows"] == 6
    assert row["first"] == pytest.approx(9.0) and row["last"] == pytest.approx(4.1)
    assert json.dumps(row), "the alert row must stay JSON-serializable"
    assert "kill" not in row and "status" not in row, \
        "this row states what the loss DID; the verdict beside it states what a model concluded"


def _stub_signature(stub):
    """An `inspect.Signature` for a stub read out of the AST (never imported).

    Read rather than imported because these stubs are nested inside their own tests and are not
    reachable as module attributes. Every parameter kind the suite's stubs use is carried —
    positional, `*args`, keyword-only, `**kwargs` — because `bind` is only a real check if the
    signature it binds against is the real one.
    """
    import inspect

    args = stub.args
    kinds = inspect.Parameter
    parameters = []
    positional = list(args.posonlyargs) + list(args.args)
    # `args.defaults` covers the LAST n positional parameters; everything before them is required.
    filled = ([inspect.Parameter.empty] * (len(positional) - len(args.defaults))
              + [None] * len(args.defaults))
    for arg, default in zip(args.posonlyargs, filled):
        parameters.append(kinds(arg.arg, kinds.POSITIONAL_ONLY, default=default))
    for arg, default in zip(args.args, filled[len(args.posonlyargs):]):
        parameters.append(kinds(arg.arg, kinds.POSITIONAL_OR_KEYWORD, default=default))
    if args.vararg is not None:
        parameters.append(kinds(args.vararg.arg, kinds.VAR_POSITIONAL))
    for arg, node in zip(args.kwonlyargs, args.kw_defaults):
        parameters.append(kinds(arg.arg, kinds.KEYWORD_ONLY,
                                default=inspect.Parameter.empty if node is None else None))
    if args.kwarg is not None:
        parameters.append(kinds(args.kwarg.arg, kinds.VAR_KEYWORD))
    parameters = [p for p in parameters if p.name != "self"]
    return inspect.Signature(parameters)


def test_the_loop_and_the_verdict_helper_agree_on_the_positional_contract():
    """The loop calls `_training_verdict` POSITIONALLY, and every test that substitutes a stub for it
    re-declares that signature by hand.

    A stub one parameter short raises `TypeError` INSIDE the loop's own per-tick
    `except Exception: continue`, so the monitor spins instead of failing — measured: adding
    `trajectory_text` turned `test_monitor_cancellation_joins_the_paid_verdict_worker` into a hang,
    not a red test. Bind the real call site's argument count against the real signature so the next
    parameter cannot strand a stub silently.

    TWO call shapes count, because the loop moved between them: the verdict used to be handed to
    `to_thread.run_sync` as a callable plus its arguments, and since the log-tools derivation was
    pushed into the worker (it globs + fstats the workdir, and as a `run_sync` ARGUMENT it ran on the
    event-loop thread) it is CALLED inside a closure the worker runs instead. Both are the same fact
    — the arguments reaching that method — so the finder takes either rather than pinning the
    dispatch shape, which is not the property.

    **KEYWORDS COUNT TOO, and until 2026-08-20 they did not.** This assertion counted POSITIONAL
    arguments, which is the MECHANISM the first two breakages happened to use and not the property.
    Adding `contract_text=contract_text` as a KEYWORD left `passed` at 5, kept every assertion here
    green, and turned `test_monitor_cancellation_joins_the_paid_verdict_worker` into a 15-minute
    hang for the THIRD time — the exact failure this test's own docstring describes, walked into by
    the guard that was written to prevent it. The property is that the loop's call, however it is
    spelled, BINDS against every stub, so the check is now an `inspect.Signature.bind` of the real
    call against each stub's real parameter list.
    """
    import ast
    import inspect
    from pathlib import Path

    from tests._source_scan import function_tree

    tree = function_tree(_tm.TrainingMonitorMixin._monitor_training)
    passed = None
    keywords: list = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        first = node.args[0] if node.args else None
        if isinstance(func, ast.Attribute) and func.attr == "_training_verdict":
            passed = len(node.args)            # called directly, in the worker's closure
            keywords = [k.arg for k in node.keywords if k.arg]
        elif isinstance(first, ast.Attribute) and first.attr == "_training_verdict":
            passed = len(node.args) - 1        # everything after the callable itself
            keywords = [k.arg for k in node.keywords if k.arg]
    assert passed is not None, "the loop must still call _training_verdict on a worker thread"
    real = inspect.signature(_tm.TrainingMonitorMixin._training_verdict)
    # BIND, do not count. The real method is the first thing the call must fit.
    real.bind(None, *[None] * passed, **{name: None for name in keywords})

    # ...and the same bound over every STUB the suite substitutes for it. Binding the loop against
    # the real signature is only half the rule and it is the half that was already true both times
    # this broke: `trajectory_text` and then `tools` each left `test_train_monitor.py`'s
    # `_blocking_verdict` one parameter short, and each turned a test into a HANG while this
    # assertion stayed green. A stub is found the way the loop finds it — an assignment to
    # `<something>._training_verdict` — so a stub added later inherits the check instead of escaping
    # a hand-written list of file names.
    stubs = 0
    for path in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"))
        functions = {node.name: node for node in ast.walk(module)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for node in ast.walk(module):
            if not isinstance(node, ast.Assign):
                continue
            targets = [t for t in node.targets
                       if isinstance(t, ast.Attribute) and t.attr == "_training_verdict"]
            if not targets or not isinstance(node.value, ast.Name):
                continue
            stub = functions.get(node.value.id)
            if stub is None:                    # not a def in this file (a lambda, an import) — skip
                continue
            stubs += 1
            signature = _stub_signature(stub)
            try:
                signature.bind(*[None] * passed, **{name: None for name in keywords})
            except TypeError as exc:
                raise AssertionError(
                    f"{path.name}::{stub.name}{signature} cannot accept the loop's call "
                    f"({passed} positional, keywords {sorted(keywords)}): {exc}. A stub the call "
                    "does not fit raises TypeError inside the monitor's own per-tick "
                    "`except Exception: continue`, so the test HANGS instead of "
                "failing.")
    assert stubs, "no _training_verdict stub found — this half of the guard went vacuous"


# ------------------------------------------------------------------------------ the tracker's scope
def test_the_tracker_accumulates_one_window_per_tick_and_resets_on_a_new_curve():
    # The trajectory's value is its SPAN, so the tracker keeps one reduction per tick from the first
    # one onward: consecutive 128 KiB tails do not overlap at a 10-minute cadence, and re-reading a
    # multi-GB log per tick is the cost the bounded seek-to-tail read exists to avoid.
    tracker = LossTrajectoryTracker()
    for k in range(6):
        tracker.observe("\n".join(f"loss: {9.0 - k + d}" for d in (0.0, 0.1, -0.1)))
    assert tracker.summary().direction == "descending" and tracker.summary().windows == 6
    # A different stage log is a DIFFERENT curve; splicing them would invent both a jump and a trend.
    tracker.reset()
    assert tracker.summary().direction == "unknown" and tracker.summary().windows == 0
    assert tracker.observe("nothing numeric here") is None, "a silent tick contributes no window"


def test_the_window_history_is_bounded():
    tracker = LossTrajectoryTracker(max_windows=4)
    for k in range(20):
        tracker.observe(f"loss: {20.0 - k}")
    trajectory = tracker.summary()
    assert trajectory.windows == 4
    assert trajectory.last == pytest.approx(1.0), "the newest reading must survive the bound"


# ---------------------------------------------------------------------------------- through the loop
def test_a_descending_run_is_not_killed_and_the_row_says_why(tmp_path):
    """End to end, at the v7 defect's own shape: a confident `broken` about a log whose measured
    trajectory is descending neither kills nor arms, and the durable row carries the measurement."""
    wd = tmp_path / "node_0"
    wd.mkdir()
    log = wd / "train.log"
    log.write_text("\n".join(f"{{'loss': {24.4 - 0.001 * i:.4f}, 'epoch': 1.0}}" for i in range(8)))

    class _DescendingClient:
        """Answers `broken` every tick — the model's verdict is deliberately NOT changed by this
        rung — while the log keeps descending by a whole point per tick."""

        def __init__(self):
            self.calls = 0

        def complete_tool(self, messages, schema):
            self.calls += 1
            base = 24.4 - self.calls
            with open(log, "w", encoding="utf-8") as fh:
                fh.write("\n".join(f"{{'loss': {base - 0.001 * i:.4f}, 'epoch': 1.0}}"
                                   for i in range(8)))
            return {"status": "broken", "reason": "loss pinned, not learning", "confidence": 0.95}

    client = _DescendingClient()
    host, spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(client), kill=True, plan=_TRAIN_PLAN,
        until=lambda h: sum(1 for t, d in h.store.events
                            if t == EV_TRAIN_MONITOR_ALERT and d.get("trajectory_veto")) >= 1)

    assert host.kill_signal.get("kill") is None and not host.cancel.is_set()
    vetoed = [d for t, d in host.store.events
              if t == EV_TRAIN_MONITOR_ALERT and d.get("trajectory_veto")]
    assert vetoed, "the counterfactual must be durable: it WOULD have killed but for the curve"
    assert vetoed[-1]["status"] == "broken", "the model keeps its verdict; only the kill is refused"
    assert vetoed[-1]["trajectory"]["direction"] == "descending"
    tm_spans = [s for s in spans if s.get("name") == "train_monitor"]
    assert any(s["attributes"].get("trajectory_veto") for s in tm_spans)
    # The FIRST tick holds one reading, i.e. a tail, i.e. no trajectory — so it arms exactly as it
    # did before this rung existed, and the arm's own re-look is what produces the second reading
    # that then vetoes. Every tick from the second onward refuses to re-arm, so the cost of a
    # descending run being called `broken` forever is one confirmation call, not one per tick.
    armed = [s for s in tm_spans if s["attributes"].get("kill_armed")]
    assert len(armed) <= 1, "only the pre-trajectory first look may arm a gate the curve refuses"


def test_a_non_finite_run_is_still_killed(tmp_path):
    """The positive control through the whole loop: `runs/rubertlite-dr-unified-v6` node 5's shape
    — a descending curve that then prints a nan — still reaches the kill."""
    wd = tmp_path / "node_0"
    wd.mkdir()
    log = wd / "train.log"
    log.write_text("\n".join(f"{{'loss': {9.0 - 0.001 * i:.4f}, 'grad_norm': 1.0}}" for i in range(6)))

    class _ExplodingClient:
        def __init__(self):
            self.calls = 0

        def complete_tool(self, messages, schema):
            self.calls += 1
            with open(log, "w", encoding="utf-8") as fh:
                if self.calls >= 2:
                    fh.write(f"{{'loss': nan, 'grad_norm': nan, 'epoch': {self.calls}}}\n")
                else:
                    fh.write("\n".join(f"{{'loss': {8.0 - 0.001 * i:.4f}, 'grad_norm': 1.0}}"
                                       for i in range(6)))
            return {"status": "broken", "reason": "loss is nan", "confidence": 0.95}

    host, _spans = _run_verdict_monitor(
        tmp_path, workdir=wd, developer=_FakeDeveloper(_ExplodingClient()), kill=True,
        plan=_TRAIN_PLAN, until=lambda h: h.kill_signal.get("kill"))
    assert host.kill_signal.get("kill") is True
    assert host.kill_signal.get("terminal_reason") == "monitor_broken"
