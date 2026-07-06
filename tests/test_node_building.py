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


def test_a_second_build_replaces_the_first_marker():
    st = fold(_BASE + [
        _ev("node_building", node_id=7, operator="improve", parent_ids=[]),
        _ev("node_building", node_id=8, operator="merge", parent_ids=[7, 6]),
    ])
    assert st.building["node_id"] == 8 and st.building["operator"] == "merge"
