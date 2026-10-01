"""Owner discovery of external mode and unanswered evaluator questions.

Checkpoint writes do not advance the event log. Cache identities therefore name
the config and checkpoint journal too; all items use the same incarnation and
evaluator-claim projection as harness progress. No candidate or recovery is sent.
"""
from pathlib import Path

from looplab.core.atomicio import file_identity
from looplab.core.config import read_config_snapshot
from looplab.core.errors import ConfigRefusal
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.checkpoint_history import project_checkpoints
from looplab.harness.journals import read_source
from looplab.serve.attention import _opaque_id, _timestamp


def optional_identity(path: Path):
    try:
        return file_identity(path.stat())
    except FileNotFoundError:
        return None


def external_mode(rd: Path) -> bool | None:
    try:
        return bool(read_config_snapshot(rd / "config.snapshot.json").external_harness)
    except (OSError, ConfigRefusal):
        return None


def attention_source_identity(rd: Path):
    return (file_identity((rd / "events.jsonl").stat()),
            optional_identity(rd / "config.snapshot.json"),
            optional_identity(rd / "harness_checkpoints.jsonl"))


def external_checkpoint_attention(run_id: str, rd: Path, events) -> list[dict]:
    mode = external_mode(rd)
    if mode is None and (rd / "config.snapshot.json").exists():
        raise ValueError("run mode could not be read")
    if mode is not True:
        return []
    rows, health = read_source(rd / "harness_checkpoints.jsonl")
    state = fold(events)
    generation = run_generation_token(events)
    history = project_checkpoints(rows, health, events, state, generation)
    if not health["read_complete"]:
        raise ValueError("external checkpoint history is incomplete")
    items = []
    for row in history:
        if row["status"] != "pending":
            continue
        question = row["question"]
        seq = question["claim_seq"]
        if seq < 0:
            seq = next((event.seq for event in reversed(events)
                        if event.type == "node_created"
                        and event.data.get("node_id") == question["node_id"]
                        and event.data.get("generation", 0) == question["node_generation"]), 0)
        items.append({
            "id": _opaque_id(run_id, generation, question["node_id"],
                             "external_checkpoint:" + question["checkpoint_id"]),
            "kind": "external_checkpoint", "severity": "action",
            "title": "External agent answer needed",
            "detail": "Read the current evaluation question in Agent cycle before responding.",
            "run_id": run_id, "generation": generation, "seq": seq,
            "created": _timestamp(question.get("created_at", 0)),
            "browser": False, "active": True, "derived": False,
            "node_id": question["node_id"], "node_generation": question["node_generation"],
        })
    return items
