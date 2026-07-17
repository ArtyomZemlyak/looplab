"""A2 retro-tag: persist only terminal, exact-snapshot concept membership with explicit provenance.

Offline heuristic tags remain display-only; reviewed agentic tags may upgrade them into classifier
evidence. The CLI mutation owns ``engine.lock`` and CASes the event tail of a fully finalized run.
"""
import pytest
from typer.testing import CliRunner

from looplab.cli import _engine_singleton, app
from looplab.cli.inspect_cmds import _persist_node_concepts
from looplab.core.models import (NODE_CONCEPT_PROVENANCE_CLASSIFIER,
                                 NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC,
                                 classifier_verified_node_concepts)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


runner = CliRunner()


def _store(tmp_path) -> EventStore:
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dense-retrieval", "goal": "g", "direction": "max"})
    for i in (0, 1, 2):
        s.append("node_created", {"node_id": i, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"seed": float(i)},
                                           "rationale": "decoupled contrastive temperature"}})
        s.append("node_evaluated", {"node_id": i, "metric": 0.8 + i * 0.01})
    return s


def test_offline_persist_folds_as_display_only_provenance(tmp_path):
    s = _store(tmp_path)
    st = fold(s.read_all())
    tags = {0: frozenset({"loss/contrastive", "hyperparameter/temperature"}),
            1: frozenset({"regularization/r-drop"}),
            2: frozenset()}                       # empty -> skipped
    n = _persist_node_concepts(s, st, tags, "offline-heuristic", vocab_size=12)
    assert n == 2                                 # node 2 (empty) skipped
    st2 = fold(s.read_all())
    assert st2.node_concepts[0] == ["hyperparameter/temperature", "loss/contrastive"]   # sorted
    assert st2.node_concepts[1] == ["regularization/r-drop"]
    assert 2 not in st2.node_concepts
    assert st2.node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC
    assert classifier_verified_node_concepts(st2, 0) == []
    assert st2.node_concepts_at_vocab == {}


def test_persist_skips_unknown_node_ids(tmp_path):
    s = _store(tmp_path)
    st = fold(s.read_all())
    n = _persist_node_concepts(s, st, {99: frozenset({"loss/x"})}, "offline-heuristic", 3)
    assert n == 0
    assert fold(s.read_all()).node_concepts == {}


def test_persist_yields_to_operator_retag(tmp_path):
    # An operator re-tag (EV_CONCEPT_TAG_EDITED) must win over a later retro-tag (invariant 5).
    s = _store(tmp_path)
    s.append("concept_tag_edited",
             {"node_id": 0, "concepts": ["operator/pinned"], "generation": 0})
    st = fold(s.read_all())
    assert st.node_concept_provenance.get(0) == "operator-edited"
    _persist_node_concepts(s, st, {0: frozenset({"loss/contrastive"})}, "offline-heuristic", 5)
    st2 = fold(s.read_all())
    assert st2.node_concepts[0] == ["operator/pinned"]              # operator still wins
    assert st2.node_concept_provenance[0] == "operator-edited"


def test_same_agentic_ids_upgrade_heuristic_once_then_deduplicate(tmp_path):
    s = _store(tmp_path)
    tags = {0: frozenset({"loss/contrastive"})}
    first = _persist_node_concepts(s, fold(s.read_all()), tags, "offline-heuristic", 5)
    upgraded = _persist_node_concepts(s, fold(s.read_all()), tags, "agentic", 9)
    repeated = _persist_node_concepts(s, fold(s.read_all()), tags, "agentic", 9)

    assert (first, upgraded, repeated) == (1, 1, 0)
    events = [event for event in s.read_all() if event.type == "node_concepts"]
    assert [event.data["mode"] for event in events] == ["offline-heuristic", "agentic"]
    state = fold(s.read_all())
    assert state.node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_CLASSIFIER
    assert classifier_verified_node_concepts(state, 0) == ["loss/contrastive"]
    assert state.node_concepts_at_vocab[0] == 9


def test_offline_repeat_is_idempotent_and_cannot_downgrade_classifier(tmp_path):
    s = _store(tmp_path)
    initial = {0: frozenset({"loss/agentic"})}
    assert _persist_node_concepts(s, fold(s.read_all()), initial, "llm", 8) == 1
    assert _persist_node_concepts(s, fold(s.read_all()), initial, "llm", 8) == 0
    # Even different coarse ids may not replace independent classifier evidence.
    coarse = {0: frozenset({"loss/coarse"})}
    assert _persist_node_concepts(s, fold(s.read_all()), coarse, "offline-heuristic", 3) == 0
    state = fold(s.read_all())
    assert state.node_concepts[0] == ["loss/agentic"]
    assert state.node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_CLASSIFIER


def test_persist_helper_rejects_unknown_producer_mode(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError, match="unsupported node-concept producer mode"):
        _persist_node_concepts(
            s, fold(s.read_all()), {0: frozenset({"loss/x"})}, "classifier-v-next", 1)


def _finish_modern(store: EventStore, *, complete: bool) -> None:
    scope = "retro-tag-finalize"
    tail = store.read_all()[-1].seq
    begun = store.append("finalize_step", {
        "scope": scope,
        "step": "begun",
        "after_seq": tail,
    })
    finish = store.append("run_finished", {
        "reason": "done",
        "after_seq": begun.seq,
        "finalization_required": True,
        "finalize_scope": scope,
    })
    store.append("finalization_finished", {"finish_seq": finish.seq})
    if complete:
        store.append("finalize_step", {"scope": scope, "step": "complete"})


def _concept_coverage(*args: str):
    return runner.invoke(app, ["concept-coverage", *args])


def test_cli_persist_requires_finished_not_merely_stopped(tmp_path):
    _store(tmp_path)
    result = _concept_coverage(str(tmp_path), "--offline", "--persist")
    assert result.exit_code == 2
    assert "fully finalized FINISHED boundary" in result.output
    assert not any(event.type == "node_concepts"
                   for event in EventStore(tmp_path / "events.jsonl").read_all())


def test_cli_persist_rejects_run_finished_before_scoped_finalize_complete(tmp_path):
    store = _store(tmp_path)
    _finish_modern(store, complete=False)
    state = fold(store.read_all())
    assert state.finished and not state.finalization_pending()  # marker alone used to bypass the guard

    result = _concept_coverage(str(tmp_path), "--offline", "--persist")

    assert result.exit_code == 2
    assert "fully finalized FINISHED boundary" in result.output
    assert not any(event.type == "node_concepts" for event in store.read_all())


def test_cli_persist_rejects_invalidated_scope_without_explicit_complete(tmp_path):
    store = _store(tmp_path)
    _finish_modern(store, complete=False)
    store.append("annotation", {"text": "foreign event invalidates recovery scope"})
    state = fold(store.read_all())
    from looplab.engine.finalize import incomplete_finalize_scope

    assert state.finished and not state.finalization_pending()
    assert incomplete_finalize_scope(store.read_all()) is None

    result = _concept_coverage(str(tmp_path), "--offline", "--persist")

    assert result.exit_code == 2
    assert "fully finalized FINISHED boundary" in result.output
    assert not any(event.type == "node_concepts" for event in store.read_all())


@pytest.mark.parametrize("protocol", ["legacy", "modern"])
def test_cli_persist_accepts_quiescent_finished_protocols_and_is_idempotent(tmp_path, protocol):
    store = _store(tmp_path)
    if protocol == "legacy":
        store.append("run_finished", {"reason": "done"})
    else:
        _finish_modern(store, complete=True)

    first = _concept_coverage(str(tmp_path), "--offline", "--persist")
    second = _concept_coverage(str(tmp_path), "--offline", "--persist")

    assert first.exit_code == second.exit_code == 0, first.output + second.output
    assert "persisted" in first.output and "persisted 0" in second.output
    state = fold(store.read_all())
    assert state.node_concepts
    assert set(state.node_concept_provenance.values()) == {
        NODE_CONCEPT_PROVENANCE_OFFLINE_HEURISTIC}
    assert all(classifier_verified_node_concepts(state, node_id) == []
               for node_id in state.node_concepts)


def test_cli_persist_rejects_finished_state_while_engine_lock_is_live(tmp_path):
    store = _store(tmp_path)
    store.append("run_finished", {"reason": "done"})
    with _engine_singleton(tmp_path) as owned:
        assert owned
        result = _concept_coverage(str(tmp_path), "--offline", "--persist")
    assert result.exit_code == 2
    assert "engine is still writing terminal artifacts" in result.output
    assert not any(event.type == "node_concepts" for event in store.read_all())


def test_cli_persist_cas_rejects_event_appended_during_analysis(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.append("run_finished", {"reason": "done"})
    from looplab.search import concept_graph
    original = concept_graph.tag_nodes_heuristic
    raced = False

    def append_racing_event(state, graph):
        nonlocal raced
        tags = original(state, graph)
        if not raced:
            raced = True
            store.append("annotation", {"text": "concurrent review note"})
        return tags

    monkeypatch.setattr(concept_graph, "tag_nodes_heuristic", append_racing_event)
    result = _concept_coverage(str(tmp_path), "--offline", "--persist")

    assert raced and result.exit_code == 2
    assert "changed while concept tags were being built" in result.output
    assert not any(event.type == "node_concepts" for event in store.read_all())
