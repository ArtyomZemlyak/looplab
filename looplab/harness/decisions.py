"""Durable, idea-bound decisions for enabled external pre-admission phases."""
from __future__ import annotations

import hashlib
from pathlib import Path

import orjson
from fastapi import HTTPException

from looplab.core.atomicio import append_jsonl_bytes_locked
from looplab.core.models import Idea, IdeaEmission, durable_idea_payload
from looplab.events.eventstore import EventStoreLockError, interprocess_lock
from looplab.events.replay import fold
from looplab.harness.obligations import evidence_revision
from looplab.harness.journals import read_event_source, read_receipts, same_receipt_request
from looplab.events.run_generation import run_generation_token


DECISION_PHASES = ("novelty", "foresight", "candidate_ranking", "strategy")


def idea_digest(idea: Idea) -> str:
    return hashlib.sha256(orjson.dumps(durable_idea_payload(idea),
                                      option=orjson.OPT_SORT_KEYS)).hexdigest()


def implementation_digest(idea: Idea, code, files, deleted) -> str:
    if (code is not None and not isinstance(code, str)
            or not isinstance(files, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in files.items())
            or not isinstance(deleted, list) or not all(isinstance(path, str) for path in deleted)):
        raise HTTPException(400, "invalid reviewed implementation")
    if not (code or files or deleted):
        raise HTTPException(400, "a reviewed implementation needs code or files")
    payload = {"idea": durable_idea_payload(idea), "code": code,
               "files": files, "deleted": deleted}
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()


def required_decisions(settings, node_count: int) -> dict[str, int]:
    """Minimum reviewed options; strategy is due only at its configured cadence."""
    if not settings.external_harness:
        return {}
    required = {}
    if settings.novelty_mode != "off" or settings.novelty_gate:
        required["novelty"] = 1
    if settings.foresight and settings.foresight_panel > 1:
        required["foresight"] = settings.foresight_panel
    if settings.best_of_n > 1:
        required["candidate_ranking"] = settings.best_of_n
    if node_count and node_count % settings.strategist_every == 0:
        required["strategy"] = 1
    return required


def decision_file(rd: Path) -> Path:
    return rd / "harness_decisions.jsonl"


def missing_decisions(rd: Path, settings, state, idea: Idea, generation: str,
                      *, code=None, files=None, deleted=None) -> list[str]:
    required = required_decisions(settings, len(state.nodes))
    if not required:
        return []
    path = decision_file(rd)
    rows = read_receipts(path, decisions=True)
    digest = idea_digest(idea)
    candidate_digest = (implementation_digest(idea, code, files or {}, deleted or [])
                        if "candidate_ranking" in required else None)
    revision = evidence_revision(state)
    return [phase for phase, minimum in required.items() if not any(
        row.get("run_uid") == state.run_uid and row.get("generation") == generation
        and row.get("at_node") == len(state.nodes) and row.get("phase_id") == phase
        and row.get("evidence_revision") == revision
        and row.get("idea_sha256") == digest and row.get("decision") == "submit"
        and (phase != "candidate_ranking" or row.get("candidate_sha256") == candidate_digest)
        and type(row.get("options_considered")) is int
        and row["options_considered"] >= minimum for row in rows)]


def _reviewed_idea(raw: dict) -> Idea:
    if (not isinstance(raw, dict) or set(raw) - set(Idea.model_fields)
            or not isinstance(raw.get("operator"), str)):
        raise HTTPException(400, "invalid decision idea")
    fields = {"concepts", "concepts_added", "concepts_removed"} & set(raw)
    try:
        if "concept_mode" in raw or fields:
            emission = {**raw}
            emission.setdefault("concept_mode", "full" if "concepts" in fields else "delta")
            idea = IdeaEmission.model_validate(emission).to_idea()
        else:
            idea = Idea.model_validate(raw)
    except ValueError as exc:
        raise HTTPException(400, "invalid decision idea") from exc
    if not idea.operator.strip():
        raise HTTPException(400, "idea.operator must be nonempty")
    return idea


def publish_decision(srv, rd: Path, body) -> dict:
    from looplab.core.config import read_config_snapshot

    try:
        with srv.commands.sequence(rd):
            settings = read_config_snapshot(rd / "config.snapshot.json")
            if not settings.external_harness:
                raise HTTPException(409, "decision receipts require external harness mode")
            events = read_event_source(rd)
            generation = run_generation_token(events)
            if not generation or generation != body.expected_generation.lower():
                raise HTTPException(409, {"code": "run_generation_changed",
                                          "current_generation": generation})
            state = fold(events)
            if not state.run_uid:
                raise HTTPException(409, "run has no durable identity")
            idea = _reviewed_idea(body.idea)
            digest = idea_digest(idea)
            options = {digest, *(idea_digest(_reviewed_idea(raw)) for raw in body.alternatives)}
            required = required_decisions(settings, len(state.nodes))
            minimum = required.get(body.phase_id, 1)
            selected_digest = None
            if body.phase_id == "candidate_ranking":
                hashes = []
                artifacts = []
                for raw in body.implementations:
                    if set(raw) - {"idea", "code", "files", "deleted"} or "idea" not in raw:
                        raise HTTPException(400, "invalid reviewed implementation fields")
                    hashes.append(implementation_digest(
                        _reviewed_idea(raw["idea"]), raw.get("code"),
                        raw.get("files") or {}, raw.get("deleted") or []))
                    artifacts.append(hashlib.sha256(orjson.dumps({
                        "code": raw.get("code"), "files": raw.get("files") or {},
                        "deleted": raw.get("deleted") or [],
                    }, option=orjson.OPT_SORT_KEYS)).hexdigest())
                if body.selected_index >= len(hashes) or len(set(artifacts)) < minimum:
                    raise HTTPException(400, f"candidate_ranking requires {minimum} distinct complete implementations")
                selected = body.implementations[body.selected_index]
                if idea_digest(_reviewed_idea(selected["idea"])) != digest:
                    raise HTTPException(400, "the selected implementation must match the reviewed Idea")
                selected_digest = hashes[body.selected_index]
                options = set(hashes)
            if body.decision == "submit" and len(options) < minimum:
                raise HTTPException(400, f"{body.phase_id} requires {minimum} distinct reviewed option(s)")
            row = {"run_uid": state.run_uid, "generation": generation,
                   "at_node": len(state.nodes), "phase_id": body.phase_id,
                   "evidence_revision": evidence_revision(state),
                   "idea_sha256": digest, "decision": body.decision,
                   "options_considered": len(options),
                   "option_sha256": sorted(options),
                   **({"candidate_sha256": selected_digest} if selected_digest else {}),
                   "reason": body.reason, "action_id": body.action_id}
            payload = orjson.dumps(row, option=orjson.OPT_SORT_KEYS)
            path = decision_file(rd)
            with interprocess_lock(Path(str(path) + ".lock"), required=True):
                for old in read_receipts(path, decisions=True):
                    if old.get("run_uid") == state.run_uid and old.get("action_id") == body.action_id:
                        if not same_receipt_request(old, row):
                            raise HTTPException(409, "decision action_id was reused with different content")
                        return {"ok": True, "replayed": True, "decision": old}
                append_jsonl_bytes_locked(path, payload)
            return {"ok": True, "replayed": False, "decision": row}
    except HTTPException:
        raise
    except (OSError, EventStoreLockError, ValueError, KeyError) as exc:
        raise HTTPException(503, "decision ledger unavailable") from exc
