"""Unified self-driving agent (merge of Researcher + Developer + Strategist).

Offline — synthetic RunState, the toy backend, and fake chat clients. Covers:
  * the pure legal-action gate (forced phases + envelope never names an illegal parent),
  * the pilot's action choice (always in `legal`; malformed/abstaining -> policy fallback),
  * make_roles wiring (one object as both roles; the Strategist Protocol),
  * `agent_decision` folds audit-only (additive; never drives selection),
  * an end-to-end toy run that self-drives the pipeline, with OUTCOME parity vs the pure
    policy path and a clean replay of the recorded log.
"""
from __future__ import annotations

import json

import anyio

from looplab.core.config import Settings
from looplab.events.eventstore import EventStore
from looplab.core.models import Event, Idea, Node, NodeStatus, RunState
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree, legal_actions
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.tasks import make_roles
from looplab.adapters.toytask import ToyTask
from looplab.agents.unified_agent import UnifiedAgent


def _st(pending: bool = False) -> RunState:
    st = RunState(goal="minimize loss", direction="min")
    st.nodes = {
        0: Node(id=0, operator="draft",
                idea=Idea(operator="draft", params={"x": 0.0, "y": 0.0}),
                metric=10.0, status=NodeStatus.evaluated),
        1: Node(id=1, parent_ids=[0], operator="improve",
                idea=Idea(operator="improve", params={"x": 2.0, "y": 1.0}),
                metric=4.0, status=NodeStatus.evaluated),
        2: Node(id=2, operator="draft",
                idea=Idea(operator="draft", params={"x": 9.0, "y": 9.0}),
                status=NodeStatus.failed, error_reason="crash", error="boom"),
    }
    st.best_node_id = 1
    if pending:
        st.nodes[3] = Node(id=3, parent_ids=[1], operator="improve",
                           idea=Idea(operator="improve", params={"x": 3.0, "y": -1.0}),
                           status=NodeStatus.pending)
    return st


# --------------------------------------------------------------------------- legal_actions
def test_legal_actions_forced_evaluate_on_pending():
    st = _st(pending=True)
    pol = GreedyTree(n_seeds=3, max_nodes=8)
    acts = legal_actions(st, pol, max_nodes=8)
    assert acts == [{"kind": "evaluate", "node_id": 3}]   # forced: no discretion while pending


def test_legal_actions_empty_at_budget():
    st = _st()
    assert legal_actions(st, GreedyTree(n_seeds=3, max_nodes=3), max_nodes=3) == []


def test_legal_actions_seed_only_draft():
    st = RunState(goal="g", direction="min")          # zero nodes < n_seeds
    acts = legal_actions(st, GreedyTree(n_seeds=3, max_nodes=8), max_nodes=8)
    assert acts == [{"kind": "draft"}]


def test_legal_actions_envelope_has_no_illegal_parent():
    st = _st()
    acts = legal_actions(st, GreedyTree(n_seeds=2, max_nodes=8), max_nodes=8)
    kinds = {a["kind"] for a in acts}
    assert "draft" in kinds and "improve" in kinds       # explore/exploit envelope
    feasible_ids = {n.id for n in st.feasible_nodes()}
    for a in acts:
        if a["kind"] == "improve":
            assert a["parent_id"] in feasible_ids        # never an infeasible/missing parent
        if a["kind"] == "debug":
            assert st.nodes[a["parent_id"]].status is NodeStatus.failed
        if a["kind"] == "merge":
            assert all(p in feasible_ids for p in a["parent_ids"])


# --------------------------------------------------------------------------- pilot choice
class _FakeChatClient:
    def __init__(self, scripted):
        self.scripted = list(scripted)

    def chat(self, messages, tools, tool_choice="auto"):
        return self.scripted.pop(0)


def _emit(name, args):
    return {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _agent(pilot_client=None):
    r, d = ToyTask().build_roles()
    return UnifiedAgent(researcher=r, developer=d, strategist=None, pilot_client=pilot_client)


def test_choose_action_picks_within_legal():
    st = _st()
    legal = legal_actions(st, GreedyTree(n_seeds=2, max_nodes=8), max_nodes=8)
    client = _FakeChatClient([_emit("choose_action", {"index": 1, "rationale": "go"})])
    choice = _agent(client).choose_action(st, legal, recommended=legal[0])
    assert choice["index"] == 1 and 0 <= choice["index"] < len(legal)


def test_choose_action_out_of_range_falls_back_to_recommended():
    st = _st()
    legal = legal_actions(st, GreedyTree(n_seeds=2, max_nodes=8), max_nodes=8)
    recommended = {"kind": "improve", "parent_id": 1}
    client = _FakeChatClient([_emit("choose_action", {"index": 999})])   # illegal index
    choice = _agent(client).choose_action(st, legal, recommended=recommended)
    assert 0 <= choice["index"] < len(legal)
    assert legal[choice["index"]] == recommended         # never escapes the legal set


def test_choose_action_no_pilot_client_uses_recommendation():
    st = _st()
    legal = legal_actions(st, GreedyTree(n_seeds=2, max_nodes=8), max_nodes=8)
    recommended = {"kind": "improve", "parent_id": 1}
    choice = _agent(pilot_client=None).choose_action(st, legal, recommended=recommended)
    assert legal[choice["index"]] == recommended


def test_choose_action_matches_a_merge_recommendation_regardless_of_parent_order():
    # Regression: a merge recommendation was matched to the legal menu by ORDERED parent tuple, but a
    # merge is symmetric in its parents. A non-greedy policy whose recommended pair differs in order
    # from the menu's top-2 order then failed to match, and the no-pilot fallback silently defaulted to
    # legal[0] (draft) instead of the recommended merge.
    st = _st()
    legal = [{"kind": "draft"}, {"kind": "merge", "parent_ids": [1, 2]}]
    recommended = {"kind": "merge", "parent_ids": [2, 1]}          # reverse of the menu's parent order
    choice = _agent(pilot_client=None).choose_action(st, legal, recommended=recommended)
    assert legal[choice["index"]]["kind"] == "merge"              # matched the merge, not defaulted to draft
    assert legal[choice["index"]]["parent_ids"] == [1, 2]


# --------------------------------------------------------------------------- wiring
def test_make_roles_unified_returns_one_object():
    r, d = make_roles(ToyTask(), Settings(backend="llm", unified_agent=True))
    assert r is d and isinstance(r, UnifiedAgent)
    # plays every role: Researcher.propose, Developer.implement/repair, Strategist.decide, pilot
    for m in ("propose", "implement", "repair", "decide", "choose_action"):
        assert callable(getattr(r, m))


def test_make_roles_split_is_unchanged_when_flag_off():
    r, d = make_roles(ToyTask(), Settings(backend="llm", unified_agent=False))
    assert r is not d and not isinstance(r, UnifiedAgent)


def test_unified_absorbs_strategist():
    # strategist_backend="rule" => the agent IS the strategist; decide() delegates to the rule
    # baseline (no model needed). "off" (default) => decide() returns None (split-mode parity).
    from looplab.agents.strategist import StrategyContext
    r, _ = make_roles(ToyTask(), Settings(backend="llm", unified_agent=True,
                                          strategist_backend="rule"))
    ctx = StrategyContext(node_count=0, phase="seed", available_policies=["greedy"])
    assert r.decide(_st(), ctx) is not None              # rule baseline fires in seed phase

    off, _ = make_roles(ToyTask(), Settings(backend="llm", unified_agent=True,
                                            strategist_backend="off"))
    assert off.decide(_st(), ctx) is None                # off => no strategy (parity)


def test_per_stage_model_overrides_role_model():
    # agent_stage_models["propose"] must beat the H3 researcher_model fallback for the propose
    # stage (same client_for/_set_role_client mechanism used for every stage).
    over = make_roles(ToyTask(), Settings(
        backend="llm", unified_agent=True, researcher_model="r-model",
        agent_stage_models={"propose": "stage-prop"}))[0]
    assert over.researcher.client.model == "stage-prop"   # explicit stage map wins
    base = make_roles(ToyTask(), Settings(
        backend="llm", unified_agent=True, researcher_model="r-model"))[0]
    assert base.researcher.client.model == "r-model"      # falls back to researcher_model (H3)


# --------------------------------------------------------------------------- replay (audit-only)
def test_agent_decision_folds_audit_only():
    events = [
        Event(type="run_started", data={"run_id": "r", "task_id": "t", "direction": "min"}),
        Event(type="node_created", data={
            "node_id": 0, "parent_ids": [], "operator": "draft",
            "idea": Idea(operator="draft", params={"x": 1.0}).model_dump(mode="json"), "code": "x"}),
        Event(type="agent_decision", data={
            "at_node": 1, "chosen": {"kind": "improve", "parent_id": 0},
            "legal": [{"kind": "draft"}, {"kind": "improve", "parent_id": 0}],
            "recommended": {"kind": "improve", "parent_id": 0}, "rationale": "refine"}),
        Event(type="node_evaluated", data={"node_id": 0, "metric": 3.0}),
    ]
    st = fold(events)
    assert len(st.agent_decisions) == 1 and st.agent_decisions[0]["chosen"]["kind"] == "improve"
    assert st.best().id == 0                              # decision never altered selection
    # An OLD log (no agent_decision) folds to an empty list — additive, non-load-bearing.
    assert fold([e for e in events if e.type != "agent_decision"]).agent_decisions == []


# --------------------------------------------------------------------------- end-to-end
def _toy_engine(run_dir, *, unified: bool, drives: bool, max_nodes: int = 10):
    task = ToyTask()
    if unified:
        r, _split_d = task.build_roles()
        _r2, d = task.build_roles()
        agent = UnifiedAgent(researcher=r, developer=d, strategist=None, pilot_client=None)
        researcher = developer = agent
    else:
        researcher, developer = task.build_roles()
    return Engine(
        run_dir, task=task, researcher=researcher, developer=developer,
        sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=max_nodes),
        max_parallel=1, max_nodes=max_nodes, n_seeds=3,
        unified_agent=unified, agent_drives_actions=drives)


def test_unified_mode_skips_developer_backend_swap(tmp_path):
    """R1: in unified mode researcher IS developer (one agent); a Strategy-driven developer swap
    must NOT replace self.developer (it would desync the two handles + rebuild a whole new agent).
    The split path still swaps normally."""
    task = ToyTask()
    r, _ = task.build_roles()
    _r2, d = task.build_roles()
    agent = UnifiedAgent(researcher=r, developer=d, strategist=None, pilot_client=None)
    swapped = []
    factory = lambda name: (swapped.append(name) or object())   # noqa: E731

    uni = Engine(tmp_path / "u", task=task, researcher=agent, developer=agent,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=8),
                 developer_factory=factory, unified_agent=True)
    uni._apply_strategy({"developer": "opencode"})
    assert uni.developer is uni.researcher is agent and swapped == []   # no swap, identity intact

    rr, dd = task.build_roles()
    split = Engine(tmp_path / "s", task=task, researcher=rr, developer=dd,
                   sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=8),
                   developer_factory=factory, unified_agent=False)
    split._apply_strategy({"developer": "opencode"})
    assert swapped == ["opencode"] and split.developer is not dd       # split still swaps


def test_unified_self_drive_toy_parity_and_replay(tmp_path):
    # Pure policy path (baseline).
    base = anyio.run(_toy_engine(tmp_path / "base", unified=False, drives=False).run)
    # Self-driving unified path: pilot has no model -> takes the policy recommendation each turn,
    # so the OUTCOME must match the baseline while the log additionally carries agent_decision.
    uni = anyio.run(_toy_engine(tmp_path / "uni", unified=True, drives=True).run)

    assert base.finished and uni.finished
    assert len(uni.nodes) == len(base.nodes) == 10
    assert {i: n.operator for i, n in uni.nodes.items()} == \
           {i: n.operator for i, n in base.nodes.items()}          # identical tree shape
    assert abs(uni.best().metric - base.best().metric) < 1e-9      # identical outcome
    assert uni.agent_decisions and not base.agent_decisions        # self-driving audit recorded

    # Replay: re-folding the recorded log reproduces the same state with NO model/engine calls.
    replayed = fold(EventStore(tmp_path / "uni" / "events.jsonl").read_all())
    assert sorted(replayed.nodes) == sorted(uni.nodes)
    assert replayed.best().id == uni.best().id
    assert len(replayed.agent_decisions) == len(uni.agent_decisions)
