"""A Card build's COMMIT asks the one parent fence (review 2026-09-22, ENG1-11).

`speculation.py::_create_precoded_node` revalidates a reserved Card build on the commit fold and
separates a GENUINE supersession (the Card is dropped) from a TRANSIENT freeze (the Card is kept for a
later rebuild). Its parent half was the last inline copy of `node_build.py::parent_generations_current`
— a negated `any` over the four clauses every other creation path already shares — and it reads the
helper now. The helper's own truth table is `tests/test_node_commit_epilogue.py`'s; what this file
pins is that the commit ASKS it, which no existing test did: dropping the parent clause from the
commit entirely left the whole Card suite green.

DRIVEN through the real Card lane: an `improve` Card of an evaluated parent is elected, built and
claimed, and in the window between the claim's `node_building` CAS and the commit's revalidation the
parent is re-attempted, struck or aborted — each a CONTROL event an operator can land at any moment.
The hook also records that the world-change moved nothing ELSE the commit fences on (the epoch, the
Card's own drop/merge), so the supersession it observes can only be the parent clause's. The control
arm runs the identical fixture with no world-change and requires the child to land, so a lane that
simply stopped committing cannot pass.
"""
from __future__ import annotations

import pytest

from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.events.types import (
    EV_CARD_ADDED,
    EV_CARD_BUILD_DONE,
    EV_NODE_ABORT,
    EV_NODE_CREATED,
    EV_NODE_EVALUATED,
    EV_NODE_FAILED,
    EV_NODE_RESET,
    EV_NODE_TOMBSTONED,
)
# The receipt fixture is AUTOUSE in its own module and stays autouse when imported here: these tests
# admit speculation through the production boundary and then replace only the roles.
from tests.test_card_speculation_engine import (  # noqa: F401  (imported for its autouse effect)
    _admit_unit_speculation_receipt,
    _build_result,
    _engine,
    _request,
    _seed_evaluated_node_zero,
    _start,
)

_CARD = "card-9"
_WORLD_CHANGES = {
    "none": None,
    "reset": (EV_NODE_RESET, {"node_id": 0, "generation": 0, "from_stage": "eval",
                              "reason": "the parent was re-attempted while its child committed"}),
    "tombstoned": (EV_NODE_TOMBSTONED, {"node_ids": [0]}),
    "aborted": (EV_NODE_ABORT, {"node_id": 0, "generation": 0,
                                "reason": "the operator aborted the parent mid-commit"}),
}


@pytest.mark.parametrize("change", list(_WORLD_CHANGES))
def test_a_parent_that_moved_under_the_commit_supersedes_the_card_build(
        tmp_path, monkeypatch, change):
    engine, _producer = _engine(tmp_path / f"precoded-parent-{change}")
    _start(engine)
    engine._ensure_speculation_state()
    _seed_evaluated_node_zero(engine)
    engine.store.append(EV_NODE_EVALUATED, {
        "node_id": 0, "generation": 0, "metric": 1.0, "eval_seconds": 0.0})
    idea = Idea(operator="improve", params={"x": 0.4, "y": -1.0},
                rationale="refine the incumbent",
                hypothesis="a smaller step from the incumbent improves the objective",
                card_id=_CARD)
    action = Engine._card_action(idea, [0], {"0": 0}, 0, 0, scored_against_empty=False)
    engine.store.append(EV_CARD_ADDED, Engine._card_added_payload(
        _CARD, Engine._card_statement(idea), action, idea, source="researcher", at_node=1))
    result = _build_result(engine, _request(engine))
    assert result.success is True and result.action.get("parent_id") == 0

    # THE WINDOW: `_claim_requested_card_build` has written `node_building` and not yet handed the
    # build to `_create_precoded_node`, whose tail-CAS plan is the revalidation under test.
    promote = engine._append_rung_promotion
    after_change = []

    def _land_the_control(commit_action):
        if _WORLD_CHANGES[change] is not None:
            engine.store.append(*_WORLD_CHANGES[change])
        seen = fold(engine.store.read_all())
        card = seen.cards[_CARD]
        after_change.append((seen.search_epoch, card.dropped_reason, card.merged_into,
                             seen.halted))
        return promote(commit_action)

    monkeypatch.setattr(engine, "_append_rung_promotion", _land_the_control)
    engine._spec_builds[result.key] = result
    assert engine._serve_card_builds() is True

    # Nothing the commit fences on moved but the parent: same epoch, the Card neither dropped nor
    # merged, the run not halted (which would be the TRANSIENT branch and keep the Card).
    assert after_change == [(0, None, None, False)]
    events = engine.store.read_all()
    children = [e.data for e in events
                if e.type == EV_NODE_CREATED and e.data.get("node_id") != 0]
    failed = [e.data for e in events if e.type == EV_NODE_FAILED]
    done = [e.data for e in events if e.type == EV_CARD_BUILD_DONE]
    card = fold(events).cards[_CARD]
    if change == "none":
        assert [c["parent_ids"] for c in children] == [[0]], "the control never committed"
        assert failed == []
        assert card.dropped_reason is None
        return
    assert children == [], (
        f"a Card build was committed as the child of a parent that was {change} under it")
    assert [(f["reason"], f["error"]) for f in failed] == [
        ("superseded", "speculative build became stale before commit")]
    # A GENUINE supersession drops the Card; the transient freeze would have kept it.
    assert card.dropped_reason == "superseded"
    assert [d.get("skipped") for d in done] == ["stale"]
