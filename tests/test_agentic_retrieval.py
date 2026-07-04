"""ADR-16: agentic retrieval — KnowledgeTools + the tool-using Researcher agent loop.
Offline (fake chat client), no model needed."""
from __future__ import annotations

import json

from looplab.agents.agent import ToolUsingResearcher
from looplab.tools.knowledge_tools import KnowledgeTools
from looplab.core.models import RunState


def _seed_kb(d):
    (d / "degrees.md").write_text(
        "# degree selection\nThe optimal polynomial degree matches the true degree (here 2).\n",
        encoding="utf-8")
    (d / "ridge.md").write_text(
        "# ridge\nRidge lambda shrinks coefficients to reduce overfitting.\n",
        encoding="utf-8")


def test_knowledge_tools(tmp_path):
    _seed_kb(tmp_path)
    kt = KnowledgeTools(str(tmp_path))
    names = {f["function"]["name"] for f in kt.specs()}
    assert {"kb_search", "grep", "list_notes", "read_note"} <= names

    assert "degree" in kt.execute("grep", {"pattern": "optimal polynomial"})
    assert "degrees.md" in kt.execute("list_notes", {})
    assert "ridge" in kt.execute("read_note", {"name": "ridge.md"}).lower()
    # semantic search surfaces the relevant note
    assert "degree" in kt.execute("kb_search", {"query": "what polynomial degree to use"}).lower()
    # file access is restricted to the knowledge dir
    assert "no such note" in kt.execute("read_note", {"name": "../../secrets.txt"}).lower()


class _FakeChatClient:
    """Scripts assistant messages; records the messages it received each turn."""
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.turns = []

    def chat(self, messages, tools, tool_choice="auto"):
        self.turns.append(list(messages))
        return self.scripted.pop(0)


def _tool_call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_agent_loop_uses_tool_then_emits(tmp_path):
    _seed_kb(tmp_path)
    client = _FakeChatClient([
        _tool_call("kb_search", {"query": "polynomial degree"}),   # turn 1: consult KB
        _tool_call("emit", {"operator": "draft",                   # turn 2: final Idea
                            "params": {"degree": 2.0, "lam": 0.0},
                            "rationale": "kb says degree 2"}),
    ])
    r = ToolUsingResearcher(client, KnowledgeTools(str(tmp_path)),
                            bounds={"degree": (0.0, 6.0), "lam": (0.0, 100.0)})
    idea = r.propose(RunState(goal="g"), None)

    assert idea.params == {"degree": 2.0, "lam": 0.0}
    # The KB result was fed back as a tool message before the second turn.
    second_turn = client.turns[1]
    tool_msgs = [m for m in second_turn if m.get("role") == "tool"]
    assert tool_msgs and "degree" in tool_msgs[0]["content"].lower()


def test_agent_loop_clamps_out_of_bounds_emit(tmp_path):
    _seed_kb(tmp_path)
    client = _FakeChatClient([
        _tool_call("emit", {"operator": "draft", "params": {"degree": 99.0}, "rationale": "x"}),
    ])
    r = ToolUsingResearcher(client, KnowledgeTools(str(tmp_path)),
                            bounds={"degree": (0.0, 6.0), "lam": (0.0, 100.0)})
    idea = r.propose(RunState(goal="g"), None)
    assert idea.params["degree"] == 6.0           # clamped to the bound
    assert idea.params["lam"] == 50.0             # missing -> filled with midpoint
