"""A2 (retro-tag): `concept-coverage --persist` folds built tags into the run so the UI/cross-run see them.

The CLI builds a concept map for a finished run and (opt-in) appends generation-fenced
`EV_NODE_CONCEPTS` events. The round-trip must: (a) fold into `node_concepts` with CLASSIFIER
provenance, (b) skip nodes with no tags / unknown ids, (c) never override an operator re-tag.
"""
from looplab.cli.inspect_cmds import _persist_node_concepts
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _store(tmp_path) -> EventStore:
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dense-retrieval", "goal": "g", "direction": "max"})
    for i in (0, 1, 2):
        s.append("node_created", {"node_id": i, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"seed": float(i)}, "rationale": "r"}})
        s.append("node_evaluated", {"node_id": i, "metric": 0.8 + i * 0.01})
    return s


def test_persist_folds_into_node_concepts_as_classifier(tmp_path):
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
    assert st2.node_concept_provenance[0] == "classifier"
    assert st2.node_concepts_at_vocab[0] == 12


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
