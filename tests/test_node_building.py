"""`node_building`: a TRANSIENT marker emitted the instant the engine starts building a node (before
the Researcher/Developer run) so the UI can show it + stream its live agent-trace immediately, instead
of only after the minutes-long dev session ends with node_created. It folds to `st.building`, NOT
`st.nodes`, so it never affects node-id allocation (max(nodes)+1) or resume — the crux of the design."""
from __future__ import annotations

import itertools

from looplab.core.models import Event
from looplab.events.replay import fold
from looplab.events.types import ALL_EVENT_TYPES, EV_NODE_BUILDING


def _ev(t, **d):
    return Event(seq=next(_ev.c), ts=1.0, type=t, data=d)


_ev.c = itertools.count()
_BASE = [_ev("run_started", run_id="r", task_id="t", goal="g", direction="min")]


def test_registered():
    assert EV_NODE_BUILDING in ALL_EVENT_TYPES        # else the engine's append silently no-ops


def test_building_marker_not_in_nodes_and_id_allocation_safe():
    st = fold(_BASE + [_ev("node_building", node_id=5, operator="improve", parent_ids=[3])])
    assert st.building == {"node_id": 5, "operator": "improve", "parent_ids": [3], "started": 1.0}
    assert st.nodes == {}                             # NOT a real node
    # the id the engine would allocate next is unaffected — a build marker must never bump it (else the
    # node it announces gets a DIFFERENT id on the follow-up node_created, or resume skips/duplicates it)
    assert max(st.nodes, default=-1) + 1 == 0


def test_building_marker_preserves_only_a_bounded_canonical_card_id():
    valid = fold(_BASE + [_ev(
        "node_building", node_id=5, operator="improve", parent_ids=[3], card_id=" card-7 ",
    )])
    assert valid.building == {
        "node_id": 5, "operator": "improve", "parent_ids": [3], "started": 1.0,
        "card_id": "card-7",
    }
    assert valid.buildings[5] is valid.building

    for invalid in (7, "", "   ", "x" * 257, "card\n7"):
        state = fold(_BASE + [_ev(
            "node_building", node_id=6, operator="draft", parent_ids=[], card_id=invalid,
        )])
        assert "card_id" not in state.building
        assert "card_id" not in state.buildings[6]


def test_node_created_supersedes_the_marker():
    evs = _BASE + [
        _ev("node_building", node_id=0, operator="draft", parent_ids=[]),
        _ev("node_created", node_id=0, operator="draft", idea={"operator": "draft", "rationale": "x"}),
    ]
    st = fold(evs)
    assert st.building is None                         # cleared
    assert 0 in st.nodes                               # the real node is here


def test_node_failed_clears_a_stale_marker():
    # a build that fails outright still clears the marker (defensive; normally node_created clears first)
    st = fold(_BASE + [
        _ev("node_building", node_id=2, operator="improve", parent_ids=[1]),
        _ev("node_created", node_id=2, operator="improve", idea={"operator": "improve", "rationale": "x"}),
        _ev("node_failed", node_id=2, reason="crash", error="boom"),
    ])
    assert st.building is None


def test_malformed_build_generation_degrades_to_legacy_marker_and_can_be_recovered():
    prefix = _BASE + [_ev(
        "node_building", node_id=12, operator="draft", parent_ids=[], generation="bad",
    )]
    staged = fold(prefix)
    assert "generation" not in staged.buildings[12]

    recovered = fold(prefix + [_ev(
        "node_failed", node_id=12, reason="build_interrupted", error="resume recovery",
    )])
    assert recovered.building is None
    assert recovered.buildings == {}


def test_a_second_build_replaces_the_first_marker():
    st = fold(_BASE + [
        _ev("node_building", node_id=7, operator="improve", parent_ids=[]),
        _ev("node_building", node_id=8, operator="merge", parent_ids=[7, 6]),
    ])
    assert st.building["node_id"] == 8 and st.building["operator"] == "merge"


def test_run_finished_clears_a_dangling_build_marker():
    """Mega-review 07-06: a dev session that dies MID-BUILD (no node_created / node_failed) would leave
    `st.building` set. When the run then finishes, the fold must drop the marker — else the UI shows a
    breathing 'building…' card + a false 'working' pulse on a run that is over."""
    st = fold(_BASE + [
        _ev("node_building", node_id=9, operator="improve", parent_ids=[]),
        _ev("run_finished", reason="aborted"),
    ])
    assert st.finished is True
    assert st.building is None                          # no phantom card survives the finish


def test_error_finish_retains_crash_prefix_for_resume_recovery():
    st = fold(_BASE + [
        _ev("node_building", node_id=10, operator="draft", parent_ids=[], card_id="card-10"),
        _ev("run_finished", reason="error"),
    ])
    assert st.finished is True
    assert st.building is st.buildings[10]
    assert st.building["card_id"] == "card-10"
