"""Artifact nodes, review round 4 (critic 2026-10-08): the consumer's LINEAGE and the paths around it.

Each test drives one defect the review found on the artifact-node lane (doc 73 §1.4):

  1. a child of a consumer whose producer is being RE-produced was pinned to the parent's superseded
     lifecycle (a paid build guaranteed to end `artifact_unavailable`), and a consumer whose artifact
     is gone for good — deleted, aborted, failed — was still bred from every turn;
  2. an idea's declared `uses` REPLACED the uses its consumer parent passed down;
  3. a Card claim rebuilt the idea without `node_kind` / `uses`;
  4. a re-proposal (`node_reset` from `propose`) kept the OLD node's kind and uses on a new idea;
  6. no Developer prompt said a node is an artifact, or where a consumer's artifacts are;
  7. confirm / the noise floor recorded a seed `_run_eval` never launched as a measured one;
  8. an artifact on a host-scored task ran the host scorer and could never succeed;
  9. the drain-only resume and the plateau stop handed a waiting consumer to the dispatch;
 10. a node's `uses` could name the node itself and wait on its own lifecycle forever;
 11. a Windows host bound its artifact workdirs at drive-letter paths inside a Linux container;
 12. an artifact counted as an MCTS visit and as a subtree's eval cost.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import anyio

from factories import make_engine
from looplab.core.models import Idea
from looplab.engine.artifact_fence import (artifact_build_note, refuse_unrunnable_builds,
                                           unproducible_uses)
from looplab.events.eventstore import Event
from looplab.events.replay import fold
from looplab.events.types import (EV_CARD_ADDED, EV_CONFIRM_EVAL, EV_EVAL_NOISE_SEED,
                                  EV_NODE_CREATED, EV_NODE_EVAL_STARTED, EV_NODE_EVALUATED,
                                  EV_NODE_FAILED, EV_NODE_RESET, EV_POLICY_DECISION)
from looplab.runtime.command_eval import RunResult
from looplab.search.policy import GreedyTree, subtree_eval_cost

_CLEAN_NO_METRIC = RunResult(exit_code=0, stdout="prepared", metric=None, timed_out=False, stderr="")


def _rows(*specs):
    return [Event(seq=i, ts=0.0, type=t, data=d) for i, (t, d) in enumerate(specs)]


def _nc(nid, parents=(), idea=None, **extra):
    return (EV_NODE_CREATED, {"node_id": nid, "parent_ids": list(parents), "operator": "inject",
                              "idea": {"operator": "inject", **(idea or {})}, "code": "x", **extra})


def _ev(nid, generation=0, metric=None, **extra):
    return (EV_NODE_EVALUATED, {"node_id": nid, "generation": generation, "metric": metric,
                                "violations": [], **extra})


def _append(store, *specs):
    for kind, data in specs:
        store.append(kind, data)


# ------------------------------------------------------------- 1. no breeding from a dead artifact

def test_a_consumer_whose_artifact_is_gone_is_never_bred_from_in_a_real_run(tmp_path):
    """DRIVEN through `Engine.run`: the champion reads artifact #0, which was deleted. Every greedy
    turn improves the champion, so every turn used to buy a build that could only end
    `artifact_unavailable`. Now no node is built from it and each refusal is a `policy_decision`."""
    engine = make_engine(tmp_path / "run", policy=GreedyTree(n_seeds=1, max_nodes=4))
    _append(engine.store,
            ("run_started", {"run_id": "r", "task_id": "toy", "direction": "min"}),
            _nc(0, node_kind="artifact"), _ev(0),
            _nc(1, idea={"params": {"x": 3.0, "y": -1.0}}, uses=[0], uses_attempts={"0": 0}),
            _ev(1, metric=0.0),
            ("node_tombstoned", {"node_ids": [0]}))
    state = fold(engine.store.read_all())
    assert state.best().id == 1 and unproducible_uses(state, [1]) == [0]
    anyio.run(engine.run)
    final = fold(engine.store.read_all())
    built = [n for n in final.nodes.values() if n.id > 1]
    assert built, "the run kept searching instead of stopping on the dead lineage"
    assert all(1 not in n.parent_ids for n in built), [(n.id, n.parent_ids) for n in built]
    assert not [n for n in built if n.error_reason == "artifact_unavailable"]
    reasons = [e.data.get("reason") or "" for e in engine.store.read_all()
               if e.type == EV_POLICY_DECISION]
    assert any(r.startswith("artifact_unavailable: artifact(s) #0") for r in reasons), reasons


def test_the_refusal_rule_spares_live_lineages_and_picks_a_runnable_parent():
    st = fold(_rows(
        _nc(0, node_kind="artifact"), _ev(0),
        _nc(1, uses=[0], uses_attempts={"0": 0}), _ev(1, metric=1.0),
        _nc(2), _ev(2, metric=2.0),
        (EV_NODE_RESET, {"node_id": 0, "from_stage": "eval"})))        # being RE-produced
    assert unproducible_uses(st, [1]) == [], "a producer being re-produced is not dead"
    kept, refused = refuse_unrunnable_builds(st, [{"kind": "improve", "parent_id": 1}])
    assert refused == [] and kept == [{"kind": "improve", "parent_id": 1}]
    dead = fold([*_rows(
        _nc(0, node_kind="artifact"), _ev(0),
        _nc(1, uses=[0], uses_attempts={"0": 0}), _ev(1, metric=1.0),
        _nc(2), _ev(2, metric=2.0),
        (EV_NODE_RESET, {"node_id": 0, "from_stage": "eval"}),
        (EV_NODE_FAILED, {"node_id": 0, "generation": 1, "error": "x", "reason": "crash"}))])
    assert unproducible_uses(dead, [1]) == [0], "failed in its current lifecycle: dead"
    kept, refused = refuse_unrunnable_builds(dead, [{"kind": "improve", "parent_id": 1}])
    assert refused == [{"kind": "improve", "parent_id": 1}]
    assert [(a["kind"], a.get("parent_id")) for a in kept] == [("improve", 2)]
    assert kept[0]["_reason"].startswith("artifact_unavailable") and kept[0]["_scores"] == {}
    # beside other work nothing is substituted; with no runnable parent the turn drafts
    kept, _ = refuse_unrunnable_builds(dead, [{"kind": "evaluate", "node_id": 9},
                                              {"kind": "merge", "parent_ids": [1, 2]}])
    assert kept == [{"kind": "evaluate", "node_id": 9}]
    lone = fold(_rows(_nc(0, node_kind="artifact"), _ev(0),
                      _nc(1, uses=[0], uses_attempts={"0": 0}), _ev(1, metric=1.0),
                      ("node_tombstoned", {"node_ids": [0]})))
    kept, _ = refuse_unrunnable_builds(lone, [{"kind": "improve", "parent_id": 1}])
    assert [a["kind"] for a in kept] == ["draft"]
    # a run without artifacts: identity
    plain = fold(_rows(_nc(1), _ev(1, metric=1.0)))
    actions = [{"kind": "improve", "parent_id": 1}]
    assert refuse_unrunnable_builds(plain, actions) == (actions, [])


# ------------------------------------------------------------- 2 + 10. the fold's uses rule

def test_a_declared_use_adds_to_the_inherited_ones_and_never_names_itself():
    st = fold(_rows(
        _nc(0, node_kind="artifact"), _ev(0),
        _nc(5, node_kind="artifact"), _ev(5),
        _nc(1, uses=[0], uses_attempts={"0": 0}), _ev(1, metric=1.0),
        _nc(2, parents=[1], idea={"uses": [5, 0]}),                   # inherited 0, declared 5
        _nc(3, idea={"uses": [5]}),                                   # no parent: declared only
        _nc(4, parents=[1])))                                         # nothing declared
    assert st.nodes[2].uses == [0, 5], "the ordered union: inherited first, then declared"
    assert st.nodes[2].uses_attempts == {"0": 0, "5": 0}
    assert st.nodes[3].uses == [5] and st.nodes[4].uses == [0], "old shapes fold as before"
    selfish = fold(_rows(
        _nc(0, node_kind="artifact"), _ev(0),
        _nc(6, node_kind="artifact"),
        (EV_NODE_RESET, {"node_id": 6, "from_stage": "implement"}),
        _nc(6, node_kind="artifact", uses=[6, 0], generation=1),      # a rebuild row naming itself
        _nc(7, idea={"node_kind": "artifact", "uses": [7]})))
    assert selfish.nodes[6].uses == [0] and "6" not in selfish.nodes[6].uses_attempts
    assert selfish.nodes[7].uses == []


# ------------------------------------------------------------- 3. a Card claim keeps what the node IS

def _mint_and_claim(engine, idea):
    from looplab.search.card_selection import card_action as projected_card_action
    events = engine.store.read_all()
    plan = engine._plan_native_card(
        events, fold(events), idea, parents=[], parent_generations={},
        scored_against=None, source="researcher", at_node=1)
    assert plan.disposition == "mint", plan.disposition
    engine.store.append(EV_CARD_ADDED, plan.payload)
    events = engine.store.read_all()
    state = fold(events)
    card = state.cards[plan.card_id]
    engine._card_claim_refusal = None
    reservation = engine._prepare_existing_card_claim(
        events, state, projected_card_action(card), card, node_id=2)
    assert reservation is not None, engine._card_claim_refusal
    return plan, card, reservation.idea


def test_a_claimed_card_builds_the_artifact_or_consumer_its_proposal_described(tmp_path):
    engine = make_engine(tmp_path / "run", n_seeds=0, max_nodes=4, card_driven_selection=True)
    _append(engine.store, _nc(0, node_kind="artifact"), _ev(0))
    base = dict(operator="draft", params={"x": 1.0}, rationale="because", hypothesis="h")
    plan, card, executed = _mint_and_claim(engine, Idea(**base, node_kind="artifact"))
    assert plan.payload["idea"]["node_kind"] == "artifact" and executed.node_kind == "artifact"
    assert card.selection_ready is True, "the new keys are admitted, not a lossy future schema"
    plan2, card2, executed2 = _mint_and_claim(
        engine, Idea(**{**base, "hypothesis": "h2"}, uses=[0]))
    assert executed2.uses == [0] and card2.selection_ready is True
    # outside the digest: the same action with and without the fields mints the same receipt
    plain = engine._card_added_payload("card-x", "s", _receipt_action(plan.payload),
                                       Idea(**base), source="researcher", at_node=1)
    marked = engine._card_added_payload("card-x", "s", _receipt_action(plan.payload),
                                        Idea(**base, node_kind="artifact"), source="researcher",
                                        at_node=1)
    assert plain["ownership_receipt"] == marked["ownership_receipt"]
    assert "node_kind" not in plain["idea"] and "uses" not in plain["idea"]


def _receipt_action(payload):
    idea = payload["idea"]
    return {"operator": idea["operator"], "params": idea["params"], "space": idea["space"],
            "eval_profile": idea["eval_profile"], "eval_timeout": idea["eval_timeout"],
            "parent_id": payload["parent_id"], "parent_ids": payload["parent_ids"],
            "parent_generations": payload["parent_generations"],
            "scored_against": payload["scored_against"],
            "scored_against_generation": payload["scored_against_generation"],
            "scored_against_empty": payload["scored_against_empty"],
            "footprint": payload.get("footprint")}


# ------------------------------------------------------------- 4 + 6. rebuilds, and the Developer note

class _RecordingDeveloper:
    def __init__(self):
        self.ideas = []

    def implement(self, idea):
        self.ideas.append(idea)
        return "print(1)"


class _FixedResearcher:
    def __init__(self, idea):
        self.idea = idea

    def propose(self, state, parent):
        return self.idea.model_copy(deep=True)


def test_a_reproposal_is_a_new_idea_and_an_implement_rebuild_keeps_what_the_node_is(tmp_path):
    dev = _RecordingDeveloper()
    engine = make_engine(tmp_path / "run", developer=dev)
    _append(engine.store,
            ("run_started", {"run_id": "r", "task_id": "toy", "direction": "min"}),
            _nc(0, node_kind="artifact"), _ev(0))
    engine._create_node({"kind": "draft"}, preproposed=Idea(
        operator="draft", params={"x": 1.0}, rationale="prep", hypothesis="h", node_kind="artifact",
        uses=[0]))
    node = fold(engine.store.read_all()).nodes[1]
    assert node.kind == "artifact" and node.uses == [0]
    assert "THIS NODE IS AN ARTIFACT" in dev.ideas[-1].rationale
    assert "LOOPLAB_USES_WORKDIRS" in dev.ideas[-1].rationale and "#0" in dev.ideas[-1].rationale
    # implement: same idea, same kind, same uses — and the Developer is told so again
    _append(engine.store, (EV_NODE_FAILED, {"node_id": 1, "generation": 0, "error": "x",
                                            "reason": "crash", "eval_seconds": 0.0}),
            (EV_NODE_RESET, {"node_id": 1, "generation": 0, "from_stage": "implement"}))
    engine._rerun_node(fold(engine.store.read_all()).nodes[1], fold(engine.store.read_all()))
    rebuilt = fold(engine.store.read_all()).nodes[1]
    assert rebuilt.attempt == 1 and rebuilt.kind == "artifact" and rebuilt.uses == [0]
    assert "THIS NODE IS AN ARTIFACT" in dev.ideas[-1].rationale
    # propose: a NEW idea under the same id carries none of the old node's
    _append(engine.store, (EV_NODE_FAILED, {"node_id": 1, "generation": 1, "error": "x",
                                            "reason": "crash", "eval_seconds": 0.0}),
            (EV_NODE_RESET, {"node_id": 1, "generation": 1, "from_stage": "propose"}))
    engine.researcher = _FixedResearcher(Idea(operator="draft", params={"x": 2.0},
                                              rationale="a plain experiment", hypothesis="h2"))
    engine._rerun_node(fold(engine.store.read_all()).nodes[1], fold(engine.store.read_all()))
    row = [e.data for e in engine.store.read_all()
           if e.type == EV_NODE_CREATED and e.data["node_id"] == 1][-1]
    assert "node_kind" not in row and "uses" not in row and "uses_attempts" not in row
    reproposed = fold(engine.store.read_all()).nodes[1]
    assert reproposed.attempt == 2 and reproposed.kind is None and reproposed.uses == []
    assert dev.ideas[-1].rationale == "a plain experiment", "a plain node's prompt is untouched"


def test_a_child_of_a_consumer_is_told_where_its_inherited_artifacts_are(tmp_path):
    dev = _RecordingDeveloper()
    engine = make_engine(tmp_path / "run", developer=dev)
    _append(engine.store, _nc(0, node_kind="artifact"), _ev(0),
            _nc(1, uses=[0], uses_attempts={"0": 0}), _ev(1, metric=1.0), _nc(2), _ev(2, metric=2.0))
    state = fold(engine.store.read_all())
    idea = Idea(operator="improve", rationale="tune it")
    engine._implement_result(idea, None, state=state)
    assert dev.ideas[-1] is idea, "no parent, no artifacts: the very same object"
    engine._implement_result(idea, state.nodes[2], state=state)
    assert dev.ideas[-1] is idea
    engine._implement_result(idea, state.nodes[1], state=state)
    assert "produced by node(s) #0" in dev.ideas[-1].rationale and idea.rationale == "tune it"
    assert artifact_build_note(None, []) == ""


# ------------------------------------------------------------- 7. a refused seed is not a measured one

def _confirm_engine(tmp_path):
    engine = make_engine(tmp_path / "run", confirm_top_k=1, confirm_seeds=1, max_nodes=2)
    _append(engine.store,
            ("run_started", {"run_id": "r", "task_id": "toy", "direction": "min"}),
            _nc(0, node_kind="artifact"), _ev(0),
            _nc(1, uses=[0], uses_attempts={"0": 0}), _ev(1, metric=1.0))
    engine.store.append(EV_NODE_RESET, {"node_id": 0, "from_stage": "eval"})   # the pin moves

    async def reserve(nd, *, resource_pin=None, wait_once=False):
        return {**engine._resource_request_for_node(nd, resource_pin=resource_pin), "gpu_ids": []}

    engine._wait_reserve_node_resources = reserve
    engine._release_gpus = lambda ids: None
    return engine


def test_confirm_and_the_noise_floor_never_record_a_seed_nothing_ran(tmp_path):
    from looplab.engine.confirm_phase import _CONFIRM_ARTIFACT_REFUSED
    engine = _confirm_engine(tmp_path)
    node = fold(engine.store.read_all()).nodes[1]
    spawned = []
    engine.sandbox.run = lambda *a, **k: spawned.append(1)
    result = anyio.run(engine._run_confirm_seed, node, engine.confirm_seed_base)
    assert result is _CONFIRM_ARTIFACT_REFUSED and spawned == []
    rows = [e.data for e in engine.store.read_all() if e.type == EV_CONFIRM_EVAL]
    assert [r["reason"] for r in rows] == ["artifact_unavailable"]
    assert fold(engine.store.read_all()).confirm_seed_results.get(1, {}) == {}, "not memoized"
    anyio.run(engine._run_confirm_seed, node, engine.confirm_seed_base)
    assert len([e for e in engine.store.read_all() if e.type == EV_CONFIRM_EVAL]) == 1, "deduped"
    assert anyio.run(engine._run_noise_seed, node, 0) is None
    assert not [e for e in engine.store.read_all() if e.type == EV_EVAL_NOISE_SEED]


# ------------------------------------------------------------- 8. an artifact is never host-scored

def test_an_artifact_on_a_host_scored_task_runs_no_host_scorer_and_succeeds(tmp_path):
    from looplab.adapters.repo_task import EvalSpec, RepoTask
    fixture = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
    host = tmp_path / "host-scorers" / "score.py"
    host.parent.mkdir(parents=True)
    # The operator's scorer reads the candidate's PREDICTIONS, which a preparation step never writes.
    host.write_text("import os, sys\nsys.exit(0 if os.path.exists('preds.txt') else 3)\n",
                    encoding="utf-8")
    task = RepoTask(id="fix", goal="maximize metric", direction="max", editable_path=str(fixture),
                    edit_surface=["*.json"], protect=["ttrain.py"],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"], timeout=60,
                                  metric={"kind": "stdout_json", "key": "metric"},
                                  host_scorer={"command": [sys.executable, str(host)]}))
    researcher, developer = task.build_roles()
    engine = make_engine(tmp_path / "run", task=task, researcher=researcher, developer=developer)
    engine._inline_repair = False
    _append(engine.store, ("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "inject", "node_kind": "artifact",
        "idea": {"operator": "inject", "rationale": "prepare"}, "code": "",
        "files": {"config.json": '{"x": 2.0}'}}),
            ("node_created", {
        "node_id": 1, "parent_ids": [], "operator": "inject",
        "idea": {"operator": "inject", "rationale": "experiment"}, "code": "",
        "files": {"config.json": '{"x": 2.0}'}}))
    state = fold(engine.store.read_all())
    wd = tmp_path / "wd"
    wd.mkdir()
    # no host stage: the task's own command alone, the single-command eval of a task without one
    assert engine._resolved_stages(state.nodes[0], str(wd)) == []
    assert [s["name"] for s in engine._resolved_stages(state.nodes[1], str(wd))] == [
        "self_score", "score"], "the experiment keeps its host scorer"
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    artifact = fold(engine.store.read_all()).nodes[0]
    assert artifact.status.value == "evaluated" and artifact.metric is None, artifact.error


# ------------------------------------------------------------- 9. a waiting consumer is not handed

def _waiting_pair(tmp_path):
    """Producer #0 and consumer #1 pinned to its lifecycle 0, both with an interrupted evaluation:
    the drain owes both, and #1 can only run once #0 is produced."""
    engine = make_engine(tmp_path / "run")
    _append(engine.store,
            ("run_started", {"run_id": "r", "task_id": "toy", "direction": "min"}),
            _nc(0, node_kind="artifact"), _nc(1, uses=[0], uses_attempts={"0": 0}),
            (EV_NODE_EVAL_STARTED, {"node_id": 0, "generation": 0}),
            (EV_NODE_EVAL_STARTED, {"node_id": 1, "generation": 0}))
    handed = []

    async def dispatch(evals, state, max_es, *, research=True):
        ids = [a["node_id"] for a in evals]
        handed.append(ids)
        for nid in ids:
            engine.store.append(EV_NODE_EVALUATED, {"node_id": nid, "generation": 0,
                                                    "metric": None if nid == 0 else 1.0,
                                                    "violations": []})

    async def nothing():
        return None

    engine._dispatch_evals = dispatch
    engine._drain_adopted_evals = nothing
    engine._raise_deferred_eval_budget_stop = nothing
    return engine, handed


def test_the_drain_and_the_plateau_stop_evaluate_the_producer_before_its_waiting_consumer(tmp_path):
    engine, handed = _waiting_pair(tmp_path / "drain")
    assert anyio.run(engine._drain_only_turn, fold(engine.store.read_all()), None) == "continue"
    assert anyio.run(engine._drain_only_turn, fold(engine.store.read_all()), None) == "continue"
    assert handed == [[0], [1]], "the consumer waited a turn instead of pausing the drain STUCK"

    engine, handed = _waiting_pair(tmp_path / "plateau")
    engine._close_card_build_before_terminal_gate = lambda state, max_es: False
    engine._evals_inflight = lambda: False

    async def ladder(state, *, decision_seq, finish_reason=None):
        return "break"

    engine._handle_no_actions = ladder
    turn = lambda: engine._plateau_stop_turn(  # noqa: E731
        fold(engine.store.read_all()), "why", decision_seq=0, max_es=None)
    assert anyio.run(turn) == "continue"
    assert anyio.run(turn) == "continue"
    assert handed == [[0], [1]]


# ------------------------------------------------------------- 11. Windows hosts and the container

def test_a_windows_host_mounts_used_artifacts_at_container_paths(monkeypatch):
    from looplab.engine.eval_dispatch import DOCKER_USES_ROOT, docker_use_binds
    from looplab.runtime import command_eval as ce
    assert docker_use_binds(["/r/nodes/node_0"], windows=False) == ([("/r/nodes/node_0", True)], None)
    binds, value = docker_use_binds([r"C:\r\nodes\node_0", r"C:\r\nodes\node_3"], windows=True)
    assert [b[2] for b in binds] == [f"{DOCKER_USES_ROOT}/0", f"{DOCKER_USES_ROOT}/1"]
    assert value == f"{DOCKER_USES_ROOT}/0:{DOCKER_USES_ROOT}/1"
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")
    wrap = ce.make_docker_wrap("/tmp/root", "python:3.12-slim",
                               binds=[("/srv/a", True, f"{DOCKER_USES_ROOT}/0")],
                               env={"LOOPLAB_USES_WORKDIRS": value})
    argv = wrap(["python", "x.py"], "/tmp/root")
    mount = argv[argv.index("--mount", argv.index("--mount") + 1) + 1] \
        if argv.count("--mount") > 1 else argv[argv.index("--mount") + 1]
    assert any(a.endswith(f"dst={DOCKER_USES_ROOT}/0,readonly") for a in argv), mount
    assert f"LOOPLAB_USES_WORKDIRS={value}" in argv


# ------------------------------------------------------------- 12. an artifact is no trial of the tree

def test_an_artifact_is_neither_a_visit_nor_an_expense_of_its_subtree():
    st = fold(_rows(
        _nc(0), _ev(0, metric=1.0, eval_seconds=10.0),
        _nc(1, parents=[0], node_kind="artifact"), _ev(1, eval_seconds=1000.0)))
    assert subtree_eval_cost(st, [0, 1]) == 10.0
