"""Run-scoped, evidence-linked lesson publishing for externally driven runs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import orjson
from fastapi import HTTPException

from looplab.adapters.tasks import load_task
from looplab.core.atomicio import append_jsonl_bytes_locked
from looplab.core.models import NodeStatus
from looplab.engine.claims_health import _valid_claim_source_row
from looplab.engine.lesson_hygiene import distilled_claim_stance, lesson_id
from looplab.engine.lessons_reconcile import LessonReconcileMixin
from looplab.engine.memory import task_fingerprint, unreliable_metric_ids
from looplab.events.eventstore import EventStore, EventStoreLockError, interprocess_lock, read_jsonl_lenient
from looplab.events.replay import fold
from looplab.serve.run_commands import run_generation_token


def publish_lesson(srv, rd: Path, body) -> dict:
    """Append exactly one lesson to the run's own shared store, with a stable retry key.

    Run sequencing fences reset/delete and serializes the state observation; the shared
    lessons lock makes duplicate detection and append one transaction across UI workers.
    The external agent supplies the conclusion, never the evidence or scope metadata.
    """
    try:
        with srv.commands.sequence(rd):
            settings = json.loads((rd / "config.snapshot.json").read_bytes())
            if not settings.get("external_harness"):
                raise HTTPException(409, "lessons are externally authored only in external harness runs")
            memory_dir = settings.get("memory_dir")
            if not isinstance(memory_dir, str) or not memory_dir:
                raise HTTPException(400, "this run has no cross-run memory_dir")
            events = EventStore(rd / "events.jsonl").read_all()
            generation = run_generation_token(events)
            if not generation or generation != body.expected_generation.lower():
                raise HTTPException(409, {"code": "run_generation_changed",
                                          "current_generation": generation})
            state = fold(events)
            if not state.run_id:
                raise HTTPException(409, "run has not started")
            evidence = sorted(set(body.evidence))
            if not evidence:
                raise HTTPException(400, "a lesson requires measured or failed node evidence")
            for nid in evidence:
                node = state.nodes.get(nid)
                if (node is None or node.status not in (NodeStatus.evaluated, NodeStatus.failed)
                        or node.tombstoned
                        or nid in (state.aborted_nodes or [])):
                    raise HTTPException(409, {"code": "lesson_evidence_not_terminal", "node_id": nid})
            if body.outcome == "supported" and any(
                    nid in unreliable_metric_ids(state) for nid in evidence):
                raise HTTPException(409, "supported lessons cannot cite an unreliable metric")

            task = load_task(rd / "task.snapshot.json", existing_run=True)
            best = state.best()
            fp = task_fingerprint(
                str(getattr(task, "kind", "") or ""), state.direction, state.goal,
                metric=str(getattr(task, "metric", "") or ""),
                param_names=list((best.idea.params or {}).keys()) if best and best.idea else [],
                universal=bool(settings.get("fingerprint_universal", False)))
            statement = body.statement.strip()
            payload = {"statement": statement, "outcome": body.outcome,
                       "role": body.role, "evidence": evidence,
                       "confidence": body.confidence,
                       "expected_generation": generation}
            digest = hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
            row = {"task_id": state.task_id, "fingerprint": fp,
                   "kind": str(getattr(task, "kind", "") or ""), "statement": statement,
                   "outcome": body.outcome,
                   "claim_stance": distilled_claim_stance(body.outcome),
                   "confidence": body.confidence, "direction": state.direction,
                   "run_id": state.run_id, "evidence": evidence,
                   "evidence_sig": {str(nid): LessonReconcileMixin._node_sig(
                       state.nodes[nid]) for nid in evidence},
                   "operators": sorted({str(state.nodes[nid].operator) for nid in evidence})[:8],
                   "harness_action_id": body.action_id,
                   "harness_payload_sha256": digest}
            if state.run_uid:
                row["run_uid"] = state.run_uid
            if body.role == "shared":
                row.pop("role")
            if not _valid_claim_source_row(row, research=False):
                raise HTTPException(400, "lesson does not meet the cross-run source contract")

            path = Path(memory_dir) / "lessons.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with interprocess_lock(Path(str(path) + ".lock"), required=True):
                # A replay lookup must inspect the complete store. Refuse a pathological
                # unbounded scan instead of searching a tail and risking duplicate writes.
                if path.exists() and path.stat().st_size > 64 * 1024 * 1024:
                    raise HTTPException(503, {"code": "lesson_store_too_large"})
                for old in read_jsonl_lenient(path):
                    if (old.get("harness_action_id") == body.action_id
                            and old.get("run_uid", old.get("run_id")) == (state.run_uid or state.run_id)):
                        if old.get("harness_payload_sha256") != digest:
                            raise HTTPException(409, "lesson action_id was reused with different content")
                        return {"ok": True, "replayed": True, "lesson_id": lesson_id(old),
                                "lesson": old}
                append_jsonl_bytes_locked(path, orjson.dumps(row))
            return {"ok": True, "replayed": False, "lesson_id": lesson_id(row), "lesson": row}
    except HTTPException:
        raise
    except (OSError, EventStoreLockError, ValueError, KeyError) as exc:
        raise HTTPException(503, {"code": "lesson_store_unavailable"}) from exc
