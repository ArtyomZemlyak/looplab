"""Evidence-bound auto-skill drafts authored by an external reasoning owner."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import orjson
from fastapi import HTTPException

from looplab.core.atomicio import append_jsonl_bytes_locked, atomic_write_text
from looplab.core.jsonutil import canonical_json_digest
from looplab.engine.external_skill_source import (normalize_source, exclusion_policy, check_exclusions,
    validate_external_skill_source)
from looplab.core.models import NodeStatus
from looplab.core.fitness import is_usable_metric
from looplab.engine.lessons_reconcile import LessonReconcileMixin
from looplab.engine.memory import assess_skill_statement, unreliable_metric_ids, write_auto_skill
from looplab.events.eventstore import EventStoreLockError, interprocess_lock
from looplab.harness.journals import read_event_source, read_knowledge_source
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.tools.skills import parse_skill_frontmatter


def parse_external_skill_request(body):
    source = getattr(body, "external_source", None)
    code = getattr(body, "validation_code", None)
    terms = getattr(body, "excluded_terms", None)
    review = getattr(body, "exclusion_review", None)
    if source is None and code is None and terms is None and review is None:
        return None
    try:
        source = normalize_source(source)
        # Native task identity is added during both sequenced evidence checks.
        exclusion_policy("request-shape-check", terms, review_reason=review)
        if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > 32000:
            raise ValueError("invalid_external_skill_validation")
        return {"source": source, "validation_code": code, "excluded_terms": terms,
                "exclusion_review": review}
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(422, {"code": "invalid_external_skill_source"}) from exc


_external_request = parse_external_skill_request


def publish_skill_candidate(srv, rd: Path, body) -> dict:
    external = parse_external_skill_request(body)
    if external is None:
        return _publish(srv, rd, body)
    # Preflight and exact replay before any payload execution. CPU smoke never holds
    # the command sequencer or the cross-run memory/lesson writer locks.
    preflight = _publish(srv, rd, body, external=external, preflight=True)
    if preflight.get("ok"):
        return preflight
    policy = exclusion_policy(preflight["task_id"], external["excluded_terms"],
                              review_reason=external["exclusion_review"])
    try:
        checked = validate_external_skill_source(external["source"], external["validation_code"],
            policy, protected_roots=[str(rd), str(preflight["memory_dir"])])
    except ValueError as exc:
        raise HTTPException(422, {"code": "external_skill_source_excluded"}) from exc
    if checked["passed"] is not True:
        raise HTTPException(422, {"code": "external_skill_smoke_failed", "validation": checked})
    return _publish(srv, rd, body, external=external,
                    verified={"preflight": preflight, "validation": checked, "policy": policy})


def _publish(srv, rd: Path, body, *, external=None, preflight=False, verified=None) -> dict:
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
            events = read_event_source(rd)
            generation = run_generation_token(events)
            if not generation or generation != body.expected_generation.lower():
                raise HTTPException(409, {"code": "run_generation_changed",
                                          "current_generation": generation})
            state = fold(events)
            if not state.run_uid:
                raise HTTPException(409, "run has no durable identity")

            payload = {"generation": generation, "lesson_action_id": body.lesson_action_id,
                       "body": body.body.strip()}
            if external is not None:
                payload["external_import"] = external
            digest = hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
            base = Path(memory_dir)
            receipts = base / "skill_candidate_actions.jsonl"
            base.mkdir(parents=True, exist_ok=True)
            with interprocess_lock(Path(str(receipts) + ".lock"), required=True):
                if receipts.exists() and receipts.stat().st_size > 64 * 1024 * 1024:
                    raise HTTPException(503, {"code": "skill_receipts_unavailable"})
                for row in read_knowledge_source(receipts):
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
                    source = next((row for row in read_knowledge_source(lessons)
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
                    external_metadata = None
                    if external is not None and any(not is_usable_metric(state.nodes[nid].metric)
                                                    for nid in evidence):
                        raise HTTPException(409, "external skill requires finite measured lesson evidence")
                    if external is not None:
                        policy = exclusion_policy(state.task_id, external["excluded_terms"],
                                                  review_reason=external["exclusion_review"])
                        try:
                            check_exclusions(external["source"], policy,
                                extra_text=body.body + "\n" + external["validation_code"])
                        except ValueError as exc:
                            raise HTTPException(422, {"code": "external_skill_source_excluded"}) from exc
                        binding = {"run_uid": state.run_uid, "generation": generation,
                            "task_id": state.task_id, "memory_dir": str(base),
                            "objective_key": state.objective_key, "direction": state.direction, "goal": state.goal,
                            "lesson_sha256": canonical_json_digest(source),
                            "evidence_sha256": canonical_json_digest({str(nid): state.nodes[nid].model_dump(mode="json")
                                                                      for nid in evidence}),
                            "policy_sha256": canonical_json_digest(policy), "payload_sha256": digest}
                        if preflight:
                            return binding
                        if verified is None or verified["preflight"] != binding:
                            raise HTTPException(409, {"code": "external_skill_evidence_changed"})
                        artifact = {"schema": "looplab.external-skill-import/v1",
                            "request": external, "policy": policy, "binding": binding,
                            "validation": verified["validation"]}
                        # Source snapshots live OUTSIDE skills/: a selected SKILL.md can
                        # never be discovered as an unreviewed manual skill.
                        relative = "external_skill_sources/" + digest + ".json"
                        target = base / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        artifact_text = json.dumps(artifact, ensure_ascii=False, sort_keys=True)
                        atomic_write_text(target, artifact_text)
                        external_metadata = {"source_sha256": external["source"]["snapshot_sha256"],
                            "artifact_sha256": hashlib.sha256(artifact_text.encode()).hexdigest(),
                            "artifact_ref": relative, "passed": True}
                    written = write_auto_skill(
                        base / "skills", assessment.statement, body.body, fingerprint,
                        state.task_id, classifier_version="external-evidence-v1",
                        source_statement=statement,
                        **({"external_source_sha256": external_metadata["source_sha256"]}
                           if external_metadata is not None else {}))
                    if written is None:
                        raise HTTPException(503, {"code": "skill_store_unavailable"})
                    metadata = parse_skill_frontmatter(written.read_text(encoding="utf-8"))
                    skill = {"name": metadata.get("name"), "status": metadata.get("status"),
                             "claim_sha256": metadata.get("claim_sha256"),
                             "source_statement_sha256": metadata.get("source_statement_sha256")}
                    if external_metadata is not None:
                        skill["external_source"] = external_metadata
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
