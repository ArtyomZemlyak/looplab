"""The operator's nodes are not the engine's search (doc 69 §6.2, item 69.25).

`minionerec-backbones-v10`: the endgame plan was cut once, on 24.09, and the operator's inject batches
never re-cut it — `PLAN_REASONS` had no reason to. Of nodes 0-17, twelve were the operator's (0-3, and
the batch 9-16 that carried the node count past the reserve start) and six the engine's, so the
engine's one node after the operator's "main axis is the BACKBONE" directive was the reserve's
ensemble, node 17, on the budget's last slot.

Under `Settings.endgame_inject_recut` the reserve is `endgame_reserve_frac` of `max_nodes` LESS the
nodes an operator inject created (`engine/plan.py::operator_injected`), and a batch that moves that
cut's start writes an `injected` plan row (`engine/plan.py::replan`). Driven here: the pure rule, the
counter over a real log, the engine's `_ensure_plan`, and a run whose operator batch lands mid-run —
with the setting off the engine's next node is decided inside the reserve, with it on outside.
"""
from __future__ import annotations

import itertools

import anyio
import pytest

from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings, settings_from_snapshot
from looplab.engine.options import EngineOptions
from looplab.engine.plan import (
    HARD_STALL_RUNGS, PLAN_REASONS, build_plan, in_endgame, operator_injected, replan)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_INJECT_NODE, EV_PAUSE, EV_PLAN, EV_RESUME
from tests.factories import make_engine


def _plan(**kw):
    base = {"max_nodes": 20, "n_seeds": 2, "reserve_frac": 0.25, "at_node": 0}
    return build_plan(**{**base, **kw})


def _replan(plan, **kw):
    base = {"max_nodes": 20, "n_seeds": 2, "reserve_frac": 0.25, "stall_rung": 0, "champion": 0}
    return replan(plan, **{**base, **kw})


# ------------------------------------------------------------------------------------ the rule
def test_the_reserve_is_cut_from_the_engine_s_share():
    """MUTATIONS: cut over `max_nodes` (ignore `injected`); always write the key -> red."""
    plain = _plan()
    assert (plain["reserve"], plain["endgame_start"]) == (5, 15) and "injected" not in plain
    assert _plan(injected=0) == plain, "no injected node: the historical row byte for byte"
    assert _plan(injected=-3) == plain, "a negative count is no count"
    cut = _plan(injected=4)
    assert (cut["reserve"], cut["endgame_start"], cut["injected"]) == (4, 16, 4)
    assert cut["phases"][-1]["nodes"] == 4 and cut["phases"][1]["nodes"] == 14
    # Every slot the operator's: the reserve keeps its one slot at the end of the budget.
    assert _plan(injected=20)["endgame_start"] == 19
    assert _plan(injected=99)["endgame_start"] == 19


def test_the_endgame_never_starts_earlier_than_without_the_batch():
    for max_nodes, frac, injected in itertools.product(
            (4, 7, 12, 19, 31, 100), (0.05, 0.2, 0.25, 0.5, 0.9), range(0, 40, 3)):
        base = build_plan(max_nodes=max_nodes, n_seeds=2, reserve_frac=frac, at_node=0)
        cut = build_plan(max_nodes=max_nodes, n_seeds=2, reserve_frac=frac, at_node=0,
                         injected=injected)
        if base is None:
            continue
        assert cut["endgame_start"] >= base["endgame_start"], (max_nodes, frac, injected)


@pytest.mark.parametrize("stall_nodes", [0, 3], ids=["permanent-stall", "bounded-stall"])
def test_a_batch_that_moves_the_cut_writes_one_injected_row(stall_nodes):
    """MUTATIONS: drop the re-cut; re-cut whatever the count; write a row that does not move the
    start -> red."""
    plan = _plan()
    row = _replan(plan, at_node=10, injected=4, stall_nodes=stall_nodes)
    assert row["reason"] == "injected" and "injected" in PLAN_REASONS
    assert (row["endgame_start"], row["reserve"], row["injected"], row["at_node"]) == (16, 4, 4, 10)
    assert _replan(row, at_node=11, injected=4, stall_nodes=stall_nodes) is None, "once per batch"
    # A count that does not move the start writes nothing (and the next turn asks again, for free).
    assert _replan(plan, at_node=10, injected=1, stall_nodes=stall_nodes) is None
    # A later batch re-cuts again, from the row that recorded the first.
    again = _replan(row, at_node=14, injected=8, stall_nodes=stall_nodes)
    assert (again["reason"], again["endgame_start"], again["injected"]) == ("injected", 17, 8)


def test_only_a_moved_count_re_cuts():
    """The `injected` row answers a BATCH: a cut the formula would now put elsewhere for another
    reason (here a reserve fraction the row was not cut with) is not re-cut as `injected` while the
    operator's count is the one the row recorded (MUTATION: drop the count comparison)."""
    for recorded in (0, 4):
        row = _plan(injected=recorded)
        for stall_nodes in (0, 3):
            assert _replan(row, reserve_frac=0.5, at_node=6, injected=recorded,
                           stall_nodes=stall_nodes) is None, (recorded, stall_nodes)


def test_the_incident_s_shape():
    """v10's shape: nineteen slots, the operator's twelve among the first eighteen. Without the
    re-cut node 17 is decided inside the reserve; with it, outside, and the reserve keeps the last
    slot."""
    plan = build_plan(max_nodes=19, n_seeds=3, reserve_frac=0.2, at_node=0)
    assert plan["endgame_start"] == 15 and in_endgame(plan, 17)
    row = replan(plan, max_nodes=19, n_seeds=3, reserve_frac=0.2, at_node=17, stall_rung=0,
                 stall_nodes=3, champion=6, injected=12)
    assert (row["reason"], row["endgame_start"]) == ("injected", 18)
    assert not in_endgame(row, 17) and in_endgame(row, 18)


def test_a_budget_change_cuts_over_the_engine_s_share_too():
    plan = _plan(injected=4)
    for stall_nodes in (0, 3):
        grown = _replan(plan, max_nodes=32, at_node=18, injected=10, stall_nodes=stall_nodes)
        assert grown["reason"] == "budget_changed"
        assert (grown["endgame_start"], grown["injected"]) == (32 - round(22 * 0.25), 10)


def test_a_stall_row_is_not_re_cut_by_a_batch():
    # `stall_nodes` 0: the permanent stall endgame stands.
    plan = _plan()
    stalled = _replan(plan, at_node=8, stall_rung=HARD_STALL_RUNGS, stall_nodes=0, injected=2)
    assert stalled["reason"] == "stagnation" and stalled["endgame_start"] == 8
    assert stalled["injected"] == 2, "the row carries the count it was taken over"
    assert _replan(stalled, at_node=12, injected=6, stall_nodes=0) is None
    # `stall_nodes` 3: a live episode closes by its own terms …
    episode = _replan(plan, at_node=8, stall_rung=HARD_STALL_RUNGS, stall_nodes=3)
    assert (episode["endgame_start"], episode["endgame_end"]) == (8, 11)
    # … a batch landing inside it EXTENDS it by the batch: the episode is K of the ENGINE's nodes
    # (critic 2026-09-30, crit_v48 F3 — the operator's ids used to spend it) …
    extended = _replan(episode, at_node=9, injected=2, stall_nodes=3, stall_rung=3)
    assert (extended["reason"], extended["endgame_start"], extended["endgame_end"]) == (
        "injected", 8, 13)
    assert extended["injected"] == 2 and extended["stall_champions"] == [0]
    assert _replan(extended, at_node=12, injected=2, stall_nodes=3, stall_rung=3) is None
    # … and its `reopened` row cuts over the engine's share.
    reopened = _replan(extended, at_node=13, injected=2, stall_nodes=3, stall_rung=3)
    assert reopened["reason"] == "reopened" and reopened["reopen_cause"] == "episode_spent"
    assert (reopened["endgame_start"], reopened["injected"]) == (16, 2)
    assert reopened["stall_champions"] == [0]
    # The injected re-cut of a row after an episode keeps the spent-champion memory.
    moved = _replan(reopened, at_node=14, injected=10, stall_nodes=3, stall_rung=3)
    assert moved["reason"] == "injected" and moved["stall_champions"] == [0]


def test_a_stall_started_with_the_count_recorded_is_not_re_cut_after():
    plan = _plan()
    episode = _replan(plan, at_node=8, stall_rung=HARD_STALL_RUNGS, stall_nodes=3, injected=3)
    assert episode["reason"] == "stagnation" and episode["injected"] == 3
    reopened = _replan(episode, at_node=11, stall_nodes=3, stall_rung=3, injected=3)
    assert _replan(reopened, at_node=12, stall_nodes=3, stall_rung=3, injected=3) is None


def test_turning_it_off_restores_the_historical_cut():
    row = _replan(_plan(), at_node=10, injected=4, stall_nodes=3)
    off = _replan(row, at_node=11, injected=0, stall_nodes=3)
    assert off["reason"] == "injected" and off["endgame_start"] == 15 and "injected" not in off


def test_with_no_injected_node_every_rule_is_the_historical_one():
    """The rows `tests/test_endgame_admission.py` pins, asked again with `injected=0`."""
    plan = build_plan(max_nodes=100, n_seeds=3, reserve_frac=0.2, at_node=0)
    cases = [
        dict(at_node=12, stall_rung=2, champion=5, stall_nodes=3),
        dict(at_node=12, stall_rung=1, champion=5, stall_nodes=3),
        dict(at_node=12, stall_rung=2, champion=5, stall_nodes=0),
        dict(at_node=12, stall_rung=0, champion=5, stall_nodes=0, max_nodes=99),
        dict(at_node=12, stall_rung=0, champion=5, stall_nodes=3, max_nodes=99),
    ]
    for case in cases:
        kw = {"max_nodes": 100, "n_seeds": 3, "reserve_frac": 0.2, **case}
        assert replan(plan, **kw, injected=0) == replan(plan, **kw), case


# ------------------------------------------------------------------------------------ the count
def test_the_count_is_the_operator_s_node_ids_the_fold_holds(tmp_path):
    """MUTATIONS: count rows not ids; count ids the fold does not hold; drop the source test."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "toy", "direction": "min"})

    def created(node_id, **extra):
        store.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {"x": 0.0}},
                                      "code": "c", **extra})

    created(0)
    created(1, source="manual")
    created(2, source="manual")
    created(3)
    store.append("node_reset", {"node_id": 1, "mode": "implement"})
    created(1, generation=1)                   # the reset rebuild of the operator's node
    store.append("node_reset", {"node_id": 2, "mode": "implement"})
    created(2, generation=1, source="manual")  # …a rebuild that keeps its stamp: still ONE id
    created("9", source="manual")               # the fold keys it as node 9, and so does the count
    created([7], source="manual")               # no usable id: the fold drops it, the count too
    events = store.read_all()
    state = fold(events)
    assert set(state.nodes) == {0, 1, 2, 3, 9}
    assert operator_injected(events, state.nodes) == 3
    assert operator_injected(events, {0: None, 3: None}) == 0, "ids the fold does not hold"


# ------------------------------------------------------------------------------------ the switch
def test_on_for_new_runs_off_for_a_pre_field_snapshot_and_in_the_bare_library(tmp_path):
    assert Settings().endgame_inject_recut is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["endgame_inject_recut"] is False
    legacy = Settings().masked_snapshot()
    legacy.pop("endgame_inject_recut")
    assert settings_from_snapshot(legacy).endgame_inject_recut is False
    assert EngineOptions().endgame_inject_recut is False
    assert EngineOptions.from_settings(Settings()).endgame_inject_recut is True
    assert make_engine(tmp_path / "run")._endgame_inject_recut is False


def _seeded_engine(tmp_path, *, recut: bool):
    """A plan-carrying engine over a log whose first four nodes are the operator's."""
    eng = make_engine(tmp_path, n_seeds=2, max_nodes=20, endgame_reserve_frac=0.25,
                      endgame_inject_recut=recut)
    eng.store.append("run_started", {"run_id": "r", "task_id": "toy", "direction": "min"})
    for node_id in range(6):
        eng.store.append("node_created", {
            "node_id": node_id, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {"x": float(node_id)}}, "code": "c",
            **({"source": "manual"} if node_id < 4 else {})})
    return eng


@pytest.mark.parametrize("recut", [True, False], ids=["on", "off"])
def test_the_engine_counts_the_operator_s_nodes_into_its_plan(tmp_path, recut):
    """Through `Engine._ensure_plan`, the one writer: the first row is cut over the engine's share
    with the setting on, over the whole budget with it off (MUTATION: never pass the count)."""
    eng = _seeded_engine(tmp_path / "run", recut=recut)
    assert eng._ensure_plan(fold(eng.store.read_all())) is True
    (row,) = [e.data for e in eng.store.read_all() if e.type == EV_PLAN]
    if recut:
        assert (row["reason"], row["endgame_start"], row["injected"]) == ("initial", 16, 4)
    else:
        assert row == build_plan(max_nodes=20, n_seeds=2, reserve_frac=0.25, at_node=6)
    assert eng._ensure_plan(fold(eng.store.read_all())) is False, "nothing moved: no second row"


# ------------------------------------------------------------------------------------ driven
def _pause_at(eng, nodes: int) -> None:
    """The operator's stop, landing at the creation boundary once `nodes` exist — on the main task,
    after the plan check, the way a UI `pause` lands between two turns."""
    real = eng._ensure_plan

    def hooked(state):
        wrote = real(state)
        if len(state.nodes) >= nodes and not hooked.done:
            hooked.done = True
            eng.store.append(EV_PAUSE, {"reason": "operator"})
        return wrote

    hooked.done = False
    eng._ensure_plan = hooked


def _run_with_a_batch(tmp_path, *, recut: bool, batch: int = 6):
    """Fourteen slots, a quarter reserved (endgame from node 10 over the whole budget). The operator
    stops the run at node 4, injects `batch` experiments and resumes."""
    rd = tmp_path / ("on" if recut else "off")
    kw = {"n_seeds": 2, "max_nodes": 14, "endgame_reserve_frac": 0.25,
          "endgame_inject_recut": recut}
    first = make_engine(rd, **kw)
    _pause_at(first, 4)
    paused = anyio.run(first.run)
    assert paused.paused and not paused.finished
    before = len(paused.nodes)
    assert before in (4, 5), before
    store = EventStore(rd / "events.jsonl")
    for i in range(batch):
        store.append(EV_INJECT_NODE, {"idea": {"operator": "manual",
                                               "params": {"x": float(i) - 2.0, "y": 0.5},
                                               "rationale": f"operator batch {i}"}})
    store.append(EV_RESUME, {})
    state = anyio.run(make_engine(rd, **kw).run)
    events = store.read_all()
    return state, events, before + batch


def _decided_at(events, node_id):
    """The fold the engine decided `node_id` on: the log up to its `node_building` row."""
    index = next(i for i, e in enumerate(events)
                 if e.type == "node_building" and e.data.get("node_id") == node_id)
    return fold(events[:index])


@pytest.mark.parametrize("recut", [True, False], ids=["on", "off"])
def test_a_batch_that_crosses_the_reserve_start_leaves_the_engine_its_search(tmp_path, recut):
    """The incident, driven: with the setting off the batch carries the run past the reserve's
    start and the engine's next node is decided inside the reserve; with it on the plan is re-cut
    (`injected`, the reserve two slots of the engine's eight) and that node is ordinary search."""
    state, events, after_batch = _run_with_a_batch(tmp_path, recut=recut)
    assert state.finished and state.injects_done == 6
    assert operator_injected(events, state.nodes) == 6
    plans = [e.data for e in events if e.type == EV_PLAN]
    decided = _decided_at(events, after_batch)
    if recut:
        assert [p["reason"] for p in plans] == ["initial", "injected"], plans
        assert (plans[1]["endgame_start"], plans[1]["injected"], plans[1]["at_node"]) == (
            12, 6, after_batch)
        assert not in_endgame(decided.plan, len(decided.nodes))
        assert state.nodes[after_batch].operator != "merge"
    else:
        assert [p["reason"] for p in plans] == ["initial"], plans
        assert plans[0]["endgame_start"] == 10 and "injected" not in plans[0]
        assert in_endgame(decided.plan, len(decided.nodes))
    replayed = fold(events)
    assert replayed.plan == plans[-1]


# ------------------------------------------------------------------------------------ critic crit_v48
def test_a_single_injected_node_is_recorded_on_the_row():
    """MUTATION (M04): write the key only for a count above one."""
    assert _plan(injected=1)["injected"] == 1


def test_a_batch_inside_a_reserve_the_run_had_entered_keeps_its_start():
    """F2 (driven by the critic: a second top-2 ensemble over the operator's two injects). The
    engine's own count before the batch had reached the start, so the reserve it was spending stays
    (MUTATION: drop the begun-reserve test). A batch before the start still re-cuts."""
    plan = _plan()                                            # start 15 of 20
    assert _replan(plan, at_node=18, injected=2) is None      # 16 engine nodes >= 15: entered
    assert _replan(plan, at_node=17, injected=2) is None      # 15 >= 15: the reserve's first node
    moved = _replan(plan, at_node=16, injected=2)             # 14 < 15: not entered yet
    assert (moved["reason"], moved["endgame_start"]) == ("injected", 16)


def test_a_batch_re_cuts_with_the_row_s_own_fraction():
    """F6: a fraction raised live is not a re-cut by the historical rule, and a batch must not
    smuggle one in (driven: 0.25 -> 0.5 moved the start from 15 to 10 on one inject). MUTATION: cut
    with the live fraction."""
    plan = _plan()
    assert _replan(plan, reserve_frac=0.5, at_node=9, injected=1) is None
    moved = _replan(plan, reserve_frac=0.5, at_node=9, injected=4)
    assert (moved["endgame_start"], moved["reserve_frac"]) == (16, 0.25)


def test_a_batch_inside_a_live_stall_episode_extends_it():
    """F3: the episode is K of the ENGINE's nodes; three injects at node 9 of [8, 11) spent it after
    one engine node, and the champion's one episode was gone (MUTATION: drop the extension)."""
    base = build_plan(max_nodes=100, n_seeds=2, reserve_frac=0.2, at_node=0)
    kw = {"max_nodes": 100, "n_seeds": 2, "reserve_frac": 0.2, "stall_rung": HARD_STALL_RUNGS,
          "stall_nodes": 3, "champion": 0}
    episode = replan(base, at_node=8, **kw)
    assert (episode["endgame_start"], episode["endgame_end"]) == (8, 11)
    grown = replan(episode, at_node=12, injected=3, **kw)
    assert (grown["reason"], grown["endgame_start"], grown["endgame_end"]) == ("injected", 8, 14)
    assert grown["injected"] == 3 and grown["champion"] == 0
    assert replan(grown, at_node=13, injected=3, **kw) is None, "one row per batch"
    spent = replan(grown, at_node=14, injected=3, **kw)
    assert (spent["reason"], spent["reopen_cause"]) == ("reopened", "episode_spent")
    assert replan(episode, at_node=11, **kw)["reason"] == "reopened", "no batch: as before"


def test_the_count_is_over_the_nodes_the_caller_s_fold_holds(tmp_path):
    """MUTATION (M31): count over a FRESH fold's nodes -> an inject that landed after the caller's
    fold is counted into a plan cut for a state that does not hold it."""
    eng = _seeded_engine(tmp_path, recut=True)
    stale = fold(eng.store.read_all())
    for node_id in (6, 7):
        eng.store.append("node_created", {
            "node_id": node_id, "parent_ids": [], "operator": "manual",
            "idea": {"operator": "manual", "params": {"x": 0.0}}, "code": "c", "source": "manual"})
    assert eng._ensure_plan(stale) is True
    row = [e.data for e in eng.store.read_all() if e.type == EV_PLAN][-1]
    assert row["injected"] == 4 and row["at_node"] == 6


def test_the_strategist_brief_reads_the_plan_the_batch_re_cut(tmp_path):
    """F1 (the critic's probe, driven in the product's cadence configuration): the consult the batch
    makes due ran BEFORE the turn's re-cut, so its brief said the run was INSIDE a reserve the
    `injected` row one seq later moved (MUTATION: re-cut only at the creation boundary)."""
    from looplab.agents.strategist import RuleStrategist, _node_budget_note
    kw = {"n_seeds": 2, "max_nodes": 14, "endgame_reserve_frac": 0.25,
          "endgame_inject_recut": True, "strategist_budget_brief": True,
          "cadence_while_evaluating": True}
    rd = tmp_path / "run"
    first = make_engine(rd, strategist=RuleStrategist(), **kw)
    # The engine's own ceiling beside the policy's: the operator's batch is served against it.
    first.max_nodes, first.n_seeds = 14, 2
    real = first._ensure_plan

    def pause_at_four(state):
        wrote = real(state)
        if len(state.nodes) >= 4 and not pause_at_four.done:
            pause_at_four.done = True
            first.store.append(EV_PAUSE, {"reason": "operator"})
        return wrote

    pause_at_four.done = False
    first._ensure_plan = pause_at_four
    assert anyio.run(first.run).paused
    store = EventStore(rd / "events.jsonl")
    for i in range(6):
        store.append(EV_INJECT_NODE, {"idea": {"operator": "manual",
                                               "params": {"x": float(i) - 2.0, "y": 0.5},
                                               "rationale": f"operator batch {i}"}})
    store.append(EV_RESUME, {})
    second = make_engine(rd, strategist=RuleStrategist(), **kw)
    second.max_nodes, second.n_seeds = 14, 2
    seen: list = []
    real_ctx = second._strategy_ctx

    def record(state):
        ctx = real_ctx(state)
        seen.append((len(state.nodes), (state.plan or {}).get("endgame_start"),
                     _node_budget_note(ctx), second.store.read_all()[-1].seq))
        return ctx

    second._strategy_ctx = record
    anyio.run(second.run)
    events = store.read_all()
    injected = next(e for e in events if e.type == EV_PLAN and e.data["reason"] == "injected")
    after_batch = [row for row in seen if row[0] >= 10]
    assert after_batch, seen
    nodes, start, note, seq = after_batch[0]
    assert seq > injected.seq and start == injected.data["endgame_start"]
    assert "INSIDE the plan's endgame reserve" not in note
