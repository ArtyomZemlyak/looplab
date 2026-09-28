"""Every discoverable decision needs an actual read and an admissible external action."""
from looplab.core.prompts import PROMPT_KEYS, UNGOVERNED_PROMPT_FAMILIES
from looplab.harness.phases import PHASES, phase_catalog, phase_detail
from looplab.harness.obligations import run_obligations
from looplab.core.config import Settings
from types import SimpleNamespace


def test_phase_catalog_covers_all_promptstore_decisions_and_legacy_families():
    assert {key for phase in PHASES for key in phase.prompts} == set(PROMPT_KEYS)
    assert {phase.legacy_prompt_family for phase in PHASES
            if phase.legacy_prompt_family} == {
                family for family, _ in UNGOVERNED_PROMPT_FAMILIES}
    assert all(phase.reads and phase.writes and phase.internal for phase in PHASES)
    assert phase_detail("research")["entity"] == "ResearchMemo"
    assert phase_detail("configuration")["write_access"]["PUT /api/settings"] == "operator"
    assert phase_detail("configuration")["write_access"]["PUT /api/runs/{run_id}/config"] == "external_agent"
    assert phase_detail("genesis")["write_access"]["POST /api/start"] == "operator"
    assert phase_catalog("research")[0]["id"] == "research"
    assert phase_detail("absent") is None


def test_external_writes_resolve_to_live_commands_or_http_routes(tmp_path):
    from looplab.serve.protocol import CONTROL_EVENTS
    from looplab.serve.server import make_app

    paths = make_app(tmp_path).openapi()["paths"]
    for phase in PHASES:
        for ref in (*phase.reads, *phase.writes):
            if ref.startswith("command:"):
                assert ref.removeprefix("command:") in CONTROL_EVENTS, (phase.id, ref)
                continue
            method, path = ref.split(" ", 1)
            assert path in paths and method.lower() in paths[path], (phase.id, ref)


def test_concept_obligation_follows_config_and_reports_admission_gate():
    task = SimpleNamespace(eval=SimpleNamespace(stages=[]), repo_spec=lambda: None)
    enabled = run_obligations(task, Settings(backend="toy", external_harness=True),
                              generation="a" * 64)
    assert enabled["phase_obligations"]["concept_tags"]["required"] is True
    assert enabled["phase_obligations"]["concept_tags"]["enforced"] is True
    assert enabled["phase_obligations"]["research"]["required"] is True
    assert enabled["phase_obligations"]["report"]["required"] is True
    disabled = run_obligations(task, Settings(
        backend="toy", external_harness=True, concept_pivot=False,
        concept_run_base=False, cross_run_concepts=False), generation="a" * 64)
    assert disabled["phase_obligations"]["concept_tags"]["required"] is False
    assert disabled["generation"] == enabled["generation"]


def test_phase_receipts_expire_when_measured_outcome_changes_at_same_node_count(tmp_path):
    import json
    from looplab.core.models import Idea, Node, NodeStatus, RunState
    from looplab.harness.decisions import decision_file, idea_digest, missing_decisions
    from looplab.harness.obligations import evidence_revision
    from looplab.harness.reviews import missing_reviews, review_file

    state = RunState(run_id="demo", run_uid="incarnation", task_id="task",
                     goal="g", direction="min")
    idea = Idea(operator="draft")
    state.nodes[0] = Node(id=0, operator="draft", idea=idea,
                          status=NodeStatus.evaluated, metric=1.0)
    settings = Settings(backend="toy", external_harness=True,
                        novelty_mode="llm", foresight=False, best_of_n=1,
                        report_every=0)
    generation = "a" * 64
    revision = evidence_revision(state)
    decision_file(tmp_path).write_text(json.dumps({
        "run_uid": state.run_uid, "generation": generation, "at_node": 1,
        "phase_id": "novelty", "idea_sha256": idea_digest(idea),
        "evidence_revision": revision, "decision": "submit", "options_considered": 1}) + "\n")
    review_file(tmp_path).write_text(json.dumps({
        "run_uid": state.run_uid, "generation": generation, "at_node": 1,
        "phase_id": "concept_merge", "evidence_revision": revision,
        "decision": "no_applicable_action"}) + "\n")
    assert "novelty" not in missing_decisions(tmp_path, settings, state, idea, generation)
    assert "concept_merge" not in missing_reviews(tmp_path, settings, state, generation)
    state.nodes[0].metric = 2.0
    assert "novelty" in missing_decisions(tmp_path, settings, state, idea, generation)
    assert "concept_merge" in missing_reviews(tmp_path, settings, state, generation)


def test_enabled_memos_and_reports_must_follow_new_measurements():
    from looplab.core.models import Idea, Node, NodeStatus, RunState
    from looplab.harness.obligations import final_report_due, research_due

    settings = Settings(backend="toy", external_harness=True,
                        deep_research_every=0, report_every=1)
    state = RunState(run_id="demo", task_id="task", goal="g", direction="min")
    state.nodes[0] = Node(id=0, operator="draft", idea=Idea(operator="draft"),
                          status=NodeStatus.evaluated, metric=1.0)
    state.research = [{"at_node": 1, "summary": "First result"}]
    state.report = {"at_node": 1, "headline": "First result"}
    events = [SimpleNamespace(seq=10, type="node_evaluated", data={}),
              SimpleNamespace(seq=11, type="research_completed", data={"at_node": 1}),
              SimpleNamespace(seq=12, type="report_generated", data={"at_node": 1})]
    assert not research_due(settings, state, events)
    assert not final_report_due(settings, state, events)
    events.append(SimpleNamespace(seq=13, type="node_reset", data={"node_id": 0}))
    assert research_due(settings, state, events)
    assert final_report_due(settings, state, events)
