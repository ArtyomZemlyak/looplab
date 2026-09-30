"""Small measured result receipt for Assistant, derived from the cached run-list fold."""
from looplab.core.fitness import counts_toward_best, is_usable_metric


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

    def measurement(node):
        value = node.confirmed_mean if node.confirmed_mean is not None else node.metric
        if not is_usable_metric(value):
            return None
        return {
            "node_id": node.id, "attempt": node.attempt, "value": float(value),
            "confirmed": node.confirmed_mean is not None,
            "seeds": node.confirmed_seeds if node.confirmed_mean is not None else None,
        }

    return {"first": measurement(first), "selected": measurement(best)}
