"""Actionable allowed-field hints on fail-closed unknown-field refusals.

Motivated by live external-agent command forensics (16/46 command rejections on
`qwen-full2026-joint-query-sid-20261103` were unknown-field errors where the
server previously answered only `correct the command payload` without naming
the allowed keys).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.events.run_generation import run_generation_token
from looplab.serve.run_commands import RunCommandService
from looplab.serve.server import make_app


def _setup_run(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    monkeypatch.setenv("LOOPLAB_UI_ANONYMOUS", "1")
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.setenv("LOOPLAB_AGENT_EVENTS", "0")
    monkeypatch.setenv("LOOPLAB_UI_AUTO_RESUME", "0")

    runs_root = tmp_path / "runs"
    rd = runs_root / "r1"
    rd.mkdir(parents=True)
    settings = Settings(backend="toy", external_harness=True)
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")), encoding="utf-8")
    (rd / "task.snapshot.json").write_text(json.dumps({"kind": "quadratic"}), encoding="utf-8")
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r1", "run_uid": "u1", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft", "idea": {"operator": "draft"}, "code": "print(1)"})
    store.append("node_failed", {"node_id": 0, "error": "boom"})
    store.append("pause", {"reason": "operator_pause"})
    return runs_root, run_generation_token(store.read_all())


@pytest.mark.parametrize(
    ("cmd_type", "payload", "expected_substrings"),
    [
        (
            "report_generated",
            {"at_node": 1, "trigger": "manual", "headline": "Summary"},
            ["report_generated has unknown field(s): at_node, headline, trigger", "allowed: content", "nest report fields under data.content"],
        ),
        (
            "node_reset",
            {"node_id": 0, "generation": 0, "reason": "retry transient error"},
            ["node_reset has unknown field(s): reason", "allowed: from_stage, generation, node_id, resume_inputs"],
        ),
        (
            "budget_extend",
            {"add_nodes": 2, "reason": "extend campaign"},
            ["budget_extend has unknown field(s): reason", "allowed: add_nodes,"],
        ),
        (
            "resume",
            {"drain_only": True},
            ["resume has unknown field(s): drain_only", "allowed: reason"],
        ),
        (
            "hint",
            {"text": "Focus on calibration", "source": "codex"},
            ["hint has unknown field(s): source", "allowed: directive_id, replace, review_nomination, status, supersedes, text, topic"],
        ),
        (
            "research_completed",
            {"memo": {"summary": "Checked literature", "next_steps": ["try A"]}},
            ["research_completed.memo has unknown field(s): next_steps", "next_experiments"],
        ),
        (
            "run_reopened",
            {"reason": "reopen"},
            ["run_reopened has unknown field(s): reason", "takes no data fields"],
        ),
    ],
)
def test_unknown_field_command_rejections_name_allowed_fields_in_message_and_remediation(
    tmp_path, monkeypatch, cmd_type, payload, expected_substrings
):
    runs_root, gen = _setup_run(tmp_path, monkeypatch)
    app = make_app(runs_root)
    srv = app.state.looplab
    srv.commands = RunCommandService(
        srv, spawn_engine=lambda *a, **kw: None,
        startup_timeout=0.2, command_timeout=0.4, poll_interval=0.01, max_observation_timeout=0.8,
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/runs/r1/commands",
            headers={"Idempotency-Key": f"bad-{cmd_type}"},
            json={"expected_generation": gen, "type": cmd_type, "data": payload},
        )
        assert resp.status_code in (200, 400)
        body = resp.json()
        err = body.get("error") or body.get("detail")
        serialized = json.dumps(err)
        for sub in expected_substrings:
            assert sub in serialized, (sub, err)
        if isinstance(err, dict) and "remediation" in err:
            assert "allowed:" in err["remediation"] or "takes no data fields" in err["remediation"]
