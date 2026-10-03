"""Credential-free operator handoff for an already launched external run.

Paths belong to the server host, not necessarily the coding client's machine.
Only whitelisted task constraints leave this read; no settings/environment dump,
credential, launch command or automatic client configuration is produced.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from looplab.adapters.tasks import load_task
from looplab.harness.snapshot_settings import read_harness_settings
from looplab.core.redact import redact_persisted_identity, redact_persisted_text
from looplab.engine.run_lifecycle import engine_liveness
from looplab.events.eventstore import EventStore, log_integrity
from looplab.events.replay import fold
from looplab.events.run_generation import run_generation_token
from looplab.harness.obligations import run_obligations


def snapshot(rd: Path, expected_generation: str, *, credential_configured: bool) -> dict:
    integrity = log_integrity(rd / "events.jsonl")
    if integrity.get("unreadable"):
        raise OSError("event history unreadable")
    events = EventStore(rd / "events.jsonl").read_all()
    generation = run_generation_token(events)
    if not generation or generation != expected_generation.lower():
        raise HTTPException(409, "run generation changed")
    if not integrity["complete"]:
        raise HTTPException(409, "run event history is incomplete; inspect source health first")
    try:
        settings = read_harness_settings(rd)
        task = load_task(rd / "task.snapshot.json", existing_run=True)
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(503, "run task or settings snapshot is unavailable") from exc
    if not settings.external_harness:
        raise HTTPException(409, "this run uses the built-in agent cycle")
    state = fold(events)
    constraints = run_obligations(task, settings, generation=generation)["enforced_on_candidate"]
    spec = task.repo_spec() if callable(getattr(task, "repo_spec", None)) else None

    def text(value):
        return redact_persisted_text(value, max_chars=2000, entropy=False, single_line=True)

    def names(values):
        values = values or []
        return {"items": [text(value) for value in values[:40]], "total": len(values),
                "truncated": len(values) > 40}

    return {
        "version": 1, "generation": generation,
        "run_id": redact_persisted_identity(rd.name, max_chars=256),
        "run_uid": redact_persisted_identity(state.run_uid, max_chars=256),
        "event_seq": events[-1].seq, "mode": "external_harness",
        "server_paths": {"run_root": text(str(rd.parent.resolve())),
                         "run_dir": text(str(rd.resolve())),
                         "meaning": "Paths on the UI/engine host; a remote coding client may not have these paths."},
        "engine_running": engine_liveness(rd),
        "agent_connection": "not_measured",
        "credential_configured": bool(credential_configured),
        "workspace": {
            "kind": "repository" if spec else "script",
            "source_paths": names([ed["path"] for ed in (spec or {}).get("editables", [])]),
            "edit_surface": names(constraints["edit_surface"]),
            "protected_names": names(constraints["protected_names"]),
            "operator_stages": names(constraints["operator_stages"]),
            "meaning": "Submit ready-made code/files through inject_node; LoopLab materializes node workspaces, validates the patch and owns evaluation."},
        "credential_policy": "Use only a distinct LOOPLAB_HARNESS_TOKEN in the MCP process; remove LOOPLAB_UI_TOKEN from its environment. Pass the secret separately through the client's protected credential mechanism.",
        "scope": "The harness token controls launched external runs on this server and has limited controls on internal runs; it is not restricted to this run. Prefer a separate server/run root for external work.",
        "recovery": "Reconnect to this same run. Read current /state generation and command receipts before resubmitting; inspect checkpoints even after evaluator completion. Explicitly pause or finalize when done. No internal agent takeover is automatic.",
    }
