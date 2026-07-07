"""ADR-16: agentic retrieval — KnowledgeTools + the tool-using Researcher agent loop.
Offline (fake chat client), no model needed."""
from __future__ import annotations

import json

from looplab.agents.agent import ToolUsingResearcher
from looplab.tools.knowledge_tools import KnowledgeTools
from looplab.core.models import Idea, RunState


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


def test_agent_loop_rejects_empty_emit_and_reprompts(tmp_path):
    _seed_kb(tmp_path)
    client = _FakeChatClient([
        _tool_call("emit", {"operator": "draft", "params": {}, "rationale": ""}),   # empty no-op -> bounced
        _tool_call("emit", {"operator": "draft", "params": {"degree": 3.0},          # re-emit: real idea
                            "rationale": "try degree 3"}),
    ])
    r = ToolUsingResearcher(client, KnowledgeTools(str(tmp_path)),
                            bounds={"degree": (0.0, 6.0), "lam": (0.0, 100.0)})
    idea = r.propose(RunState(goal="g"), None)
    assert idea.params.get("degree") == 3.0 and (idea.rationale or "").strip()   # the SECOND, valid emit won
    # The empty emit was answered with a rejection tool-message before the re-emit turn.
    second_turn = client.turns[1]
    rej = [m for m in second_turn if m.get("role") == "tool" and "not accepted" in m.get("content", "").lower()]
    assert rej, "empty emit should have been bounced back with a rejection message"


# --- tool-using Researcher loop resilience (review rounds, mega-review, deep audit) ---------------

# live-surfaced bug — the agentic Researcher must survive a junk emit (non-numeric params)
def test_tool_researcher_finalize_drops_nonnumeric_params():
    from looplab.agents.agent import ToolUsingResearcher
    r = ToolUsingResearcher(client=None, tools=None, bounds=None)
    # the live model returned this and crashed the run before the fix
    idea = r._finalize({"operator": "modify_metric", "params": {"new_metric": "linear"},
                        "rationale": "switch reward landscape"})
    assert idea.params == {} and idea.rationale == "switch reward landscape"
    # numeric params survive; bounds still fill/clamp
    r2 = ToolUsingResearcher(client=None, tools=None, bounds={"x": (0.0, 10.0)})
    idea2 = r2._finalize({"operator": "improve", "params": {"x": "99", "junk": "nope"}})
    assert idea2.params == {"x": 10.0}                       # "99" clamped to 10, junk dropped


def test_tool_researcher_propose_survives_transport_failure():
    """A transport failure (LLMError after retries) on the agentic path degrades to a safe Idea,
    it does NOT crash the run."""
    from looplab.agents.agent import ToolUsingResearcher
    from looplab.core.llm import LLMError
    from looplab.core.models import Idea, RunState

    class _DeadClient:
        def chat(self, *a, **k):
            raise LLMError("endpoint down")

        def complete_tool(self, *a, **k):
            raise LLMError("endpoint down")

        def complete_text(self, *a, **k):
            raise LLMError("endpoint down")

    class _Tools:
        def specs(self):
            return []

        def execute(self, name, args):
            return "(none)"

    r = ToolUsingResearcher(_DeadClient(), _Tools())
    idea = r.propose(RunState(run_id="r"), None)
    assert isinstance(idea, Idea)                              # degraded, not crashed


def test_force_emit_coerces_non_object_to_dict():
    """A forced emit returning a valid-but-non-object JSON ("[…]") must become {} so finalize's .get
    can't AttributeError."""
    from looplab.agents.agent import _force_emit

    class _Client:
        def complete_tool(self, messages, schema):
            return ["not", "a", "dict"]

    out = _force_emit(_Client(), [], {"function": {"parameters": {}}})
    assert out == {}


def test_force_emit_preserves_none_for_couldnt_force():
    """A client that RETURNS None (couldn't force a tool call) must stay None so the loop nudges +
    retries, not finalize on empty args."""
    from looplab.agents.agent import _force_emit

    class _Client:
        def complete_tool(self, messages, schema):
            return None

    assert _force_emit(_Client(), [], {"function": {"parameters": {}}}) is None


# B1 — the agentic Researcher survives a malformed-JSON tool call (does not crash the run)
def test_tool_loop_survives_malformed_json_args():
    from looplab.agents.agent import ToolUsingResearcher

    class _Tools:
        def specs(self): return []
        def execute(self, n, a): return ""

    class _Client:
        def chat(self, messages, tool_specs, tool_choice="auto"):
            return {"tool_calls": [{"id": "1", "function": {"name": "emit",
                                                            "arguments": '{"params": {'}}]}  # malformed

    r = ToolUsingResearcher(client=_Client(), tools=_Tools(), bounds=None)
    idea = r.propose(RunState(goal="g", direction="max"), None)
    assert isinstance(idea, Idea) and idea.operator                # fell back, no crash
