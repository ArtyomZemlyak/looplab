"""Durable, operator-authored task-family guidance with an exact applicability receipt."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import orjson
from fastapi import HTTPException
from looplab.adapters.tasks import load_task
from looplab.engine.memory import task_fingerprint
from looplab.events.eventstore import interprocess_lock
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.journals import read_event_source, read_knowledge_source


def scope_fingerprint(task) -> list[str]:
    return task_fingerprint(getattr(task, "kind", ""), getattr(task, "direction", ""),
                            getattr(task, "goal", ""), metric=str(getattr(task, "metric", "") or ""),
                            universal=True)


def valid_operator_note(row) -> bool:
    return (isinstance(row, dict) and row.get("record_kind") == "operator_note"
            and isinstance(row.get("note_id"), str) and row["note_id"]
            and isinstance(row.get("note"), str) and row["note"]
            and isinstance(row.get("task_family"), str) and row["task_family"]
            and row.get("direction") in ("min", "max")
            and isinstance(row.get("fingerprint"), list)
            and all(isinstance(token, str) and token for token in row["fingerprint"]))


def publish_operator_note(srv, rd: Path, body: OperatorNoteBody) -> dict:
    family = body.task_family.strip()
    text = body.note.strip()
    if not family or not text:
        raise HTTPException(400, "task_family and note must be non-empty")
    digest = hashlib.sha256(orjson.dumps({"task_family": family, "note": text},
                                        option=orjson.OPT_SORT_KEYS)).hexdigest()
    with srv.commands.sequence(rd):
        settings = json.loads((rd / "config.snapshot.json").read_bytes())
        memory_dir = settings.get("memory_dir")
        if not isinstance(memory_dir, str) or not memory_dir:
            raise HTTPException(400, "this run has no cross-run memory_dir")
        events = read_event_source(rd)
        generation = run_generation_token(events)
        if not generation or generation != body.expected_generation.lower():
            raise HTTPException(409, {"code": "run_generation_changed",
                                      "current_generation": generation})
        state = fold(events)
        task = load_task(rd / "task.snapshot.json", existing_run=True)
        path = Path(memory_dir) / "operator_notes.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with interprocess_lock(Path(str(path) + ".lock"), required=True):
            if path.exists() and path.stat().st_size > 64 * 1024 * 1024:
                raise HTTPException(503, {"code": "operator_note_store_too_large"})
            for old in read_knowledge_source(path):
                if old.get("action_id") == body.action_id and old.get("run_uid") == state.run_uid:
                    if old.get("payload_sha256") != digest:
                        raise HTTPException(409, "operator-note action_id was reused with different content")
                    return {"ok": True, "replayed": True, "note": old}
            row = {
                "record_kind": "operator_note", "note_id": "on_" + digest[:24],
                "note": text, "task_family": family, "task_id": task.id,
                "kind": task.kind, "direction": task.direction,
                "fingerprint": scope_fingerprint(task), "goal": task.goal,
                "run_id": state.run_id, "run_uid": state.run_uid,
                "action_id": body.action_id, "payload_sha256": digest,
                "created_at": time.time(),
            }
            with path.open("ab") as stream:
                stream.write(orjson.dumps(row) + b"\n")
                stream.flush()
        return {"ok": True, "replayed": False, "note": row}
