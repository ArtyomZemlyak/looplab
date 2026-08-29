"""Public, evidence-backed activity for one search-node lifecycle.

``Node.status == pending`` deliberately says only that a lifecycle has no terminal yet.  It does
not say whether the Developer is still building it, the evaluator owns it, or it is waiting for an
evaluation slot. Those distinctions already exist in the folded record, but the eval-boundary
receipts are internal because they were introduced for budget recovery. This module is the single
public projection of those facts; the engine and selection model continue to read the original
fields directly.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from looplab.core.models import NodeStatus, RunState


NODE_ACTIVITY_SCHEMA = 1


def _build_marker(state: RunState, node_id: int) -> Optional[Mapping[str, Any]]:
    """Return this node's current build marker, including legacy singular projections."""

    marker = state.buildings.get(node_id)
    if marker is None and state.building and state.building.get("node_id") == node_id:
        marker = state.building
    return marker if isinstance(marker, Mapping) else None


def _started_at(value: Any) -> Optional[float]:
    """Wire timestamps only when the record carries a usable positive epoch value."""

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def public_node_activity(state: RunState, node_id: int) -> dict[str, Any]:
    """Project the current lifecycle phase without inferring execution from ``pending``.

    The status vocabulary is intentionally smaller than the engine's pipeline vocabulary:

    * ``building`` — a generation-matched ``node_building`` marker is still open;
    * ``queued`` — ``node_created`` promised admission and the current owner has not admitted it;
    * ``evaluating`` — the current engine owner admitted that exact generation;
    * ``pending`` — a legacy/untracked pending lifecycle, whose start cannot be proven;
    * terminal statuses mirror the node's durable status.

    ``engine_running`` is a server-side liveness fact and is therefore not folded here.  Consumers
    combine it with this projection: an ``evaluating`` lifecycle on a stopped engine is interrupted,
    not presented as live training.
    """

    node = state.nodes.get(node_id)
    marker = _build_marker(state, node_id)
    if marker is not None:
        generation = marker.get("generation")
        if type(generation) is not int or generation < 0:
            generation = node.attempt if node is not None else 0
        out: dict[str, Any] = {
            "schema": NODE_ACTIVITY_SCHEMA,
            "status": "building",
            "generation": generation,
            "evidence": "node_building",
        }
        if (started := _started_at(marker.get("started"))) is not None:
            out["started_at"] = started
        return out

    if node is None:
        return {
            "schema": NODE_ACTIVITY_SCHEMA,
            "status": "pending",
            "generation": 0,
            "evidence": "node_missing",
        }

    base: dict[str, Any] = {
        "schema": NODE_ACTIVITY_SCHEMA,
        "generation": node.attempt,
    }
    if node.status is NodeStatus.pending:
        if getattr(node, "eval_start_boundary", False) is not True:
            return {**base, "status": "pending", "evidence": "legacy_untracked"}
        if getattr(node, "eval_activity_started", False) is not True:
            return {**base, "status": "queued", "evidence": "node_created_boundary"}
        out = {**base, "status": "evaluating", "evidence": "node_eval_started"}
        if (started := _started_at(getattr(node, "eval_started_at", None))) is not None:
            out["started_at"] = started
        return out

    return {
        **base,
        "status": node.status.value,
        "evidence": f"node_{node.status.value}",
    }
