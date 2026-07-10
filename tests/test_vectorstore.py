

def test_search_drops_dimension_mismatch_zeros():
    # a query embedded at a different dim than the stored vectors -> cosine 0.0 -> not a real hit
    from looplab.tools.vectorstore import InMemoryVectorStore, Item
    vs = InMemoryVectorStore()
    vs.upsert("i", [Item("a", [1.0, 0.0], {}), Item("b", [0.0, 1.0], {})])
    assert vs.search("i", [1.0, 0.0, 0.0], 3) == []          # 3-dim query vs 2-dim store -> no hits
    assert [h.id for h in vs.search("i", [1.0, 0.0], 3)] == ["a"]   # matching dim still works
