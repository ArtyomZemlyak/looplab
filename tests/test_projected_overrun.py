"""The engine compares its own projection against the wall it will be held to.

`runs/e5small-dr-unified-v4` node 6, the worked case and the whole reason this exists. At 15:45 the
monitor recorded "at 6% of a ~10h run"; the stage's declared wall was 28000 s (7.78 h). Seven hours
later it was killed on that wall having burned 7.78 GPU-hours, and the deadline judge — asked 6.4
seconds before the kill — correctly refused to grant a rescue, because thirty minutes cannot close a
2.2-hour gap.

Nothing was missing except the SUBTRACTION. The remaining time was measured, the wall was declared,
and no code put the two beside each other. The deadline judge is the last line; this is the first
one, and it fires while acting is still cheap.

Corpus-wide: nine `stage_finished.status == "timeout"` rows discarding 57.6 GPU-hours, every one
landing within seconds of its own declared wall.
"""
from __future__ import annotations

import math

from looplab.engine.train_monitor import eval_log_plan, projected_overrun_s


def test_node_6_would_have_been_known_seven_hours_early():
    """The real numbers: ~30 min observed, ~9.5 h still to run, a 28000 s wall."""
    over = projected_overrun_s(1800.0, 34200.0, 28000.0)
    assert over is not None and math.isclose(over, 8000.0)


def test_a_stage_that_fits_says_nothing():
    """The row exists only when there is something to say — silence is not a verdict of 'fits', and
    a caller is told so in the docstring."""
    assert projected_overrun_s(1800.0, 1000.0, 28000.0) is None
    assert projected_overrun_s(1000.0, 0.0, 1000.0) is None, "exactly at the wall is not an overrun"


def test_every_unanswerable_input_refuses_rather_than_guesses():
    """Fail-CLOSED in both directions: a wrong overrun would reschedule real GPU time, and a wrong
    silence only leaves today's behaviour."""
    for args in [(None, 1.0, 1.0), (1.0, None, 1.0), (1.0, 1.0, None),
                 ("soon", 1.0, 1.0), (1.0, "soon", 1.0), (1.0, 1.0, "soon"),
                 (float("nan"), 1.0, 1.0), (1.0, float("inf"), 1.0), (1.0, 1.0, float("nan")),
                 (-1.0, 1.0, 1.0), (1.0, -1.0, 1.0), (1.0, 1.0, 0.0), (1.0, 1.0, -5.0)]:
        assert projected_overrun_s(*args) is None, args


def test_the_wall_comes_from_the_manifest_the_stage_will_actually_be_held_to():
    """Read from the resolved stage list, not from a setting: the number that kills a stage is the
    one its own manifest declared, and a stage without one contributes nothing rather than a
    default that would invent an overrun."""
    plan = eval_log_plan([
        {"name": "mine", "timeout": 7200.0},
        {"name": "train", "timeout": 30000.0},
        {"name": "undeclared"},
        {"name": "nonsense", "timeout": -5},
        {"name": "unreadable", "timeout": "soon"},
    ])
    assert plan.timeouts == {"mine": 7200.0, "train": 30000.0}


def test_a_plan_built_from_nothing_carries_no_walls():
    assert eval_log_plan([]).timeouts == {}
    assert eval_log_plan(None).timeouts == {}


def test_the_projection_is_conservative_by_construction():
    """`eta_s` counts training steps and not the tail, and on the two e5 nodes measured it
    UNDER-stated the truth by 4-5%. So a positive answer is a floor, never an invention — which is
    the direction that makes it safe to act on."""
    true_remaining = 34200.0
    measured = true_remaining * 0.955            # the observed 4.5% under-statement
    assert projected_overrun_s(1800.0, measured, 28000.0) < projected_overrun_s(
        1800.0, true_remaining, 28000.0)


# ---------------------------------------------------------------------------------------------
# THE STAMP. Everything above tests the arithmetic; none of it would redden if the alert row never
# carried the answer — which is the state this fixes, and the same shape as the defect itself: a
# number the engine computes and nothing reads.
# ---------------------------------------------------------------------------------------------

from types import SimpleNamespace                                            # noqa: E402

from looplab.engine.train_monitor import stamp_projected_overrun            # noqa: E402


def _traj(span, eta):
    return SimpleNamespace(span_s=span, eta_s=eta)


def test_the_alert_carries_the_projection_and_the_wall_it_missed():
    alert = {}
    stamp_projected_overrun(alert, _traj(1800.0, 34200.0),
                            SimpleNamespace(stage="train"),
                            eval_log_plan([{"name": "train", "timeout": 28000.0}]))
    assert alert["projected_overrun_s"] == 8000.0
    assert alert["stage_wall_s"] == 28000.0, "the wall is recorded too, or the number is unreadable"


def test_a_stage_that_fits_leaves_the_row_untouched():
    alert = {"status": "healthy"}
    stamp_projected_overrun(alert, _traj(1800.0, 1000.0),
                            SimpleNamespace(stage="train"),
                            eval_log_plan([{"name": "train", "timeout": 28000.0}]))
    assert alert == {"status": "healthy"}


def test_an_unresolved_stage_or_an_undeclared_wall_stamps_nothing():
    """Three separate absences, each meaning 'the engine cannot say' — and none of them may be
    quietly rendered as 'it fits'."""
    plan = eval_log_plan([{"name": "train", "timeout": 28000.0}])
    for resolved, p in [(None, plan),
                        (SimpleNamespace(stage=None), plan),
                        (SimpleNamespace(stage="mine"), plan),      # a stage with no declared wall
                        (SimpleNamespace(stage="train"), None)]:
        alert = {}
        stamp_projected_overrun(alert, _traj(1800.0, 34200.0), resolved, p)
        assert alert == {}, (resolved, p)


def test_a_trajectory_that_measured_nothing_stamps_nothing():
    alert = {}
    stamp_projected_overrun(alert, _traj(None, None), SimpleNamespace(stage="train"),
                            eval_log_plan([{"name": "train", "timeout": 28000.0}]))
    assert alert == {}
