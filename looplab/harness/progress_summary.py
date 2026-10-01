"""Small discovery view derived from the same obligations as the full progress read.

This is an observed prefix with an independent last-read engine lock probe,
never a candidate permit or proof of agent connection. Continue and finish are
separate choices; an expansion-only gate must not force research on an agent
that is trying to finish.
"""
from __future__ import annotations

from looplab.harness.phases import CHECKPOINT_DECISION_PHASES
from looplab.harness.checkpoint_history import allowed_verdicts

_RUN = "/api/runs/{run_id}"


def _step(code, title, detail, reads, *, action=None, phase_id=None):
    return {"code": code, "title": title, "detail": detail,
            "owner": "external_agent", "reads": reads,
            "action": action, "phase_id": phase_id}


def next_step(progress: dict) -> dict:
    step = _next_step(progress)
    execution = progress["execution"]
    alive = execution["engine_running"]
    label = "running" if alive is True else "stopped" if alive is False else "unknown"
    if step["code"] == "answer_checkpoint" and alive is False:
        step["detail"] = ("This is a recorded question from a stopped engine. Inspect state and saved command receipts before choosing explicit recovery or cancellation. Resume may re-evaluate the interrupted attempt and supersede this question; refresh after resume before answering. "
                          + step["detail"])
        step["reads"].append(f"GET {_RUN}/command-receipt?expected_generation=TOKEN&command_id={{command_id}}")
    step["detail"] += f" Engine last observed: {label}. Agent connection: not measured."
    if progress["complete"] and progress["finish_pending_nodes"]:
        counts = execution["recorded_node_counts"]
        step["detail"] += (f" Recorded activity: {counts['evaluating']} admitted, "
                           f"{counts['queued']} queued, {counts['building']} building, "
                           f"{counts['pending']} untracked.")
    return step


def _next_step(progress: dict) -> dict:
    if not progress["complete"]:
        return _step("inspect_sources", "Check incomplete sources",
                     "A missing receipt is not proof that no action occurred. Inspect source_health and ask the operator to recover damaged journals before trusting missing receipts.",
                     [f"GET {_RUN}/harness-progress?expected_generation=TOKEN", f"GET {_RUN}/events"])
    if progress["pending_checkpoint_count"]:
        q = progress["pending_checkpoints"][0]["question"]
        title = {"stage_check": "Review the completed stage",
                 "train_monitor": "Answer the training monitor",
                 "deadline_grace": "Decide whether to extend the deadline"}.get(
                     q["phase_id"], "Answer the evaluation question")
        detail = "Evaluation has an unanswered checkpoint. Read live state, the full question and its allowed verdicts; evaluator completion alone does not settle the node."
        detail += " Allowed verdicts: " + ", ".join(allowed_verdicts(q)) + "."
        if q["phase_id"] in ("train_monitor", "asha_live") and not q["kill_enabled"]:
            detail += " This checkpoint does not grant abort authority."
        if q.get("stop_refusal") == "objective_retargeted":
            detail += " The objective was retargeted; this ASHA curve remains on the task scale."
        if q["phase_id"] == "deadline_grace":
            detail += " While waiting for a verdict, the command may keep running; this wait has no automatic timeout. The runtime caps one extension starting after extend is consumed."
        if progress["recorded_lifecycle"]["paused"]:
            detail = "Run is paused for new work; its recorded in-flight evaluation still has this checkpoint. Answering does not resume search. " + detail
        return _step("answer_checkpoint", title, detail,
                     [f"GET {_RUN}/state?observe_only=true", f"GET {_RUN}/harness-checkpoints?expected_generation=TOKEN"],
                     action=f"POST {_RUN}/harness-checkpoints",
                     phase_id=CHECKPOINT_DECISION_PHASES.get(q["phase_id"]))
    lifecycle = progress["recorded_lifecycle"]
    if any(lifecycle.values()):
        title = ("Inspect recorded finish" if lifecycle["finished"] else
                 "Inspect stop request" if lifecycle["stop_requested"] else "Run is paused")
        return _step("inspect_lifecycle", title,
                     "Read live state and command receipts before deciding to resume or complete finalization. Journal state does not certify engine or agent liveness.",
                     [f"GET {_RUN}/state?observe_only=true", f"GET {_RUN}/events",
                      f"GET {_RUN}/command-receipt?expected_generation=TOKEN&command_id={{command_id}}"])
    if progress["finish_pending_nodes"]:
        execution = progress["execution"]
        counts = execution["recorded_node_counts"]
        alive = execution["engine_running"]
        if alive is False:
            title = "Engine stopped · inspect submitted experiments"
            detail = "No live engine owner was observed. Recorded evaluation starts do not mean training continues. Inspect state, checkpoints and original command receipts before choosing explicit recovery."
        elif alive is None:
            title = "Engine status unknown · inspect submitted experiments"
            detail = "The engine lock probe is inconclusive. Recorded node activity does not prove live training. Inspect state and checkpoints before choosing recovery."
        else:
            title = ("Inspect evaluations already started" if counts["evaluating"] else
                     "Submitted experiments are awaiting evaluation" if counts["queued"] else
                     "Inspect submitted experiments")
            detail = "LoopLab owns evaluation; poll checkpoints for questions and results. A live engine does not prove the agent is connected."
        return _step("inspect_pending", title,
                     detail + " Finalization waits for settlement or explicit cancellation; further proposals have separate gates.",
                     [f"GET {_RUN}/state?observe_only=true", f"GET {_RUN}/harness-checkpoints?expected_generation=TOKEN",
                      f"GET {_RUN}/command-receipt?expected_generation=TOKEN&command_id={{command_id}}"])
    return _step("choose_direction", "Choose the next experiment or finish",
                 "Use measured evidence to choose. The two paths below have different obligations; policy advice does not submit a candidate.",
                 [f"GET {_RUN}/state?observe_only=true", f"GET {_RUN}/harness-contract"])


def brief(progress: dict) -> dict:
    """Omit observations, reports and history payloads before MCP's response cap.

    Counts, source health and pagination receipts survive the projection. Read the
    full endpoint for history and checkpoints for authoritative verdict authority.
    """
    keys = ("generation", "run_uid", "event_seq", "at_node", "evidence_revision",
            "complete", "source_health", "recorded_lifecycle", "execution", "next_step",
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
