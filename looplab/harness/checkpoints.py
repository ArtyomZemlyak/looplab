"""Durable, lifecycle-fenced questions asked of the external agent during an eval.

The engine never calls a model here. A stage cannot advance until its question is
answered; a live observer can ask while the subprocess is running. The append-only
ledger survives server and engine restarts, and answers are bound to a run incarnation
and the exact node lifecycle. The engine does not trust an answer about a different
checkpoint or a completed/reset node.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import orjson
from fastapi import HTTPException

from looplab.core.atomicio import append_jsonl_bytes_locked
from looplab.core.jsonlio import read_jsonl_lenient_with_health
from looplab.events.eventstore import (EventStore, EventStoreLockError, interprocess_lock,
                                       log_integrity)
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.checkpoint_history import (CHECKPOINT_PHASES as _PHASES,
                                               allowed_verdicts, checkpoint_records, project_checkpoints)

_MAX_LEDGER = 16 * 1024 * 1024


class CheckpointSubjectGone(ValueError):
    """`ask` refused because the fold PROVES the asked lifecycle moved: the node's generation is
    past the asked one, or it is the asked one and no longer pending.

    No answer could ever be matched to such a question (`pending` lists only pending lifecycles),
    and the state does not heal: a lifecycle leaves `pending` only through a terminal or a reset,
    and a reset bumps the generation. One caller meets it on purpose — a confirm RE-MEASUREMENT of
    an evaluated node runs the same stage chain (in external mode the operator's `force_confirm`;
    the noise floor has the same shape but never runs there) — and one by accident, an attempt a
    reset or abort abandoned mid-eval. A caller that retried it would spin until cancelled, and a
    re-measurement passes no cancel (critic 2026-09-29, driven): so it is a TYPE, apart from the
    transient errors a caller does retry. A node the read does not show, or shows at an EARLIER
    generation, proves nothing moved — the read may simply be behind — and stays a plain,
    retryable `ValueError`."""


def _claim_seq(events, node_id: int, node_generation: int) -> int:
    """The latest evaluator invocation, even after a crash/reclaim of the same ID."""
    return max((event.seq for event in events if event.type == "eval_invocation_claimed"
                and event.data.get("node_id") == node_id
                and event.data.get("generation") == node_generation), default=-1)


def _path(rd: Path) -> Path:
    return Path(rd) / "harness_checkpoints.jsonl"


def _rows(path: Path) -> list[dict]:
    if path.exists() and path.stat().st_size > _MAX_LEDGER:
        raise OSError("external checkpoint ledger exceeds its review bound")
    rows, health = read_jsonl_lenient_with_health(path)
    checkpoint_records(rows, health)
    if not health["read_complete"]:
        raise OSError("external checkpoint journal is incomplete; repair the source before responding")
    return rows


def _projection(rows: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    health = {"read_complete": True}
    records = checkpoint_records(rows, health)
    if not health["read_complete"]:
        raise OSError("external checkpoint records are incomplete")
    return records


def _require_event_source(rd: Path):
    path = Path(rd) / "events.jsonl"
    if not log_integrity(path)["complete"]:
        raise OSError("external checkpoint event history is incomplete")
    # Display readers may call a missing/empty log complete. An existing checkpoint
    # cannot be resolved without its run's event source.
    if path.stat().st_size == 0:
        raise OSError("external checkpoint event history is absent")


def _events(rd: Path):
    _require_event_source(rd)
    events = EventStore(Path(rd) / "events.jsonl").read_all()
    # Detect a damaged append during the read; an ordinary valid append is harmless.
    _require_event_source(rd)
    return events


def ask(rd: Path, node_id: int, node_generation: int, phase_id: str,
        *, stage: str = "", expectation: str = "", observation: str = "",
        kill_enabled: bool = False) -> dict:
    """Open one question; the engine calls this in the eval worker or observer."""
    if phase_id not in _PHASES:
        raise ValueError("unknown external checkpoint phase")
    events = _events(rd)
    state = fold(events)
    node = state.nodes.get(node_id)
    if node is None or node.attempt < node_generation:
        # Not proof of a move: a read that does not yet show the lifecycle the evaluator runs.
        raise ValueError("node lifecycle not visible to the checkpoint yet")
    if node.attempt != node_generation or node.status != "pending":
        raise CheckpointSubjectGone("node lifecycle changed before checkpoint")
    row = {"type": "question", "checkpoint_id": uuid.uuid4().hex,
           "run_uid": state.run_uid, "run_generation": run_generation_token(events),
           "node_id": node_id, "node_generation": node_generation,
           "claim_seq": _claim_seq(events, node_id, node_generation),
           "phase_id": phase_id, "stage": stage[:160],
           "expectation": expectation[:1000], "observation": observation[-6000:],
           "kill_enabled": bool(kill_enabled), "created_at": time.time()}
    path = _path(rd)
    with interprocess_lock(Path(str(path) + ".lock"), required=True):
        _rows(path)
        append_jsonl_bytes_locked(path, orjson.dumps(row))
    return row


def answer_for(rd: Path, checkpoint_id: str) -> dict | None:
    events = _events(rd)
    state = fold(events)
    history = project_checkpoints(_rows(_path(rd)), {"read_complete": True},
                                  events, state, run_generation_token(events))
    # Receipt replay proves prior acceptance. A worker needs authority for THIS
    # pending lifecycle and claim; a reset/reclaim must not inherit the verdict.
    for row in history:
        question = row["question"]
        if (question["checkpoint_id"] == checkpoint_id
                and row["lifecycle"] == "same_node_attempt"
                and state.nodes[question["node_id"]].status == "pending"):
            return row["answer"]
    return None


def pending(rd: Path, expected_generation: str) -> list[dict]:
    events = _events(rd)
    if run_generation_token(events) != expected_generation.lower():
        raise HTTPException(409, "run generation changed")
    state = fold(events)
    questions, answers = _projection(_rows(_path(rd)))
    return [q for key, q in questions.items() if key not in answers
            and q["run_generation"] == expected_generation.lower()
            and q["run_uid"] == state.run_uid
            and q.get("claim_seq") == _claim_seq(events, q["node_id"], q["node_generation"])
            and (node := state.nodes.get(q["node_id"])) is not None
            and node.attempt == q["node_generation"] and node.status == "pending"]


def respond(srv, rd: Path, body) -> dict:
    from looplab.core.config import read_config_snapshot
    from looplab.runtime.command_eval import STAGE_CHECK_HARD_KINDS

    try:
        with srv.commands.sequence(rd):
            if not read_config_snapshot(rd / "config.snapshot.json").external_harness:
                raise HTTPException(409, "checkpoints require external harness mode")
            events = _events(rd)
            generation = run_generation_token(events)
            if not generation or generation != body.expected_generation.lower():
                raise HTTPException(409, "run generation changed")
            state = fold(events)
            path = _path(rd)
            with interprocess_lock(Path(str(path) + ".lock"), required=True):
                questions, answers = _projection(_rows(path))
                q = questions.get(body.checkpoint_id)
                if q is None or q["run_generation"] != generation or q["run_uid"] != state.run_uid:
                    raise HTTPException(404, "checkpoint not found in this run incarnation")
                previous = answers.get(body.checkpoint_id)
                if previous:
                    if (previous["action_id"] != body.action_id
                            or previous["verdict"] != body.verdict
                            or previous["failure_kind"] != body.failure_kind
                            or previous["reason"] != body.reason):
                        raise HTTPException(409, "checkpoint already answered differently")
                    return {"ok": True, "replayed": True, "answer": previous}
                node = state.nodes.get(q["node_id"])
                if node is None or node.attempt != q["node_generation"] or node.status != "pending":
                    raise HTTPException(409, "checkpoint node lifecycle changed")
                if q.get("claim_seq") != _claim_seq(events, q["node_id"], q["node_generation"]):
                    raise HTTPException(409, "checkpoint belongs to a superseded evaluator attempt")
                if q["phase_id"] == "stage_check":
                    if body.verdict not in allowed_verdicts(q):
                        raise HTTPException(400, "stage check needs proceed, inconclusive or fail")
                    if body.verdict == "fail" and (body.failure_kind not in STAGE_CHECK_HARD_KINDS
                                                    or body.failure_kind == "unstructured"
                                                    or (body.failure_kind == "declared_condition_violated"
                                                        and not q["expectation"])):
                        raise HTTPException(400, "invalid stage failure kind")
                elif q["phase_id"] == "deadline_grace":
                    if body.verdict not in allowed_verdicts(q):
                        raise HTTPException(400, "deadline review needs extend or stop")
                elif body.verdict not in allowed_verdicts(q):
                    raise HTTPException(400, "this monitor is advisory; abort is disabled")
                if body.verdict != "fail" and body.failure_kind:
                    raise HTTPException(400, "failure_kind is only valid on fail")
                row = {"type": "answer", "checkpoint_id": body.checkpoint_id,
                       "verdict": body.verdict, "failure_kind": body.failure_kind,
                       "reason": body.reason, "action_id": body.action_id,
                       "answered_at": time.time()}
                append_jsonl_bytes_locked(path, orjson.dumps(row))
                return {"ok": True, "replayed": False, "answer": row}
    except HTTPException:
        raise
    except (OSError, EventStoreLockError, ValueError, KeyError) as exc:
        raise HTTPException(503, "checkpoint ledger unavailable") from exc
