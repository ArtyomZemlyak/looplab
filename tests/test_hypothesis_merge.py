"""Agentic belief-board merge (1 card = 1 hypothesis): the engine decides paraphrase merges (hybrid
retrieval + agent) and records `hypothesis_merged` events; the FOLD (`_derive_cards`) applies them
deterministically to the single Card board (alias evidence -> canonical, no LLM). The canonical card's
DISPLAY `statement` becomes the merged text; `seed_statement` stays the original join key.
Replay-safe, order-tolerant, back-compat."""
from looplab.core.models import RunState, hypothesis_id
from looplab.events.replay import _derive_cards


def _state(added, merged=None):
    st = RunState(goal="g", direction="max")
    st.hypotheses_added = added
    st.hypotheses_merged = merged or []
    return st


def test_no_merge_events_leaves_board_untouched():
    h1, h2 = "increase the learning rate", "add dropout regularization"
    st = _state([{"statement": h1, "id": hypothesis_id(h1), "at_node": 1},
                 {"statement": h2, "id": hypothesis_id(h2), "at_node": 2}])
    _derive_cards(st)
    assert len(st.cards) == 2                       # back-compat: no merge -> nothing folded


def test_merge_folds_alias_into_canonical():
    h1, h2 = "increase the learning rate", "use a higher LR"
    id1, id2 = hypothesis_id(h1), hypothesis_id(h2)
    st = _state([{"statement": h1, "id": id1, "at_node": 1},
                 {"statement": h2, "id": id2, "at_node": 2}],
                [{"canonical": id1, "aliases": [id2], "statement": "raise the learning rate"}])
    _derive_cards(st)
    assert list(st.cards) == [id1]                  # only the canonical survives
    assert st.cards[id1].statement == "raise the learning rate"


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
    _derive_cards(st)
    assert list(st.cards) == [id1]
    assert st.cards[id1].evidence == [1, 2]         # unioned + sorted


def test_merge_resolves_alias_chains():
    a, b, c = "aa aa", "bb bb", "cc cc"
    ia, ib, ic = hypothesis_id(a), hypothesis_id(b), hypothesis_id(c)
    st = _state([{"statement": a, "id": ia, "at_node": 1},
                 {"statement": b, "id": ib, "at_node": 2},
                 {"statement": c, "id": ic, "at_node": 3}],
                [{"canonical": ib, "aliases": [ic], "statement": "b or c"},
                 {"canonical": ia, "aliases": [ib], "statement": "the one"}])
    _derive_cards(st)
    assert list(st.cards) == [ia]                   # c -> b -> a all collapse to a


def test_merge_is_deterministic_and_order_tolerant():
    h1, h2 = "increase the learning rate", "use a higher LR"
    id1, id2 = hypothesis_id(h1), hypothesis_id(h2)
    added = [{"statement": h1, "id": id1, "at_node": 1}, {"statement": h2, "id": id2, "at_node": 2}]
    merged = [{"canonical": id1, "aliases": [id2], "statement": "raise LR"}]
    a, b = _state(list(added), list(merged)), _state(list(added), list(merged))
    _derive_cards(a)
    _derive_cards(b)
    assert list(a.cards) == list(b.cards)


def test_malformed_merge_event_is_tolerated():
    h1 = "increase the learning rate"
    id1 = hypothesis_id(h1)
    st = _state([{"statement": h1, "id": id1, "at_node": 1}],
                [{"canonical": "", "aliases": []}, {"aliases": ["x"]}, {"canonical": "y"},
                 # truthy but NON-ITERABLE aliases — the dispatch guard admits it (both fields truthy),
                 # so an un-guarded `for a in aliases` would TypeError and brick EVERY subsequent fold.
                 {"canonical": "z", "aliases": 1}, {"canonical": "w", "aliases": True, "statement": 5}])
    _derive_cards(st)                                # must not raise
    assert id1 in st.cards


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

    from looplab.core.models import Card

    def _card(cid, stmt):
        # 1 card = 1 hypothesis: the merge cadence reads the open BELIEF board (open_research_cards()),
        # so populate real Cards — verdict 'open' (the old status) + a non-empty seed, not selection-ready.
        return Card(id=cid, seed_statement=stmt, statement=stmt, verdict="open", status="proposed")

    ids = [f"h{i}" for i in range(5)]
    cards = {ids[i]: _card(ids[i], f"statement {i}") for i in range(5)}
    st = RunState(goal="g", direction="max")
    st.cards = cards
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
    small.cards = {"a": _card("a", "x"), "b": _card("b", "y")}      # only 2 open (< 4)
    eng2._maybe_merge_hypotheses(small)
    assert not (tmp_path / "e2.jsonl").exists() or not list(EventStore(tmp_path / "e2.jsonl").read_all())


def test_belief_merge_excludes_native_work_item_cards(tmp_path, monkeypatch):
    """Peer review: belief consolidation must exclude native WORK-ITEM cards by IDENTITY — a card that
    owns an action (`selection_provenance.action_source != "none"`), not by transient readiness. Merging
    a receipt-backed work item would collapse its distinct action identity into another card."""
    import looplab.search.hybrid_merge as hm
    from looplab.core.models import Card, CardSelectionProvenance
    from looplab.engine.orchestrator import Engine
    from looplab.events.eventstore import EventStore

    eng = Engine.__new__(Engine)
    eng._track_hypotheses = True
    eng._embedder = None
    eng._reflect_client = lambda: object()
    eng.store = EventStore(tmp_path / "events.jsonl")

    def _belief(cid, stmt):                       # owns no action -> a pure belief (action_source none)
        return Card(id=cid, seed_statement=stmt, statement=stmt, verdict="open", status="proposed")

    # A receipt-bound native work item: owns exactly one action, and NOT selection-ready (default) so the
    # OLD `not selection_ready` filter would have admitted it — the identity filter must exclude it anyway.
    native = Card(id="card-9", seed_statement="native work item", statement="native work item",
                  verdict="open", status="proposed",
                  selection_provenance=CardSelectionProvenance(action_source="node", action_owner_count=1))
    assert native.selection_ready is False

    st = RunState(goal="g", direction="max")
    st.cards = {**{f"h{i}": _belief(f"h{i}", f"belief statement {i}") for i in range(4)},
                "card-9": native}
    st.nodes = {}

    seen = {}
    def fake_consolidate(texts, client, **kw):
        seen["texts"] = list(texts)
        return [{"members": [i], "merged": texts[i]} for i in range(len(texts))]
    monkeypatch.setattr(hm, "consolidate", fake_consolidate)
    import looplab.engine.orchestrator as orch
    import looplab.engine.research_cadence as rc
    monkeypatch.setattr(orch, "fold", lambda evs: st)
    monkeypatch.setattr(rc, "fold", lambda evs: st)

    eng._maybe_merge_hypotheses(st)
    # only the 4 pure beliefs are merge candidates; the native work item is excluded by identity
    assert "native work item" not in seen.get("texts", [])
    assert len(seen.get("texts", [])) == 4


# =================================================================================================
# WHY THIS MERGE STILL MAY NOT RUN FROM THE BACKGROUND LOOP IN CARD MODE
#
# `runs/rubertlite-dr-unified-v6` is the case for lifting the restriction: research ran concurrently
# four times during one 90-minute evaluation, `_maybe_merge_hypotheses`'s gate (>=4 open, +2 since
# the last pass) was satisfied many times over, and `hypothesis_merged` fired ZERO times, because in
# Card mode the consolidator runs only on the joined main-task cadence the run never reached. The
# board finished with eleven cards for five ideas.
#
# It is not lifted, and these two tests are the reason rather than the registry comment. Invariant #1
# asks "does any reader key on this event's POSITION?" — for `hypothesis_merged`, two do.
# =================================================================================================

def _ev(seq, event_type, data):
    from looplab.events.eventstore import Event
    return Event(seq=seq, ts=0.0, type=event_type, data=data)


def _merge_and_drop_log(order):
    """One belief merged into another, and an operator drop naming the ALIAS — in both orders."""
    alpha, beta = "belief alpha", "belief beta"
    a, b = hypothesis_id(alpha), hypothesis_id(beta)
    base = [_ev(0, "run_started", {"goal": "g", "direction": "max"}),
            _ev(1, "hypothesis_added", {"statement": alpha, "source": "deep_research", "at_node": 0}),
            _ev(2, "hypothesis_added", {"statement": beta, "source": "deep_research", "at_node": 0})]
    merge = _ev(3, "hypothesis_merged",
                {"canonical": a, "aliases": [b], "statement": "one belief", "at_node": 0})
    drop = _ev(4, "card_dropped",
               {"id": b, "reason": "operator abandoned the direction", "dropped_by": "operator"})
    return base + ([merge, drop] if order == "merge_first" else [drop, merge]), a


def test_a_background_merge_could_land_either_side_of_an_operator_drop():
    """`hypothesis_merged` is NOT splice-neutral: the FOLD ITSELF keys on its position.

    `replay._on_hypothesis_merged` stamps `_event_index` onto the receipt and
    `card_ledger._CardAliases.canon_at` resolves a control through only the merge edges durable AT
    that index. So the same three facts — two beliefs, one merge, one operator drop of the alias —
    fold to a DIFFERENT board depending on which side of the drop the merge landed on. A background
    writer's position against an operator control event is thread schedule, which is why this event
    is in `NON_CARD_SELECTION_BACKGROUND_APPENDABLE` and not in `BACKGROUND_APPENDABLE`."""
    from looplab.events.replay import fold

    merge_first, canonical = _merge_and_drop_log("merge_first")
    drop_first, _ = _merge_and_drop_log("drop_first")
    after_merge_first = fold(merge_first)
    after_drop_first = fold(drop_first)

    # Same surviving card either way — and opposite verdicts on the operator's drop.
    assert list(after_merge_first.cards) == [canonical] == list(after_drop_first.cards)
    assert after_merge_first.cards[canonical].status == "dropped"
    assert after_merge_first.cards[canonical].dropped_reason == "operator abandoned the direction"
    assert after_drop_first.cards[canonical].status != "dropped"
    assert after_drop_first.cards[canonical].dropped_reason is None


def test_a_background_merge_would_move_the_paid_proposal_fence():
    """The second position-keyed reader: `speculation._proposal_authority_seq`.

    It is captured before the slow paid `_prepare_node_idea` and compared for EQUALITY at commit, and
    it excludes `DIAGNOSTIC_EVENTS` plus the two LLM-accounting rows — nothing else.
    `hypothesis_merged` is FOLDED, so it is none of those, and a merge appended from the concurrent
    loop while the Developer call is in flight discards a proposal the run has already paid for."""
    from looplab.engine.orchestrator import Engine
    from looplab.events.types import EV_HYPOTHESIS_MERGED

    before = [_ev(0, "run_started", {"goal": "g", "direction": "max"})]
    assert Engine._proposal_authority_seq(before) == 0
    merged = before + [_ev(1, EV_HYPOTHESIS_MERGED,
                           {"canonical": "a", "aliases": ["b"], "statement": "m"})]
    assert Engine._proposal_authority_seq(merged) == 1        # the fence moved: the CAS now fails
    # …whereas a diagnostic row in the same window does not, which is the property that made the
    # background research task legal in the first place.
    diagnostic = before + [_ev(1, "train_monitor_alert", {"node_id": 0, "verdict": "healthy"})]
    assert Engine._proposal_authority_seq(diagnostic) == 0
