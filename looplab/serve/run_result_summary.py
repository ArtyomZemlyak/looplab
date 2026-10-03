"""Small measured result receipt for Assistant, derived from the cached run-list fold."""
from looplab.core.fitness import counts_toward_best, is_usable_metric
from looplab.events.replay import flagged_node_ids


def current_trust_signals(state):
    """Advisory evidence is distinct from selection exclusion, and bound to the attempt."""
    signals = {}
    for record in state.reward_hacks:
        node = state.nodes.get(record.get("node_id"))
        if (node is None or node.tombstoned or node.id in state.aborted_nodes
                or record.get("generation", 0) != node.attempt):
            continue
        named = [s for s in record.get("signals", [])
                 if isinstance(s.get("signal"), str) and s["signal"].strip()]
        if named:
            # Bind the full folded record, including audit version/code digest;
            # identical warning text does not imply identical evidence.
            signals.setdefault(node.id, []).append(record)
    return signals


def run_result_summary(state, trajectory):
    """Use the engine's winner and trajectory; never infer a winner from raw scores.

    The first trajectory point is the first eligible experiment, not necessarily a
    task-declared baseline. This receipt makes no comparison or robustness claim.
    """
    best = state.best()
    if (best is None or best.status != "evaluated" or best.tombstoned
            or not counts_toward_best(best, set(state.breed_excluded), set(state.aborted_nodes))
            or not trajectory or not trajectory.get("points")):
        return None
    first = state.nodes.get(trajectory["points"][0][2])
    if first is None:
        return None
    advisory = set(current_trust_signals(state)) - flagged_node_ids(state)

    def measurement(node):
        value = node.confirmed_mean if node.confirmed_mean is not None else node.metric
        if not is_usable_metric(value):
            return None
        return {
            "node_id": node.id, "attempt": node.attempt, "value": float(value),
            "confirmed": node.confirmed_mean is not None,
            "seeds": node.confirmed_seeds if node.confirmed_mean is not None else None,
            "score": float(node.metric) if is_usable_metric(node.metric) else None,
            "confirmed_std": (node.confirmed_std if node.confirmed_mean is not None
                              and is_usable_metric(node.confirmed_std) and node.confirmed_std >= 0 else None),
            "trust_advisory": node.id in advisory,
        }

    return {"first": measurement(first), "selected": measurement(best)}
