"""BACKGROUND_APPENDABLE (engine invariant #1's one enforced exception — docs/15 §P4.1).

The concurrent-research task appends from a background coroutine (`orchestrator._spawn_research`
-> `research_cadence._record_deep_research`), which is safe ONLY while every such event type is
selection-neutral and order-tolerant in the fold: its position in events.jsonl depends on the
thread schedule, so if it could affect which node wins, replay would be nondeterministic.
These tests turn that prose argument into a red test. ``hypothesis_merged`` has a separate
non-Card-selection registry because it now changes native Card ownership/readiness:
  1. registry sanity — the set exists, is registered, and stays a subset of ALL_EVENT_TYPES;
  2. selection neutrality — folding the SAME log with a background event spliced at EVERY
     position yields the identical best node and node set;
  3. source guard — the background task's code path appends only via `_record_deep_research`
     (the method the assertions gate), mirroring test_signal_delivery's needle discipline.
"""
from __future__ import annotations

import inspect

from looplab.core.models import Event, card_ownership_receipt
from looplab.events.replay import fold
from looplab.events.types import (ALL_EVENT_TYPES, BACKGROUND_APPENDABLE,
                                   EV_CARD_BUILD_DONE, EV_CARD_BUILD_REQUESTED, EV_HINT,
                                   EV_HYPOTHESIS_ADDED, EV_HYPOTHESIS_MERGED, EV_LLM_USAGE,
                                   EV_RESEARCH_COMPLETED,
                                   NON_CARD_SELECTION_BACKGROUND_APPENDABLE)


def _base_events() -> list[Event]:
    """A tiny two-node run: node 1 wins under direction=min."""
    rows = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "min"}),
        ("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                          "idea": {"operator": "draft", "params": {"x": 1.0}}, "code": ""}),
        ("node_evaluated", {"node_id": 0, "metric": 5.0, "eval_seconds": 0.1}),
        ("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                          "idea": {"operator": "improve", "params": {"x": 2.0}}, "code": ""}),
        ("node_evaluated", {"node_id": 1, "metric": 1.0, "eval_seconds": 0.1}),
    ]
    return [Event(seq=i, ts=float(i), type=t, data=d) for i, (t, d) in enumerate(rows)]


def _payload(etype: str) -> dict:
    if etype == EV_RESEARCH_COMPLETED:
        return {"memo": {"summary": "s"}, "at_node": 0, "trigger": "auto", "served_manual": False}
    if etype == EV_HINT:
        return {"text": "try this research direction: x", "kind": "steer"}
    if etype == EV_HYPOTHESIS_ADDED:
        return {"statement": "explore larger x", "source": "deep_research", "at_node": 0}
    if etype == EV_HYPOTHESIS_MERGED:
        return {"canonical": "h1", "aliases": ["h2"], "statement": "explore larger x", "at_node": 0}
    if etype == EV_LLM_USAGE:
        return {"cost": 0.001, "calls": 1, "prompt_tokens": 10,
                "completion_tokens": 2, "total_tokens": 12}
    raise AssertionError(f"add a payload builder for new background type {etype!r}")


def test_registry_sane():
    assert BACKGROUND_APPENDABLE, "the enforced exception set must not be empty"
    assert BACKGROUND_APPENDABLE <= ALL_EVENT_TYPES
    # Growing this set is a DECISION, not a drive-by: a new member needs a payload builder above
    # and must pass the splice test below. This assertion forces that edit to happen here.
    assert BACKGROUND_APPENDABLE == frozenset({
        EV_RESEARCH_COMPLETED, EV_HINT, EV_HYPOTHESIS_ADDED, EV_LLM_USAGE,
    })
    assert NON_CARD_SELECTION_BACKGROUND_APPENDABLE == frozenset({EV_HYPOTHESIS_MERGED})
    assert NON_CARD_SELECTION_BACKGROUND_APPENDABLE <= ALL_EVENT_TYPES
    assert BACKGROUND_APPENDABLE.isdisjoint(NON_CARD_SELECTION_BACKGROUND_APPENDABLE)
    assert {EV_CARD_BUILD_REQUESTED, EV_CARD_BUILD_DONE}.isdisjoint(BACKGROUND_APPENDABLE)


def test_background_events_are_selection_neutral_at_every_position():
    base = _base_events()
    for etype in sorted(BACKGROUND_APPENDABLE):
        ref = fold(base)
        for pos in range(1, len(base) + 1):     # after run_started .. at the tail
            spliced = base[:pos] + [Event(seq=99, ts=99.0, type=etype,
                                          data=_payload(etype))] + base[pos:]
            st = fold(spliced)
            assert st.best_node_id == ref.best_node_id, (etype, pos)
            assert set(st.nodes) == set(ref.nodes), (etype, pos)
            assert [n.metric for n in st.nodes.values()] == [n.metric for n in ref.nodes.values()]


def test_hypothesis_merge_is_not_background_neutral_for_native_card_selection():
    def added(card_id: str, statement: str) -> tuple[str, dict]:
        action = {
            "operator": "draft", "params": {}, "space": None,
            "eval_profile": None, "eval_timeout": None,
            "parent_id": None, "parent_ids": [], "parent_generations": {},
            "scored_against": None, "scored_against_generation": None,
            "scored_against_empty": True, "footprint": None,
        }
        receipt = card_ownership_receipt(card_id, statement, action)
        assert receipt is not None
        return "card_added", {
            "id": card_id, "statement": statement, "source": "engine",
            "idea": {"operator": "draft", "params": {}, "eval_timeout": None},
            "parent_id": None, "parent_ids": [], "parent_generations": {},
            "scored_against": None, "scored_against_generation": None,
            "scored_against_empty": True, "ownership_receipt": receipt,
        }

    rows = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "min"}),
        added("card-a", "first ready action"),
        added("card-b", "second ready action"),
    ]
    before = fold([
        Event(seq=index, type=kind, data=data)
        for index, (kind, data) in enumerate(rows)
    ])
    assert all(card.selection_ready for card in before.cards.values())

    rows.append((EV_HYPOTHESIS_MERGED, {
        "canonical": "card-a", "aliases": ["card-b"],
        "statement": "merged direction", "at_node": 0,
    }))
    after = fold([
        Event(seq=index, type=kind, data=data)
        for index, (kind, data) in enumerate(rows)
    ])
    assert list(after.cards) == ["card-a"]
    assert after.cards["card-a"].selection_ready is False
    assert "merged_work_items" in after.cards["card-a"].selection_blockers


def test_background_task_appends_only_via_the_gated_method():
    # The background research coroutines must reach the store ONLY through _record_deep_research
    # (whose appends carry the BACKGROUND_APPENDABLE assertions). A direct store.append added to the
    # one-shot spawn OR the repeating overlap loop would bypass the gate silently — this source scan
    # turns that into a red test. Both the one-shot (_spawn_research::_bg) and the repeating
    # (_research_overlap_loop) background writers are covered.
    import looplab.engine.orchestrator as orch
    for meth in ("_spawn_research", "_research_overlap_loop"):
        src = inspect.getsource(getattr(orch.Engine, meth))
        assert "store.append" not in src, f"{meth} must not append directly — see BACKGROUND_APPENDABLE"
        assert "_record_deep_research" in src, f"{meth} must write only via _record_deep_research"
