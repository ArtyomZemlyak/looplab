"""Agentic hypothesis-board merge: the engine decides paraphrase merges (hybrid retrieval + agent) and
records `hypothesis_merged` events; the FOLD applies them deterministically (alias evidence -> canonical,
no LLM in the fold). Replay-safe, order-tolerant, back-compat."""
from looplab.core.models import RunState, hypothesis_id
from looplab.events.replay import _derive_hypotheses


def _state(added, merged=None):
    st = RunState(goal="g", direction="max")
    st.hypotheses_added = added
    st.hypotheses_merged = merged or []
    return st


def test_no_merge_events_leaves_board_untouched():
    h1, h2 = "increase the learning rate", "add dropout regularization"
    st = _state([{"statement": h1, "id": hypothesis_id(h1), "at_node": 1},
                 {"statement": h2, "id": hypothesis_id(h2), "at_node": 2}])
    _derive_hypotheses(st)
    assert len(st.hypotheses) == 2                       # back-compat: no merge -> nothing folded


def test_merge_folds_alias_into_canonical():
    h1, h2 = "increase the learning rate", "use a higher LR"
    id1, id2 = hypothesis_id(h1), hypothesis_id(h2)
    st = _state([{"statement": h1, "id": id1, "at_node": 1},
                 {"statement": h2, "id": id2, "at_node": 2}],
                [{"canonical": id1, "aliases": [id2], "statement": "raise the learning rate"}])
    _derive_hypotheses(st)
    assert list(st.hypotheses) == [id1]                  # only the canonical survives
    assert st.hypotheses[id1].statement == "raise the learning rate"


def test_merge_unions_evidence_from_nodes():
    # two nodes each state a different paraphrase; merging must union their evidence onto the canonical
    from looplab.core.models import Node, Idea, NodeStatus
    h1, h2 = "raise learning rate", "increase lr"
    id1, id2 = hypothesis_id(h1), hypothesis_id(h2)
    st = _state([], [{"canonical": id1, "aliases": [id2], "statement": "raise the LR"}])
    st.nodes = {
        1: Node(id=1, operator="draft", idea=Idea(operator="draft", params={}, hypothesis=h1),
                status=NodeStatus.evaluated),
        2: Node(id=2, operator="draft", idea=Idea(operator="draft", params={}, hypothesis=h2),
                status=NodeStatus.evaluated),
    }
    _derive_hypotheses(st)
    assert list(st.hypotheses) == [id1]
    assert st.hypotheses[id1].evidence == [1, 2]         # unioned + sorted


def test_merge_resolves_alias_chains():
    a, b, c = "aa aa", "bb bb", "cc cc"
    ia, ib, ic = hypothesis_id(a), hypothesis_id(b), hypothesis_id(c)
    st = _state([{"statement": a, "id": ia, "at_node": 1},
                 {"statement": b, "id": ib, "at_node": 2},
                 {"statement": c, "id": ic, "at_node": 3}],
                [{"canonical": ib, "aliases": [ic], "statement": "b or c"},
                 {"canonical": ia, "aliases": [ib], "statement": "the one"}])
    _derive_hypotheses(st)
    assert list(st.hypotheses) == [ia]                   # c -> b -> a all collapse to a


def test_merge_is_deterministic_and_order_tolerant():
    h1, h2 = "increase the learning rate", "use a higher LR"
    id1, id2 = hypothesis_id(h1), hypothesis_id(h2)
    added = [{"statement": h1, "id": id1, "at_node": 1}, {"statement": h2, "id": id2, "at_node": 2}]
    merged = [{"canonical": id1, "aliases": [id2], "statement": "raise LR"}]
    a, b = _state(list(added), list(merged)), _state(list(added), list(merged))
    _derive_hypotheses(a)
    _derive_hypotheses(b)
    assert list(a.hypotheses) == list(b.hypotheses)


def test_malformed_merge_event_is_tolerated():
    h1 = "increase the learning rate"
    id1 = hypothesis_id(h1)
    st = _state([{"statement": h1, "id": id1, "at_node": 1}],
                [{"canonical": "", "aliases": []}, {"aliases": ["x"]}, {"canonical": "y"},
                 # truthy but NON-ITERABLE aliases — the dispatch guard admits it (both fields truthy),
                 # so an un-guarded `for a in aliases` would TypeError and brick EVERY subsequent fold.
                 {"canonical": "z", "aliases": 1}, {"canonical": "w", "aliases": True, "statement": 5}])
    _derive_hypotheses(st)                                # must not raise
    assert id1 in st.hypotheses


def test_engine_pass_writes_merge_events_gated(tmp_path, monkeypatch):
    """`_maybe_merge_hypotheses` records `hypothesis_merged` for agent-decided merges, gated on
    track_hypotheses + a client + a grown board; then re-folds so the aliases fold away."""
    import looplab.search.hybrid_merge as hm
    from looplab.engine.orchestrator import Engine
    from looplab.events.eventstore import EventStore

    eng = Engine.__new__(Engine)
    eng._track_hypotheses = True
    eng._embedder = None
    eng._reflect_client = lambda: object()
    eng.store = EventStore(tmp_path / "events.jsonl")

    class _H:
        def __init__(self, hid, stmt):
            self.id, self.statement, self.status = hid, stmt, "open"

    ids = [f"h{i}" for i in range(5)]
    hyps = {i: _H(ids[i], f"statement {i}") for i in range(5)}
    st = RunState(goal="g", direction="max")
    st.hypotheses = hyps
    st.nodes = {}

    # agent merges the first two open hypotheses
    def fake_consolidate(texts, client, **kw):
        return [{"members": [0, 1], "merged": "merged 01"}] + [{"members": [i], "merged": texts[i]}
                                                               for i in range(2, len(texts))]
    # monkeypatch (NOT raw assignment) so pytest RESTORES these module globals at teardown — a bare
    # `hm.consolidate = ...` / `orch.fold = ...` leaks into every later test in the process: the
    # leaked `orch.fold` (returning this fixed st with empty .nodes) made a real Engine's `_create_node`
    # compute node_id=max({},default=-1)+1==0 forever, spinning the whole suite (184MB log / 95% CPU).
    monkeypatch.setattr(hm, "consolidate", fake_consolidate)
    # `_maybe_merge_hypotheses` moved to engine/research_cadence.py, which binds `fold` from its
    # canonical home — patch BOTH modules so the returned state stays simple for the assertion
    # (the orchestrator patch alone stopped reaching it after the mixin extraction).
    import looplab.engine.orchestrator as orch
    import looplab.engine.research_cadence as rc
    monkeypatch.setattr(orch, "fold", lambda evs: st)
    monkeypatch.setattr(rc, "fold", lambda evs: st)

    eng._maybe_merge_hypotheses(st)
    rows = list(EventStore(tmp_path / "events.jsonl").read_all())
    merged = [e for e in rows if e.type == "hypothesis_merged"]
    assert len(merged) == 1
    assert merged[0].data["canonical"] == "h0" and merged[0].data["aliases"] == ["h1"]

    # gate: too-small board -> no write
    eng2 = Engine.__new__(Engine)
    eng2._track_hypotheses = True
    eng2._embedder = None
    eng2._reflect_client = lambda: object()
    eng2.store = EventStore(tmp_path / "e2.jsonl")
    small = RunState(goal="g", direction="max")
    small.hypotheses = {0: _H("a", "x"), 1: _H("b", "y")}      # only 2 open (< 4)
    eng2._maybe_merge_hypotheses(small)
    assert not (tmp_path / "e2.jsonl").exists() or not list(EventStore(tmp_path / "e2.jsonl").read_all())
