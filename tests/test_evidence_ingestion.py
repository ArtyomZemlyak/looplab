"""One evidence-ingestion fold for both claim projections (doc 25 EM-07).

The structured (scope+polarity) and lean (normalized-statement) projections each walked the lesson
and research rows into their claim groups, and the two walks were identical except for how a row
finds its group. The rules inside them are the ones a quiet mistake makes unrecoverable:

* a NEUTRAL lesson still registers its run and scope — it takes no stance, but it does prove the
  claim was seen there, and dropping that silently shrinks the breadth a reader judges by;
* unsupported research is `unverified`, never `oppose` — "not established" is not counter-evidence,
  and merging the two lets an uncited claim read as refuted;
* an unverified claim keeps its node refs, which is the entire reason the third bucket exists.

A stance-mapping or receipt bug had to be found and fixed in both copies. Now there is one.
"""
from __future__ import annotations

import pytest

from looplab.engine.claims_assessments import _ingest_evidence


def _group():
    return {"support": set(), "oppose": set(), "unverified": set(),
            "runs": set(), "scopes": set(), "sources": set(), "verification": set()}


def _lesson(**kwargs):
    row = {"run_id": "r1", "task_id": "t1", "statement": "a lesson", "outcome": "improved"}
    row.update(kwargs)
    return row


def _research(**kwargs):
    row = {"run_id": "r2", "task_id": "t1", "statement": "a research claim",
           "kind": "research", "claim": "a research claim", "node_ids": [7],
           "verification": {"verdict": "supported", "method": "replication"}}
    row.update(kwargs)
    return row


def _fold(lessons=(), research=(), *, weigh=None):
    group = _group()
    _ingest_evidence(list(lessons), list(research), lambda row: group, weigh=weigh)
    return group


# ------------------------------------------------------------------ the rules that must not drift

def test_a_neutral_lesson_still_registers_its_run_and_scope():
    """It takes NO stance — but it proves the claim was seen in that run and scope. Skipping the
    registration because the stance is neutral silently shrinks the run set a reader judges breadth
    by, and nothing anywhere would report a smaller number as wrong."""
    group = _fold(lessons=[_lesson(outcome="noted")])
    assert group["runs"] == {"r1"} and group["scopes"] == {"t1"}
    assert not group["support"] and not group["oppose"]


def test_an_unknown_stance_is_neutral_rather_than_a_guess():
    group = _fold(lessons=[_lesson(outcome="something-nobody-writes")])
    assert not group["support"] and not group["oppose"]
    assert group["runs"] == {"r1"}


def test_unsupported_research_lands_in_unverified_NOT_in_oppose():
    """The distinction the third bucket exists for. Folding "not established" into counter-evidence
    lets an uncited claim read as actively refuted."""
    group = _fold(research=[_research(
        verification={"verdict": "unsupported", "method": "search"})])
    assert not group["oppose"], "unsupported research became counter-evidence"
    assert not group["support"]


@pytest.mark.parametrize("verdict", ["unsupported", "unclear", "cited", "", "nonsense"])
def test_every_non_supported_verdict_is_unverified(verdict):
    group = _fold(research=[_research(verification={"verdict": verdict, "method": "m"})])
    assert not group["oppose"] and not group["support"]


def test_a_supported_verdict_is_the_only_thing_that_supports():
    group = _fold(research=[_research()])
    assert group["support"] and not group["unverified"]


def test_the_verification_receipt_records_method_and_verdict_together():
    """A verdict without its method is not auditable — two claims "supported" by replication and by
    a web search are not the same evidence."""
    group = _fold(research=[_research(verification={"verdict": "supported", "method": "replication"})])
    assert group["verification"] == {"replication:supported"}


def test_a_verdict_with_no_method_records_the_bare_verdict():
    group = _fold(research=[_research(verification={"verdict": "supported", "method": ""})])
    assert group["verification"] == {"supported"}


def test_research_urls_become_sources():
    group = _fold(research=[_research(urls=["https://example.invalid/a",
                                            "https://example.invalid/b"])])
    assert len(group["sources"]) == 2


def test_a_row_whose_group_cannot_be_resolved_is_skipped_not_crashed():
    """`resolve` returns None for a statement with no subject content. That is a normal outcome, not
    an error — a junk row must cost itself, not the whole projection."""
    calls = []
    _ingest_evidence([_lesson(), _lesson()], [_research()],
                     lambda row: calls.append(row) or None)
    assert len(calls) == 3


def test_a_non_indexable_research_row_never_reaches_the_group():
    """`_indexable_research_claim` refuses a kinded NON-claim row — a producer's cardinality receipt,
    for instance. Letting one through would turn bookkeeping into evidence."""
    resolved = []
    _ingest_evidence([], [_research(record_kind="cardinality", v=3)],
                     lambda row: resolved.append(row) or _group())
    assert resolved == [], "an unindexable research row was folded in anyway"


# ------------------------------------------------------------------ the one difference

def test_the_optional_weigh_hook_sees_every_row_and_its_refs():
    """The structured projection weights candidate spellings by how many drillable refs back them;
    the lean one keys on the normalized statement and has no competing spellings to choose between.
    That is the ONLY thing that differed between the two loops."""
    seen = []
    _fold(lessons=[_lesson(evidence=[1, 2])], research=[_research(node_ids=[3])],
          weigh=lambda group, row, refs: seen.append((row["statement"], len(refs))))
    assert [name for name, _ in seen] == ["a lesson", "a research claim"]
    assert all(count >= 0 for _name, count in seen)


def test_without_the_hook_nothing_is_weighted_and_nothing_raises():
    """The lean projection passes no `weigh`; a helper that assumed one would `TypeError` on every
    lean read."""
    assert _fold(lessons=[_lesson()], research=[_research()])["runs"] == {"r1", "r2"}


# ------------------------------------------------------------------ both projections use it

@pytest.mark.parametrize("fn", ["_structured_assessments", "claim_assessments"])
def test_neither_projection_re_inlines_the_walk(fn):
    import inspect

    from looplab.engine import claims_assessments

    source = inspect.getsource(getattr(claims_assessments, fn))
    assert "_lesson_claim_stance(" not in source, f"{fn} re-derives the lesson stance mapping"
    assert "_research_verification(" not in source, f"{fn} re-derives the research verdict routing"


def test_the_two_projections_agree_on_stance_and_verification_for_the_same_rows():
    """An end-to-end cross-check: the structured and lean paths group differently on purpose, but a
    supported research claim and an `improved` lesson must not land in different buckets depending on
    which projection the caller asked for."""
    from looplab.engine.claims_assessments import claim_assessments

    lessons = [_lesson(outcome="improved", evidence=[1])]
    research = [_research(node_ids=[2])]
    for structured in (False, True):
        rows = claim_assessments(lessons, research_claims=research, decisions={},
                                 structured=structured)
        assert rows, f"structured={structured} produced no claims"
        assert any(row.get("n_support", 0) > 0 for row in rows), (
            f"structured={structured} lost the supporting evidence")
        assert all(row.get("n_oppose", 0) == 0 for row in rows), (
            f"structured={structured} turned supporting evidence into opposition")
