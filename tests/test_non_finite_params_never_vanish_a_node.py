"""A non-finite experiment parameter must cost the PARAMETER, never the whole node (review
2026-09-22, SCJ-04).

The chain, each link measured: a tool-call argument is decoded with `json.loads`, which admits
`NaN`, `Infinity` and `1e309` (-> inf); `Idea.params` is `dict[str, float]`, and pydantic's float
admits both; the event store writes with orjson, which serializes a non-finite float as `null`; and
the fold rebuilds every idea through `Idea(**d["idea"])`, which refuses `null` for a float — so
`replay._on_node_created` skipped the event and the node was ABSENT from every fold, every view and
every resume, while the live process had built, evaluated and paid for it. `merge_idea` reached the
same state by arithmetic: the mean of two finite values near the float ceiling overflowed to inf.

The fix is on the WRITE side only — the fold is unchanged, and must be: orjson refuses to READ
`NaN`/`Infinity`/`1e309`, so no log the store can read carries a non-finite param, and the tolerant
reader dropping one cannot change any existing fold. `IdeaEmission` (the strict modern producer
schema) REFUSES a non-finite value so the model is asked again; `Idea` (the durable reader every
other writer constructs) DROPS it; `merge_idea` skips it and sums without intermediate overflow.
"""
from __future__ import annotations

import json
import math

import pytest
from pydantic import ValidationError

from looplab.core.models import Idea, IdeaEmission, Node
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.operators import merge_idea

# The shape a provider's tool-call arguments decode to: `json.loads` is what `core/llm_toolcall.py`
# uses, and it admits all three spellings.
_ARGS = ('{"operator": "draft", "concept_mode": "full", "rationale": "try it",'
         ' "params": {"lr": NaN, "wd": Infinity, "floor": -Infinity, "huge": 1e309, "ok": 0.5}}')


def _fold_one_created(tmp_path, idea: Idea):
    """Write `idea` the way the engine's emitter does (a `model_dump` into `node_created`) through
    the real orjson store, and fold what a resume would read back."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": idea.operator,
                                  "idea": idea.model_dump()})
    return fold(EventStore(tmp_path / "events.jsonl").read_all())


def test_a_non_finite_param_is_dropped_and_the_node_survives_the_fold(tmp_path):
    args = json.loads(_ARGS)
    assert math.isnan(args["params"]["lr"]) and args["params"]["huge"] == math.inf
    idea = Idea(**args)
    assert idea.params == {"ok": 0.5}
    state = _fold_one_created(tmp_path, idea)
    assert sorted(state.nodes) == [0], "the node vanished from the fold"
    assert state.nodes[0].idea.params == {"ok": 0.5}


def test_the_strict_producer_schema_refuses_a_non_finite_param_so_the_model_is_asked_again():
    with pytest.raises(ValidationError, match="finite"):
        IdeaEmission.model_validate(json.loads(_ARGS))
    # A finite emission is untouched, and crosses into the durable model unchanged.
    ok = IdeaEmission.model_validate(json.loads(
        '{"operator": "draft", "concept_mode": "full", "params": {"lr": 0.01, "n": 3}}'))
    assert ok.to_idea().params == {"lr": 0.01, "n": 3.0}


def test_the_agentic_emit_is_bounced_instead_of_collapsing_to_an_empty_draft():
    """Before, the agentic Researcher ACCEPTED the emit, then `to_idea()`'s JSON dump turned NaN into
    `null`, the durable validation refused it, and `_finalize` fell back to a draft with NO params —
    every other parameter the model chose was lost too. Now the pre-accept check bounces the emit
    back to the model with the reason."""
    from looplab.agents.agent import ToolUsingResearcher

    class _Idle:
        def complete_tool(self, *_a, **_kw):
            raise AssertionError("not called")

        def complete_text(self, *_a, **_kw):
            raise AssertionError("not called")

    researcher = ToolUsingResearcher(_Idle(), tools=None)
    bounced = researcher._validate_emit(json.loads(_ARGS))
    assert bounced is not None and "finite" in bounced


def test_a_non_finite_grid_point_costs_the_point_and_an_all_non_finite_grid_costs_the_dimension(
        tmp_path):
    """`Idea.space` is the same float, one level down, and vanished the node the same way."""
    idea = Idea(operator="draft", params={"ok": 0.5},
                space=json.loads('{"lr": [0.1, NaN, 0.3], "wd": [Infinity, 1e309], "d": [1, 2]}'))
    assert idea.space == {"lr": [0.1, 0.3], "d": [1.0, 2.0]}
    state = _fold_one_created(tmp_path, idea)
    assert state.nodes[0].idea.space == {"lr": [0.1, 0.3], "d": [1.0, 2.0]}
    with pytest.raises(ValidationError, match="finite"):
        IdeaEmission.model_validate({"operator": "draft", "concept_mode": "full",
                                     "space": {"lr": [0.1, float("nan")]}})


def test_a_healed_idea_is_a_fixed_point_of_its_own_validation():
    """Card action digests are minted from an idea and re-derived by REBUILDING it, so a healed idea
    must rebuild to itself (the `_clamp_fill` incident in `agents/roles.py` is what a non-fixed
    point costs: a Card that can never be claimed again)."""
    idea = Idea(**json.loads(_ARGS))
    assert Idea.model_validate(idea.model_dump()) == idea
    assert Idea.model_validate(idea.model_dump(mode="json")) == idea


def test_a_value_that_is_not_a_number_at_all_is_still_the_fields_own_decision():
    """The drop is for NON-FINITE numbers only: a value that is not a number keeps failing exactly
    as it always did, and a stored `null` still fails the fold as it always did (the fold is not
    changed by this fix; healing old logs would be a separate, invariant-5 decision)."""
    with pytest.raises(ValidationError):
        Idea(operator="draft", params={"name": "linear"})
    with pytest.raises(ValidationError):
        Idea(operator="draft", params={"lr": None})
    assert Idea(operator="draft", params={"flag": True, "n": "3"}).params == {"flag": 1.0, "n": 3.0}


def test_merge_idea_neither_overflows_nor_carries_a_non_finite_parent_value(tmp_path):
    big = [Node(id=i, operator="draft", idea=Idea(operator="draft", params={"x": 1e308, "y": 1.0}))
           for i in (0, 1)]
    merged = merge_idea(big)
    assert merged.params == {"x": 1e308, "y": 1.0}           # the MEAN of 1e308s, not inf
    # An in-memory parent can still hold a non-finite value (`model_construct`, or a mutation after
    # validation): the mean skips it rather than propagating it.
    odd = Node(id=2, operator="draft",
               idea=Idea.model_construct(operator="draft", params={"x": math.nan, "y": 3.0}))
    merged = merge_idea([big[0], odd])
    assert merged.params == {"x": 1e308, "y": 2.0}
    state = _fold_one_created(tmp_path, merged)
    assert state.nodes[0].idea.params == {"x": 1e308, "y": 2.0}
