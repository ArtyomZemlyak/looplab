"""How long the running stage still has — the number nothing in this engine could state.

`_resource_envelope` carries a GPU count and memory and no time at all; a search for `eta` /
`predicted_duration` / `estimated_seconds` across `engine/` and `search/` found nothing. So every
scheduling question an operator asks — "can a second experiment fit beside this one?" — was
unanswerable from the record, not merely unanswered.

It is derived from what the trajectory tracker ALREADY collects on every tick (`progress_done`,
`progress_total`, `at`), so it costs no new parse and cannot drift from the progress the same row
reports.

VALIDATED AGAINST A REAL 9.13-HOUR NODE (`runs/e5small-dr-unified-v4` node 4), replaying its own
tqdm line: at step 695 the ETA implied a 9.16 h total (+0.3 %), at step 1923 it implied 9.14 h
(+0.1 %). Early on it under-states — at step 260 it implied 8.81 h (-3.5 %) — because a step-rate
figure counts training steps and not the tail (in-process test, checkpoint write, score stage).
That bias is one-directional and correctable, but not from two nodes: this records the RAW
extrapolation and lets `node_evaluated.eval_seconds` supply the truth until a correction can be
measured.
"""
from looplab.engine.train_monitor import (
    LossTrajectory, LossWindow, summarize_trajectory, trajectory_row,
)


def _w(done, at, total=1000, count=1):
    return LossWindow(median=1.0, masd=0.0, count=count, first=1.0, last=1.0,
                      minimum=1.0, maximum=1.0, progress_done=done, progress_total=total, at=at)


def test_the_arithmetic_is_the_run_s_own_rate():
    """100 steps in 10 s is 0.1 s/step. 800 remain — counted from the LAST observed step, not the
    first, which is the arithmetic slip this assertion was written with the first time."""
    tr = summarize_trajectory([_w(100, 0.0), _w(200, 10.0)])
    assert tr.eta_s == (1000 - 200) * (10.0 / 100) == 80.0


def test_it_reaches_the_durable_row_rounded():
    tr = summarize_trajectory([_w(100, 0.0), _w(200, 10.0)])
    assert trajectory_row(tr)["eta_s"] == 80.0


def test_a_finished_bar_answers_zero_not_a_negative():
    """A bar at or past its own total is finishing, not overdue."""
    assert summarize_trajectory([_w(500, 0.0), _w(1000, 5.0)]).eta_s == 0.0
    assert summarize_trajectory([_w(500, 0.0), _w(1200, 5.0)]).eta_s == 0.0


# --------------------------------------------------------------------------- the five refusals

def test_one_window_cannot_state_a_rate():
    """One window is a tail by another name — but note WHICH guard says so.

    The refusal comes from the forward-motion check, not from a window count: with one window
    `first` and `last` are the same object, so nothing advanced. A separate `len(rows) < 2` guard
    was written here first and deleted when mutation showed it could not fail, because this guard
    already covers the case."""
    tr = summarize_trajectory([_w(100, 0.0)])
    assert tr.eta_s is None
    assert "eta_s" not in (trajectory_row(tr) or {})


def test_a_missing_progress_pair_refuses():
    assert summarize_trajectory([_w(None, 0.0), _w(200, 10.0)]).eta_s is None
    assert summarize_trajectory([_w(100, 0.0), _w(200, 10.0, total=None)]).eta_s is None


def test_a_non_positive_total_refuses():
    assert summarize_trajectory([_w(100, 0.0, total=0), _w(200, 10.0, total=0)]).eta_s is None


def test_a_counter_that_did_not_advance_refuses():
    """A stalled or restarted bar. Dividing by zero advance would mint an infinite ETA; dividing by
    a NEGATIVE one (a restart) would mint a confident wrong number, which is worse."""
    assert summarize_trajectory([_w(200, 0.0), _w(200, 10.0)]).eta_s is None
    assert summarize_trajectory([_w(200, 0.0), _w(50, 10.0)]).eta_s is None


def test_a_non_positive_span_refuses():
    assert summarize_trajectory([_w(100, 5.0), _w(200, 5.0)]).eta_s is None
    assert summarize_trajectory([_w(100, 0.0), _w(200, None)]).eta_s is None


def test_an_absent_eta_is_never_read_as_soon():
    """The reader-side contract: a row without `eta_s` means the engine cannot say."""
    row = trajectory_row(summarize_trajectory([_w(100, 0.0), _w(100, 10.0)]))
    assert row is not None and "eta_s" not in row


# --------------------------------------------------------------------------- the real node

def test_it_lands_within_a_few_percent_on_a_real_nine_hour_node():
    """Replays node 4's own progress line. The point is not a golden number but the SHAPE: an early
    estimate that is already close and converges rather than wandering."""
    # (step, elapsed seconds) sampled from the live log at 1/3 and 9/10 of the way through
    early = summarize_trajectory([_w(260, 4056.0, total=2109), _w(695, 10836.0, total=2109)])
    late = summarize_trajectory([_w(695, 10836.0, total=2109), _w(1923, 29988.0, total=2109)])
    actual_total_s = 9.13 * 3600

    early_total = 10836.0 + early.eta_s
    late_total = 29988.0 + late.eta_s
    assert abs(early_total - actual_total_s) / actual_total_s < 0.10, early_total / 3600
    assert abs(late_total - actual_total_s) / actual_total_s < 0.05, late_total / 3600


# ------------------------------------------------------- the two ends must share ONE progress bar

def test_a_SANITY_BAR_paired_with_a_young_train_bar_no_longer_INVENTS_an_overrun():
    """The dangerous mix, and the reason this was fixed on 2026-08-30 rather than left noted.

    `_latest_progress` reports the LAST counter in a tick's tail, whichever lane rendered it, and
    109 of the 109 stage logs above 200 KB on this box carry more than one bar lane. Pairing
    `rows[0]`'s done with `rows[-1]`'s done/total therefore subtracted two unrelated counters. Most
    mixes only DEFLATE the rate, but a first window ending on a near-complete eval-on-start /
    sanity-check bar and a last window on a young train bar gives a small positive `advanced` over a
    real span, which OVERSTATES the per-step time.

    That became load-bearing in `ac189252`: a beyond-grace `projected_overrun_s` now opens the
    durable alert gate on its own, so an inflated ETA can mint a row about a stage that fits.

    Here: a 2-step sanity bar completes, then 98 training steps in 600 s. The old pairing read
    "98 steps in 600 s" as the TRAIN rate (6.1 s/step) and projected 10,490 remaining steps at
    ~17.8 hours. There is only one train-bar window, so the honest answer is that no rate is
    measurable yet.

    Mutation: pair `rows[0]` with `rows[-1]` again and this returns a number instead of None.
    """
    windows = [_w(2, 0.0, total=2), _w(100, 600.0, total=10590)]
    assert summarize_trajectory(windows).eta_s is None


def test_an_in_epoch_VALIDATION_interlude_is_skipped_not_treated_as_a_wall():
    """Coverage matters as much as correctness here. A validation bar between two train-bar windows
    is the ORDINARY shape, so refusing whenever the previous window is on another lane would answer
    None for most real runs and withdraw the projection entirely.

    The pair is taken from the two TRAIN windows across the interlude: 100 steps in 20 s. The wall
    time includes the validation, so the rate comes out deflated — the conservative direction.

    Mutation: stop the walk at the first foreign total (the first cut of this fix) and this is None.
    """
    windows = [_w(100, 0.0, total=1000), _w(3, 10.0, total=361), _w(200, 20.0, total=1000)]
    assert summarize_trajectory(windows).eta_s == (1000 - 200) * (20.0 / 100) == 160.0


def test_no_earlier_window_on_this_lane_answers_None():
    """Mutation: fall back to `rows[0]` when nothing matches, and the mixed pair is back."""
    assert summarize_trajectory([_w(50, 0.0, total=361), _w(120, 10.0, total=10590)]).eta_s is None


def test_a_bar_that_RESTARTED_inside_its_own_lane_is_not_a_rate():
    """Equal totals do not prove one continuous count: an epoch bar re-rendering from 0 shares its
    total with the bar before it. The nearest same-lane window being AHEAD of the last one is that
    restart, and everything older belongs to a previous cycle.

    THE FIXTURE HAS TO REACH PAST THE RESTART, and the first one did not: with every earlier window
    AHEAD of the last, a mutant that scans on finds no usable anchor either and answers None for its
    own reason. The mutation run said so. Here the oldest window (done 20) sits BEHIND the last
    (done 30), so scanning past the restart at done 900 finds it and measures 10 steps in 20 s —
    a plausible 1940 s ETA computed across a counter reset.

    Mutation: `continue` past the restart instead of refusing, and this returns 1940.0.
    """
    assert summarize_trajectory([_w(20, 0.0), _w(900, 10.0), _w(30, 20.0)]).eta_s is None


def test_the_ordinary_single_lane_run_is_UNCHANGED():
    """The regression guard: with one bar throughout, the anchor is the immediately preceding window
    and the arithmetic is exactly what it always was."""
    assert summarize_trajectory([_w(100, 0.0), _w(200, 10.0)]).eta_s == 80.0
    assert summarize_trajectory([_w(50, 0.0), _w(100, 5.0), _w(200, 15.0)]).eta_s == (
        (1000 - 200) * (10.0 / 100))
