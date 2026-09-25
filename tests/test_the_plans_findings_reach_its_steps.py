"""What the plan phase established reaches every step session, and a speculative build hands off too.

Measured 2026-09-25 on MiniOneRec inf12: step sessions re-read 73% of what their plan had just read,
three plans separately concluded the same premise was false and none of it reached a step; and 20 plan
and 58 plan_step sessions ran under a speculative `card_build` with no handoff brief at all.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

from looplab.core.models import Idea


def _dev():
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    return LLMRepoDeveloper(object(), task, plan_decompose=True, plan_min_steps=2)


def test_every_step_session_is_shown_the_plans_findings(monkeypatch):
    import looplab.agents.agent as agent_mod
    step_prompts: list = []

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": [{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"}],
                             "findings": "The step-1 dedup premise is FALSE: beams diverge at SID 1."})
        step_prompts.append(str(messages[-1].get("content", "")))
        tools.execute("write_file", {"path": "solution.py", "content": "print(1)\n"})
        return finalize({"summary": "s"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    dev = _dev()
    dev._phase_context = True
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    assert len(step_prompts) == 2
    assert all("dedup premise is FALSE" in p and "WHAT YOUR PLAN PHASE ESTABLISHED" in p
               for p in step_prompts), step_prompts


def test_a_plan_without_findings_changes_nothing(monkeypatch):
    dev = _dev()
    dev._phase_context = True
    outline = dev._plan_outline([{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"}], 1)
    assert "WHAT YOUR PLAN PHASE ESTABLISHED" not in outline and "THE WHOLE PLAN" in outline


def test_the_findings_are_bounded():
    from looplab.adapters import repo_developer
    assert repo_developer._PLAN_FINDINGS_CHARS == 2000


def test_a_speculative_build_opens_the_handoff_scope_like_a_serial_one():
    from looplab.engine.speculation import SpeculationMixin
    src = inspect.getsource(SpeculationMixin._build_requested_card)
    assert "handoff_scope(enabled=" in src
