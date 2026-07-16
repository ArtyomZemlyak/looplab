"""Phase 0 (themes → concepts): the Researcher AUTHORS a node's concepts on the Idea, and they fold
into RunState.node_concepts at node_created — no tagging cadence, no LLM, fully offline/deterministic.
See core/models.py `Idea.concepts` and events/replay.py `_on_node_created`. A later `node_concepts`
event (the tagging cadence consolidator) refines them (last-write-wins)."""
from looplab.core.models import Idea
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _store(tmp_path) -> EventStore:
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})
    return s


def _created(node_id, concepts=None):
    idea = {"operator": "draft", "params": {"seed": float(node_id)}, "rationale": "r"}
    if concepts is not None:
        idea["concepts"] = concepts
    return {"node_id": node_id, "parent_ids": [], "operator": "draft", "idea": idea}


def test_idea_concepts_round_trip():
    idea = Idea(operator="draft", params={"seed": 1.0}, rationale="try dcl",
                concepts=["loss/contrastive/dcl", "regularization/r-drop"])
    d = idea.model_dump()
    assert d["concepts"] == ["loss/contrastive/dcl", "regularization/r-drop"]
    assert Idea(**d).concepts == idea.concepts            # rides on the Idea through the event log
    assert Idea(operator="draft", params={}, rationale="r").concepts == []   # default empty


def test_authored_concepts_fold_into_node_concepts(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["loss/dcl", "architecture/moe"]))
    s.append("node_evaluated", {"node_id": 0, "metric": 0.8})
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["loss/dcl", "architecture/moe"]


def test_no_concepts_leaves_node_concepts_absent(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, None))           # Researcher emitted no concepts
    st = fold(s.read_all())
    assert 0 not in st.node_concepts                       # never write an empty membership set


def test_cadence_node_concepts_override_authored(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["loss/dcl"]))
    # a later tagging cadence refines the node's tags against a grown vocabulary — last write wins
    s.append("node_concepts", {"node_id": 0,
                               "concepts": ["loss/contrastive/dcl", "hyperparameter/temperature"],
                               "at_vocab": 12})
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["loss/contrastive/dcl", "hyperparameter/temperature"]


def test_reset_reemit_without_concepts_keeps_prior(tmp_path):
    # a node_reset re-emits node_created for the same (pending) node; an empty-concepts re-emit must
    # not clobber a prior tagging (the guard is on non-empty authored concepts).
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["loss/dcl"]))
    s.append("node_reset", {"node_id": 0, "stage": "propose"})
    s.append("node_created", _created(0, None))            # rebuilt idea carries no concepts
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["loss/dcl"]


def test_authored_concepts_replay_stable(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, ["b/x", "a/y"]))
    events = s.read_all()
    assert fold(events).node_concepts == fold(events).node_concepts == {0: ["b/x", "a/y"]}
