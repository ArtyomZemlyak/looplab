"""The two seams the node-mutation tools all share (doc 25 TO-02).

`_node_subtree` was written out verbatim three times — once to PLAN a delete, once to COMMIT it,
and once inside the purge to RE-VERIFY that the approved scope still holds. The third copy compares
its answer against the first two, so a copy that drifted would not produce a wrong deletion: it
would produce a permanent refusal of a correct approval, which reads like data corruption.

`_node_lifecycle_unchanged` was written out twice, in `_reset_node` and `_retag_node`, as the fence
that rejects an approval whose subject moved while the confirm card was open. Getting it wrong
means a mutation lands on a node the operator never saw.
"""
from __future__ import annotations

from looplab.tools.machine_runs_tools import _node_lifecycle_unchanged, _node_subtree


class _Node:
    def __init__(self, node_id: int, parents=(), *, attempt: int = 0, tombstoned: bool = False):
        self.id = node_id
        self.parent_ids = list(parents)
        self.attempt = attempt
        self.tombstoned = tombstoned


class _State:
    def __init__(self, *nodes):
        self.nodes = {node.id: node for node in nodes}


class _Event:
    def __init__(self, seq: int):
        self.seq = seq


class _Store:
    """Just the `read_all` the fence uses, with a fold the test controls."""

    def __init__(self, events, state, monkeypatch):
        self._events = events
        import looplab.events.replay as replay
        monkeypatch.setattr(replay, "fold", lambda _events: state)

    def read_all(self):
        return self._events


# ------------------------------------------------------------------ the subtree walk

def test_a_lone_node_is_its_own_subtree():
    assert _node_subtree(_State(_Node(0), _Node(1), _Node(2)), 1) == {1}


def test_descendants_are_collected_transitively():
    state = _State(_Node(0), _Node(1, [0]), _Node(2, [1]), _Node(3, [2]), _Node(9))
    assert _node_subtree(state, 1) == {1, 2, 3}
    assert _node_subtree(state, 0) == {0, 1, 2, 3}


def test_a_merge_node_joins_when_any_single_parent_is_inside():
    """The graph is a DAG: a merge has several parents. Requiring ALL of them would leave a child of
    the deleted node alive with a dangling parent link — the exact orphaning this walk prevents."""
    state = _State(_Node(0), _Node(1), _Node(2, [0, 1]))
    assert _node_subtree(state, 0) == {0, 2}
    assert _node_subtree(state, 1) == {1, 2}


def test_a_descendant_discovered_late_still_pulls_in_its_own_children():
    """Iteration order over `state.nodes` is not topological, so a single pass would stop early.
    Node 3 is visited before its parent 2 joins; the fixpoint sweep is what catches it."""
    state = _State(_Node(3, [2]), _Node(2, [1]), _Node(1, [0]), _Node(0))
    assert _node_subtree(state, 1) == {1, 2, 3}


def test_a_parent_cycle_terminates_instead_of_spinning():
    """A malformed log can persist a cycle. The walk must still answer, because the delete path has
    no other way to bound its scope."""
    state = _State(_Node(0, [1]), _Node(1, [0]), _Node(2, [1]))
    assert _node_subtree(state, 0) == {0, 1, 2}


def test_an_absent_root_still_names_itself_and_nothing_else():
    """The purge calls this for a node that may already be gone; it must not raise there."""
    assert _node_subtree(_State(_Node(5)), 42) == {42}


def test_ancestors_are_never_swept_in():
    """Only descendants: deleting a node must not take out the tree it hangs from."""
    state = _State(_Node(0), _Node(1, [0]), _Node(2, [1]))
    assert _node_subtree(state, 2) == {2}


# ------------------------------------------------------------------ the permission fence

def _fence(monkeypatch, *, events, state, node_id=1, expected_tail=7, generation=3):
    store = _Store(events, state, monkeypatch)
    return _node_lifecycle_unchanged(
        store, node_id=node_id, expected_tail=expected_tail, generation=generation)


def test_an_untouched_run_passes_the_fence(monkeypatch):
    assert _fence(monkeypatch, events=[_Event(7)], state=_State(_Node(1, attempt=3))) is True


def test_any_later_append_fails_the_fence_even_when_the_node_is_identical(monkeypatch):
    """The operator approved an action against a run they were SHOWN. A sibling append changed that
    run, so the approval no longer describes what will happen."""
    assert _fence(monkeypatch, events=[_Event(8)], state=_State(_Node(1, attempt=3))) is False


def test_a_rebuilt_node_fails_the_fence(monkeypatch):
    assert _fence(monkeypatch, events=[_Event(7)], state=_State(_Node(1, attempt=4))) is False


def test_a_tombstoned_node_fails_the_fence(monkeypatch):
    assert _fence(monkeypatch, events=[_Event(7)],
                  state=_State(_Node(1, attempt=3, tombstoned=True))) is False


def test_a_vanished_node_fails_the_fence(monkeypatch):
    assert _fence(monkeypatch, events=[_Event(7)], state=_State(_Node(2, attempt=3))) is False


def test_an_empty_log_is_tail_minus_one_not_a_crash(monkeypatch):
    """A run whose log is empty has no last seq; the fence answers rather than raising."""
    assert _fence(monkeypatch, events=[], state=_State(_Node(1, attempt=3)),
                  expected_tail=-1) is True
    assert _fence(monkeypatch, events=[], state=_State(_Node(1, attempt=3)),
                  expected_tail=0) is False
