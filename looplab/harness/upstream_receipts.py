"""Offline upstream reply checks: identities, complete gate legs and measured pairs.

Hashes do not establish semantic completeness. Failed partial comparisons stay
readable; malformed passing evidence never supplies a verdict to the agent.
"""
import math
import re
import statistics


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def integer(value):
    return type(value) is int and 0 <= value <= (1 << 53) - 1


def number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def selector(value):
    return (isinstance(value, dict) and set(value) == {"run_dir", "event_seq", "digest"}
        and isinstance(value["run_dir"], str) and 0 < len(value["run_dir"]) <= 4096
        and "\0" not in value["run_dir"] and integer(value["event_seq"]) and sha(value["digest"]))


def stage(row):
    return (isinstance(row, dict) and isinstance(row.get("name"), str) and bool(row["name"])
        and isinstance(row.get("status"), str) and bool(row["status"])
        and type(row.get("exit_code")) is int and number(row.get("seconds")) and row["seconds"] >= 0)


def execution(row):
    return (isinstance(row, dict) and isinstance(row.get("label"), str)
        and type(row.get("exit_code")) is int and type(row.get("timed_out")) is bool
        and type(row.get("valid")) is bool and number(row.get("seconds")) and row["seconds"] >= 0
        and "metric" in row and (row["metric"] is None or number(row["metric"]))
        and isinstance(row.get("workdir"), str) and isinstance(row.get("artifacts"), dict)
        and all(isinstance(k, str) and sha(v) for k, v in row["artifacts"].items())
        and "stages" in row and (row["stages"] is None or isinstance(row["stages"], list)
            and all(stage(s) for s in row["stages"])))


def equivalence(row):
    """Full paired samples and their statistics, including readable failed comparisons."""
    values = row.get("values")
    if (row.get("profile") != "full" or not isinstance(values, list) or len(values) != 2
        or not all(isinstance(v, list) and len(v) <= 10 and all(number(x) for x in v) for v in values)):
        return False
    fields = {"means", "sem", "tolerance", "delta", "source_reproduced"}
    if not fields.intersection(row):
        return row["passed"] is False
    if (not fields <= row.keys() or not 2 <= len(values[0]) == len(values[1]) <= 10
        or not all(isinstance(row[k], list) and len(row[k]) == 2 and all(number(x) for x in row[k]) for k in ("means", "sem"))
        or any(x < 0 for x in row["sem"]) or not number(row["tolerance"]) or row["tolerance"] < 0
        or not number(row["delta"]) or type(row["source_reproduced"]) is not bool):
        return False
    try:
        means = [statistics.mean(v) for v in values]
        sem = [statistics.stdev(v) / math.sqrt(len(v)) for v in values]
        return (all(math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
                    for a, b in zip(row["means"] + row["sem"] + [row["delta"]], means + sem + [means[1] - means[0]]))
            and row["passed"] == (row["source_reproduced"] and abs(row["delta"]) <= row["tolerance"]))
    except (OverflowError, ValueError):
        return False


def passing_checks(row):
    """A passing verdict contains all gate legs, bound to actual paired executions."""
    checks, executions = row["checks"], row["executions"]
    kinds = [c["kind"] for c in checks]
    if (kinds.count("equivalence") != 1 or "test" not in kinds or "regression" not in kinds
        or row.get("inputs_unchanged") is not True or not all(c["passed"] for c in checks)):
        return False
    eq = next(c for c in checks if c["kind"] == "equivalence")
    repeats = len(eq["values"][0])
    if len(executions) != kinds.count("test") + 2 * (kinds.count("regression") + kinds.count("repair") + repeats):
        return False
    offset = 0
    for check in checks:
        if check["kind"] == "equivalence":
            continue
        name = check.get("name")
        count = 1 if check["kind"] == "test" else 2
        rows = executions[offset:offset + count]
        offset += count
        if not isinstance(name, str) or not name or any(not r["valid"] or r["timed_out"] for r in rows):
            return False
        if count == 1:
            if rows[0]["label"] != "test-" + name or rows[0]["exit_code"] != 0:
                return False
        elif (rows[0]["label"] != "old-" + name or rows[1]["label"] != "new-" + name
              or rows[1]["exit_code"] != 0 or (rows[0]["exit_code"] == 0 if check["kind"] == "repair"
                  else rows[0]["exit_code"] != 0 or rows[0]["artifacts"] != rows[1]["artifacts"])):
            return False
    for i, execution_row in enumerate(executions[-2 * repeats:]):
        side, repeat = i % 2, i // 2
        if (execution_row["label"] != ("old-source" if side == 0 else "new-source") + str(repeat)
            or not execution_row["valid"] or execution_row["timed_out"] or execution_row["exit_code"] != 0
            or execution_row["metric"] != eq["values"][side][repeat]):
            return False
    return True


def gate(row):
    return (isinstance(row, dict) and type(row.get("passed")) is bool
        and sha(row.get("input_identity")) and isinstance(row.get("checks"), list)
        and isinstance(row.get("executions"), list) and len(row["executions"]) <= 116
        and all(execution(r) for r in row["executions"])
        and all(isinstance(r, dict) and type(r.get("passed")) is bool
                and r.get("kind") in ("test", "regression", "repair", "equivalence")
                and (r["kind"] != "equivalence" or equivalence(r)) for r in row["checks"])
        and number(row.get("eval_seconds")) and row["eval_seconds"] >= 0
        and math.isclose(row["eval_seconds"], sum(r["seconds"] for r in row["executions"]), rel_tol=1e-9, abs_tol=1e-9)
        and (not row["passed"] or passing_checks(row)))


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
