"""A generation that produced no usable tool call must record WHAT it said.

The error branches of `complete_tool` used to stamp `usage`, `cost` and `error` and drop
`output`/`thinking` — so the trace could say a call cost 65,536 completion tokens and could not say
what a single one of them was.

That is exactly backwards. A generation that returned its answer is the one nobody needs to read;
one that burned the whole completion cap without emitting the forced call is the only evidence of
why. Measured 2026-08-20 on an AlgoTune `propose` phase: two calls, 889 s and 593 s, $0.019, both
`no tool_calls in response`, both at exactly the cap — 25 minutes of a run whose wall clock is
68–94 % LLM calls, and nothing kept but the number.
"""
from __future__ import annotations

import pytest

from looplab.core.llm import OpenAICompatibleClient


class _Gen:
    """Records what the client stamps, in the fluent shape `tracing.generation()` returns."""

    def __init__(self):
        self.seen = {}

    def _set(self, key):
        def setter(value):
            self.seen[key] = value
            return self
        return setter

    def __getattr__(self, name):
        return self._set(name)


@pytest.mark.parametrize("reason", ["no tool_calls in response", "forced emit not honored"])
def test_the_model_text_is_recorded_on_an_unusable_generation(reason):
    gen = _Gen()
    msg = {"role": "assistant",
           "content": "<think>let me reconsider the whole task</think>here is prose, not a call",
           "reasoning": ""}
    usage = {"prompt_tokens": 9025, "completion_tokens": 65536, "total_tokens": 74561}

    OpenAICompatibleClient._stamp_unusable_generation(gen, msg, usage, reason)

    assert gen.seen["error"] == reason
    assert gen.seen["usage"] == usage, "the size must survive — it is how the runaway was spotted"
    # The half that used to be lost:
    assert "here is prose, not a call" in gen.seen["output"]
    assert "reconsider the whole task" in gen.seen["thinking"]


def test_thinking_and_answer_are_separated_not_concatenated():
    """The reasoning goes to `thinking` and the answer to `output`, the same split a SUCCESSFUL
    generation gets — otherwise the two surfaces disagree about what a generation produced and the
    error case is the one that reads oddly."""
    gen = _Gen()
    OpenAICompatibleClient._stamp_unusable_generation(
        gen, {"content": "<think>PRIVATE</think>PUBLIC"}, {}, "no tool_calls in response")
    assert "PRIVATE" not in gen.seen["output"]
    assert "PRIVATE" in gen.seen["thinking"]
    assert "PUBLIC" in gen.seen["output"]


def test_an_empty_answer_still_records_the_size_and_the_reason():
    """A model that emits nothing at all is the sharpest version of this: no text to keep, and the
    usage is then the ONLY evidence, so it must not be dropped along with the empty output."""
    gen = _Gen()
    usage = {"completion_tokens": 65536}
    OpenAICompatibleClient._stamp_unusable_generation(gen, {"content": ""}, usage,
                                                      "no tool_calls in response")
    assert gen.seen["usage"] == usage
    assert gen.seen["error"] == "no tool_calls in response"
    assert gen.seen["output"] == "" or isinstance(gen.seen["output"], str)


def test_both_error_branches_route_through_the_one_stamp():
    """Two branches, one rule. `called_names` resolves real `ast.Call` nodes (CLAUDE.md tier 3), so
    a commented-out call cannot satisfy this — and if a third refusal branch is added later without
    the stamp, the count here is what notices."""
    from _source_scan import called_names

    calls = called_names(OpenAICompatibleClient.complete_tool)
    assert calls.count("self._stamp_unusable_generation") == 2, (
        f"expected both unusable-generation branches to stamp the evidence, saw {calls.count('self._stamp_unusable_generation')}")
