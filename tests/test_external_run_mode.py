"""External ownership crosses engine, evaluation and shared command intake."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from looplab.core.config import Settings
from looplab.core.models import RunState
from looplab.harness.contract import candidate_surface_refusal
from looplab.events.replay import fold
from looplab.events.types import (EV_FORCE_ABLATE, EV_FORK, EV_INJECT_NODE,
                                  EV_NODE_RESET, EV_RUN_ABORT)
from looplab.serve.control_validation import normalize_control
from looplab.serve.server import make_app
from tests.factories import http_run_generation
from tests.factories import post_command, command_terminal
from tests.factories import make_engine


class NoInternalRole:
    is_code_generating = False

    def __getattr__(self, name):
        raise AssertionError(f"external run invoked internal role: {name}")


@pytest.mark.parametrize("external", [False, True])
def test_external_research_and_report_use_internal_projections_without_internal_llm(tmp_path, external):
    from looplab.events.eventstore import EventStore
    root = tmp_path / "runs"
    rd = root / "demo"
    rd.mkdir(parents=True)
    (rd / "config.snapshot.json").write_text(
        json.dumps(Settings(backend="toy" if external else "llm",
                            external_harness=external).model_dump(mode="json")))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "task", "goal": "g",
                                 "direction": "min"})
    client = TestClient(make_app(root))
    memo = {"summary": "A measured baseline suggests a simpler model.",
            "open_questions": ["Does regularization improve it?"],
            "claims": [{"statement": "Baseline is promising", "node_ids": [999]}]}
    result = post_command(client, "research_completed", {"memo": memo}, key="research-1")
    assert result.status_code == 200, result.text
    assert command_terminal(client, result.json())["status"] == "succeeded"
    assert result.json()["id"] == post_command(
        client, "research_completed", {"memo": memo}, key="research-1").json()["id"]
    content = {"headline": "The first batch is ready", "verdict": "More trials needed"}
    result = post_command(client, "report_generated", {"content": content}, key="report-1")
    assert result.status_code == 200, result.text
    assert command_terminal(client, result.json())["status"] == "succeeded"
    state = fold(store.read_all())
    assert state.research[-1]["summary"] == memo["summary"]
    assert state.research[-1]["trigger"] == "external"
    assert state.research[-1]["verification"]["method"] == "deterministic"
    assert state.report["headline"] == content["headline"]
    assert state.report["trigger"] == "external"
    research_event = next(e for e in store.read_all() if e.type == "research_completed")
    assert research_event.data["memo_id"] == research_event.data["memo"]["memo_id"]
    assert research_event.data["served_manual"] is False
    for event_type, data in (("research_completed", {"memo": {**memo, "verification": {"method": "llm"}}}),
                             ("report_generated", {"content": {**content, "trigger": "finish"}})):
        response = post_command(client, event_type, data, key=f"forge-{event_type}")
        assert response.status_code == 200 and response.json()["status"] == "rejected", response.text


def test_external_mode_requires_offline_backend():
    with pytest.raises(ValueError, match="backend=toy"):
        Settings(external_harness=True)
    assert Settings(backend="toy", external_harness=True).external_harness


def test_external_candidate_surface_reuses_patch_policy():
    spec = {"editables": [{"name": "."}], "edit_surface": ["src/**/*.py"],
            "protected_names": ["src/private.py", "grader.py"]}
    assert candidate_surface_refusal(spec, {"src/model.py": "pass"}, []) is None
    assert "outside_surface" in candidate_surface_refusal(spec, {"notes.txt": "x"}, [])
    assert "protected" in candidate_surface_refusal(spec, {"src/private.py": "x"}, [])
    assert "escapes" in candidate_surface_refusal(spec, {"../outside.py": "x"}, [])
    assert "repository task" in candidate_surface_refusal(None, {"x.py": "x"}, [])


def test_engine_refuses_hand_authored_unsafe_external_overlay(tmp_path):
    engine = make_engine(tmp_path / "run", external_harness=True)
    engine._repo_spec = {"editables": [{"name": "."}],
                         "edit_surface": ["src/**/*.py"],
                         "protected_names": ["grader.py"]}
    with pytest.raises(ValueError, match="outside_surface"):
        engine._prepare_injected_node(SimpleNamespace(nodes={}, aborted_nodes=[]), {
            "idea": {"operator": "draft"}, "files": {"notes.txt": "x"}})


@pytest.mark.parametrize("event_type,data", [
    (EV_FORK, {"from_node_id": 0}),
    (EV_FORCE_ABLATE, {"node_id": 0}),
    (EV_NODE_RESET, {"node_id": 0, "from_stage": "propose"}),
    (EV_NODE_RESET, {"node_id": 0, "from_stage": "implement"}),
    (EV_INJECT_NODE, {"idea": {"operator": "draft", "params": {"x": 3.0}}}),
])
def test_external_mode_refuses_implicit_internal_builds(tmp_path, event_type, data):
    (tmp_path / "config.snapshot.json").write_text(
        json.dumps(Settings(backend="toy", external_harness=True).model_dump(mode="json")))
    with pytest.raises(HTTPException) as caught:
        normalize_control(SimpleNamespace(), tmp_path, event_type, data)
    assert caught.value.status_code in (400, 409)


def test_external_intake_refuses_file_overlay_on_script_task(tmp_path):
    (tmp_path / "config.snapshot.json").write_text(
        json.dumps(Settings(backend="toy", external_harness=True).model_dump(mode="json")))
    example = Path(__file__).resolve().parents[1] / "examples" / "toy_task.json"
    (tmp_path / "task.snapshot.json").write_bytes(example.read_bytes())
    with pytest.raises(HTTPException, match="repository task"):
        normalize_control(SimpleNamespace(), tmp_path, EV_INJECT_NODE, {
            "idea": {"operator": "draft", "params": {"x": 3.0, "y": -1.0}},
            "files": {"unsafe.txt": "content"}})


def test_external_run_waits_for_candidate_evaluates_and_stops(tmp_path):
    engine = make_engine(tmp_path / "run", researcher=NoInternalRole(), developer=NoInternalRole(),
                         n_seeds=1, max_nodes=3, external_harness=True)

    async def scenario():
        async with anyio.create_task_group() as group:
            result = {}

            async def run():
                result["state"] = await engine.run()

            group.start_soon(run)
            with anyio.fail_after(8):
                while not fold(engine.store.read_all()).setup_done:
                    await anyio.sleep(0.02)
                assert not fold(engine.store.read_all()).nodes
                assert "External agent control" in (engine.run_dir / "AGENTS.md").read_text()
                engine.store.append(EV_INJECT_NODE, {
                    "idea": {"operator": "draft", "params": {"x": 3.0, "y": -1.0}},
                    "code": ("import json\nx, y = 3.0, -1.0\n"
                             "print(json.dumps({'metric': (x - 3)**2 + (y + 1)**2}))\n"),
                })
                while not fold(engine.store.read_all()).evaluated_nodes():
                    await anyio.sleep(0.02)

                engine.store.append(EV_RUN_ABORT, {"reason": "external agent finished"})
                while "state" not in result:
                    await anyio.sleep(0.02)
            assert result["state"].finished
            assert len(result["state"].evaluated_nodes()) == 1
            group.cancel_scope.cancel()

    anyio.run(scenario)


def test_external_agent_publishes_reusable_lessons_without_internal_reflection(tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    run_dir = tmp_path / "runs" / "demo"
    engine = make_engine(run_dir, researcher=NoInternalRole(), developer=NoInternalRole(),
                         max_nodes=3, external_harness=True, memory_dir=str(memory_dir),
                         reflection_priors=True, comparative_lessons=True,
                         cross_run_curation=True, concept_tidy=True)

    async def scenario():
        async with anyio.create_task_group() as group:
            result = {}

            async def run():
                result["state"] = await engine.run()

            group.start_soon(run)
            with anyio.fail_after(10):
                while not fold(engine.store.read_all()).setup_done:
                    await anyio.sleep(0.02)
                engine.store.append(EV_INJECT_NODE, {
                    "idea": {"operator": "draft", "params": {"x": 3.0, "y": -1.0}},
                    "code": ("import json\nx, y = 3.0, -1.0\n"
                             "print(json.dumps({'metric': (x - 3)**2 + (y + 1)**2}))\n"),
                })
                while not fold(engine.store.read_all()).evaluated_nodes():
                    await anyio.sleep(0.02)

                (run_dir / "config.snapshot.json").write_text(json.dumps(
                    Settings(backend="toy", external_harness=True,
                             memory_dir=str(memory_dir)).model_dump(mode="json")))
                (run_dir / "task.snapshot.json").write_bytes((
                    Path(__file__).resolve().parents[1] / "examples" /
                    "toy_task.json").read_bytes())

                def publish():
                    client = TestClient(make_app(tmp_path / "runs"))
                    assert client.put("/api/settings", json={
                        "settings": {"memory_dir": str(memory_dir)}}).status_code == 200
                    generation = http_run_generation(client)
                    body = {"expected_generation": generation, "action_id": "lesson-one",
                            "statement": "Explicit candidates can reach the optimum in one trial",
                            "outcome": "supported", "role": "researcher", "evidence": [0]}
                    preview = client.post("/api/runs/demo/novelty-preview", json={
                        "expected_generation": generation,
                        "idea": {"operator": "draft", "params": {"x": 3.0, "y": -1.0}}})
                    assert preview.status_code == 200, preview.text
                    assert preview.json()["grade"]["level"] == 1
                    assert preview.json()["advisory"] is True
                    assert client.post("/api/runs/demo/novelty-preview", json={
                        "expected_generation": "0" * 64,
                        "idea": {"operator": "draft"}}).status_code == 409
                    endpoint = "/api/runs/demo/lessons"
                    invalid = client.post(endpoint, json={**body, "evidence": [99]})
                    assert invalid.status_code == 409, invalid.text
                    first = client.post(endpoint, json=body)
                    assert first.status_code == 200, first.text
                    assert first.json()["replayed"] is False
                    again = client.post(endpoint, json=body)
                    assert again.status_code == 200 and again.json()["replayed"] is True
                    changed = client.post(endpoint, json={**body, "statement": "different"})
                    assert changed.status_code == 409
                    assert client.post(endpoint, json={
                        **body, "confidence": 0.8}).status_code == 409
                    stale = client.post(endpoint, json={**body, "expected_generation": "0" * 64})
                    assert stale.status_code == 409
                    memory = client.get("/api/memory").json()
                    assert any(row["statement"] == body["statement"] for row in memory["lessons"])
                    return body

                body = await anyio.to_thread.run_sync(publish)
                engine.store.append(EV_NODE_RESET, {
                    "node_id": 0, "generation": 0, "from_stage": "eval"})
                while not (fold(engine.store.read_all()).nodes[0].attempt >= 1
                           and fold(engine.store.read_all()).evaluated_nodes()):
                    await anyio.sleep(0.02)
                from looplab.engine.claims import load_claim_lessons
                while load_claim_lessons(memory_dir):
                    await anyio.sleep(0.02)
                # The result happens to be numerically identical, but a new attempt cannot
                # silently preserve an agent lesson about the previous measurement.
                new_generation = http_run_generation(TestClient(make_app(tmp_path / "runs")))
                fresh = await anyio.to_thread.run_sync(lambda: TestClient(
                    make_app(tmp_path / "runs")).post("/api/runs/demo/lessons", json={
                        **body, "action_id": "lesson-after-reset",
                        "expected_generation": new_generation}))
                assert fresh.status_code == 200, fresh.text
                engine.store.append(EV_RUN_ABORT, {"reason": "external agent finished"})
                while "state" not in result:
                    await anyio.sleep(0.02)
            assert result["state"].finished
            rows = load_claim_lessons(memory_dir)
            authored = [row for row in rows if row["statement"] == body["statement"]]
            assert len(authored) == 1
            assert authored[0]["fingerprint"] and authored[0]["evidence_sig"]
            assert authored[0]["run_uid"] == result["state"].run_uid
            assert len(rows) == 1  # finalization did not run its own lesson model
            from looplab.tools.memory_tools import MemoryTools
            next_run = RunState(task_id=result["state"].task_id,
                                goal=result["state"].goal,
                                direction=result["state"].direction,
                                run_id="next-run", run_uid="next-run-uid")
            reader = MemoryTools(str(memory_dir), role="researcher")
            reader.bind_state(next_run)
            assert body["statement"] in reader.execute("search_lessons", {"query": "optimum"})
            client = TestClient(make_app(tmp_path / "runs"))
            finished = client.post("/api/runs/demo/lessons", json={
                **body, "action_id": "post-finalize", "statement": "The final run kept its optimum",
                "expected_generation": http_run_generation(client)})
            assert finished.status_code == 200, finished.text
            assert len(load_claim_lessons(memory_dir)) == 2
            group.cancel_scope.cancel()

    anyio.run(scenario)
