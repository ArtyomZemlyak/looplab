"""How many coordinates a node moved from its parent — the arbiter of "one hypothesis, one change".

A card claims a hypothesis; the knob delta says whether the experiment tested it. On
`runs/e5small-dr-unified-v4`: node 8 reads Δ1 (`max_grad_norm`) and its card claims one knob — a
clean answer. Node 4 reads Δ3 (batch, accum, learning rate) while its card also claims one, which is
exactly why its 0.789365 cannot answer the question it was proposed to answer.

The same number decides whether a REPAIR changed the hypothesis or merely fitted it to the machine:
intersect the moved paths with the paths the hypothesis names. Empty means the card still describes
the experiment; non-empty means it does not.
"""
from __future__ import annotations

from types import SimpleNamespace

from looplab.core.param_carriers import effective_params, node_knob_delta, resolved_params


def _n(nid, params, parents=(), applied=None):
    prov = {"applied_params": {"applied": applied}} if applied is not None else None
    return SimpleNamespace(id=nid, parent_ids=list(parents),
                           idea=SimpleNamespace(params=dict(params)), metric_provenance=prov)


def test_absence_means_INHERITED_not_changed():
    """THE BUG THIS EXISTS TO NOT HAVE. Comparing bare records reads a path the child never mentions
    as a change: node 8 declares one knob, node 3's record names twelve, and the naive union called
    a one-knob experiment a fifteen-knob one."""
    parent = _n(3, {"a": 1.0, "b": 2.0, "c": 3.0})
    child = _n(8, {"grad": 1.0}, parents=[3])
    by_id = {3: parent, 8: child}
    assert node_knob_delta(child, parent, by_id) == ["grad"]


def test_a_changed_value_counts_and_an_equal_one_does_not():
    parent = _n(0, {"a": 1.0, "b": 2.0})
    child = _n(1, {"a": 1.0, "b": 9.0}, parents=[0])
    assert node_knob_delta(child, parent, {0: parent, 1: child}) == ["b"]


def test_resolution_layers_up_the_whole_chain():
    """A grandchild that restates nothing still has its grandparent's coordinates."""
    a = _n(0, {"x": 1.0, "y": 2.0})
    b = _n(1, {"y": 5.0}, parents=[0])
    c = _n(2, {}, parents=[1])
    by_id = {0: a, 1: b, 2: c}
    assert resolved_params(c, by_id) == {"x": 1.0, "y": 5.0}
    assert node_knob_delta(c, b, by_id) == []


def test_the_applied_record_wins_over_the_declaration():
    """The delta is about what RAN. A node that proposed 8192 and applied 4096 differs from a parent
    at 4096 by nothing."""
    parent = _n(0, {"bs": 4096.0})
    child = _n(1, {"bs": 8192.0}, parents=[0], applied={"bs": 4096.0})
    assert effective_params(child)["bs"] == 4096.0
    assert node_knob_delta(child, parent, {0: parent, 1: child}) == []


def test_no_parent_is_no_delta():
    assert node_knob_delta(_n(0, {"a": 1.0}), None) == []


def test_a_cycle_in_the_lineage_costs_depth_not_a_hang():
    """`parent_ids` is persisted data; a malformed chain must degrade, never spin."""
    a = _n(0, {"x": 1.0}, parents=[1])
    b = _n(1, {"y": 2.0}, parents=[0])
    assert resolved_params(a, {0: a, 1: b}) == {"x": 1.0, "y": 2.0}


def test_without_a_lineage_map_only_the_childs_own_paths_are_compared():
    """The degraded call still refuses to read absence as change."""
    parent = _n(0, {"a": 1.0, "b": 2.0})
    child = _n(1, {"a": 9.0}, parents=[0])
    assert node_knob_delta(child, parent) == ["a"]


def test_the_live_run_reproduces_the_two_cases_this_was_built_for():
    from pathlib import Path

    import pytest
    log = Path("/home/jovyan/data/looplab/runs/e5small-dr-unified-v4/events.jsonl")
    if not log.exists():
        pytest.skip("the live run is not on this box")
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold

    nodes = fold(EventStore(log).read_all()).nodes
    assert node_knob_delta(nodes[8], nodes[3], nodes) == ["train.training.max_grad_norm"], (
        "node 8's card claims ONE knob and the record must agree")
    assert len(node_knob_delta(nodes[4], nodes[3], nodes)) == 3, (
        "node 4 moved three knobs while claiming one — the confound must be visible")


def test_the_digest_line_leads_with_the_delta():
    """WIRING, and the ORDER: Δ is the most compressed decision-relevant fact on the line, and the
    coordinate list is long enough to push anything after it out of a reader's first glance."""
    from looplab.events.digest import _node_line

    parent = _n(3, {"a": 1.0})
    child = _n(8, {"grad": 1.0}, parents=[3])
    for node in (parent, child):
        node.status = None
        node.operator = "improve"
        node.metric = 0.5
        node.trials = None
        node.triage_rationale = ""
        node.robust_metric = 0.5
        node.theme = ""
        node.concepts = []
    state = SimpleNamespace(nodes={3: parent, 8: child})
    line = _node_line(child, state)
    assert "[Δ1 vs #3: grad]" in line
    # With no applied record the coordinates fall back to the declaration, unmarked — that is the
    # documented behaviour, so the ORDER is asserted against whatever spelling the render chose.
    assert "'grad': 1.0" in line, line
    assert line.index("Δ1") < line.index("'grad': 1.0"), "the delta must precede the coordinates"


def test_zero_delta_says_the_difference_is_in_code():
    """Δ0 with a different metric is its own signal, and silence would read as 'identical'."""
    from looplab.events.digest import _node_line

    parent = _n(3, {"a": 1.0})
    child = _n(9, {"a": 1.0}, parents=[3])
    for node in (parent, child):
        node.status = None
        node.operator = "improve"
        node.metric = 0.5
        node.trials = None
        node.triage_rationale = ""
        node.robust_metric = 0.5
        node.theme = ""
        node.concepts = []
    line = _node_line(child, SimpleNamespace(nodes={3: parent, 9: child}))
    assert "Δ0 vs #3 — the difference is in CODE, not params" in line


def test_a_merge_reports_against_EVERY_parent():
    """Found by reading the diff, not by a red test. A merge descends from two nodes and the first
    version printed only `parents[0]`: on the live run node 13 read "Δ0 vs #11" while also
    descending from #10, which it differs from by three knobs. Half the lineage was invisible on the
    one line a reader uses to judge what an experiment tested."""
    from looplab.events.digest import _node_line

    a = _n(10, {"x": 1.0, "y": 1.0})
    b = _n(11, {"x": 1.0, "y": 2.0}, parents=[10])
    m = _n(13, {"x": 1.0, "y": 2.0}, parents=[11, 10])
    for node in (a, b, m):
        node.status = None
        node.operator = "merge"
        node.metric = 0.5
        node.robust_metric = 0.5
        node.trials = None
        node.triage_rationale = ""
        node.theme = ""
        node.concepts = []
    line = _node_line(m, SimpleNamespace(nodes={10: a, 11: b, 13: m}))
    assert "Δ0 vs #11" in line, line
    assert "Δ1 vs #10" in line, "the SECOND parent must not be silent"


def test_the_code_note_appears_only_when_EVERY_parent_delta_is_zero():
    """Otherwise a merge that matches one parent and differs from the other would claim its
    difference is in code while a knob is visibly named beside it."""
    from looplab.events.digest import _node_line

    a = _n(10, {"x": 1.0})
    b = _n(11, {"x": 1.0}, parents=[10])
    m = _n(13, {"x": 1.0}, parents=[11, 10])
    for node in (a, b, m):
        node.status = None
        node.operator = "merge"
        node.metric = 0.5
        node.robust_metric = 0.5
        node.trials = None
        node.triage_rationale = ""
        node.theme = ""
        node.concepts = []
    assert "difference is in CODE" in _node_line(m, SimpleNamespace(nodes={10: a, 11: b, 13: m}))

    # The discriminating ORDER: a non-zero parent FIRST and a zero parent LAST. A "last parent
    # wins" accumulator passes every other arrangement — it took a mutation to notice, because the
    # first version of this test put the zero parent first and stayed green under exactly that bug.
    a.idea = SimpleNamespace(params={"x": 9.0})          # #10 now differs by one knob
    m.parent_ids = [10, 11]                              # …and the ZERO parent is last
    line = _node_line(m, SimpleNamespace(nodes={10: a, 11: b, 13: m}))
    assert "Δ1 vs #10" in line and "Δ0 vs #11" in line, line
    assert "difference is in CODE" not in line, (
        "one parent matching cannot license 'the difference is in code'")


def _renderable(node):
    """Fill the attributes `_node_line` reads beyond the delta, so a test can drive the real line."""
    node.status = None
    node.operator = "improve"
    node.metric = 0.5
    node.robust_metric = 0.5
    node.trials = None
    node.triage_rationale = ""
    node.theme = ""
    node.concepts = []
    return node


def test_an_EMPTY_comparison_makes_no_claim_about_where_the_difference_is():
    """"Nothing moved" and "there was nothing to compare" are different facts.

    `node_knob_delta` returns `[]` for both, and one `all_zero` flag could not tell them apart — so
    the digest asserted "the difference is in CODE, not params" into the Researcher's prompt from a
    comparison that never happened. It is not an edge case on the task family this line was built
    for: a `params_style: "none"` repo run declares no `Idea.params` at all, so BOTH sides are empty
    on every node and EVERY node line carried the claim.
    """
    from looplab.events.digest import _node_line

    parent = _renderable(_n(3, {}))
    child = _renderable(_n(9, {}, parents=[3]))
    line = _node_line(child, SimpleNamespace(nodes={3: parent, 9: child}))
    assert "difference is in CODE" not in line, (
        "MUTATION: collapse `compared_any` back into `all_zero` and a positive claim about where "
        "the difference lies is emitted from two empty coordinate sets")
    assert "no coordinates recorded" in line, (
        "and the absence is SAID rather than rendered as a measured Δ0")


def test_a_parent_missing_from_the_fold_is_an_absence_not_an_agreement():
    """The other route to an empty comparison: `node_knob_delta` returns `[]` when the parent is not
    in `state.nodes`, which a Δ0 render turns into a claim that the two agreed."""
    from looplab.events.digest import _node_line

    child = _renderable(_n(9, {"a": 1.0}, parents=[3]))
    line = _node_line(child, SimpleNamespace(nodes={9: child}))
    assert "difference is in CODE" not in line and "Δ0 vs #3" not in line, line
    assert "no coordinates recorded" in line


def test_a_real_agreement_still_makes_the_claim():
    """The counter-assertion: the fix must not have silenced the signal the line exists for."""
    from looplab.events.digest import _node_line

    parent = _renderable(_n(3, {"a": 1.0}))
    child = _renderable(_n(9, {"a": 1.0}, parents=[3]))
    assert "Δ0 vs #3 — the difference is in CODE, not params" in _node_line(
        child, SimpleNamespace(nodes={3: parent, 9: child}))
