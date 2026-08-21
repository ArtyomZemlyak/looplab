"""The reasoning depth must be set in ONE place, and two spellings of it are a refusal.

`Settings.llm_reasoning` emits `reasoning_effort` (the OpenAI/DeepSeek spelling).
`Settings.llm_reasoning_extra` is merged by KEY, so an operator who writes OpenRouter's
`{"reasoning": {"effort": "medium"}}` there does NOT override it — they ship both fields and let the
provider decide.

Measured 2026-08-20 on an AlgoTune run configured exactly that way: the request carried
`reasoning_effort: high` beside `reasoning: {effort: medium}`, and two `propose` calls burned the
full 65,536-token completion cap without emitting a tool call — 889 s and 593 s, $0.019, both ERROR
("no tool_calls in response"), both retried. The `medium` that campaign believed it was running at
had never taken effect on a single call.

Refusing beats picking a winner: which field the provider honours is undocumented, so a precedence
rule here would be a guess presented as a setting.
"""
from __future__ import annotations

import pytest

from looplab.core.errors import ConfigRefusal, OperatorRefusal
from looplab.core.llm import reasoning_body


def test_the_two_spellings_together_are_refused():
    with pytest.raises(ConfigRefusal) as got:
        reasoning_body("deepseek/x", "high", extra={"reasoning": {"effort": "medium"}})
    msg = str(got.value)
    # The message must name BOTH keys — that is the whole remedy, and an operator who cannot see
    # which two fields collided cannot pick one.
    assert "reasoning_effort" in msg and "reasoning" in msg
    assert "llm_reasoning" in msg and "llm_reasoning_extra" in msg


def test_it_is_a_typed_refusal_so_the_cli_prints_one_line():
    assert issubclass(ConfigRefusal, OperatorRefusal)


@pytest.mark.parametrize("extra", [
    None,
    {},
    {"provider": {"order": ["siliconflow/fp8"], "allow_fallbacks": False}},
    {"max_tokens": 8192},
])
def test_extra_that_names_no_depth_is_untouched(extra):
    body = reasoning_body("deepseek/x", "high", extra=extra)
    assert body["reasoning_effort"] == "high"
    for k, v in (extra or {}).items():
        assert body[k] == v


def test_the_documented_escape_hatch_still_works():
    """`llm_reasoning=""` sends nothing of ours, so the provider's own spelling in `extra` is the
    only depth in the body and there is nothing to collide with. That is how an operator uses a
    provider key we do not model — the reason `extra` exists."""
    body = reasoning_body("deepseek/x", "", extra={"reasoning": {"effort": "medium"}})
    assert body == {"reasoning": {"effort": "medium"}}
    assert "reasoning_effort" not in body


def test_the_qwen_spelling_collides_too():
    """`chat_template_kwargs` is the third spelling — a qwen model takes the depth there, so an
    `extra` naming it clashes with ours exactly as `reasoning` does. The guard is over the SET of
    depth keys, not over one pair, which is what makes it hold for a fourth."""
    with pytest.raises(ConfigRefusal):
        reasoning_body("qwen3-30b", "high", extra={"chat_template_kwargs": {"enable_thinking": False}})


def test_the_clash_is_between_spellings_and_not_between_values():
    """The guard's first cut compared LIKE keys and therefore detected exactly zero of the case it
    was written for — `reasoning` and `reasoning_effort` are never the same key. Identical VALUES
    under two spellings are still two instructions, and still refused."""
    with pytest.raises(ConfigRefusal):
        reasoning_body("deepseek/x", "medium", extra={"reasoning": {"effort": "medium"}})


def test_the_campaign_sets_the_depth_in_exactly_one_place():
    """The benchmark is the operator we control, and it is the one that got this wrong."""
    import json
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "benchmarks" / "algotune" / "campaign.sh").read_text(encoding="utf-8")
    m = re.search(r"^DEFAULT_REASONING_EXTRA='(.*)'$", src, re.M)
    assert m, "campaign.sh no longer holds a single-quoted REASONING_EXTRA default"
    extra = json.loads(m.group(1))
    assert not ({"reasoning", "reasoning_effort", "chat_template_kwargs", "thinking"} & set(extra)), (
        f"the campaign's REASONING_EXTRA names the depth again: {sorted(extra)}")
    assert "LOOPLAB_LLM_REASONING=" in src, "the campaign no longer sets the depth at all"
