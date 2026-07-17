"""PART V (B): run-base + per-node concept DELTA authoring.

A node may author only what CHANGES vs the run base + its parents (`concepts_added`/`concepts_removed`);
the fold POST-PASS materializes node_concepts = run_base ∪ inherited − removed + added. The materialization
is a topological read over the fully-folded DAG, so `fold` stays ORDER-TOLERANT (invariant 5). A
classifier/operator event still wins over an authored delta.
"""
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _store(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})
    return s


def _created(node_id, parent_ids=(), *, concepts=None, added=None, removed=None):
    idea = {"operator": "draft", "params": {"seed": float(node_id)}, "rationale": "r"}
    if concepts is not None:
        idea["concepts"] = concepts
    if added is not None:
        idea["concepts_added"] = added
    if removed is not None:
        idea["concepts_removed"] = removed
    return {"node_id": node_id, "parent_ids": list(parent_ids), "operator": "draft", "idea": idea}


def test_delta_materializes_base_minus_removed_plus_added(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["model/transformer", "loss/contrastive"]})
    # root node swaps transformer -> diffusion: remove transformer, add diffusion (vs the run base).
    s.append("node_created", _created(0, added=["model/diffusion"], removed=["model/transformer"]))
    st = fold(s.read_all())
    assert st.run_base_concepts == ["model/transformer", "loss/contrastive"]
    assert st.node_concepts[0] == ["loss/contrastive", "model/diffusion"]     # sorted; transformer dropped
    assert st.node_concept_provenance[0] == "researcher-authored"


def test_child_inherits_parent_delta(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["a"]})
    s.append("node_created", _created(0, added=["b"]))            # node0: a + b
    s.append("node_created", _created(1, parent_ids=[0], added=["c"], removed=["a"]))  # node1: (a+b) - a + c = b,c
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["a", "b"]
    assert st.node_concepts[1] == ["b", "c"]                     # inherited a+b, removed a, added c


def test_delta_materialization_is_order_tolerant(tmp_path):
    # Invariant 5, B-specific: the topological post-pass sees the whole DAG, so the POSITION of the
    # `run_concepts` base event relative to the nodes does not change the result — the base can be set
    # up front, mid-run, or after every node and each folds identically. (Causality still holds: a parent
    # node_created precedes its child, as in every real log — fold has always required that.)
    def _events(base_last):
        s = _store(tmp_path / ("last" if base_last else "first"))
        nodes = [
            ("node_created", _created(0, added=["b"])),
            ("node_created", _created(1, parent_ids=[0], added=["c"], removed=["a"])),
            ("node_created", _created(2, parent_ids=[1], removed=["b"])),
        ]
        seq = nodes + [("run_concepts", {"concepts": ["a"]})] if base_last \
            else [("run_concepts", {"concepts": ["a"]})] + nodes
        for typ, data in seq:
            s.append(typ, data)
        return fold(s.read_all()).node_concepts
    (tmp_path / "last").mkdir(); (tmp_path / "first").mkdir()
    expected = {0: ["a", "b"], 1: ["b", "c"], 2: ["c"]}
    assert _events(base_last=False) == expected     # base set first
    assert _events(base_last=True) == expected       # base set AFTER every node -> identical (post-pass)


def test_full_set_authoring_is_unchanged(tmp_path):
    # A node with only the legacy full `concepts` (no delta) folds exactly as before — direct membership.
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["base/x"]})
    s.append("node_created", _created(0, concepts=["loss/dcl", "arch/moe"]))   # full set, ignores base
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["loss/dcl", "arch/moe"]      # not merged with base — legacy path intact
    assert 0 not in st.node_concept_deltas


def test_classifier_event_overrides_an_authored_delta(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["a"]})
    s.append("node_created", _created(0, added=["b"]))
    s.append("node_concepts", {"node_id": 0, "concepts": ["classifier/x"], "generation": 0})
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["classifier/x"]              # classifier wins over the delta
    assert st.node_concept_provenance[0] == "classifier"


def test_operator_retag_overrides_an_authored_delta(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["a"]})
    s.append("node_created", _created(0, added=["b"]))
    s.append("concept_tag_edited", {"node_id": 0, "concepts": ["operator/pinned"], "generation": 0})
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["operator/pinned"]
    assert st.node_concept_provenance[0] == "operator-edited"


def test_overridden_delta_parent_contributes_its_real_set_to_children(tmp_path):
    # A delta parent that a classifier overrode must pass its CLASSIFIER set (not its raw delta) down.
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["a"]})
    s.append("node_created", _created(0, added=["b"]))
    s.append("node_concepts", {"node_id": 0, "concepts": ["z"], "generation": 0})   # override parent -> {z}
    s.append("node_created", _created(1, parent_ids=[0], added=["c"]))
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["z"]
    # node1 inherits the PARENT's effective set (the classifier's {z}, not the raw delta), then adds c.
    # Base `a` does NOT reappear: base flows through roots, and node0's classifier override replaced it.
    assert st.node_concepts[1] == ["c", "z"]
