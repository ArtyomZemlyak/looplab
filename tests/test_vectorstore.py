

def test_search_drops_dimension_mismatch_zeros():
    # a query embedded at a different dim than the stored vectors -> cosine 0.0 -> not a real hit
    from looplab.tools.vectorstore import InMemoryVectorStore, Item
    vs = InMemoryVectorStore()
    vs.upsert("i", [Item("a", [1.0, 0.0], {}), Item("b", [0.0, 1.0], {})])
    assert vs.search("i", [1.0, 0.0, 0.0], 3) == []          # 3-dim query vs 2-dim store -> no hits
    assert [h.id for h in vs.search("i", [1.0, 0.0], 3)] == ["a"]   # matching dim still works


def test_concurrent_upsert_during_search_does_not_raise():
    """The index dicts are shared mutable state and the engine runs worker threads (concurrent
    research, llm_parallel). `search` iterated `store.values()` unguarded, so an `upsert`/`delete`
    landing mid-scan raised RuntimeError("dictionary changed size during iteration")."""
    import threading

    from looplab.tools.vectorstore import InMemoryVectorStore, Item

    vs = InMemoryVectorStore()
    vs.upsert("i", [Item(f"seed{n}", [1.0, 0.0], {}) for n in range(200)])
    stop = threading.Event()
    errors: list = []

    def _write():
        n = 0
        while not stop.is_set():
            vs.upsert("i", [Item(f"w{n}", [1.0, 0.0], {})])
            vs.delete("i", [f"w{max(0, n - 50)}"])
            n += 1

    writer = threading.Thread(target=_write, daemon=True)
    writer.start()
    try:
        for _ in range(400):
            try:
                vs.search("i", [1.0, 0.0], 5)
            except Exception as exc:  # noqa: BLE001 — the whole point is that nothing escapes
                errors.append(exc)
                break
    finally:
        stop.set()
        writer.join(5)
    assert not errors, errors
