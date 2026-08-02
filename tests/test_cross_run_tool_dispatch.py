"""The CrossRunTools verb table and its shared PARTIAL-coverage receipt (doc 25 TO-01).

`_execute` used to be one ~856-line `if name == ...` chain holding all eight tool implementations,
with the source/scope/claim warning sequence hand-rolled about a dozen times inside it. Two things
came out of that: one private method per verb (the shape `RunControlTools` already used), and one
`_partial_warnings` helper.

What these pin is the part a refactor can silently break without any test noticing, because the
symptom is not an error — it is a confident-looking answer with its "absence is not proof" caveat
missing, or in the wrong order, or attached to a complete projection that never needed it.
"""
from __future__ import annotations

import pytest

from looplab.tools import cross_run_tools as crt
from looplab.tools.cross_run_tools import CrossRunTools


COMPLETE_SOURCE = {"source_complete": True}
COMPLETE_SCOPE = {"scope_complete": True}
PARTIAL_SOURCE = {"source_complete": False, "source_concepts_omitted": 3,
                  "source_outcomes_omitted": 1, "source_unknown_capsules": 2}
PARTIAL_SCOPE = {"scope_complete": False, "scope_unknown_capsules": 4,
                 "scope_fingerprint_unknown_capsules": 1,
                 "scope_direction_unknown_capsules": 2, "scope_fingerprint_items_omitted": 5}


# --------------------------------------------------------------- the verb table

def test_every_advertised_tool_has_a_method_and_every_method_is_advertised():
    """A two-way check: a spec with no handler answers "(unknown cross-run tool)" to an agent that
    was just told the tool exists, and a handler with no spec is dead code no agent can reach."""
    advertised = {spec["function"]["name"] for spec in CrossRunTools("/tmp/whatever").specs()}
    implemented = {name[len("_tool_"):] for name in vars(CrossRunTools)
                   if name.startswith("_tool_")}
    assert advertised == implemented == set(crt._TOOL_NAMES)


def test_an_unregistered_name_cannot_reach_a_same_named_method(tmp_path, monkeypatch):
    """`name` comes from the model. Without the registry gate, any string that happens to spell an
    attribute of this class would be callable through the tool channel."""
    reached = []
    monkeypatch.setattr(CrossRunTools, "_tool_smuggled",
                        lambda self, args, gov: reached.append(args) or "reached", raising=False)
    tools = CrossRunTools(tmp_path)
    assert tools._execute("smuggled", {}) == "(unknown cross-run tool)"
    assert not reached

    # ...and the gate is the REGISTRY, not the spelling: adding the name makes it reachable.
    monkeypatch.setattr(crt, "_TOOL_NAMES", set(crt._TOOL_NAMES) | {"smuggled"})
    assert tools._execute("smuggled", {}) == "reached" and reached == [{}]


def test_an_unknown_tool_is_reported_not_raised(tmp_path):
    assert CrossRunTools(tmp_path).execute("no_such_tool", {}) == "(unknown cross-run tool)"


def test_the_handler_receives_the_exact_snapshot_the_re_entry_resolved(tmp_path, monkeypatch):
    """The governance re-entry above the dispatch pins ONE ledger, and the handler must be given
    THAT object. A handler that re-resolved its own would project rows under a ledger the caller's
    receipt does not describe — and the receipt is what an auditor reads to decide whether an
    absence meant "nothing" or "the store could not be read"."""
    from looplab.engine import governance_health

    sentinel = {"aliases": {}, "splits": {}, "decisions": {}, "marker": object()}
    monkeypatch.setattr(governance_health, "project_governed_sources",
                        lambda base, reenter, **kwargs: reenter(sentinel))
    seen = []
    monkeypatch.setattr(CrossRunTools, "_tool_cross_run_atlas",
                        lambda self, args, gov: seen.append(gov) or "ok", raising=False)
    assert CrossRunTools(tmp_path)._execute("cross_run_atlas", {}) == "ok"
    assert seen == [sentinel] and seen[0] is sentinel


# --------------------------------------------------------------- the shared receipt

def test_a_complete_projection_prints_no_caveat_at_all():
    """Not "an empty string in the list" — no entry. A blank line in a receipt reads as a rendering
    bug to a human and is noise to a model."""
    assert crt._partial_warnings(source=COMPLETE_SOURCE, scope=COMPLETE_SCOPE) == []
    assert crt._partial_warnings() == []


def test_each_incomplete_view_contributes_exactly_its_own_warning():
    source_only = crt._partial_warnings(source=PARTIAL_SOURCE, scope=COMPLETE_SCOPE)
    scope_only = crt._partial_warnings(source=COMPLETE_SOURCE, scope=PARTIAL_SCOPE)
    assert len(source_only) == 1 and source_only[0].startswith("WARNING: PARTIAL capsule source")
    assert len(scope_only) == 1
    assert scope_only[0].startswith("WARNING: PARTIAL capsule applicability scope")


def test_source_precedes_scope_precedes_claims():
    """The receipt order is contract: it reads narrowest-cause-first, and every caller printed it
    this way. A reordering is invisible until both caveats fire at once."""
    warnings = crt._partial_warnings(
        source=PARTIAL_SOURCE, scope=PARTIAL_SCOPE,
        claims=({"source_complete": False}, {"producer_claims_omitted": 2}))
    assert len(warnings) == 3
    assert warnings[0].startswith("WARNING: PARTIAL capsule source")
    assert warnings[1].startswith("WARNING: PARTIAL capsule applicability scope")
    assert warnings[2].startswith("WARNING: PARTIAL claim evidence source")


def test_the_claim_caveat_fires_on_an_incomplete_source_and_only_then():
    """`_partial_claim_source_warning` has no empty answer of its own, so its guard lives in the
    helper — and getting that polarity backwards attaches the caveat to sound evidence while
    dropping it from partial evidence, which is exactly inverted."""
    assert crt._partial_warnings(claims=({"source_complete": True}, {})) == []
    attached = crt._partial_warnings(claims=({"source_complete": False}, {}))
    assert len(attached) == 1 and attached[0].startswith("WARNING: PARTIAL claim evidence source")
    # A view that never declared completeness is UNKNOWN, so it warns rather than staying silent.
    assert len(crt._partial_warnings(claims=({}, {}))) == 1


def test_an_omitted_view_is_not_the_same_as_a_complete_one():
    """`None` means "this tool has no such projection", so it prints nothing — but it must not be
    read as a positive completeness claim by anything downstream either."""
    assert crt._partial_warnings(source=None, scope=PARTIAL_SCOPE) == crt._partial_warnings(
        scope=PARTIAL_SCOPE)


@pytest.mark.parametrize("view,expected", [
    ({"source_complete": True}, 0),
    ({"source_complete": False}, 1),
    ({"source_complete": "yes"}, 1),       # a truthy non-True value is not a completeness proof
    ({}, 1),                                # absent means unknown, which fails closed
])
def test_completeness_is_identity_against_true_never_truthiness(view, expected):
    assert len(crt._partial_warnings(source=view)) == expected


# --------------------------------------------------------------- the scope-conditional caveat

def test_an_own_scope_slug_search_never_borrows_the_cross_run_applicability_caveat(
        tmp_path, monkeypatch):
    """`find_concept_slugs(scope="own")` answers from THIS run's concepts, so the cross-run capsule
    applicability receipt is not part of its answer.

    The four call sites in that tool pass their views conditionally (`want != "own"`,
    `capsule_scope_uncertain`) rather than unconditionally, and collapsing them into the shared
    helper is where that condition could quietly be dropped. Attaching a caveat a scope cannot have
    earned is not harmless: it tells the agent its OWN concept list might be incomplete.
    """
    incomplete = {"scope_complete": False, "scope_unknown_capsules": 2,
                  "scope_fingerprint_unknown_capsules": 1,
                  "scope_direction_unknown_capsules": 0, "scope_fingerprint_items_omitted": 0}
    monkeypatch.setattr(CrossRunTools, "_partition_capsules",
                        lambda self, caps: ([], list(caps), incomplete))

    class _State:
        run_id, task_id, direction, goal = "rNow", "t", "max", "retrieval quality"
        nodes: dict = {}
        concepts: dict = {}

    tools = CrossRunTools(tmp_path)
    tools.bind_state(_State())

    own = tools.execute("find_concept_slugs", {"scope": "own"})
    everything = tools.execute("find_concept_slugs", {"scope": "all"})
    assert "PARTIAL capsule applicability scope" not in own
    assert "PARTIAL capsule applicability scope" in everything
