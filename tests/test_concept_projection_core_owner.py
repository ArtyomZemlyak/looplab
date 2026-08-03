"""`search/concept_projection.py` has exactly one concept-identity implementation (doc 25 SE-05).

The module shipped with its imports wrapped in `try/except ModuleNotFoundError` and a full parallel
implementation behind them — a rename hop-cap walker, a hand-rolled consolidation-map normalizer, a
bare-string receipt compatibility branch — under the comment "the owner lands in the paired core
commit; keep this commit testable alone".

`looplab/core/concepts.py` has been in-tree and imported UNCONDITIONALLY by the sibling modules
(`concept_graph.py`) for a long time, so none of those branches could execute. What they cost was not
runtime: it was that every read of this module required mentally diffing two implementations of an
identity projection whose disagreements are silent — a rename resolved one way here and another way
in `core.concepts` changes which concepts a proposal inherits, with no error anywhere.

These tests pin the behaviours the deleted branches were shadowing, so the deletion is provably a
deletion of DEAD code and not of a second opinion that happened to be right sometimes.
"""
from __future__ import annotations

import pytest

from looplab.core import concepts as core_concepts
from looplab.search import concept_projection


def test_the_core_owner_is_imported_unconditionally():
    """The guard on the finding itself: a re-introduced try/except would restore the lattice."""
    import inspect

    source = inspect.getsource(concept_projection)
    assert "ModuleNotFoundError" not in source, (
        "the 'paired core commit' fallback lattice came back; core.concepts is in-tree and the "
        "sibling modules import it unconditionally")
    assert "paired core commit" not in source


def test_no_second_implementation_of_the_identity_projection_remains():
    import inspect

    source = inspect.getsource(concept_projection)
    for gone in ("_fallback_resolve", "_RENAME_HOP_CAP", "_CORE_MATERIALIZATION_REASONS"):
        assert gone not in source, f"{gone} belonged to the fallback implementation"


# ------------------------------------------------------------------ rename resolution

@pytest.mark.parametrize("raw,rename,expected", [
    ("loss/contrastive", {}, ("loss/contrastive", None)),
    ("loss/a", {"loss/a": "loss/b"}, ("loss/b", None)),
    ("loss/a", {"loss/a": "loss/b", "loss/b": "loss/c"}, ("loss/c", None)),
])
def test_a_rename_chain_resolves_to_its_endpoint(raw, rename, expected):
    assert concept_projection.canonical_recorded_concept(raw, rename) == expected


def test_a_rename_cycle_is_refused_rather_than_walked_forever():
    concept_id, problem = concept_projection.canonical_recorded_concept(
        "loss/a", {"loss/a": "loss/b", "loss/b": "loss/a"})
    assert concept_id is None and problem


def test_an_unnormalizable_id_is_refused():
    concept_id, problem = concept_projection.canonical_recorded_concept("not a concept id!!", {})
    assert concept_id is None and problem


def test_resolution_agrees_with_the_core_owner_exactly():
    """The deleted walker and `core.concepts.resolve_concept` were two answers to one question. Now
    there is one — asserted rather than assumed, because a wrapper that dropped the reason or
    stringified it differently would be a silent behaviour change at this boundary."""
    rename = {"loss/a": "loss/b", "reg/x": "reg/y", "cycle/p": "cycle/q", "cycle/q": "cycle/p"}
    for raw in ("loss/a", "reg/x", "cycle/p", "arch/moe", "!!bad!!", "", None, 7):
        core_id, core_reason = core_concepts.resolve_concept(raw, rename)
        projected_id, projected_reason = concept_projection.canonical_recorded_concept(raw, rename)
        assert projected_id == core_id
        assert projected_reason == (str(core_reason) if core_reason is not None else None)


# ------------------------------------------------------------------ consolidation-map corruption

def test_a_benign_collapse_is_not_reported_as_corruption():
    """Two raw spellings that normalize to the SAME source id collapse BY DESIGN. The old
    `len(projected) != len(raw)` heuristic called that corruption and poisoned `global_reasons`
    run-wide — every node_concept_delta went unavailable and delta_safe was False for the whole run.
    The core helper's own signals are what replaced it, and only one implementation now reads them."""
    projected, reasons = concept_projection._rename_projection(
        {"reg dropout": "reg/x", "reg-dropout": "reg/x"})
    assert "invalid_consolidation_map" not in reasons
    assert projected


@pytest.mark.parametrize("raw", [{"!!bad!!": "loss/b"}, {"loss/a": "!!bad!!"}, {"loss/a": None},
                                 "not a mapping", 7])
def test_a_genuinely_corrupt_map_is_flagged(raw):
    _projected, reasons = concept_projection._rename_projection(raw)
    assert "invalid_consolidation_map" in reasons


def test_an_absent_map_is_neither_corrupt_nor_a_projection():
    assert concept_projection._rename_projection(None) == ({}, set())


# ------------------------------------------------------------------ the receipt boundary

def test_only_the_typed_envelope_crosses_the_receipt_boundary():
    assert concept_projection._materialization_receipt(
        {"status": "unavailable", "reasons": ["delta_dependency_cycle"]}) == (
            "unavailable", ("delta_dependency_cycle",))


def test_a_bare_reason_string_fails_closed_even_when_it_is_a_RECOGNIZED_reason():
    """This is the branch the deletion actually removes behaviour-wise on paper, so it gets its own
    test: the fallback accepted a bare recognized reason and synthesized `status="unavailable"`
    around it. The core owner does not, and must not — a receipt that lost its status is not evidence
    that the materialization was merely partial, and inventing one downgrades a hard failure into a
    softer story the reader will believe.
    """
    for reason in sorted(core_concepts.CONCEPT_MATERIALIZATION_REASONS):
        assert concept_projection._materialization_receipt(reason) is None


@pytest.mark.parametrize("raw", [None, {}, {"status": "complete", "reasons": ["x"]},
                                 {"status": "partial", "reasons": []},
                                 {"status": "partial", "reasons": ["", "x"]},
                                 {"status": "partial"}, {"reasons": ["x"]}, "not a receipt", 7])
def test_a_malformed_receipt_is_no_receipt(raw):
    assert concept_projection._materialization_receipt(raw) is None
