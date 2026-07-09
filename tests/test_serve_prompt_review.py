"""Serve-side fixes from the agent-prompt mega-review (docs/PROMPT_REVIEW.md):
P10 — web genesis defaults backend=llm for a generative task (CLI parity with cli.py's genesis);
D7 — /nodes/{nid}/logs surfaces per-stage logs for OPERATOR `cmd.stages` pipelines (not just the
Developer's looplab_stages.json manifest); and the `_boss_context` advisory-vs-action split — the
action-less /chat channel is told to RECOMMEND, only the /command action-router is told to ACT.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402


# ---- P10: genesis backend=llm default for generative tasks (CLI parity) -------------------------
def _genesis_spec(monkeypatch, task: dict, settings: dict | None = None):
    """Drive /api/genesis with a scripted single-shot plan (the tool loop fails on a bare object()
    client, so the route falls back to parse_structured — the same pattern as the test_report.py
    genesis tests) and return the normalized spec card."""
    from looplab.serve.server import _GenesisSpec
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: object())
    monkeypatch.setattr("looplab.core.parse.parse_structured",
                        lambda *a, **k: _GenesisSpec(run_id="p10-run", task=task,
                                                     settings=dict(settings or {}),
                                                     reply="plan", rationale="test"))
    return None  # caller builds the client + posts (needs its own tmp_path root)


def test_genesis_defaults_backend_llm_for_generative_task(tmp_path, monkeypatch):
    """A composable (kind-less) repo task authored by web genesis must launch with backend=llm —
    Settings.backend defaults to "toy", which would give NoOpRepoDeveloper and every node silently
    re-evaluating the unchanged baseline (mega-review P10; cli.py's genesis already does this)."""
    task = {"goal": "maximize recall", "direction": "max", "repo": str(tmp_path / "repo"),
            "cmd": {"command": ["python", "test.py"],
                    "metric": {"reader": "stdout_json", "key": "recall"}}}
    _genesis_spec(monkeypatch, task)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/genesis", json={"instruction": "optimize my repo"}).json()
    assert r["ok"] is True
    assert r["spec"]["settings"]["backend"] == "llm"       # injected into the launch card's settings


def test_genesis_respects_explicit_backend(tmp_path, monkeypatch):
    """An explicit backend override in the boss's settings (or the prior draft) wins — the injection
    only fills the gap, mirroring cli.py's `backend_chosen` guard."""
    task = {"goal": "maximize recall", "direction": "max", "repo": str(tmp_path / "repo"),
            "cmd": {"command": ["python", "test.py"],
                    "metric": {"reader": "stdout_json", "key": "recall"}}}
    _genesis_spec(monkeypatch, task, settings={"backend": "cli_agent"})
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/genesis", json={"instruction": "optimize my repo"}).json()
    assert r["ok"] is True
    assert r["spec"]["settings"]["backend"] == "cli_agent"  # explicit choice respected verbatim


def test_genesis_non_generative_task_gets_no_backend(tmp_path, monkeypatch):
    """A non-generative (offline-optimizable) task keeps the default backend — no injection."""
    task = {"benchmark": "quadratic", "goal": "min (x-3)^2", "direction": "min"}
    _genesis_spec(monkeypatch, task)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/genesis", json={"instruction": "solve the toy"}).json()
    assert r["ok"] is True
    assert "backend" not in r["spec"]["settings"]           # quadratic stays on its default


# ---- D7: node_logs for OPERATOR cmd.stages pipelines --------------------------------------------
def test_node_logs_surfaces_operator_cmd_stages(tmp_path):
    """An operator-declared `cmd.stages` pipeline writes NO looplab_stages.json (the engine ignores
    the Developer manifest in that mode) — the stage names must come from the run's
    task.snapshot.json, in pipeline order, with no phantom `score` stage appended (the LAST operator
    stage carries the metric) and stray logs still excluded (mega-review D7)."""
    rd = tmp_path / "demo"
    rd.mkdir()
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "max"})
    s.append("node_building", {"node_id": 0, "operator": "draft", "parent_ids": []})
    # verbatim composable snapshot, as `run` writes it: cmd (not eval) carrying the stage pipeline
    (rd / "task.snapshot.json").write_text(json.dumps({
        "goal": "g", "direction": "max", "repo": str(tmp_path / "repo"),
        "cmd": {"stages": [{"name": "data_prep", "command": ["python", "prep.py"]},
                           {"name": "train", "command": ["python", "train.py"], "timeout": 7200},
                           {"name": "final_eval", "command": ["python", "eval.py"]}],
                "metric": {"reader": "stdout_json", "key": "recall"}}}), encoding="utf-8")
    nd = rd / "nodes" / "node_0"
    nd.mkdir(parents=True)
    (nd / "data_prep.log").write_text("wrote shards\n")
    (nd / "train.log").write_text("Epoch 1 loss=0.5\n")
    (nd / "final_eval.log").write_text("recall: 0.8\n")
    (nd / "debug.log").write_text("stray framework log\n")
    client = TestClient(make_app(tmp_path))
    body = client.get("/api/runs/demo/nodes/0/logs").json()
    assert list(body["stages"]) == ["data_prep", "train", "final_eval"]   # operator pipeline order
    assert "Epoch 1 loss=0.5" in body["stages"]["train"]
    assert "debug" not in body["stages"] and "score" not in body["stages"]
    assert body["eval"] == ""                       # multi-stage → no eval.log, no fallback dup


# ---- _boss_context: advisory wording on /chat, imperative on /command ---------------------------
def _stalled_run(root: Path, name: str = "demo") -> Path:
    """A run that is neither finished nor engine-alive → the STALLED status branch."""
    rd = root / name
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": name, "task_id": "t", "goal": "g", "direction": "min"})
    return rd


def test_chat_gets_advisory_stalled_wording(tmp_path, monkeypatch):
    """/chat has no actions channel: its RUN STATUS must recommend the operator act, not command the
    model to `resume` — the imperative invited hallucinated 'I'll resume it' replies (mega-review)."""
    _stalled_run(tmp_path)
    captured = {}

    class _Cap:
        def __init__(self, s): self.model = s.llm_model
        def complete_text(self, msgs): captured["sys"] = msgs[0]["content"]; return "ok"

    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: _Cap(s))
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/demo/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.json()["ok"]
    assert "STALLED" in captured["sys"]                     # the liveness fact still reaches the model
    assert "RECOMMEND the operator" in captured["sys"]      # …as advice
    assert "you MUST act" not in captured["sys"]            # never as an order it can't execute


def test_command_keeps_imperative_stalled_wording(tmp_path, monkeypatch):
    """/command IS the actions channel: the boss must still be ordered to act on a stalled run."""
    _stalled_run(tmp_path)
    captured = {}
    from looplab.serve.server import _Plan

    def _cap_parse(client, msgs, *a, **k):
        captured["sys"] = msgs[0]["content"]
        return _Plan(reply="on it")

    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s: object())
    monkeypatch.setattr("looplab.core.parse.parse_structured", _cap_parse)
    client = TestClient(make_app(tmp_path))
    r = client.post("/api/runs/demo/command", json={"instruction": "status?"}).json()
    assert r["ok"]
    assert "you MUST act" in captured["sys"]                # the action-router keeps the imperative


# ---- P9/P20: the boss/chat prompts teach the verbs the mapper actually accepts ------------------
def test_command_prompt_teaches_finalize_and_stop_pause_synonymy():
    """P9: the taught vocabulary must reach the wrap-up path (`finalize` → run_abort) and must not
    present stop/pause as distinct outcomes (both map to the same freeze event)."""
    from looplab.serve.serve_prompts import CHAT_SYSTEM, COMMAND_SYSTEM
    assert "finalize" in COMMAND_SYSTEM and "wraps it up" in COMMAND_SYSTEM
    assert "synonym" in COMMAND_SYSTEM                       # pause ≡ stop stated explicitly
    # P20: reset teaches pipeline stage names, not just the three lifecycle stages
    assert "eval-PIPELINE stage name" in COMMAND_SYSTEM
    # P37: the chat prompt's operator list includes merge (its sibling prompts already did)
    assert "improve/draft/debug/merge" in CHAT_SYSTEM
