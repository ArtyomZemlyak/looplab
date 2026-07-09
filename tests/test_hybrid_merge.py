"""Hybrid retrieval (lexical + BM25 + vector, RRF-fused) + agent-decided merge — the quality core that
replaces blind single-signal merging (an exact hash / one cosine threshold) everywhere LoopLab merges
similar items."""
import looplab.search.hybrid_merge as hm
from looplab.search.hybrid_merge import (BM25, HybridRetriever, agent_merge,
                                         cluster_near_duplicates, consolidate)
from looplab.search.hybrid_merge import _tokens


def test_bm25_ranks_by_rare_shared_term():
    corpus = ["switch optimizer to AdamW", "increase the learning rate", "add dropout regularization"]
    b = BM25([_tokens(c) for c in corpus])
    scores = b.scores(_tokens("AdamW optimizer"))
    assert scores[0] == max(scores) and scores[0] > 0        # the doc with the rare shared terms wins


def test_hybrid_retriever_fuses_signals():
    corpus = ["increase the learning rate to improve convergence",
              "raise learning rate to 2e-3",
              "add dropout regularization to reduce overfitting"]
    r = HybridRetriever(corpus)
    cands = r.candidates("boost the learning rate", k=3)
    idxs = [i for i, _s in cands]
    assert 0 in idxs and 1 in idxs                            # both LR docs surface
    assert idxs[0] in (0, 1)                                  # an LR doc ranks first, not the dropout one


def test_hybrid_excludes_self_row():
    corpus = ["alpha beta gamma", "alpha beta gamma", "delta epsilon"]
    r = HybridRetriever(corpus)
    cands = r.candidates(corpus[0], k=5, exclude=0)
    assert 0 not in [i for i, _s in cands]                    # the query's own row is dropped
    assert 1 in [i for i, _s in cands]                        # its twin is found


def test_cluster_groups_token_sharing_near_dups():
    texts = ["increase the learning rate for convergence",
             "increase learning rate to speed convergence",
             "totally unrelated optimizer choice adamw"]
    clusters = cluster_near_duplicates(texts, k=4)
    # 0 and 1 share most tokens -> same cluster; the unrelated one may or may not attach, but 0~1 hold
    c01 = next(c for c in clusters if 0 in c)
    assert 1 in c01


def test_agent_merge_fail_open_without_client():
    r = agent_merge(None, ["a", "b", "c"])
    assert r == [{"members": [0], "merged": "a"}, {"members": [1], "merged": "b"},
                 {"members": [2], "merged": "c"}]


def test_agent_merge_repairs_partition(monkeypatch):
    # model double-claims index 1 and references out-of-range 9; repair -> disjoint, full coverage
    def fake(client, msgs, schema, parser):
        return schema(groups=[{"members": [0, 1, 1, 9], "merged": "M01"}, {"members": [1], "merged": "x"}])
    monkeypatch.setattr(hm, "parse_structured", fake)
    r = agent_merge(object(), ["aa", "ab", "cc"])
    assert r == [{"members": [0, 1], "merged": "M01"}, {"members": [2], "merged": "cc"}]


def test_consolidate_maps_cluster_indices_back_to_global(monkeypatch):
    texts = ["increase the learning rate for convergence",
             "increase learning rate to speed convergence",
             "add dropout 0.1 to reduce overfitting"]
    def fake(client, msgs, schema, parser):
        return schema(groups=[{"members": [0, 1], "merged": "raise LR"}])   # cluster-local 0,1
    monkeypatch.setattr(hm, "parse_structured", fake)
    out = consolidate(texts, object(), kind="lessons")
    members = sorted(tuple(g["members"]) for g in out)
    assert (0, 1) in members and (2,) in members             # global indices, full coverage


def test_consolidate_no_client_is_all_singletons():
    out = consolidate(["a", "b", "c"], None)
    assert out == [{"members": [0], "merged": "a"}, {"members": [1], "merged": "b"},
                   {"members": [2], "merged": "c"}]


def test_consolidate_threads_prompts_and_parser_to_agent_merge(monkeypatch):
    # Regression: `consolidate` used to DROP prompts/parser (agent_merge accepted them, the wrapper
    # every production caller uses didn't) — a merge_system.md override / non-tool_call parser never
    # reached the adjudication call.
    seen = {}

    def fake_agent_merge(client, items, *, kind="items", goal="", parser="tool_call", prompts=None):
        seen["parser"], seen["prompts"] = parser, prompts
        return [{"members": [i], "merged": t} for i, t in enumerate(items)]

    monkeypatch.setattr(hm, "agent_merge", fake_agent_merge)
    sentinel = object()
    consolidate(["increase the learning rate now", "increase the learning rate today"],
                object(), parser="json", prompts=sentinel)
    assert seen == {"parser": "json", "prompts": sentinel}


def test_merge_system_renders_kind_and_kind_aware_detail(monkeypatch):
    captured = {}

    def fake(client, msgs, schema, parser):
        captured["sys"], captured["parser"] = msgs[0]["content"], parser
        return schema(groups=[])

    monkeypatch.setattr(hm, "parse_structured", fake)
    # hypotheses: $kind substituted; the pre-existing preserve-the-numbers rule kept verbatim
    agent_merge(object(), ["aa", "ab"], kind="research hypotheses", parser="json")
    assert "candidate research hypotheses" in captured["sys"] and "$kind" not in captured["sys"]
    assert "thresholds, numbers" in captured["sys"]
    assert captured["parser"] == "json"                       # configured parser reaches the call
    # lessons: number-free synthesis rule (lesson statements are deliberately generalizable) —
    # the numbers wording would fight engine.lessons' "NOT these exact numbers" contract
    agent_merge(object(), ["aa", "ab"], kind="research lessons")
    assert "number-free" in captured["sys"] and "thresholds, numbers" not in captured["sys"]


def test_merge_system_promptstore_override_uses_dollar_vars(monkeypatch, tmp_path):
    from looplab.core.prompts import PromptStore
    (tmp_path / "merge_system.md").write_text("Adjudicate these $kind. Preserve $detail.",
                                              encoding="utf-8")
    captured = {}

    def fake(client, msgs, schema, parser):
        captured["sys"] = msgs[0]["content"]
        return schema(groups=[])

    monkeypatch.setattr(hm, "parse_structured", fake)
    agent_merge(object(), ["aa", "ab"], kind="research lessons",
                prompts=PromptStore(str(tmp_path)))
    assert captured["sys"].startswith("Adjudicate these research lessons. Preserve ")
    assert "$kind" not in captured["sys"] and "$detail" not in captured["sys"]


def test_merge_system_legacy_brace_kind_override_still_substituted(monkeypatch, tmp_path):
    # Back-compat: the pre-$var contract substituted a literal `{kind}` AFTER the render, so an
    # existing merge_system.md override written with `{kind}` must keep rendering the real kind.
    from looplab.core.prompts import PromptStore
    (tmp_path / "merge_system.md").write_text("Adjudicate these {kind}.", encoding="utf-8")
    captured = {}

    def fake(client, msgs, schema, parser):
        captured["sys"] = msgs[0]["content"]
        return schema(groups=[])

    monkeypatch.setattr(hm, "parse_structured", fake)
    agent_merge(object(), ["aa", "ab"], kind="research lessons",
                prompts=PromptStore(str(tmp_path)))
    assert captured["sys"] == "Adjudicate these research lessons."
