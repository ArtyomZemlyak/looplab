"""`looplab resume --drain-only` (doc 68 68.3a): evaluate what is owed, then pause.

A rescore — `node_reset {from_stage: "eval"}` — could not be run without resuming the whole search:
a resumed run evaluated the reset node and went on creating nodes, consulting and researching.
Driven through the real CLI on a real toy run, read back off the run's own log.
"""
from __future__ import annotations

from typer.testing import CliRunner

from looplab.cli import app
from looplab.engine.orchestrator import (DRAIN_ONLY_PAUSE_REASON, DRAIN_ONLY_STUCK_REASON, Engine,
                                         drain_owed)
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

_RUN = ["run", "--no-genesis", "--kind", "quadratic", "--goal", "min (x-3)^2", "--direction", "min",
        "--backend", "toy", "--max-nodes", "4"]


def _finished_run(tmp_path):
    rd = tmp_path / "run"
    out = CliRunner().invoke(app, [*_RUN, "--out", str(rd)])
    assert out.exit_code == 0, out.output
    store = EventStore(rd / "events.jsonl")
    state = fold(store.read_all())
    assert state.finished and len(state.nodes) == 4
    return rd, store


def test_a_rescore_is_evaluated_and_nothing_else_happens(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(tmp_path / "kn"))
    rd, store = _finished_run(tmp_path)
    before = fold(store.read_all())
    node = before.nodes[1]
    store.append("node_reset", {"node_id": 1, "from_stage": "eval", "generation": node.attempt})
    mark = store.read_all()[-1].seq
    # The research overlap is ASKED for on every ordinary dispatch and is a no-op when nothing is
    # due, so its rows alone cannot show the drain never asked: count the asks.
    asked = []
    monkeypatch.setattr(Engine, "_spawn_research", lambda self, tg, state: asked.append(1) or False)

    out = CliRunner().invoke(app, ["resume", str(rd), "--drain-only"])
    assert out.exit_code == 0, out.output

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
                        "eval_noise_floor", "run_finished"}, sorted(kinds)
    assert asked == [], "the drain's dispatch overlapped a research think"
    # It ends PAUSED, saying why, so the next plain `resume` continues the search.
    assert after.paused
    pauses = [e.data for e in tail if e.type == "pause"]
    assert pauses and pauses[-1].get("reason") == DRAIN_ONLY_PAUSE_REASON


def test_with_nothing_pending_it_only_pauses(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(tmp_path / "kn"))
    rd, store = _finished_run(tmp_path)
    mark = store.read_all()[-1].seq
    out = CliRunner().invoke(app, ["resume", str(rd), "--drain-only"])
    assert out.exit_code == 0, out.output
    tail = [e for e in store.read_all() if e.seq > mark]
    # The resume lift, the entry's own prior receipts and the loop's exit receipt bracket it; the
    # one decision in between is the pause.
    bookkeeping = {"resume", "resume_served", "prior_injected", "run_loop_exited"}
    assert [e.type for e in tail if e.type not in bookkeeping] == ["pause"], [e.type for e in tail]
    assert fold(store.read_all()).paused


def test_what_the_drain_owes_is_a_reset_or_an_interrupted_evaluation(tmp_path, monkeypatch):
    """`drain_owed` over the REAL fold of a real run's log, one control event at a time."""
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(tmp_path / "kn"))
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
    is a SEARCH decision: the drain leaves it pending for the next plain resume."""
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(tmp_path / "kn"))
    rd, store = _finished_run(tmp_path)
    created = next(e.data for e in store.read_all() if e.type == "node_created"
                   and e.data.get("node_id") == 3)
    store.append("node_created", {**created, "node_id": 4, "parent_ids": [3],
                                  "parent_generations": {"3": 0}})
    mark = store.read_all()[-1].seq
    out = CliRunner().invoke(app, ["resume", str(rd), "--drain-only"])
    assert out.exit_code == 0, out.output
    tail = [e for e in store.read_all() if e.seq > mark]
    after = fold(store.read_all())
    assert after.nodes[4].status.value == "pending"
    assert not any(e.type in ("node_eval_started", "node_evaluated", "node_failed") for e in tail)
    assert [e.data.get("reason") for e in tail if e.type == "pause"] == [DRAIN_ONLY_PAUSE_REASON]


def test_a_dispatch_that_admits_nothing_pauses_instead_of_spinning(tmp_path, monkeypatch):
    """The eval budget's reservation rule can refuse a lane before the spent seconds reach the
    ceiling the loop head tests, and the drain then handed the same node to the dispatch on every
    turn, forever (found by a mutant that hung). Driven with a dispatch that admits nothing."""
    monkeypatch.setenv("LOOPLAB_MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("LOOPLAB_KNOWLEDGE_DIR", str(tmp_path / "kn"))
    rd, store = _finished_run(tmp_path)
    store.append("node_reset", {"node_id": 1, "from_stage": "eval",
                                "generation": fold(store.read_all()).nodes[1].attempt})
    mark = store.read_all()[-1].seq
    calls = []

    async def admits_nothing(self, evals, state, max_es, *, research=True):
        calls.append([a["node_id"] for a in evals])
        if len(calls) > 3:          # a spin, not a hang: fail loudly instead of looping forever
            raise AssertionError(f"the drain spun: {calls}")

    monkeypatch.setattr(Engine, "_dispatch_evals", admits_nothing)
    out = CliRunner().invoke(app, ["resume", str(rd), "--drain-only"])
    assert out.exit_code == 0, out.output
    assert calls == [[1]], calls
    tail = [e for e in store.read_all() if e.seq > mark]
    assert [e.data.get("reason") for e in tail if e.type == "pause"] == [
        DRAIN_ONLY_STUCK_REASON.format(ids="1")]
    assert fold(store.read_all()).nodes[1].status.value == "pending"
