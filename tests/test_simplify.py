"""THE CHAMPION CAN BE SIMPLIFIED (doc 67 67.5, `Settings.ablation_simplify`).

A code-block ablation re-runs the champion once per pipeline block with that block commented out. The
probes are off-tree, so a block the champion did not need was measured, recorded and kept: the
champion only grew. Now a probe that measured the objective NO WORSE without a block NOMINATES a node
— the parent's program with that block commented out, exactly what the probe ran — which is built and
evaluated the ordinary way (`search/policy.py::simplify_actions`, `engine/ablation.py::_simplify`),
and the selector takes it on a tie by a NAMED rule: non-inferiority within one SE of the difference
(`core/fitness.py::one_se_non_inferior`), "on a tie, simpler"
(`events/replay_selection.py::simpler_tie`). Without that rule an equal simplification never becomes
the champion, and "the champion only grows" would have survived the nomination.
"""
from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest

from factories import make_engine
from looplab.core.models import Idea, durable_idea_payload
from looplab.events.replay import fold
from looplab.search.policy import (KIND_SIMPLIFY, GreedyTree, legal_actions, simplify_actions)

CODE = "import os\n\nx = 1\n\nprint(x)\n"


# ------------------------------------------------------------------ the named rule

@pytest.mark.parametrize("candidate, incumbent, std, n, direction, held", [
    (1.0, 1.0, 0.0, 0, "min", True),        # equal: not worse
    (1.01, 1.0, 0.0, 0, "min", False),      # no spread: any loss is a loss
    (0.99, 1.0, 0.0, 0, "max", False),
    (1.01, 1.0, 0.1, 4, "min", True),       # within one SE of the difference
    (1.2, 1.0, 0.1, 4, "min", False),       # beyond it
    (0.95, 1.0, 0.1, 4, "max", True),
    (0.8, 1.0, 0.1, 4, "max", False),
])
def test_non_inferiority_is_the_superiority_rule_turned_around(candidate, incumbent, std, n,
                                                               direction, held):
    from looplab.core.fitness import one_se_non_inferior
    from looplab.trust import gate

    assert one_se_non_inferior(candidate, incumbent, std, n, direction, std, n) is held
    assert gate.one_se_non_inferior is one_se_non_inferior, "the gate's spelling is the same rule"


# ------------------------------------------------------------------ the fold's receipt

def _log(store, *nodes, direction="min"):
    """A log of crafted nodes: `(id, parents, metric, extra)` — `extra` goes into `node_created`.
    A node carrying a `simplified` receipt is given the code it names — its parent's code with that
    block commented out — unless `extra` spells its own: the fold holds a receipt to its cut."""
    from looplab.core.code_blocks import code_blocks, comment_block

    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g",
                                 "direction": direction})
    codes: dict = {}
    for node_id, parents, metric, extra in nodes:
        operator = extra.pop("operator", "draft" if not parents else "improve")
        code = CODE
        receipt = extra.get("simplified")
        if isinstance(receipt, dict) and receipt.get("parent_id") in codes:
            parent_code = codes[receipt["parent_id"]]
            spans = code_blocks(parent_code)
            if isinstance(receipt.get("block"), int) and 0 <= receipt["block"] < len(spans):
                code = comment_block(parent_code, spans[receipt["block"]])
        codes[node_id] = extra.get("code", code)
        store.append("node_created", {
            "node_id": node_id, "parent_ids": list(parents), "operator": operator,
            "idea": durable_idea_payload(Idea(operator=operator, params={"x": 0.5},
                                              rationale="r")),
            "code": code, "files": {}, **extra})
        if metric is not None:
            store.append("node_evaluated", {"node_id": node_id, "generation": 0, "metric": metric,
                                            "violations": []})


def _receipt(parent, block=0, generation=0):
    return {"operator": "simplify", "parent_generations": {str(parent): generation},
            "simplified": {"parent_id": parent, "generation": generation, "block": block,
                           "ablation_id": "a" * 32}}


def _state(tmp_path, *nodes, direction="min", extra=()):
    from looplab.events.eventstore import EventStore
    store = EventStore(tmp_path / "events.jsonl")
    _log(store, *nodes, direction=direction)
    for etype, data in extra:
        store.append(etype, data)
    return fold(store.read_all())


def test_a_receipt_is_folded_only_where_the_build_that_writes_it_would(tmp_path):
    """The operator, ONE parent, and that parent at the lifecycle the node was cut from — a
    hand-edited receipt on any other row makes no node "simpler"."""
    good = _receipt(0)
    state = _state(tmp_path / "a", (0, [], 1.0, {}), (1, [0], 1.0, dict(good)))
    assert state.nodes[1].simplified == good["simplified"]
    for bad in ({**good, "operator": "improve"},
                {**good, "simplified": {**good["simplified"], "parent_id": 7}},
                {**good, "simplified": {**good["simplified"], "generation": 1}},
                {**good, "simplified": {**good["simplified"], "block": True}},
                {**good, "simplified": {**good["simplified"], "ablation_id": None}},
                {**good, "simplified": "block 0"}):
        rd = tmp_path / f"b{len(list(tmp_path.iterdir()))}"
        rd.mkdir()
        state = _state(rd, (0, [], 1.0, {}), (1, [0], 1.0, dict(bad)))
        assert state.nodes[1].simplified is None, bad


# ------------------------------------------------------------------ on a tie, simpler

def test_an_equal_simplification_is_the_champion_and_a_worse_one_is_not(tmp_path):
    tied = _state(tmp_path / "tied", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)))
    assert tied.best_node_id == 1, "equal, and simpler"
    worse = _state(tmp_path / "worse", (0, [], 1.0, {}), (1, [0], 1.0001, _receipt(0)))
    assert worse.best_node_id == 0, "no spread measured: any loss is a loss"
    better = _state(tmp_path / "max", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)),
                    direction="max")
    assert better.best_node_id == 1
    plain = _state(tmp_path / "plain", (0, [], 1.0, {}), (1, [0], 1.0, {}), direction="min")
    assert plain.best_node_id == 0, "no receipt, no rule: the historical id tie-break"


def test_the_deepest_simplification_that_is_still_not_worse_wins(tmp_path):
    chain = _state(tmp_path / "chain", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)),
                   (2, [1], 1.0, _receipt(1, block=1)))
    assert chain.best_node_id == 2
    # Held to the LEADER, not to the link before it: no drift down a chain of small losses.
    drift = _state(tmp_path / "drift", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)),
                   (2, [1], 1.001, _receipt(1, block=1)))
    assert drift.best_node_id == 1


def test_a_simplification_of_a_replaced_lifecycle_is_not_simpler(tmp_path):
    """A `node_reset` gives the parent a new lifecycle; a child cut from the old one is not a
    simplification of what stands there now."""
    state = _state(tmp_path / "reset", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)), extra=[
        ("node_reset", {"node_id": 0, "generation": 0}),
        ("node_created", {"node_id": 0, "generation": 1, "parent_ids": [], "operator": "draft",
                          "idea": durable_idea_payload(Idea(operator="draft", params={"x": 0.5},
                                                            rationale="r")),
                          "code": CODE, "files": {}}),
        ("node_evaluated", {"node_id": 0, "generation": 1, "metric": 1.0, "violations": []})])
    assert state.nodes[0].attempt == 1
    assert state.best_node_id == 0


def test_the_confirmation_spread_makes_a_near_tie_a_tie(tmp_path):
    """With BOTH confirmed, non-inferiority has its spread: within one SE of the difference the
    simplification stands; beyond it, it does not."""
    from looplab.events.replay_selection import select_best_node

    state = _state(tmp_path / "c", (0, [], 1.0, {}), (1, [0], 1.05, _receipt(0)))
    for node, mean in ((state.nodes[0], 1.0), (state.nodes[1], 1.05)):
        node.confirmed_mean, node.confirmed_std, node.confirmed_seeds = mean, 0.1, 4
    pool = [state.nodes[0], state.nodes[1]]
    assert select_best_node(state, pool).id == 1, "0.05 < SE_diff 0.0707"
    state.nodes[1].confirmed_mean = 1.2
    assert select_best_node(state, pool).id == 0


def test_a_confirmed_mean_is_never_held_against_a_single_measurement(tmp_path):
    """The slot passes rank a MIXED pool; a cut is compared with its leader on one ruler or not at
    all."""
    from looplab.events.replay_selection import simpler_tie

    state = _state(tmp_path / "m", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)))
    state.nodes[0].confirmed_mean, state.nodes[0].confirmed_std = 1.0, 0.1
    state.nodes[0].confirmed_seeds = 4
    ranked = [state.nodes[0], state.nodes[1]]
    assert simpler_tie(state, ranked[0], ranked) is ranked[0]
    state.nodes[1].confirmed_mean, state.nodes[1].confirmed_std = 1.0, 0.1
    state.nodes[1].confirmed_seeds = 4
    assert simpler_tie(state, ranked[0], ranked) is ranked[1]


def test_a_chain_is_held_to_the_leader_so_small_losses_do_not_add_up(tmp_path):
    """Each link within one SE of the node before it, the second one beyond one SE of the LEADER:
    the rule measures every cut against the node the pick crowned, never against its own parent."""
    from looplab.events.replay_selection import select_best_node

    state = _state(tmp_path / "d", (0, [], 1.0, {}), (1, [0], 1.05, _receipt(0)),
                   (2, [1], 1.10, _receipt(1, block=1)))
    for node, mean in zip(state.nodes.values(), (1.0, 1.05, 1.10)):
        node.confirmed_mean, node.confirmed_std, node.confirmed_seeds = mean, 0.1, 4
    assert select_best_node(state, list(state.nodes.values())).id == 1


def test_the_verifier_breaks_the_tie_before_simplicity(tmp_path):
    from looplab.events.replay_selection import select_best_node

    state = _state(tmp_path / "v", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)))
    state.select_verifier_tiebreak = True
    state.nodes[0].verifier_score, state.nodes[1].verifier_score = 0.9, 0.4
    assert select_best_node(state, [state.nodes[0], state.nodes[1]]).id == 0
    state.nodes[1].verifier_score = 0.9
    assert select_best_node(state, [state.nodes[0], state.nodes[1]]).id == 1


def test_the_holdout_stage_takes_a_simplification_graded_no_worse(tmp_path):
    from looplab.events.replay_selection import select_best_node

    state = _state(tmp_path / "h", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)))
    state.holdout_select = True
    state.nodes[0].holdout_metric, state.nodes[1].holdout_metric = 0.7, 0.7
    pool = [state.nodes[0], state.nodes[1]]
    assert select_best_node(state, pool).id == 1
    state.nodes[1].holdout_metric = 0.71
    assert select_best_node(state, pool).id == 0, "the unseen grade decides; no spread there"


def test_the_extra_measurement_is_an_extra_slot_never_a_better_nodes(tmp_path):
    """The simplification that ties the leader is measured — or it could never win — in an EXTRA
    slot at the end: every node of the pass's own top-k keeps its slot and its place, the leader
    first (critic 2026-09-27, driven: moved to the front it took the only confirm slot, pushed a
    tied runner-up out of a budget-limited pass, stood as the significance test's leader, and took a
    holdout slot from a better node)."""
    from looplab.events.replay_selection import simpler_slots

    state = _state(tmp_path / "s", (0, [], 1.0, {}), (2, [], 1.5, {}), (3, [], 1.2, {}),
                   (1, [0], 1.0, _receipt(0)))
    ranked = [state.nodes[0], state.nodes[3], state.nodes[2], state.nodes[1]]
    assert [n.id for n in simpler_slots(state, ranked, 1)] == [0, 1]
    assert [n.id for n in simpler_slots(state, ranked, 2)] == [0, 3, 1]
    assert [n.id for n in simpler_slots(state, ranked, 4)] == [0, 3, 2, 1], "already in: no dup"
    plain = [state.nodes[2], state.nodes[0]]
    assert simpler_slots(state, plain, 1) == [state.nodes[2]], "no tie, no extra slot"
    assert simpler_slots(state, [], 3) == [] and simpler_slots(state, ranked, 0) == []


# ------------------------------------------------------------------ the nomination

def _ablated(tmp_path, *records, direction="min"):
    return _state(tmp_path, (0, [], 1.0, {}), direction=direction, extra=[
        ("ablate", {"parent_id": 0, "generation": 0, "ablation_id": aid, "mode": mode,
                    "blocks": 3, "impacts": {}, "signed_impacts": signed, **extra})
        for aid, mode, signed, extra in records])


def test_a_no_worse_code_block_probe_nominates_its_program_best_first(tmp_path):
    state = _ablated(tmp_path / "n", ("a" * 32, "code_blocks",
                                      {"0": 0.0, "1": -0.5, "2": 0.25}, {}))
    actions = simplify_actions(state, state.nodes[0])
    assert [(a["kind"], a["block"]) for a in actions] == [(KIND_SIMPLIFY, 2), (KIND_SIMPLIFY, 0)]
    assert all(a["ablation_id"] == "a" * 32 for a in actions)


@pytest.mark.parametrize("record", [
    ("a" * 32, "params", {"0": 0.3}, {}),                     # a parameter set to 0.0 is not removed
    ("a" * 32, "code_blocks", {"0": None}, {}),               # the run broke without it
    ("a" * 32, "code_blocks", {"0": 0.3}, {"superseded": True}),
    ("a" * 32, "code_blocks", {"7": 0.3}, {}),                # no such block
])
def test_nothing_else_nominates(tmp_path, record):
    state = _ablated(tmp_path / "x", record)
    assert simplify_actions(state, state.nodes[0]) == []


def test_a_comment_only_block_is_never_nominated(tmp_path):
    """Its probe re-ran the identical program: "no worse" measured nothing, and a node cut from it
    would take the tie as a simplification that simplified nothing."""
    from looplab.events.eventstore import EventStore

    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": durable_idea_payload(Idea(operator="draft", params={"x": 0.5}, rationale="r")),
        "code": "# a header\n# more of it\n\nimport os\n\nprint(1)\n", "files": {}})
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.0,
                                    "violations": []})
    store.append("ablate", {"parent_id": 0, "generation": 0, "ablation_id": "a" * 32,
                            "mode": "code_blocks", "blocks": 3, "impacts": {},
                            "signed_impacts": {"0": 0.0, "1": 0.0, "2": -1.0}})
    state = fold(store.read_all())
    assert [a["block"] for a in simplify_actions(state, state.nodes[0])] == [1]


def test_the_probes_of_a_replaced_lifecycle_nominate_nothing(tmp_path):
    """A `node_reset` gave the node a new lifecycle: the blocks an ablation of the OLD one measured
    are not the new program's."""
    idea = durable_idea_payload(Idea(operator="draft", params={"x": 0.5}, rationale="r"))
    state = _ablated(tmp_path / "g", ("a" * 32, "code_blocks", {"0": 0.3}, {}))
    assert simplify_actions(state, state.nodes[0]), "the current lifecycle's probe nominates"
    from looplab.events.eventstore import EventStore
    store = EventStore(tmp_path / "g" / "events.jsonl")
    store.append("node_reset", {"node_id": 0, "generation": 0})
    store.append("node_created", {"node_id": 0, "generation": 1, "parent_ids": [],
                                  "operator": "draft", "idea": idea, "code": CODE, "files": {}})
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 1.0,
                                    "violations": []})
    state = fold(store.read_all())
    assert state.nodes[0].attempt == 1 and state.ablations
    assert simplify_actions(state, state.nodes[0]) == []


def test_a_later_pass_speaks_last_and_a_spent_block_is_never_nominated_again(tmp_path):
    state = _ablated(tmp_path / "l", ("a" * 32, "code_blocks", {"0": 0.1, "1": 0.2}, {}),
                     ("b" * 32, "code_blocks", {"0": -0.1}, {}))
    assert [a["block"] for a in simplify_actions(state, state.nodes[0])] == [1]
    spent = _state(tmp_path / "spent", (0, [], 1.0, {}), (1, [0], None, _receipt(0, block=1)),
                   extra=[("ablate", {"parent_id": 0, "generation": 0, "ablation_id": "a" * 32,
                                      "mode": "code_blocks", "blocks": 3, "impacts": {},
                                      "signed_impacts": {"1": 0.2}})])
    assert simplify_actions(spent, spent.nodes[0]) == [], "a failed or pending build spends it"
    assert simplify_actions(state, state.nodes[0], refused={(0, 0, 1)}) == []


def test_the_policy_nominates_only_where_the_engine_enabled_it(tmp_path):
    state = _ablated(tmp_path / "p", ("a" * 32, "code_blocks", {"0": 0.0}, {}))
    policy = GreedyTree(n_seeds=1, max_nodes=10, ablate_every=1, enable_merge=False)
    assert policy.next_actions(state)[0]["kind"] != KIND_SIMPLIFY, "off by default"
    assert not any(a["kind"] == KIND_SIMPLIFY for a in legal_actions(state, policy, max_nodes=10))
    policy.simplify_ablated = True
    action = policy.next_actions(state)[0]
    assert (action["kind"], action["parent_id"], action["block"]) == (KIND_SIMPLIFY, 0, 0)
    assert any(a["kind"] == KIND_SIMPLIFY for a in legal_actions(state, policy, max_nodes=10))
    policy.ablation_capable = False
    assert policy.next_actions(state)[0]["kind"] != KIND_SIMPLIFY
    policy.ablation_capable, policy.simplify_refused = True, frozenset({(0, 0, 0)})
    assert policy.next_actions(state)[0]["kind"] != KIND_SIMPLIFY, "a refused build yields the turn"


def test_the_strategist_whitelist_keeps_the_switch_and_only_a_grant_applies_it(tmp_path):
    """The whitelist row keeps a boolean `operators.simplify` and drops anything else; the engine
    applies it only under an `ablation_simplify` grant, which no role holds by default — a new
    mechanism is off until an arm measures it, and a Strategist may not turn it on by itself."""
    from looplab.agents.strategist import StrategyContext, validate_strategy
    from looplab.core.config import default_agent_control

    ctx = StrategyContext(available_policies=("greedy",), available_developers=())
    assert validate_strategy({"operators": {"simplify": True}}, ctx)["operators"] == {
        "simplify": True}
    dropped = validate_strategy({"operators": {"simplify": "yes"}}, ctx) or {}
    assert "simplify" not in (dropped.get("operators") or {})
    assert "ablation_simplify" not in default_agent_control()
    locked = make_engine(tmp_path / "locked")
    locked._apply_strategy({"operators": {"simplify": True}})
    assert locked._ablation_simplify is False and locked.policy.simplify_ablated is False
    granted = make_engine(tmp_path / "granted", agent_control={
        **default_agent_control(), "ablation_simplify": ["strategist"]})
    granted._apply_strategy({"operators": {"simplify": True}})
    assert granted._ablation_simplify is True and granted.policy.simplify_ablated is True
    granted._apply_strategy({"policy": "greedy"})     # a rebuilt policy is stamped again
    assert granted.policy.simplify_ablated is True


# ------------------------------------------------------------------ the engine

def _crafted(tmp_path, *, metric=1.0, code=CODE, **engine_kw):
    engine = make_engine(tmp_path, policy=GreedyTree(n_seeds=1, max_nodes=12, ablate_every=1,
                                                     enable_merge=False), **engine_kw)
    engine.store.append("run_started", {"run_id": tmp_path.name, "task_id": "toy", "goal": "g",
                                        "direction": "min", **engine._run_start_pinned_values()})
    idea = Idea(operator="draft", params={"x": 0.5, "y": 0.5}, rationale="r")
    engine.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                         "idea": durable_idea_payload(idea), "code": code})
    engine.store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": metric,
                                           "violations": []})
    return engine


def _probed(engine, monkeypatch, *metrics):
    probes = iter([SimpleNamespace(metric=m, exit_code=0 if m is not None else 1,
                                   timed_out=False) for m in metrics])

    async def _probe(source, workdir, parent_id, generation):
        return next(probes), 1.0, True

    monkeypatch.setattr(engine, "_timed_ablation_probe", _probe)
    monkeypatch.setattr(engine, "_build_refine_block_child", lambda *a, **k: None)
    anyio.run(engine._ablate, 0)


def test_the_engine_builds_the_program_the_probe_measured_and_it_takes_the_tie(tmp_path,
                                                                            monkeypatch):
    engine = _crafted(tmp_path / "e", ablate_code_blocks=True, ablation_simplify=True)
    assert engine.policy.simplify_ablated is True, "stamped at launch"
    _probed(engine, monkeypatch, 1.0, 1.5, None)      # block 0 not needed; 1 needed; 2 essential
    state = fold(engine.store.read_all())
    action = engine.policy.next_actions(state)[0]
    assert (action["kind"], action["block"]) == (KIND_SIMPLIFY, 0)
    anyio.run(engine._simplify, action)
    state = fold(engine.store.read_all())
    child = state.nodes[1]
    assert child.operator == "simplify" and child.parent_ids == [0]
    assert child.code == "# [ablated] import os\n\nx = 1\n\nprint(x)\n", "the probe's program"
    assert child.simplified == {"parent_id": 0, "generation": 0, "block": 0,
                                "ablation_id": action["ablation_id"]}
    assert simplify_actions(state, state.nodes[0]) == [], "spent"
    engine.store.append("node_evaluated", {"node_id": 1, "generation": 0, "metric": 1.0,
                                           "violations": []})
    assert fold(engine.store.read_all()).best_node_id == 1, "on a tie, simpler"
    anyio.run(engine._simplify, action)
    assert len(fold(engine.store.read_all()).nodes) == 2, "never built twice"


def test_off_nothing_is_nominated_and_nothing_is_built(tmp_path, monkeypatch):
    engine = _crafted(tmp_path / "off", ablate_code_blocks=True)
    assert engine.policy.simplify_ablated is False
    _probed(engine, monkeypatch, 1.0, 1.5, None)
    state = fold(engine.store.read_all())
    assert engine.policy.next_actions(state)[0]["kind"] != KIND_SIMPLIFY


def test_a_refused_reservation_is_spent_for_the_process_and_said(tmp_path, monkeypatch, caplog):
    """A VERDICT on the proposal spends the nomination at once; a RACE (the world moved under the
    reservation) is re-decided by the next turn, as an operator inject's is — three times, the
    bound this branch carries because it `continue`s above the runaway guard (critic 2026-09-27)."""
    from looplab.engine.ablation import _SIMPLIFY_RACE_RETRIES

    for code, retries in (("card_contract", 0), ("no_slot", _SIMPLIFY_RACE_RETRIES)):
        engine = _crafted(tmp_path / code, ablate_code_blocks=True, ablation_simplify=True)
        _probed(engine, monkeypatch, 1.0, 1.5, None)
        state = fold(engine.store.read_all())
        action = engine.policy.next_actions(state)[0]

        def _refuse(*_a, refusal=None, code=code, **_k):
            refusal.append(code)
            return None

        monkeypatch.setattr(engine, "_reserve_node_build", _refuse)
        for _ in range(retries):
            anyio.run(engine._simplify, action)
            assert engine.policy.simplify_refused == frozenset(), code
            assert engine.policy.next_actions(state)[0]["kind"] == KIND_SIMPLIFY, "retried"
        anyio.run(engine._simplify, action)
        assert len(fold(engine.store.read_all()).nodes) == 1
        assert engine.policy.simplify_refused == frozenset({(0, 0, 0)}), code
        assert f"was not built: no reservation ({code})" in caplog.text
        assert engine.policy.next_actions(state)[0]["kind"] != KIND_SIMPLIFY, "the turn moves on"


def _with_simplification(engine, metric=1.0):
    engine.store.append("node_created", {
        "node_id": 1, "parent_ids": [0], "operator": "simplify",
        "idea": durable_idea_payload(Idea(operator="simplify", params={"x": 0.5, "y": 0.5},
                                          rationale="r")),
        "code": "# [ablated] import os\n\nx = 1\n\nprint(x)\n", "files": {},
        "parent_generations": {"0": 0},
        "simplified": {"parent_id": 0, "generation": 0, "block": 0, "ablation_id": "a" * 32}})
    engine.store.append("node_evaluated", {"node_id": 1, "generation": 0, "metric": metric,
                                           "violations": []})
    return fold(engine.store.read_all())


def test_the_holdout_grades_the_tie_in_an_extra_slot_and_mlebench_its_champion(tmp_path):
    """Top-k holdout slots keep the leader's; the tie gets one more. MLE-bench grades the search
    champion alone — the node the selector would crown, the simplification that holds the tie —
    or it could never win."""
    engine = _crafted(tmp_path / "h", holdout_top_k=1)
    state = _with_simplification(engine)
    assert engine._holdout_topk(state) == [0, 1]
    worse = _crafted(tmp_path / "w", holdout_top_k=1)
    assert worse._holdout_topk(_with_simplification(worse, metric=1.5)) == [0]
    engine._host_grader = {"kind": "mlebench"}
    assert engine._holdout_topk(state) == [1], "one private grade: the champion the tie crowns"
    worse._host_grader = {"kind": "mlebench"}
    assert worse._holdout_topk(fold(worse.store.read_all())) == [0]


def _confirm(engine, monkeypatch, seeds):
    """Drive the real confirm phase with `seeds[node_id]` as each node's confirm seeds (None =
    a failed seed); return which node each seed ran for and the certificate."""
    confirmed = []

    async def _seed(nd, s, objective=None):
        confirmed.append(nd.id)
        values = seeds[nd.id]
        value = values[len([c for c in confirmed if c == nd.id]) - 1]
        engine.store.append("confirm_eval", {"node_id": nd.id, "generation": nd.attempt,
                                             "seed": s, "eval_seconds": 1.0, "metric": value})
        return value

    monkeypatch.setattr(engine, "_run_confirm_seed", _seed)
    anyio.run(engine._confirm_phase, fold(engine.store.read_all()))
    [certificate] = [e.data for e in engine.store.read_all() if e.type == "best_confirmed"]
    return confirmed, certificate


def test_one_confirm_slot_keeps_the_leader_and_the_tie_gets_one_more(tmp_path, monkeypatch):
    """The selector ranks confirmed nodes among confirmed ones, so the simplification is confirmed
    too — in an extra slot, after the leader."""
    engine = _crafted(tmp_path / "c", confirm_top_k=1, confirm_seeds=2)
    _with_simplification(engine)
    confirmed, certificate = _confirm(engine, monkeypatch, {0: [1.0, 1.0], 1: [1.0, 1.0]})
    assert confirmed == [0, 0, 1, 1], confirmed


def test_a_cut_that_fails_every_seed_is_never_certified(tmp_path, monkeypatch):
    """With every seed of the only other slot failed, the certificate keeps the SEARCH's leader —
    it named the cut when the cut stood first (critic 2026-09-27, driven: `CHAMPION = node 1`)."""
    engine = _crafted(tmp_path / "f", confirm_top_k=1, confirm_seeds=2)
    _with_simplification(engine)
    _confirmed, certificate = _confirm(engine, monkeypatch, {0: [None, None], 1: [None, None]})
    assert certificate["node_id"] == 0 and certificate["significant"] is False


def test_the_significance_test_is_against_the_search_leader(tmp_path, monkeypatch):
    """`significant` says whether the confirm winner beat the node the SEARCH ranked first; with
    the cut standing first it was measured against the cut instead (critic 2026-09-27, driven)."""
    engine = _crafted(tmp_path / "g", confirm_top_k=3, confirm_seeds=2)
    _with_simplification(engine)
    engine.store.append("node_created", {
        "node_id": 2, "parent_ids": [], "operator": "draft",
        "idea": durable_idea_payload(Idea(operator="draft", params={"x": 0.1, "y": 0.1},
                                          rationale="r")),
        "code": "print(2)\n", "files": {}})
    engine.store.append("node_evaluated", {"node_id": 2, "generation": 0, "metric": 1.0,
                                           "violations": []})
    # Three nodes tied at 1.0 (min): the leader (node 0), its cut (node 1) and a runner-up (node 2).
    # Confirmed, the runner-up (~0.50) beats the LEADER (~1.01) by far more than one SE — but not
    # the noisy cut (~0.55), which the old order stood first.
    confirmed, certificate = _confirm(engine, monkeypatch, {
        0: [1.0, 1.02], 1: [0.5, 0.6], 2: [0.49, 0.51]})
    assert confirmed[:2] == [0, 0], "the search leader is confirmed first"
    assert certificate["node_id"] == 2 and certificate["significant"] is True, certificate


def test_the_flag_ships_off_resumes_off_and_is_off_at_every_constructor():
    from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
    from looplab.engine.options import EngineOptions

    assert Settings().ablation_simplify is False
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["ablation_simplify"] is False
    legacy = {k: v for k, v in Settings().masked_snapshot().items() if k != "ablation_simplify"}
    assert settings_from_snapshot(legacy).ablation_simplify is False
    assert EngineOptions().ablation_simplify is False
    assert EngineOptions.from_settings(Settings(ablation_simplify=True)).ablation_simplify is True


# ------------------------------------------------------------------ the loop, driven

def test_a_real_run_simplifies_its_champion_and_crowns_the_simplification(tmp_path):
    """The whole path through the ENGINE LOOP, nothing stubbed: the toy Developer's program with an
    unused paragraph in front of it. The code-block ablation probes each block for real, the probe
    without the unused import measures the objective unchanged, the policy nominates it, the
    dispatcher builds the program the probe ran, the eval measures it — and on that exact tie the
    simplification, not the node it was cut from, is the champion."""
    from looplab.agents.toy_roles import ToyObjectiveDeveloper

    class _Padded(ToyObjectiveDeveloper):
        def implement(self, idea):
            return "import math\n\n" + super().implement(idea)

    engine = make_engine(tmp_path / "loop", developer=_Padded(),
                         policy=GreedyTree(n_seeds=1, max_nodes=6, ablate_every=1,
                                           enable_merge=False),
                         ablate_code_blocks=True, ablation_simplify=True)

    async def _bounded():
        with anyio.fail_after(120):
            return await engine.run()

    state = anyio.run(_bounded)
    simplified = [n for n in state.nodes.values() if n.operator == KIND_SIMPLIFY]
    assert simplified, [(n.id, n.operator) for n in state.nodes.values()]
    node = simplified[0]
    parent = state.nodes[node.parent_ids[0]]
    assert node.simplified["block"] == 0 and node.simplified["parent_id"] == parent.id
    assert node.code == "# [ablated] import math\n\n" + parent.code.split("\n\n", 1)[1]
    assert node.metric == parent.metric, "the unused import changed nothing"
    assert state.best_node_id == node.id, "on a tie, simpler: the champion is the simplification"
    # Its own ablation nominated nothing: the block left behind is a comment, and nothing it holds
    # can be removed without breaking the run.
    assert len(simplified) == 1


# ------------------------------------------------------------------ the critic's pass (2026-09-27)

def _abl(block_impacts, *, generation=0, blocks=3, aid="a" * 32):
    return ("ablate", {"parent_id": 0, "generation": generation, "ablation_id": aid,
                       "mode": "code_blocks", "blocks": blocks, "impacts": {},
                       "signed_impacts": block_impacts})


def test_a_simplification_the_card_view_hides_still_spends_its_block(tmp_path):
    """HIGH (driven: 1507 turns in 10 s). The Card lane hands the policy a view without tombstoned,
    gated or discarded nodes; a simplification it hid spent nothing there, `_simplify` re-checked on
    the whole fold, found it spent and returned silently — every turn. The engine now stamps the
    spent set off the WHOLE fold on every selection turn."""
    from looplab.search.card_selection import _effective_policy_state

    engine = _crafted(tmp_path / "e", ablation_simplify=True)
    for kind, data in (_abl({"0": 0.0}),):
        engine.store.append(kind, data)
    _with_simplification(engine)
    engine.store.append("node_tombstoned", {"node_ids": [1]})
    state = fold(engine.store.read_all())
    view = _effective_policy_state(state)
    assert 1 not in view.nodes
    assert [a["block"] for a in simplify_actions(view, view.nodes[0])] == [0], (
        "the view alone would nominate the block again")
    engine._select_actions(state)
    assert engine.policy.simplify_spent == frozenset({(0, 0, 0)}), "stamped off the whole fold"
    assert simplify_actions(view, view.nodes[0], spent=engine.policy.simplify_spent) == []
    assert all(a.get("kind") != KIND_SIMPLIFY for a in engine.policy.next_actions(view))


def test_every_way_simplify_declines_is_a_stamped_refusal(tmp_path, monkeypatch):
    """A nomination `_simplify` will not build — spent, refused, superseded — is refused for the
    process, never returned from silently; and a refused one is never built, whoever asks."""
    engine = _crafted(tmp_path / "r", ablate_code_blocks=True, ablation_simplify=True)
    _probed(engine, monkeypatch, 1.0, 1.5, None)
    action = engine.policy.next_actions(fold(engine.store.read_all()))[0]
    engine._simplify_refused.add((0, 0, 0))
    anyio.run(engine._simplify, action)
    assert len(fold(engine.store.read_all()).nodes) == 1, "a refused nomination is never built"
    engine._simplify_refused.clear()
    engine._stamp_simplify()
    _with_simplification(engine)                          # the block is spent by another build
    anyio.run(engine._simplify, action)
    assert engine.policy.simplify_refused == frozenset({(0, 0, 0)}), "said, not skipped"
    assert len(fold(engine.store.read_all()).nodes) == 2


def test_the_tolerance_is_the_leaders_own_se():
    """HIGH (driven: a cut at 3.0 ± 5 over a leader at 1.0, minimized). SE_diff grows with the CUT's
    noise; the band is capped at the leader's SE, as the verifier's tie band is."""
    from looplab.core.fitness import one_se_non_inferior

    assert not one_se_non_inferior(3.0, 1.0, 5.0, 2, "min", 0.1, 2), "noise buys no band"
    assert one_se_non_inferior(1.05, 1.0, 0.1, 4, "min", 0.1, 4)
    assert not one_se_non_inferior(1.06, 1.0, 0.1, 4, "min", 0.1, 4)
    assert not one_se_non_inferior(1.01, 1.0, 0.5, 4, "min", 0.0, 0), "no leader spread: exact"
    assert one_se_non_inferior(0.95, 1.0, 0.5, 4, "max", 0.1, 4)
    assert not one_se_non_inferior(0.94, 1.0, 0.5, 4, "max", 0.1, 4)


def _confirmed(state, **means):
    for nid, (mean, std, seeds) in means.items():
        node = state.nodes[int(nid[1:])]
        node.confirmed_mean, node.confirmed_std, node.confirmed_seeds = mean, std, seeds
    return state


def test_a_cut_some_pool_node_beats_by_more_than_one_se_is_never_taken(tmp_path):
    """HIGH (driven). The leader was crowned over a better node — by a confirm certificate, or by
    the verifier inside its CI tie band — and a cut non-inferior to the LEADER was still
    significantly worse than that node: the tie rule crossed a boundary no selector rung may."""
    from looplab.events.replay_selection import simpler_tie

    state = _confirmed(_state(tmp_path / "b", (0, [], 1.0, {}), (2, [], 0.9, {}),
                              (1, [0], 1.04, _receipt(0))),
                       n0=(1.0, 0.1, 4), n1=(1.04, 0.1, 4), n2=(0.9, 0.01, 4))
    pool = [state.nodes[0], state.nodes[1], state.nodes[2]]
    assert simpler_tie(state, state.nodes[0], pool) is state.nodes[0]
    state.nodes[2].confirmed_mean = 1.0                  # no longer significantly better
    assert simpler_tie(state, state.nodes[0], pool) is state.nodes[1]


def test_the_holdout_stage_compares_a_cut_whatever_its_confirmation(tmp_path):
    """The one-ruler skip is the SEARCH stage's (a confirmed mean beside one measurement); on the
    holdout stage every leader has one unseen score, confirmed or not."""
    from looplab.events.replay_selection import simpler_tie

    state = _confirmed(_state(tmp_path / "h", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0))),
                       n0=(1.0, 0.1, 4))
    state.nodes[0].holdout_metric = state.nodes[1].holdout_metric = 0.7
    pool = [state.nodes[0], state.nodes[1]]
    assert simpler_tie(state, state.nodes[0], pool, holdout=True) is state.nodes[1]
    assert simpler_tie(state, state.nodes[0], pool) is state.nodes[0], "search stage: two rulers"


def test_among_cuts_of_one_depth_the_better_then_the_lower_id(tmp_path):
    from looplab.events.replay_selection import simpler_tie

    state = _confirmed(_state(tmp_path / "d", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)),
                              (2, [0], 0.99, _receipt(0, block=1))),
                       n0=(1.0, 0.1, 4), n1=(1.0, 0.1, 4), n2=(0.99, 0.1, 4))
    pool = list(state.nodes.values())
    assert simpler_tie(state, state.nodes[0], pool) is state.nodes[2], "the better value"
    state.nodes[2].confirmed_mean = 1.0
    assert simpler_tie(state, state.nodes[0], pool) is state.nodes[1], "then the lower id"


def test_a_cut_outside_the_pool_is_never_crowned(tmp_path):
    """The selector's pool is the eligible population (an infeasible cut is not in it): a cut is
    taken only from the pool it is handed (driven: an infeasible cut became champion)."""
    from looplab.events.replay_selection import simpler_tie

    state = _state(tmp_path / "p", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)))
    assert simpler_tie(state, state.nodes[0], [state.nodes[0]]) is state.nodes[0]
    infeasible = _state(tmp_path / "i", (0, [], 1.0, {}), (1, [0], None, _receipt(0)), extra=[
        ("node_evaluated", {"node_id": 1, "generation": 0, "metric": 1.0,
                            "violations": ["mem > 1GB"]})])
    assert infeasible.best_node_id == 0


def test_the_verifier_needs_its_flag(tmp_path):
    from looplab.events.replay_selection import select_best_node

    state = _state(tmp_path / "v", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)))
    state.nodes[0].verifier_score, state.nodes[1].verifier_score = 0.9, 0.4
    assert state.select_verifier_tiebreak is False
    assert select_best_node(state, [state.nodes[0], state.nodes[1]]).id == 1, "flag off: no veto"


def test_a_receipt_is_the_cut_its_parent_had_or_nothing(tmp_path):
    """The fold held a receipt to its SHAPE: a row with other code took the tie as a
    simplification (driven: `code is the cut: False | champion: 1`). It is now the exact cut —
    the parent's code with that block commented out — and the parent's files, from ONE parent."""
    forged = _state(tmp_path / "f", (0, [], 1.0, {}),
                    (1, [0], 1.0, {**_receipt(0), "code": "print('anything')\n"}))
    assert forged.nodes[1].simplified is None and forged.best_node_id == 0
    files = _state(tmp_path / "x", (0, [], 1.0, {}),
                   (1, [0], 1.0, {**_receipt(0), "files": {"helper.py": "X = 1\n"}}))
    assert files.nodes[1].simplified is None
    out_of_range = _state(tmp_path / "o", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0, block=7)))
    assert out_of_range.nodes[1].simplified is None
    two = _state(tmp_path / "t", (0, [], 1.0, {}), (2, [], 1.0, {}),
                 (1, [0, 2], 1.0, {**_receipt(0), "parent_generations": {"0": 0, "2": 0}}))
    assert two.nodes[1].simplified is None, "exactly one parent"
    good = _state(tmp_path / "g", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)))
    assert good.nodes[1].simplified is not None and good.best_node_id == 1


def test_a_record_that_does_not_describe_the_code_nominates_nothing(tmp_path):
    """A block count the code does not segment into named other code — it nominated every index,
    blocks the program does not have — and a hand-edited key the policy could not read raised out
    of every turn ("²" is `isdigit()`, not `int()`); a huge count took seconds a turn."""
    import time

    mismatch = _ablated(tmp_path / "m", ("a" * 32, "code_blocks", {"0": 0.0, "3": 0.0},
                                         {"blocks": 4}))
    assert simplify_actions(mismatch, mismatch.nodes[0]) == []
    junk = _ablated(tmp_path / "j", ("a" * 32, "code_blocks", {"²": 0.0, "1": 0.0}, {}))
    assert [a["block"] for a in simplify_actions(junk, junk.nodes[0])] == [1]
    huge = _ablated(tmp_path / "h", ("a" * 32, "code_blocks", {"0": 0.0}, {"blocks": 10 ** 7}))
    started = time.monotonic()
    assert simplify_actions(huge, huge.nodes[0]) == []
    assert time.monotonic() - started < 0.5


def test_a_later_pass_that_broke_the_run_withdraws_an_earlier_gain(tmp_path):
    state = _ablated(tmp_path / "w", ("a" * 32, "code_blocks", {"0": 0.1, "1": 0.2}, {}),
                     ("b" * 32, "code_blocks", {"0": None}, {}))
    assert [a["block"] for a in simplify_actions(state, state.nodes[0])] == [1]


def test_a_receipt_spends_its_block_in_its_own_lifecycle_only(tmp_path):
    """The champion was reset and probed again: a cut of its OLD lifecycle spends nothing of the
    new one's."""
    from looplab.events.eventstore import EventStore

    root = tmp_path / "l"
    _state(root, (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)), extra=[_abl({"0": 0.0})])
    store = EventStore(root / "events.jsonl")
    idea = durable_idea_payload(Idea(operator="draft", params={"x": 0.5}, rationale="r"))
    store.append("node_reset", {"node_id": 0, "generation": 0})
    store.append("node_created", {"node_id": 0, "generation": 1, "parent_ids": [],
                                  "operator": "draft", "idea": idea, "code": CODE, "files": {}})
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 1.0,
                                    "violations": []})
    kind, data = _abl({"0": 0.0}, generation=1, aid="b" * 32)
    store.append(kind, data)
    state = fold(store.read_all())
    assert state.nodes[1].simplified["generation"] == 0 and state.nodes[0].attempt == 1
    assert [a["block"] for a in simplify_actions(state, state.nodes[0])] == [0]


def test_a_deleted_or_aborted_champion_nominates_nothing(tmp_path):
    for kind, data in (("node_tombstoned", {"node_ids": [0]}), ("node_abort", {"node_id": 0})):
        state = _ablated(tmp_path / kind, ("a" * 32, "code_blocks", {"0": 0.0}, {}))
        from looplab.events.eventstore import EventStore
        EventStore(tmp_path / kind / "events.jsonl").append(kind, data)
        state = fold(EventStore(tmp_path / kind / "events.jsonl").read_all())
        assert simplify_actions(state, state.nodes[0]) == [], kind


def test_a_block_that_runs_nothing_is_not_nominated():
    """A docstring, a bare constant or a `pass` paragraph: its probe re-ran the program that runs,
    and a cut of it "simplifies" nothing (driven: a module docstring nominated)."""
    from looplab.search.policy import _removes_something

    code = '"""A solution."""\n\nimport os\n\n42\n\npass\n\nx = 1\n\n# note\n'
    assert _removes_something(code, 6) == frozenset({1, 4})
    assert _removes_something("def f(:\n\nx = 1\n\n# c\n", 3) == frozenset({0, 1}), (
        "code that does not parse: the comment rule")
    assert _removes_something(code, 5) == frozenset(), "a record for other code: nothing"


def test_a_host_graded_run_nominates_nothing(tmp_path):
    """Its probe reads the candidate's own stdout, never the host's grade: the self-report minus
    the host grade read as a gain for every block (driven)."""
    state = _ablated(tmp_path / "hg", ("a" * 32, "code_blocks", {"0": 0.0}, {}))
    assert simplify_actions(state, state.nodes[0])
    state.host_grading = {"kind": "mlebench"}
    assert simplify_actions(state, state.nodes[0]) == []


def test_a_due_simplification_is_not_replaced_by_an_unpinned_card():
    """The Card lane let any eligible Card replace it, so it was built only on an empty board
    (driven: one ready Card -> `[('improve', 0, 'card-1')]`)."""
    from looplab.search.card_selection import _protected_due_action

    action = {"kind": KIND_SIMPLIFY, "parent_id": 0, "block": 2}
    assert _protected_due_action([action]) == ("simplify", (0,))
    assert _protected_due_action([action, {"kind": "draft"}]) is None
    assert _protected_due_action([{"kind": KIND_SIMPLIFY, "parent_id": True}]) is None


def test_an_operators_own_pin_applies_the_switch(tmp_path):
    """`set_strategy` records the pin per top-level field (`_pinned: ["operators"]`), the grant
    is looked up per knob — so the operator's own `operators.simplify` was silently not applied."""
    engine = make_engine(tmp_path / "o")
    engine._apply_strategy({"source": "operator", "_pinned": ["operators"],
                            "operators": {"simplify": True}})
    assert engine._ablation_simplify is True and engine.policy.simplify_ablated is True
    strategist = make_engine(tmp_path / "s")
    strategist._apply_strategy({"source": "strategist", "_pinned": ["operators"],
                                "operators": {"simplify": True}})
    assert strategist._ablation_simplify is False, "a merged record keeps each knob's own grant"


def test_the_agent_lane_offers_a_simplification_only_where_ablation_can_run(tmp_path):
    state = _ablated(tmp_path / "a", ("a" * 32, "code_blocks", {"0": 0.0}, {}))
    policy = GreedyTree(n_seeds=1, max_nodes=10)
    policy.simplify_ablated = True
    assert any(a["kind"] == KIND_SIMPLIFY for a in legal_actions(state, policy, max_nodes=10))
    policy.ablation_capable = False
    assert not any(a["kind"] == KIND_SIMPLIFY for a in legal_actions(state, policy, max_nodes=10))


def test_the_pilot_menu_names_each_block_and_recommends_the_policys(monkeypatch):
    from looplab.agents import agent as agent_mod
    from looplab.agents.unified_agent import UnifiedAgent
    from looplab.core.models import RunState

    seen: dict = {}

    def fake_loop(client, tools, messages, emit_spec, **opts):
        seen.update(messages=messages)
        return opts["fallback"](messages)

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    agent = UnifiedAgent(researcher=object(), developer=object(), pilot_client=object())
    legal = [{"kind": KIND_SIMPLIFY, "parent_id": 0, "block": 0},
             {"kind": KIND_SIMPLIFY, "parent_id": 0, "block": 2}]
    agent.choose_action(RunState(goal="g"), legal, dict(legal[1]), brief="b")
    menu = seen["messages"][1]["content"]
    assert "[0] simplify parent=0 block=0" in menu and "[1] simplify parent=0 block=2" in menu
    assert "Policy recommends index 1" in menu


def test_the_simplify_node_is_the_parents_program_files_included(tmp_path, monkeypatch):
    """The node carries the parent's helper files — and the probe that nominated it ran with them,
    as the parent's own eval does (a probe without them crashed on the first import and measured
    every block of a multi-file node "essential")."""
    files = {"helper.py": "VALUE = 1\n"}
    engine = _crafted(tmp_path / "f", ablate_code_blocks=True, ablation_simplify=True)
    kind, data = "node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": durable_idea_payload(Idea(operator="draft", params={"x": 0.5, "y": 0.5},
                                          rationale="r")),
        "code": CODE, "files": files, "generation": 0}
    # re-create node 0 with files on a fresh run
    engine = make_engine(tmp_path / "files", policy=GreedyTree(n_seeds=1, max_nodes=12,
                                                               ablate_every=1, enable_merge=False),
                         ablate_code_blocks=True, ablation_simplify=True)
    engine.store.append("run_started", {"run_id": "files", "task_id": "toy", "goal": "g",
                                        "direction": "min", **engine._run_start_pinned_values()})
    data.pop("generation")
    engine.store.append(kind, data)
    engine.store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 1.0,
                                           "violations": []})
    seen_files = []
    probes = iter([1.0, 1.5, None])

    async def _probe(source, workdir, parent_id, generation):
        from pathlib import Path
        seen_files.append(sorted(p.name for p in Path(workdir).iterdir()))
        m = next(probes)
        return SimpleNamespace(metric=m, exit_code=0 if m is not None else 1,
                               timed_out=False), 1.0, True

    monkeypatch.setattr(engine, "_timed_ablation_probe", _probe)
    monkeypatch.setattr(engine, "_build_refine_block_child", lambda *a, **k: None)
    anyio.run(engine._ablate, 0)
    assert seen_files and all("helper.py" in names for names in seen_files), seen_files
    action = engine.policy.next_actions(fold(engine.store.read_all()))[0]
    anyio.run(engine._simplify, action)
    child = fold(engine.store.read_all()).nodes[1]
    assert child.files == files and child.simplified is not None


def test_a_parent_reset_during_the_build_supersedes_it(tmp_path, monkeypatch):
    engine = _crafted(tmp_path / "s", ablate_code_blocks=True, ablation_simplify=True)
    _probed(engine, monkeypatch, 1.0, 1.5, None)
    action = engine.policy.next_actions(fold(engine.store.read_all()))[0]
    real = engine._reserve_node_build

    def _reserve_then_reset(*args, **kwargs):
        reservation = real(*args, **kwargs)
        engine.store.append("node_reset", {"node_id": 0, "generation": 0,
                                           "from_stage": "eval"})
        return reservation

    monkeypatch.setattr(engine, "_reserve_node_build", _reserve_then_reset)
    anyio.run(engine._simplify, action)
    state = fold(engine.store.read_all())
    built = [n for n in state.nodes.values() if n.operator == "simplify"]
    assert not built or all(n.simplified is None for n in built), "no cut of a moved lifecycle"
    failed = [e.data for e in engine.store.read_all() if e.type == "node_failed"]
    assert failed and failed[-1]["reason"] == "superseded", failed


def test_a_simplify_node_counts_toward_the_card_budget_of_a_fallback_batch():
    """`card_next_actions` bounds a wide policy fallback by the remaining Card budget: a simplify
    creates a node, so it is counted like one."""
    import inspect

    from looplab.search import card_selection

    assert '"simplify"' in inspect.getsource(card_selection.card_next_actions)


def test_a_reset_simplification_is_re_derived_never_rebuilt_by_a_developer(tmp_path, monkeypatch):
    """A propose/implement reset re-derives the same cut and receipt — the Developer rebuild it got
    paid a model call and landed a row without the receipt, so the block was nominated again."""
    engine = _crafted(tmp_path / "rr", ablation_simplify=True)
    state = _with_simplification(engine)
    engine.store.append("node_reset", {"node_id": 1, "generation": 0, "from_stage": "implement"})
    monkeypatch.setattr(engine, "_implement_result",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Developer asked")))
    state = fold(engine.store.read_all())
    engine._rerun_node(state.nodes[1], state)
    after = fold(engine.store.read_all()).nodes[1]
    assert after.attempt == 1 and after.rerun_from is None
    assert after.code == "# [ablated] import os\n\nx = 1\n\nprint(x)\n"
    assert after.simplified == {"parent_id": 0, "generation": 0, "block": 0,
                                "ablation_id": "a" * 32}
    # A cut whose parent lifecycle moved re-derives nothing: it fails, superseded.
    moved = _crafted(tmp_path / "mv", ablation_simplify=True)
    _with_simplification(moved)
    moved.store.append("node_reset", {"node_id": 1, "generation": 0, "from_stage": "implement"})
    moved.store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    state = fold(moved.store.read_all())
    moved._rerun_node(state.nodes[1], state)
    failed = [e.data for e in moved.store.read_all() if e.type == "node_failed"]
    assert failed and failed[-1]["node_id"] == 1 and failed[-1]["reason"] == "superseded"


def test_the_endgame_reserve_passes_a_simplification():
    from looplab.engine.plan import endgame_actions

    state = SimpleNamespace(nodes={i: None for i in range(9)}, best=lambda: None)
    plan = {"endgame_start": 5, "phases": [{"kinds": ["merge", "sweep"]}]}
    action = {"kind": KIND_SIMPLIFY, "parent_id": 0, "block": 1}
    assert endgame_actions(state, plan, [action]) == [action]


def test_a_single_measurement_never_beats_a_confirmed_cut(tmp_path):
    """`_significantly_beaten` holds the cut to nodes on ITS ruler: the slot passes rank a MIXED
    pool, and a single number is never held against a confirmed mean, either way."""
    from looplab.events.replay_selection import simpler_tie

    state = _confirmed(_state(tmp_path / "m", (0, [], 1.0, {}), (2, [], 0.5, {}),
                              (1, [0], 1.0, _receipt(0))),
                       n0=(1.0, 0.1, 4), n1=(1.0, 0.1, 4))
    pool = [state.nodes[0], state.nodes[1], state.nodes[2]]
    assert state.nodes[2].confirmed_mean is None and state.nodes[2].metric == 0.5
    assert simpler_tie(state, state.nodes[0], pool) is state.nodes[1]


def test_the_lifecycle_check_after_the_reservation_names_the_move(tmp_path, monkeypatch):
    engine = _crafted(tmp_path / "n", ablate_code_blocks=True, ablation_simplify=True)
    _probed(engine, monkeypatch, 1.0, 1.5, None)
    action = engine.policy.next_actions(fold(engine.store.read_all()))[0]
    real = engine._reserve_node_build

    def _reserve_then_reset(*args, **kwargs):
        reservation = real(*args, **kwargs)
        engine.store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
        return reservation

    monkeypatch.setattr(engine, "_reserve_node_build", _reserve_then_reset)
    anyio.run(engine._simplify, action)
    [failed] = [e.data for e in engine.store.read_all() if e.type == "node_failed"]
    assert failed["error"] == "parent lifecycle changed while building", failed
    assert not any(e.type == "node_created" and e.data.get("operator") == "simplify"
                   for e in engine.store.read_all()), "nothing was emitted for a moved lifecycle"
