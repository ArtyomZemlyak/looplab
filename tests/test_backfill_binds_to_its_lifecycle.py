"""A backfill row lands only on the lifecycle it was READ for (review 2026-09-22, EVT-09).

`applied_params_backfilled` and `score_metrics_backfilled` both carry a REQUIRED `generation` —
the lifecycle whose workdir / score log the maintenance pass read — and neither handler looked at
it. The pass is planned against one fold and applied later without the engine's lock, so a node
reset and re-evaluated in between received lifecycle 0's reconstruction beside lifecycle 1's
metric: an old experiment's recovered objectives, or the configuration an old tree applied,
presented as describing a number they never produced. Both handlers now bind through the fold's
own rule (`replay.py::_generation_matches`): a stamped row must name the current lifecycle, and an
unstamped one is a legacy row that binds as it always did.
"""
from __future__ import annotations

from looplab.core.models import Event
from looplab.events.replay import fold
from looplab.events.types import (EV_APPLIED_PARAMS_BACKFILLED, EV_NODE_CREATED,
                                  EV_NODE_EVALUATED, EV_NODE_RESET, EV_RUN_STARTED,
                                  EV_SCORE_METRICS_BACKFILLED)


def _terminal(generation: int, metric: float) -> dict:
    return {"node_id": 0, "generation": generation, "metric": metric, "eval_seconds": 1.0,
            "violations": [], "trials": [], "extra_metrics": {}, "stdout_tail": "",
            "metric_provenance": {"source": "stdout"}}


def _fold(*tail) -> "object":
    rows = [
        (EV_RUN_STARTED, {"run_id": "r", "task_id": "t", "direction": "max"}),
        (EV_NODE_CREATED, {"node_id": 0, "parent_ids": [], "operator": "draft",
                           "idea": {"operator": "draft", "params": {"lr": 0.1}},
                           "code": "print(1)", "files": {}, "generation": 0}),
        (EV_NODE_EVALUATED, _terminal(0, 0.79)),
        (EV_NODE_RESET, {"node_id": 0, "generation": 0, "from_stage": "eval"}),
        (EV_NODE_EVALUATED, _terminal(1, 0.81)),
        *tail,
    ]
    return fold([Event(seq=i, ts=float(i + 1), type=t, data=d) for i, (t, d) in enumerate(rows)])


def _scores(**stamp) -> tuple:
    return (EV_SCORE_METRICS_BACKFILLED, {
        "node_id": 0, "read_at": 5.0, "extra_metrics": {"ndcg@100": 0.46},
        "precision_decimals": {"ndcg@100": 2}, "unrecoverable": "", **stamp})


def _applied(**stamp) -> tuple:
    return (EV_APPLIED_PARAMS_BACKFILLED, {
        "node_id": 0, "read_at": 5.0, "workdir_digest": "1:2:3", "unrecoverable": "",
        "applied_params": {"lr": {"value": 0.1, "source": "cfg.yaml"}}, **stamp})


def test_a_score_backfill_read_for_a_superseded_lifecycle_does_not_land():
    """MUTATION: drop the `_generation_matches` guard from `_on_score_metrics_backfilled` and the
    gen-0 objectives land on the gen-1 node, marked `backfilled` beside a metric they never saw."""
    st = _fold(_scores(generation=0))
    node = st.nodes[0]
    assert node.attempt == 1 and node.metric == 0.81, "precondition: gen 1 was re-evaluated"
    assert node.extra_metrics == {}
    assert node.extra_metrics_backfill == {}


def test_a_score_backfill_for_the_current_or_an_unstamped_lifecycle_still_lands():
    """The guard refuses only a NAMED superseded lifecycle — the backfill still does its job."""
    for stamp in ({"generation": 1}, {}):
        node = _fold(_scores(**stamp)).nodes[0]
        assert node.extra_metrics == {"ndcg@100": 0.46}, stamp
        assert node.extra_metrics_backfill.get("backfilled") is True, stamp


def test_an_applied_params_backfill_read_for_a_superseded_lifecycle_does_not_land():
    """MUTATION: drop the guard from `_on_applied_params_backfilled` and lifecycle 0's workdir
    reading becomes lifecycle 1's `applied_params`."""
    node = _fold(_applied(generation=0)).nodes[0]
    assert node.metric == 0.81, "precondition"
    assert node.metric_provenance == {"source": "stdout"}
    for stamp in ({"generation": 1}, {}):
        prov = _fold(_applied(**stamp)).nodes[0].metric_provenance
        assert prov["applied_params"]["backfilled"] is True, stamp


def test_a_bool_generation_names_no_lifecycle():
    """`True == 1`, and the fold refuses a bool stamp everywhere else (`coerce_node_id`)."""
    assert _fold(_scores(generation=True)).nodes[0].extra_metrics == {}
    assert _fold(_applied(generation=True)).nodes[0].metric_provenance == {"source": "stdout"}
