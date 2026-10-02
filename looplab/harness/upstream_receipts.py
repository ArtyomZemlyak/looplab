"""Offline shape/evidence checks for upstream HTTP 200 pages and acknowledgements."""
import math
import re


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def integer(value):
    return type(value) is int and 0 <= value <= (1 << 53) - 1


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def selector(value):
    return (isinstance(value, dict) and set(value) == {"run_dir", "event_seq", "digest"}
        and isinstance(value["run_dir"], str) and 0 < len(value["run_dir"]) <= 4096
        and "\0" not in value["run_dir"] and integer(value["event_seq"]) and sha(value["digest"]))


def execution(row):
    return (isinstance(row, dict) and isinstance(row.get("label"), str)
        and type(row.get("exit_code")) is int and type(row.get("timed_out")) is bool
        and type(row.get("valid")) is bool and number(row.get("seconds")) and row["seconds"] >= 0
        and "metric" in row and (row["metric"] is None or number(row["metric"]))
        and isinstance(row.get("workdir"), str) and isinstance(row.get("artifacts"), dict)
        and all(isinstance(k, str) and sha(v) for k, v in row["artifacts"].items())
        and "stages" in row)


def gate(row):
    return (isinstance(row, dict) and type(row.get("passed")) is bool
        and sha(row.get("input_identity")) and isinstance(row.get("checks"), list)
        and isinstance(row.get("executions"), list) and len(row["executions"]) <= 116
        and all(execution(r) for r in row["executions"])
        and all(isinstance(r, dict) and type(r.get("passed")) is bool
                and r.get("kind") in ("test", "regression", "repair", "equivalence") for r in row["checks"])
        and number(row.get("eval_seconds")) and row["eval_seconds"] >= 0
        and math.isclose(row["eval_seconds"], sum(r["seconds"] for r in row["executions"]), rel_tol=1e-9, abs_tol=1e-9)
        and (not row["passed"] or bool(row["checks"]) and bool(row["executions"])
             and row.get("inputs_unchanged") is True and all(r["passed"] for r in row["checks"])))


def event(row, kind):
    """Required durable identities and measurement fields; unknown fields are readable."""
    if (not isinstance(row, dict) or not integer(row.get("seq")) or not sha(row.get("request_hash"))
        or not isinstance(row.get("action_id"), str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", row["action_id"]) is None
        or not isinstance(row.get("proposal_id"), str) or re.fullmatch(r"up_[0-9a-f]{24}", row["proposal_id"]) is None):
        return False
    if kind in ("upstream_proposal_started",):
        return True
    if kind == "upstream_proposal_failed":
        return isinstance(row.get("code"), str) and bool(row["code"])
    if kind == "upstream_proposed":
        return (selector(row.get("selector")) and selector(row.get("old_selector"))
            and sha(row.get("manifest_hash")) and sha(row.get("source_signature"))
            and sha(row.get("expected_base_revision")) and integer(row.get("source_node_id"))
            and isinstance(row.get("capability_paths"), list) and sha(row.get("source_recipe_digest"))
            and isinstance(row.get("source_recipe_paths"), list)
            and isinstance(row.get("flag"), dict) and isinstance(row.get("critic"), dict))
    if kind == "upstream_gate_started":
        return sha(row.get("input_identity"))
    if kind == "upstream_execution":
        return execution(row.get("execution"))
    if kind == "upstream_gate_finished":
        from looplab.engine.upstream_state import digest
        try:
            return gate(row.get("result")) and sha(row.get("evidence_token")) and row["evidence_token"] == digest(row["result"])
        except (ValueError, TypeError):
            return False
    if kind == "upstream_gate_abandoned":
        return isinstance(row.get("claim_action_id"), str) and isinstance(row.get("reason"), str) and bool(row["reason"].strip())
    if kind == "base_advanced":
        return (selector(row.get("selector")) and sha(row.get("from_revision"))
            and sha(row.get("evidence_token")) and integer(row.get("gate_seq")) and row["gate_seq"] < row["seq"]
            and integer(row.get("source_node_id")) and isinstance(row.get("flag"), dict))
    return False


def page_detail(page):
    active, candidates, history = page["active_base"], page["candidates"], page["history"]
    if (active["selector"] is not None and not selector(active["selector"])) or (page["enabled"] and active["selector"] is None):
        return False
    if active["advance_seq"] is not None and not integer(active["advance_seq"]):
        return False
    if len(candidates["rows"]) > 200 or not all(isinstance(r, dict) and event(r, r.get("type")) for r in history):
        return False
    if any(a["seq"] >= b["seq"] for a, b in zip(history, history[1:])):
        return False
    return all(isinstance(r, dict) and integer(r.get("node_id")) and integer(r.get("generation"))
        and sha(r.get("hunk_hash")) and sha(r.get("source_signature")) and isinstance(r.get("path"), str)
        and r.get("classification") in ("capability", "recipe", "already_promoted")
        and r.get("origin") in ("idea", "repair") and isinstance(r.get("pending_trigger_nodes"), list)
        and all(integer(n) for n in r["pending_trigger_nodes"]) for r in candidates["rows"])
