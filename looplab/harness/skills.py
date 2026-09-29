"""Evidence-bound auto-skill drafts authored by an external reasoning owner."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import orjson
from fastapi import HTTPException

from looplab.core.atomicio import append_jsonl_bytes_locked
from looplab.core.models import NodeStatus
from looplab.engine.lessons_reconcile import LessonReconcileMixin
from looplab.engine.memory import assess_skill_statement, unreliable_metric_ids, write_auto_skill
from looplab.events.eventstore import EventStore, EventStoreLockError, interprocess_lock, read_jsonl_lenient
from looplab.events.replay import fold
from looplab.serve.run_commands import run_generation_token
from looplab.tools.skills import parse_skill_frontmatter


def publish_skill_candidate(srv, rd: Path, body) -> dict:
    """Join a portable technique to a current supported lesson and write one skill.

    The agent chooses whether and what to distill. The server selects the evidence,
    fingerprint and lifecycle status. An identical action_id is a stable receipt.
    """
    try:
        with srv.commands.sequence(rd):
            settings = json.loads((rd / "config.snapshot.json").read_bytes())
            if not settings.get("external_harness"):
                raise HTTPException(409, "external skill authoring requires an external harness run")
            memory_dir = settings.get("memory_dir")
            if not isinstance(memory_dir, str) or not memory_dir:
                raise HTTPException(400, "this run has no cross-run memory_dir")
            events = EventStore(rd / "events.jsonl").read_all()
            generation = run_generation_token(events)
            if not generation or generation != body.expected_generation.lower():
                raise HTTPException(409, {"code": "run_generation_changed",
                                          "current_generation": generation})
            state = fold(events)
            if not state.run_uid:
                raise HTTPException(409, "run has no durable identity")

            payload = {"generation": generation, "lesson_action_id": body.lesson_action_id,
                       "body": body.body.strip()}
            digest = hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
            base = Path(memory_dir)
            receipts = base / "skill_candidate_actions.jsonl"
            base.mkdir(parents=True, exist_ok=True)
            with interprocess_lock(Path(str(receipts) + ".lock"), required=True):
                if receipts.exists() and receipts.stat().st_size > 64 * 1024 * 1024:
                    raise HTTPException(503, {"code": "skill_receipts_unavailable"})
                for row in read_jsonl_lenient(receipts):
                    if row.get("run_uid") == state.run_uid and row.get("action_id") == body.action_id:
                        if row.get("payload_sha256") != digest:
                            raise HTTPException(409, "skill action_id was reused with different content")
                        return {"ok": True, "replayed": True, "skill": row["skill"]}

                lessons = base / "lessons.jsonl"
                if not lessons.exists() or lessons.stat().st_size > 64 * 1024 * 1024:
                    raise HTTPException(409, "supported lesson evidence is unavailable")
                # A lesson can be retired after a node reset. Inspect the current
                # store under its writer's lock, then keep it held through the skill
                # write so retirement cannot overtake this evidence check.
                with interprocess_lock(Path(str(lessons) + ".lock"), required=True):
                    source = next((row for row in read_jsonl_lenient(lessons)
                                   if row.get("run_uid") == state.run_uid
                                   and row.get("harness_action_id") == body.lesson_action_id), None)
                    if source is None or source.get("outcome") != "supported":
                        raise HTTPException(409, "the supported lesson is missing or retired")
                    evidence = source.get("evidence") or []
                    signatures = source.get("evidence_sig") or {}
                    unreliable = unreliable_metric_ids(state)
                    if not evidence or not isinstance(signatures, dict):
                        raise HTTPException(409, "lesson has no verifiable evidence")
                    for nid in evidence:
                        node = state.nodes.get(nid) if type(nid) is int else None
                        if (node is None or node.status is not NodeStatus.evaluated
                                or node.tombstoned or nid in (state.aborted_nodes or [])
                                or nid in unreliable
                                or signatures.get(str(nid)) != LessonReconcileMixin._node_sig(node)):
                            raise HTTPException(409, "lesson evidence changed or is not a measured result")
                    statement = str(source.get("statement") or "")
                    assessment = assess_skill_statement(statement)
                    if not assessment.promotable:
                        raise HTTPException(422, {"code": "skill_statement_not_portable",
                                                  "reason": assessment.reason})
                    fingerprint = source.get("fingerprint")
                    if not isinstance(fingerprint, list) or not all(
                            isinstance(term, str) for term in fingerprint):
                        raise HTTPException(409, "lesson fingerprint is invalid")
                    written = write_auto_skill(
                        base / "skills", assessment.statement, body.body, fingerprint,
                        state.task_id, classifier_version="external-evidence-v1",
                        source_statement=statement)
                    if written is None:
                        raise HTTPException(503, {"code": "skill_store_unavailable"})
                    metadata = parse_skill_frontmatter(written.read_text(encoding="utf-8"))
                    skill = {"name": metadata.get("name"), "status": metadata.get("status"),
                             "claim_sha256": metadata.get("claim_sha256"),
                             "source_statement_sha256": metadata.get("source_statement_sha256")}
                    append_jsonl_bytes_locked(receipts, orjson.dumps({
                        "run_uid": state.run_uid, "run_id": state.run_id,
                        "action_id": body.action_id, "payload_sha256": digest,
                        "lesson_action_id": body.lesson_action_id, "skill": skill,
                    }))
                    return {"ok": True, "replayed": False, "skill": skill}
    except HTTPException:
        raise
    except (OSError, EventStoreLockError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(503, {"code": "skill_store_unavailable"}) from exc
