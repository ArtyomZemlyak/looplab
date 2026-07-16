"""Phase 2a: `project_hierarchy` — a hierarchy is a pure PROJECTION over concept ids, not stored.
Default lens is_a nests by the concept PATH (parent of a/b/c is a/b); ancestor prefixes are
materialized so a deep-only tag still shows its parent groups. Deterministic + replay-safe."""
from looplab.search.concept_graph import project_hierarchy


def test_is_a_tree_from_paths():
    h = project_hierarchy(["loss/contrastive/dcl", "loss/mnr", "architecture/moe"])
    assert h["lens"] == "is_a"
    assert h["roots"] == ["architecture", "loss"]
    n = h["nodes"]
    # every prefix is materialized, even the untagged group nodes
    assert set(n) == {"architecture", "architecture/moe", "loss", "loss/contrastive",
                      "loss/contrastive/dcl", "loss/mnr"}
    assert n["loss"]["parent"] is None and n["loss"]["depth"] == 0 and n["loss"]["tagged"] is False
    assert n["loss"]["children"] == ["loss/contrastive", "loss/mnr"]
    assert n["loss/contrastive"]["tagged"] is False          # synthetic ancestor
    assert n["loss/contrastive"]["children"] == ["loss/contrastive/dcl"]
    assert n["loss/contrastive/dcl"]["tagged"] is True and n["loss/contrastive/dcl"]["depth"] == 2
    assert n["loss/mnr"]["tagged"] is True and n["loss/mnr"]["parent"] == "loss"
    assert n["architecture/moe"]["tagged"] is True


def test_empty():
    assert project_hierarchy([]) == {"lens": "is_a", "roots": [], "nodes": {}}
    assert project_hierarchy(None)["nodes"] == {}


def test_normalizes_and_dedups_ids():
    h = project_hierarchy(["Loss/DCL", "loss/dcl", " loss/dcl "])   # case/space normalized + deduped
    assert h["roots"] == ["loss"]
    assert set(h["nodes"]) == {"loss", "loss/dcl"}
    assert h["nodes"]["loss/dcl"]["tagged"] is True


def test_deterministic():
    a = project_hierarchy(["b/y", "a/x", "a/x/z"])
    b = project_hierarchy(["a/x/z", "a/x", "b/y"])              # input order must not matter
    assert a == b
    assert a["roots"] == ["a", "b"]
    # Regression: the `nodes` dict must be id-SORTED, not raw-set (hash) order. `==` above is
    # order-insensitive and cannot catch it; without an explicit sort the projection's json.dumps is
    # not byte-stable across processes (PYTHONHASHSEED), breaking the HTTP etag/caching + diff tests
    # that consume View 1. Assert the concrete insertion order both times.
    assert list(a["nodes"]) == sorted(a["nodes"]) == list(b["nodes"])
