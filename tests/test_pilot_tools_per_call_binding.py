"""Concurrent judges on ONE facade each read the run state THEY were handed (review 2026-09-22, TAT-08).

`UnifiedAgent._pilot_emit` bound the run state onto the SHARED `_pilot_tools` object
(`self._pilot_tools.bind_state(state, None)`) and handed that same object to the tool loop. That was
harmless while every call ran on the engine's loop thread one at a time, and stopped being harmless
when the crash-triage judge moved OFF the loop thread (doc 52 row 12: `engine/evaluate.py` runs
`self._triage_crash` through `_offload_under_proposal_sink`, one worker per failing node). With
`eval_parallel > 1` two nodes' triages run at once on the one facade, each binding its own ADMISSION
fold (`EvalAttempt.state = fold(events_at_start)`); the second bind lands on the object the first judge
is still reading through. A node admitted before the other was even created binds a state that does
not contain it — so the first judge's `read_experiment` on its OWN node answered "(no experiment #N)".

Confirmed by driving it (two threads, one facade, a barrier so both have bound before either reads —
red before the fix), then fixed by binding a PER-CALL VIEW (`tool_loop.bound_toolset`) instead of the
shared object.
"""
from __future__ import annotations

import threading

from looplab.agents.tool_loop import CompositeTools
from looplab.core.models import Idea, Node, RunState
from looplab.tools.run_tools import RunTools


def _state(node_id: int, marker: str) -> RunState:
    st = RunState(goal="g", direction="min")
    st.nodes[node_id] = Node(id=node_id, operator="improve",
                             idea=Idea(operator="improve", params={}, rationale=marker))
    return st


def test_two_concurrent_triages_each_read_their_own_state(monkeypatch):
    from looplab.agents import agent as agent_mod
    from looplab.agents.unified_agent import UnifiedAgent

    # Node 1 exists only in A's admission fold and node 2 only in B's — the shape two evals admitted
    # at different moments carry.
    states = {"A": (_state(1, "ALPHA-RATIONALE"), 1), "B": (_state(2, "BRAVO-RATIONALE"), 2)}
    both_bound = threading.Barrier(2, timeout=10)
    read: dict[str, str] = {}

    def fake_loop(client, tools, messages, emit_spec, **kw):
        who = threading.current_thread().name
        both_bound.wait()                   # both judges have bound before either one reads
        read[who] = tools.execute("read_experiment", {"node_id": states[who][1]})
        return kw["fallback"](messages)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    agent = UnifiedAgent(researcher=object(), developer=object(), pilot_client=object(),
                         pilot_tools=CompositeTools([RunTools()]))

    def triage(who):
        state, nid = states[who]
        agent.triage_crash(type("N", (), {"id": nid, "code": "print(1)"})(), "boom", 1,
                           state=state)

    threads = [threading.Thread(target=triage, args=(who,), name=who) for who in states]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert "ALPHA-RATIONALE" in read["A"], read["A"]
    assert "BRAVO-RATIONALE" in read["B"], read["B"]


def test_the_pilot_and_a_triage_do_not_share_a_binding_either(monkeypatch):
    """The pilot runs on the loop thread while a triage runs in a worker — the same object, the same
    race, from the other caller."""
    from looplab.agents import agent as agent_mod
    from looplab.agents.unified_agent import UnifiedAgent

    both_bound = threading.Barrier(2, timeout=10)
    read: dict[str, str] = {}

    def fake_loop(client, tools, messages, emit_spec, **kw):
        who = threading.current_thread().name
        both_bound.wait()
        read[who] = tools.execute("read_experiment", {"node_id": 1 if who == "pilot" else 2})
        return kw["fallback"](messages)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    agent = UnifiedAgent(researcher=object(), developer=object(), pilot_client=object(),
                         pilot_tools=CompositeTools([RunTools()]))
    pilot = threading.Thread(name="pilot", target=lambda: agent.choose_action(
        _state(1, "PILOT-SEES-ONE"), [{"kind": "draft"}]))
    triage = threading.Thread(name="triage", target=lambda: agent.triage_crash(
        type("N", (), {"id": 2, "code": ""})(), "boom", 1, state=_state(2, "TRIAGE-SEES-TWO")))
    for t in (pilot, triage):
        t.start()
    for t in (pilot, triage):
        t.join(timeout=20)
    assert "PILOT-SEES-ONE" in read["pilot"] and "TRIAGE-SEES-TWO" in read["triage"], read


def test_the_per_call_log_tools_ride_beside_the_bound_view(monkeypatch):
    """The triage judge usually arrives WITH a per-call provider — its log tools, `repair_log_tools`
    being on by default — merged over the standing toolset. What is merged must be the BOUND view:
    the shared object is never bound any more, so merging it would hand the judge standing tools
    that answer for no run at all."""
    from looplab.agents import agent as agent_mod
    from looplab.agents.unified_agent import UnifiedAgent

    class _Logs:
        def specs(self):
            return [{"type": "function", "function": {
                "name": "read_log", "description": "d",
                "parameters": {"type": "object", "properties": {}}}}]

        def execute(self, name, args):
            return "THIS-ATTEMPT-LOG"

    read: dict[str, str] = {}

    def fake_loop(client, tools, messages, emit_spec, **kw):
        read["experiment"] = tools.execute("read_experiment", {"node_id": 1})
        read["log"] = tools.execute("read_log", {})
        return kw["fallback"](messages)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    agent = UnifiedAgent(researcher=object(), developer=object(), pilot_client=object(),
                         pilot_tools=CompositeTools([RunTools()]))
    agent.triage_crash(type("N", (), {"id": 1, "code": ""})(), "boom", 1,
                       state=_state(1, "WITH-LOG-TOOLS"), tools=_Logs())
    assert "WITH-LOG-TOOLS" in read["experiment"], read
    assert read["log"] == "THIS-ATTEMPT-LOG"


def test_the_view_is_bound_and_the_shared_toolset_is_not():
    """The rule, as a truth table: binding a view never rebinds the original (nor any provider inside
    it), and a view offers and routes every tool the original does, in the same order."""
    from looplab.agents.tool_loop import bound_toolset

    shared = CompositeTools([RunTools()])
    view_a = bound_toolset(shared, _state(1, "A-SIDE"))
    view_b = bound_toolset(shared, _state(2, "B-SIDE"))
    assert shared.providers[0].state is None, "the shared provider was rebound"
    assert set(view_a.providers[0].state.nodes) == {1}
    assert set(view_b.providers[0].state.nodes) == {2}
    assert view_a.specs() == shared.specs() == view_b.specs()
    assert "A-SIDE" in view_a.execute("read_experiment", {"node_id": 1})
    assert "(no experiment #1)" in view_b.execute("read_experiment", {"node_id": 1})


def test_a_nested_composite_is_viewed_all_the_way_down():
    from looplab.agents.tool_loop import bound_toolset

    inner = CompositeTools([RunTools()])
    outer = CompositeTools([inner])
    view = bound_toolset(outer, _state(1, "NESTED"))
    assert inner.providers[0].state is None
    assert "NESTED" in view.execute("read_experiment", {"node_id": 1})


def test_a_bare_provider_gets_a_view_too():
    from looplab.agents.tool_loop import bound_toolset

    bare = RunTools()
    view = bound_toolset(bare, _state(1, "BARE"))
    assert bare.state is None and view is not bare
    assert "BARE" in view.execute("read_experiment", {"node_id": 1})
    assert bound_toolset(None, _state(1, "x")) is None
