"""A ledger of unscored drafts must not report itself as empty.

`list_experiments` defaults to `sort="best"`, and `best`/`worst` rank by metric, so
`digest.top_nodes` drops every node that has not been evaluated yet. Before this, a run whose
experiments were all still in flight answered `(no matching experiments)` -- which reads as NOTHING
HAS BEEN TRIED to the two callers that exist precisely to avoid re-proposing work already running.

Measured over the probe corpus on 2026-08-28: 48 calls across eight runs (gpt56luna, ctlEdge,
fxSpectral, fxKcenters, sol10, solHull and others) received that empty answer from
`hyp_prioritize`/`foresight_rank` while a `sort=recent` call moments away in the SAME run listed the
drafts. Empty-answer rate by sort: 28-37 % for `best`, 1-3 % for `recent`.

The refuter is `test_a_ledger_of_unscored_drafts_says_so`: drop the new branch and it fails, because
the old code returns the bare "(no matching experiments)".
"""
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.tools.run_tools import RunTools


def _tools(state):
    tools = RunTools()
    tools.bind_state(state)
    return tools


def _unscored_run():
    st = RunState(goal="g", direction="max")
    st.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={"x": 0.1}),
                metric=None, status=NodeStatus.pending),
        1: Node(id=1, operator="draft", idea=Idea(operator="draft", params={"x": 0.2}),
                metric=None, status=NodeStatus.pending),
    }
    return st


def test_a_ledger_of_unscored_drafts_says_so():
    out = _tools(_unscored_run()).execute("list_experiments", {})
    assert "no matching experiments" not in out, (
        "two experiments are in flight; calling the ledger empty invites a duplicate proposal")
    assert "2 current experiment(s)" in out, "the caller needs the COUNT, not just a hedge"
    assert "sort=recent" in out, "the caller needs the way to actually see them"


def test_the_default_sort_is_the_one_that_was_lying():
    # No `sort` key at all — this is the call shape hyp_prioritize and foresight_rank make.
    bare = _tools(_unscored_run()).execute("list_experiments", {})
    explicit = _tools(_unscored_run()).execute("list_experiments", {"sort": "best"})
    assert bare == explicit and "no SCORED experiments yet" in bare


def test_worst_is_covered_too():
    out = _tools(_unscored_run()).execute("list_experiments", {"sort": "worst"})
    assert "no SCORED experiments yet" in out


def test_a_genuinely_empty_run_still_says_no_matching_experiments():
    st = RunState(goal="g", direction="max")
    st.nodes = {}
    assert _tools(st).execute("list_experiments", {}) == "(no matching experiments)"


def test_a_theme_that_matches_nothing_keeps_its_own_honest_zero():
    # The metric filter is not what emptied this list, so the new wording must NOT fire: node 0 is
    # scored and present, and only the theme excluded it.
    st = RunState(goal="g", direction="max")
    st.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={"x": 0.3}),
                        metric=0.7, status=NodeStatus.evaluated)}
    st.node_concepts = {0: ["live/a"]}
    out = _tools(st).execute("list_experiments", {"theme": "absent"})
    assert "no SCORED experiments yet" not in out


def test_recent_is_untouched():
    out = _tools(_unscored_run()).execute("list_experiments", {"sort": "recent"})
    assert "#0" in out and "#1" in out and "no SCORED experiments yet" not in out
