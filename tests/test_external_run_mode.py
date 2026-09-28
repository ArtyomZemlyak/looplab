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
from looplab.core.models import (NODE_CONCEPT_PROVENANCE_AUTHORED, Idea, Node, NodeStatus,
                                 RunState, hypothesis_id)
from looplab.harness.obligations import run_base_due
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


def test_external_concept_base_is_due_after_first_scored_authored_node():
    settings = Settings(backend="toy", external_harness=True)
    state = RunState(task_id="task", run_id="demo", goal="g", direction="min")
    state.nodes[0] = Node(id=0, operator="draft", idea=Idea(operator="draft"),
                          code="print(1)", status=NodeStatus.evaluated, metric=0.9)
    state.node_concepts[0] = ["search/grid"]
    state.node_concept_provenance[0] = NODE_CONCEPT_PROVENANCE_AUTHORED
    assert run_base_due(settings, state)
    state.run_base_concepts = ["search/grid"]
    assert not run_base_due(settings, state)
    state.run_base_concepts = []
    settings.concept_run_base = False
    assert not run_base_due(settings, state)


def test_external_hypothesis_merge_reviews_current_board_atomically(tmp_path):
    from looplab.events.eventstore import EventStore
    from looplab.harness.hypotheses import merge_due

    rd = tmp_path / "runs" / "demo"
    rd.mkdir(parents=True)
    settings = Settings(backend="toy", external_harness=True, deep_research_every=-1)
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "task", "goal": "g",
                                 "direction": "min"})
    statements = ["Increase regularization", "Use stronger regularization",
                  "Try a longer warmup", "Reduce model width"]
    ids = [hypothesis_id(statement) for statement in statements]
    for statement, hid in zip(statements, ids):
        store.append("hypothesis_added", {"statement": statement, "id": hid})
    state = fold(store.read_all())
    assert merge_due(settings, state, store.read_all())
    (rd / "task.snapshot.json").write_bytes((
        Path(__file__).resolve().parents[1] / "examples" / "toy_task.json").read_bytes())
    with pytest.raises(HTTPException) as blocked:
        normalize_control(SimpleNamespace(state=lambda _: state), rd, EV_INJECT_NODE, {
            "idea": {"operator": "draft", "hypothesis": statements[0]},
            "code": "print(1)"})
    assert blocked.value.detail["code"] == "external_hypothesis_merge_review_required"
    client = TestClient(make_app(tmp_path / "runs"))
    generation = http_run_generation(client)
    endpoint = "/api/runs/demo/harness-hypotheses"
    board = client.get(endpoint, params={"expected_generation": generation})
    assert board.status_code == 200 and board.json()["due"]
    body = {"expected_generation": generation, "action_id": "merge-1",
            "expected_board_sha256": board.json()["board_sha256"],
            "decision": "merge", "canonical": ids[0], "aliases": [ids[1]],
            "statement": "Strengthen regularization", "reason": "Both ask the same regularization question"}
    assert client.post(endpoint, json={**body,
                       "expected_board_sha256": "0" * 64}).status_code == 409
    assert client.post(endpoint, json={**body, "aliases": ["unknown"]}).status_code == 400
    result = client.post(endpoint, json=body)
    assert result.status_code == 200, result.text
    assert client.post(endpoint, json=body).json()["replayed"]
    events = store.read_all()
    assert [event.type for event in events[-2:]] == [
        "hypothesis_merged", "hypothesis_merge_reviewed"]
    merged = fold(events)
    assert ids[1] not in merged.cards
    assert merged.cards[ids[0]].statement == "Strengthen regularization"
    assert not client.get(endpoint, params={"expected_generation": generation}).json()["due"]
    fifth = "Try a different optimizer"
    store.append("hypothesis_added", {"statement": fifth, "id": hypothesis_id(fifth)})
    assert client.get(endpoint, params={"expected_generation": generation}).json()["due"]
    no_merge = {"expected_generation": generation, "action_id": "merge-2",
                "expected_board_sha256": client.get(endpoint, params={
                    "expected_generation": generation}).json()["board_sha256"],
                "decision": "no_merge", "reason": "Remaining questions test distinct levers"}
    assert client.post(endpoint, json=no_merge).status_code == 200
    assert not client.get(endpoint, params={"expected_generation": generation}).json()["due"]
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(1)"})
    store.append("node_evaluated", {"node_id": 0, "metric": 0.8})
    assert client.get(endpoint, params={"expected_generation": generation}).json()["due"]


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


def test_configured_concept_tags_are_enforced_at_external_candidate_admission(tmp_path):
    (tmp_path / "task.snapshot.json").write_bytes(
        (Path(__file__).resolve().parents[1] / "examples" / "toy_task.json").read_bytes())
    state = RunState(task_id="task", run_id="demo", goal="test", direction="min")
    srv = SimpleNamespace(state=lambda _: state)
    config = tmp_path / "config.snapshot.json"
    enabled = Settings(backend="toy", external_harness=True, deep_research_every=-1,
                       track_hypotheses=False)
    config.write_text(json.dumps(enabled.model_dump(mode="json")))
    candidate = {"idea": {"operator": "draft"}, "code": "print(1)"}
    with pytest.raises(HTTPException) as caught:
        normalize_control(srv, tmp_path, EV_INJECT_NODE, candidate)
    assert caught.value.detail["code"] == "external_concepts_required"
    tagged = {**candidate, "idea": {**candidate["idea"],
                                   "concepts": ["search/grid"]}}
    assert normalize_control(srv, tmp_path, EV_INJECT_NODE, tagged)["idea"]["concepts"] == ["search/grid"]
    state.run_base_concepts = ["search/grid"]
    inherited = {**candidate, "idea": {"operator": "draft", "concept_mode": "delta",
                                      "concepts_added": [], "concepts_removed": []}}
    assert normalize_control(srv, tmp_path, EV_INJECT_NODE, inherited)["idea"]["concept_mode"] == "delta"
    erased = {**inherited, "idea": {**inherited["idea"], "concepts_removed": ["search/grid"]}}
    with pytest.raises(HTTPException) as caught:
        normalize_control(srv, tmp_path, EV_INJECT_NODE, erased)
    assert caught.value.detail["code"] == "external_concepts_required"
    config.write_text(json.dumps(Settings(
        backend="toy", external_harness=True, concept_pivot=False,
        concept_run_base=False, cross_run_concepts=False,
        deep_research_every=-1, track_hypotheses=False).model_dump(mode="json")))
    assert normalize_control(srv, tmp_path, EV_INJECT_NODE, candidate)["idea"]["operator"] == "draft"


def test_configured_research_and_report_gate_external_lifecycle(tmp_path):
    (tmp_path / "task.snapshot.json").write_bytes(
        (Path(__file__).resolve().parents[1] / "examples" / "toy_task.json").read_bytes())
    settings = Settings(backend="toy", external_harness=True,
                        concept_pivot=False, concept_run_base=False, cross_run_concepts=False,
                        track_hypotheses=False)
    (tmp_path / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    state = RunState(task_id="task", run_id="demo", goal="test", direction="min")
    srv = SimpleNamespace(state=lambda _: state)
    candidate = {"idea": {"operator": "draft"}, "code": "print(1)"}
    with pytest.raises(HTTPException) as caught:
        normalize_control(srv, tmp_path, EV_INJECT_NODE, candidate)
    assert caught.value.detail["code"] == "external_research_required"
    state.research.append({"at_node": 0, "summary": "Opening direction"})
    assert normalize_control(srv, tmp_path, EV_INJECT_NODE, candidate)["idea"]["operator"] == "draft"
    state.nodes[0] = SimpleNamespace(status="evaluated", metric=1.0, attempt=0,
                                     tombstoned=False)
    with pytest.raises(HTTPException) as caught:
        normalize_control(srv, tmp_path, EV_RUN_ABORT, {"reason": "finished"})
    assert caught.value.detail["code"] == "external_report_required"
    state.report = {"at_node": 1, "headline": "Result"}
    assert normalize_control(srv, tmp_path, EV_RUN_ABORT, {"reason": "finished"})["reason"] == "finished"

    settings.track_hypotheses = True
    settings.deep_research_every = -1
    (tmp_path / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    with pytest.raises(HTTPException) as caught:
        normalize_control(srv, tmp_path, EV_INJECT_NODE, candidate)
    assert caught.value.detail["code"] == "external_hypothesis_required"
    hypothesis = {**candidate, "idea": {**candidate["idea"],
                                      "hypothesis": "Explicit search reduces error"}}
    assert normalize_control(srv, tmp_path, EV_INJECT_NODE, hypothesis)["idea"]["hypothesis"]


def test_enabled_novelty_foresight_and_best_of_n_require_idea_bound_decisions(tmp_path):
    from looplab.events.eventstore import EventStore
    root = tmp_path / "runs"
    rd = root / "demo"
    rd.mkdir(parents=True)
    (rd / "task.snapshot.json").write_bytes(
        (Path(__file__).resolve().parents[1] / "examples" / "toy_task.json").read_bytes())
    settings = Settings(backend="toy", external_harness=True, deep_research_every=-1,
                        track_hypotheses=False, concept_pivot=False, concept_run_base=False,
                        cross_run_concepts=False, novelty_mode="llm", foresight=True,
                        foresight_panel=2, best_of_n=2, report_every=0)
    (rd / "config.snapshot.json").write_text(json.dumps(settings.model_dump(mode="json")))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "run_uid": "incarnation-one",
                                 "task_id": "task", "goal": "g", "direction": "min"})
    srv = SimpleNamespace(state=lambda _: fold(store.read_all()))
    candidate = {"idea": {"operator": "draft", "params": {"x": 3}}, "code": "print(1)"}
    with pytest.raises(HTTPException) as caught:
        normalize_control(srv, rd, EV_INJECT_NODE, candidate)
    assert set(caught.value.detail["phases"]) == {"novelty", "foresight", "candidate_ranking"}
    client = TestClient(make_app(root))
    contract = client.get("/api/runs/demo/harness-contract")
    assert contract.status_code == 200, contract.text
    assert contract.json()["phase_obligations"]["foresight"]["required"]
    generation = http_run_generation(client)
    for phase, count in (("novelty", 1), ("foresight", 2), ("candidate_ranking", 2)):
        body = {"expected_generation": generation, "action_id": "review-" + phase,
                "phase_id": phase, "idea": candidate["idea"], "decision": "submit",
                "alternatives": ([{"operator": "alternative", "params": {"x": 2}}]
                                 if count == 2 and phase != "candidate_ranking" else []),
                "implementations": ([
                    {"idea": candidate["idea"], "code": candidate["code"]},
                    {"idea": {"operator": "alternative", "params": {"x": 2}},
                     "code": "print(2)"}]
                    if phase == "candidate_ranking" else []),
                "reason": "Compared the candidate with alternatives"}
        response = client.post("/api/runs/demo/harness-decisions", json=body)
        assert response.status_code == 200, response.text
        assert client.post("/api/runs/demo/harness-decisions", json=body).json()["replayed"]
        if phase == "candidate_ranking":
            same_code = {**body, "action_id": "same-code-options",
                         "implementations": [body["implementations"][0],
                                             {**body["implementations"][1], "code": candidate["code"]}]}
            assert client.post("/api/runs/demo/harness-decisions", json=same_code).status_code == 400
    assert normalize_control(srv, rd, EV_INJECT_NODE, candidate)["idea"]["params"] == {"x": 3}
    different_code = {**candidate, "code": "print(3)"}
    with pytest.raises(HTTPException) as caught:
        normalize_control(srv, rd, EV_INJECT_NODE, different_code)
    assert caught.value.detail["phases"] == ["candidate_ranking"]
    changed = {**candidate, "idea": {**candidate["idea"], "params": {"x": 4}}}
    with pytest.raises(HTTPException) as caught:
        normalize_control(srv, rd, EV_INJECT_NODE, changed)
    assert "novelty" in caught.value.detail["phases"]

    review = {"expected_generation": generation, "action_id": "review-concept-merge",
              "phase_id": "concept_merge", "decision": "no_applicable_action",
              "reason": "There are no measured concept pairs to consolidate yet"}
    response = client.post("/api/runs/demo/harness-reviews", json=review)
    assert response.status_code == 200, response.text
    assert client.post("/api/runs/demo/harness-reviews", json=review).json()["replayed"]
    assert client.post("/api/runs/demo/harness-reviews", json={
        **review, "reason": "Changed conclusion from the same action"}).status_code == 409
    assert client.post("/api/runs/demo/harness-reviews", json={
        **review, "action_id": "forged-review", "decision": "completed",
        "action_ref": "missing-ledger-action"}).status_code == 409
    state = srv.state(rd)
    state.nodes[0] = SimpleNamespace(status="evaluated", metric=1.0, attempt=0,
                                     tombstoned=False)
    srv_with_node = SimpleNamespace(state=lambda _: state)
    with pytest.raises(HTTPException) as caught:
        normalize_control(srv_with_node, rd, EV_RUN_ABORT, {"reason": "finished"})
    assert caught.value.detail["code"] == "external_reviews_required"
    assert "concept_merge" in caught.value.detail["phases"]


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
                            "statement": "Use explicit candidate search to reach the optimum in small parameter spaces",
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
                    skill_body = {"expected_generation": generation, "action_id": "skill-one",
                                  "lesson_action_id": "lesson-one",
                                  "body": "Search bounded candidate parameter pairs and validate the chosen score."}
                    skill_endpoint = "/api/runs/demo/skill-candidates"
                    missing = client.post(skill_endpoint, json={**skill_body,
                        "lesson_action_id": "missing"})
                    assert missing.status_code == 409, missing.text
                    skill = client.post(skill_endpoint, json=skill_body)
                    assert skill.status_code == 200, skill.text
                    assert skill.json()["skill"]["status"] == "candidate"
                    assert client.post(skill_endpoint, json=skill_body).json()["replayed"] is True
                    assert client.post(skill_endpoint, json={**skill_body,
                        "body": "different"}).status_code == 409
                    assert client.post(skill_endpoint, json={**skill_body,
                        "expected_generation": "0" * 64}).status_code == 409
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
