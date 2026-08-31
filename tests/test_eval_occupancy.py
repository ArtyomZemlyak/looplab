"""Was this run starved? — and the bootstrap is not starvation.

MEASURED, and the reason the module exists: answering "were the GPUs busy" took an ad-hoc script
three times in one day and the first two answers were wrong the SAME way, by counting the stretch
before the first build could possibly have finished as a starved lane. Separated, over this box's
two runs — same engine, same `eval_parallel: 2`, opposite outcomes:

    v9   bootstrap 1.09 h | 23.66 h span, 6.61 h dead (28 %), windows of 1.41 h and 5.20 h
    v10  bootstrap 1.50 h |  2.82 h span, 0.00 h dead (0 %)

One percentage over the whole run could not tell those apart.

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

import types

from looplab.events.eval_occupancy import eval_occupancy

H = 3600.0


def _ev(kind, ts, node=None):
    row = {"type": kind, "ts": ts}
    if node is not None:
        row["data"] = {"node_id": node}
    return row


def _obj(kind, ts, node=None):
    """The shape `EventStore.read_all()` yields — an object, not a mapping."""
    return types.SimpleNamespace(type=kind, ts=ts,
                                 data={"node_id": node} if node is not None else {})


def _run(rows):
    return eval_occupancy(rows)


def test_the_bootstrap_is_reported_SEPARATELY_and_never_as_dead_time():
    """The error this module exists to stop. Mutation: fold the pre-first-eval stretch into
    `dead_seconds`, and a healthy run reports starvation it never had — which is exactly the wrong
    number I published twice before writing this."""
    rows = [_ev("run_started", 0.0), _ev("node_eval_started", 2 * H, 0),
            _ev("node_evaluated", 5 * H, 0)]
    out = _run(rows)
    assert out["bootstrap_seconds"] == 2 * H
    assert out["dead_seconds"] == 0.0
    assert out["dead_share"] == 0.0


def test_dead_share_is_over_the_SPAN_not_the_whole_run():
    """Mutation: divide by `run_seconds`. A long bootstrap then shrinks every later starvation
    figure, which is the same category error in the denominator instead of the numerator."""
    rows = [_ev("run_started", 0.0),
            _ev("node_eval_started", 10 * H, 0), _ev("node_evaluated", 11 * H, 0),
            _ev("node_eval_started", 12 * H, 1), _ev("node_evaluated", 13 * H, 1)]
    out = _run(rows)
    assert out["span_seconds"] == 3 * H          # first start -> last event
    assert out["dead_seconds"] == 1 * H          # the hole between the two evals
    assert abs(out["dead_share"] - 1 / 3) < 1e-9


def test_the_dead_WINDOWS_are_named_not_just_totalled():
    """A 6.61 h total says a run was starved; `12.28h -> 13.69h` and `13.76h -> 18.96h` says WHERE
    to look, which is what made v9's 5.20 h window findable at all. Mutation: return the sum only."""
    rows = [_ev("run_started", 0.0),
            _ev("node_eval_started", 1 * H, 0), _ev("node_evaluated", 2 * H, 0),
            _ev("node_eval_started", 4 * H, 1), _ev("node_evaluated", 5 * H, 1),
            _ev("node_eval_started", 9 * H, 2), _ev("node_evaluated", 10 * H, 2)]
    out = _run(rows)
    assert out["dead_windows"] == [(2 * H, 4 * H), (5 * H, 9 * H)]
    assert out["dead_seconds"] == 6 * H


def test_OVERLAPPING_evals_are_one_busy_stretch_not_two():
    """The whole point on a multi-lane run. Mutation: sum interval lengths instead of merging, and
    two concurrent evals report the lane as busy twice over — so a run at width 2 could show
    negative dead time."""
    rows = [_ev("run_started", 0.0),
            _ev("node_eval_started", 1 * H, 0), _ev("node_evaluated", 4 * H, 0),
            _ev("node_eval_started", 2 * H, 1), _ev("node_evaluated", 6 * H, 1)]
    out = _run(rows)
    assert out["dead_windows"] == []
    assert out["dead_seconds"] == 0.0
    assert out["concurrency"][2] == 2 * H        # 2h..4h had both lanes live


def test_an_UNFINISHED_eval_counts_busy_to_the_last_event_and_says_so():
    """True of a live run and the most a killed one can prove. Mutation: drop the open interval, and
    a run whose only eval is still going reports itself as entirely starved."""
    rows = [_ev("run_started", 0.0), _ev("node_eval_started", 1 * H, 0), _ev("hint", 5 * H)]
    out = _run(rows)
    assert out["open_intervals"] == 1
    assert out["dead_seconds"] == 0.0
    assert out["span_seconds"] == 4 * H


def test_a_run_with_NO_evaluation_reports_no_span_rather_than_100_percent_dead():
    """Mutation: treat the whole run as a starved span. A run still doing its first build has not
    starved — it has not started — and calling that 100 % dead is the bootstrap error at its limit."""
    out = _run([_ev("run_started", 0.0), _ev("card_added", 3 * H)])
    assert out["span_seconds"] == 0.0 and out["dead_seconds"] == 0.0
    assert out["bootstrap_seconds"] == 3 * H


def test_BOTH_event_shapes_are_accepted():
    """`EventStore.read_all()` yields objects; a `json.loads` reader yields dicts. Mutation: filter
    on `isinstance(row, dict)` and the CLI's own call silently returns an empty, wrong answer — no
    exception, just a section that declines to print. That is what happened on the first wiring."""
    mapping = [_ev("run_started", 0.0), _ev("node_eval_started", 1 * H, 0),
               _ev("node_evaluated", 3 * H, 0)]
    objects = [_obj("run_started", 0.0), _obj("node_eval_started", 1 * H, 0),
               _obj("node_evaluated", 3 * H, 0)]
    a, b = _run(mapping), _run(objects)
    assert a["bootstrap_seconds"] == b["bootstrap_seconds"] == 1 * H
    assert a["span_seconds"] == b["span_seconds"] == 2 * H
    assert b["intervals"], "an object-shaped log must produce intervals, not silence"


def test_the_first_terminal_wins_as_the_fold_does():
    """Invariant 2 is "exactly one terminal per node, first wins". Mutation: `ends[node] = stamp`
    unconditionally, and a duplicate terminal stretches the interval past where the node ended."""
    rows = [_ev("run_started", 0.0), _ev("node_eval_started", 1 * H, 0),
            _ev("node_evaluated", 2 * H, 0), _ev("node_failed", 9 * H, 0)]
    out = _run(rows)
    assert out["intervals"] == [(1 * H, 2 * H, 0)]


def test_a_width_caps_reported_concurrency():
    """Mutation: ignore `width`. A run cannot have more lanes busy than it declared, and a count
    above it is a reader artifact rather than a fact about the box."""
    rows = [_ev("run_started", 0.0),
            _ev("node_eval_started", 1 * H, 0), _ev("node_evaluated", 5 * H, 0),
            _ev("node_eval_started", 1 * H, 1), _ev("node_evaluated", 5 * H, 1),
            _ev("node_eval_started", 1 * H, 2), _ev("node_evaluated", 5 * H, 2)]
    assert eval_occupancy(rows, width=2)["concurrency"].get(3) is None
    assert eval_occupancy(rows)["concurrency"].get(3) == 4 * H


def test_junk_rows_never_raise():
    """This reads a log it is reporting the health of. Mutation: index a non-row and the command
    dies on the file it exists to describe."""
    out = _run(["nonsense", None, 42, _ev("run_started", 0.0),
                _ev("node_eval_started", 1 * H, 0), _ev("node_evaluated", 2 * H, 0)])
    assert out["span_seconds"] == 1 * H
