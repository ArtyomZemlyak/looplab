"""`looplab resume --drain-only` (doc 68 68.3a): evaluate what is owed, then pause.

A rescore — `node_reset {from_stage: "eval"}` — could not be run without resuming the whole search:
a resumed run evaluated the reset node and went on creating nodes, consulting and researching.
Driven through the real CLI on a real toy run, read back off the run's own log.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.cli.run_cmds import drain_only_refusal
from looplab.core.models import NodeStatus
from looplab.engine.orchestrator import (DRAIN_ONLY_PAUSE_REASON, DRAIN_ONLY_STUCK_REASON, Engine,
                                         drain_owed)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

_RUN = ["run", "--no-genesis", "--kind", "quadratic", "--goal", "min (x-3)^2", "--direction", "min",
        "--backend", "toy", "--max-nodes", "4"]


def _finished_run(tmp_path, *extra, nodes=4):
    rd = tmp_path / "run"
    out = CliRunner().invoke(app, [*_RUN, *extra, "--out", str(rd)])
    assert out.exit_code == 0, out.output
    store = EventStore(rd / "events.jsonl")
    state = fold(store.read_all())
    assert state.finished and len(state.nodes) == nodes
    return rd, store


def _reset(store, node_id, stage="eval"):
    store.append("node_reset", {"node_id": node_id, "from_stage": stage,
                                "generation": fold(store.read_all()).nodes[node_id].attempt})


def _drain(rd):
    return CliRunner().invoke(app, ["resume", str(rd), "--drain-only"])


def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(tmp_path / "kn"))


def test_a_rescore_is_evaluated_and_nothing_else_happens(tmp_path, monkeypatch):
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    node = fold(store.read_all()).nodes[1]
    _reset(store, 1)
    # A queued operator inject stays queued: the drain serves no forced request (critic
    # 2026-09-26: a hook placed after `_serve_forced_requests` built it and nothing went red).
    store.append("inject_node", {"idea": {"operator": "manual", "params": {"x": 0.5},
                                          "rationale": "operator hunch"},
                                 "parent_id": None, "code": None})
    mark = store.read_all()[-1].seq
    # The research overlap is ASKED for on every ordinary dispatch and is a no-op when nothing is
    # due, so its rows alone cannot show the drain never asked: count the asks.
    asked = []
    monkeypatch.setattr(Engine, "_spawn_research", lambda self, tg, state: asked.append(1) or False)

    out = _drain(rd)
    assert out.exit_code == 0, out.output
    assert "drain-only: 1 evaluation(s) owed (node(s) 1)" in out.output

    after_events = store.read_all()
    after = fold(after_events)
    tail = [e for e in after_events if e.seq > mark]
    # The reset node was evaluated, on its new lifecycle…
    assert after.nodes[1].attempt == node.attempt + 1
    assert after.nodes[1].metric is not None
    assert any(e.type == "node_evaluated" and e.data.get("node_id") == 1 for e in tail)
    # …and nothing else was bought: no node, no build, no consult, no research, no ladder.
    assert len(after.nodes) == 4
    kinds = {e.type for e in tail}
    assert not kinds & {"node_created", "node_building", "strategy_decision", "card_build_requested",
                        "research_attempted", "research_completed", "node_confirmed",
                        "eval_noise_floor", "run_finished", "inject_done"}, sorted(kinds)
    assert asked == [], "the drain's dispatch overlapped a research think"
    assert after.injects_done == 0 and len(after.inject_requests) == 1, "the inject stays queued"
    # It ends PAUSED, saying why, so the next plain `resume` continues the search.
    assert after.paused
    pauses = [e.data for e in tail if e.type == "pause"]
    assert pauses and pauses[-1].get("reason") == DRAIN_ONLY_PAUSE_REASON


def test_a_finished_run_nobody_reset_is_left_exactly_as_it_was(tmp_path, monkeypatch):
    """Critic 2026-09-26, driven: the plain lift appended `resume` to a finished run, which opens a
    new search epoch — and after a holdout disclosure the rotation re-queued every evaluated node,
    which the drain then re-evaluated, all four, on a run nobody had reset."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    for node_id in sorted(fold(store.read_all()).nodes)[:3]:     # the shape `holdout.py` writes
        store.append("holdout_evaluated", {"node_id": node_id, "generation": 0,
                                           "metric": 1.0, "search_epoch": 0})
    before = store.read_all()
    out = _drain(rd)
    assert out.exit_code == 0, out.output
    assert "run is finished and nothing is owed an evaluation" in out.output
    assert [e.seq for e in store.read_all()] == [e.seq for e in before], "nothing was appended"
    after = fold(store.read_all())
    assert after.finished and after.search_epoch == fold(before).search_epoch


def test_nothing_owed_lifts_no_pause(tmp_path, monkeypatch):
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    _reset(store, 1)
    assert _drain(rd).exit_code == 0                  # rescored, then paused by the drain
    before = [e.seq for e in store.read_all()]
    out = _drain(rd)
    assert out.exit_code == 0 and "nothing is owed an evaluation" in out.output
    assert [e.seq for e in store.read_all()] == before and fold(store.read_all()).paused


def test_what_the_cli_refuses_to_drain():
    """`drain_only_refusal`'s truth table over folded-state stand-ins."""
    owed = SimpleNamespace(id=1, status=NodeStatus.pending, tombstoned=False, rerun_from=None,
                           attempt=1, eval_started=False)

    def state(*, nodes=(), holdout=()):
        return SimpleNamespace(nodes={n.id: n for n in nodes}, aborted_nodes=set(),
                               holdout_evaluated_ids=set(holdout))

    assert drain_only_refusal(state(nodes=[owed]), "pending_finalize")[0] == 2
    assert drain_only_refusal(state(nodes=[owed]), "finalization_pending")[0] == 2
    assert drain_only_refusal(state(), "finished")[0] == 0, "finished, nothing owed: no lift"
    assert drain_only_refusal(state(), "paused")[0] == 0, "nothing owed: no lift, no re-pause"
    assert drain_only_refusal(state(nodes=[owed], holdout=[0]), "paused")[0] == 2
    assert drain_only_refusal(state(nodes=[owed], holdout=[0]), "finished")[0] == 2
    assert drain_only_refusal(state(nodes=[owed]), "paused") is None
    assert drain_only_refusal(state(nodes=[owed]), "finished") is None, (
        "a finished run that still owes work — the eval budget finalized it — is drained")
    assert drain_only_refusal(state(nodes=[owed]), "live") is None
    waiting = SimpleNamespace(**{**vars(owed), "attempt": 0, "rerun_from": "implement"})
    assert drain_only_refusal(state(nodes=[waiting]), "live") is None, "the loop head rebuilds it"


def test_what_the_drain_owes_is_a_reset_or_an_interrupted_evaluation(tmp_path, monkeypatch):
    """`drain_owed` over the REAL fold of a real run's log, one control event at a time."""
    _isolated(monkeypatch, tmp_path)
    _rd, store = _finished_run(tmp_path)
    created = next(e.data for e in store.read_all() if e.type == "node_created"
                   and e.data.get("node_id") == 3)

    def owed(node_id):
        state = fold(store.read_all())
        return drain_owed(state, state.nodes[node_id])

    assert not owed(1), "an evaluated node is owed nothing"
    # A build the search made and never dispatched: pending, generation 0, no eval started.
    store.append("node_created", {**created, "node_id": 4, "parent_ids": [3],
                                  "parent_generations": {"3": 0}})
    assert fold(store.read_all()).nodes[4].status.value == "pending" and not owed(4)
    store.append("node_eval_started", {"node_id": 4, "generation": 0})
    assert owed(4), "an evaluation that started and never landed a terminal is owed"
    store.append("node_reset", {"node_id": 2, "from_stage": "eval", "generation": 0})
    assert owed(2), "a reset opened a lifecycle that is owed its evaluation"
    store.append("node_reset", {"node_id": 1, "from_stage": "implement", "generation": 0})
    assert not owed(1), "a rebuild is the loop head's, not the drain's"
    store.append("node_abort", {"node_id": 2, "generation": 1})
    assert not owed(2), "an aborted lifecycle is withdrawn"
    store.append("node_tombstoned", {"node_ids": [4]})
    assert not owed(4), "a tombstoned node is withdrawn"


def test_a_build_the_search_made_is_left_for_the_search(tmp_path, monkeypatch):
    """A pending node no reset or interruption left owed — a Card's speculative build, above all —
    is a SEARCH decision: the drain rescores the reset node and leaves the build pending."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    created = next(e.data for e in store.read_all() if e.type == "node_created"
                   and e.data.get("node_id") == 3)
    _reset(store, 1)
    store.append("node_created", {**created, "node_id": 4, "parent_ids": [3],
                                  "parent_generations": {"3": 0}})
    mark = store.read_all()[-1].seq
    out = _drain(rd)
    assert out.exit_code == 0, out.output
    tail = [e for e in store.read_all() if e.seq > mark]
    after = fold(store.read_all())
    assert after.nodes[4].status.value == "pending" and after.nodes[1].metric is not None
    assert not any(e.data.get("node_id") == 4 for e in tail
                   if e.type in ("node_eval_started", "node_evaluated", "node_failed"))
    assert [e.data.get("reason") for e in tail if e.type == "pause"] == [DRAIN_ONLY_PAUSE_REASON]


def test_a_spent_eval_budget_pauses_the_drain_instead_of_finalizing(tmp_path, monkeypatch):
    """Critic 2026-09-26, driven: the loop head's eval-budget gate FINALIZED a drained run — the
    reset node left pending, the report naming the wrong champion. The drain is asked first."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path, "-s", "max_eval_seconds=0.000001", nodes=3)  # seed batch
    assert fold(store.read_all()).stop_reason == "eval_budget"
    _reset(store, 2)
    mark = store.read_all()[-1].seq
    out = _drain(rd)
    assert out.exit_code == 0, out.output
    tail = [e for e in store.read_all() if e.seq > mark]
    assert "run_finished" not in {e.type for e in tail}, [e.type for e in tail]
    reasons = [e.data.get("reason") for e in tail if e.type == "pause"]
    assert len(reasons) == 1 and "the run's eval budget is spent" in reasons[0]
    assert "node(s) 2 were not evaluated" in reasons[0]
    after = fold(store.read_all())
    assert after.paused and after.nodes[2].status.value == "pending"


def test_a_finalize_requested_mid_drain_is_the_loop_heads_to_settle(tmp_path, monkeypatch):
    """A halt intent that lands during the drain hands the turn back to the loop head, whose stop
    handling finishes the run in-process (critic 2026-09-26: returning `break` left a run with
    `stop_requested` and no finish, waiting for another driver)."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    _reset(store, 1)
    mark = store.read_all()[-1].seq

    async def finalize_lands(self, evals, state, max_es, *, research=True):
        self.store.append("run_abort", {"reason": "finalized"})

    monkeypatch.setattr(Engine, "_dispatch_evals", finalize_lands)
    out = _drain(rd)
    assert out.exit_code == 0, out.output
    tail = [e.type for e in store.read_all() if e.seq > mark]
    assert "run_finished" in tail and "pause" not in tail, tail
    assert fold(store.read_all()).finished


def test_a_dispatch_that_admits_nothing_pauses_instead_of_spinning(tmp_path, monkeypatch):
    """A turn in which no owed lifecycle moved would hand the same node to the dispatch on every
    turn, forever (found by a mutant that hung). Driven with a dispatch that admits nothing."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    _reset(store, 1)
    mark = store.read_all()[-1].seq
    calls = []

    async def admits_nothing(self, evals, state, max_es, *, research=True):
        calls.append([a["node_id"] for a in evals])
        if len(calls) > 3:          # a spin, not a hang: fail loudly instead of looping forever
            raise AssertionError(f"the drain spun: {calls}")

    monkeypatch.setattr(Engine, "_dispatch_evals", admits_nothing)
    out = _drain(rd)
    assert out.exit_code == 0, out.output
    assert calls == [[1]], calls
    tail = [e for e in store.read_all() if e.seq > mark]
    assert [e.data.get("reason") for e in tail if e.type == "pause"] == [
        DRAIN_ONLY_STUCK_REASON.format(ids="1")]
    assert fold(store.read_all()).nodes[1].status.value == "pending"


def _config(rd, **values):
    path = rd / "config.snapshot.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **values}))


def test_a_drain_is_never_finished_as_a_systemic_failure(tmp_path, monkeypatch):
    """Critic 2026-09-26, driven: every other node failed, the operator fixed the environment and
    reset one — and the systemic-failure gate above the hook FINISHED the run, the reset node left
    pending and the run's lessons written to cross-run memory."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    for node_id in range(4):
        _reset(store, node_id)
        store.append("node_failed", {"node_id": node_id, "generation": 1, "error": "env broken",
                                     "reason": "crash"})
    _reset(store, 0)
    mark = store.read_all()[-1].seq
    out = _drain(rd)
    assert out.exit_code == 0, out.output
    tail = [e for e in store.read_all() if e.seq > mark]
    assert "run_finished" not in {e.type for e in tail}, [e.type for e in tail]
    assert fold(store.read_all()).nodes[0].metric is not None
    assert [e.data.get("reason") for e in tail if e.type == "pause"] == [DRAIN_ONLY_PAUSE_REASON]


def test_a_budget_finalized_run_that_owes_work_is_drained_once_the_budget_is_raised(
        tmp_path, monkeypatch):
    """The budget pause's remedy is the run's config, not a plain resume — which finalizes the run
    on the same budget, the reset node still pending. The drain then saw a FINISHED run and said
    "nothing owed" while the node was (critic 2026-09-26, driven)."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path, "-s", "max_eval_seconds=0.000001", nodes=3)
    _reset(store, 2)
    assert _drain(rd).exit_code == 0                         # paused: the budget is spent
    reason = [e.data.get("reason") for e in store.read_all() if e.type == "pause"][-1]
    assert "raise `max_eval_seconds` in the run's config" in reason
    assert "would finalize the run" in reason
    assert CliRunner().invoke(app, ["resume", str(rd)]).exit_code == 0   # the plain resume...
    state = fold(store.read_all())
    assert state.finished and state.nodes[2].status.value == "pending"  # ...finalized it
    _config(rd, max_eval_seconds=1e9)
    out = _drain(rd)
    assert out.exit_code == 0, out.output
    assert "drain-only: 1 evaluation(s) owed (node(s) 2)" in out.output
    # Not "resuming to continue with the current settings" right after "then pausing" (NIT).
    assert "run was finished — lifting it for the drain; it pauses again" in out.output
    after = fold(store.read_all())
    assert after.nodes[2].metric is not None and after.paused and not after.finished


def test_a_finished_host_graded_run_is_not_lifted_across_its_split(tmp_path, monkeypatch):
    """MEDIUM (critic 2026-09-26, driven on a host-graded task): lifting a FINISH opens a new search
    epoch, and the holdout split the host scores the search on is salted by it — the drained node
    was ranked against incumbents measured on other rows (0.5111 against 0.4444, where the leader's
    own code scored 0.4889 on the new rows). Refused before anything is appended (doc 68 68.3c)."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path, "-s", "max_eval_seconds=0.000001", nodes=3)
    _reset(store, 2)
    assert _drain(rd).exit_code == 0                                   # budget pause
    assert CliRunner().invoke(app, ["resume", str(rd)]).exit_code == 0  # finalized, node 2 owed
    # The run as a host-graded one: the grading row setup appends, and the split `run_started`
    # pinned (the default 0.25).
    store.append("host_grading", {"predictions": "predictions.json", "scorer": "accuracy"})
    state = fold(store.read_all())
    assert state.finished and state.host_grading and state.holdout_fraction == 0.25
    _config(rd, max_eval_seconds=1e9)
    before = [e.seq for e in store.read_all()]
    out = _drain(rd)
    assert out.exit_code == 2, out.output
    assert "re-carves the split the host scores the search on" in out.output
    assert "node(s) 2 would be scored on other rows" in out.output
    assert [e.seq for e in store.read_all()] == before, "refused before anything was appended"


def test_a_reset_after_a_disclosure_is_refused_rather_than_retraining_every_incumbent(
        tmp_path, monkeypatch):
    """One reset after a holdout disclosure re-opens EVERY evaluated node (the epoch rule), and the
    drain retrained all four while saying "every reset or interrupted evaluation finished" (critic
    2026-09-26, driven). The re-queued nodes are named and the drain refuses before anything."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    for node_id in (0, 2, 3):
        store.append("holdout_evaluated", {"node_id": node_id, "generation": 0, "metric": 1.0,
                                           "search_epoch": 0})
    _reset(store, 1)
    before = [e.seq for e in store.read_all()]
    out = _drain(rd)
    assert out.exit_code == 2, out.output
    assert "node(s) 0, 2, 3 were re-queued by the holdout epoch rotation, not reset" in out.output
    assert [e.seq for e in store.read_all()] == before, "refused before anything was appended"


def test_a_time_budget_pauses_between_evaluations(tmp_path, monkeypatch):
    """The dispatch re-checks the eval budget before each evaluation but never the wall clock, so a
    batch ran every owed node past `max_seconds` (critic 2026-09-26). With a time budget the drain
    hands one node per turn and asks the clock between them."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    _reset(store, 1)
    _reset(store, 2)
    _config(rd, max_seconds=0.000001)
    mark = store.read_all()[-1].seq
    out = _drain(rd)
    assert out.exit_code == 0, out.output
    tail = [e for e in store.read_all() if e.seq > mark]
    reasons = [e.data.get("reason") for e in tail if e.type == "pause"]
    assert len(reasons) == 1 and "this invocation's time budget" in reasons[0], reasons
    assert "drain again: the time budget is this invocation's" in reasons[0], (
        "not the eval budget's remedy: `max_seconds` resets per invocation (critic 2026-09-26)")
    assert not any(e.type == "node_evaluated" for e in tail)


def test_a_time_budget_hands_one_eval_width_per_turn(tmp_path, monkeypatch):
    """LOW (critic 2026-09-26, driven): one node per turn serialized a parallel drain under ANY time
    budget (4.3 s became 6.4 s at `max_parallel=2`). The width is the normal loop's own overshoot
    bound, and the clock is asked between batches."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path, "-s", "eval_parallel=2")
    for node_id in (1, 2, 3):
        _reset(store, node_id)
    _config(rd, max_seconds=1e9)
    handed = []

    async def record(self, evals, state, max_es, *, research=True):
        handed.append([a["node_id"] for a in evals])

    monkeypatch.setattr(Engine, "_dispatch_evals", record)
    out = _drain(rd)
    assert out.exit_code == 0, out.output
    assert handed == [[1, 2]], handed        # one width, then nothing moved: a stated stall pause


def test_the_requeue_walk_is_skipped_on_a_log_that_never_disclosed(tmp_path, monkeypatch):
    """NIT (critic 2026-09-26): the lifecycle walk behind the requeue refusal ran a full fold pass on
    every drain; a rotation re-queues only after a disclosure, so a log without one skips it."""
    import looplab.events.git_export as git_export

    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    _reset(store, 1)
    monkeypatch.setattr(git_export, "node_lifecycles",
                        lambda *a, **k: pytest.fail("walked a log that never disclosed a holdout"))
    out = _drain(rd)
    assert out.exit_code == 0, out.output


def test_a_lifecycle_that_moves_mid_dispatch_is_progress_not_a_stall(tmp_path, monkeypatch):
    """A reset landing while the dispatch runs moves the node to a new generation: the next turn
    hands it again, and only a turn in which nothing moved pauses as stuck."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    _reset(store, 1)
    calls = []

    async def reset_once(self, evals, state, max_es, *, research=True):
        calls.append([a["node_id"] for a in evals])
        if len(calls) == 1:
            _reset(self.store, 1)
        elif len(calls) > 3:
            raise AssertionError(f"the drain spun: {calls}")

    monkeypatch.setattr(Engine, "_dispatch_evals", reset_once)
    out = _drain(rd)
    assert out.exit_code == 0, out.output
    assert calls == [[1], [1]], calls
    assert fold(store.read_all()).nodes[1].attempt == 2


def test_an_implement_reset_is_counted_as_a_rebuild_before_it_is_owed(tmp_path, monkeypatch):
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    _reset(store, 1, "implement")
    out = _drain(rd)
    assert out.exit_code == 0, out.output
    assert "0 evaluation(s) owed and 1 reset(s) to rebuild first (node(s) 1)" in out.output
    assert fold(store.read_all()).nodes[1].metric is not None


@pytest.mark.parametrize("max_seconds,batches", [(None, [[1, 2]]), (1e9, [[1], [2]])])
def test_a_time_budget_hands_one_node_per_turn(tmp_path, monkeypatch, max_seconds, batches):
    """Without a time budget every owed node goes to one dispatch; with one, a node per turn, so
    the wall clock is asked between evaluations (the dispatch itself only re-checks eval seconds)."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    _reset(store, 1)
    _reset(store, 2)
    if max_seconds is not None:
        _config(rd, max_seconds=max_seconds)
    handed = []

    async def evaluate(self, evals, state, max_es, *, research=True):
        handed.append([a["node_id"] for a in evals])
        for a in evals:
            node = fold(self.store.read_all()).nodes[a["node_id"]]
            self.store.append("node_evaluated", {"node_id": node.id, "generation": node.attempt,
                                                 "metric": 1.0, "violations": []})

    monkeypatch.setattr(Engine, "_dispatch_evals", evaluate)
    assert _drain(rd).exit_code == 0
    assert handed == batches


def _host_graded_log(tmp_path, *, finished):
    """Two evaluated nodes on a host-graded run whose split is salted by the search epoch."""
    store = EventStore(tmp_path / "hg" / "events.jsonl")
    (tmp_path / "hg").mkdir(parents=True, exist_ok=True)
    store.append("run_started", {"run_id": "hg", "task_id": "t", "goal": "g", "direction": "max",
                                 "holdout_fraction": 0.25})
    store.append("host_grading", {"predictions": "predictions.json", "scorer": "accuracy"})
    for nid, metric in ((0, 0.4), (1, 0.5)):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                      "code": f"print({nid})"})
        store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": metric,
                                        "violations": []})
    store.append("run_finished" if finished else "pause",
                 {"reason": "done"} if finished else {})
    store.append("node_reset", {"node_id": 1, "from_stage": "eval", "generation": 0})
    return store.read_all()


def test_a_split_re_carved_since_the_incumbents_were_measured_refuses_the_drain(tmp_path):
    """MEDIUM (critic 2026-09-26, driven): a reset of a FINISHED host-graded run opens a new search
    epoch itself and clears the finish, so the finished-run clause never saw it, and the drain scored
    the reset node on re-carved rows against incumbents measured on the old ones. Decided from the
    epoch each incumbent was MEASURED in (`engine/run_boundary.py::measured_epochs`)."""
    from looplab.engine.run_boundary import classify_prior_run, measured_epochs

    paused = _host_graded_log(tmp_path / "p", finished=False)
    state = fold(paused)
    assert state.search_epoch == 0 and measured_epochs(paused) == {0: 0, 1: 0}
    assert drain_only_refusal(state, classify_prior_run(state, paused), paused) is None, (
        "the split never moved: the drain proceeds")

    reopened = _host_graded_log(tmp_path / "f", finished=True)
    state = fold(reopened)
    assert state.search_epoch == 1 and not state.finished, "the reset reopened it, epoch and all"
    code, message = drain_only_refusal(state, classify_prior_run(state, reopened), reopened)
    assert code == 2 and "re-carved (search epoch 1) after node(s) 0 were measured" in message


def test_a_reset_of_a_finished_host_graded_run_is_not_drained_across_its_split(
        tmp_path, monkeypatch):
    """The CLI end of the same rule: finish, reset, drain — refused before anything is appended."""
    _isolated(monkeypatch, tmp_path)
    rd, store = _finished_run(tmp_path)
    store.append("host_grading", {"predictions": "predictions.json", "scorer": "accuracy"})
    _reset(store, 1)
    before = [e.seq for e in store.read_all()]
    out = _drain(rd)
    assert out.exit_code == 2, out.output
    assert "was re-carved (search epoch 1) after node(s) 0, 2, 3 were measured" in out.output
    assert [e.seq for e in store.read_all()] == before


def test_a_drain_acks_only_what_it_serves_and_says_it_is_a_drain(tmp_path):
    """LOW (critic 2026-09-26): a drain acked every marked intent it folded — a fork or an inject
    then read "applied — the engine is processing it" though the drain paused without it, and the
    search that followed never re-acks an acked intent. It acks what it serves, marked
    `drain_only`; the rest waits for that search."""
    from tests.factories import make_engine

    for drain in (True, False):
        eng = make_engine(tmp_path / f"run-{drain}", drain_only=drain)
        eng.store.append("node_reset", {"node_id": 0, "from_stage": "eval", "generation": 0,
                                        "_command_id": "reset-cmd"})
        eng.store.append("fork", {"node_id": 0, "_command_id": "fork-cmd"})
        eng._ack_commands(eng.store.read_all())
        acks = {e.data["command_id"]: e.data for e in eng.store.read_all()
                if e.type == "command_ack"}
        if drain:
            assert set(acks) == {"reset-cmd"} and acks["reset-cmd"]["drain_only"] is True
        else:
            assert set(acks) == {"reset-cmd", "fork-cmd"}
            assert all("drain_only" not in ack for ack in acks.values())


def test_an_ignored_terminal_measures_nothing(tmp_path):
    """LOW (critic 2026-09-26, driven): `measured_epochs` recorded the current epoch for ANY later
    `node_evaluated` row while the node was evaluated — an ignored duplicate after a reopen marked a
    stale incumbent as measured on the new rows, and the drain went ahead across the split."""
    from looplab.engine.run_boundary import classify_prior_run, measured_epochs

    root = tmp_path / "dup"
    root.mkdir()
    store = EventStore(root / "events.jsonl")
    store.append("run_started", {"run_id": "dup", "task_id": "t", "goal": "g", "direction": "max",
                                 "holdout_fraction": 0.25})
    store.append("host_grading", {"predictions": "predictions.json", "scorer": "accuracy"})
    for nid, metric in ((0, 0.4), (1, 0.5)):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                      "code": f"print({nid})"})
        store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": metric,
                                        "violations": []})
    store.append("run_finished", {"reason": "done"})
    store.append("resume", {})                       # a reopen: search epoch 1
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 0.9,
                                    "violations": []})   # a duplicate the fold ignores
    store.append("pause", {})
    store.append("node_reset", {"node_id": 1, "from_stage": "eval", "generation": 0})
    events = store.read_all()
    state = fold(events)
    assert state.search_epoch == 1 and state.nodes[0].metric == 0.4, "the duplicate is ignored"
    assert measured_epochs(events)[0] == 0, "node 0 was measured in epoch 0, not re-measured"
    refused = drain_only_refusal(state, classify_prior_run(state, events), events)
    assert refused is not None and "after node(s) 0 were measured" in refused[1], refused
