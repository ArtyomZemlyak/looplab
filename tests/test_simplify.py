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


def _rebuilt(tmp_path, code, files=None):
    """The parent reset and rebuilt with `code` (and `files`), re-measured at the cut's value."""
    return _state(tmp_path, (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)), extra=[
        ("node_reset", {"node_id": 0, "generation": 0}),
        ("node_created", {"node_id": 0, "generation": 1, "parent_ids": [], "operator": "draft",
                          "idea": durable_idea_payload(Idea(operator="draft", params={"x": 0.5},
                                                            rationale="r")),
                          "code": code, "files": dict(files or {})}),
        ("node_evaluated", {"node_id": 0, "generation": 1, "metric": 1.0, "violations": []})])


def test_a_simplification_of_a_replaced_program_is_not_simpler(tmp_path):
    """A rebuild that CHANGED the parent's code: the child is the old program minus a block, not a
    simplification of what stands there now — the block index names another paragraph."""
    state = _rebuilt(tmp_path / "reset", CODE + "\ny = x * 2\n")
    assert state.nodes[0].attempt == 1
    assert state.best_node_id == 0
    moved = _rebuilt(tmp_path / "files", CODE, files={"helper.py": "VALUE = 2\n"})
    assert moved.best_node_id == 0, "the same code over other files is another program"


def test_a_cut_is_of_its_own_parent_its_code_and_its_files():
    """`core/code_blocks.py::still_cut_of` — the rule the selector, the policy's `taken` and the
    engine's spent stamp share: the parent the receipt names, that parent's code minus the block,
    over that parent's files (critic 2026-09-27: its clauses were each deletable under the suite)."""
    from looplab.core.code_blocks import cut_of, still_cut_of

    files = {"helper.py": "VALUE = 1\n"}
    parent = SimpleNamespace(id=0, code=CODE, files=files)
    receipt = {"parent_id": 0, "generation": 0, "block": 0}
    child = SimpleNamespace(code=cut_of(CODE, 0), files=dict(files), simplified=receipt)
    assert still_cut_of(parent, child)
    assert not still_cut_of(SimpleNamespace(id=2, code=CODE, files=files), child), (
        "the same program under another id is not the parent the receipt names")
    assert not still_cut_of(SimpleNamespace(id=0, code=CODE, files={}), child), "its files moved"
    assert not still_cut_of(SimpleNamespace(id=0, code=CODE + "\ny = 2\n", files=files), child)
    assert not still_cut_of(parent, SimpleNamespace(code=child.code, files=dict(files),
                                                    simplified={**receipt, "block": 1}))


def test_a_re_measured_program_keeps_its_simplification(tmp_path):
    """…while a new lifecycle of the SAME program — a reset rebuilt to the same code, or an epoch
    re-queue that re-measures both — keeps the cut a cut of it, so the tie it measured is still its
    (critic 2026-09-27, driven: a re-queue re-measured both and the tie went back to the parent,
    because the receipt's lifecycle was the test)."""
    state = _rebuilt(tmp_path / "same", CODE)
    assert state.nodes[0].attempt == 1 and state.nodes[1].simplified["generation"] == 0
    assert state.best_node_id == 1


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

def _crafted(tmp_path, *, metric=1.0, code=CODE, files=None, eval_seconds=None, **engine_kw):
    engine = make_engine(tmp_path, policy=GreedyTree(n_seeds=1, max_nodes=12, ablate_every=1,
                                                     enable_merge=False), **engine_kw)
    engine.store.append("run_started", {"run_id": tmp_path.name, "task_id": "toy", "goal": "g",
                                        "direction": "min", **engine._run_start_pinned_values()})
    idea = Idea(operator="draft", params={"x": 0.5, "y": 0.5}, rationale="r")
    engine.store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                         "idea": durable_idea_payload(idea), "code": code,
                                         **({"files": dict(files)} if files else {})})
    engine.store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": metric,
                                           "violations": [],
                                           **({"eval_seconds": eval_seconds}
                                              if eval_seconds is not None else {})})
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


def _crafted_at_generation_one(tmp_path, monkeypatch):
    """`_crafted`, its node 0 reset and rebuilt to the same program, re-measured, then ablated at
    generation 1 (block 0 not needed; 1 needed; 2 essential)."""
    engine = _crafted(tmp_path, ablate_code_blocks=True, ablation_simplify=True)
    idea = durable_idea_payload(Idea(operator="draft", params={"x": 0.5, "y": 0.5}, rationale="r"))
    engine.store.append("node_reset", {"node_id": 0, "generation": 0})
    engine.store.append("node_created", {"node_id": 0, "generation": 1, "parent_ids": [],
                                         "operator": "draft", "idea": idea, "code": CODE})
    engine.store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 1.0,
                                           "violations": []})
    _probed(engine, monkeypatch, 1.0, 1.5, None)
    return engine


def test_every_simplify_key_names_the_lifecycle_it_was_decided_on(tmp_path, monkeypatch):
    """The receipt, the spent stamp and the refusal name the parent's CURRENT lifecycle — each
    written as generation 0 passed every test, all of which ran at generation 0 (critic 2026-09-27,
    MB7/MB8/MB10) — and a cut of a program the parent no longer is spends nothing of the new one."""
    engine = _crafted_at_generation_one(tmp_path / "g1", monkeypatch)
    state = fold(engine.store.read_all())
    action = engine.policy.next_actions(state)[0]
    assert state.nodes[0].attempt == 1 and (action["kind"], action["block"]) == (KIND_SIMPLIFY, 0)
    anyio.run(engine._simplify, action)
    state = fold(engine.store.read_all())
    assert state.nodes[1].simplified["generation"] == 1, "the lifecycle the cut was taken from"
    engine._stamp_simplify(state)
    assert engine.policy.simplify_spent == frozenset({(0, 1, 0)})
    idea = durable_idea_payload(Idea(operator="draft", params={"x": 0.5, "y": 0.5}, rationale="r"))
    engine.store.append("node_reset", {"node_id": 0, "generation": 1})
    engine.store.append("node_created", {"node_id": 0, "generation": 2, "parent_ids": [],
                                         "operator": "draft", "idea": idea,
                                         "code": CODE + "\ny = x * 2\n"})
    engine._stamp_simplify(fold(engine.store.read_all()))
    assert engine.policy.simplify_spent == frozenset(), "a cut of the old program spends nothing"

    refusing = _crafted_at_generation_one(tmp_path / "r1", monkeypatch)
    action = refusing.policy.next_actions(fold(refusing.store.read_all()))[0]

    def _refuse(*_a, refusal=None, **_k):
        refusal.append("card_contract")
        return None

    monkeypatch.setattr(refusing, "_reserve_node_build", _refuse)
    anyio.run(refusing._simplify, action)
    assert refusing.policy.simplify_refused == frozenset({(0, 1, 0)})


def test_race_retries_are_per_block_and_restart_once_the_run_moves(tmp_path, monkeypatch):
    """The race bound is a nomination's — one block's races spend nothing of another block's
    (mutant M04) — and it is counted at ONE node count: races separated by a node landing are
    progress, not a spinning loop, so they start the count over (critic 2026-09-27, NIT)."""
    from looplab.engine.ablation import _SIMPLIFY_RACE_RETRIES

    engine = _crafted(tmp_path / "e", ablate_code_blocks=True, ablation_simplify=True)
    _probed(engine, monkeypatch, 1.0, 1.0, None)          # blocks 0 AND 1 measured no worse
    state = fold(engine.store.read_all())
    action, other = simplify_actions(state, state.nodes[0])
    assert (action["block"], other["block"]) == (0, 1)

    def _race(*_a, refusal=None, **_k):
        refusal.append("no_slot")
        return None

    monkeypatch.setattr(engine, "_reserve_node_build", _race)
    for _ in range(_SIMPLIFY_RACE_RETRIES):
        anyio.run(engine._simplify, other)
    assert engine._simplify_races[(0, 0, 1)][0] == _SIMPLIFY_RACE_RETRIES
    assert (0, 0, 0) not in engine._simplify_races, "another block's races"
    for _ in range(_SIMPLIFY_RACE_RETRIES - 1):
        anyio.run(engine._simplify, action)
    engine.store.append("node_created", {
        "node_id": 1, "parent_ids": [], "operator": "draft", "code": "print(1)\n", "files": {},
        "idea": durable_idea_payload(Idea(operator="draft", params={"x": 0.5}, rationale="r"))})
    anyio.run(engine._simplify, action)
    assert engine._simplify_races[(0, 0, action["block"])] == (1, 2), "the count starts over"
    assert (0, 0, action["block"]) not in engine.policy.simplify_refused


def _with_simplification(engine, metric=1.0, *, files=None, eval_seconds=None):
    engine.store.append("node_created", {
        "node_id": 1, "parent_ids": [0], "operator": "simplify",
        "idea": durable_idea_payload(Idea(operator="simplify", params={"x": 0.5, "y": 0.5},
                                          rationale="r")),
        "code": "# [ablated] import os\n\nx = 1\n\nprint(x)\n", "files": dict(files or {}),
        "parent_generations": {"0": 0},
        "simplified": {"parent_id": 0, "generation": 0, "block": 0, "ablation_id": "a" * 32}})
    engine.store.append("node_evaluated", {"node_id": 1, "generation": 0, "metric": metric,
                                           "violations": [],
                                           **({"eval_seconds": eval_seconds}
                                              if eval_seconds is not None else {})})
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


@pytest.mark.parametrize("leader_seed, champion", [
    (1.0, 1),        # the confirmations tie: on a tie, simpler
    (0.9, 0),        # the leader confirms better than it measured: its robust value moved
])
def test_a_re_entered_confirm_pass_keeps_the_cuts_extra_slot(tmp_path, monkeypatch, leader_seed,
                                                            champion):
    """MEDIUM (critic 2026-09-27, driven): the extra slot was decided on the robust ruler, which the
    pass itself moves — once the leader was confirmed, a re-entry saw "two rulers" and the cut lost
    its slot for good: one retryable refusal on the cut's first seed crowned node 0 where the
    uninterrupted pass crowned node 1. The slot is decided on the SINGLE measurements, so a leader
    whose confirmations moved its robust value leaves the cut its slot too; who is crowned is the
    selector's, over both confirmations."""
    from looplab.engine import confirm_phase

    engine = _crafted(tmp_path / "re", confirm_top_k=1, confirm_seeds=2)
    _with_simplification(engine)
    refused: list = []

    async def _no_pace():
        return None

    async def _seed(nd, s, objective=None):
        if nd.id == 1 and not refused:
            refused.append(s)
            return confirm_phase._CONFIRM_RETRYABLE
        value = leader_seed if nd.id == 0 else 1.0
        engine.store.append("confirm_eval", {"node_id": nd.id, "generation": nd.attempt,
                                             "seed": s, "eval_seconds": 1.0, "metric": value})
        return value

    monkeypatch.setattr(engine, "_pace_confirm_refusal", _no_pace)
    monkeypatch.setattr(engine, "_run_confirm_seed", _seed)
    anyio.run(engine._confirm_phase, fold(engine.store.read_all()))
    mid = fold(engine.store.read_all())
    assert refused and not mid.confirmed_done, "the refusal leaves the pass open"
    assert mid.nodes[0].confirmed_mean is not None, "the leader was confirmed before it"
    anyio.run(engine._confirm_phase, mid)
    state = fold(engine.store.read_all())
    assert state.nodes[1].confirmed_mean is not None, "the re-entry confirmed the cut"
    assert state.best_node_id == champion


def test_the_cuts_extra_slot_is_never_paid_past_the_budget(tmp_path, monkeypatch):
    """LOW (critic 2026-09-27, driven): the extra slot sat inside the always-confirmed prefix, so
    with `confirm_top_k=1` the cut's seeds ran on a spent budget (400 eval-seconds against 200). The
    prefix is the pass's own top-k: the leader is confirmed whatever the budget, the cut when it
    allows."""
    engine = _crafted(tmp_path / "b", confirm_top_k=1, confirm_seeds=2, max_eval_seconds=1.0,
                      eval_seconds=100.0)
    _with_simplification(engine, eval_seconds=100.0)
    confirmed, certificate = _confirm(engine, monkeypatch, {0: [1.0, 1.0], 1: [1.0, 1.0]})
    assert confirmed == [0, 0], confirmed
    assert certificate["node_id"] == 0


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


def test_an_epoch_re_queue_keeps_the_tie_with_the_simpler_node(tmp_path):
    """The critic's own case (2026-09-27, driven): a disclosure consumed by a reopen re-queues every
    incumbent — parent and cut alike, the same programs — and each is re-measured on the new split
    at the value it had. The receipt still names generation 0 while both now stand at 1; the tie is
    still the cut's."""
    from looplab.events.eventstore import EventStore

    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min",
                                 "holdout_fraction": 0.25})
    _log_nodes = _state(tmp_path / "shape", (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)))
    assert _log_nodes.best_node_id == 1
    for event in EventStore(tmp_path / "shape" / "events.jsonl").read_all()[1:]:
        store.append(event.type, dict(event.data))
    store.append("holdout_evaluated", {"node_id": 1, "generation": 0, "metric": 1.0, "gap": 0.0,
                                       "n_holdout": 3, "search_epoch": 0})
    store.append("run_finished", {"reason": "done"})
    store.append("resume", {})
    requeued = fold(store.read_all())
    assert {n.id: (n.status.value, n.attempt) for n in requeued.nodes.values()} == {
        0: ("pending", 1), 1: ("pending", 1)}
    for nid in (0, 1):
        store.append("node_evaluated", {"node_id": nid, "generation": 1, "metric": 1.0,
                                        "violations": []})
    state = fold(store.read_all())
    assert state.nodes[1].simplified["generation"] == 0
    assert state.best_node_id == 1


def test_a_receipt_spends_its_block_of_its_own_program_only(tmp_path):
    """The champion was reset, rebuilt and probed again: a cut of its OLD program spends nothing of
    a CHANGED one's — and still spends the block of the same program re-measured, where a second
    cut would be the node that already exists."""
    from looplab.events.eventstore import EventStore

    for label, code, nominated in (("changed", CODE.replace("x = 1", "x = 2"), [0]),
                                   ("same", CODE, [])):
        root = tmp_path / label
        _state(root, (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)), extra=[_abl({"0": 0.0})])
        store = EventStore(root / "events.jsonl")
        idea = durable_idea_payload(Idea(operator="draft", params={"x": 0.5}, rationale="r"))
        store.append("node_reset", {"node_id": 0, "generation": 0})
        store.append("node_created", {"node_id": 0, "generation": 1, "parent_ids": [],
                                      "operator": "draft", "idea": idea, "code": code,
                                      "files": {}})
        store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 1.0,
                                        "violations": []})
        kind, data = _abl({"0": 0.0}, generation=1, aid="b" * 32)
        store.append(kind, data)
        state = fold(store.read_all())
        assert state.nodes[1].simplified["generation"] == 0 and state.nodes[0].attempt == 1
        assert [a["block"] for a in simplify_actions(state, state.nodes[0])] == nominated, label


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


def test_a_program_too_deep_to_dump_never_breaks_a_policy_turn(tmp_path):
    """MEDIUM (critic 2026-09-27, driven): `ast.dump` recurses and only the parse was contained — a
    parseable champion with a ~300-branch elif chain raised RecursionError out of every policy turn,
    and again on every resume. Such a program falls back to the comment rule."""
    from looplab.core.code_blocks import code_blocks
    from looplab.search.policy import _removes_something

    deep = ("v = 3\n\nif v == 0:\n    y = 0\n"
            + "".join(f"elif v == {i}:\n    y = {i}\n" for i in range(1, 400)) + "\nprint(y)\n")
    blocks = len(code_blocks(deep))
    assert _removes_something(deep, blocks) == frozenset(range(blocks)), "the comment rule"
    state = _state(tmp_path / "d", (0, (), 1.0, {"code": deep}), extra=[("ablate", {
        "parent_id": 0, "generation": 0, "ablation_id": "a" * 32, "mode": "code_blocks",
        "blocks": blocks, "impacts": {}, "signed_impacts": {"0": 0.0}})])
    policy = GreedyTree(n_seeds=1, max_nodes=8)
    policy.simplify_ablated = True
    assert [(a["kind"], a.get("block")) for a in policy.next_actions(state)] == [
        (KIND_SIMPLIFY, 0)]


def test_one_walk_decides_which_blocks_pay_a_dump(monkeypatch):
    """LOW (critic 2026-09-27, driven): a parse and a dump per block cost 13.7 s on the event-loop
    thread for a 2,001-line, 401-block champion. One walk decides which blocks COULD run nothing and
    only those are dumped — with the answer a dump per block gives."""
    from looplab.core.code_blocks import code_blocks
    from looplab.search import policy as policy_mod

    def per_block(code):                     # the rule as it was: every block pays a dump
        lines, whole, out = code.splitlines(), policy_mod._semantic_dump(code), set()
        for index, (start, end) in enumerate(code_blocks(code)):
            if not any(lines[k].strip() and not lines[k].strip().startswith("#")
                       for k in range(start, end)):
                continue
            if whole is not None and policy_mod._semantic_dump(
                    "\n".join(lines[:start] + lines[end:])) == whole:
                continue
            out.add(index)
        return frozenset(out)

    for code in (
        '"""Doc."""\n\nimport os\n\n# c\n\nx = 1\n\npass\n\ndef f():\n    """d"""\n'
        '    return 1\n\n"stray"; y = 2\n\nprint(x)\n',
        'def g():\n    pass\n\n    """d"""\n\n    return 2\n\n...\n',
        's = """\na\n\nb\n\nc\n"""\n\nprint(s)\n',
        "x = (1 +\n\n2)\n\nprint(x)\n",
        "def broken(:\n\nx = 1\n",
        # a form feed is a line to `splitlines`, not to the tokenizer: the numberings part, and the
        # docstring after it read as code under the walk's line numbers
        'import os\n\x0c\n"""doc"""\n\nx = 1\n',
    ):
        assert policy_mod._removes_something(code, len(code_blocks(code))) == per_block(code), code
    dumps: list = []
    real = policy_mod._semantic_dump
    monkeypatch.setattr(policy_mod, "_semantic_dump", lambda code: dumps.append(1) or real(code))
    big = "\n".join(f"def f{i}(x):\n    return x * {i}\n" for i in range(400)) + '\n"""d"""\n'
    policy_mod._removes_something.cache_clear()
    removed = policy_mod._removes_something(big, len(code_blocks(big)))
    assert removed == frozenset(range(400)), "every def runs something; the stray string does not"
    assert len(dumps) == 2, "the program, and the one block that could run nothing"


def test_the_walk_spares_the_dump_for_multi_line_statements_and_pays_it_where_a_string_is_cut(
        monkeypatch):
    """The dump count on shapes the first count test did not hold (critic 2026-09-27: a mutant of
    the walk survived it): a statement that starts on a block's SECOND code line and spans lines,
    three hundred times over, costs no dump — and a docstring broken by a blank line, whose token
    each of its two blocks cuts, costs one each."""
    from looplab.core.code_blocks import code_blocks
    from looplab.search import policy as policy_mod

    body = "".join(f'"note {i}"\nx{i} = ({i} +\n      1)\n\n' for i in range(300))
    # …and a string that spans lines INSIDE its one block cuts no edge: no dump for it either.
    big = '"""Title.\n\nBody."""\n\n' + body + 's = """one\ntwo"""\n'
    dumps: list = []
    real = policy_mod._semantic_dump
    monkeypatch.setattr(policy_mod, "_semantic_dump", lambda code: dumps.append(1) or real(code))
    policy_mod._removes_something.cache_clear()
    removed = policy_mod._removes_something(big, len(code_blocks(big)))
    assert removed == frozenset(range(303)), "every block's removal changes what runs or parses"
    assert len(dumps) == 3, "the program, and the two blocks the docstring's token spans"


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


def test_a_due_simplification_keeps_its_turn_through_the_card_lane():
    """The lane's call site, not only the helper (critic 2026-09-27, MB1: a call site that dropped
    the key survived every test): one ready unpinned Card and a due simplification — the turn is
    the simplification; an operator's pin is the override band it always was."""
    from test_card_driven_selection import _node, _ready_card

    from looplab.core.models import RunState
    from looplab.search.card_selection import card_next_actions

    due = {"kind": KIND_SIMPLIFY, "parent_id": 0, "block": 2}

    class _Policy:
        n_seeds = 1
        card_select_k = 1

        def next_actions(self, _state):
            return [dict(due)]

        def card_score(self, _state, _card, *, scoring):
            return 0, (1.0,)

    def board(pinned):
        return RunState(direction="max", nodes={0: _node(0, metric=0.9)}, best_node_id=0,
                        cards={"card-1": _ready_card("card-1", parents=(0,), pinned=pinned)})

    assert card_next_actions(board(False), _Policy(), 10) == [due]
    [pinned] = card_next_actions(board(True), _Policy(), 10)
    assert (pinned["kind"], pinned["_card_id"]) == ("improve", "card-1")


def test_operators_is_never_an_operator_pin_and_the_knob_is_its_grants(tmp_path):
    """LOW (critic 2026-09-27, driven): an exemption for an operator-pinned `operators` dict was
    dead code — `set_strategy` refuses the field and the pin reader keeps only its own seven — while
    the settings row said "an operator's own pin applies". The exemption is gone, the row says what
    holds: `operators.simplify` is its grant's, whoever wrote the record."""
    from fastapi import HTTPException

    from looplab.serve.control_validation import normalize_control

    engine = make_engine(tmp_path / "o")
    engine._apply_strategy({"source": "operator", "_pinned": ["operators"],
                            "operators": {"simplify": True}})
    assert engine._ablation_simplify is False, "no role holds the grant by default"
    with pytest.raises(HTTPException) as refused:
        normalize_control(None, tmp_path, "set_strategy",
                          {"strategy": {"operators": {"simplify": True}}})
    assert refused.value.status_code == 400 and "operators" in str(refused.value.detail)


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


def test_a_simplify_node_counts_toward_the_card_budget_of_a_fallback_batch(tmp_path):
    """`card_next_actions` bounds a wide policy fallback by the remaining Card budget: a simplify
    creates a node, so it is counted like one. Driven through the lane (the pin this replaces read
    the function's source for the literal, which a comment satisfied — critic 2026-09-27, MB2)."""
    from looplab.search.card_selection import card_budget_used, card_next_actions

    state = _state(tmp_path / "b", (0, (), 1.0, {}))
    wide = [{"kind": KIND_SIMPLIFY, "parent_id": 0, "block": block} for block in range(3)]
    policy = SimpleNamespace(n_seeds=1, next_actions=lambda _state: [dict(a) for a in wide])
    left = 2
    actions = card_next_actions(state, policy, card_budget_used(state) + left)
    assert actions == wide[:left], "one node per slot left, never the whole batch"


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
    # A parent re-measured on the SAME program still gives that cut: re-derived, naming the
    # parent's current lifecycle — it failed `superseded` on the receipt's generation (critic
    # 2026-09-27, NIT).
    same = _crafted(tmp_path / "same", ablation_simplify=True)
    _with_simplification(same)
    same.store.append("node_reset", {"node_id": 1, "generation": 0, "from_stage": "implement"})
    same.store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    state = fold(same.store.read_all())
    same._rerun_node(state.nodes[1], state)
    after = fold(same.store.read_all()).nodes[1]
    assert [e for e in same.store.read_all() if e.type == "node_failed"] == []
    assert after.code == "# [ablated] import os\n\nx = 1\n\nprint(x)\n"
    assert after.simplified["generation"] == 1 and after.parent_generations == {"0": 1}
    # A parent whose program CHANGED, or that a reset emptied, gives another cut or none: nothing
    # is re-derived, it fails, superseded.
    idea = durable_idea_payload(Idea(operator="draft", params={"x": 0.5, "y": 0.5}, rationale="r"))
    for label, rebuilt in (("changed", CODE.replace("x = 1", "x = 2")), ("emptied", None)):
        moved = _crafted(tmp_path / label, ablation_simplify=True)
        _with_simplification(moved)
        moved.store.append("node_reset", {"node_id": 1, "generation": 0,
                                          "from_stage": "implement"})
        moved.store.append("node_reset", {"node_id": 0, "generation": 0,
                                          "from_stage": "implement"})
        if rebuilt is not None:
            moved.store.append("node_created", {"node_id": 0, "generation": 1, "parent_ids": [],
                                                "operator": "draft", "idea": idea,
                                                "code": rebuilt})
        state = fold(moved.store.read_all())
        moved._rerun_node(state.nodes[1], state)
        failed = [e.data for e in moved.store.read_all() if e.type == "node_failed"]
        assert failed and failed[-1]["node_id"] == 1 and failed[-1]["reason"] == "superseded", label


def test_a_re_derived_simplification_carries_its_parents_files(tmp_path):
    """The cut is the parent's program — its helper files too (critic 2026-09-27, M40: a re-derivation
    that dropped them survived, the tests' parents having none)."""
    files = {"helper.py": "VALUE = 1\n"}
    engine = _crafted(tmp_path / "rf", ablation_simplify=True, files=files)
    _with_simplification(engine, files=files)
    engine.store.append("node_reset", {"node_id": 1, "generation": 0, "from_stage": "implement"})
    state = fold(engine.store.read_all())
    engine._rerun_node(state.nodes[1], state)
    after = fold(engine.store.read_all()).nodes[1]
    assert after.attempt == 1 and after.files == files and after.simplified is not None


def test_the_endgame_reserve_takes_one_simplification_per_champion_after_the_ensemble(tmp_path):
    """The reserve passes a simplification of the champion — it pays no model and polishes the
    champion — but not ahead of the once-only ensemble, and once per champion: five nominated
    blocks of a champion each cut measured worse took a reserve of three, and the ensemble and the
    sweeps never ran (critic 2026-09-27, driven)."""
    from looplab.engine.plan import endgame_actions

    action = {"kind": KIND_SIMPLIFY, "parent_id": 0, "block": 1}
    sweeps = {"endgame_start": 0, "phases": [{"kinds": ["sweep"]}]}
    ensemble = {"endgame_start": 0, "phases": [{"kinds": ["merge", "sweep"]}]}
    state = _state(tmp_path / "r", (0, [], 1.0, {}), (1, [], 2.0, {}))
    assert endgame_actions(state, sweeps, [action]) == [action]
    assert [a["kind"] for a in endgame_actions(state, ensemble, [action])] == ["merge"], (
        "the once-only ensemble goes first")
    cut = _state(tmp_path / "c", (0, [], 1.0, {}), (1, [0], 1.1, _receipt(0)))
    assert cut.best_node_id == 0, "the cut measured worse"
    assert [a["kind"] for a in endgame_actions(cut, sweeps, [action])] == ["improve"], (
        "a second cut of the same champion yields the slot to the sweeps")


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


# ------------------------------------------------------------------ the critic's second pass (2026-09-27)

def test_the_significance_guard_is_the_one_se_rule_itself_never_the_capped_band(tmp_path):
    """`_significantly_beaten` asks the run's >1-SE rule (`one_se_better`, the uncapped difference
    spread) — NOT the non-inferiority band, capped at one node's SE (mutant M10): a node 0.03 ahead
    of the cut, within SE_diff 0.051 but beyond its own SE 0.01, does not beat it."""
    from looplab.events.replay_selection import simpler_tie

    state = _confirmed(_state(tmp_path / "s", (0, [], 1.0, {}), (2, [], 1.03, {}),
                              (1, [0], 1.0, _receipt(0)), direction="max"),
                       n0=(1.0, 0.1, 4), n1=(1.0, 0.1, 4), n2=(1.03, 0.02, 4))
    pool = [state.nodes[0], state.nodes[1], state.nodes[2]]
    assert simpler_tie(state, state.nodes[0], pool) is state.nodes[1]
    beaten = _confirmed(_state(tmp_path / "b", (0, [], 1.0, {}), (2, [], 1.055, {}),
                               (1, [0], 1.0, _receipt(0)), direction="max"),
                        n0=(1.0, 0.1, 4), n1=(1.0, 0.1, 4), n2=(1.055, 0.02, 4))
    pool = [beaten.nodes[0], beaten.nodes[1], beaten.nodes[2]]
    assert simpler_tie(beaten, beaten.nodes[0], pool) is beaten.nodes[0], (
        "0.055 ahead is beyond SE_diff 0.051 — on the two nodes' own spreads, never widened")


def test_one_number_each_is_compared_exactly_whatever_is_confirmed(tmp_path):
    """On a single-number ruler — the holdout's, and the raw one the confirm pass's slot is decided
    on — the confirmation spread is never used, even where both nodes carry one (mutant M37)."""
    from looplab.events.replay_selection import _significantly_beaten

    state = _confirmed(_state(tmp_path / "e", (0, [], 1.01, {}), (1, [], 1.0, {}),
                              direction="max"),
                       n0=(1.01, 0.3, 4), n1=(1.0, 0.3, 4))
    cut, other = state.nodes[1], state.nodes[0]
    assert _significantly_beaten(state, cut, [other, cut], lambda n: n.metric, True)
    assert not _significantly_beaten(state, cut, [other, cut], lambda n: n.robust_metric, False)


def _lifecycle_one(tmp_path):
    """Node 0 reset and rebuilt to the same program, re-measured, and probed again at generation 1."""
    from looplab.events.eventstore import EventStore

    root = tmp_path
    _state(root, (0, [], 1.0, {}))
    store = EventStore(root / "events.jsonl")
    idea = durable_idea_payload(Idea(operator="draft", params={"x": 0.5}, rationale="r"))
    store.append("node_reset", {"node_id": 0, "generation": 0})
    store.append("node_created", {"node_id": 0, "generation": 1, "parent_ids": [],
                                  "operator": "draft", "idea": idea, "code": CODE, "files": {}})
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 1.0,
                                    "violations": []})
    kind, data = _abl({"0": 0.0}, generation=1, aid="b" * 32)
    store.append(kind, data)
    return fold(store.read_all())


def test_spent_and_refused_are_keyed_on_the_lifecycle_they_name(tmp_path):
    """A stamp of generation 0 spends and refuses nothing of generation 1 (mutants MB7/MB8); the
    same block of the CURRENT lifecycle is spent / refused."""
    state = _lifecycle_one(tmp_path)
    node = state.nodes[0]
    assert node.attempt == 1
    assert [a["block"] for a in simplify_actions(state, node)] == [0]
    assert [a["block"] for a in simplify_actions(state, node, spent={(0, 0, 0)})] == [0]
    assert [a["block"] for a in simplify_actions(state, node, refused={(0, 0, 0)})] == [0]
    assert simplify_actions(state, node, spent={(0, 1, 0)}) == []
    assert simplify_actions(state, node, refused={(0, 1, 0)}) == []


def test_the_pilots_decision_record_names_the_block_it_chose(tmp_path, monkeypatch):
    """Two simplifications of one champion are two actions; the `agent_decision` record names the
    BLOCK so the durable record can say which was chosen (mutant M31)."""
    engine = _crafted(tmp_path / "a", ablate_code_blocks=True, ablation_simplify=True)
    _probed(engine, monkeypatch, 1.0, 1.0, None)
    state = fold(engine.store.read_all())

    class _Pilot:
        def choose_action(self, state, legal, recommended, brief=""):
            index = next(i for i, a in enumerate(legal)
                         if a.get("kind") == KIND_SIMPLIFY and a.get("block") == 1)
            return {"index": index, "rationale": "the second block"}

    engine.researcher = _Pilot()
    chosen = engine._agent_next_actions(state)
    assert chosen and chosen[0]["kind"] == KIND_SIMPLIFY and chosen[0]["block"] == 1, chosen
    [decision] = [e.data for e in engine.store.read_all() if e.type == "agent_decision"]
    assert decision["chosen"]["kind"] == KIND_SIMPLIFY and decision["chosen"]["block"] == 1


# ------------------------------------------------------------------ a repaired cut (critic 2026-09-27)

_REPAIRED = "# [ablated] import os\n\nx = 1\n\nprint(x)\nimport sys\n"


def _repaired(node_id=1, **extra):
    return [("node_repaired", {"node_id": node_id, "generation": 0, "attempt": 1, **extra}),
            ("node_evaluated", {"node_id": node_id, "generation": 0, "metric": 1.2,
                                "violations": []})]


def test_a_repaired_cut_still_spends_its_block(tmp_path):
    """HIGH (critic 2026-09-27, driven). The inline repair (`node_repaired`, the default crash path)
    rewrites a pending cut in place, so it is no longer its parent minus the block — and with
    `still_cut_of` as the whole `taken` rule the block was free again: the next turn nominated it
    and the engine built the program that had just crashed, once per repair while the parent stayed
    champion. The cut the fold certified at the build (`Node.simplified_cut`) keeps it spent."""
    from looplab.core.code_blocks import built_as_cut_of, still_cut_of

    state = _state(tmp_path, (0, [], 1.0, {}), (1, [0], None, _receipt(0)),
                   extra=[_abl({"0": 0.0}), *_repaired(code=_REPAIRED)])
    parent, cut = state.nodes[0], state.nodes[1]
    assert cut.code == _REPAIRED and cut.simplified is not None
    assert not still_cut_of(parent, cut) and built_as_cut_of(parent, cut)
    assert simplify_actions(state, parent) == []
    policy = GreedyTree(n_seeds=1, max_nodes=10)
    policy.simplify_ablated = True
    assert KIND_SIMPLIFY not in [a["kind"] for a in policy.next_actions(state)]


def test_a_repaired_cut_is_never_built_again_by_the_engine(tmp_path, monkeypatch):
    """The engine half: the spent stamp keeps the block (the Card lane's view reads it) and
    `_simplify` re-checks on the whole fold, so no second node with the crashed program lands."""
    engine = _crafted(tmp_path / "e", ablate_code_blocks=True, ablation_simplify=True)
    _probed(engine, monkeypatch, 1.0, 1.5, None)
    state = fold(engine.store.read_all())
    action = engine.policy.next_actions(state)[0]
    assert (action["kind"], action["block"]) == (KIND_SIMPLIFY, 0)
    anyio.run(engine._simplify, action)
    first_cut = fold(engine.store.read_all()).nodes[1].code
    for etype, data in _repaired(code=first_cut + "import sys\n"):
        engine.store.append(etype, data)
    state = fold(engine.store.read_all())
    engine._stamp_simplify(state)
    assert engine.policy.simplify_spent == frozenset({(0, 0, 0)})
    assert simplify_actions(state, state.nodes[0]) == []
    anyio.run(engine._simplify, action)
    assert sorted(fold(engine.store.read_all()).nodes) == [0, 1]


def test_a_repair_that_converged_on_the_new_programs_cut_is_that_cut(tmp_path):
    """The other half of `cut_spent`: the parent was re-developed and its new program minus the
    block IS what the repaired cut became (the same crash, the same fix, in both) — the tree
    already has that program, though the cut was BUILT as another."""
    from looplab.core.code_blocks import built_as_cut_of, still_cut_of
    from looplab.events.eventstore import EventStore

    _state(tmp_path, (0, [], 1.0, {}), (1, [0], None, _receipt(0)), extra=_repaired(code=_REPAIRED))
    store = EventStore(tmp_path / "events.jsonl")
    idea = durable_idea_payload(Idea(operator="draft", params={"x": 0.5}, rationale="r"))
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "implement"})
    store.append("node_created", {"node_id": 0, "generation": 1, "parent_ids": [],
                                  "operator": "draft", "idea": idea,
                                  "code": CODE + "import sys\n", "files": {}})
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 1.0,
                                    "violations": []})
    kind, data = _abl({"0": 0.0}, generation=1, aid="b" * 32)
    store.append(kind, data)
    state = fold(store.read_all())
    parent, cut = state.nodes[0], state.nodes[1]
    assert still_cut_of(parent, cut) and not built_as_cut_of(parent, cut)
    assert simplify_actions(state, parent) == []


def test_a_repaired_cut_of_another_program_spends_nothing_of_this_one(tmp_path):
    """What the cut was built as is compared to the cut THIS program gives: a parent re-developed
    to another program, one with other files, or another node with the same code is not the
    program the repaired cut was cut from."""
    from looplab.events.eventstore import EventStore

    idea = durable_idea_payload(Idea(operator="draft", params={"x": 0.5}, rationale="r"))
    for label, code, files in (("code", CODE.replace("x = 1", "x = 2"), {}),
                               ("files", CODE, {"helper.py": "VALUE = 2\n"})):
        root = tmp_path / label
        _state(root, (0, [], 1.0, {}), (1, [0], None, _receipt(0)),
               extra=_repaired(code=_REPAIRED))
        store = EventStore(root / "events.jsonl")
        store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "implement"})
        store.append("node_created", {"node_id": 0, "generation": 1, "parent_ids": [],
                                      "operator": "draft", "idea": idea, "code": code,
                                      "files": files})
        store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 1.0,
                                        "violations": []})
        kind, data = _abl({"0": 0.0}, generation=1, aid="b" * 32)
        store.append(kind, data)
        state = fold(store.read_all())
        assert [a["block"] for a in simplify_actions(state, state.nodes[0])] == [0], label
    state = _state(tmp_path / "twin", (0, [], 1.0, {}), (1, [0], None, _receipt(0)),
                   (2, [], 1.0, {}),
                   extra=[*_repaired(code=_REPAIRED),
                          ("ablate", {"parent_id": 2, "generation": 0, "ablation_id": "c" * 32,
                                      "mode": "code_blocks", "blocks": 3, "impacts": {},
                                      "signed_impacts": {"0": 0.0}})])
    assert state.nodes[2].code == state.nodes[0].code
    assert [a["block"] for a in simplify_actions(state, state.nodes[2])] == [0]


def test_a_repair_of_the_cuts_files_alone_still_spends_its_block(tmp_path):
    """A repair can rewrite only the helper files: the cut's code is still the parent's minus the
    block, its files are not the parent's, and it was built as that cut all the same."""
    from looplab.core.code_blocks import built_as_cut_of, still_cut_of

    files = {"helper.py": "VALUE = 1\n"}
    state = _state(tmp_path, (0, [], 1.0, {"files": files}),
                   (1, [0], None, {**_receipt(0), "files": dict(files)}),
                   extra=[_abl({"0": 0.0}), *_repaired(files={"helper.py": "VALUE = 3\n"})])
    parent, cut = state.nodes[0], state.nodes[1]
    assert cut.files == {"helper.py": "VALUE = 3\n"} and cut.code == _cut_of_parent(parent)
    assert not still_cut_of(parent, cut) and built_as_cut_of(parent, cut)
    assert simplify_actions(state, parent) == []


def _cut_of_parent(parent, block=0):
    from looplab.core.code_blocks import cut_of
    return cut_of(parent.code, block)


def test_the_certified_cut_is_fold_internal_and_hashes_any_text(tmp_path):
    """`Node.simplified_cut` is the fold's record, not a public field: a node's dump — every DTO
    and snapshot — does not move. And its hash is total: a lone surrogate hashes."""
    from looplab.core.code_blocks import cut_identity

    state = _state(tmp_path, (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)), (2, [0], 1.0, {}))
    assert state.nodes[1].simplified_cut == cut_identity(state.nodes[1].code, {})
    assert state.nodes[2].simplified_cut is None
    assert "simplified_cut" not in state.nodes[1].model_dump()
    assert len(cut_identity("x = '\ud800'\n", {"a.py": "\udfff"})) == 64


# ------------------------------------------------------------------ the critic's pass over fe811509

def _exact_rule(code):
    """`_removes_something` as the per-block rule states it — one dump per block, no fast path."""
    from looplab.core.code_blocks import code_blocks
    from looplab.search.policy import _semantic_dump

    spans, lines, whole = code_blocks(code), code.splitlines(), _semantic_dump(code)
    out = set()
    for index, (start, end) in enumerate(spans):
        if not any(ln.strip() and not ln.strip().startswith("#") for ln in lines[start:end]):
            continue
        if (whole is not None
                and _semantic_dump("\n".join(lines[:start] + lines[end:])) == whole):
            continue
        out.add(index)
    return frozenset(out)


_BREAKS = "\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"


@pytest.mark.parametrize("label, code, runs_nothing", [
    ("an else over a pass", "x = int('1')\nif x:\n    y = 2\n\nelse:\n    pass\n\nprint(y)\n", 1),
    ("a finally over a pass", "try:\n    a = 1\nexcept ValueError:\n    a = 2\n\n"
                              "finally:\n    pass\n\nprint(a)\n", 1),
    ("a for-else over a docstring", "for i in range(3):\n    z = i\n\nelse:\n    'nothing'\n", 1),
    ("a three-line module docstring", '"""Solution.\nTrains a model.\n"""\n\nimport os\n\n'
                                      'x = 1\n\nprint(x)\n', 0),
    ("a string in a finally body", "import os\n\ntry:\n    x = 1\nfinally:\n    y = 2\n\n"
                                   "    'nothing runs here'\n\nprint(x)\n", 2),
    *[(f"a docstring holding {ch!r}", f'"""a{ch}b"""\n\n"""doc"""\n\nx = 1\n', 1)
      for ch in _BREAKS],
    # A FRAGMENT of a statement that runs something — no statement starts in the block (critic
    # 2026-09-27, driven: the two kinds the line-coverage gate still nominated).
    ("a clause header split by a backslash", "x = int('1')\nif x:\n    y = 2\n\nelse\\\n:\n"
                                             "    pass\n\nprint(y)\n", 1),
    ("an empty string inside a concatenation", "s = ('a'\n\n     ''\n\n     )\n\nprint(s)\n", 1),
    ("an else header with a space", "x = int('1')\nif x:\n    y = 2\n\nelse :\n    pass\n\nprint(y)\n", 1),
    ("an else header with a comment", "x = int('1')\nif x:\n    y = 2\n\nelse:  # note\n    pass\n"
                                      "\nprint(y)\n", 1),
    # A block whose edge CUTS a token that spans lines: the `'''` it opens swallows a copy of its
    # own statement, which is code again once the block is gone (critic 2026-09-27, driven).
    ("a string its block opens swallows a copy of its statement",
     "x = 1\n'''\n\nx = 1  # '''\nprint(x)\n", 0),    # …and the same with the string's two line breaks a bare CR each, which `splitlines` and `ast`
    # break on and a default `readline` does not (critic 2026-09-27, driven).
    ("a CR-broken string its block opens swallows a copy of its statement",
     "x = 1\n'''\r\rx = 1  # '''\nprint(x)\n", 0),
])
def test_the_fast_path_is_the_per_block_rule(label, code, runs_nothing):
    """LOW (critic 2026-09-27, driven: 335 of 40,000 random programs). An `else:`/`finally:` header
    is a line no statement covers, so the fast path read a paragraph `else:\\n    pass` as changing
    what runs — and nominated a cut that ran the same program. The fast path is only allowed to
    SKIP a dump the per-block rule would not change the answer of: a multi-line docstring's every
    line, a `finally` body, and every line break only `splitlines` honours (the numberings part)."""
    from looplab.core.code_blocks import code_blocks
    from looplab.search.policy import _removes_something

    answer = _removes_something.__wrapped__(code, len(code_blocks(code)))
    assert answer == _exact_rule(code), label
    assert runs_nothing not in answer, f"{label}: block {runs_nothing} runs nothing"


def test_the_raw_slot_hears_the_verifier_on_the_raw_evidence_on_every_entry(tmp_path):
    """Two critic findings on one rule (2026-09-27, both driven). First: `simpler_tie(raw=True)`
    consulted the CURRENT verifier scores, and the leader's own `node_confirmed` clears its score,
    so a re-entered pass granted the cut the slot its first entry denied. Then the fix skipped the
    verifier on the raw ruler, and a cut it vetoed got the slot and — both confirmed, both scores
    cleared, the verifier re-scoring only exact ties — the crown. The raw ruler now asks the scores
    the verifier gave the RAW evidence, which no confirmation clears: the veto holds on every entry,
    and the selector hears it after both are confirmed."""
    from looplab.core.code_blocks import code_blocks, comment_block
    from looplab.core.fitness import verifier_evidence_digest
    from looplab.events.eventstore import EventStore
    from looplab.events.replay_selection import simpler_slots, simpler_tie

    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min",
                                 "select_verifier": True})
    idea = durable_idea_payload(Idea(operator="draft", params={"x": 0.5}, rationale="r"))
    for nid, code in ((0, CODE), (1, "print(1)\n")):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": idea, "code": code, "files": {}})
    store.append("node_created", {
        "node_id": 2, "parent_ids": [0], "operator": "simplify", "idea": idea,
        "code": comment_block(CODE, code_blocks(CODE)[0]), "files": {},
        "parent_generations": {"0": 0},
        "simplified": {"parent_id": 0, "generation": 0, "block": 0, "ablation_id": "a" * 32}})
    for nid in (0, 1, 2):
        store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": 1.0,
                                        "violations": []})
    state = fold(store.read_all())
    for nid, score in ((0, 0.9), (2, 0.2)):
        store.append("node_verified", {"node_id": nid, "generation": 0, "score": score,
                                       "evidence_digest": verifier_evidence_digest(
                                           "min", state.nodes[nid])})

    def slots(st):
        ranked = sorted(st.breedable_nodes(), key=lambda n: (n.metric, n.id))
        return [n.id for n in simpler_slots(st, ranked, 2, raw=True)]

    first = fold(store.read_all())
    assert first.nodes[0].verifier_score == 0.9 and slots(first) == [0, 1]
    store.append("node_confirmed", {"node_id": 0, "generation": 0, "mean": 1.0, "std": 0.01,
                                    "seeds": 2})
    again = fold(store.read_all())
    assert again.nodes[0].verifier_score is None and slots(again) == [0, 1], (
        "the re-entered pass reads the veto its first entry read")
    # …and a cut confirmed by any route is still not crowned over the verifier's veto.
    store.append("node_confirmed", {"node_id": 2, "generation": 0, "mean": 1.0, "std": 0.01,
                                    "seeds": 2})
    both = fold(store.read_all())
    assert both.nodes[2].verifier_score is None
    assert simpler_tie(both, both.nodes[0], [both.nodes[0], both.nodes[2]]) is both.nodes[0]
    # A NEW lifecycle is new raw evidence: the old raw score no longer speaks for it.
    store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 1.0, "violations": []})
    reset = fold(store.read_all())
    assert reset.nodes[0].attempt == 1 and slots(reset) == [0, 1, 2]


def test_without_a_verifier_the_raw_slot_is_the_same_on_every_entry(tmp_path):
    """The first finding's other half: with no verifier, the leader's confirmation must not cost
    the cut the slot either ("two rulers" on the robust ruler) — one refused seed would change
    which node is confirmed and crowned."""
    from looplab.events.replay_selection import simpler_slots

    state = _state(tmp_path, (0, [], 1.0, {}), (1, [0], 1.0, _receipt(0)))
    ranked = [state.nodes[0], state.nodes[1]]
    assert [n.id for n in simpler_slots(state, ranked, 1, raw=True)] == [0, 1]
    confirmed = _confirmed(state, n0=(1.0, 0.01, 2))
    assert [n.id for n in simpler_slots(confirmed, ranked, 1, raw=True)] == [0, 1]
    assert [n.id for n in simpler_slots(confirmed, ranked, 1)] == [0], (
        "precondition: on the robust ruler the confirmed leader and the cut are two rulers")


def test_each_ruler_hears_the_verifier_on_its_own_evidence(tmp_path):
    """The current scores decide when both exist, else the raw ones — and the raw ruler hears the
    raw evidence ONLY. Driven through the group record (the live producer's row): the pair is
    scored on its single measurements, then confirmed, then the confirmed tie re-scored the other
    way round. The robust ruler follows the re-score; the confirm pass's ruler keeps the raw veto."""
    from looplab.core.code_blocks import code_blocks, comment_block
    from looplab.core.fitness import VERIFIER_SELECTION_CONTRACT, verifier_evidence_digest
    from looplab.events.eventstore import EventStore
    from looplab.events.replay_selection import simpler_tie

    def group(st, scores):
        return {"v": 1, "contract": VERIFIER_SELECTION_CONTRACT,
                "requested_samples": st.select_verifier_samples,
                "members": [{"node_id": nid, "generation": st.nodes[nid].attempt, "score": score,
                             "n_samples": st.select_verifier_samples, "agreement": 1.0,
                             "method": "llm",
                             "evidence_digest": verifier_evidence_digest(st.direction,
                                                                         st.nodes[nid])}
                            for nid, score in scores]}

    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min",
                                 "select_verifier": True})
    idea = durable_idea_payload(Idea(operator="draft", params={"x": 0.5}, rationale="r"))
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": idea, "code": CODE, "files": {}})
    store.append("node_created", {
        "node_id": 1, "parent_ids": [0], "operator": "simplify", "idea": idea,
        "code": comment_block(CODE, code_blocks(CODE)[0]), "files": {},
        "parent_generations": {"0": 0},
        "simplified": {"parent_id": 0, "generation": 0, "block": 0, "ablation_id": "a" * 32}})
    for nid in (0, 1):
        store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": 1.0,
                                        "violations": []})
    store.append("verifier_group_scored", group(fold(store.read_all()), ((0, 0.9), (1, 0.2))))
    raw = fold(store.read_all())
    assert (raw.nodes[0].verifier_score, raw.nodes[1].verifier_score) == (0.9, 0.2)
    assert simpler_tie(raw, raw.nodes[0], list(raw.nodes.values()), raw=True) is raw.nodes[0]
    for nid in (0, 1):
        store.append("node_confirmed", {"node_id": nid, "generation": 0, "mean": 1.0,
                                        "std": 0.01, "seeds": 2})
    store.append("verifier_group_scored", group(fold(store.read_all()), ((0, 0.2), (1, 0.9))))
    st = fold(store.read_all())
    assert (st.nodes[0].verifier_score, st.nodes[1].verifier_score) == (0.2, 0.9)
    pool = list(st.nodes.values())
    assert simpler_tie(st, st.nodes[0], pool) is st.nodes[1], "the robust ruler reads the re-score"
    assert simpler_tie(st, st.nodes[0], pool, raw=True) is st.nodes[0], (
        "the raw ruler reads the raw evidence, whatever a confirmed re-score said")


def test_the_holdout_slots_decide_the_tie_on_the_robust_ruler(tmp_path):
    """`simpler_slots` passes its ruler through (critic 2026-09-27, N4): the holdout slots rank on
    the confirmations, where the cut ties, while one measurement each says it lost."""
    from looplab.events.replay_selection import simpler_slots

    state = _confirmed(_state(tmp_path, (0, [], 1.0, {}), (1, [0], 1.05, _receipt(0))),
                       n0=(1.0, 0.1, 4), n1=(1.02, 0.1, 4))
    ranked = [state.nodes[0], state.nodes[1]]
    assert [n.id for n in simpler_slots(state, ranked, 1)] == [0, 1]
    assert [n.id for n in simpler_slots(state, ranked, 1, raw=True)] == [0]


def test_the_reserves_one_cut_counts_from_its_start_and_of_its_champion(tmp_path):
    """The once-per-champion rule counts a cut of THE champion made IN the reserve — its first slot
    included — and nothing else (critic 2026-09-27, N5/N6/N17)."""
    from looplab.engine.plan import endgame_actions

    action = {"kind": KIND_SIMPLIFY, "parent_id": 0, "block": 1}
    plan = {"endgame_start": 2, "phases": [{"kinds": ["sweep"]}]}
    at_start = _state(tmp_path / "s", (0, [], 1.0, {}), (1, [], 2.0, {}),
                      (2, [0], 1.1, _receipt(0)))
    assert [a["kind"] for a in endgame_actions(at_start, plan, [action])] == ["improve"]
    before = _state(tmp_path / "b", (0, [], 1.0, {}), (1, [0], 1.1, _receipt(0)),
                    (2, [], 2.0, {}))
    assert before.best_node_id == 0 and endgame_actions(before, plan, [action]) == [action]
    other = _state(tmp_path / "o", (0, [], 1.0, {}), (1, [], 2.0, {}),
                   (2, [1], 2.1, _receipt(1)))
    assert other.best_node_id == 0 and endgame_actions(other, plan, [action]) == [action]


@pytest.mark.parametrize("phases", [[1], {"x": 1}, [{"kinds": 5}], "merge"])
def test_a_malformed_plan_never_costs_a_card_its_slot(tmp_path, phases):
    """The reserve's kinds are read before the kept-Card return now (the simplify rule needs them),
    so they are read totally: a last phase that is not a dict raised where a Card kept its slot
    (critic 2026-09-27, NIT)."""
    from looplab.engine.plan import endgame_actions
    from looplab.search.card_selection import META_CARD_ID

    state = _state(tmp_path, (0, [], 1.0, {}), (1, [], 2.0, {}))
    card = {"kind": "improve", "parent_id": 0, META_CARD_ID: "c"}
    assert endgame_actions(state, {"endgame_start": 0, "phases": phases}, [card]) == [card]


def test_an_empty_kinds_list_is_the_default_reserve(tmp_path):
    """`kinds: []` names no reserve and reads as the default one — the ensemble first — as a missing
    or malformed `kinds` does; an empty list is not "no endgame kinds" (critic 2026-09-27, L1)."""
    from looplab.engine.plan import endgame_actions

    state = _state(tmp_path, (0, [], 1.0, {}), (1, [], 2.0, {}))
    action = {"kind": "improve", "parent_id": 0}
    for phases in ([{"kinds": []}], [{"kinds": ["merge"]}], None):
        out = endgame_actions(state, {"endgame_start": 0, "phases": phases}, [action])
        assert [a["kind"] for a in out] == ["merge"], phases
    assert [a["kind"] for a in endgame_actions(
        state, {"endgame_start": 0, "phases": [{"kinds": ["sweep"]}]}, [action])] == ["improve"], (
        "precondition: a reserve without the ensemble does not merge")


def test_the_spent_stamp_names_the_parents_lifecycle_now(tmp_path):
    """The Card lane's view hides the cut, so the stamp is all that spends its block — keyed on the
    parent's CURRENT lifecycle: the parent was re-measured on the same program (attempt 0 → 1) and
    re-ablated, and a stamp keyed on the receipt's generation spent nothing of it (critic
    2026-09-27, N7)."""
    engine = _crafted(tmp_path / "n7", ablation_simplify=True)
    _with_simplification(engine)
    engine.store.append("node_reset", {"node_id": 0, "generation": 0, "from_stage": "eval"})
    engine.store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": 1.0,
                                           "violations": []})
    kind, data = _abl({"0": 0.0}, generation=1, aid="b" * 32)
    engine.store.append(kind, data)
    state = fold(engine.store.read_all())
    engine._stamp_simplify(state)
    assert engine.policy.simplify_spent == frozenset({(0, 1, 0)})
    view = state.model_copy(update={"nodes": {k: v for k, v in state.nodes.items() if k != 1}})
    assert simplify_actions(view, view.nodes[0]) != [], "precondition: hidden, it spends nothing"
    assert simplify_actions(view, view.nodes[0], spent=engine.policy.simplify_spent) == []


def test_a_spent_budget_still_confirms_the_top_two(tmp_path, monkeypatch):
    """The confirm pass's floor, on the line the extra slot's budget rule edited: on a SPENT eval
    budget it still confirms the top TWO of its own top-k — enough for a demotion decision (critic
    2026-09-27, N15)."""
    engine = make_engine(tmp_path / "c", policy=GreedyTree(n_seeds=1, max_nodes=12),
                         confirm_top_k=3, confirm_seeds=2, max_eval_seconds=1.0)
    engine.store.append("run_started", {"run_id": "c", "task_id": "toy", "goal": "g",
                                        "direction": "min", **engine._run_start_pinned_values()})
    for nid, metric in ((0, 1.0), (1, 1.1), (2, 1.2)):
        engine.store.append("node_created", {
            "node_id": nid, "parent_ids": [], "operator": "draft",
            "idea": durable_idea_payload(Idea(operator="draft", params={"x": 0.1 * nid},
                                              rationale="r")),
            "code": f"print({nid})\n"})
        engine.store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": metric,
                                               "violations": [], "eval_seconds": 100.0})
    ran: list = []

    async def _seed(nd, s, objective=None):
        ran.append(nd.id)
        engine.store.append("confirm_eval", {"node_id": nd.id, "generation": nd.attempt,
                                             "seed": s, "eval_seconds": 1.0,
                                             "metric": 1.0 + nd.id})
        return 1.0 + nd.id

    monkeypatch.setattr(engine, "_run_confirm_seed", _seed)
    anyio.run(engine._confirm_phase, fold(engine.store.read_all()))
    assert sorted(set(ran)) == [0, 1], ran
