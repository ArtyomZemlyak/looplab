"""Parity tests for the DeepResearcher memo loop after it was folded onto the shared
`agent.drive_tool_loop` (instead of a hand-rolled clone). Offline (fake chat client), no model.

These pin the behaviors the old clone owned so the fold stays byte-identical: the historical
nudge/stuck wording, the consulted-sources ledger (title/url/snippet truncation), the 4000-char
tool-result cap, the malformed-args guard, the "(no tools)" observation, and the forced-memo
fallback when the turn budget runs out.
"""
from __future__ import annotations

import json

from looplab.deep_research import _SYSTEM, DeepResearcher, state_brief
from looplab.models import RunState


class _FakeTools:
    """One search tool; records executed calls so tests can assert the parsed args."""

    def __init__(self, result: str = "ridge regression survey"):
        self.result = result
        self.calls: list[tuple] = []

    def specs(self):
        return [{"type": "function", "function": {
            "name": "search", "description": "search",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string"}}}}}]

    def execute(self, name, args):
        self.calls.append((name, args))
        return self.result


class _FakeChatClient:
    """Scripts assistant messages; records the messages and tool specs it received each turn.
    Deliberately has NO `complete_tool`, so `_force_emit` fails over to the nudge path — the
    same shape the offline fakes in test_agentic_retrieval.py exercise."""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.turns: list[list[dict]] = []
        self.specs_seen: list[list[str]] = []

    def chat(self, messages, tools, tool_choice="auto"):
        self.turns.append([dict(m) for m in messages])
        self.specs_seen.append([t["function"]["name"] for t in tools])
        return self.scripted.pop(0)


class _ForcingClient(_FakeChatClient):
    """Adds `complete_tool`, so the forced-emit paths (in-loop stuck stop, and the `_forced`
    fallback via parse_structured's tool_call parser) return a scripted memo dict."""

    def __init__(self, scripted, forced):
        super().__init__(scripted)
        self.forced = forced
        self.forced_calls: list[list[dict]] = []

    def complete_tool(self, messages, json_schema):
        self.forced_calls.append([dict(m) for m in messages])
        return self.forced


def _tool_call(name, args, call_id="c1"):
    return {"content": "", "tool_calls": [
        {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_tool_then_stall_then_nudge_then_emit_message_parity():
    """tool call -> result -> prose stall -> nudge -> final emit: memo fields, sources ledger,
    and the EXACT message sequence sent to the client all match the pre-fold loop."""
    tools = _FakeTools(result="R" * 5000)               # long result: capped at 4000 in the trace
    emit_args = {"summary": "s", "reasoning": "r", "findings": ["f1"],
                 "claims": [{"statement": "c", "node_ids": [1], "urls": ["http://x"]}],
                 "recommended_directions": ["d1"]}
    client = _FakeChatClient([
        _tool_call("search", {"query": "ridge", "url": "http://a"}),
        {"content": "let me think in prose"},           # no complete_tool -> force fails -> nudge
        _tool_call("emit", emit_args, call_id="c2"),
    ])
    state = RunState(goal="g")
    memo = DeepResearcher(client, tools).research(state, trigger="cadence")

    assert memo.summary == "s" and memo.reasoning == "r"
    assert memo.findings == ["f1"] and memo.recommended_directions == ["d1"]
    assert memo.claims == [{"statement": "c", "node_ids": [1], "urls": ["http://x"]}]
    assert memo.trigger == "cadence" and memo.at_node == 0
    # sources ledger: same (title, url, snippet) shape with the same [:200] snippet truncation
    assert memo.sources == [{"title": "search(ridge)", "url": "http://a",
                             "snippet": "R" * 200}]
    assert tools.calls == [("search", {"query": "ridge", "url": "http://a"})]
    # only the wired tools + emit are offered — no self-plan tool injected into this stage
    assert client.specs_seen[0] == ["search", "emit"]

    # Turn 1: the memo prompts, byte-identical.
    assert client.turns[0] == [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": state_brief(state) +
            "\nReview the run. Consult sources if useful, then emit your memo."}]
    # Final turn: system, user, assistant(tool_calls), tool result, prose, historical nudge.
    final = client.turns[2]
    assert [m["role"] for m in final] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert final[3] == {"role": "tool", "tool_call_id": "c1", "name": "search",
                        "content": "R" * 4000}          # 4000-char tool-result cap preserved
    assert final[4] == {"role": "assistant", "content": "let me think in prose"}
    assert final[5] == {"role": "user", "content": "Now call `emit` with your memo."}


def test_malformed_tool_args_degrade_to_empty_dict():
    """A junk model emitting unparseable JSON arguments never crashes the memo: the tool runs
    with {} and the source is still recorded (empty label/url), exactly as before."""
    tools = _FakeTools(result="ok")
    client = _FakeChatClient([
        {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "search", "arguments": "{not json"}}]},
        _tool_call("emit", {"summary": "done"}, call_id="c2"),
    ])
    memo = DeepResearcher(client, tools).research(RunState(goal="g"))
    assert tools.calls == [("search", {})]
    assert memo.sources == [{"title": "search()", "url": "", "snippet": "ok"}]
    assert memo.summary == "done"


def test_turn_budget_exhaustion_forces_memo_fallback():
    """Out of turns without an emit -> the `_forced` fallback builds the memo from the accumulated
    history (parse_structured over messages + the historical 'Emit the memo now.' instruction)."""
    tools = _FakeTools(result="partial evidence")
    client = _ForcingClient(
        [_tool_call("search", {"query": "q"})],         # one tool turn, then the budget is gone
        forced={"summary": "forced memo", "findings": ["from history"]})
    memo = DeepResearcher(client, tools, max_turns=1).research(RunState(goal="g"))
    assert memo.summary == "forced memo" and memo.findings == ["from history"]
    assert memo.sources == [{"title": "search(q)", "url": "", "snippet": "partial evidence"}]
    forced_msgs = client.forced_calls[-1]
    assert forced_msgs[-1] == {"role": "user", "content": "Emit the memo now."}
    # the trace it synthesized from still holds the executed tool round
    assert any(m.get("role") == "tool" and m.get("content") == "partial evidence"
               for m in forced_msgs)


def test_stuck_detector_stops_with_historical_wording():
    """B1: four identical call+result rounds trip the stuck detector; the stop message keeps the
    stage's historical wording, and the forced emit is taken from complete_tool."""
    tools = _FakeTools(result="same")
    call = _tool_call("search", {"query": "loop"})
    client = _ForcingClient([call, call, call, call], forced={"summary": "unstuck memo"})
    memo = DeepResearcher(client, tools).research(RunState(goal="g"))
    assert memo.summary == "unstuck memo"
    assert len(memo.sources) == 4                       # every consulted round is still recorded
    reason = 'repeated the same call+result search({"query": "loop"}) 4 times with no progress'
    assert client.forced_calls[-1][-1] == {
        "role": "user",
        "content": f"Stop: you appear to be stuck ({reason}). Call `emit` with your memo now."}


def test_no_tools_hallucinated_call_gets_no_tools_observation():
    """tools=None: only `emit` is offered, and a hallucinated tool call observes the stage's
    historical "(no tools)" string (not the shared loop's "(unknown tool: …)" wording)."""
    client = _FakeChatClient([
        _tool_call("search", {"query": "q"}),
        _tool_call("emit", {"summary": "s"}, call_id="c2"),
    ])
    memo = DeepResearcher(client, tools=None).research(RunState(goal="g"))
    assert memo.summary == "s"
    assert memo.sources == [{"title": "search(q)", "url": "", "snippet": "(no tools)"}]
    assert client.specs_seen[0] == ["emit"]
    tool_msgs = [m for m in client.turns[1] if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "(no tools)"


def test_two_prose_stalls_fall_back_to_forced_memo():
    """Two consecutive un-forceable prose turns end the loop (with exactly one nudge in between);
    with no forcing client at all, the fallback degrades to the historical empty-memo summary."""
    client = _FakeChatClient([
        {"content": "prose one"},
        {"content": "prose two"},
    ])
    memo = DeepResearcher(client, tools=_FakeTools()).research(RunState(goal="g"))
    assert memo.summary == "(deep research produced no memo)"
    assert memo.sources == []
    nudges = [m for m in client.turns[1] if m["role"] == "user"
              and m["content"] == "Now call `emit` with your memo."]
    assert len(nudges) == 1
