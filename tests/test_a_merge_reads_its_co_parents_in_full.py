"""An ensemble merge's Developer can read every co-parent file in full, not only the prompt's excerpt.

Measured 2026-09-24 on MiniOneRec inf12 card-5: the co-parent's module was 18.9 KB and its engine
diff 53 KB against a 6,000-char excerpt per file; the plan's first step ("reconstruct the co-parent's
module") spent its whole 46-minute budget with zero writes, because the code it had to recombine was
nowhere it could read.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

from looplab.adapters.repo_developer import CoParentFileTools, co_parent_block
from looplab.core.models import Idea


def _node(node_id, files):
    return types.SimpleNamespace(id=node_id, files=files, idea=None, operator="improve",
                                 metric=1.03, stages=[], repairs=0, error=None)


BIG = "".join(f"line_{i} = {i}\n" for i in range(3000))


def test_every_line_of_a_large_co_parent_file_is_reachable_page_by_page():
    tools = CoParentFileTools([_node(0, {"opt/exp30.py": BIG})])
    seen, start = [], 1
    while True:
        page = tools.execute("read_co_parent_file",
                             {"node_id": 0, "path": "opt/exp30.py", "start_line": start})
        body, _, more = page.partition("\n… (more below — continue with start_line=")
        seen.append(body if more else page)
        if not more:
            break
        start = int(more.rstrip(")"))
    assert "".join(seen) == BIG


def test_it_lists_the_files_and_refuses_what_is_not_a_co_parent():
    tools = CoParentFileTools([_node(0, {"a.py": "x\n", "b.py": "yy\n"})])
    assert "a.py" in tools.execute("list_co_parent_files", {"node_id": 0})
    assert "not a co-parent" in tools.execute("read_co_parent_file", {"node_id": 9, "path": "a.py"})
    assert "recorded no file" in tools.execute("read_co_parent_file", {"node_id": 0, "path": "c.py"})


def test_the_excerpt_points_at_the_tool_when_it_cuts():
    block = co_parent_block([_node(0, {"opt/exp30.py": BIG})], base={})
    assert "read_co_parent_file(node_id=0, path='opt/exp30.py')" in block


def test_a_real_merge_build_offers_the_tool_and_only_during_that_build(monkeypatch):
    import looplab.agents.agent as agent_mod
    offered: list = []

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        names = {spec["function"]["name"] for spec in tools.specs()} if tools else set()
        offered.append("read_co_parent_file" in names)
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": []})
        if name == "propose_plan":
            return finalize({"steps": []})
        return finalize({"summary": "s"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(fixture),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    dev = LLMRepoDeveloper(object(), task)
    parent = _node(1, {"solution.py": "print(1)\n"})
    dev.implement_from(Idea(operator="merge", params={}, rationale="x"), parent,
                       co_parents=[_node(0, {"opt/exp30.py": BIG})])
    assert offered and all(offered), offered
    offered.clear()
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    assert offered and not any(offered), "a plain build must not carry a stale co-parent tool"
