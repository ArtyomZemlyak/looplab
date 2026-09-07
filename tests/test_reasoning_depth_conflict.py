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

WHAT THE GUARD MAY NOT DO, and did until 2026-08-22: refuse a key because of its NAME. The first
predicate was the flat set `{reasoning, reasoning_effort, chat_template_kwargs, thinking}` on both
sides, which reads "either side uses that key for anything" and not "either side sets the DEPTH".
Two documented, coherent configurations were refused by it — OpenRouter's
`{"reasoning": {"exclude": true}}` (hide the reasoning tokens from the RESPONSE; no depth in it at
all) and any non-thinking `chat_template_kwargs` on a Qwen target — and both are exactly what
`reasoning_body`'s own docstring offers `extra` FOR. `REASONING_DEPTH_KNOBS` is where the
distinction now lives, and every test below that needs to know which fields set the depth reads it
from there rather than restating it: a fifth provider spelling added to the table inherits the
guard, and a member moved out of it is asserted to become permissible in the same edit.

The permission and the MERGE are one change, not two. `chat_template_kwargs` is the one key both
sides shape, so permitting a non-depth member of it under a flat `{**body, **extra}` would have
dropped our `enable_thinking` — trading a loud refusal for the silent loss of the depth setting that
this whole guard exists to stop. `test_a_non_depth_chat_template_kwarg_survives_beside_the_depth`
is the one that holds that line.
"""
from __future__ import annotations

import pytest

from looplab.core.errors import ConfigRefusal, OperatorRefusal
from looplab.core.llm import REASONING_DEPTH_KNOBS, reasoning_body, reasoning_depth_keys


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
    """The benchmark is the operator we control, and it is the one that got this wrong.

    The question asked of the campaign's default is the SAME question the guard asks of any
    operator's `llm_reasoning_extra` — so it is asked with the same function. Restating the depth
    keys here (as this test did) makes the test agree with a copy of the table instead of with the
    table, which is how the campaign's default and the guard drift apart in opposite directions."""
    import json
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "benchmarks" / "algotune" / "campaign.sh").read_text(encoding="utf-8")
    m = re.search(r"^DEFAULT_REASONING_EXTRA='(.*)'$", src, re.M)
    assert m, "campaign.sh no longer holds a single-quoted REASONING_EXTRA default"
    extra = json.loads(m.group(1))
    named = reasoning_depth_keys(extra)
    assert not named, f"the campaign's REASONING_EXTRA names the depth again: {named}"
    assert "LOOPLAB_LLM_REASONING=" in src, "the campaign no longer sets the depth at all"
    # And the pair the campaign actually ships is accepted end to end, on both target styles: the
    # provider pin is not a depth setting and must never have been treated as one.
    for model in ("deepseek/deepseek-v4-flash-0731", "qwen3-30b"):
        assert reasoning_body(model, "medium", extra=extra)


# ---------------------------------------------------------------------------------------------
# Which fields set the DEPTH — both directions, derived from `REASONING_DEPTH_KNOBS`.
# ---------------------------------------------------------------------------------------------

def _registered_depth_settings():
    """Every (label, payload) the registry says IS a depth setting. Derived, never listed."""
    for key, members in REASONING_DEPTH_KNOBS.items():
        if members is None:
            yield f"{key} (whole key)", {key: "set-by-the-operator"}
        else:
            for member in members:
                yield f"{key}.{member}", {key: {member: "set-by-the-operator"}}


@pytest.mark.parametrize("label,payload",
                         list(_registered_depth_settings()),
                         ids=[label for label, _ in _registered_depth_settings()])
def test_every_registered_depth_knob_still_clashes_with_llm_reasoning(label, payload):
    """The refusing direction, driven off the table so a spelling added to it inherits the guard.

    Asked on the target style that does NOT already shape this key, so the clash is always between
    two DIFFERENT request fields — the shape the incident took."""
    assert reasoning_depth_keys(payload), f"{label} is registered as depth but not detected as one"
    model = "gpt-5" if "chat_template_kwargs" in payload else "qwen3-30b"
    with pytest.raises(ConfigRefusal):
        reasoning_body(model, "high", extra=payload)


@pytest.mark.parametrize("key", sorted(k for k, m in REASONING_DEPTH_KNOBS.items() if m is not None))
def test_a_member_the_registry_does_not_name_is_not_a_depth_setting(key):
    """The permitting direction, and the falsifier for the defect: a scoped key's OTHER members are
    the operator's to use. The member name is built FROM the registered ones, so it cannot collide
    with a member the table gains later."""
    members = REASONING_DEPTH_KNOBS[key]
    outsider = "not_" + "_nor_".join(members)
    assert reasoning_depth_keys({key: {outsider: True}}) == []
    body = reasoning_body("qwen3-30b", "high", extra={key: {outsider: True}})
    assert body[key][outsider] is True


@pytest.mark.parametrize("key", sorted(k for k, m in REASONING_DEPTH_KNOBS.items() if m is not None))
def test_a_scoped_key_whose_value_is_not_a_mapping_fails_closed(key):
    """`{"reasoning": "high"}` names no member this code can read, and an operator who writes it
    means the depth. Nothing can be inspected, so the whole key counts."""
    assert reasoning_depth_keys({key: "high"}) == [key]


def test_openrouter_hide_reasoning_tokens_is_accepted():
    """Regression 1. `reasoning.exclude` suppresses the reasoning tokens in the RESPONSE — it is a
    fact about what comes back, not about how hard the model thinks, so it does not contradict
    `llm_reasoning="high"` and both fields must reach the request."""
    body = reasoning_body("deepseek/x", "high", extra={"reasoning": {"exclude": True}})
    assert body == {"reasoning_effort": "high", "reasoning": {"exclude": True}}


def test_a_non_depth_chat_template_kwarg_survives_beside_the_depth():
    """Regression 2, and the merge that makes permitting it safe.

    A Qwen target takes the depth INSIDE `chat_template_kwargs`, so this is the one key both sides
    shape. Accepting the operator's other template kwargs under a whole-key overwrite would have
    silently deleted `enable_thinking` — a worse outcome than the refusal it replaced, and one no
    request log would explain. Both members have to be in the body."""
    body = reasoning_body("qwen3-30b", "high",
                          extra={"chat_template_kwargs": {"add_generation_prompt": False}})
    assert body == {"chat_template_kwargs": {"enable_thinking": True,
                                             "add_generation_prompt": False}}


def test_the_operators_own_member_still_wins_inside_a_shared_key():
    """The merge must not turn `extra` into a weaker escape hatch than it was. Naming OUR member is
    still a clash and still refuses; that is the previous test's boundary, from the other side."""
    with pytest.raises(ConfigRefusal):
        reasoning_body("qwen3-30b", "high",
                       extra={"chat_template_kwargs": {"enable_thinking": False,
                                                       "add_generation_prompt": False}})


def test_a_mixed_extra_is_refused_on_the_depth_member_alone():
    """One `extra` carrying both a depth member and a non-depth one is a contradiction: the
    permission is per MEMBER, so the non-depth neighbour does not launder it."""
    with pytest.raises(ConfigRefusal) as got:
        reasoning_body("deepseek/x", "high", extra={"reasoning": {"exclude": True,
                                                                  "effort": "low"}})
    assert "reasoning.effort" in str(got.value)      # the member to edit, not just its container


def test_the_refusal_is_raised_once_per_client_and_never_per_request(monkeypatch):
    """WHERE this fires, driven rather than asserted about.

    It is not a per-request check: `make_llm_client` is `reasoning_body`'s only production caller,
    the result is frozen onto the client, and every later request merges that frozen dict. This
    matters because it decides whether the refusal could sensibly move to config validation — it is
    already at construction, and it stays there because the predicate needs the MODEL, which one
    `Settings` resolves several of."""
    import looplab.core.llm as llm
    from looplab.core.config import Settings

    calls: list[tuple] = []
    real = llm.reasoning_body
    monkeypatch.setattr(llm, "reasoning_body",
                        lambda *a, **kw: (calls.append(a), real(*a, **kw))[1])

    settings = Settings(llm_model="qwen3-30b", llm_base_url="http://127.0.0.1:9/v1",
                        llm_reasoning="high",
                        llm_reasoning_extra={"chat_template_kwargs": {"add_generation_prompt": False}})
    client = llm.make_llm_client(settings, stream=False)
    assert len(calls) == 1                      # once, at construction

    class _Reply:
        def model_dump(self):
            return {"choices": [{"message": {"content": "hi"}}], "usage": {}}

    seen: list[dict] = []

    def fake_create(**kwargs):
        seen.append(kwargs.get("extra_body") or {})
        return _Reply()

    monkeypatch.setattr(client._sdk.chat.completions, "create", fake_create)
    for _ in range(3):
        client.complete_text([{"role": "user", "content": "x"}])
    assert len(calls) == 1                      # still once, after three requests
    # ...and the frozen toggle carried BOTH members to every one of them.
    assert seen == [{"chat_template_kwargs": {"enable_thinking": True,
                                              "add_generation_prompt": False}}] * 3


def test_a_real_clash_refuses_at_client_construction():
    """The other half of the same boundary: the genuine contradiction never reaches a request."""
    from looplab.core.config import Settings
    from looplab.core.llm import make_llm_client

    settings = Settings(llm_model="deepseek/x", llm_base_url="http://127.0.0.1:9/v1",
                        llm_reasoning="high",
                        llm_reasoning_extra={"reasoning": {"effort": "medium"}})
    with pytest.raises(ConfigRefusal):
        make_llm_client(settings)
