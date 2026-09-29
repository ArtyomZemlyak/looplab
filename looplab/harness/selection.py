"""External agent's evidence-bound tie verification and MCTS branch estimates."""
from __future__ import annotations

import hashlib
from pathlib import Path

import orjson
from fastapi import HTTPException

from looplab.core.config import read_config_snapshot
from looplab.core.fitness import VERIFIER_SELECTION_CONTRACT, verifier_evidence_digest
from looplab.events.eventstore import (EventStore, EventStoreConcurrencyError,
                                       EventStoreLockError)
from looplab.events.replay import fold, verifier_tie_groups
from looplab.events.types import EV_NODE_VALUE_ESTIMATED, EV_VERIFIER_GROUP_SCORED
from looplab.engine.value_estimate import VALUE_ESTIMATE_CADENCE_CAP, VALUE_ESTIMATE_RATIONALE_CAP
from looplab.harness.obligations import evidence_revision
from looplab.serve.run_commands import run_generation_token


def _groups(state) -> list[list]:
    if (not state.select_verifier_tiebreak
            or state.select_verifier_contract != VERIFIER_SELECTION_CONTRACT):
        return []
    return [group for group in verifier_tie_groups(state) if len(group) >= 2]


def verification_due(settings, state) -> bool:
    return bool(settings.external_harness and settings.select_verifier and _groups(state))


def _value_weight(settings, state) -> float:
    strategy = state.active_strategy or {}
    policy = strategy.get("policy", settings.policy)
    params = strategy.get("policy_params") or {}
    if policy != "mcts" or not isinstance(params, dict):
        return 0.0
    return float(params.get("value_weight", settings.mcts_value_weight) or 0.0)


def _value_candidates(settings, state) -> list:
    if not settings.external_harness or _value_weight(settings, state) <= 0:
        return []
    pool = state.breedable_nodes()
    return sorted((node for node in pool if node.value_prior is None),
                  key=lambda node: node.id) if len(pool) >= 2 else []


def value_due(settings, state) -> bool:
    return bool(_value_candidates(settings, state))


def status(rd: Path, expected_generation: str) -> dict:
    events = EventStore(rd / "events.jsonl").read_all()
    if run_generation_token(events) != expected_generation.lower():
        raise HTTPException(409, "run generation changed")
    settings = read_config_snapshot(rd / "config.snapshot.json")
    state = fold(events)
    return {"selection_contract": state.select_verifier_contract,
            "requested_samples": state.select_verifier_samples,
            "tie_groups": [[{"node_id": node.id, "generation": node.attempt,
                             "metric": node.robust_metric,
                             "evidence_digest": verifier_evidence_digest(state.direction, node)}
                            for node in group] for group in _groups(state)]
            if settings.external_harness else [],
            "value_weight": _value_weight(settings, state),
            "evidence_revision": evidence_revision(state),
            "value_candidates": [{"node_id": n.id, "generation": n.attempt,
                                  "metric": n.robust_metric}
                                 for n in _value_candidates(settings, state)[:VALUE_ESTIMATE_CADENCE_CAP]]}


def _request_hash(rows) -> str:
    return hashlib.sha256(orjson.dumps(rows, option=orjson.OPT_SORT_KEYS)).hexdigest()


def verify_group(srv, rd: Path, body) -> dict:
    try:
        with srv.commands.sequence(rd):
            settings = read_config_snapshot(rd / "config.snapshot.json")
            store = EventStore(rd / "events.jsonl")
            events = store.read_all()
            if run_generation_token(events) != body.expected_generation.lower():
                raise HTTPException(409, "run generation changed")
            state = fold(events)
            request = [row.model_dump() for row in body.members]
            digest = _request_hash(request)
            older = next((e for e in events if e.type == EV_VERIFIER_GROUP_SCORED
                          and e.data.get("action_id") == body.action_id), None)
            if older is not None:
                if older.data.get("request_sha256") != digest:
                    raise HTTPException(409, "verifier action_id reused differently")
                return {"ok": True, "replayed": True, "group": older.data}
            if not verification_due(settings, state):
                raise HTTPException(409, "no selection tie needs external verification")
            groups = _groups(state)
            ids = {row.node_id for row in body.members}
            group = next((g for g in groups if {node.id for node in g} == ids), None)
            if group is None or len(ids) != len(body.members):
                raise HTTPException(409, "submit the complete current selector tie")
            nodes = {node.id: node for node in group}
            members = []
            for row in body.members:
                node = nodes[row.node_id]
                if (row.generation != node.attempt or row.evidence_digest !=
                        verifier_evidence_digest(state.direction, node)):
                    raise HTTPException(409, "verifier node evidence changed")
                if len(row.samples) != state.select_verifier_samples:
                    raise HTTPException(400, "provide the configured number of result-sound samples")
                yes = sum(row.samples)
                agreement = max(yes, len(row.samples) - yes) / len(row.samples)
                if agreement <= 0.5:
                    raise HTTPException(400, "inconclusive verifier samples lack a strict majority")
                members.append({"node_id": node.id, "generation": node.attempt,
                                "score": round(yes / len(row.samples), 4),
                                "n_samples": len(row.samples), "agreement": agreement,
                                "method": "external_agent",
                                "evidence_digest": row.evidence_digest})
            payload = {"v": 1, "contract": VERIFIER_SELECTION_CONTRACT,
                       "requested_samples": state.select_verifier_samples,
                       "members": members, "action_id": body.action_id,
                       "request_sha256": digest}
            store.append(EV_VERIFIER_GROUP_SCORED, payload,
                         expected_last_seq=events[-1].seq, require_lock=True)
            return {"ok": True, "replayed": False, "group": payload}
    except HTTPException:
        raise
    except EventStoreConcurrencyError as exc:
        raise HTTPException(409, "selection evidence changed; refresh") from exc
    except (OSError, EventStoreLockError, ValueError, KeyError) as exc:
        raise HTTPException(503, "selection verifier unavailable") from exc


def estimate_values(srv, rd: Path, body) -> dict:
    try:
        with srv.commands.sequence(rd):
            settings = read_config_snapshot(rd / "config.snapshot.json")
            store = EventStore(rd / "events.jsonl")
            events = store.read_all()
            if run_generation_token(events) != body.expected_generation.lower():
                raise HTTPException(409, "run generation changed")
            state = fold(events)
            request = [row.model_dump() for row in body.estimates]
            digest = _request_hash(request)
            older = next((e for e in events if e.type == EV_NODE_VALUE_ESTIMATED
                          and e.data.get("action_id") == body.action_id), None)
            if older is not None:
                if older.data.get("request_sha256") != digest:
                    raise HTTPException(409, "value action_id reused differently")
                return {"ok": True, "replayed": True}
            if body.expected_evidence_revision != evidence_revision(state):
                raise HTTPException(409, "measured evidence changed; refresh")
            candidates = _value_candidates(settings, state)
            required = {node.id: node for node in candidates[:VALUE_ESTIMATE_CADENCE_CAP]}
            if not required or {row.node_id for row in body.estimates} != set(required) or (
                    len(body.estimates) != len(required)):
                raise HTTPException(409, "submit estimates for the complete current candidate batch")
            records = []
            for row in body.estimates:
                node = required[row.node_id]
                if row.generation != node.attempt:
                    raise HTTPException(409, "value estimate node lifecycle changed")
                records.append((EV_NODE_VALUE_ESTIMATED, {
                    "node_id": node.id, "generation": node.attempt,
                    "value": round(row.value, 4), "rationale": row.rationale[:VALUE_ESTIMATE_RATIONALE_CAP],
                    "action_id": body.action_id, "request_sha256": digest}))
            store.append_many(records, expected_last_seq=events[-1].seq, require_lock=True)
            return {"ok": True, "replayed": False, "count": len(records)}
    except HTTPException:
        raise
    except EventStoreConcurrencyError as exc:
        raise HTTPException(409, "value evidence changed; refresh") from exc
    except (OSError, EventStoreLockError, ValueError, KeyError) as exc:
        raise HTTPException(503, "value estimate unavailable") from exc
