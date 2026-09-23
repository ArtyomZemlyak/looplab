"""Memora harmonic memory (idea import): abstraction+anchor indexing, consolidation on write, and
anchor-expansion on retrieval — layered over KnowledgeTools as an OPT-IN mode that degrades
to the exact pre-Memora behavior with no abstractor and to a deterministic lexical abstractor with no
LLM. All offline (no real network/model)."""
from __future__ import annotations

from looplab.core.config import Settings
from looplab.tools.memora import (Abstraction, CachedAbstractor, LLMAbstractor, chat_completer,
                            expand_by_anchors, lexical_abstraction, make_abstractor)
from looplab.tools.knowledge_tools import KnowledgeTools
from looplab.tools.vectorstore import InMemoryVectorStore, hash_embed


# --------------------------- abstraction primitives ------------------------ #
def test_lexical_abstraction_is_deterministic_and_strips_stopwords():
    a = lexical_abstraction("Ridge regression with ridge lambda to reduce overfitting on the data")
    b = lexical_abstraction("Ridge regression with ridge lambda to reduce overfitting on the data")
    assert a == b                                        # pure/reproducible
    assert "the" not in a.anchors and "data" not in a.anchors  # stopwords dropped
    assert "ridge" in a.anchors                          # the repeated salient cue is an anchor
    assert "ridge" in a.index_text() and a.primary       # index text = primary + anchors


def test_abstraction_merge_unions_anchors_and_keeps_richer_primary():
    a = Abstraction("short one", ["x", "y"])
    b = Abstraction("a much longer essence phrase", ["y", "z"])
    m = a.merge(b)
    assert m.primary == "a much longer essence phrase"   # richer (longer) primary wins
    assert m.anchors == ["x", "y", "z"]                  # order-preserving union, deduped


def test_make_abstractor_on_by_default_and_disablable():
    ab = make_abstractor(Settings())                     # memora ON by default -> lexical abstractor
    assert ab is not None and isinstance(ab("hello ridge ridge world"), Abstraction)
    assert make_abstractor(Settings(memora=False)) is None  # opt-out -> callers stay legacy


# ------------------------------ LLM abstractor ----------------------------- #
def test_llm_abstractor_parses_json_reply():
    def complete(prompt):
        return 'sure: {"abstraction": "updated orion timeline", "anchors": ["Orion", "timeline"]}'
    ab = LLMAbstractor(complete)("Dave and Sarah agreed the new Project Orion timeline")
    assert ab.primary == "updated orion timeline"
    assert ab.anchors == ["orion", "timeline"]           # lowercased, sorted, deduped


def test_llm_abstractor_degrades_to_lexical_and_stays_degraded():
    calls = {"n": 0}

    def dead(prompt):
        calls["n"] += 1
        raise RuntimeError("endpoint down")
    la = LLMAbstractor(dead)
    out = la("polynomial degree selection matters")
    assert isinstance(out, Abstraction) and out.anchors  # fell back to lexical, never raised
    la("another memory")                                 # sticky degrade: no further LLM attempts
    assert calls["n"] == 1


def test_make_abstractor_uses_cached_llm_when_enabled():
    def complete(prompt):
        return '{"abstraction": "x y z", "anchors": ["a"]}'
    ab = make_abstractor(Settings(memora=True, memora_llm=True), complete=complete)
    assert isinstance(ab, CachedAbstractor)              # LLM path is cache-wrapped
    out = ab("some memory content")
    assert out.primary == "x y z" and out.anchors == ["a"]
    # memora_llm off -> deterministic lexical, not the LLM/cache path
    lex = make_abstractor(Settings(memora=True, memora_llm=False), complete=complete)
    assert not isinstance(lex, CachedAbstractor)
    assert isinstance(lex("hello ridge ridge"), Abstraction)


# ------------------------------ cached abstractor -------------------------- #
def test_cached_abstractor_calls_inner_once_per_content():
    calls = {"n": 0}

    def inner(text):
        calls["n"] += 1
        return Abstraction("p", ["a"])
    ca = CachedAbstractor(inner)
    assert ca("hello world") == ca("hello world") and calls["n"] == 1  # second is a hit
    ca("different")
    assert calls["n"] == 2


def test_cached_abstractor_persists_across_instances(tmp_path):
    p = tmp_path / "cache.json"
    calls = {"n": 0}

    def inner(text):
        calls["n"] += 1
        return Abstraction("essence", ["anchor"])
    CachedAbstractor(inner, path=str(p))("mem")
    assert p.exists() and calls["n"] == 1
    out = CachedAbstractor(inner, path=str(p))("mem")    # fresh instance loads the persisted cache
    assert out.primary == "essence" and out.anchors == ["anchor"] and calls["n"] == 1


def test_cached_abstractor_tolerates_a_corrupt_cache_entry(tmp_path):
    # Regression: a structurally-valid but wrong-typed cache value (anchors=null or a number) made
    # `list(...)` raise, contradicting the class's "never raises / a corrupt cache starts empty"
    # contract (the load path only rejects a non-dict top level, not per-entry type drift).
    import json
    p = tmp_path / "cache.json"

    def inner(_text):
        return Abstraction("fresh", ["a"])

    key = CachedAbstractor(inner, path=str(p))._key("mem")
    for bad in (None, 5):
        p.write_text(json.dumps({key: {"primary": "cached", "anchors": bad}}), encoding="utf-8")
        out = CachedAbstractor(inner, path=str(p))("mem")     # loads the corrupt entry, must not raise
        assert isinstance(out, Abstraction)
        assert out.primary == "cached" and out.anchors == []  # non-list anchors coerced to []


def test_cached_abstractor_namespaced_by_model(tmp_path):
    p = tmp_path / "cache.json"
    CachedAbstractor(lambda t: Abstraction("m1", []), path=str(p), namespace="model-1")("x")
    calls = {"n": 0}

    def inner(t):
        calls["n"] += 1
        return Abstraction("m2", [])
    out = CachedAbstractor(inner, path=str(p), namespace="model-2")("x")  # different model -> miss
    assert out.primary == "m2" and calls["n"] == 1


def test_cached_abstractor_tolerates_corrupt_cache(tmp_path):
    p = tmp_path / "cache.json"
    p.write_text("{ not json", encoding="utf-8")
    out = CachedAbstractor(lambda t: Abstraction("ok", []), path=str(p))("x")
    assert out.primary == "ok"                           # corrupt cache -> start empty, still works


def test_chat_completer_adapts_a_chat_client():
    class Client:
        def chat(self, messages, tools, tool_choice="auto"):
            assert tools == [] and tool_choice == "none"
            return {"content": "hi"}
    assert chat_completer(Client())("prompt") == "hi"


# ----------------------------- expand_by_anchors --------------------------- #
def test_expand_by_anchors_empty_without_anchors():
    store = InMemoryVectorStore()
    hits = store.search("kb", hash_embed("q"), 1)        # empty index
    assert expand_by_anchors(store, "kb", hits, hash_embed) == []


# ------------------------- KnowledgeTools (harmonic) ----------------------- #
def _lex(t):
    return lexical_abstraction(t)


def test_knowledge_tools_legacy_is_unchanged(tmp_path):
    (tmp_path / "n.md").write_text("ridge lambda shrinks coefficients", encoding="utf-8")
    kt = KnowledgeTools(str(tmp_path))                   # no abstractor -> legacy
    item = next(iter(kt._index._idx["kb"].values()))
    assert "anchors" not in item.payload                 # raw-text index, no harmonic keys


def test_knowledge_tools_harmonic_indexes_anchors(tmp_path):
    (tmp_path / "n.md").write_text("ridge ridge regularization overfitting penalty", encoding="utf-8")
    kt = KnowledgeTools(str(tmp_path), abstract=_lex)
    item = next(iter(kt._index._idx["kb"].values()))
    assert item.payload["anchors"] and "ridge" in item.payload["anchors"]


def test_knowledge_tools_consolidates_duplicate_notes(tmp_path):
    (tmp_path / "a.md").write_text("gradient boosting tabular classification xgboost", encoding="utf-8")
    (tmp_path / "b.md").write_text("gradient boosting tabular classification xgboost trees",
                                   encoding="utf-8")
    kt = KnowledgeTools(str(tmp_path), abstract=_lex, consolidate_threshold=0.85)
    kb = kt._index._idx["kb"]
    assert len(kb) == 1                                  # two near-duplicates folded into one
    only = next(iter(kb.values()))
    assert only.payload["merged"] == 2
    assert "trees" in only.payload["text"]               # kept the richer (longer) memory value


def test_kb_search_anchor_expansion_surfaces_related_note(tmp_path):
    (tmp_path / "ridge.md").write_text(
        "ridge penalty shrink coefficients regularization regularization overfitting overfitting",
        encoding="utf-8")
    (tmp_path / "lasso.md").write_text(
        "lasso sparsity selection regularization regularization overfitting overfitting",
        encoding="utf-8")
    kt = KnowledgeTools(str(tmp_path), abstract=_lex, k=1)
    out = kt.execute("kb_search", {"query": "ridge penalty shrink coefficients"})
    assert "ridge.md" in out                             # direct hit for the query
    assert "[related via anchors] lasso.md" in out       # reached via shared anchors, not the query

    legacy = KnowledgeTools(str(tmp_path), k=1).execute(
        "kb_search", {"query": "ridge penalty shrink coefficients"})
    assert "lasso.md" not in legacy                      # legacy: no anchor-expansion


def test_abstraction_cache_is_bounded(tmp_path, monkeypatch):
    """The abstraction cache is loaded whole into RAM and re-serialized IN FULL on every miss.

    Unbounded, that is unbounded memory plus quadratic cumulative I/O over a long-lived shared memory
    corpus — each new note rewrites every entry that came before it. Evicting oldest-first bounds
    both; an evicted key simply re-abstracts, which this class already treats as an ordinary perf
    miss. (A compacted on-disk KV is the real fix and stays a separate change; this stops the growth.)

    The bound is driven at a cap of 20, not the shipped 5,000: `_evict` reads the module constant at
    call time, and the property is the eviction ORDER and the trim-on-load, not the number. At the
    real cap this test made 5,025 fsync'd full rewrites — the quadratic I/O it guards against — and
    was the slowest test in the suite at 116 s (review 2026-09-22, TST-04).
    """
    import json

    from looplab.tools import memora
    from looplab.tools.memora import Abstraction, CachedAbstractor

    cap = 20
    monkeypatch.setattr(memora, "_MAX_ABSTRACTION_CACHE", cap)
    c = CachedAbstractor(lambda t: Abstraction(t, []), path=str(tmp_path / "abs.json"))
    for i in range(cap + 25):
        c(f"note {i}")
    assert len(c._cache) == cap
    assert c._key(f"note {cap + 24}") in c._cache, "the newest entry was evicted"
    assert c._key("note 0") not in c._cache, "eviction must drop the OLDEST entries first"

    # an over-large cache persisted by an older build is trimmed on load, not carried forward — and
    # the trim keeps the NEWEST entries (a file written before the bound existed, so over it)
    old = {c._key(f"old {i}"): {"primary": f"old {i}", "anchors": []} for i in range(cap * 3)}
    (tmp_path / "old.json").write_text(json.dumps(old), encoding="utf-8")
    reloaded = CachedAbstractor(lambda t: Abstraction(t, []), path=str(tmp_path / "old.json"))
    assert len(reloaded._cache) == cap
    assert c._key(f"old {cap * 3 - 1}") in reloaded._cache
    assert c._key("old 0") not in reloaded._cache


def test_the_shared_provider_wiring_actually_reaches_a_client(monkeypatch):
    """`memora_llm` DEFAULTS ON, and its whole effect is the `complete` callable this wiring hands
    the abstractor. The extraction of `_make_abstractor` out of `factory.py` (which had both
    factories at module scope) into `providers.py` (which does not) turned the call into a
    `NameError` that the blind except beside it read as "a client we can't build" — so every run
    silently used lexical abstractions and nothing anywhere said so.

    Driven on the observable: what the wiring passes to `make_abstractor`. `None` here is a live
    feature reported as absent."""
    from looplab.agents import providers
    from looplab.tools import memora

    seen: dict = {}
    monkeypatch.setattr(memora, "make_abstractor",
                        lambda settings, complete=None, cache_path=None:
                        seen.setdefault("complete", complete))
    providers._make_abstractor(Settings(memora=True, memora_llm=True))
    assert callable(seen["complete"]), "memora_llm is on and the abstractor got no completer"

    seen.clear()
    providers._make_abstractor(Settings(memora=True, memora_llm=False))
    assert seen["complete"] is None, "memora_llm off must stay lexical and spend nothing"
