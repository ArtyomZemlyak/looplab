"""The one emit-terminated tool loop the genesis planner and boss router share (doc 25 SR-11).

Both hand-built the same scaffolding around `drive_tool_loop`: an `emit` spec carrying a pydantic
model's JSON schema, a result cell, a finalizer that filters kwargs to declared fields and degrades
to an EMPTY model on junk, and the settings-driven turn/time limits. Only the model, the tools and
the prompts differed — and prompts are contracts, so they stay verbatim at the call sites.

What is pinned here is the degradation behaviour. These loops end by rendering a card to a human, so
"the model emitted nonsense" must produce a usable empty plan rather than an exception, and a
hallucinated field must be dropped rather than carried into a model that permits extras.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from looplab.agents import agent as agent_module
from looplab.agents import tool_loop
from looplab.agents.agent import emit_loop


class _Plan(BaseModel):
    # `extra="allow"` on purpose: the helper filters unknown keys itself rather than relying on
    # every caller's model to forbid them, and a model that TOLERATES extras is the only place that
    # difference is observable.
    model_config = {"extra": "allow"}

    reply: str = ""
    actions: list = []


class _Settings:
    agent_max_turns = 7
    agent_time_budget_s = 12.5


@pytest.fixture
def driven(monkeypatch):
    """Capture what `emit_loop` hands to `drive_tool_loop` and let a test drive the callbacks."""
    captured: dict = {}

    def _drive(client, tools, messages, emit_spec, **kwargs):
        captured.update(client=client, tools=tools, messages=messages,
                        emit_spec=emit_spec, **kwargs)
        return None

    monkeypatch.setattr(agent_module, "drive_tool_loop", _drive)
    # A REAL option name. This fixture used to return `{"stuck_retries": 3}` — a misspelling of
    # `stuck_repeat` that no assertion could distinguish from the real thing, because the fake driver
    # below takes `**kwargs` and the bundle was an untyped dict all the way to the call. That is the
    # typo class doc 25 AG-01 closes: `LoopOptions.coerce` now refuses an unknown option by name, so
    # the fixture has to name one that exists — and the property this file pins (the bundle is still
    # spread in, it is not dropped on the floor) is asserted on that real key instead.
    monkeypatch.setattr(tool_loop, "loop_opts_from_settings", lambda s: {"stuck_repeat": 3})
    return captured


def _run(driven, **kwargs):
    return emit_loop("client", "tools", [{"role": "user", "content": "go"}], _Plan, _Settings(),
                     description="Emit the plan.", **kwargs)


def test_the_emit_spec_carries_the_models_schema_under_the_callers_description(driven):
    _run(driven)
    spec = driven["emit_spec"]
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "emit"
    assert spec["function"]["description"] == "Emit the plan."
    assert spec["function"]["parameters"] == _Plan.model_json_schema()


def test_the_loop_limits_come_from_settings_and_are_not_hardcoded(driven):
    """A hardcoded ceiling once cut a slow reasoning model off before it emitted, silently dropping
    the plan to a no-op advisory reply."""
    _run(driven)
    assert driven["max_turns"] == 7 and driven["time_budget_s"] == 12.5
    assert driven["stuck_repeat"] == 3, "loop_opts_from_settings must still be spread in"


def test_absent_limit_settings_default_to_unlimited_rather_than_a_guess(driven):
    class _Bare:
        pass

    emit_loop("c", "t", [], _Plan, _Bare(), description="d")
    assert driven["max_turns"] == 0 and driven["time_budget_s"] == 0.0


def test_a_well_formed_emit_becomes_the_result(driven):
    _run(driven)
    finalized = driven["finalize"]({"reply": "hello", "actions": [{"kind": "pause"}]})
    assert finalized.reply == "hello" and finalized.actions == [{"kind": "pause"}]


def test_unknown_keys_are_dropped_before_the_model_sees_them(driven):
    """A hallucinated field must not reach a model that permits extras."""
    _run(driven)
    finalized = driven["finalize"]({"reply": "ok", "invented": "ignore me"})
    assert finalized.reply == "ok" and not hasattr(finalized, "invented")


@pytest.mark.parametrize("junk", [None, {}, {"reply": ["not", "a", "string"]}, {"actions": 7},
                                  7, "a bare string", [{"reply": "wrapped in a list"}]])
def test_a_junk_emit_degrades_to_an_empty_model_never_an_exception(driven, junk):
    """The caller's next move is to render a card to a human; an exception here loses the whole
    turn, including everything the model just read.

    The non-mapping cases matter as much as the mis-typed ones: a model that emits a scalar or a
    list makes `.items()` raise an AttributeError, which a `ValueError`-only guard would let escape.
    """
    _run(driven)
    finalized = driven["finalize"](junk)
    assert isinstance(finalized, _Plan)


def test_without_a_fallback_the_result_is_whatever_was_emitted(driven):
    _run(driven)
    driven["finalize"]({"reply": "emitted"})
    assert driven["fallback"]([]).reply == "emitted"


def test_a_loop_that_never_emitted_and_has_no_fallback_answers_none(driven):
    assert _run(driven) is None
    assert driven["fallback"]([]) is None


def test_the_fallback_sees_the_pending_messages_and_whatever_was_emitted(driven):
    """The genesis planner forces a final structured call FROM the accumulated messages rather than
    discarding what the model read — so both halves have to reach it."""
    seen = []
    _run(driven, fallback=lambda pending, emitted: seen.append((pending, emitted)) or _Plan(
        reply="forced"))
    answer = driven["fallback"]([{"role": "assistant", "content": "I read the repo"}])
    assert seen == [([{"role": "assistant", "content": "I read the repo"}], None)]
    assert answer.reply == "forced"


def test_the_fallbacks_answer_becomes_the_loop_result(driven, monkeypatch):
    """A caller that forces a final call in its fallback must not also have to write into a cell of
    its own — that mutable-cell bookkeeping is exactly what was duplicated."""
    def _drive_then_fall_back(client, tools, messages, emit_spec, **kwargs):
        kwargs["fallback"](messages)

    monkeypatch.setattr(agent_module, "drive_tool_loop", _drive_then_fall_back)
    result = emit_loop("c", "t", [], _Plan, _Settings(), description="d",
                       fallback=lambda pending, emitted: _Plan(reply="from fallback"))
    assert result.reply == "from fallback"


def test_an_emit_already_seen_is_preferred_over_re_deriving_it(driven, monkeypatch):
    """`drive_tool_loop` can finalize and still take the fallback path; the emitted value must reach
    the fallback so it can short-circuit instead of paying for another round-trip."""
    def _drive_emit_then_fall_back(client, tools, messages, emit_spec, **kwargs):
        kwargs["finalize"]({"reply": "already emitted"})
        kwargs["fallback"](messages)

    monkeypatch.setattr(agent_module, "drive_tool_loop", _drive_emit_then_fall_back)
    calls = []
    result = emit_loop("c", "t", [], _Plan, _Settings(), description="d",
                       fallback=lambda pending, emitted: calls.append(emitted) or emitted)
    assert [plan.reply for plan in calls] == ["already emitted"]
    assert result.reply == "already emitted"
