"""The run's metric TRAJECTORY as a bounded series on the run-summary row (doc 52 row 26).

`/api/runs` carried `nodes` as a COUNT and no series, and `ui/src/crossRunRank.js` named what that
cost in its own constant: a cross-run trajectory overlay would have needed one `/api/runs/{id}/state`
per run — 45 folds on the request thread for a panel that opens on a click, against a server whose
live runs re-fold on every poll. The series lives HERE instead, derived once from the same fold the
summary row is built from and cached WITH it (`serve/run_projections.py::run_summaries` keys its
cache on `file_identity`), so it costs one derivation per changed log and nothing per poll.

WHAT THE SERIES IS. The running best over the run's evaluated experiments, in node-id order — the
same line `ui/src/charts.jsx::Trajectory` draws for one run, spelled once on the server so the
cross-run overlay and the single-run chart cannot disagree about which node moved the frontier:

  * the x population is every evaluated, non-tombstoned, non-aborted node with a usable metric
    (`RunState.evaluated_nodes()` minus `aborted_nodes` — the chart's `nodeIsActive`), so an
    infeasible node still occupies an x slot: it was an experiment the run paid for;
  * only a FEASIBLE node may advance the best (`RunState.feasible_nodes()`, the set `best()` selects
    from), so the line never claims a best the engine rejected;
  * the value is `confirmed_mean` when the node was re-measured, else `metric` — the chart's rule.

WHAT IS CARRIED, AND WHY IT IS SMALL. A running best is a STEP function, so the series is exact as its
CHANGE points alone: `[index, best, node_id]` for every experiment that improved the best, plus the
final experiment's index when the run went on after its last improvement, so a line drawn from the
points reaches the run's end. A run with 2,000 experiments and 14 improvements is 15 triples. Runs
with more improvements than `TRAJECTORY_CAP` are subsampled at an even stride that keeps the first
and the last point and say so with `complete: False` — the reader draws a coarser step, never a
different best.

`None` when no feasible measured node exists: an absent series is "nothing to draw", which is what the
overlay must print, and never a flat line at zero.
"""
from __future__ import annotations

from typing import Optional

from looplab.core.fitness import is_usable_metric
from looplab.core.models import RunState

TRAJECTORY_VERSION = 1
# The most change points one row carries. Sized against the chart, not the disk: the overlay draws
# at most eight runs on one axis, and 200 steps per run is already past what a 760-px chart can
# separate.
TRAJECTORY_CAP = 200


def _node_value(node) -> Optional[float]:
    """The y value the trajectory plots — the chart's `confirmed_mean ?? metric`."""
    if node.confirmed_mean is not None and is_usable_metric(node.confirmed_mean):
        return float(node.confirmed_mean)
    return float(node.metric) if is_usable_metric(node.metric) else None


def _subsample(points: list, cap: int) -> list:
    """`cap` points at an even stride over `points`, first and last kept. `cap >= 2`."""
    last = len(points) - 1
    keep = sorted({round(k * last / (cap - 1)) for k in range(cap)})
    return [points[i] for i in keep]


def running_best(state: RunState, *, cap: int = TRAJECTORY_CAP) -> Optional[dict]:
    """The bounded running-best series of `state`, or `None` when there is nothing to draw.

    Shape: `{"version", "points": [[index, best, node_id], ...], "evaluated", "complete"}`, where
    `index` is the 0-based position among the run's drawn experiments (node-id order), `best` the
    running best AFTER that experiment, `node_id` the node holding that best, `evaluated` the size
    of the x population and `complete` whether every change point is present.
    """
    aborted = set(state.aborted_nodes)
    drawn = sorted((n for n in state.evaluated_nodes()
                    if n.id not in aborted and _node_value(n) is not None), key=lambda n: n.id)
    advancers = {n.id for n in state.feasible_nodes()}
    points: list = []
    best: Optional[float] = None
    best_id: Optional[int] = None
    for index, node in enumerate(drawn):
        value = _node_value(node)
        if node.id in advancers and (best is None or state.is_better(value, best)):
            best, best_id = value, node.id
            points.append([index, best, best_id])
    if not points:
        return None
    last_index = len(drawn) - 1
    if points[-1][0] != last_index:
        # The run kept evaluating after its last improvement: extend the line to where it stopped,
        # holding the best it had — the final experiment's index, the best's own node.
        points.append([last_index, best, best_id])
    complete = len(points) <= max(2, int(cap))
    if not complete:
        points = _subsample(points, max(2, int(cap)))
    return {"version": TRAJECTORY_VERSION, "points": points, "evaluated": len(drawn),
            "complete": complete}
