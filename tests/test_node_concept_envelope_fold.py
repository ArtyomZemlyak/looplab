"""The concept envelope is folded by its own function, and it needs the PRE-rebind node (doc 25 EV-08).

`_on_node_created` was a 226-line handler mixing node lifecycle with ~110 lines of concept-envelope
policy — mode discrimination, transitional canonicalization, receipt protection, and writes to four
sidecar maps plus four `_FoldCtx` sets. That block is now `_fold_node_concept_envelope`, next to the
other concept-membership writers.

The split has one load-bearing subtlety, and this file exists for it. Receipt protection compares the
REPLACED node's idea with the new one: a same-subject re-emission keeps an existing CLASSIFIER /
OPERATOR / OFFLINE receipt, while a genuine subject CHANGE clears it. The old node is captured by the
caller BEFORE `st.nodes[n.id]` is rebound, so the helper takes it as a parameter. Re-deriving it
inside — the obvious "simplification" now that the block is a function with `st` in scope — would
compare the new node with ITSELF, report every replacement as unchanged, and silently preserve a
classifier receipt describing an idea that no longer exists.

That direction had NO test. `test_concept_tag_edited.py` covers the RETAIN half
(`test_operator_edit_survives_implement_reset_and_node_created_reemit`); nothing drove a replacement
whose idea genuinely changed.
"""
from __future__ import annotations

import ast
import inspect

from looplab.core.models import (NODE_CONCEPT_PROVENANCE_AUTHORED,
                                 NODE_CONCEPT_PROVENANCE_CLASSIFIER)
from looplab.events import replay
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _tagged_node(tmp_path, concepts=("loss/a",)):
    """A node whose membership carries an INDEPENDENT classifier receipt."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {},
                                           "rationale": "the original idea",
                                           "concepts": list(concepts)}})
    store.append("node_concepts", {"node_id": 0, "concepts": ["classifier/tag"], "mode": "llm",
                                   "generation": 0})
    return store


# ------------------------------------------------------------------ the pre-rebind node matters

def test_a_replacement_with_a_different_idea_clears_the_classifier_receipt(tmp_path):
    """The subject CHANGED, so the independent classification describes an idea that is gone.

    This is what the `current` parameter buys. A helper that re-read `st.nodes.get(n.id)` after the
    rebind would compare the new node with itself, find them equal, and keep the stale receipt —
    which then feeds novelty admission and cross-run evidence as if it had been verified.
    """
    store = _tagged_node(tmp_path)
    before = fold(store.read_all())
    assert before.node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_CLASSIFIER, "precondition"

    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {},
                                           "rationale": "a genuinely different idea"}})
    after = fold(store.read_all())

    assert after.node_concept_provenance.get(0) != NODE_CONCEPT_PROVENANCE_CLASSIFIER, (
        "a replacement node kept the previous idea's classifier receipt")
    assert after.node_concepts.get(0) in (None, []), (
        "the stale classified membership survived the subject change")


def test_a_same_subject_re_emission_keeps_the_classifier_receipt(tmp_path):
    """The other half of the truth table, so the test above cannot be satisfied by clearing always.

    An implement/eval reset re-emits `node_created` for the UNCHANGED idea; the receipt describes
    that same idea and stands.
    """
    store = _tagged_node(tmp_path)
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {},
                                           "rationale": "the original idea",
                                           "concepts": ["loss/a"]}})
    after = fold(store.read_all())

    assert after.node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_CLASSIFIER
    assert after.node_concepts[0] == ["classifier/tag"]


def test_the_proposer_concept_envelope_alone_does_not_count_as_a_subject_change(tmp_path):
    """Subject equality deliberately EXCLUDES the proposer's own concept fields: the independent
    tagger read none of them, so a re-emission that only re-authors the taxonomy must not invalidate
    an evidence receipt."""
    store = _tagged_node(tmp_path)
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {},
                                           "rationale": "the original idea",
                                           "concepts": ["loss/a", "arch/moe"]}})

    assert fold(store.read_all()).node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_CLASSIFIER


def test_an_authored_membership_still_lands_on_a_first_create(tmp_path):
    """The ordinary path through the extracted helper: no prior node, no receipt to protect."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {},
                                           "concepts": ["loss/a"]}})
    st = fold(store.read_all())

    assert st.node_concepts[0] == ["loss/a"]
    assert st.node_concept_provenance[0] == NODE_CONCEPT_PROVENANCE_AUTHORED


def test_a_delta_envelope_still_reaches_the_delta_sidecar(tmp_path):
    """`concept_mode="delta"` writes the deltas sidecar rather than the membership map; the fold
    post-pass resolves it topologically. Driven here because the mode discrimination moved."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {},
                                           "concepts": ["loss/a"]}})
    store.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "mutate",
                                  "idea": {"operator": "mutate", "params": {},
                                           "concept_mode": "delta",
                                           "concepts_added": ["arch/moe"],
                                           "concepts_removed": ["loss/a"]}})
    st = fold(store.read_all())

    assert st.node_concept_deltas[1] == {"added": ["arch/moe"], "removed": ["loss/a"]}
    assert st.node_concept_provenance[1] == NODE_CONCEPT_PROVENANCE_AUTHORED


# ------------------------------------------------------------------ the split itself

def test_the_lifecycle_handler_no_longer_folds_the_concept_envelope():
    source = inspect.getsource(replay._on_node_created)
    for spelling in ("node_concept_provenance", "node_concept_deltas", "node_concepts_at_vocab",
                     "concept_mode", "receipt_protected"):
        assert spelling not in source, (
            f"_on_node_created folds the concept envelope again ({spelling!r}); the ~110-line "
            "sub-machine belongs in _fold_node_concept_envelope")
    assert "_fold_node_concept_envelope(" in source, "the handler stopped delegating"


def test_the_envelope_helper_does_not_read_the_node_map():
    """What makes hoisting `st.nodes[n.id] = n` above the call safe. If the helper started reading
    `st.nodes`, it would observe the ALREADY-REBOUND node, and the hoist would stop being a pure
    reordering — the same aliasing that makes re-deriving `current` wrong."""
    tree = ast.parse(inspect.getsource(replay._fold_node_concept_envelope).lstrip())
    reads = [node for node in ast.walk(tree)
             if isinstance(node, ast.Attribute) and node.attr == "nodes"
             and isinstance(node.value, ast.Name) and node.value.id == "st"]
    assert reads == [], "_fold_node_concept_envelope reads st.nodes; `current` is its only view of it"


def test_the_helper_takes_the_replaced_node_rather_than_re_deriving_it():
    """A signature guard, because the behavioural test above is the only thing standing between the
    codebase and an inviting one-line 'cleanup'."""
    params = [a.arg for a in
              ast.parse(inspect.getsource(replay._fold_node_concept_envelope).lstrip()).body[0].args.args]
    assert params == ["st", "ctx", "n", "d", "current"], params
