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
    """A log of crafted nodes: `(id, parents, metric, extra)` — `extra` goes into `node_created`."""
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g",
                                 "direction": direction})
    for node_id, parents, metric, extra in nodes:
        operator = extra.pop("operator", "draft" if not parents else "improve")
        store.append("node_created", {
            "node_id": node_id, "parent_ids": list(parents), "operator": operator,
            "idea": durable_idea_payload(Idea(operator=operator, params={"x": 0.5},
                                              rationale="r")),
            "code": CODE, "files": {}, **extra})
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
    from looplab.events.replay_selection import simpler_first

    state = _state(tmp_path / "m", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)))
    state.nodes[0].confirmed_mean, state.nodes[0].confirmed_std = 1.0, 0.1
    state.nodes[0].confirmed_seeds = 4
    ranked = [state.nodes[0], state.nodes[1]]
    assert simpler_first(state, ranked) == ranked
    state.nodes[1].confirmed_mean, state.nodes[1].confirmed_std = 1.0, 0.1
    state.nodes[1].confirmed_seeds = 4
    assert [n.id for n in simpler_first(state, ranked)] == [1, 0]


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


def test_the_extra_measurements_go_to_the_node_the_selector_would_crown(tmp_path):
    """One holdout slot (MLE-bench grades the search champion alone) or one confirm slot: the
    simplification that ties the leader gets it, or it could never win."""
    from looplab.events.replay_selection import simpler_first

    state = _state(tmp_path / "s", (0, [], 1.0, {}), (2, [], 1.5, {}), (1, [0], 1.0, _receipt(0)))
    ranked = [state.nodes[0], state.nodes[1], state.nodes[2]]
    assert [n.id for n in simpler_first(state, ranked)] == [1, 0, 2]
    plain = [state.nodes[2], state.nodes[0]]
    assert simpler_first(state, plain) == plain


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
    engine = _crafted(tmp_path / "r", ablate_code_blocks=True, ablation_simplify=True)
    _probed(engine, monkeypatch, 1.0, 1.5, None)
    state = fold(engine.store.read_all())
    action = engine.policy.next_actions(state)[0]

    def _refuse(*_a, refusal=None, **_k):
        refusal.append("no_slot")
        return None

    monkeypatch.setattr(engine, "_reserve_node_build", _refuse)
    anyio.run(engine._simplify, action)
    assert len(fold(engine.store.read_all()).nodes) == 1
    assert engine.policy.simplify_refused == frozenset({(0, 0, 0)})
    assert "was not built: no reservation (no_slot)" in caplog.text
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


def test_one_holdout_slot_goes_to_the_simplification(tmp_path):
    """MLE-bench grades the search champion alone: ranked by id, the tie's one slot went to the
    parent, the holdout pool was the parent, and the simplification could never win."""
    engine = _crafted(tmp_path / "h", holdout_top_k=1)
    state = _with_simplification(engine)
    assert engine._holdout_topk(state) == [1]
    worse = _crafted(tmp_path / "w", holdout_top_k=1)
    assert worse._holdout_topk(_with_simplification(worse, metric=1.5)) == [0]


def test_one_confirm_slot_goes_to_the_simplification(tmp_path, monkeypatch):
    """The selector ranks confirmed nodes among confirmed ones: an unconfirmed simplification
    could never take a tie from a confirmed parent, so the slot goes to it first."""
    engine = _crafted(tmp_path / "c", confirm_top_k=1, confirm_seeds=2)
    state = _with_simplification(engine)
    confirmed = []

    async def _seed(nd, s, objective=None):
        confirmed.append(nd.id)
        return 1.0

    monkeypatch.setattr(engine, "_run_confirm_seed", _seed)
    anyio.run(engine._confirm_phase, state)
    assert confirmed and set(confirmed) == {1}, confirmed


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
