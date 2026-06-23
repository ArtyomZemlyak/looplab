"""I2: structured-output parsing + auto-fallback, cost accounting, LLM role seam."""
from __future__ import annotations

import pytest

from autornd.llm import BudgetExceeded, CostAccountant
from autornd.models import Idea
from autornd.parse import ParseError, parse_structured
from autornd.roles import LLMResearcher


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
    from autornd.models import RunState
    idea = r.propose(RunState(goal="g"), None)
    assert isinstance(idea, Idea) and idea.params["x"] == 3.0


def test_extract_json_ignores_trailing_braces():
    from autornd.parse import _extract_json
    obj = _extract_json('Sure: {"operator": "draft", "params": {"x": 1.0}} note: see {y}')
    assert obj["operator"] == "draft" and obj["params"] == {"x": 1.0}


def test_extract_code_prefers_python_fence():
    from autornd.parse import extract_code
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
