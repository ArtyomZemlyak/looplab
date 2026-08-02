"""The shared skeleton of the live cross-run builders (doc 25 EC-01).

`engine/cross_run_context.py` centralizes what the Strategist note and the Researcher advisory used
to duplicate: the governance re-entry, the governed source load, row scoping, and the v2 audit
receipt. Centralizing it means one edit now changes BOTH agents' view of the portfolio, so the
properties that were previously protected by "it is written twice, someone would notice" need real
guards.

Two of these were found by deliberately breaking the module and watching the existing 64 cross-run
tests stay green: dropping a store from the governed source list, and digesting the raw projection
instead of the bounded one. Neither is visible in rendered output — the first silently weakens what
the ledger governs, the second silently changes what the digest identifies.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from looplab.engine import cross_run_context as ctx


def test_all_three_governed_stores_are_listed():
    """Every store the projection READS must be one the ledger GOVERNS.

    `project_governed_sources` takes the names as a tuple, so omitting one produces no error at all:
    the builder still returns text, governed by a ledger that never saw the omitted store. The
    rendered output is unchanged, which is exactly why this needs asserting rather than observing.
    """
    assert set(ctx.CROSS_RUN_SOURCE_NAMES) == {
        "concept_capsules.jsonl", "lessons.jsonl", "research_claims.jsonl"}


def test_governance_re_entry_passes_every_source_and_includes_concepts():
    """The re-entry idiom itself, which was written out twice before and is now written once."""
    seen = {}

    def fake_project(base, reenter, *, include_concepts, source_names):
        seen.update(base=base, include_concepts=include_concepts, source_names=source_names)
        return reenter({"ledger": "resolved"})

    import looplab.engine.governance_health as gh

    original = gh.project_governed_sources
    gh.project_governed_sources = fake_project
    try:
        result = ctx.enter_governed("BASE", lambda governance: f"re-entered:{governance['ledger']}")
    finally:
        gh.project_governed_sources = original

    assert result == "re-entered:resolved"
    assert seen["base"] == "BASE"
    assert seen["include_concepts"] is True, (
        "concept capsules are one of the governed stores; excluding them from the projection means "
        "the concept half of the note is ungoverned while still being rendered")
    assert tuple(seen["source_names"]) == tuple(ctx.CROSS_RUN_SOURCE_NAMES)


def test_the_corpus_digest_identifies_the_BOUNDED_projection():
    """A raw hash over the projection is both a credential oracle and an identity for bytes the
    model was never shown. The digest must be over the sanitized, bounded form — and the two must
    actually differ for content the bounds would trim, or this proves nothing."""
    from looplab.trust.cross_run import sanitize_cross_run_projection

    projection = {"parts": ["x" * 5_000, "y" * 5_000], "nested": [{"deep": "z" * 5_000}]}
    bounds = {"max_chars": 200, "max_items": 4, "max_total_items": 8}

    def canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                          separators=(",", ":")).encode("utf-8")

    raw = hashlib.sha256(canonical(projection)).hexdigest()
    sanitized = hashlib.sha256(
        canonical(sanitize_cross_run_projection(projection, **bounds))).hexdigest()
    assert raw != sanitized, "the bounds trimmed nothing, so this test cannot tell the two apart"
    assert ctx.corpus_digest(projection, **bounds) == sanitized
    assert ctx.corpus_digest(projection, **bounds) != raw


def test_an_incomplete_empty_read_is_not_an_authoritative_absence():
    """The asymmetry the receipts exist for. An empty portfolio yields no guidance; an INCOMPLETE
    read that happens to retain nothing must not, or an unknown is presented as proven absence."""
    complete = {"source_complete": True}
    partial = {"source_complete": False}
    assert ctx.empty_after_complete_read([], [], [], complete, complete) is True
    assert ctx.empty_after_complete_read([], [], [], partial, complete) is False
    assert ctx.empty_after_complete_read([], [], [], complete, partial) is False
    assert ctx.empty_after_complete_read([], [], [], None, complete) is False
    assert ctx.empty_after_complete_read([], [], [], complete, None) is False
    # ...and anything actually retained is never "empty", however complete the read was.
    assert ctx.empty_after_complete_read([{"x": 1}], [], [], complete, complete) is False
    assert ctx.empty_after_complete_read([], [{"x": 1}], [], complete, complete) is False
    assert ctx.empty_after_complete_read([], [], [{"x": 1}], complete, complete) is False


@pytest.mark.parametrize("row,visible", [
    ({"direction": "max", "task_id": "t", "run_id": "other"}, True),
    ({"direction": "min", "task_id": "t", "run_id": "other"}, False),   # opposite objective
    ({"direction": "", "task_id": "t", "run_id": "other"}, False),      # unknown, not merely different
    ({"task_id": "t", "run_id": "other"}, False),                       # absent direction
    ({"direction": "max", "task_id": "elsewhere", "run_id": "other"}, False),
    ({"direction": "max", "task_id": "t", "run_id": "live"}, False),    # this run never sees itself
])
def test_row_scoping_rejects_every_uninterpretable_row(row, visible):
    predicate = ctx.visible_row_predicate("max", task_id="t", excluded_run="live")
    assert predicate(row) is visible


def test_a_row_is_invisible_when_the_run_has_no_task_scope():
    """No task id means no exact scope, and a portfolio-wide projection is not a substitute — it
    would apply another task's outcomes to this one."""
    predicate = ctx.visible_row_predicate("max", task_id="", excluded_run="live")
    assert predicate({"direction": "max", "task_id": "t", "run_id": "other"}) is False


def test_the_receipt_shape_is_one_schema_for_both_agents():
    """The reason this module exists. An auditor compares the Strategist's and the Researcher's
    receipts for the same run; two hand-maintained schemas drift and stop being comparable."""
    receipt = ctx.build_receipt(
        scope_task="t", excluded_run="live", lessons=[1, 2], capsules=[3], research=[],
        scope_key="concept_source", scope_value={"receipt_known": True},
        claim_source={"source_complete": True}, corpus="c" * 64, rendered="rendered text")
    assert list(receipt) == [
        "v", "scope_task", "excluded_run", "n_lessons", "n_capsules", "n_research",
        "concept_source", "claim_source", "corpus_digest", "render_digest"]
    assert receipt["v"] == 2
    assert (receipt["n_lessons"], receipt["n_capsules"], receipt["n_research"]) == (2, 1, 0)
    assert receipt["render_digest"] == hashlib.sha256(b"rendered text").hexdigest()

    # The one field that legitimately differs keeps its position, so both receipts stay diffable.
    other = ctx.build_receipt(
        scope_task="t", excluded_run="live", lessons=[], capsules=[], research=[],
        scope_key="concept_scope", scope_value={"scope_complete": True},
        claim_source=None, corpus="c" * 64, rendered="")
    assert list(other).index("concept_scope") == list(receipt).index("concept_source")


def test_the_unavailable_receipt_is_closed_and_content_free():
    """Suppressing untrusted policy is safe; erasing its health state is not. Disabled, empty and
    unavailable all render as "" to the agent — only the receipt tells them apart."""
    class _Exc:
        def public_receipt(self):
            return {"status": "ledger_unreadable"}

    receipt = ctx.unavailable_receipt(_Exc())
    assert receipt == {"v": 2, "status": "unavailable", "complete": False,
                       "governance": {"status": "ledger_unreadable"}}
    assert "corpus_digest" not in receipt and "render_digest" not in receipt, (
        "an unavailable read has no corpus to identify; emitting a digest would give the absence an "
        "identity indistinguishable from a real empty projection")


def test_both_builders_go_through_the_shared_skeleton():
    """A structural check with a specific job: the two builders were 190-line near-duplicates, and
    nothing stops a future edit from re-inlining one half of the pipeline into one of them."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "looplab" / "engine"
    for module, receipt_attr in (("strategy.py", "_cross_run_note_receipt"),
                                 ("proposal_cues.py", "_cross_run_advisory_receipt")):
        source = (root / module).read_text(encoding="utf-8-sig")
        assert "cross_run_context as ctx" in source, f"{module} no longer uses the shared skeleton"
        for helper in ("ctx.advisory_enabled", "ctx.enter_governed", "ctx.load_governed_sources",
                       "ctx.build_receipt", "ctx.unavailable_receipt", "ctx.corpus_digest"):
            assert helper in source, f"{module} re-inlined {helper}"
        # The duplicated spellings must not come back alongside the shared ones.
        assert '"v": 2,\n                "scope_task"' not in source, (
            f"{module} hand-builds a v2 receipt again")
        assert "project_governed_sources(" not in source, (
            f"{module} re-inlined the governance re-entry instead of calling ctx.enter_governed")
        ast.parse(source)   # the scan is only meaningful over a file that still parses
        assert receipt_attr in source
