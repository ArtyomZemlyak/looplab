"""Phase 3a: multi-lens projection over the typed concept-edge set. Directed rels (uses/part_of) read
(src, rel, dst) as "src's parent is dst"; symmetric co_occurs is oriented by touch (parent = higher-
touch endpoint). Deterministic spanning arborescence + cross_parents + cycle-avoidance."""
import itertools

from looplab.search.concept_graph import (project_lens, default_lenses, concept_touch_counts)


def _edges(triples):
    """triples: (src, rel, dst, confidence) -> the RunState.concept_edges dict shape."""
    return {f"{s}\t{r}\t{d}": {"src": s, "rel": r, "dst": d, "provenance": "x", "confidence": c}
            for (s, r, d, c) in triples}


def test_default_lenses():
    ls = default_lenses()
    assert ls[0]["name"] == "is_a"                                  # is_a is the default (first)
    assert {l["name"] for l in ls} == {"is_a", "uses", "part_of", "co_occurs"}


def test_touch_counts():
    assert concept_touch_counts({0: ["loss/dcl", "arch/moe"], 1: ["loss/dcl"], 2: []}) \
        == {"loss/dcl": 2, "arch/moe": 1}


def test_directed_uses_lens():
    h = project_lens(["agents", "rag", "llm"],
                     _edges([("agents", "uses", "llm", 1.0), ("rag", "uses", "llm", 1.0)]), "uses")
    assert h["lens"] == "uses" and h["roots"] == ["llm"]
    assert h["nodes"]["llm"]["children"] == ["agents", "rag"]
    assert h["nodes"]["agents"]["parent"] == "llm" and h["nodes"]["agents"]["depth"] == 1


def test_symmetric_co_occurs_oriented_by_touch():
    e = _edges([("a", "co_occurs", "b", 1.0), ("a", "co_occurs", "c", 1.0), ("b", "co_occurs", "c", 1.0)])
    h = project_lens(["a", "b", "c"], e, "co_occurs", touch={"a": 5, "b": 2, "c": 1})
    assert h["roots"] == ["a"]                                      # highest-touch = hub/root
    assert h["nodes"]["a"]["children"] == ["b", "c"]
    assert h["nodes"]["c"]["parent"] == "a"                        # a vs b tie on conf -> min id "a"
    assert h["nodes"]["c"]["cross_parents"] == ["b"]              # dropped b-c membership kept visible


def test_cycle_avoidance():
    h = project_lens(["a", "b"], _edges([("a", "uses", "b", 1.0), ("b", "uses", "a", 1.0)]), "uses")
    assert len(h["roots"]) == 1 and set(h["nodes"]) == {"a", "b"}   # one root, no cycle, both present
    root = h["roots"][0]
    assert h["nodes"]["b" if root == "a" else "a"]["parent"] == root


def test_confidence_picks_primary_parent():
    h = project_lens(["x", "p1", "p2"], _edges([("x", "uses", "p1", 0.3), ("x", "uses", "p2", 0.9)]), "uses")
    assert h["nodes"]["x"]["parent"] == "p2" and h["nodes"]["x"]["cross_parents"] == ["p1"]


def test_deterministic_regardless_of_edge_order():
    t = [("a", "co_occurs", "b", 1.0), ("a", "co_occurs", "c", 1.0), ("b", "co_occurs", "c", 1.0)]
    base = project_lens(["a", "b", "c"], _edges(t), "co_occurs", touch={"a": 3, "b": 2, "c": 1})
    for perm in itertools.permutations(t):
        assert project_lens(["a", "b", "c"], _edges(list(perm)), "co_occurs",
                            touch={"a": 3, "b": 2, "c": 1}) == base


def test_empty_edges():
    assert project_lens(["a"], {}, "uses") == {
        "lens": "uses", "roots": ["a"], "nodes": {"a": {"parent": None, "depth": 0, "children": [],
                                                        "tagged": True, "cross_parents": []}}}


def test_nodes_dict_is_id_sorted_not_hash_ordered():
    # Regression: the `nodes` dict was built by iterating the raw `all_ids` SET, so its key order
    # followed randomized string hashing (PYTHONHASHSEED) — json.dumps of the projection (returned by
    # the /concepts endpoint) was not byte-stable across processes despite the "DETERMINISTIC" docstring.
    t = [("zeta", "uses", "hub", 1.0), ("alpha", "uses", "hub", 1.0), ("mid", "uses", "alpha", 1.0)]
    h = project_lens(["zeta", "alpha", "hub", "mid"], _edges(t), "uses")
    assert list(h["nodes"]) == sorted(h["nodes"])


def test_string_is_a_lens_filters_to_is_a_not_all_rels():
    # Regression: `_lens_rels("is_a")` returned None (== NO filter), so a string is_a lens mixed EVERY
    # relation into one tree, disagreeing with the equivalent dict form {"rels": ["is_a"]}. Both spellings
    # must project only the is_a edges.
    e = _edges([("a", "is_a", "root", 1.0), ("a", "uses", "b", 1.0), ("b", "co_occurs", "c", 1.0)])
    as_string = project_lens(["a", "b", "c", "root"], e, "is_a")
    as_dict = project_lens(["a", "b", "c", "root"], e, {"name": "is_a", "rels": ["is_a"]})
    assert as_string["nodes"] == as_dict["nodes"]        # the two spellings agree
    assert as_string["nodes"]["a"]["parent"] == "root"   # followed the is_a edge
    assert as_string["nodes"]["b"]["parent"] is None      # the `uses` edge was NOT mixed in
