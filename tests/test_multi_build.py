"""Multi-build fold (`st.buildings`) — the parallel_build>1 superset of the singular `st.building`.

Under concurrent builds each `_create_node` worker appends its own `node_building`; the singular
`st.building` only ever holds the LAST-appended one (last-writer-wins, an accepted seam), so the UI
would render just one ghost. `st.buildings` (node_id->marker) tracks EVERY in-flight build so the DAG
can render all of them. These pin the fold behaviour: populate on node_building, clear the RIGHT entry
(its own generation) on each terminal, clear-all on finalize, pop-all-affected on tombstone. The
singular field is left exactly as-is (back-compat), so this is purely additive per replay invariant #5.
"""
from __future__ import annotations

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

_IDEA = {"operator": "draft", "params": {}, "rationale": ""}


def _started(s):
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})


def test_concurrent_builds_are_all_tracked_singular_is_last(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    _started(s)
    s.append("node_building", {"node_id": 0, "operator": "draft", "parent_ids": []})
    s.append("node_building", {"node_id": 1, "operator": "draft", "parent_ids": []})
    st = fold(s.read_all())
    assert sorted(st.buildings.keys()) == [0, 1]        # every concurrent build tracked
    assert st.building["node_id"] == 1                  # singular = the last-appended (back-compat)


def test_node_created_clears_only_its_own_buildings_entry(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    _started(s)
    s.append("node_building", {"node_id": 0, "operator": "draft", "parent_ids": []})
    s.append("node_building", {"node_id": 1, "operator": "draft", "parent_ids": []})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft", "idea": _IDEA})
    st = fold(s.read_all())
    assert 0 not in st.buildings and 1 in st.buildings   # only node 0 landed; node 1 still building
    assert 0 in st.nodes
    # the singular marker (node 1) is NOT cleared by node 0's create (it isn't node 1's terminal)
    assert st.building["node_id"] == 1


def test_node_failed_clears_the_buildings_entry(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    _started(s)
    s.append("node_building", {"node_id": 0, "operator": "draft", "parent_ids": []})
    s.append("node_building", {"node_id": 1, "operator": "draft", "parent_ids": []})
    s.append("node_failed", {"node_id": 1, "operator": "draft", "reason": "crash", "error": "boom"})
    st = fold(s.read_all())
    assert 1 not in st.buildings and 0 in st.buildings   # the failed node's entry is gone; survivor stays
    # node 1 WAS the singular marker, so its failure clears st.building; the singular does NOT
    # auto-fall-back to the surviving build — only its own `st.buildings` entry keeps node 0 visible.
    assert st.building is None


def test_stale_generation_terminal_cannot_pop_newer_buildings_entry(tmp_path):
    # Parity with the singular-field guard (test_stale_terminal_cannot_clear_new_generation_building_marker):
    # a late generation-1 failure must NOT erase a generation-2 build entry.
    s = EventStore(tmp_path / "e.jsonl")
    _started(s)
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft", "idea": _IDEA})
    s.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "implement"})
    s.append("node_reset", {"node_id": 0, "generation": 1, "from_stage": "implement"})
    s.append("node_building", {"node_id": 0, "generation": 2, "operator": "draft"})
    s.append("node_failed", {"node_id": 0, "generation": 1, "reason": "superseded", "error": "late"})
    st = fold(s.read_all())
    assert st.buildings[0]["generation"] == 2           # stale gen-1 terminal did not pop the gen-2 entry
    assert st.building["generation"] == 2               # singular parity (the existing guarantee)


def test_run_finished_clears_all_buildings(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    _started(s)
    s.append("node_building", {"node_id": 0, "operator": "draft", "parent_ids": []})
    s.append("node_building", {"node_id": 1, "operator": "draft", "parent_ids": []})
    s.append("run_finished", {"reason": "done"})
    st = fold(s.read_all())
    assert st.buildings == {} and st.building is None    # no dangling breathing ghosts on a finished run


def test_node_abort_clears_its_buildings_entry(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    _started(s)
    s.append("node_building", {"node_id": 0, "operator": "draft", "parent_ids": []})
    s.append("node_building", {"node_id": 1, "operator": "draft", "parent_ids": []})
    s.append("node_abort", {"node_id": 0})
    st = fold(s.read_all())
    assert 0 not in st.buildings and 1 in st.buildings


def test_tombstone_pops_all_affected_builds(tmp_path):
    # A node created-then-rebuilding is in BOTH st.nodes (pending) and st.buildings; tombstoning its
    # subtree must pop its build entry too (the pop-all-affected path).
    s = EventStore(tmp_path / "e.jsonl")
    _started(s)
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft", "idea": _IDEA})
    s.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "implement"})
    s.append("node_building", {"node_id": 0, "generation": 1, "operator": "draft"})
    assert 0 in fold(s.read_all()).buildings
    s.append("node_tombstoned", {"node_ids": [0]})
    st = fold(s.read_all())
    assert 0 not in st.buildings


def test_backcompat_serial_build_and_old_log(tmp_path):
    # Serial single build -> buildings has exactly the one entry (mirrors the singular).
    s = EventStore(tmp_path / "e.jsonl")
    _started(s)
    s.append("node_building", {"node_id": 0, "operator": "draft", "parent_ids": []})
    st = fold(s.read_all())
    assert list(st.buildings.keys()) == [0] and st.building["node_id"] == 0
    # An old log with no node_building at all -> buildings stays empty (default_factory).
    old = EventStore(tmp_path / "old.jsonl")
    _started(old)
    old.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft", "idea": _IDEA})
    assert fold(old.read_all()).buildings == {}
