"""A batched proposal's result is a VALUE, not three engine attributes (review 2026-09-22, ENG1-12).

`novelty.py::_propose_batch` used to return only its ideas and hand everything else back through
three engine attributes — `_pending_batch_telemetry`, `_pending_batch_dropped`,
`_pending_batch_novelty_gated` — written from the worker thread the batch runs on, read by
`node_build.py::_consume_batch_proposal`, and reset by seventeen statements at eight sites in
three files, in three different orders. The gate capability was also consumed BY IDENTITY in
`_prepare_node_idea` (`preproposed is batch_idea` -> `already_gated=True`), so a batch whose caller
forgot the reset — or raised before reaching it — left a live novelty-gate bypass on the engine for
the next caller. Driven on the tree before this change: a chunk whose reservation raised after the
paid proposal left all three attributes populated, and the dead batch's Idea then skipped the
novelty gate entirely (0 gate calls) — the chunk path had no `finally`.

It now returns `novelty.py::BatchProposal(ideas, telemetry, dropped, gated)`, and the gate
capability travels explicitly as `already_gated=proposal.crossed_gate(idea)`. With no shared
attribute there is nothing to reset, so what the resets protected (doc 25 ES-08) is proved here by
DRIVING both call sites — `_handle_create_actions`' chunk through a real run, and
`_stage_card_creates` — including an exception mid-batch:
  * each accepted reservation carries ITS OWN roll's telemetry (the padding/alignment rule);
  * the accepted reservations are durable BEFORE the batch's node-less rejects are recorded, and
    every reject is recorded exactly once;
  * nothing of a batch outlives it: after a batch that raised mid-staging, a later batch records
    only its own drops, and an Idea from the dead batch handed to `_create_node` is GATED.
"""
from __future__ import annotations

import functools

import anyio
import pytest

from looplab.adapters.toytask import ToyTask
from looplab.core.models import Idea
from looplab.engine.novelty import BatchProposal
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_ADDED, EV_CARD_AUTO_DROPPED, EV_NODE_BUILDING
from tests.factories import TOY_TASK, make_engine


class _ScriptedResearcher:
    """Roll N proposes x = 0.5 for rolls 1-2 (so roll 2 is an INTRA-BATCH DUPLICATE) and distinct
    points after, and stamps each roll's own steering cue (`seconds == N`) — so every durable
    receipt says which roll's telemetry it carries."""

    def __init__(self):
        self.calls = 0
        self._steering_context: list = []

    def propose(self, state, parent):
        self.calls += 1
        roll = self.calls
        self._steering_context = [{"kind": "experiment_time_budget", "seconds": float(roll)}]
        x = 0.5 if roll <= 2 else 0.5 + roll
        return Idea(operator="draft", params={"x": x, "y": 0.0}, rationale=f"trial {roll}")


def _roll_of(card_added_row) -> float:
    return card_added_row.data["steering_context"][0]["seconds"]


def _batch_attributes(engine) -> set[str]:
    return {name for name, value in vars(engine).items()
            if "batch" in name.lower() and not callable(value)}


def _spy_on_proposals(engine) -> list[BatchProposal]:
    """Record every `BatchProposal` the REAL producer returns (the instance attribute is what
    `_consume_batch_proposal` calls)."""
    real = engine._propose_batch
    seen: list[BatchProposal] = []

    def _spy(state, width):
        proposal = real(state, width)
        seen.append(proposal)
        return proposal

    engine._propose_batch = _spy
    return seen


def _count_gates(engine) -> list[int]:
    real = engine._apply_novelty_gate
    calls: list[int] = []

    def _counting(state, idea, **kwargs):
        calls.append(1)
        return real(state, idea, **kwargs)

    engine._apply_novelty_gate = _counting
    return calls


# ------------------------------------------------------------------ the producer returns a value

@pytest.mark.parametrize("native", [False, True])
def test_the_real_producer_returns_its_whole_result_and_leaves_nothing_on_the_engine(
        tmp_path, native):
    engine = make_engine(tmp_path / "run", n_seeds=3, max_nodes=6)
    engine.store.append("run_started", {"run_id": "r", "task_id": "toy", "direction": "min"})
    engine._novelty_mode = "off"
    scripted = _ScriptedResearcher()
    if native:
        scripted.propose_batch = lambda state, n: [scripted.propose(state, None) for _ in range(n)]
    engine.researcher = scripted
    before = _batch_attributes(engine)

    proposal = engine._propose_batch(fold(engine.store.read_all()), 3)

    assert isinstance(proposal, BatchProposal)
    assert [idea.params["x"] for idea in proposal.ideas] == ([0.5, 3.5] if native
                                                             else [0.5, 3.5, 4.5])
    assert len(proposal.telemetry) == len(proposal.ideas), "telemetry must align 1:1"
    assert [row["reason"] for row in proposal.dropped] == ["intra_batch_duplicate"]
    assert len(proposal.gated) == len(proposal.ideas)
    assert all(proposal.crossed_gate(idea) for idea in proposal.ideas)
    assert _batch_attributes(engine) == before == set(), (
        "a batch result landed on the engine, where a later batch can read it")


def test_the_capability_is_identity_not_equality():
    idea = Idea(operator="draft", params={"x": 1.0}, rationale="a trial")
    proposal = BatchProposal([idea], [None], [], (idea,))
    assert proposal.crossed_gate(idea)
    assert not proposal.crossed_gate(idea.model_copy(deep=True)), (
        "an EQUAL proposal has not itself crossed the gate")
    assert not BatchProposal([idea]).crossed_gate(idea), "no gated set, no capability"


# ------------------------------------------------------------------ call site 1: the chunk

def _chunk_engine(run_dir):
    task = ToyTask.load(TOY_TASK)
    engine = make_engine(run_dir, task=task, n_seeds=4, max_nodes=4)
    engine.parallel_build = 2                  # two lanes -> `_handle_create_actions`' chunk path
    engine.role_factory = task.build_roles
    engine.researcher = _ScriptedResearcher()
    engine._novelty_mode = "off"
    return engine


def test_the_chunk_reserves_each_idea_with_its_own_telemetry_before_recording_its_rejects(tmp_path):
    engine = _chunk_engine(tmp_path / "run")
    anyio.run(engine.run)
    events = engine.store.read_all()
    added = {e.data["id"]: e for e in events if e.type == EV_CARD_ADDED}
    dropped = [e for e in events if e.type == EV_CARD_AUTO_DROPPED
               and e.data.get("reason") == "intra_batch_duplicate"]
    reserved = [e for e in events if e.type == EV_NODE_BUILDING]

    # Every reservation's Card carries ITS OWN roll (the padding rule, through the real chunk):
    # rolls 1 and 3 in the first chunk (2 was the duplicate), 4 and 5 in the second.
    assert [_roll_of(added[e.data["card_id"]]) for e in reserved] == [1.0, 3.0, 4.0, 5.0]
    # The duplicate is recorded exactly once, with its own roll...
    assert len(dropped) == 1 and _roll_of(added[dropped[0].data["id"]]) == 2.0
    # ...and only AFTER the chunk's accepted reservations are durable.
    first_chunk = reserved[:2]
    assert added[dropped[0].data["id"]].seq > max(e.seq for e in first_chunk), (
        "a reject was recorded before the reservations it must not shift")


def test_a_chunk_that_raises_mid_batch_leaves_no_capability_behind(tmp_path, monkeypatch):
    """The chunk path never had a `finally`: a reservation that RAISES after the paid proposal left
    all three attributes populated — and the gate list a live bypass — for whoever came next."""
    engine = _chunk_engine(tmp_path / "run")
    proposals = _spy_on_proposals(engine)
    real_reserve = engine._reserve_node_build
    reservations: list[int] = []

    def _second_raises(*args, **kwargs):
        reservations.append(1)
        if len(reservations) == 2:
            raise RuntimeError("the store failed mid-chunk")
        return real_reserve(*args, **kwargs)

    engine._reserve_node_build = _second_raises
    with pytest.raises(BaseException):
        anyio.run(engine.run)
    assert proposals, "precondition: the chunk proposed before it raised"
    assert _batch_attributes(engine) == set()

    # An Idea of the dead batch, handed to the unreserved compatibility build, is GATED.
    engine._reserve_node_build = real_reserve
    gates = _count_gates(engine)
    stray = proposals[-1].ideas[-1]
    engine._create_node({"kind": "draft"}, preproposed=stray)
    assert gates == [1], "a batch that raised left its novelty-gate bypass on the engine"


# ------------------------------------------------------------------ call site 2: card staging

def _staging_engine(run_dir):
    engine = make_engine(run_dir, card_driven_selection=True, max_nodes=8, n_seeds=2)
    engine.store.append("run_started", {"run_id": "r", "task_id": "toy", "goal": "g",
                                        "direction": "min", "card_driven_selection": True})
    engine.researcher = _ScriptedResearcher()
    engine._novelty_mode = "off"
    return engine


def _stage(engine, width: int) -> list[str]:
    state = fold(engine.store.read_all())
    return anyio.run(functools.partial(
        engine._stage_card_creates, [{"kind": "draft"}] * width, state))


def test_staging_publishes_the_batch_before_recording_its_rejects(tmp_path):
    engine = _staging_engine(tmp_path / "run")
    staged = _stage(engine, 2)
    events = engine.store.read_all()
    added = [e for e in events if e.type == EV_CARD_ADDED]
    dropped = [e for e in events if e.type == EV_CARD_AUTO_DROPPED]
    assert len(staged) == 2 and [_roll_of(e) for e in added] == [1.0, 3.0, 2.0]
    assert [e.data["reason"] for e in dropped] == ["intra_batch_duplicate"]
    assert dropped[0].data["id"] == added[-1].data["id"], "the reject is recorded last, once"


def test_a_staging_batch_that_raises_mid_batch_leaves_nothing_a_later_batch_reads(
        tmp_path, monkeypatch):
    """The `finally` reset in `_stage_card_creates` existed for exactly this: staging raising
    after the paid proposal. There is nothing left to reset — so prove the NEXT batch through the
    same call site records only its own rejects, and that the dead batch's Ideas carry no bypass."""
    engine = _staging_engine(tmp_path / "run")
    proposals = _spy_on_proposals(engine)
    real_stage = engine._stage_prepared_card
    engine._stage_prepared_card = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("staging failed mid-batch"))
    with pytest.raises(RuntimeError, match="mid-batch"):
        _stage(engine, 2)
    dead = proposals[-1]
    assert [row["reason"] for row in dead.dropped] == ["intra_batch_duplicate"], (
        "precondition: the dead batch HAD a reject to leak")
    assert _batch_attributes(engine) == set()

    # Straight after the raise, before anything else can overwrite what it left: an Idea of the dead
    # batch is GATED when it is built — unless its holder states otherwise.
    engine._stage_prepared_card = real_stage
    gates = _count_gates(engine)
    engine._create_node({"kind": "draft"}, preproposed=dead.ideas[0])
    assert gates == [1], "a batch that raised left its novelty-gate bypass behind"
    engine._create_node({"kind": "draft"}, preproposed=dead.ideas[1],
                        already_gated=dead.crossed_gate(dead.ideas[1]))
    assert gates == [1], "the holder's explicit capability must spare the second gate"

    # The next batch through the same call site — rolls 4 and 5, no duplicate — stages its own two
    # Cards and records no reject at all: nothing of the dead batch is recorded late.
    staged = _stage(engine, 2)
    events = engine.store.read_all()
    added = {e.data["id"]: e for e in events if e.type == EV_CARD_ADDED}
    assert [_roll_of(added[card_id]) for card_id in staged] == [4.0, 5.0]
    assert not [e for e in events if e.type == EV_CARD_AUTO_DROPPED], (
        "a later batch recorded the dead batch's reject")


# ------------------------------------------------------------------ the one consumer

def test_the_compatibility_build_gates_exactly_what_did_not_cross_the_gate(tmp_path):
    engine = make_engine(tmp_path / "run", n_seeds=3, max_nodes=6)
    engine.store.append("run_started", {"run_id": "r", "task_id": "toy", "direction": "min"})
    engine.researcher = _ScriptedResearcher()
    engine._novelty_mode = "off"
    proposal = engine._propose_batch(fold(engine.store.read_all()), 3)
    gates = _count_gates(engine)

    first, second, third = proposal.ideas
    engine._create_node({"kind": "draft"}, preproposed=first,
                        already_gated=proposal.crossed_gate(first))
    assert gates == [], "an Idea the batch gated must not be gated twice"
    copy = second.model_copy(deep=True)
    engine._create_node({"kind": "draft"}, preproposed=copy,
                        already_gated=proposal.crossed_gate(copy))
    assert gates == [1], "an EQUAL copy has not crossed the gate"
    engine._create_node({"kind": "draft"}, preproposed=third)
    assert gates == [1, 1], "no capability without the holder's statement"
