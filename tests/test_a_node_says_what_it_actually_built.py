"""A node whose Developer did not build the idea it was given says so, and every reader of that node
sees it.

Measured 2026-09-25 on MiniOneRec inf12: nodes 13, 14 and 17 were recorded under "de-duplicate the
first decode step" and none contained it (a one-flag revert twice, a fused MLP once). Each got a
metric, the novelty gate rejected the idea four more times as "already tried", and the Researcher,
reading the code, kept re-proposing it.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from looplab.core.idea_report import IDEA_REPORT_NAME, idea_report_note, idea_report_text
from looplab.core.models import Idea


def _node(report=None, **kw):
    files = {IDEA_REPORT_NAME: report} if report is not None else {}
    return types.SimpleNamespace(files=files, **kw)


def test_a_report_is_only_written_for_a_declared_value():
    assert idea_report_text({"summary": "s"}) is None
    assert idea_report_text({"idea_implemented": "maybe"}) is None
    data = json.loads(idea_report_text({"idea_implemented": "different",
                                        "built_instead": "a fused gate/up MLP"}))
    assert data == {"idea_implemented": "different", "built_instead": "a fused gate/up MLP"}


def test_only_a_deviation_is_noted():
    assert idea_report_note(_node()) == ""
    assert idea_report_note(_node(idea_report_text({"idea_implemented": "as_proposed"}))) == ""
    note = idea_report_note(_node(idea_report_text({"idea_implemented": "different",
                                                    "built_instead": "reverted one flag"})))
    assert "idea different" in note and "reverted one flag" in note
    assert idea_report_note(_node("not json")) == ""


def test_the_novelty_judge_sees_the_deviation():
    from looplab.engine.novelty import _prior_outcome
    node = _node(idea_report_text({"idea_implemented": "not_implemented", "built_instead": "nothing"}),
                 status=types.SimpleNamespace(value="evaluated"), metric=1.03, error_reason=None)
    assert _prior_outcome(node).startswith("metric=1.03 [idea not_implemented")


def _dev():
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    return LLMRepoDeveloper(object(), task)


def test_both_done_specs_ask_for_it():
    dev = _dev()
    for spec in (dev._emit_spec(), dev._repair_emit_spec()):
        props = spec["function"]["parameters"]["properties"]
        assert set(props["idea_implemented"]["enum"]) == {
            "as_proposed", "partly", "different", "not_implemented"}
        assert "built_instead" in props


def test_a_real_build_ships_the_report_with_its_files(monkeypatch):
    import looplab.agents.agent as agent_mod

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": []})
        tools.execute("write_file", {"path": "solution.py", "content": "print(1)\n"})
        return finalize({"summary": "s", "idea_implemented": "partly",
                         "built_instead": "only the MLP part"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    dev = _dev()
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    assert json.loads(dev.last_files[IDEA_REPORT_NAME])["idea_implemented"] == "partly"
