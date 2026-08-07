"""Defect B: a provider that dies MID-RUN must be noticed on the PROPOSAL path.

The repair path already handles this correctly — a `developer_crash` terminal plus a run-level pause
whose reason names the provider, resumable once it is back (verified live, `/tmp/ll-s4d/run`). The
Researcher/proposal path had no equivalent, and every LLM role degrades on purpose, so the failure
was invisible. Measured live (`/tmp/ll-s4b/run`, provider killed after node 0 evaluated): the run did
NOT pause; fifteen seconds later it had built three more nodes with byte-identical bounds-midpoint
params `{iters: 255.0, l2: 0.5, lr: 0.5005}`, spliced

    "fallback (agent parse failed: LLM request to http://127.0.0.1:8933/v1 failed: Connection error.)"

into the hypothesis board, the node rationale and the research memo, wrote the winner into CROSS-RUN
MEMORY, printed `finished=True  nodes=4  BEST node 3: metric=0.925`, emitted `run_finished` with no
reason at all, and exited 0.

What these tests pin is the shape copied from the repair path: the non-proposal never becomes a Card
or a node, the run freezes with a reason naming the provider, and a resume continues cleanly.
"""
from __future__ import annotations

import anyio
import pytest

from looplab.agents.roles import (
    RESEARCHER_FALLBACK_PREFIX, is_researcher_fallback, researcher_fallback_cause,
    researcher_fallback_rationale)
from looplab.core.models import Idea
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_ADDED, EV_NODE_CREATED, EV_PAUSE, EV_RESUME
from tests.factories import make_engine


_DEAD = "LLM request to http://127.0.0.1:8933/v1 failed: Connection error."


class _DeadProviderResearcher:
    """The exact Idea a Researcher returns when its provider is unreachable.

    Not a mock of the transport: this is what `ToolUsingResearcher._fallback` / `LLMResearcher.propose`
    actually construct after their own retries, which is the object the engine has to recognise.
    """

    def __init__(self, *, revive_after: int = 0):
        self.calls = 0
        self.revive_after = revive_after

    def propose(self, _state, _parent) -> Idea:
        self.calls += 1
        if self.calls > self.revive_after:
            return Idea(operator="draft", params={},
                        rationale=researcher_fallback_rationale("agent parse failed", _DEAD))
        return Idea(operator="draft", params={"x": 0.5, "y": 0.5},
                    rationale="a real proposal", hypothesis="x=0.5 improves the objective")


def test_the_sentinel_round_trips_through_both_researchers():
    plain = Idea(operator="draft", params={},
                 rationale=researcher_fallback_rationale("parse failed", "all parsers failed"))
    agentic = Idea(operator="draft", params={},
                   rationale=researcher_fallback_rationale("agent parse failed", _DEAD))
    assert is_researcher_fallback(plain) and is_researcher_fallback(agentic)
    assert researcher_fallback_cause(agentic) == f"agent parse failed: {_DEAD}"
    # Byte-compatible with every log written before the constant existed.
    assert plain.rationale.startswith("fallback (parse failed: ")
    assert agentic.rationale.startswith("fallback (agent parse failed: ")
    assert RESEARCHER_FALLBACK_PREFIX == "fallback ("
    # A real proposal is never mistaken for one, and neither is a foreign object.
    assert not is_researcher_fallback(Idea(operator="draft", rationale="raise the learning rate"))
    assert not is_researcher_fallback(object())
    assert not is_researcher_fallback(None)


def test_a_degraded_proposal_never_becomes_a_card_or_a_node(tmp_path):
    engine = make_engine(tmp_path / "dead-provider", researcher=_DeadProviderResearcher())
    engine.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                        "direction": "min"})
    state = fold(engine.store.read_all())
    assert engine._prepare_node_idea(
        {"kind": "draft"}, state, researcher=engine.researcher,
        prospective_node_id=0, source="researcher") is None, (
        "a degraded FALLBACK is the ABSENCE of a proposal; nothing may be minted from it")
    events = engine.store.read_all()
    assert not [event for event in events if event.type == EV_CARD_ADDED]
    assert not [event for event in events if event.type == EV_NODE_CREATED]


@pytest.mark.parametrize("main_task", [True, False])
def test_the_pause_names_the_provider_and_is_run_global(tmp_path, main_task):
    engine = make_engine(tmp_path / f"pause-{main_task}", researcher=_DeadProviderResearcher())
    engine.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                        "direction": "min"})
    idea = engine.researcher.propose(None, None)
    assert engine._refuse_degraded_proposal(idea, main_task=main_task) is True
    if not main_task:
        # The worker-thread discipline QUEUES; only the main task may append the run-global event.
        assert fold(engine.store.read_all()).paused is False
        engine._drain_create_pause()

    paused = fold(engine.store.read_all())
    assert paused.paused is True
    pause = [event for event in engine.store.read_all() if event.type == EV_PAUSE][-1]
    reason = pause.data["reason"]
    assert "127.0.0.1:8933" in reason and "Connection error" in reason
    assert "resume" in reason
    # NODE-LESS: `replay.py::_on_pause` drops a pause naming a node unless that node is already
    # failed with `error_reason == "developer_crash"`. There is no node here, so naming one would
    # append a pause the fold silently ignores — the same invisible failure as the defect itself.
    assert "node_id" not in pause.data
    assert paused.pause_node_id is None


def test_a_run_whose_provider_dies_pauses_with_no_champion_and_no_case(tmp_path):
    engine = make_engine(tmp_path / "run-pauses", researcher=_DeadProviderResearcher(),
                         max_nodes=4, n_seeds=2)
    anyio.run(engine.run)
    state = fold(engine.store.read_all())
    assert state.paused is True, "a dead provider on the proposal path must FREEZE the run"
    assert state.finished is False, (
        "freeze, not finish — a finished run writes the report, the reflection note and the "
        "durable cross-run case over experiments that were never proposed")
    assert not state.nodes and state.best() is None
    assert not [e for e in engine.store.read_all() if e.type == EV_CARD_ADDED]
    # The whole point of freezing: `looplab resume` continues once the endpoint is back.
    assert [e for e in engine.store.read_all() if e.type == EV_PAUSE]


def test_the_experiments_already_run_survive_the_freeze(tmp_path):
    """The provider dying at proposal N must not cost the run nodes 0..N-1, or their champion."""
    researcher = _DeadProviderResearcher(revive_after=1)
    engine = make_engine(tmp_path / "partial", researcher=researcher, max_nodes=4, n_seeds=1)
    anyio.run(engine.run)
    state = fold(engine.store.read_all())
    assert state.paused is True and state.finished is False
    assert len(state.nodes) == 1 and state.best() is not None
    assert all(not is_researcher_fallback(node.idea) for node in state.nodes.values()), (
        "a fallback rationale reached a durable node")
    # Resuming with a healthy provider continues the same run rather than starting over.
    researcher.revive_after = 99
    engine.store.append(EV_RESUME, {})
    resumed = make_engine(tmp_path / "partial", researcher=researcher, max_nodes=4, n_seeds=1)
    anyio.run(resumed.run)
    after = fold(resumed.store.read_all())
    assert after.finished is True
    assert len(after.nodes) > 1 and after.best() is not None


def test_the_card_staging_lane_refuses_it_too(tmp_path):
    """The lane that actually authored the live poisoned Cards.

    `/tmp/ll-s4b/run` ran at the shipped defaults — `card_driven_selection=True` — so each degraded
    proposal became a `card_added` whose STATEMENT was the transport error, and only then a
    `card_build_requested` and a node. `_stage_card_creates` reaches `_stage_prepared_card` without
    crossing `_prepare_node_idea`'s `_link` funnel for batch proposals, so it carries its own guard.
    """
    engine = make_engine(tmp_path / "card-lane", researcher=_DeadProviderResearcher(),
                         card_driven_selection=True, max_nodes=4, n_seeds=2)
    engine.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                        "direction": "min", "card_driven_selection": True})
    state = fold(engine.store.read_all())
    assert engine._stage_card_creates([{"kind": "draft"}], state) == []
    events = engine.store.read_all()
    assert not [event for event in events if event.type == EV_CARD_ADDED], (
        "the transport error reached the durable Card board as a hypothesis STATEMENT")
    assert fold(events).paused is True


def test_the_batch_staging_lane_refuses_it_too_including_its_audit_rows(tmp_path):
    """The SECOND door into the same board, opened on 2026-08-07.

    A one-action lane goes through `_prepare_node_idea`'s `_link` funnel; a MULTI-action one goes
    through `_consume_batch_proposal`, and its rejected proposals are audited as node-less closed
    Cards. Against a dead provider every one of those "rejected proposals" IS the transport error, so
    the audit wrote five `card_added` rows carrying it as the hypothesis STATEMENT — after the
    circuit breaker had already refused the batch and paused the run.

    Unreachable until the prefetch-off staging lane widened from `stageable[:1]` to the whole batch
    (the seed/population width). Same rule as every other lane here: a degraded fallback is the
    ABSENCE of a proposal, so it is not a rejected one either.
    """
    engine = make_engine(tmp_path / "card-batch-lane", researcher=_DeadProviderResearcher(),
                         card_driven_selection=True, max_nodes=4, n_seeds=2)
    engine.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                        "direction": "min", "card_driven_selection": True})
    state = fold(engine.store.read_all())

    assert engine._stage_card_creates([{"kind": "draft"}, {"kind": "draft"}], state) == []
    events = engine.store.read_all()
    assert not [event for event in events if event.type == EV_CARD_ADDED], (
        "the batch lane's dropped-proposal audit put the transport error on the Card board")
    assert fold(events).paused is True

    # …and a genuinely rejected proposal still gets its audit Card: the guard is on the FALLBACK
    # sentinel, not on "the batch dropped something".
    real = Idea(operator="draft", params={"x": 0.5, "y": 0.5}, rationale="a real proposal",
                hypothesis="x=0.5 improves the objective")
    engine._record_dropped_batch_cards([{"idea": real, "reason": "duplicate"}])
    assert [event for event in engine.store.read_all() if event.type == EV_CARD_ADDED]


def test_a_card_driven_run_against_a_dead_provider_pauses(tmp_path):
    engine = make_engine(tmp_path / "card-run", researcher=_DeadProviderResearcher(),
                         card_driven_selection=True, max_nodes=4, n_seeds=2)
    anyio.run(engine.run)
    state = fold(engine.store.read_all())
    assert state.paused is True and state.finished is False
    assert not state.nodes and not state.cards and state.best() is None


def test_one_dead_provider_writes_exactly_one_pause(tmp_path):
    """A single loop turn can refuse the same non-proposal twice — once in the staging lane, once in
    the serial compatibility try that follows a rejected staging attempt. Both refusals are correct;
    two identical `pause` rows for one dead provider are noise in the log the operator reads.
    Measured on the live reproduction (`/tmp/ll-fixb/run`, provider killed at call 6): seq 10 AND 11,
    byte-identical reasons."""
    engine = make_engine(tmp_path / "one-pause", researcher=_DeadProviderResearcher(),
                         card_driven_selection=True, max_nodes=4, n_seeds=2)
    anyio.run(engine.run)
    pauses = [e for e in engine.store.read_all() if e.type == EV_PAUSE]
    assert len(pauses) == 1, f"{len(pauses)} pause rows for one dead provider"
    assert fold(engine.store.read_all()).paused is True


def test_a_mechanical_repair_of_a_legacy_fallback_node_does_not_raise_a_false_pause(tmp_path):
    """The repair operator COPIES the failing parent's Idea, rationale and all.

    A log written before this change carries nodes whose rationale IS the old fallback string
    (`/tmp/ll-s4b/run` has three). Resuming or replaying such a run and debugging one of them must not
    raise a provider pause naming a failure that is not happening now — the breaker belongs on a fresh
    PROPOSAL, and neither the repair copy nor the merge operator's mean/ensemble Idea is one.
    """
    class _NeverProposes:
        def propose(self, *_args, **_kwargs):
            raise AssertionError("the repair path must reuse the parent's Idea, not propose")

    class _Repairing:
        is_code_generating = True

        def implement(self, _idea):
            return "print(1)"

        def repair(self, *_args, **_kwargs):
            return "print(1)"

    engine = make_engine(tmp_path / "legacy-debug", researcher=_NeverProposes(),
                         developer=_Repairing())
    engine.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                        "direction": "min"})
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft", "code": "print(1)",
        "idea": {"operator": "draft", "params": {"x": 0.0, "y": 0.0},
                 # verbatim from the live log this defect was found in
                 "rationale": "fallback (agent parse failed: LLM request … Connection error.)"}})
    engine.store.append("node_failed", {"node_id": 0, "generation": 0, "error": "boom",
                                        "reason": "crash", "eval_seconds": 0.1})
    state = fold(engine.store.read_all())
    assert is_researcher_fallback(state.nodes[0].idea)     # the legacy node really is one

    idea = engine._prepare_node_idea(
        {"kind": "debug", "parent_id": 0}, state, researcher=engine.researcher,
        prospective_node_id=1, source="researcher")
    assert idea is not None, "the repair path was refused as if it were a degraded proposal"
    assert fold(engine.store.read_all()).paused is False
