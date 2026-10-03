"""Bounded public reference for a single-parent score comparison, from the folded lineage."""


def public_parent_comparison(node):
    """Never replace a recorded parent lifecycle with the parent's current attempt."""
    if node is None or len(node.parent_ids) != 1:
        return None
    parent_id = node.parent_ids[0]
    attempt = node.parent_generations.get(str(parent_id))
    if (isinstance(parent_id, bool) or not isinstance(parent_id, int) or parent_id < 0
            or isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0):
        return None
    return {"version": 1, "node_id": parent_id, "attempt": attempt}
