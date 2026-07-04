"""Multi-seed top-k confirmation (I12, ADR-15). Re-evaluate the top-k search nodes
under several seeds, summarize mean/std, and pick the **robust** winner by mean —
demoting a seed-lucky leader whose single-eval metric flattered it. Uses the >1-SE
gate to report whether the robust winner is *significantly* better than the
single-eval leader.
"""
from __future__ import annotations

from typing import Callable

from looplab.trust.cv import cv_summary
from looplab.trust.gate import one_se_better
from looplab.core.models import Node


def robust_selection(summaries: list[dict], leader_id, direction: str) -> dict:
    """The PURE robust-winner selection step over per-node confirm summaries (each carrying
    `node_id`/`mean`/`std`/`n`), given the single-eval leader's node id and the task direction.
    Returns `{"robust": <winning summary>, "significant": <bool>}`. Shared by `confirm_top_k`
    below and the engine's confirm-phase tail (engine/confirm_phase.py) so the two selections
    can never drift — the decision must be byte-identical in both."""
    chooser = min if direction == "min" else max
    # Selection: the robust winner = best confirmed MEAN (demotes seed-lucky leaders,
    # whose robust mean is worse than their lucky single score).
    robust = chooser(summaries, key=lambda s: (s["mean"], s["node_id"]))
    # the single leader may have been skipped (no usable seeds) -> fall back to the robust pick
    leader_summ = next((s for s in summaries if s["node_id"] == leader_id), robust)
    # Variance gate (I10): is the demotion statistically meaningful (>1 SE of the
    # difference)? Recorded for transparency; selection still uses the robust mean.
    significant = robust["node_id"] != leader_summ["node_id"] and one_se_better(
        robust["mean"], leader_summ["mean"], robust["std"], robust["n"], direction,
        incumbent_std=leader_summ["std"], incumbent_n=leader_summ["n"])
    return {"robust": robust, "significant": significant}


def confirm_top_k(
    nodes: list[Node],
    eval_fn: Callable[[Node, int], float],
    k: int,
    seeds: list[int],
    direction: str = "min",
) -> dict:
    """`eval_fn(node, seed) -> metric`. Returns the robust best plus per-node summaries."""
    ranked = sorted(nodes, key=lambda n: n.metric, reverse=(direction == "max"))
    candidates = ranked[:k]

    summaries = []
    for nd in candidates:
        scores = [eval_fn(nd, s) for s in seeds]
        if not scores:        # a node with zero usable seed results must NOT win with a
            continue          # fabricated 0.0 mean — skip it (mirrors the orchestrator's guard)
        summ = cv_summary(scores)
        summaries.append({"node_id": nd.id, "single_metric": nd.metric, **summ})

    if not summaries:  # nothing to confirm
        return {"best_node_id": None, "robust": None, "summaries": [],
                "demoted_single_leader": False, "significant": False}

    single_leader = candidates[0]  # best single-eval metric
    sel = robust_selection(summaries, single_leader.id, direction)
    robust = sel["robust"]
    return {
        "best_node_id": robust["node_id"],
        "robust": robust,
        "summaries": summaries,
        "demoted_single_leader": robust["node_id"] != single_leader.id,
        "significant": sel["significant"],
    }
