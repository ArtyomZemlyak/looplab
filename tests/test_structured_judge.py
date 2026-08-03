"""One structured-judge invocation, shared by the two verifiers (doc 25 CT-09).

`trust/verify.py` (the D8 memo-claim verifier) and `trust/verifier.py` (the advisory criteria
scorer) do different jobs — a full merge would be wrong — but both reached the model through the
same call, written out twice three files apart under near-identical names. A change to the judge-call
CONTRACT (turn budget, fallback, parser) had to be found by grep in both.

What is pinned here is the contract, not the shape: with tools the judge reads the run and the plain
parse becomes the FALLBACK, without them it is the plain parse, and the turn budget is one constant.
Each caller keeps its own failure policy, because those are deliberately different — `verify_memo`
falls back to its deterministic verdicts, `verify` drops the sample.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.trust import judge, verifier, verify as memo_verify
from looplab.trust.judge import JUDGE_MAX_TURNS, structured_judge


class _Model:
    """Stand-in for the pydantic output model; identity is all the helper needs to forward."""


def test_without_tools_it_is_the_plain_structured_parse(monkeypatch):
    seen = {}

    def _parse(client, msgs, model, parser):
        seen.update(client=client, msgs=msgs, model=model, parser=parser)
        return "parsed"

    monkeypatch.setattr("looplab.core.parse.parse_structured", _parse)
    out = structured_judge("CLIENT", ["m"], _Model, parser="tool_call")
    assert out == "parsed"
    assert seen == {"client": "CLIENT", "msgs": ["m"], "model": _Model, "parser": "tool_call"}


def test_with_tools_it_goes_agentic_and_keeps_the_plain_parse_as_the_fallback(monkeypatch):
    captured = {}
    fell_back = {"args": ()}

    def _parse(client, msgs, model, parser):
        fell_back["args"] = (client, msgs, model, parser)
        return "fallback"

    monkeypatch.setattr("looplab.core.parse.parse_structured", _parse)

    def _agentic(client, tools, msgs, model, *, parser, loop_opts, fallback):
        captured.update(client=client, tools=tools, msgs=msgs, model=model,
                        parser=parser, loop_opts=loop_opts, fallback=fallback)
        return "agentic"

    monkeypatch.setattr("looplab.agents.agent.agentic_struct", _agentic)
    out = structured_judge("CLIENT", ["m"], _Model, parser="json", tools="TOOLS")
    assert out == "agentic"
    assert captured["tools"] == "TOOLS"
    assert captured["loop_opts"] == {"max_turns": JUDGE_MAX_TURNS}

    # The fallback must be a REAL plain parse, not a stub that returns None: a tool loop that
    # yields nothing valid has to still produce a verdict. Patched BEFORE the call, because the
    # closure binds `parse_structured` when `structured_judge` runs — patching afterwards would
    # leave the real one captured and the assertion would be testing pydantic, not the fallback.
    assert fell_back["args"] == ()
    assert captured["fallback"](["m2"]) == "fallback"
    assert fell_back["args"] == ("CLIENT", ["m2"], _Model, "json")


def test_the_turn_budget_is_one_constant_not_two_literals():
    """A budget that drifts between the two verifiers changes how much evidence each can afford to
    look at — quietly, and in different directions."""
    assert JUDGE_MAX_TURNS == 15
    for module in (memo_verify, verifier):
        source = inspect.getsource(module)
        assert '"max_turns": 15' not in source, (
            f"{module.__name__} still spells the turn budget itself")


def test_both_verifiers_reach_the_model_through_the_shared_helper():
    for module in (memo_verify, verifier):
        source = inspect.getsource(module)
        assert "structured_judge(" in source, f"{module.__name__} does not use the shared judge"
        assert "agentic_struct(" not in source, (
            f"{module.__name__} re-inlined the agentic call the helper owns")


def test_each_verifier_keeps_its_own_failure_policy():
    """The part that must NOT be shared. `verify_memo` keeps its deterministic verdicts when the
    judge fails; `verify` drops the sample and averages the rest. Folding either into the helper
    would flatten two deliberately different contracts into one."""
    assert "except Exception" not in inspect.getsource(judge.structured_judge), (
        "the helper must not own a failure policy; its callers already have different ones")

    scorer = inspect.getsource(verifier.verify)
    assert "return None" in scorer and "a bad sample is dropped" in scorer


def test_the_helper_does_not_import_agents_at_module_scope():
    """`search` imports `agents` at module level, so a module-level `agents` import in a package
    `agents` can reach closes the cycle into an ImportError at startup."""
    source = inspect.getsource(judge)
    header = source.split("def structured_judge", 1)[0]
    assert "looplab.agents" not in header


# ------------------------------------------------------------------ the misleading kinship comment

def test_the_two_evidence_text_helpers_no_longer_claim_to_mirror_each_other():
    """They share a NAME, not an implementation, and the comment saying otherwise sent readers
    looking for a duplication that is not there.

    `verify._evidence_text` takes a CLAIM dict plus a frozen source map and emits bounded REDACTED
    JSON, because a memo claim can cite external URLs that must never reach the model unredacted.
    `lesson_guard._evidence_text` takes a distilled LESSON record and renders node outcomes as prose.
    Merging them would drag a redaction contract into a path that has no URLs.
    """
    from looplab.trust import lesson_guard

    doc = lesson_guard._evidence_text.__doc__ or ""
    assert "mirrors trust/verify.py::_evidence_text" not in doc
    assert "NOT a copy" in doc and "redact" in doc.lower()

    # ...and the two really are different: different parameters, different return content.
    guard_params = list(inspect.signature(lesson_guard._evidence_text).parameters)
    memo_params = list(inspect.signature(memo_verify._evidence_text).parameters)
    assert guard_params[0] != memo_params[0], (
        "if these ever take the same first argument, revisit whether they should share code")


@pytest.mark.parametrize("name", ["verify", "verifier"])
def test_both_modules_still_exist_under_their_documented_paths(name):
    """The rename half of CT-09 is NOT done (see doc 25). Pinned so the deferral stays visible: a
    later rename must keep both spellings resolving to ONE module object, because
    `engine/research_cadence.py` documents monkeypatching `looplab.trust.verify.verify_memo`."""
    import importlib

    module = importlib.import_module(f"looplab.trust.{name}")
    assert module is not None
