"""Small discovery view derived from the same obligations as the full progress read.

This is an observed prefix, never a candidate permit or a claim that an engine or
agent is alive. Continue and finish are separate choices; an expansion-only gate
must not force research on an agent that is trying to finish.
"""
from __future__ import annotations

_RUN = "/api/runs/{run_id}"


def _step(code, title, detail, reads, *, action=None, phase_id=None):
    return {"code": code, "title": title, "detail": detail,
            "owner": "external_agent", "reads": reads,
            "action": action, "phase_id": phase_id}


def next_step(progress: dict) -> dict:
    if not progress["complete"]:
        return _step("inspect_sources", "Check incomplete sources",
                     "A missing receipt is not proof that no action occurred. Inspect source_health and ask the operator to recover damaged journals before trusting missing receipts.",
                     [f"GET {_RUN}/harness-progress", f"GET {_RUN}/events"])
    if progress["pending_checkpoint_count"]:
        q = progress["pending_checkpoints"][0]["question"]
        return _step("answer_checkpoint", "Answer the evaluation question",
                     "Evaluation has an unanswered checkpoint. Read live state, the full question and its allowed verdicts; evaluator completion alone does not settle the node.",
                     [f"GET {_RUN}/state", f"GET {_RUN}/harness-checkpoints"],
                     action=f"POST {_RUN}/harness-checkpoints", phase_id=q["phase_id"])
    lifecycle = progress["recorded_lifecycle"]
    if any(lifecycle.values()):
        title = ("Inspect recorded finish" if lifecycle["finished"] else
                 "Inspect stop request" if lifecycle["stop_requested"] else "Run is paused")
        return _step("inspect_lifecycle", title,
                     "Read live state and command receipts before deciding to resume or complete finalization. Journal state does not certify engine or agent liveness.",
                     [f"GET {_RUN}/state", f"GET {_RUN}/events",
                      f"GET {_RUN}/command-receipt?expected_generation=TOKEN&command_id={{command_id}}"])
    if progress["finish_pending_nodes"]:
        return _step("inspect_pending", "Inspect submitted experiments",
                     "Submitted nodes remain unsettled. Read live state and checkpoints; finalization must wait for settlement or explicit cancellation. Further proposals have their own gates below.",
                     [f"GET {_RUN}/state", f"GET {_RUN}/harness-checkpoints"])
    return _step("choose_direction", "Choose the next experiment or finish",
                 "Use measured evidence to choose. The two paths below have different obligations; policy advice does not submit a candidate.",
                 [f"GET {_RUN}/state", f"GET {_RUN}/harness-contract"])


def brief(progress: dict) -> dict:
    """Omit observations, reports and history payloads before MCP's response cap.

    Counts, source health and pagination receipts survive the projection. Read the
    full endpoint for history and checkpoints for authoritative verdict authority.
    """
    keys = ("generation", "run_uid", "event_seq", "at_node", "evidence_revision",
            "complete", "source_health", "recorded_lifecycle", "next_step",
            "candidate_blockers_if_expanding", "candidate_decisions_per_idea",
            "candidate_requirements", "finish_reviews_due", "finish_report_due",
            "pending_checkpoint_count")
    result = {key: progress[key] for key in keys}
    nodes = progress["finish_pending_nodes"]
    result.update(finish_pending_nodes=nodes[:20], finish_pending_node_count=len(nodes),
                  finish_pending_nodes_truncated=len(nodes) > 20)
    result["pending_checkpoints"] = [{key: row["question"][key] for key in
        ("checkpoint_id", "node_id", "node_generation", "claim_seq", "phase_id")}
        for row in progress["pending_checkpoints"][:20]]
    result["pending_checkpoints_truncated"] = progress["pending_checkpoint_count"] > 20
    result["history"] = {kind: {key: page[key] for key in
        ("total", "offset", "limit", "has_more")} for kind, page in progress["history"].items()}
    preview = progress["policy_preview"]
    result["policy_preview"] = {key: preview[key] for key in
        ("policy", "policy_source", "total_actions", "meaning")}
    result["details"] = {"history": f"GET {_RUN}/harness-progress?expected_generation=TOKEN&offset=0&limit=20",
                         "questions": f"GET {_RUN}/harness-checkpoints?expected_generation=TOKEN",
                         "phase": "MCP phases / phase_info before each decision"}
    result["refresh"] = "Refresh after events or responses; use the current /state generation. No automatic retry, resume or candidate submission."
    return result
