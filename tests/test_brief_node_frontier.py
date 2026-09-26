"""The proposal brief names the NODE FRONTIER (doc 67 67.9), under `Settings.brief_node_frontier`.

"Promising but little explored" — a leader with at most two children — and the dead ends — a leaf
that did not beat its parent and was never extended — were nowhere a proposer reads. Driven over a
tree the REAL fold builds, through the engine's own cue path.
"""
from __future__ import annotations

from looplab.core.cards import normalize_steering_context
from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.core.models import Idea, durable_idea_payload
from looplab.engine.options import EngineOptions
from looplab.events.digest import node_frontier
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from tests.factories import make_engine

# (id, parents, metric), direction max. Node 1 has THREE children (4, 5, 7), so it is a leader that
# is not "little explored"; 3 and 6 are leaves that did not beat their parent.
_TREE = [(0, [], 0.50), (1, [0], 0.60), (2, [0], 0.55), (3, [0], 0.40), (4, [1], 0.70),
         (5, [1], 0.65), (6, [4], 0.68), (7, [1], 0.62)]


def _state(tmp_path, tree=_TREE, *, tombstoned=()):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    for node_id, parents, metric in tree:
        idea = Idea(operator="improve" if parents else "draft", params={"x": float(node_id)},
                    rationale=f"n{node_id}")
        store.append("node_created", {"node_id": node_id, "parent_ids": parents,
                                      "operator": idea.operator,
                                      "idea": durable_idea_payload(idea), "code": "pass\n"})
        store.append("node_evaluated", {"node_id": node_id, "generation": 0, "metric": metric,
                                        "violations": []})
    if tombstoned:
        store.append("node_tombstoned", {"node_ids": list(tombstoned)})
    return fold(store.read_all())


def test_the_frontier_is_the_little_explored_leaders_and_the_dead_ends(tmp_path):
    frontier = node_frontier(_state(tmp_path))
    # Leaders by metric: 4 (0.70), 6 (0.68), 5 (0.65), 7 (0.62), 1 (0.60) — 1 has three children,
    # and 6 is the runner-up leaf that did NOT beat the champion, its parent: a dead end, never
    # "promising" too (critic 2026-09-26: the first cut named it both).
    assert [(n.id, count) for n, count in frontier["promising"]] == [(4, 1), (5, 0), (7, 0)]
    # Leaves that did not beat their parent, most recent first: 6 (0.68 < 4's 0.70), 3 (0.40 < 0.50).
    assert [(n.id, p.id) for n, p in frontier["dead_ends"]] == [(6, 4), (3, 0)]
    assert not ({n.id for n, _c in frontier["promising"]}
                & {n.id for n, _p in frontier["dead_ends"]})


def test_builds_in_flight_are_children_and_discarded_or_aborted_ones_are_not(tmp_path):
    """A leader with three builds under way is not "little explored" (critic 2026-09-26, driven:
    speculation prefetch makes this common); a child the operator aborted, or a speculative build
    discarded before it was evaluated, bought no exploration."""
    store_state = _state(tmp_path / "flight")
    assert 5 in [n.id for n, _c in node_frontier(store_state)["promising"]]
    store = EventStore(tmp_path / "flight" / "events.jsonl")
    for node_id in (20, 21, 22):
        store.append("node_building", {"node_id": node_id, "operator": "improve",
                                       "parent_ids": [5]})
    busy = node_frontier(fold(store.read_all()))
    assert 5 not in [n.id for n, _c in busy["promising"]], "three builds in flight under node 5"
    # Node 4's one child (6) aborted: node 4 is back to no children.
    aborted = _state(tmp_path / "aborted")
    aborted.aborted_nodes = {6}
    assert (4, 0) in [(n.id, c) for n, c in node_frontier(aborted)["promising"]]
    discarded = _state(tmp_path / "discarded")
    discarded.nodes[6].never_evaluated = True
    assert (4, 0) in [(n.id, c) for n, c in node_frontier(discarded)["promising"]]


def test_a_build_marker_whose_parents_are_not_a_list_counts_no_child(tmp_path):
    """A `node_building` row is read as written, not as a fold-validated Node: `"parent_ids": 3`
    raised `TypeError` out of every proposal's cue while the marker stood (critic 2026-09-26)."""
    _state(tmp_path)
    store = EventStore(tmp_path / "events.jsonl")
    store.append("node_building", {"node_id": 20, "operator": "improve", "parent_ids": 5})
    store.append("node_building", {"node_id": 21, "operator": "improve", "parent_ids": ["5", None]})
    store.append("node_building", {"node_id": 22, "operator": "improve", "parent_ids": ["x"]})
    frontier = node_frontier(fold(store.read_all()))
    assert (5, 1) in [(n.id, count) for n, count in frontier["promising"]], (
        "only the coercible id of a list counts: node 5 has one build under way")


def test_a_tombstoned_child_is_no_child(tmp_path):
    frontier = node_frontier(_state(tmp_path, tombstoned=(7,)))
    assert (1, 2) in [(n.id, count) for n, count in frontier["promising"]], (
        "node 1 is back to two children once node 7 is logically deleted")


def test_a_run_no_longer_than_its_leader_list_has_no_frontier(tmp_path):
    assert node_frontier(_state(tmp_path, _TREE[:5])) == {}


def _cue(tmp_path, state, *, on: bool):
    engine = make_engine(tmp_path / f"engine-{on}", brief_node_frontier=on)
    return engine._cue_node_frontier(state, None, engine.researcher)


def test_the_cue_says_both_halves_and_records_the_promising_nodes(tmp_path):
    state = _state(tmp_path)
    text, steering = _cue(tmp_path, state, on=True)
    assert text.startswith("\nNODE FRONTIER — promising but little explored (a leader with at most "
                           "2 children): node 4 (metric=0.7, 1 child); node 5 (metric=0.65, 0 "
                           "children)")
    assert (". Dead ends (a leaf that did not beat its parent and was never extended): node 6 "
            "(0.68 vs parent 4's 0.7); node 3 (0.4 vs parent 0's 0.5).") in text
    assert steering == [{"kind": "node_frontier", "node_ids": [4, 5, 7]}]
    assert normalize_steering_context(steering) == steering, "a registered steering kind"


def test_off_the_cue_is_silent_and_the_complexity_hint_is_the_historical_one(tmp_path):
    state = _state(tmp_path)
    assert _cue(tmp_path, state, on=False) == ("", [])
    hints = {}
    for on in (False, True):
        engine = make_engine(tmp_path / f"hint-{on}", brief_node_frontier=on)
        engine._set_complexity_hint(state, None)
        hints[on] = engine.researcher._complexity_hint
    assert "NODE FRONTIER" not in hints[False]
    note = _cue(tmp_path / "note", state, on=True)[0]
    assert note and hints[True] == hints[False] + note, "the LAST cue: ON is OFF plus the note"


def test_the_flag_ships_off_resumes_off_and_is_off_at_every_constructor():
    assert Settings().brief_node_frontier is False
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["brief_node_frontier"] is False
    legacy = {k: v for k, v in Settings().masked_snapshot().items() if k != "brief_node_frontier"}
    assert settings_from_snapshot(legacy).brief_node_frontier is False
    assert EngineOptions().brief_node_frontier is False
    assert EngineOptions.from_settings(Settings(brief_node_frontier=True)).brief_node_frontier
