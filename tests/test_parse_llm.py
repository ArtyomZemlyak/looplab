"""I2: structured-output parsing + auto-fallback, cost accounting, LLM role seam."""
from __future__ import annotations

import pytest

from looplab.core.llm import BudgetExceeded, CostAccountant
from looplab.core.models import Idea
from looplab.core.parse import ParseError, _coerce_value, parse_structured
from looplab.agents.roles import LLMResearcher


class FakeClient:
    """Implements the parse.LLMClient Protocol for offline tests (no live calls)."""

    def __init__(self, tool=None, text=None):
        self.tool = list(tool or [])
        self.text = list(text or [])

    def complete_tool(self, messages, json_schema):
        if not self.tool:
            raise RuntimeError("no tool response queued")
        r = self.tool.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def complete_text(self, messages):
        return self.text.pop(0)


def test_tool_call_path():
    c = FakeClient(tool=[{"operator": "draft", "params": {"x": 1.0}, "rationale": "r"}])
    idea = parse_structured(c, [{"role": "user", "content": "go"}], Idea, "tool_call")
    assert idea.operator == "draft" and idea.params == {"x": 1.0}


def test_auto_fallback_tool_to_baml():
    # tool_call returns an invalid object -> falls back to the text/JSON (baml) path.
    c = FakeClient(
        tool=[{}],  # missing required 'operator' -> ValidationError
        text=['Sure! Here you go: {"operator": "improve", "params": {"y": 2.0}}'],
    )
    idea = parse_structured(c, [{"role": "user", "content": "go"}], Idea, "tool_call")
    assert idea.operator == "improve" and idea.params == {"y": 2.0}


def test_all_parsers_fail_raises():
    c = FakeClient(tool=[{}], text=["no json here at all"])
    with pytest.raises(ParseError):
        parse_structured(c, [{"role": "user", "content": "go"}], Idea, "tool_call")


def test_llm_researcher_returns_idea():
    c = FakeClient(tool=[{"operator": "improve", "params": {"x": 3.0, "y": -1.0}, "rationale": "r"}])
    r = LLMResearcher(c)
    from looplab.core.models import RunState
    idea = r.propose(RunState(goal="g"), None)
    assert isinstance(idea, Idea) and idea.params["x"] == 3.0


def test_extract_json_ignores_trailing_braces():
    from looplab.core.parse import _extract_json
    obj = _extract_json('Sure: {"operator": "draft", "params": {"x": 1.0}} note: see {y}')
    assert obj["operator"] == "draft" and obj["params"] == {"x": 1.0}


def test_h2_coerces_string_numbers_in_tool_call():
    # H2 schema-aligned repair: a weak model returns numbers-as-strings -> coerced, not crashed.
    c = FakeClient(tool=[{"operator": "draft", "params": {"x": "3", "y": "1.5"}, "rationale": "r"}])
    idea = parse_structured(c, [{"role": "user", "content": "go"}], Idea, "tool_call")
    assert idea.params == {"x": 3.0, "y": 1.5}


def test_h2_lenient_json_single_quotes_and_trailing_comma():
    from looplab.core.parse import _extract_json
    obj = _extract_json("Here: {'operator': 'improve', 'params': {'x': 2.0,}}")
    assert obj["operator"] == "improve" and obj["params"]["x"] == 2.0


def test_h2_coerce_case_insensitive_keys():
    from looplab.core.parse import _coerce_to_model
    out = _coerce_to_model({"Operator": "draft", "Rationale": "hi"}, Idea)
    assert out["operator"] == "draft" and out["rationale"] == "hi"


def test_extract_code_prefers_python_fence():
    from looplab.core.parse import extract_code
    text = "Example output:\n```\nnot code\n```\nSolution:\n```python\nprint(1)\n```"
    assert extract_code(text) == "print(1)"


def test_cost_accountant_warn_and_stop():
    acc = CostAccountant(limit=1.0, warn_frac=0.8)
    acc.add(0.5)
    assert not acc.warned
    acc.add(0.4)  # 0.9 -> warn
    assert acc.warned
    assert round(acc.remaining(), 4) == 0.1
    with pytest.raises(BudgetExceeded):
        acc.add(0.2)  # 1.1 -> stop


# --- parse: boolean/int coercion safety ----------------------------------------------------------

def test_coerce_bool_recognizes_on_and_rejects_garbage():
    assert _coerce_value("on", bool) is True
    assert _coerce_value("off", bool) is False
    # An unrecognized string is returned as-is (so model validation rejects it) — not silently False.
    assert _coerce_value("maybe", bool) == "maybe"


def test_coerce_int_rounds_not_truncates_and_rejects_bool():
    assert _coerce_value(3.9, int) == 4       # round, not truncate to 3
    assert _coerce_value("3.9", int) == 4
    assert _coerce_value(True, int) is True   # a JSON bool is not silently flipped to 1


def test_coerce_infinite_float_to_int_does_not_raise():
    # round(inf) raises OverflowError; the coercer must return the raw value, not crash.
    assert _coerce_value("1e400", int) == "1e400"


def test_to_int_returns_none_on_non_finite_not_overflow():
    # to_int does int(float(v)); float() accepts 'inf'/'1e400'/'Infinity' but int() then raises
    # OverflowError. The documented contract is "None when unparseable", not a crash.
    from looplab.core.parse import to_int
    assert to_int("inf") is None and to_int("-inf") is None
    assert to_int("1e400") is None and to_int("Infinity") is None
    assert to_int("nan") is None                       # int(float('nan')) -> ValueError -> None
    assert to_int("3.7") == 3 and to_int("x") is None and to_int(None) is None


def test_parse_structured_infinite_int_raises_parse_error_not_overflow():
    from pydantic import BaseModel

    class M(BaseModel):
        choice: int

    class _Fake:
        def complete_tool(self, messages, schema):
            return {"choice": "1e400"}

        def complete_text(self, messages):
            return '{"choice": "1e400"}'

    with pytest.raises(ParseError):
        parse_structured(_Fake(), [{"role": "user", "content": "x"}], M, "tool_call")


def test_extract_code_salvages_unclosed_fence():
    # a Developer reply truncated at max tokens leaves an UNCLOSED fence; we must return the code, not
    # the literal ```python header (which is a guaranteed SyntaxError node).
    from looplab.core.parse import extract_code
    out = extract_code("Here is the solution:\n```python\nimport os\ndef train():\n    pass")
    assert out.startswith("import os") and "```" not in out
    assert extract_code("```\nx = 2").strip() == "x = 2"
    # a properly closed fence is unaffected
    assert extract_code("```python\ny = 3\n```").strip() == "y = 3"


def test_coerce_unwraps_pep604_optional():
    """Architecture review: _coerce_value must unwrap the PEP 604 `X | None` union (get_origin returns
    types.UnionType, not typing.Union) so schema-aligned coercion isn't silently skipped for those
    fields — the codebase uses `| None` pervasively."""
    from typing import Optional
    assert _coerce_value(3.9, Optional[int]) == 4          # classic Optional (already worked)
    assert _coerce_value(3.9, int | None) == 4             # PEP 604 spelling (was skipped before the fix)
    assert _coerce_value(None, int | None) is None
    assert _coerce_value("true", bool | None) is True
