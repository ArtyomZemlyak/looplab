"""One bounded, read-only view of external decisions and their current validity.

The event log is authoritative for measured outcomes; decision, review and
checkpoint sidecars are independently durable. This view names each source and
its health instead of quietly treating an unreadable or damaged sidecar as an
empty history. A response is an observed event prefix, not an atomic snapshot
of several files; clients refresh when ``event_seq`` moves.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from looplab.core.config import read_config_snapshot
from looplab.core.jsonlio import read_jsonl_lenient_with_health
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.harness.decisions import decision_file, required_decisions
from looplab.harness.hypotheses import merge_due
from looplab.harness.obligations import (concept_tags_required, evidence_revision,
                                         final_report_due, report_cadence_due,
                                         research_due, run_base_due)
from looplab.harness.reviews import (cadence_reviews_due, missing_reviews,
                                     required_reviews, review_file)
from looplab.harness.selection import value_due, verification_due
from looplab.serve.run_commands import run_generation_token

_SIDECAR_MAX_BYTES = 16 * 1024 * 1024


def _source(path: Path) -> tuple[list[dict], dict]:
    try:
        exists = path.exists()
        if exists and path.stat().st_size > _SIDECAR_MAX_BYTES:
            raise HTTPException(503, {"code": "harness_history_too_large", "source": path.name})
        rows, health = read_jsonl_lenient_with_health(path)
        return rows, {**health, "file_present": exists}
    except OSError as exc:
        raise HTTPException(503, {"code": "harness_history_unavailable", "source": path.name}) from exc


def _page(rows: list[dict], offset: int, limit: int) -> dict:
    return {"total": len(rows), "offset": offset, "limit": limit,
            "items": list(reversed(rows))[offset:offset + limit],
            "has_more": offset + limit < len(rows)}


def _validate_rows(rows: list[dict], health: dict, fields: dict[str, type]) -> list[dict]:
    valid = [row for row in rows if all(type(row.get(key)) is value
                                       for key, value in fields.items())]
    health["invalid_record_rows"] = len(rows) - len(valid)
    health["read_complete"] &= len(valid) == len(rows)
    return valid


def snapshot(rd: Path, expected_generation: str, *, offset: int = 0,
             limit: int = 20) -> dict:
    store = EventStore(rd / "events.jsonl")
    events = store.read_all()
    generation = run_generation_token(events)
    if not generation or generation != expected_generation.lower():
        raise HTTPException(409, "run generation changed")
    settings = read_config_snapshot(rd / "config.snapshot.json")
    if not settings.external_harness:
        raise HTTPException(409, "this run uses the built-in agent cycle")
    state = fold(events)
    uid = state.run_uid
    n = len(state.nodes)
    revision = evidence_revision(state)

    decisions, decision_health = _source(decision_file(rd))
    reviews, review_health = _source(review_file(rd))
    checkpoints, checkpoint_health = _source(rd / "harness_checkpoints.jsonl")
    decisions = _validate_rows(decisions, decision_health, {
        "generation": str, "run_uid": str, "phase_id": str,
        "at_node": int, "evidence_revision": str, "idea_sha256": str,
        "decision": str, "reason": str, "action_id": str})
    reviews = _validate_rows(reviews, review_health, {
        "generation": str, "run_uid": str, "phase_id": str,
        "at_node": int, "evidence_revision": str, "decision": str, "reason": str,
        "action_id": str})
    decisions = [{**row, "validity": (
        "current_evidence_for_idea" if row.get("at_node") == n
        and row.get("evidence_revision") == revision else "superseded")}
        for row in decisions if row.get("generation") == generation
        and row.get("run_uid") == uid]
    reviews = [{**row, "validity": (
        "current" if row.get("at_node") == n
        and row.get("evidence_revision") == revision else "superseded")}
        for row in reviews if row.get("generation") == generation
        and row.get("run_uid") == uid]

    questions, answers = {}, {}
    invalid_records = 0
    for row in checkpoints:
        if (row.get("type") not in ("question", "answer")
                or not isinstance(row.get("checkpoint_id"), str)
                or not row["checkpoint_id"]):
            invalid_records += 1
            continue
        if row["type"] == "question":
            if (type(row.get("node_id")) is not int
                    or type(row.get("node_generation")) is not int
                    or type(row.get("claim_seq")) is not int
                    or not all(isinstance(row.get(key), str) for key in
                               ("run_generation", "run_uid", "phase_id", "stage",
                                "expectation", "observation"))
                    or row["checkpoint_id"] in questions):
                invalid_records += 1
                continue
            questions[row["checkpoint_id"]] = row
        else:
            if (not all(isinstance(row.get(key), str) for key in
                        ("verdict", "action_id", "reason"))
                    or row["checkpoint_id"] in answers):
                invalid_records += 1
                continue
            answers[row["checkpoint_id"]] = row
    invalid_records += sum(key not in questions for key in answers)
    checkpoint_health["invalid_record_rows"] = invalid_records
    checkpoint_health["read_complete"] &= invalid_records == 0
    claim_seqs = {}
    for event in events:
        if event.type == "eval_invocation_claimed":
            node_id, attempt = event.data.get("node_id"), event.data.get("generation")
            if type(node_id) is int and type(attempt) is int:
                claim_seqs[(node_id, attempt)] = event.seq
    checkpoint_rows = []
    for q in questions.values():
        if q.get("run_generation") != generation or q.get("run_uid") != uid:
            continue
        answer = answers.get(q["checkpoint_id"])
        node = state.nodes.get(q.get("node_id"))
        same_attempt = (node is not None and node.attempt == q.get("node_generation")
                        and q.get("claim_seq") ==
                        claim_seqs.get((q["node_id"], q["node_generation"]), -1))
        checkpoint_rows.append({"question": q, "answer": answer,
                                "lifecycle": "same_node_attempt" if same_attempt else "superseded",
                                "status": "answered" if answer else
                                "pending" if same_attempt and node.status == "pending"
                                else "superseded"})

    blockers = []
    def due(name: str, condition: bool, action: str):
        if condition:
            blockers.append({"phase_id": name, "action": action})

    due("research", research_due(settings, state, events), "command:research_completed")
    due("report", report_cadence_due(settings, state, events), "command:report_generated")
    due("concept_run_base", run_base_due(settings, state), "command:run_concepts")
    due("hypothesis_merge", merge_due(settings, state, events),
        "GET/POST /api/runs/{run_id}/harness-hypotheses")
    due("selection_verifier", verification_due(settings, state),
        "GET/POST /api/runs/{run_id}/harness-selection/verify")
    due("value_estimate", value_due(settings, state),
        "GET/POST /api/runs/{run_id}/harness-selection/values")
    for phase in cadence_reviews_due(rd, settings, state, generation):
        due(phase, True, "POST /api/runs/{run_id}/harness-reviews")

    health = {"decisions": decision_health, "reviews": review_health,
              "checkpoints": checkpoint_health}
    pending = [row for row in checkpoint_rows if row["status"] == "pending"]
    return {"generation": generation, "run_uid": uid,
            "event_seq": events[-1].seq, "at_node": n,
            "evidence_revision": revision, "complete": all(
                item["read_complete"] for item in health.values()),
            "source_health": health,
            "candidate_blockers_if_expanding": blockers,
            "candidate_decisions_per_idea": required_decisions(settings, n),
            "candidate_requirements": {
                "effective_concepts": concept_tags_required(settings),
                "hypothesis_or_card": bool(settings.track_hypotheses),
            },
            "finish_reviews_due": missing_reviews(rd, settings, state, generation),
            "finish_report_due": final_report_due(settings, state, events),
            "pending_checkpoint_count": len(pending),
            "pending_checkpoints": pending[:100],
            "pending_checkpoints_truncated": len(pending) > 100,
            "history": {"decisions": _page(decisions, offset, limit),
                        "reviews": _page(reviews, offset, limit),
                        "checkpoints": _page(checkpoint_rows, offset, limit)},
            "review_phases_enabled": sorted(required_reviews(settings))}
