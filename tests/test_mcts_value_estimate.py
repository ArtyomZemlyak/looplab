"""The LLM value estimate the MCTS tree never had (docs/BACKLOG.md §0.1 row 17).

The tree valued a node by its metric alone, so two candidates that scored the same were one point to
it — a branch nobody has expanded, a branch whose every child regressed, and a branch one edit from
a win — and the only term separating them counts VISITS and cannot read a line of what either one
did. These tests are about the four things that make an opt-in, PAID selection signal safe: that OFF
is byte-identical to the old score AND buys no call, that ON separates exactly the pair the item
names, that an uninformative answer is inert rather than a quiet bias, and that the number the
policy reads comes off the LOG (so a replay expands the same nodes) and never off a live call.
"""
from __future__ import annotations

import pytest

from looplab.core.config import Settings
from looplab.core.llm import BudgetExceeded
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.policy import (KIND_IMPROVE, META_SCORES, MCTSPolicy, make_policy,
                                   value_estimate)


def _state(*rows, direction="min"):
    """`rows` are `(node_id, parents, metric, value_prior)`."""
    st = RunState(direction=direction)
    for nid, parents, metric, prior in rows:
        node = Node(id=nid, parent_ids=list(parents), operator="improve",
                    idea=Idea(operator="improve", params={}, rationale=""))
        node.status = NodeStatus.evaluated
        node.metric = metric
        node.feasible = True
        node.eval_seconds = 10.0
        node.value_prior = prior
        st.nodes[nid] = node
    return st


# --------------------------------------------------------------------------- #
# The pure adjustment
# --------------------------------------------------------------------------- #

def test_off_and_unestimated_both_return_the_reward_untouched():
    """The two ways the term is inert, and they are DIFFERENT facts: the operator turned it off, and
    nobody asked the model about this branch. Neither may become "the model called it average"."""
    assert value_estimate(1.37, 0.9, 1, 0.0) == 1.37          # weight 0: the knob at rest
    assert value_estimate(1.37, 0.9, 1, -1.0) == 1.37         # a negative weight reads priors BACKWARDS
    assert value_estimate(1.37, None, 1, 0.4) == 1.37         # nobody asked
    assert value_estimate(1.37, None, 1, 0.4) is not None


def test_an_uninformative_estimate_is_exactly_inert():
    """The failure mode this centring exists to prevent, and it is invisible in any test where the
    priors differ: a model that answers the same middling number for every branch must move nothing.
    Blending toward an ABSOLUTE anchor instead (`2 × prior`) fails here — on a run whose rewards sit
    at 1.47 a flat 0.5 would drag every candidate toward 1.0, and hardest the least-visited one,
    which is the exploration bonus running backwards."""
    for visits in (1, 2, 7):
        for reward in (0.4, 1.0, 1.47, 1.9):
            assert value_estimate(reward, 0.5, visits, 0.4) == pytest.approx(reward)


def test_the_sign_follows_the_estimate_and_the_size_follows_the_weight():
    assert value_estimate(1.0, 1.0, 1, 0.4) == pytest.approx(1.2)    # wide open: +weight/2 at 1 visit
    assert value_estimate(1.0, 0.0, 1, 0.4) == pytest.approx(0.8)    # spent: the mirror image
    assert value_estimate(1.0, 1.0, 1, 0.8) == pytest.approx(1.4)    # twice the weight, twice the say


def test_the_estimate_fades_as_the_subtree_is_really_measured():
    """The LATS/AlphaZero property and the reason this is a PRIOR: it speaks loudest where the
    evidence is thinnest. A prior that kept its full weight after ten measured descendants would be
    a model's guess outranking ten evaluations."""
    said = [value_estimate(1.0, 1.0, v, 0.4) - 1.0 for v in (1, 2, 4, 9)]
    assert said == sorted(said, reverse=True)
    assert said[0] == pytest.approx(0.2) and said[-1] == pytest.approx(0.04)


def test_an_out_of_range_prior_is_clamped_rather_than_trusted():
    """`value_estimate` is reachable from the fold's own validation AND from a hand-built policy, so
    it clamps rather than assuming its caller already did."""
    assert value_estimate(1.0, 5.0, 1, 0.4) == value_estimate(1.0, 1.0, 1, 0.4)
    assert value_estimate(1.0, -5.0, 1, 0.4) == value_estimate(1.0, 0.0, 1, 0.4)


# --------------------------------------------------------------------------- #
# The policy
# --------------------------------------------------------------------------- #

def test_off_is_byte_identical_even_when_the_log_carries_estimates():
    """The default must reproduce the historical decision exactly, not approximately — and it must
    do so on a state that HAS priors, which is the case a `value_weight=0` run resumed from a
    `value_weight>0` one really is."""
    st = _state((0, (), 1.0, 0.9), (1, (), 1.0, 0.1), (2, (0,), 0.5, None))
    plain = MCTSPolicy(n_seeds=1, max_nodes=9).next_actions(st)
    zero = MCTSPolicy(n_seeds=1, max_nodes=9, value_weight=0.0).next_actions(st)
    bare = _state((0, (), 1.0, None), (1, (), 1.0, None), (2, (0,), 0.5, None))
    unpriored = MCTSPolicy(n_seeds=1, max_nodes=9).next_actions(bare)
    assert plain[0][META_SCORES] == zero[0][META_SCORES] == unpriored[0][META_SCORES]
    assert plain[0]["parent_id"] == zero[0]["parent_id"] == unpriored[0]["parent_id"]


def test_the_estimate_separates_the_pair_the_item_names():
    """THE HEADLINE. Two candidates with the SAME metric and the same (zero) expansion history: one
    branch the model calls wide open, one it calls spent. Off, they are one point to the tree and the
    tie breaks by id — so make the SPENT one #0, to prove the estimate did the work."""
    st = _state((0, (), 0.5, 0.05), (1, (), 0.5, 0.95))
    off = MCTSPolicy(n_seeds=1, max_nodes=9).next_actions(st)[0]
    on = MCTSPolicy(n_seeds=1, max_nodes=9, value_weight=0.6).next_actions(st)[0]
    assert off["kind"] == KIND_IMPROVE and off["parent_id"] == 0     # tie -> lowest id
    assert on["parent_id"] == 1                                      # the estimate broke the tie
    assert on[META_SCORES][1] > on[META_SCORES][0]


def test_a_large_enough_metric_gap_still_outranks_a_poor_estimate():
    """An estimate that always wins is an LLM policy, not a value term: the point is the TRADE, and
    the weight sets it. The same pair flips between 0.2 and 1.2, which is the knob doing its job and
    is why the unit is stated in `Settings.mcts_value_weight` rather than left to taste."""
    st = _state((0, (), 0.01, 0.0), (1, (), 0.6, 1.0))       # #0 far better (min), model says spent
    gentle = MCTSPolicy(n_seeds=1, max_nodes=9, value_weight=0.2).next_actions(st)[0]
    assert gentle["parent_id"] == 0
    steep = MCTSPolicy(n_seeds=1, max_nodes=9, value_weight=1.2).next_actions(st)[0]
    assert steep["parent_id"] == 1


def test_a_negative_weight_cannot_read_every_estimate_backwards():
    """The same clamp `c` and `cost_weight` get, for the same reason: a `-1` here would send the
    search at the branches the model called spent and be recorded as a legitimate strategy."""
    assert MCTSPolicy(value_weight=-1.0).value_weight == 0.0
    assert make_policy("mcts", n_seeds=1, max_nodes=4, value_weight=-3.0).value_weight == 0.0


def test_the_setting_reaches_the_policy_and_only_that_policy():
    assert Settings().mcts_value_weight == 0.0        # off by default
    assert make_policy("mcts", n_seeds=1, max_nodes=4, value_weight=0.75).value_weight == 0.75
    for name in ("greedy", "evolutionary", "asha"):
        policy = make_policy(name, n_seeds=1, max_nodes=4, value_weight=0.75)
        assert not hasattr(policy, "value_weight")


# --------------------------------------------------------------------------- #
# The fold: node_value_estimated -> Node.value_prior
# --------------------------------------------------------------------------- #

def _log(tmp_path) -> EventStore:
    s = EventStore(tmp_path / "e.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "max"})
    return s


def _node(s: EventStore, nid: int, metric: float) -> None:
    s.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": f"exp {nid}"}})
    s.append("node_evaluated", {"node_id": nid, "metric": metric})


def test_the_estimate_folds_onto_the_node_and_replays_idempotently(tmp_path):
    s = _log(tmp_path)
    _node(s, 0, 0.9)
    row = {"node_id": 0, "generation": 0, "value": 0.62, "rationale": "one lever untried"}
    s.append("node_value_estimated", row)
    s.append("node_value_estimated", row)
    assert fold(s.read_all()).nodes[0].value_prior == 0.62


def test_an_unstamped_or_stale_estimate_is_dropped(tmp_path):
    """A brand-new event with one writer that always stamps the generation — so a missing stamp is
    REJECTED rather than read as current, and an estimate formed against a reset-abandoned attempt
    describes code the node no longer carries."""
    s = _log(tmp_path)
    _node(s, 0, 0.9)
    s.append("node_value_estimated", {"node_id": 0, "value": 0.62})              # no generation
    assert fold(s.read_all()).nodes[0].value_prior is None
    s.append("node_reset", {"node_id": 0, "from_stage": "eval"})                 # attempt -> 1
    s.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 0.9})
    s.append("node_value_estimated", {"node_id": 0, "generation": 0, "value": 0.62})
    assert fold(s.read_all()).nodes[0].value_prior is None


def test_an_out_of_range_or_unusable_estimate_never_reaches_the_policy(tmp_path):
    s = _log(tmp_path)
    for nid in (0, 1, 2):
        _node(s, nid, 0.9)
    s.append("node_value_estimated", {"node_id": 0, "generation": 0, "value": 1.4})
    s.append("node_value_estimated", {"node_id": 1, "generation": 0, "value": "0.5"})
    s.append("node_value_estimated", {"node_id": 2, "generation": 0, "value": None})
    st = fold(s.read_all())
    assert [st.nodes[nid].value_prior for nid in (0, 1, 2)] == [None, None, None]


def test_a_tombstoned_or_aborted_node_takes_no_estimate(tmp_path):
    """The same lifecycle gate the value, the visit count and the expense already share: a logically
    deleted node is invisible to selection, so letting an estimate land on it would be recording a
    steer for a node nothing can steer to."""
    s = _log(tmp_path)
    _node(s, 0, 0.9)
    _node(s, 1, 0.9)
    s.append("node_tombstoned", {"node_ids": [0]})
    s.append("node_abort", {"node_id": 1})
    for nid in (0, 1):
        s.append("node_value_estimated", {"node_id": nid, "generation": 0, "value": 0.62})
    st = fold(s.read_all())
    assert st.nodes[0].value_prior is None and st.nodes[1].value_prior is None


# --------------------------------------------------------------------------- #
# The engine cadence — driven, with a stub client
# --------------------------------------------------------------------------- #

class _ValueClient:
    """Answers the structured branch-value ask, cycling so the two candidates differ."""

    def __init__(self, answers=(0.9, 0.1)):
        self.answers = list(answers)
        self.calls = 0
        self.messages: list = []

    def complete_tool(self, messages, json_schema):
        self.messages.append(messages)
        self.calls += 1
        return {"headroom": self.answers[(self.calls - 1) % len(self.answers)], "why": "because"}

    def complete_text(self, messages):
        return "{}"


class _BankruptClient:
    """The operator's spend ceiling, hit inside the ask."""

    def complete_tool(self, messages, json_schema):
        raise BudgetExceeded("cost limit reached")

    def complete_text(self, messages):
        raise BudgetExceeded("cost limit reached")


def _engine(tmp_path, monkeypatch, client, *, value_weight):
    from tests.factories import make_engine

    eng = make_engine(tmp_path / "run", policy=MCTSPolicy(n_seeds=2, max_nodes=9,
                                                          value_weight=value_weight))
    monkeypatch.setattr(type(eng), "_reflect_client", lambda self: client, raising=True)
    for nid, metric in ((0, 0.5), (1, 0.5)):
        eng.store.append("node_created", {
            "node_id": nid, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {}, "rationale": f"exp {nid}"}})
        eng.store.append("node_evaluated", {"node_id": nid, "metric": metric})
    return eng


def test_the_cadence_buys_nothing_when_the_policy_cannot_use_the_number(tmp_path, monkeypatch):
    """OFF is not "the estimate is ignored", it is "the call is never made" — a knob that spent
    money to produce a number nothing reads would be the worst of both."""
    client = _ValueClient()
    eng = _engine(tmp_path, monkeypatch, client, value_weight=0.0)
    before = len(eng.store.read_all())
    eng._maybe_estimate_node_values(fold(eng.store.read_all()))
    assert client.calls == 0
    assert len(eng.store.read_all()) == before


def test_the_cadence_records_the_estimate_and_the_policy_then_reads_it(tmp_path, monkeypatch):
    """End to end, on the pair the item names: two tied candidates, an estimate per branch, the row
    in the LOG, the prior on the folded node, and the pick moved by it."""
    client = _ValueClient(answers=(0.05, 0.95))          # #0 spent, #1 wide open
    eng = _engine(tmp_path, monkeypatch, client, value_weight=0.6)
    state = eng._maybe_estimate_node_values(fold(eng.store.read_all()))
    assert client.calls == 2
    rows = [e for e in eng.store.read_all() if e.type == "node_value_estimated"]
    assert [(r.data["node_id"], r.data["value"]) for r in rows] == [(0, 0.05), (1, 0.95)]
    assert all(r.data["generation"] == 0 for r in rows)
    assert (state.nodes[0].value_prior, state.nodes[1].value_prior) == (0.05, 0.95)
    # the returned state is the RE-FOLD, so the very next selection sees the estimates
    assert eng.policy.next_actions(state)[0]["parent_id"] == 1
    # and the ask is about the branch, not a second copy of the metric the policy already reads
    asked = " ".join(m["content"] for m in client.messages[0])
    assert "how much" in asked.lower() or "still have left" in asked.lower()


def test_the_cadence_asks_once_per_node_and_then_leaves_it_alone(tmp_path, monkeypatch):
    """The estimate is frozen in the log, so a second boundary must not re-buy it: `value_prior`
    already on the folded node is what takes the candidate out of the pool."""
    client = _ValueClient()
    eng = _engine(tmp_path, monkeypatch, client, value_weight=0.6)
    state = eng._maybe_estimate_node_values(fold(eng.store.read_all()))
    assert client.calls == 2
    again = eng._maybe_estimate_node_values(state)
    assert client.calls == 2                                  # nothing re-bought
    assert again is state                                     # and no re-fold either


def test_a_single_candidate_is_not_worth_asking_about(tmp_path, monkeypatch):
    """With one candidate the pick is forced, so an estimate could not move it and buying one would
    be spending on a decision already made."""
    client = _ValueClient()
    eng = _engine(tmp_path, monkeypatch, client, value_weight=0.6)
    eng.store.append("node_tombstoned", {"node_ids": [1]})
    eng._maybe_estimate_node_values(fold(eng.store.read_all()))
    assert client.calls == 0


def test_the_spend_ceiling_ends_the_run_rather_than_the_estimate(tmp_path, monkeypatch):
    """A swallowed `BudgetExceeded` lets a run keep billing past the limit set to stop it — the
    defect `verifier.py::verify` had at a selection site (doc 50 AG-01). It must propagate."""
    eng = _engine(tmp_path, monkeypatch, _BankruptClient(), value_weight=0.6)
    with pytest.raises(BudgetExceeded):
        eng._maybe_estimate_node_values(fold(eng.store.read_all()))


class _JunkClient:
    """Answers unparseably — the ordinary degraded-endpoint case."""

    def __init__(self):
        self.calls = 0

    def complete_tool(self, messages, json_schema):
        self.calls += 1
        return {"headroom": "not a number"}

    def complete_text(self, messages):
        return "sorry"


def test_an_unusable_answer_abstains_and_is_not_re_asked_this_process(tmp_path, monkeypatch):
    """An abstention is not an average: the node stays unestimated (so `value_estimate` leaves its
    reward alone), and a degraded client cannot re-ask about the same attempt every boundary."""
    client = _JunkClient()
    eng = _engine(tmp_path, monkeypatch, client, value_weight=0.6)
    state = eng._maybe_estimate_node_values(fold(eng.store.read_all()))
    assert [e for e in eng.store.read_all() if e.type == "node_value_estimated"] == []
    assert all(n.value_prior is None for n in state.nodes.values())
    spent = client.calls
    eng._maybe_estimate_node_values(state)
    assert client.calls == spent


def test_the_cadence_runs_before_the_strategist_can_rebuild_the_policy():
    """Ordering inside `_run_cadences`: the weight the estimate is bought for is the one this turn's
    policy holds, and `_apply_strategy` may replace that policy. Bought after, the estimate would be
    spent on a weight the turn no longer uses."""
    import inspect

    from looplab.engine.orchestrator import Engine

    src = inspect.getsource(Engine._run_cadences)
    assert src.index("_maybe_estimate_node_values") < src.index("_maybe_consult_strategist")


def test_a_policy_switch_carries_the_weight_forward():
    """A run-level knob the engine does not hold: a Strategist switch to `mcts` that dropped it would
    silently stop BUYING the estimates as well as stop reading them."""
    import inspect

    from looplab.engine.strategy import StrategyCadenceMixin

    src = inspect.getsource(StrategyCadenceMixin._apply_strategy)
    assert 'pp.setdefault("value_weight", getattr(self.policy, "value_weight", 0.0))' in src
