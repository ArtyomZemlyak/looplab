"""Regression locks for the consolidated code-review fixes (round 5):
config lower bounds, direction normalization, merge non-numeric guard, secret-env redaction,
and LLM transport errors degrading to a safe fallback instead of crashing the run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from looplab.config import Settings
from looplab.eventstore import EventStore
from looplab.replay import fold


def test_config_lower_bounds():
    from pydantic import ValidationError
    for bad in ({"max_parallel": 0}, {"max_nodes": 0}, {"n_seeds": 0}, {"timeout": 0}):
        with pytest.raises(ValidationError):
            Settings(**bad)
    assert Settings().max_parallel == 1  # default still valid


def test_direction_normalized_in_fold(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "Maximize"})
    assert fold(s.read_all()).direction == "min"      # invalid -> safe default, never inverts
    s2 = EventStore(tmp_path / "e2.jsonl")
    s2.append("run_started", {"run_id": "r", "task_id": "t", "direction": "MAX"})
    assert fold(s2.read_all()).direction == "max"     # case-insensitive valid value accepted


def test_merge_idea_skips_non_numeric_params():
    from looplab.models import Idea, Node
    from looplab.operators import merge_idea
    a = Node(id=0, operator="draft", idea=Idea(operator="draft", params={"x": 2.0}))
    # A free-form repo param that isn't numeric must be skipped, not crash sum().
    b = Node(id=1, operator="draft", idea=Idea.model_construct(operator="draft",
                                                               params={"x": 4.0, "name": "linear"}))
    idea = merge_idea([a, b])
    assert idea.params["x"] == 3.0 and "name" not in idea.params


def test_sandbox_redacts_secret_env(tmp_path, monkeypatch):
    from looplab.sandbox import SubprocessSandbox
    monkeypatch.setenv("MY_SECRET_TOKEN", "leak-me")
    monkeypatch.setenv("LLM_API_KEY", "sk-leak")
    code = ("import os, json\n"
            "print(json.dumps({'secret': 'MY_SECRET_TOKEN' in os.environ,"
            " 'apikey': 'LLM_API_KEY' in os.environ, 'has_path': 'PATH' in os.environ}))\n"
            "print(json.dumps({'metric': 0.0}))\n")
    res = SubprocessSandbox().run(code, str(tmp_path), timeout=30.0)
    assert "sk-leak" not in res.stdout
    info = json.loads(res.stdout.splitlines()[0])
    assert info["secret"] is False and info["apikey"] is False   # secrets stripped from child env
    assert info["has_path"] is True                              # but PATH (functionality) kept


def test_llm_transport_error_is_clean_and_falls_back():
    from looplab.llm import OpenAICompatibleClient, LLMError
    from looplab.models import Idea
    from looplab.parse import ParseError, parse_structured
    client = OpenAICompatibleClient(model="x", base_url="http://127.0.0.1:9/v1", timeout=2.0)
    with pytest.raises(LLMError):                 # raw URLError no longer escapes
        client.complete_text([{"role": "user", "content": "hi"}])
    # parse_structured treats it as a parse failure -> ParseError (the role layer then falls back)
    with pytest.raises(ParseError):
        parse_structured(client, [{"role": "user", "content": "hi"}], Idea, "tool_call")
