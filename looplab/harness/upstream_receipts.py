"""Offline upstream reply checks: identities, complete gate legs and measured pairs.

Hashes do not establish semantic completeness. Failed partial comparisons stay
readable; malformed passing evidence never supplies a verdict to the agent.
"""
import re

from looplab.core.upstream_evidence import execution, gate, sha


def integer(value):
    return type(value) is int and 0 <= value <= (1 << 53) - 1


def selector(value):
    return (isinstance(value, dict) and set(value) == {"run_dir", "event_seq", "digest"}
        and isinstance(value["run_dir"], str) and 0 < len(value["run_dir"]) <= 4096
        and "\0" not in value["run_dir"] and integer(value["event_seq"]) and sha(value["digest"]))


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
    from looplab.engine.upstream_state import digest
    active, candidates, history = page["active_base"], page["candidates"], page["history"]
    if (active["selector"] is not None and not selector(active["selector"])) or (page["enabled"] and active["selector"] is None):
        return False
    if active["advance_seq"] is not None and not integer(active["advance_seq"]):
        return False
    advance = active["advance_seq"]
    if advance is not None and (active["selector"] is None or advance <= active["selector"]["event_seq"]):
        return False
    # The engine's CAS revision binds this generation, selector and advancement.
    # A well-formed hash alone cannot make an inconsistent snapshot readable.
    if active["revision"] != digest({"generation": page["generation"].lower(),
            "selector": active["selector"], "advance_seq": advance}):
        return False
    if candidates["bounded"] and len(candidates["rows"]) != candidates["limit"]:
        return False
    if len(candidates["rows"]) > candidates["limit"] or not all(isinstance(r, dict) and event(r, r.get("type")) for r in history):
        return False
    pagination = {"offset", "next_offset", "source_node_id"}
    if pagination & set(candidates):
        if not pagination <= set(candidates) or not integer(candidates["offset"]):
            return False
        next_offset, source = candidates["next_offset"], candidates["source_node_id"]
        if (next_offset is not None and (not integer(next_offset) or next_offset != candidates["offset"] + candidates["limit"])
                or candidates["bounded"] != (next_offset is not None)
                or source is not None and (not integer(source) or any(r.get("node_id") != source for r in candidates["rows"] if isinstance(r, dict)))):
            return False
    if any(a["seq"] >= b["seq"] for a, b in zip(history, history[1:])):
        return False
    # Older pages need not contain the current advancement. Any advancement
    # they do contain must agree with, or precede, the current base snapshot.
    if any(r["type"] == "base_advanced" and (advance is None or r["seq"] > advance
            or r["seq"] == advance and r["selector"] != active["selector"]) for r in history):
        return False
    return all(isinstance(r, dict) and integer(r.get("node_id")) and integer(r.get("generation"))
        and sha(r.get("hunk_hash")) and sha(r.get("source_signature")) and isinstance(r.get("path"), str)
        and r.get("classification") in ("capability", "recipe", "already_promoted")
        and r.get("origin") in ("idea", "repair") and isinstance(r.get("pending_trigger_nodes"), list)
        and all(integer(n) for n in r["pending_trigger_nodes"]) for r in candidates["rows"])
