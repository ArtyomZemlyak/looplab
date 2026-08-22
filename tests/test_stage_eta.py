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
