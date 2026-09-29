"""Run-scoped completion reviews for enabled cross-run agent cycles."""
from __future__ import annotations

from pathlib import Path

import orjson
from fastapi import HTTPException

from looplab.core.atomicio import append_jsonl_bytes_locked
from looplab.events.eventstore import EventStore, EventStoreLockError, interprocess_lock, read_jsonl_lenient
from looplab.events.replay import fold
from looplab.harness.obligations import evidence_revision
from looplab.serve.run_commands import run_generation_token


def required_reviews(settings) -> dict[str, tuple[str, ...]]:
    """Settings that make a knowledge review due before finalizing a nonempty run."""
    if not settings.external_harness:
        return {}
    rules = {}
    if settings.cross_run_curation and settings.memory_dir:
        rules["concept_merge"] = ("cross_run_curation",)
        rules["claim_curation"] = ("cross_run_curation",)
        if settings.task_facets_finalize:
            rules["task_facets"] = ("cross_run_curation", "task_facets_finalize")
    if settings.concept_tidy and settings.memory_dir:
        rules["concept_ratification"] = ("concept_tidy",)
    if settings.reflection_priors and settings.memory_dir:
        rules["lessons"] = ("reflection_priors",)
        rules["skill_candidates"] = ("reflection_priors",)
    return rules


def review_file(rd: Path) -> Path:
    return rd / "harness_reviews.jsonl"


_ACTION_STORES = {
    "concept_merge": ("concept_aliases.jsonl", "concept_splits.jsonl"),
    "concept_ratification": ("concept_aliases.jsonl",),
    "claim_curation": ("claim_decisions.jsonl",),
    "task_facets": ("task_facets.jsonl",),
    "lessons": ("lessons.jsonl",),
    "skill_candidates": ("skill_candidate_actions.jsonl",),
}


def _action_recorded(memory_dir: str, phase_id: str, action_ref: str, run_uid: str) -> bool:
    for name in _ACTION_STORES[phase_id]:
        path = Path(memory_dir) / name
        if not path.exists() or path.stat().st_size > 64 * 1024 * 1024:
            continue
        for row in read_jsonl_lenient(path):
            ref = row.get("harness_action_id" if phase_id == "lessons" else "action_id")
            if ref != action_ref:
                continue
            if phase_id in ("lessons", "skill_candidates") and row.get("run_uid") != run_uid:
                continue
            return True
    return False


def missing_reviews(rd: Path, settings, state, generation: str) -> list[str]:
    required = required_reviews(settings)
    if not required or not state.nodes:
        return []
    path = review_file(rd)
    if path.exists() and path.stat().st_size > 16 * 1024 * 1024:
        raise HTTPException(503, "external review ledger exceeds its bound")
    rows = read_jsonl_lenient(path)
    revision = evidence_revision(state)
    return [phase for phase in required if not any(
        row.get("run_uid") == state.run_uid and row.get("generation") == generation
        and row.get("at_node") == len(state.nodes) and row.get("phase_id") == phase
        and row.get("evidence_revision") == revision
        for row in rows)]


def cadence_reviews_due(rd: Path, settings, state, generation: str) -> list[str]:
    """A configured mid-run lesson/skill window must be decided before expansion.

    The receipt may say no action applies, but it must bind the measured evidence
    of the current window. Finalization uses the same review ledger.
    """
    n = len(state.nodes)
    if (not settings.external_harness or not settings.reflection_priors
            or not settings.memory_dir or settings.lessons_every <= 0
            or n == 0 or n % settings.lessons_every):
        return []
    due = set(missing_reviews(rd, settings, state, generation))
    phases = {"skill_candidates"}
    if settings.comparative_lessons:
        phases.add("lessons")
    return sorted(due & phases)


def publish_review(srv, rd: Path, body) -> dict:
    from looplab.core.config import read_config_snapshot

    try:
        with srv.commands.sequence(rd):
            settings = read_config_snapshot(rd / "config.snapshot.json")
            if body.phase_id not in required_reviews(settings):
                raise HTTPException(409, "this review is not enabled by the run settings")
            events = EventStore(rd / "events.jsonl").read_all()
            generation = run_generation_token(events)
            if not generation or generation != body.expected_generation.lower():
                raise HTTPException(409, {"code": "run_generation_changed",
                                          "current_generation": generation})
            state = fold(events)
            if not state.run_uid:
                raise HTTPException(409, "run has no durable identity")
            if body.decision == "completed" and not body.action_ref:
                raise HTTPException(400, "completed review requires the recorded action's reference")
            if body.decision == "completed" and not body.evidence:
                raise HTTPException(400, "completed review requires current run node evidence")
            if body.decision == "completed" and not _action_recorded(
                    str(settings.memory_dir), body.phase_id, body.action_ref, state.run_uid):
                raise HTTPException(409, "completed review does not cite a recorded domain action")
            for nid in body.evidence:
                node = state.nodes.get(nid)
                if node is None or node.tombstoned or nid in (state.aborted_nodes or []):
                    raise HTTPException(409, "review evidence node is missing or superseded")
            row = {"run_uid": state.run_uid, "generation": generation,
                   "at_node": len(state.nodes), "phase_id": body.phase_id,
                   "evidence_revision": evidence_revision(state),
                   "decision": body.decision, "reason": body.reason,
                   "action_id": body.action_id, "evidence": body.evidence,
                   "action_ref": body.action_ref}
            payload = orjson.dumps(row, option=orjson.OPT_SORT_KEYS)
            path = review_file(rd)
            with interprocess_lock(Path(str(path) + ".lock"), required=True):
                if path.exists() and path.stat().st_size > 16 * 1024 * 1024:
                    raise HTTPException(503, "external review ledger exceeds its bound")
                for old in read_jsonl_lenient(path):
                    if old.get("run_uid") == state.run_uid and old.get("action_id") == body.action_id:
                        if orjson.dumps(old, option=orjson.OPT_SORT_KEYS) != payload:
                            raise HTTPException(409, "review action_id was reused with different content")
                        return {"ok": True, "replayed": True, "review": old}
                append_jsonl_bytes_locked(path, payload)
            return {"ok": True, "replayed": False, "review": row}
    except HTTPException:
        raise
    except (OSError, EventStoreLockError, ValueError, KeyError) as exc:
        raise HTTPException(503, "review ledger unavailable") from exc
