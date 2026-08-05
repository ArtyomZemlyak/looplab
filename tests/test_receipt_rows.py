"""A health receipt must survive every projection of the rows it describes (doc 25 EM-09).

Three snapshot types carry a receipt saying whether a durable cross-run store was read COMPLETELY:
`_ClaimSourceRows` and `_ClaimAssessmentRows` (lessons/research evidence) and `_CapsuleRows`
(concept capsules). They exist for one reason: a quarantined or unreadable store must not be
reportable as an empty one. "No claim opposes this" and "the claim file could not be read" are
different answers, and only the receipt distinguishes them.

The failure mode is silent by construction. Slicing a list subclass, or rebuilding it with a
comprehension, yields a bare `list`; every consumer then probes `getattr(rows, "<receipt>", None)`,
reads absence, and falls back to a DEFAULT that says the source was complete. Nothing raises. So
these tests drive the real projections with an UNHEALTHY receipt and check the quarantine is still
there afterwards, plus a two-way registry pin (`CARRIED_FIELDS` / `__slots__` / the assignments in
`__init__`) so a receipt field added to one of these types cannot be left out of the carry set.
"""
from __future__ import annotations

import ast

import pytest

from _source_scan import function_tree

from looplab.core.receipts import ReceiptRows
from looplab.engine import claims_health, concept_capsules  # noqa: F401  (registers the subclasses)
from looplab.engine.claims_health import (
    ClaimEvidenceSources,
    _ClaimAssessmentRows,
    _ClaimSourceRows,
    _filter_claim_assessments,
    _filter_claim_source_rows,
)
from looplab.engine.concept_capsules import _CapsuleRows, _filter_capsule_rows

# --------------------------------------------------------------------------- #
# fixtures: one UNHEALTHY receipt per type, so "the receipt survived" is distinguishable from
# "a fresh default receipt was synthesized" — the two are identical on a healthy source.
# --------------------------------------------------------------------------- #

_QUARANTINED_READ_HEALTH = {
    "v": 1, "read_complete": False,
    "lessons": {"read_complete": False, "rows_total": 4, "rows_retained": 2,
                "rows_quarantined": 2, "malformed_rows": 1, "invalid_rows": 1},
    "research": {"read_complete": True, "rows_total": 0, "rows_retained": 0,
                 "rows_quarantined": 0, "malformed_rows": 0, "invalid_rows": 0},
}

_QUARANTINED_CAPSULE_HEALTH = {
    "source_store_complete": False, "source_rows_total": 5, "source_rows_quarantined": 3,
    "source_malformed_rows": 2, "source_invalid_capsule_rows": 1, "source_duplicate_run_rows": 0,
}

# A CURRENT (v1) aggregate receipt, i.e. the shape every live producer emits. Legacy producer-only
# receipts are deliberately not used here: `_safe_research_source_summary` normalizes those to
# `read_health_v: 0` and then rejects its own output on a second pass, so a legacy receipt is dropped
# by any re-sanitizing projection. That asymmetry predates EM-09 and is unreachable in production
# (`_research_source_summary` always stamps the current version); this change preserves it exactly
# rather than quietly altering a receipt rule, so the fixture uses the shape the live path produces.
_UNKNOWN_RESEARCH_SOURCE = {
    "source_complete": False, "producer_receipt_known": False, "producer_complete": False,
    "producer_runs": 2, "producer_partial_runs": 0, "producer_unknown_runs": 1,
    "producer_claims_total": 0, "producer_claims_retained": 0, "producer_claims_omitted": 0,
    "read_health_v": 1, "read_complete": False, "rows_total": 3, "rows_retained": 1,
    "rows_quarantined": 2, "malformed_rows": 2, "invalid_rows": 0,
    "snapshot_digest": "a" * 64,
}

_UNKNOWN_CLAIM_SOURCE = {
    "v": 1, "receipt_known": False, "source_complete": False, "read_complete": False,
    "research_source_complete": False,
    "lessons": {"read_complete": False, "rows_total": 3, "rows_retained": 1,
                "rows_quarantined": 2, "malformed_rows": 2, "invalid_rows": 0},
    "research": {"read_complete": True, "rows_total": 0, "rows_retained": 0,
                 "rows_quarantined": 0, "malformed_rows": 0, "invalid_rows": 0},
    "snapshot_digest": "",
}

_LESSON = {"task_id": "t1", "statement": "adding dropout helped"}
_OTHER_LESSON = {"task_id": "t2", "statement": "a different task's lesson"}
_CAPSULE = {"task_id": "t1", "run_id": "r1"}
_OTHER_CAPSULE = {"task_id": "t2", "run_id": "r2"}


def _unhealthy_snapshots():
    """One instance of every registered type, each carrying a receipt that says NOT complete."""
    return [
        _ClaimSourceRows([_LESSON, _OTHER_LESSON], read_health=_QUARANTINED_READ_HEALTH),
        _ClaimAssessmentRows(
            [{"statement": "adding dropout helped", "task_id": "t1"},
             {"statement": "a different task's claim", "task_id": "t2"}],
            claim_source=_UNKNOWN_CLAIM_SOURCE, research_source=_UNKNOWN_RESEARCH_SOURCE,
            evidence_sources=ClaimEvidenceSources(
                lessons=[_LESSON], research_claims=[], decisions={})),
        _CapsuleRows([_CAPSULE, _OTHER_CAPSULE], source_health=_QUARANTINED_CAPSULE_HEALTH),
    ]


def _ids(snapshots):
    return [type(snapshot).__name__ for snapshot in snapshots]


# --------------------------------------------------------------------------- #
# the registry: every carried field is declared, slotted, and constructible
# --------------------------------------------------------------------------- #

def test_every_receipt_carrying_list_is_a_registered_subclass():
    """A fourth "list plus a receipt" type must join this base, not re-derive the carrying rules.

    EM-09 found three copies; the point of the base is that there is now one place where "a
    projection keeps the receipt" is decided, so a new snapshot type showing up here is a review
    trigger rather than a silent fourth copy.
    """
    assert {cls.__name__ for cls in ReceiptRows.__subclasses__()} == {
        "_ClaimSourceRows", "_ClaimAssessmentRows", "_CapsuleRows"}


@pytest.mark.parametrize("cls", [_ClaimSourceRows, _ClaimAssessmentRows, _CapsuleRows],
                         ids=lambda cls: cls.__name__)
def test_carried_fields_match_the_slots_and_the_constructor(cls):
    """`CARRIED_FIELDS` is the registry `filter`/`map`/slicing rebuild from, so it must equal both
    what `__init__` actually assigns and what the class permits to exist. AST, not substrings: a
    commented-out assignment is not an `ast.Assign` node."""
    tree = function_tree(cls.__init__)
    assigned = {
        target.attr
        for node in ast.walk(tree) if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }
    carried = set(cls.CARRIED_FIELDS)
    assert carried == assigned, (
        f"{cls.__name__}.CARRIED_FIELDS {sorted(carried)} != the attributes __init__ sets "
        f"{sorted(assigned)}; an unregistered field is dropped by every projection")
    assert set(cls.__slots__) == carried, (
        f"{cls.__name__}.__slots__ must be exactly CARRIED_FIELDS, or an undeclared receipt can be "
        "stashed on an instance again")
    kwonly = {arg.arg for arg in tree.body[0].args.kwonlyargs}
    assert carried <= kwonly, (
        f"{cls.__name__} carries {sorted(carried - kwonly)} but its __init__ cannot take them back "
        "as keywords — every projection rebuilds through the constructor")


def test_the_base_declares_no_instance_dict():
    """`ReceiptRows.__slots__` must stay empty-but-present: a base that omits `__slots__` gives every
    subclass a `__dict__` back, and then the subclass slots stop preventing anything."""
    assert ReceiptRows.__slots__ == ()


# --------------------------------------------------------------------------- #
# the projections: the three operations that used to drop the receipt
# --------------------------------------------------------------------------- #

def _every_attribute(snapshot) -> dict:
    """Read the instance by its SLOTS, not by `CARRIED_FIELDS`.

    Deliberately not `snapshot.carried()`: a test phrased in terms of the registry cannot notice a
    field that was slotted and assigned but never registered, which is the exact drift the registry
    is there to prevent. The slots are the class's own statement of what an instance holds.
    """
    return {name: getattr(snapshot, name) for name in type(snapshot).__slots__}


@pytest.mark.parametrize("snapshot", _unhealthy_snapshots(), ids=_ids(_unhealthy_snapshots()))
def test_filter_map_and_slice_carry_every_receipt(snapshot):
    """The named hazard: a plain-list projection loses the receipt and the store reads as complete."""
    expected = _every_attribute(snapshot)
    assert expected, "a snapshot type with nothing to carry does not belong on this base"

    narrowed = snapshot.filter(lambda row: row.get("task_id") == "t1")
    assert type(narrowed) is type(snapshot)
    assert len(narrowed) == 1 and narrowed[0]["task_id"] == "t1"
    assert _every_attribute(narrowed) == expected

    mapped = snapshot.map(lambda row: {**row, "extra": 1})
    assert type(mapped) is type(snapshot)
    assert all(row["extra"] == 1 for row in mapped)
    assert _every_attribute(mapped) == expected

    sliced = snapshot[:1]
    assert type(sliced) is type(snapshot), (
        "a slice of a list subclass is a bare list unless __getitem__ says otherwise — and a "
        "display cap is spelled as a slice")
    assert len(sliced) == 1
    assert _every_attribute(sliced) == expected

    # an integer index is still just the row
    assert snapshot[0] == list(snapshot)[0]

    # an EMPTY projection is the case the receipt matters most for: zero rows plus a lost receipt is
    # exactly the "the store is empty" answer these types exist to refuse.
    emptied = snapshot.filter(lambda row: False)
    assert list(emptied) == [] and _every_attribute(emptied) == expected


@pytest.mark.parametrize("snapshot", _unhealthy_snapshots(), ids=_ids(_unhealthy_snapshots()))
def test_an_undeclared_attribute_cannot_be_stashed_on_a_snapshot(snapshot):
    """The other half of EM-09: callers used to monkey-set evidence onto the returned list, which no
    projection carried. `__slots__` makes that a TypeError-shaped failure at the assignment instead
    of a silent disappearance three frames later."""
    with pytest.raises(AttributeError):
        snapshot.lessons_snapshot = [{"statement": "stashed"}]
    assert not hasattr(snapshot, "__dict__")


# --------------------------------------------------------------------------- #
# the production filter helpers — what a scope boundary actually calls
# --------------------------------------------------------------------------- #

def test_scoping_claim_source_rows_keeps_the_quarantine():
    rows = _ClaimSourceRows([_LESSON, _OTHER_LESSON], read_health=_QUARANTINED_READ_HEALTH)
    scoped = _filter_claim_source_rows(rows, lambda row: row["task_id"] == "t1", research=False)
    assert list(scoped) == [_LESSON]
    assert scoped.read_health["read_complete"] is False
    assert scoped.read_health["lessons"] == _QUARANTINED_READ_HEALTH["lessons"]


def test_scoping_capsule_rows_keeps_the_quarantine():
    rows = _CapsuleRows([_CAPSULE, _OTHER_CAPSULE], source_health=_QUARANTINED_CAPSULE_HEALTH)
    scoped = _filter_capsule_rows(rows, lambda row: row["task_id"] == "t1")
    assert list(scoped) == [_CAPSULE]
    assert scoped.source_health == _QUARANTINED_CAPSULE_HEALTH


def test_filtering_assessments_keeps_the_aggregate_authority():
    rows = _ClaimAssessmentRows(
        [{"statement": "a", "maturity": "machine-proposed"},
         {"statement": "b", "maturity": "operator-rejected"}],
        claim_source=_UNKNOWN_CLAIM_SOURCE, research_source=_UNKNOWN_RESEARCH_SOURCE)
    live = _filter_claim_assessments(rows, lambda row: row["maturity"] != "operator-rejected")
    assert [row["statement"] for row in live] == ["a"]
    assert live.claim_source["receipt_known"] is False
    assert live.research_source["producer_unknown_runs"] == 1


def test_a_plain_list_of_assessments_carries_no_forged_aggregate():
    """`None` is the third state, and it is NOT interchangeable with a default receipt: the retrieval
    layer reads it as "no aggregate was carried, fall back to the per-row copies", whose own fallback
    is fail-closed. Synthesizing a complete-looking receipt here would defeat that."""
    filtered = _filter_claim_assessments([{"statement": "a"}], lambda row: True)
    assert filtered.claim_source is None and filtered.research_source is None


# --------------------------------------------------------------------------- #
# end to end: a real store with a real unreadable line
# --------------------------------------------------------------------------- #

def test_a_malformed_research_line_survives_the_load_projection_and_the_scope_filter(tmp_path):
    """Drives the whole path the `map` call site sits on: a durable file with one unparseable line is
    read, row-projected, scoped to a task, and assessed — and the answer still says the source was
    not completely read, rather than reporting the surviving rows as the whole truth."""
    from looplab.engine.claims import claims_for_memory, load_research_claims

    (tmp_path / "research_claims.jsonl").write_text(
        '{"statement": "cosine schedule helped", "run_id": "r1", "task_id": "t1", '
        '"node_ids": [1], "direction": "min"}\n'
        "{ this line is not json\n",
        encoding="utf-8")

    loaded = load_research_claims(tmp_path)
    assert [row["statement"] for row in loaded] == ["cosine schedule helped"]
    assert loaded.read_health["research"]["malformed_rows"] == 1
    assert loaded.read_health["read_complete"] is False

    scoped = _filter_claim_source_rows(loaded, lambda row: row["task_id"] == "t1", research=True)
    assert len(scoped) == 1
    assert scoped.read_health["read_complete"] is False, (
        "scoping to the row's OWN task must not repair the file's quarantine")

    assessed = claims_for_memory(tmp_path, scope_task="t1", structured=True)
    assert assessed.claim_source["read_complete"] is False
    assert assessed.claim_source["source_complete"] is False


# --------------------------------------------------------------------------- #
# the evidence-source attachment (the ex-monkey-set half)
# --------------------------------------------------------------------------- #

def test_the_decision_validator_is_handed_the_exact_snapshots_it_was_projected_from(tmp_path):
    """`record_claim_decision` projects the evidence under its lock chain and the validator then
    re-projects the SAME inputs under a decision-specific scope. Those snapshots ride on a declared
    field now; if they arrive as `None` the validator dereferences `None` and raises, which is the
    loud failure the old missing-attribute access gave — never a silent re-read of the files the
    lock chain exists to freeze."""
    from looplab.engine.claims import record_claim_decision

    (tmp_path / "lessons.jsonl").write_text(
        '{"statement": "adding dropout helped", "task_id": "t1"}\n', encoding="utf-8")
    seen = []

    def _capture(snapshot):
        sources = snapshot.evidence_sources
        assert isinstance(sources, ClaimEvidenceSources)
        # the loaded snapshots themselves, receipts attached — not re-read plain lists
        assert [row["statement"] for row in sources.lessons] == ["adding dropout helped"]
        assert sources.lessons.read_health["read_complete"] is True
        assert sources.research_claims.read_health["read_complete"] is True
        assert isinstance(sources.decisions, dict)
        seen.append(sources)

    record_claim_decision(tmp_path, statement="adding dropout helped", scope="t1",
                          decision="ratified", validate_evidence=_capture)
    assert len(seen) == 1
    # and the attachment survives a projection of the assessments, unlike a stashed attribute
    assert _ClaimAssessmentRows(
        [], evidence_sources=seen[0]).filter(lambda row: True).evidence_sources is seen[0]
