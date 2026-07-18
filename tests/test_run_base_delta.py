"""PART V (B): run-base + per-node concept DELTA authoring.

A node may author only what CHANGES vs the run base + its parents (`concepts_added`/`concepts_removed`);
the fold POST-PASS materializes node_concepts = run_base ∪ inherited − removed + added. The materialization
is a topological read over the fully-folded DAG, so `fold` stays ORDER-TOLERANT (invariant 5). A
classifier/operator event still wins over an authored delta.
"""
from types import SimpleNamespace

import pytest

from looplab.core.models import Event, Idea, Node, RunState
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import FoldCursor, _materialize_concept_deltas, fold
from looplab.search.concept_graph import node_concept_delta


def _seed_host(store, *, concept_run_base=True):
    """Minimal host for the engine's base-seeding cadence method (reads _concept_run_base + store)."""
    return SimpleNamespace(_concept_run_base=concept_run_base, store=store)


def _store(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})
    return s


def _created(node_id, parent_ids=(), *, mode=None, concepts=None, added=None, removed=None):
    idea = {"operator": "draft", "params": {"seed": float(node_id)}, "rationale": "r"}
    if mode is not None:
        idea["concept_mode"] = mode
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
    s.append("node_created", _created(
        0, mode="delta", added=["model/diffusion"], removed=["model/transformer"]))
    st = fold(s.read_all())
    assert st.run_base_concepts == ["model/transformer", "loss/contrastive"]
    assert st.node_concepts[0] == ["loss/contrastive", "model/diffusion"]     # sorted; transformer dropped
    assert st.node_concept_provenance[0] == "researcher-authored"


def test_child_inherits_parent_delta(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["a"]})
    s.append("node_created", _created(0, mode="delta", added=["b"]))  # node0: a + b
    s.append("node_created", _created(
        1, parent_ids=[0], mode="delta", added=["c"], removed=["a"]))  # (a+b)-a+c
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
            ("node_created", _created(0, mode="delta", added=["b"])),
            ("node_created", _created(
                1, parent_ids=[0], mode="delta", added=["c"], removed=["a"])),
            ("node_created", _created(2, parent_ids=[1], mode="delta", removed=["b"])),
        ]
        seq = nodes + [("run_concepts", {"concepts": ["a"]})] if base_last \
            else [("run_concepts", {"concepts": ["a"]})] + nodes
        for typ, data in seq:
            s.append(typ, data)
        return fold(s.read_all()).node_concepts
    (tmp_path / "last").mkdir()
    (tmp_path / "first").mkdir()
    expected = {0: ["a", "b"], 1: ["b", "c"], 2: ["c"]}
    assert _events(base_last=False) == expected     # base set first
    assert _events(base_last=True) == expected       # base set AFTER every node -> identical (post-pass)


def test_active_delta_cycle_fails_closed_independently_of_event_order():
    started = Event(seq=0, type="run_started", data={
        "run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})

    def _fold(order):
        ideas = {0: ["cycle/a"], 1: ["cycle/b"]}
        events = [started]
        # Both nodes first exist as valid roots. Pending-node replacement may then change their parent
        # edges and produce an adversarial cycle while satisfying the fold's parent-exists invariant.
        for nid in order:
            events.append(Event(
                seq=len(events), type="node_created",
                data=_created(nid, mode="delta", added=ideas[nid])))
        for nid in order:
            events.append(Event(
                seq=len(events), type="node_created",
                data=_created(nid, parent_ids=[1 - nid], mode="delta", added=ideas[nid])))
        events.append(Event(
            seq=len(events), type="node_created",
            data=_created(2, parent_ids=[1], mode="delta", added=["descendant/c"])))
        return fold(events)

    expected = {0: [], 1: [], 2: []}
    for order in ([0, 1], [1, 0]):
        state = _fold(order)
        assert state.node_concepts == expected
        # CODEX AGENT: empty is the safe fallback, not an honest known-empty classification. Every
        # cycle member and active delta descendant gets the same deterministic typed receipt.
        assert state.node_concept_materialization_receipts == {
            0: {"status": "unavailable", "reasons": ["delta_dependency_cycle"]},
            1: {"status": "unavailable", "reasons": ["delta_dependency_cycle"]},
            2: {"status": "unavailable", "reasons": ["delta_dependency_cycle"]},
        }
        restored = type(state).model_validate_json(state.model_dump_json())
        assert (restored.node_concept_materialization_receipts
                == state.node_concept_materialization_receipts)


def test_fold_cursor_recomputes_and_clears_delta_cycle_receipts():
    events = [
        Event(seq=0, type="run_started", data={
            "run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"}),
        Event(seq=1, type="run_concepts", data={"concepts": ["base/x"]}),
        Event(seq=2, type="node_created", data=_created(0, mode="delta", added=["cycle/a"])),
        Event(seq=3, type="node_created", data=_created(1, mode="delta", added=["cycle/b"])),
        Event(seq=4, type="node_created", data=_created(
            0, parent_ids=[1], mode="delta", added=["cycle/a"])),
        Event(seq=5, type="node_created", data=_created(
            1, parent_ids=[0], mode="delta", added=["cycle/b"])),
        Event(seq=6, type="node_created", data=_created(
            2, parent_ids=[1], mode="delta", added=["descendant/c"])),
    ]
    repaired = Event(seq=7, type="node_created", data=_created(
        0, parent_ids=[], mode="delta", added=["cycle/a"]))

    cursor = FoldCursor()
    cursor.extend(events)
    cycle_snapshot = cursor.snapshot()
    assert cycle_snapshot.node_concept_materialization_receipts == {
        0: {"status": "unavailable", "reasons": ["delta_dependency_cycle"]},
        1: {"status": "unavailable", "reasons": ["delta_dependency_cycle"]},
        2: {"status": "unavailable", "reasons": ["delta_dependency_cycle"]},
    }

    cursor.extend([repaired])
    incremental = cursor.snapshot()
    expected = fold([*events, repaired])
    assert incremental.model_dump(mode="json") == expected.model_dump(mode="json")
    assert incremental.node_concept_materialization_receipts == {}
    assert incremental.node_concepts == {
        0: ["base/x", "cycle/a"],
        1: ["base/x", "cycle/a", "cycle/b"],
        2: ["base/x", "cycle/a", "cycle/b", "descendant/c"],
    }


def test_delta_materialization_handles_a_lineage_deeper_than_python_recursion_limit():
    events = [
        Event(seq=0, type="run_started", data={
            "run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"}),
        Event(seq=1, type="run_concepts", data={"concepts": ["base/x"]}),
    ]
    depth = 1_100
    for nid in range(depth):
        parents = [] if nid == 0 else [nid - 1]
        added = ["tail/y"] if nid == depth - 1 else []
        events.append(Event(
            seq=len(events), type="node_created",
            data=_created(nid, parent_ids=parents, mode="delta", added=added, removed=[])))

    st = fold(events)
    assert st.node_concepts[0] == ["base/x"]
    assert st.node_concepts[depth - 1] == ["base/x", "tail/y"]


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
    s.append("node_created", _created(0, mode="delta", added=["b"]))
    s.append("node_concepts", {"node_id": 0, "concepts": ["classifier/x"], "generation": 0})
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["classifier/x"]              # classifier wins over the delta
    assert st.node_concept_provenance[0] == "classifier"


def test_fold_cursor_suffix_matches_full_fold_without_postpass_leakage(tmp_path):
    # CODEX AGENT: the live /concepts cursor keeps the pre-post-pass state. Materializing DELTAs in one
    # response must not overwrite that accumulator before a classifier event arrives on the next poll.
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["a"]})
    s.append("node_created", _created(0, added=["b"]))
    s.append("node_concepts", {
        "node_id": 0, "concepts": ["classifier/x"], "generation": 0,
    })
    events = s.read_all()
    cursor = FoldCursor()
    cursor.extend(events[:3])
    delta_snapshot = cursor.snapshot()
    assert delta_snapshot.node_concepts[0] == ["a", "b"]
    delta_snapshot.node_concepts[0].append("caller/poison")

    cursor.extend(events[3:])
    incremental = cursor.snapshot()
    expected = fold(events)
    assert incremental.model_dump(mode="json") == expected.model_dump(mode="json")
    assert incremental.node_concepts[0] == ["classifier/x"]


def test_operator_retag_overrides_an_authored_delta(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["a"]})
    s.append("node_created", _created(0, mode="delta", added=["b"]))
    s.append("concept_tag_edited", {"node_id": 0, "concepts": ["operator/pinned"], "generation": 0})
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["operator/pinned"]
    assert st.node_concept_provenance[0] == "operator-edited"


def test_overridden_delta_parent_contributes_its_real_set_to_children(tmp_path):
    # A delta parent that a classifier overrode must pass its CLASSIFIER set (not its raw delta) down.
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["a"]})
    s.append("node_created", _created(0, mode="delta", added=["b"]))
    s.append("node_concepts", {"node_id": 0, "concepts": ["z"], "generation": 0})   # override parent -> {z}
    s.append("node_created", _created(1, parent_ids=[0], mode="delta", added=["c"]))
    st = fold(s.read_all())
    assert st.node_concepts[0] == ["z"]
    # node1 inherits the PARENT's effective set (the classifier's {z}, not the raw delta), then adds c.
    # Base `a` does NOT reappear: base flows through roots, and node0's classifier override replaced it.
    assert st.node_concepts[1] == ["c", "z"]


def test_explicit_empty_delta_root_inherits_the_full_base(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["model/transformer", "loss/contrastive"]})
    s.append("node_created", _created(0, mode="delta", added=[], removed=[]))

    st = fold(s.read_all())
    assert st.node_concept_deltas == {0: {"added": [], "removed": []}}
    assert st.node_concepts == {0: ["loss/contrastive", "model/transformer"]}
    assert st.node_concept_provenance == {0: "researcher-authored"}
    assert node_concept_delta(st, 0) == {
        "parent_ids": [],
        "added": [],
        "removed": [],
        "inherited": ["loss/contrastive", "model/transformer"],
    }


def test_explicit_empty_delta_child_inherits_parent_union(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, concepts=["arch/a", "shared/x"]))
    s.append("node_created", _created(1, concepts=["data/b", "shared/x"]))
    s.append("node_created", _created(
        2, parent_ids=[0, 1], mode="delta", added=[], removed=[]))

    st = fold(s.read_all())
    assert st.node_concepts[2] == ["arch/a", "data/b", "shared/x"]


def test_explicit_empty_delta_is_distinct_from_absent_legacy_membership(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, mode="delta", added=[], removed=[]))
    s.append("node_created", _created(1))

    st = fold(s.read_all())
    assert st.node_concepts == {0: []}
    assert st.node_concept_deltas == {0: {"added": [], "removed": []}}
    assert st.node_concept_provenance == {0: "researcher-authored"}
    assert st.nodes[1].idea.concept_mode is None


def test_explicit_full_empty_is_known_empty_but_legacy_empty_stays_absent(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, mode="full", concepts=[]))
    s.append("node_created", _created(1, concepts=[]))

    st = fold(s.read_all())
    assert st.node_concepts == {0: []}
    assert st.node_concept_provenance == {0: "researcher-authored"}
    assert 1 not in st.node_concepts


def test_default_delta_fields_without_mode_do_not_activate_delta(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["base/x"]})
    # Old serializers may include default lists while omitting the new discriminator.
    s.append("node_created", _created(0, added=[], removed=[]))

    st = fold(s.read_all())
    assert st.nodes[0].idea.concept_mode is None
    assert 0 not in st.node_concept_deltas
    assert 0 not in st.node_concepts
    assert 0 not in st.node_concept_provenance


def test_explicit_full_mode_ignores_nonempty_delta_fields(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["base/x"]})
    s.append("node_created", _created(
        0, mode="full", concepts=["full/y"], added=["wrong/add"], removed=["base/x"]))

    st = fold(s.read_all())
    assert st.node_concepts == {0: ["full/y"]}
    assert st.node_concept_deltas == {}


def test_delta_child_inherits_unchanged_base_then_adds(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["base/a", "base/b"]})
    s.append("node_created", _created(0, mode="delta", added=[], removed=[]))
    s.append("node_created", _created(
        1, parent_ids=[0], mode="delta", added=["child/c"], removed=[]))

    st = fold(s.read_all())
    assert st.node_concepts == {
        0: ["base/a", "base/b"],
        1: ["base/a", "base/b", "child/c"],
    }


def test_delta_normalizes_every_operand_before_subtraction(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["  Model/Transformer/ ", "Loss/Contrastive"]})
    s.append("node_created", _created(
        0, mode="delta", added=[" Model/Diffusion "], removed=["model/transformer"]))

    st = fold(s.read_all())
    assert st.node_concepts[0] == ["loss/contrastive", "model/diffusion"]


def test_delta_resolves_full_consolidation_chain_before_set_algebra(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["Legacy/Transformer", "keep/x"]})
    s.append("concept_consolidation", {"rename": {
        "legacy/transformer": "old/attention",
        "old/attention": "architecture/attention",
        "old/addition": "mid/diffusion",
        "mid/diffusion": "architecture/diffusion",
    }})
    s.append("node_created", _created(
        0, mode="delta", added=["OLD/ADDITION"], removed=["Architecture/Attention"]))

    st = fold(s.read_all())
    assert st.node_concepts[0] == ["architecture/diffusion", "keep/x"]
    assert node_concept_delta(st, 0) == {
        "parent_ids": [],
        "added": ["architecture/diffusion"],
        "removed": ["architecture/attention"],
        "inherited": ["keep/x"],
    }


def test_delta_canonicalizes_a_legacy_full_parent_without_rewriting_it(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, concepts=["Legacy/Transformer", "keep/x"]))
    s.append("concept_consolidation", {
        "rename": {"legacy/transformer": "architecture/attention"}})
    s.append("node_created", _created(
        1, parent_ids=[0], mode="delta", removed=["Architecture/Attention"]))

    st = fold(s.read_all())
    assert st.node_concepts[0] == ["Legacy/Transformer", "keep/x"]  # legacy bytes stay raw
    assert st.node_concepts[1] == ["keep/x"]


def test_legacy_no_mode_preserves_full_and_transitional_delta_behaviour(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["base/x"]})
    s.append("node_created", _created(0, concepts=["legacy/full"]))
    # Compatibility with short-lived 40a5a94: non-empty delta fields existed before mode.
    s.append("node_created", _created(
        1, concepts=["ignored/full"], added=["legacy/delta-add"]))

    st = fold(s.read_all())
    assert st.node_concepts[0] == ["legacy/full"]
    assert st.nodes[0].idea.concept_mode is None
    assert st.node_concepts[1] == ["base/x", "legacy/delta-add"]
    assert st.nodes[1].idea.concept_mode == "delta"


def test_concept_mode_round_trips_through_model_and_event_json():
    idea = Idea(operator="draft", concept_mode="delta", concepts_added=[], concepts_removed=[])
    event = Event(type="node_created", data={
        "node_id": 0,
        "operator": "draft",
        "idea": idea.model_dump(mode="json"),
    })

    decoded = Event.model_validate_json(event.model_dump_json())
    restored = Idea.model_validate(decoded.data["idea"])
    assert decoded.data["idea"]["concept_mode"] == "delta"
    assert restored.model_dump(mode="json") == idea.model_dump(mode="json")
    assert "concept_mode" not in Idea(operator="draft").model_dump(mode="json")


def test_propose_reset_immediately_clears_delta_sidecar_and_membership(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["base/x"]})
    s.append("node_created", _created(0, mode="delta", added=["child/y"]))
    s.append("node_reset", {"node_id": 0, "from_stage": "propose", "generation": 0})

    st = fold(s.read_all())
    assert st.nodes[0].rerun_from == "propose"
    assert 0 not in st.node_concept_deltas
    assert 0 not in st.node_concepts
    assert 0 not in st.node_concept_provenance


def test_same_subject_reemit_preserves_offline_receipt_over_authored_delta(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, mode="delta", added=["authored/x"]))
    s.append("node_concepts", {
        "node_id": 0, "concepts": ["offline/y"], "mode": "offline-heuristic", "generation": 0})
    s.append("node_created", _created(0, mode="delta", added=["authored/x"]))

    st = fold(s.read_all())
    assert st.node_concepts[0] == ["offline/y"]
    assert st.node_concept_provenance[0] == "offline-heuristic"


def test_engine_seeds_run_base_from_first_authored_node_once(tmp_path):
    # The cadence seeds run_base_concepts from the first EVALUATED node's authored concepts, exactly once.
    s = _store(tmp_path)
    s.append("node_created", _created(0, concepts=["loss/dcl", "hyperparameter/temperature"]))
    s.append("node_evaluated", {"node_id": 0, "metric": 0.8})
    st = fold(s.read_all())
    assert not st.run_base_concepts                             # not seeded yet
    st2 = Engine._maybe_seed_run_base_concepts(_seed_host(s), st)
    assert st2.run_base_concepts == ["loss/dcl", "hyperparameter/temperature"]
    runc = [e for e in s.read_all() if e.type == "run_concepts"]
    assert len(runc) == 1
    # Idempotent: with the base now set, a second cadence pass must NOT re-emit.
    st3 = Engine._maybe_seed_run_base_concepts(_seed_host(s), st2)
    assert st3.run_base_concepts == st2.run_base_concepts
    assert len([e for e in s.read_all() if e.type == "run_concepts"]) == 1


def test_engine_seed_is_gated_off_by_flag(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, concepts=["loss/dcl"]))
    s.append("node_evaluated", {"node_id": 0, "metric": 0.8})
    st = fold(s.read_all())
    st2 = Engine._maybe_seed_run_base_concepts(_seed_host(s, concept_run_base=False), st)
    assert not st2.run_base_concepts
    assert not [e for e in s.read_all() if e.type == "run_concepts"]


def test_engine_seed_waits_for_an_evaluated_authored_node(tmp_path):
    # No evaluated node yet -> nothing to seed from (a created-but-unevaluated node does not seed).
    s = _store(tmp_path)
    s.append("node_created", _created(0, concepts=["loss/dcl"]))
    st = fold(s.read_all())
    st2 = Engine._maybe_seed_run_base_concepts(_seed_host(s), st)
    assert not st2.run_base_concepts
    assert not [e for e in s.read_all() if e.type == "run_concepts"]


def test_engine_seed_skips_a_node_whose_concepts_normalize_empty(tmp_path):
    # A [""] authored node must NOT emit EV_RUN_CONCEPTS (it would fold to an empty base and the "base is
    # empty" gate would re-emit every cadence — an engine spin). Seeding waits for a NON-EMPTY normalized set.
    s = _store(tmp_path)
    s.append("node_created", _created(0, concepts=[""]))        # normalizes to empty
    s.append("node_evaluated", {"node_id": 0, "metric": 0.8})
    s.append("node_created", _created(1, concepts=["loss/dcl"]))
    s.append("node_evaluated", {"node_id": 1, "metric": 0.9})
    st = fold(s.read_all())
    st2 = Engine._maybe_seed_run_base_concepts(_seed_host(s), st)
    assert st2.run_base_concepts == ["loss/dcl"]               # seeded from node1, not the empty node0
    assert len([e for e in s.read_all() if e.type == "run_concepts"]) == 1


@pytest.mark.parametrize("partial", [
    [f"axis/c{i:03d}" for i in range(65)],
    ["valid/x", "bad!"],
])
def test_engine_seed_never_launders_partial_membership_into_exact_base(tmp_path, partial):
    s = _store(tmp_path)
    s.append("node_created", _created(0, concepts=partial))
    s.append("node_evaluated", {"node_id": 0, "metric": 0.8})
    s.append("node_created", _created(1, concepts=["exact/y"]))
    s.append("node_evaluated", {"node_id": 1, "metric": 0.9})

    state = fold(s.read_all())
    assert state.node_concept_materialization_receipts[0]["status"] == "partial"
    seeded = Engine._maybe_seed_run_base_concepts(_seed_host(s), state)
    assert seeded.run_base_concepts == ["exact/y"]
    assert seeded.run_base_concept_receipt is None


def test_engine_seed_skips_aborted_evaluated_source(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(0, concepts=["aborted/x"]))
    s.append("node_evaluated", {"node_id": 0, "metric": 0.8})
    s.append("node_abort", {"node_id": 0})
    s.append("node_created", _created(1, concepts=["live/y"]))
    s.append("node_evaluated", {"node_id": 1, "metric": 0.9})

    seeded = Engine._maybe_seed_run_base_concepts(_seed_host(s), fold(s.read_all()))
    assert seeded.run_base_concepts == ["live/y"]


def test_engine_does_not_replace_a_partial_empty_base_event(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["bad!"]})
    s.append("node_created", _created(0, concepts=["exact/x"]))
    s.append("node_evaluated", {"node_id": 0, "metric": 0.8})

    state = fold(s.read_all())
    assert state.run_base_concepts == [] and state.run_base_concept_receipt is not None
    unchanged = Engine._maybe_seed_run_base_concepts(_seed_host(s), state)
    assert unchanged.run_base_concepts == []
    assert len([event for event in s.read_all() if event.type == "run_concepts"]) == 1


def test_unknown_mode_keeps_node_and_propagates_unavailable_receipt(tmp_path):
    s = _store(tmp_path)
    s.append("node_created", _created(
        0, mode="future-v9", concepts=["future/x"], added=["future/y"]))
    s.append("node_created", _created(1, parent_ids=[0], mode="delta", added=["child/z"]))

    st = fold(s.read_all())
    assert sorted(st.nodes) == [0, 1]
    assert 0 not in st.node_concepts
    expected = {"status": "unavailable", "reasons": ["concept_mode_unsupported"]}
    assert st.node_concept_materialization_receipts == {0: expected, 1: expected}
    assert st.node_concepts[1] == []


def test_cycle_descendant_keeps_independent_unavailable_parent_reason():
    events = [Event(type="node_created", data=_created(0, mode="delta")),
              Event(type="node_created", data=_created(1, mode="delta")),
              Event(type="node_created", data=_created(3, mode="future-v9")),
              Event(type="node_created", data=_created(0, parent_ids=[1], mode="delta")),
              Event(type="node_created", data=_created(1, parent_ids=[0], mode="delta")),
              Event(type="node_created", data=_created(
                  2, parent_ids=[1, 3], mode="delta"))]
    state = fold(events)
    assert state.node_concept_materialization_receipts[2] == {
        "status": "unavailable",
        "reasons": ["concept_mode_unsupported", "delta_dependency_cycle"],
    }


def test_cycle_receipt_keeps_own_and_partial_parent_rename_causes():
    state = RunState(
        nodes={
            0: Node(id=0, parent_ids=[1, 2], operator="draft", idea=Idea(
                operator="draft", concept_mode="delta")),
            1: Node(id=1, parent_ids=[0], operator="draft", idea=Idea(
                operator="draft", concept_mode="delta")),
            2: Node(id=2, operator="draft", idea=Idea(operator="draft")),
        },
        concept_consolidation={"a/x": "a/y", "a/y": "a/x"},
        node_concepts={2: ["a/x", *[f"wide/c{i:02d}" for i in range(64)]]},
        node_concept_provenance={0: "researcher-authored", 1: "researcher-authored",
                                 2: "classifier"},
        node_concept_deltas={
            0: {"added": ["a/x"], "removed": []},
            1: {"added": [], "removed": []},
        },
    )
    _materialize_concept_deltas(state, capped_inputs={2})
    assert state.node_concept_materialization_receipts[0] == {
        "status": "unavailable",
        "reasons": ["delta_dependency_cycle", "rename_cycle", "concepts_per_node_cap"],
    }


def test_missing_and_reset_window_parents_are_typed_unavailable(tmp_path):
    missing = RunState(
        nodes={1: Node(id=1, parent_ids=[99], operator="draft", idea=Idea(
            operator="draft", concept_mode="delta"))},
        node_concept_deltas={1: {"added": [], "removed": []}},
        node_concept_provenance={1: "researcher-authored"},
    )
    _materialize_concept_deltas(missing)
    assert missing.node_concept_materialization_receipts[1] == {
        "status": "unavailable", "reasons": ["delta_dependency_missing_parent"]}

    s = _store(tmp_path)
    s.append("node_created", _created(0, mode="full", concepts=["parent/x"]))
    s.append("node_created", _created(1, parent_ids=[0], mode="delta"))
    s.append("node_reset", {"node_id": 0, "from_stage": "propose", "generation": 0})
    reset_window = fold(s.read_all())
    assert reset_window.node_concepts[1] == []
    assert reset_window.node_concept_materialization_receipts[1] == {
        "status": "unavailable",
        "reasons": ["delta_dependency_unknown_parent_membership"],
    }

    untrusted = RunState(
        nodes={
            0: Node(id=0, operator="draft", idea=Idea(operator="draft")),
            1: Node(id=1, parent_ids=[0], operator="draft", idea=Idea(
                operator="draft", concept_mode="delta")),
        },
        node_concepts={0: ["future/x"]},
        node_concept_provenance={0: "untrusted-source", 1: "researcher-authored"},
        node_concept_deltas={1: {"added": [], "removed": []}},
    )
    _materialize_concept_deltas(untrusted)
    assert untrusted.node_concept_materialization_receipts[1] == {
        "status": "unavailable",
        "reasons": ["delta_dependency_unknown_parent_membership"],
    }


def test_rename_cycle_is_partial_and_propagates_valid_subset(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["cycle/a", "keep/x"]})
    s.append("concept_consolidation", {"rename": {"cycle/a": "cycle/b", "cycle/b": "cycle/a"}})
    s.append("node_created", _created(0, mode="delta"))
    s.append("node_created", _created(1, parent_ids=[0], mode="delta", added=["child/y"]))

    st = fold(s.read_all())
    assert st.node_concepts == {0: ["keep/x"], 1: ["child/y", "keep/x"]}
    expected = {"status": "partial", "reasons": ["rename_cycle"]}
    assert st.node_concept_materialization_receipts == {0: expected, 1: expected}


def test_materialized_membership_is_bounded_through_deep_and_wide_dags():
    nodes: dict[int, Node] = {}
    memberships: dict[int, list[str]] = {}
    provenance: dict[int, str] = {}
    for node_id in range(1_000):
        nodes[node_id] = Node(id=node_id, operator="draft", idea=Idea(operator="draft"))
        memberships[node_id] = [f"p{node_id:04d}/c{index:02d}" for index in range(64)]
        provenance[node_id] = "researcher-authored"
    merge_id = 1_000
    nodes[merge_id] = Node(
        id=merge_id, parent_ids=list(range(1_000)), operator="merge",
        idea=Idea(operator="merge", concept_mode="delta"))
    st = RunState(
        nodes=nodes,
        node_concepts=memberships,
        node_concept_provenance={**provenance, merge_id: "researcher-authored"},
        node_concept_deltas={merge_id: {"added": [], "removed": []}},
    )
    _materialize_concept_deltas(st)
    assert len(st.node_concepts[merge_id]) == 64
    assert st.node_concept_materialization_receipts[merge_id] == {
        "status": "partial", "reasons": ["concepts_per_node_cap"]}

    events = [Event(type="run_concepts", data={"concepts": []})]
    for node_id in range(200):
        events.append(Event(type="node_created", data=_created(
            node_id, parent_ids=[] if node_id == 0 else [node_id - 1],
            mode="delta", added=[f"lineage/c{node_id:03d}"])))
    deep = fold(events)
    assert max(map(len, deep.node_concepts.values())) == 64
    assert deep.node_concept_materialization_receipts[199]["reasons"] == ["concepts_per_node_cap"]


@pytest.mark.parametrize("producer", ["classifier", "operator"])
@pytest.mark.parametrize("bad_kind", ["future", "cap", "invalid"])
def test_protected_full_receipt_survives_same_subject_reemit(tmp_path, producer, bad_kind):
    s = _store(tmp_path)
    original = _created(0, mode="delta", added=["authored/x"])
    s.append("node_created", original)
    tags = [f"tag/c{i:02d}" for i in range(65)] if bad_kind == "cap" else ["trusted/x"]
    if bad_kind == "invalid":
        tags.append("bad!")
    if producer == "classifier":
        s.append("node_concepts", {
            "node_id": 0, "concepts": tags, "mode": "llm", "generation": 0})
    else:
        s.append("concept_tag_edited", {
            "node_id": 0, "concepts": tags, "node_generation": 0})
    replacement = _created(0, mode="future-v2" if bad_kind == "future" else "delta",
                           added=["authored/x"])
    if bad_kind == "cap":
        replacement["idea"]["concepts_added"] = [f"ignored/c{i:02d}" for i in range(65)]
    elif bad_kind == "invalid":
        replacement["idea"]["concepts_added"] = ["bad!"]
    s.append("node_created", replacement)

    st = fold(s.read_all())
    expected_provenance = "classifier" if producer == "classifier" else "operator-edited"
    assert st.node_concept_provenance[0] == expected_provenance
    expected_reasons = (["concepts_per_node_cap"] if len(tags) > 64 else
                        ["invalid_concept_id"] if "bad!" in tags else [])
    if expected_reasons:
        assert st.node_concept_materialization_receipts[0] == {
            "status": "partial", "reasons": expected_reasons}
    else:
        assert 0 not in st.node_concept_materialization_receipts


def test_invalid_only_transitional_delta_inherits_base_with_partial_receipt(tmp_path):
    s = _store(tmp_path)
    s.append("run_concepts", {"concepts": ["base/x"]})
    s.append("node_created", _created(0, added=["bad!"]))
    st = fold(s.read_all())
    assert st.nodes[0].idea.concept_mode == "delta"
    assert st.node_concepts[0] == ["base/x"]
    assert st.node_concept_materialization_receipts[0] == {
        "status": "partial", "reasons": ["invalid_concept_id"]}


def test_bounded_run_base_persists_receipt_and_round_trips():
    raw = [f"base/c{i:03d}" for i in range(100)] + ["bad!"]
    state = fold([Event(type="run_concepts", data={"concepts": raw})])
    assert len(state.run_base_concepts) == 64
    assert state.run_base_concept_receipt == {
        "status": "partial",
        "reasons": ["invalid_concept_id", "concepts_per_node_cap"],
    }
    restored = RunState.model_validate_json(state.model_dump_json())
    assert restored.run_base_concept_receipt == state.run_base_concept_receipt
