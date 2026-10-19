"""A fresh external run asks the agent for the minimum (doc 82 П2).

`profile=agent-minimal` leaves only the checks the operator declared; a launch that names neither a
backend nor a profile for an external run gets `backend=toy` + `agent-minimal` at the LAUNCH points
(`core/config.py::external_launch_defaults`), never in the `Settings` validator, so every other
`Settings(external_harness=True)` — and every resumed snapshot — keeps today's obligations.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from looplab.adapters.tasks import load_task
from looplab.core.appconfig import build_settings
from looplab.core.config import PROFILES, Settings, external_launch_defaults, settings_from_snapshot
from looplab.harness.decisions import required_decisions
from looplab.harness.obligations import run_obligations
from looplab.serve.server import make_app

ROOT = Path(__file__).resolve().parents[1]
GEN = "a" * 64


def _required(settings: Settings) -> set[str]:
    task = load_task(ROOT / "examples" / "toy_task.json")
    rows = run_obligations(task, settings, generation=GEN)["phase_obligations"]
    return {name for name, row in rows.items() if row.get("required")}


def test_agent_minimal_leaves_only_the_operator_declared_stage_check(tmp_path):
    base = dict(backend="toy", external_harness=True, memory_dir=str(tmp_path / "memory"))
    assert len(_required(Settings(**base))) == 19          # the measured bare-default load
    assert _required(Settings(**base, profile="agent-minimal")) == {"stage_check"}


def test_an_explicit_field_beats_the_profile(tmp_path):
    settings = Settings(backend="toy", external_harness=True, profile="agent-minimal",
                        report_every=2, memory_dir=str(tmp_path / "memory"))
    assert settings.report_every == 2
    assert _required(settings) == {"stage_check", "report"}
    assert settings.train_monitor is False                 # the rest of the bundle still applies


def test_profile_fills_exactly_the_documented_bundle():
    filled = Settings(backend="toy", external_harness=True, profile="agent-minimal")
    for key, value in PROFILES["agent-minimal"].items():
        assert getattr(filled, key) == value, key
    assert filled.strategist_every == 0 and filled.deep_research_every == -1


@pytest.mark.parametrize("layers, env, expected", [
    ([{}, {}, {"external_harness": True}], set(), {"backend": "toy", "profile": "agent-minimal"}),
    ([{"backend": "llm"}, {}, {"external_harness": "true"}], set(),
     {"backend": "toy", "profile": "agent-minimal"}),            # a LOWER layer's llm is not a choice
    ([{}, {}, {"external_harness": True, "backend": "llm"}], set(), {"profile": "agent-minimal"}),
    ([{"external_harness": True}, {"backend": "toy"}, {}], set(), {"profile": "agent-minimal"}),
    ([{}, {"profile": "default"}, {"external_harness": True}], set(), {"backend": "toy"}),
    ([{}, {}, {"external_harness": True}], {"profile"}, {"backend": "toy"}),
    ([{"external_harness": True}, {}, {"external_harness": False}], set(), {}),
    ([{}, {}, {}], set(), {}),
])
def test_launch_defaults_truth_table(layers, env, expected):
    assert external_launch_defaults(layers, env_fields=env) == expected


def test_strategist_every_zero_is_off_and_negative_is_refused(tmp_path):
    settings = Settings(backend="toy", external_harness=True, strategist_every=0)
    assert all("strategy" not in required_decisions(settings, n) for n in range(0, 13))
    assert "strategy" in required_decisions(settings.model_copy(update={"strategist_every": 3}), 3)
    with pytest.raises(ValidationError):
        Settings(strategist_every=-1)
    from looplab.engine.cadence import cadence_due
    from tests.factories import make_engine
    eng = make_engine(tmp_path / "run", strategist_every=0)
    assert eng.strategist_every == 0                       # the knob no longer clamps 0 up to 1
    assert not any(cadence_due(n, 0, eng.strategist_every) for n in range(1, 30))


def test_an_old_external_snapshot_keeps_every_obligation_on_resume(tmp_path):
    """A pre-П2 external run's snapshot is a full dump with `profile=default`: neither the snapshot
    reader nor a fresh-run CLI build may re-fill it with the new defaults."""
    launched = Settings(backend="toy", external_harness=True, memory_dir=str(tmp_path / "memory"))
    before = _required(settings_from_snapshot(dict(launched.masked_snapshot())))
    # The launch document a spawned `looplab run` reads carries the same full dump, credentials
    # popped (`serve/launch.py::_resolved_settings`).
    document = {k: v for k, v in launched.model_dump(mode="json").items()
                if k not in ("llm_api_key", "llm_api_key_base_url")}
    assert _required(build_settings(document, {}, {}, fresh_run=True)) == before
    assert len(before) == 19


def test_cli_build_fills_a_fresh_external_run_but_not_an_existing_one():
    fresh = build_settings({}, {}, {"external_harness": True}, fresh_run=True)
    assert fresh.backend == "toy" and fresh.profile == "agent-minimal"
    with pytest.raises(ValidationError):           # an existing run is never re-filled
        build_settings({"backend": "llm"}, {}, {"external_harness": True}, fresh_run=False)


def _repo_task(tmp_path) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "test.py").write_text("print('{}')\n", encoding="utf-8")
    return {"goal": "maximize recall", "direction": "max", "repo": str(repo),
            "cmd": {"command": ["python", "test.py"],
                    "metric": {"reader": "stdout_json", "key": "recall"}}}


def test_preflight_of_a_generative_external_launch_gets_toy_and_agent_minimal(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOPLAB_BACKEND", raising=False)
    monkeypatch.delenv("LOOPLAB_PROFILE", raising=False)
    client = TestClient(make_app(tmp_path))
    task = _repo_task(tmp_path)
    ok = client.post("/api/start/preflight", json={
        "run_id": "ext", "task": task, "settings": {"external_harness": True}})
    assert ok.status_code == 200, ok.text
    settings = ok.json()["preview"]["settings"]
    assert settings["backend"] == "toy" and settings["profile"] == "agent-minimal"
    assert ok.json()["warnings"] == []                     # no backend=llm inference for it
    bad = client.post("/api/start/preflight", json={
        "run_id": "ext", "task": task, "settings": {"external_harness": True, "backend": "llm"}})
    assert bad.status_code == 422


def test_saved_ui_backend_llm_does_not_turn_the_external_switch_into_a_422(tmp_path, monkeypatch):
    client = TestClient(make_app(tmp_path))
    assert client.put("/api/settings", json={"backend": "llm"}).status_code == 200
    ok = client.post("/api/start/preflight", json={
        "run_id": "ext2", "task": _repo_task(tmp_path), "settings": {"external_harness": True}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["preview"]["settings"]["backend"] == "toy"


def test_genesis_card_for_an_external_run_carries_no_llm_hint(tmp_path, monkeypatch):
    from looplab.serve.server import _GenesisSpec
    task = _repo_task(tmp_path)
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda s, **_kw: object())
    monkeypatch.setattr("looplab.core.parse.parse_structured",
                        lambda *a, **k: _GenesisSpec(run_id="ext-card", task=task,
                                                     settings={"external_harness": True},
                                                     reply="plan", rationale="test"))
    client = TestClient(make_app(tmp_path))
    card = client.post("/api/genesis", json={"instruction": "optimize my repo"}).json()
    assert card["ok"] is True
    assert "backend" not in card["spec"]["settings"]


def test_fresh_agent_minimal_run_asks_first_for_an_experiment(tmp_path, monkeypatch):
    """Done when (doc 82 П2): the contract of a fresh external run with no explicit settings shows
    one conditional obligation, and nothing blocks the first candidate (no research memo first)."""
    from looplab.events.eventstore import EventStore
    from looplab.events.run_generation import run_generation_token
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    rd = tmp_path / "runs" / "demo"
    rd.mkdir(parents=True)
    settings = build_settings({}, {}, {"external_harness": True,
                                       "memory_dir": str(tmp_path / "memory")}, fresh_run=True)
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    (rd / "task.snapshot.json").write_text((ROOT / "examples" / "toy_task.json").read_text())
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "u1", "task_id": "task",
                                 "goal": "min", "direction": "min"})
    generation = run_generation_token(store.read_all())
    client = TestClient(make_app(rd.parent))
    contract = client.get("/api/runs/demo/harness-contract").json()
    required = {k for k, v in contract["phase_obligations"].items() if v.get("required")}
    assert required == {"stage_check"}
    progress = client.get("/api/runs/demo/harness-progress",
                          params={"expected_generation": generation, "brief": "true"}).json()
    assert progress["candidate_blockers_if_expanding"] == []
