"""Typed completion comparison evidence; missing metadata is not permission to rank."""
from looplab.core.fitness import is_usable_metric


def valid_score_comparison(row):
    comparison, parents = row.get("score_comparison"), row.get("parents")
    if (not isinstance(comparison, dict) or type(comparison.get("version")) is not int
            or comparison["version"] != 1 or type(comparison.get("parent_count")) is not int
            or not isinstance(parents, list) or len(parents) > 8
            or comparison["parent_count"] < len(parents)):
        return False
    ids = set()
    for parent in parents:
        if (not isinstance(parent, dict)
                or any(type(parent.get(k)) is not int or parent[k] < 0 for k in ("node_id", "attempt"))
                or parent["node_id"] in ids or parent["node_id"] == row.get("node_id")
                or "score" not in parent or parent["score"] is not None and not is_usable_metric(parent["score"])
                or parent.get("comparability") not in ("same", "different", "unknown")):
            return False
        ids.add(parent["node_id"])
    count, status = comparison["parent_count"], comparison.get("status")
    if status == "no_parent":
        return count == 0 and not parents
    if status == "multiple_parents":
        return count > 1
    if count != 1:
        return False
    if status == "parent_unavailable":
        return not parents
    if len(parents) != 1 or status not in (
            "same", "different", "unknown", "base_different", "base_unknown", "ineligible", "retargeted"):
        return False
    return status != "same" or (
        row.get("status") == "evaluated" and row.get("feasible") is True
        and row.get("direction") in ("min", "max")
        and row.get("trust_flagged") is False and row.get("salvaged") is False
        and type(row.get("violations")) is int and row["violations"] == 0
        and is_usable_metric(row.get("score")) and is_usable_metric(parents[0]["score"])
        and is_usable_metric(row["score"] - parents[0]["score"])
        and parents[0]["comparability"] == "same")
