"""Memora harmonic memory (idea import): abstraction+anchor indexing, consolidation on write, and
anchor-expansion on retrieval — layered over CaseLibrary/KnowledgeTools as an OPT-IN mode that degrades
to the exact pre-Memora behavior with no abstractor and to a deterministic lexical abstractor with no
LLM. All offline (no real network/model)."""
from __future__ import annotations

from looplab.config import Settings
from looplab.memora import (Abstraction, LLMAbstractor, chat_completer, expand_by_anchors,
                            lexical_abstraction, make_abstractor)
from looplab.memory import CaseLibrary
from looplab.knowledge_tools import KnowledgeTools
from looplab.vectorstore import InMemoryVectorStore, hash_embed


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


def test_make_abstractor_off_by_default_and_lexical_when_on():
    assert make_abstractor(Settings()) is None           # memora off -> callers stay legacy
    ab = make_abstractor(Settings(memora=True))
    assert ab is not None and isinstance(ab("hello ridge ridge world"), Abstraction)


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


def test_make_abstractor_uses_llm_when_enabled_and_client_wired():
    def complete(prompt):
        return '{"abstraction": "x y z", "anchors": ["a"]}'
    ab = make_abstractor(Settings(memora=True, memora_llm=True), complete=complete)
    assert isinstance(ab, LLMAbstractor)
    # without memora_llm it stays lexical even with a client available
    assert not isinstance(make_abstractor(Settings(memora=True), complete=complete), LLMAbstractor)


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


# --------------------------- CaseLibrary (harmonic) ------------------------ #
def test_case_library_legacy_unchanged():
    lib = CaseLibrary(InMemoryVectorStore())             # no abstractor
    lib.add("c1", "tabular classification gradient boosting", {"sol": "xgb"})
    hit = lib.retrieve("tabular gradient boosting", k=1)[0]
    assert hit.id == "c1" and "anchors" not in hit.payload


def test_case_library_consolidates_near_duplicate_cases():
    lib = CaseLibrary(InMemoryVectorStore(), abstract=_lex, consolidate_threshold=0.85)
    lib.add("c1", "gradient boosting tabular classification", {"metric": 0.9, "direction": "max"})
    lib.add("c2", "gradient boosting tabular classification", {"metric": 0.95, "direction": "max"})
    cases = lib.store._idx["cases"]
    assert len(cases) == 1                               # merged, not duplicated
    only = next(iter(cases.values()))
    assert only.payload["merged"] == 2
    assert only.payload["metric"] == 0.95                # kept the better metric (direction=max)


def test_case_library_retrieve_expands_through_anchors():
    lib = CaseLibrary(InMemoryVectorStore(), abstract=_lex)
    lib.add("a", "ridge penalty shrink regularization regularization overfitting overfitting", {})
    lib.add("b", "lasso sparsity regularization regularization overfitting overfitting", {})
    lib.add("c", "convolutional image segmentation unet pixels", {})
    hits = lib.retrieve("ridge penalty shrink", k=1)
    ids = [h.id for h in hits]
    assert ids[0] == "a"                                 # direct hit
    assert "b" in ids                                    # anchor-linked, pulled in by expansion
    assert "c" not in ids                                # unrelated, not surfaced


def test_case_library_retain_if_improved_still_works_harmonic():
    lib = CaseLibrary(InMemoryVectorStore(), abstract=_lex)
    assert lib.retain_if_improved("c1", "time series forecast arima", {}, 0.5, "min")
    assert not lib.retain_if_improved("c1", "time series forecast arima", {}, 0.9, "min")
    assert lib.retain_if_improved("c1", "time series forecast arima", {}, 0.2, "min")
    hit = lib.store.get("cases", "c1")
    assert hit.payload["metric"] == 0.2 and hit.payload["anchors"]  # harmonic keys present
