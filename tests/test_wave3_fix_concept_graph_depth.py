"""Wave-3 fix: `ConceptGraph.depth_of` memoization must be EFFECTIVE.

The previous implementation wrote `memo[cid]` only when `on_path` was empty, but every recursive
descent passes a non-empty `on_path`, so no sub-node was ever cached — leaving the exponential
(2**depth) re-walk on a dense diamond DAG that the docstring/sibling test claimed memoization fixed.
The fix caches the depth VALUE of any node whose subtree pruned no cycle edge (its longest acyclic
root-path is context-independent), while the `on_path` cycle guard still makes cyclic graphs terminate
and return the same depths as the bare recursion. These tests pin BOTH: linear `parents_of` call count
on a deep dense DAG, and cycle termination with unchanged values."""
from __future__ import annotations

import time

from looplab.search.concept_graph import ConceptGraph


def _dense_diamond(depth: int) -> ConceptGraph:
    """A DAG where every layer's two nodes both point at BOTH nodes of the layer above — the shape whose
    shared ancestors an ineffective memo re-walks 2**depth times."""
    g = ConceptGraph(task_type="dense-retrieval")
    g.ensure("l0a")
    g.ensure("l0b")
    prev = ("l0a", "l0b")
    for layer in range(1, depth + 1):
        names = (f"l{layer}a", f"l{layer}b")
        for nm in names:
            g.ensure(nm, axes=prev)
        prev = names
    return g


def test_depth_of_is_memoized_linear_not_exponential():
    """On a deep dense diamond DAG the depth is correct AND `parents_of` is called a LINEAR (not
    exponential) number of times. At depth 25 an ineffective memo would run 2**25 (~33M) walks and hang;
    an effective memo visits each node once."""
    depth = 25
    g = _dense_diamond(depth)

    calls = {"n": 0}
    real_parents_of = g.parents_of

    def _counting_parents_of(cid):
        calls["n"] += 1
        return real_parents_of(cid)

    g.parents_of = _counting_parents_of  # type: ignore[assignment]

    t0 = time.perf_counter()
    assert g.depth_of(f"l{depth}a") == depth
    elapsed = time.perf_counter() - t0

    # ~2 nodes per layer + roots => O(depth) distinct nodes; each visited once means a handful of
    # parents_of calls per node. A generous linear bound (< 100x depth) still excludes any exponential
    # blow-up (2**25 == 33_554_432), which would also never complete this fast.
    assert calls["n"] < 100 * depth, f"parents_of called {calls['n']} times — memo is ineffective"
    assert elapsed < 2.0, f"depth_of took {elapsed:.2f}s — memo is ineffective"


def test_depth_of_terminates_on_a_cycle_with_unchanged_values():
    """The `on_path` cycle guard must survive the memoization change: a curated cross-link cycle still
    terminates and yields the longest ACYCLIC root path (same values as before the fix)."""
    g = ConceptGraph(task_type="dense-retrieval")
    g.ensure("a", axes=("b",))
    g.ensure("b", axes=("a",))                 # a <-> b two-node cycle
    assert g.depth_of("a") == 1 and g.depth_of("b") == 1

    g.ensure("c", axes=("a", "b"))             # node under both sides of the cycle
    assert g.depth_of("c") == 2

    # A longer cycle a -> b -> c -> a plus a real root under c: still terminates, no RecursionError.
    h = ConceptGraph(task_type="dense-retrieval")
    h.ensure("x", axes=("y",))
    h.ensure("y", axes=("z",))
    h.ensure("z", axes=("x", "root"))          # z closes the x->y->z->x cycle and hangs off `root`
    h.ensure("root")
    assert h.depth_of("x") >= 1                 # terminates and is finite
    assert h.depth_of("root") == 0


def test_depth_of_matches_plain_id_nesting():
    """Ordinary id-nesting depth is unchanged by the memoization fix (pure DAG, every node cacheable)."""
    g = ConceptGraph(task_type="dense-retrieval")
    g.ensure("loss/contrastive/dcl/dclx")
    assert g.depth_of("loss/contrastive/dcl/dclx") == 3
    assert g.depth_of("loss") == 0
