"""The shared node-creation commit epilogue (doc 25 ES-02).

`_create_node_scoped`, `_rerun_node` and `_create_injected_node` each hand-coded the same
three-stage epilogue — refetch the parents, emit `node_created`, re-fold and check it landed — and
the copies had already forced the SAME false-success fix to be applied three separate times. The
epilogue is now `Engine._commit_built_node`, and this file holds what that helper must do.

Every test here drives the real helper over a real event log and reads the fold or the raw rows: the
properties are "which terminal was written and what the payload looked like", which no call-presence
pin can distinguish from a path that re-inlines the epilogue slightly differently. The one AST test
at the bottom holds the SINGLE-SOURCING itself, which is the finding's actual content — it is the
guard that goes red when a fourth creation path (or a re-inlined one) grows its own copy.
"""
from __future__ import annotations

import ast

import pytest

from looplab.core.models import Idea, durable_idea_payload
from looplab.engine import orchestrator as orch
from looplab.engine.orchestrator import Engine, parent_generations_current
from looplab.events.replay import fold
from looplab.events.types import EV_NODE_CREATED, EV_NODE_FAILED

from tests._source_scan import called_names
from tests.factories import make_engine


class _State:
    """The two fields `parent_generations_current` reads, and nothing else."""

    def __init__(self, nodes, aborted=()):
        self.nodes = nodes
        self.aborted_nodes = set(aborted)


class _Node:
    def __init__(self, node_id, attempt=0, tombstoned=False):
        self.id = node_id
        self.attempt = attempt
        self.tombstoned = tombstoned


def _idea(**over) -> Idea:
    base = dict(operator="draft", params={"x": 1.0, "y": 2.0}, rationale="because")
    base.update(over)
    return Idea(**base)


def _engine(tmp_path, **overrides):
    return make_engine(tmp_path / "run", **overrides)


def _reserve(engine, idea, action=None):
    reservation = engine._reserve_node_build(action or {"kind": "draft", "parent_ids": []}, idea)
    assert reservation is not None
    return reservation


def _rows(engine, event_type):
    return [e.data for e in engine.store.read_all() if e.type == event_type]


# --------------------------------------------------------------------------- the parent fence rule

@pytest.mark.parametrize("nodes, aborted, generations, current", [
    ({}, (), {}, True),                                      # a seed build names no parent
    ({0: _Node(0, attempt=0)}, (), {}, True),
    ({0: _Node(0, attempt=0)}, (), {"0": 0}, True),
    ({1: _Node(1)}, (), {"0": 0}, False),                    # the parent id is gone entirely
    ({0: _Node(0, attempt=1)}, (), {"0": 0}, False),         # reset while we built
    ({0: _Node(0, tombstoned=True)}, (), {"0": 0}, False),
    ({0: _Node(0)}, (0,), {"0": 0}, False),                  # operator aborted it mid-build
    ({0: _Node(0), 1: _Node(1, attempt=2)}, (), {"0": 0, "1": 1}, False),   # ONE stale parent is stale
])
def test_parent_generations_current_truth_table(nodes, aborted, generations, current):
    """Each clause is load-bearing, and the three copies spelled it two different ways."""
    assert parent_generations_current(_State(nodes, aborted), generations) is current


def test_parent_generations_current_reads_string_parent_ids():
    """A durable payload's parent ids arrive as JSON object keys, i.e. strings."""
    state = _State({0: _Node(0, attempt=3)})
    assert parent_generations_current(state, {"0": 3}) is True
    assert parent_generations_current(state, {"0": 2}) is False


# ------------------------------------------------------------------------------- the epilogue drive

def test_stale_parent_closes_the_reservation_and_writes_no_node(tmp_path):
    """A build whose parent moved must not become a node, and must not leave a bare marker."""
    engine = _engine(tmp_path)
    seed = _reserve(engine, _idea())
    assert engine._commit_built_node(
        node_id=seed.node_id, generation=0, card_id=seed.card_id,
        parents=[], parent_generations={}, idea=seed.idea, code="print(1)",
        files={}, deleted=[], footprint_finalized=False,
        stale_error="unused", rejected_error="unused") is True

    child = _reserve(engine, _idea(operator="improve"),
                     action={"kind": "improve", "parent_ids": [seed.node_id]})
    engine.researcher.last_hyp_priority = {"stale": True}
    committed = engine._commit_built_node(
        node_id=child.node_id, generation=0, card_id=child.card_id,
        parents=[seed.node_id],
        # The parent is on generation 0; this build was reserved against a generation that a reset
        # would have produced, which is exactly the race the refetch exists for.
        parent_generations={seed.node_id: 1},
        idea=child.idea, code="print(2)", files={}, deleted=[], footprint_finalized=False,
        stale_error="parent lifecycle changed while building",
        rejected_error="node creation was rejected during replay")

    assert committed is False
    state = fold(engine.store.read_all())
    assert child.node_id not in state.nodes                  # no node evidence was created
    failed = [row for row in _rows(engine, EV_NODE_FAILED) if row["node_id"] == child.node_id]
    assert len(failed) == 1
    assert failed[0]["error"] == "parent lifecycle changed while building"
    assert failed[0]["reason"] == "superseded"
    # The abandoned build's telemetry is discarded, or it lands on the NEXT node created.
    assert engine.researcher.last_hyp_priority is None


def test_commit_omits_the_generation_key_unless_the_caller_stamps_it(tmp_path):
    """A first creation's payload shape is historical; only a rerun writes `generation`."""
    engine = _engine(tmp_path)
    first = _reserve(engine, _idea())
    assert engine._commit_built_node(
        node_id=first.node_id, generation=0, card_id=first.card_id,
        parents=[], parent_generations={}, idea=first.idea, code="print(1)",
        files={"main.py": "print(1)"}, deleted=[], footprint_finalized=True,
        stale_error="unused", rejected_error="unused",
        source="manual") is True
    created = _rows(engine, EV_NODE_CREATED)[-1]
    assert "generation" not in created
    assert created["source"] == "manual"                     # `**emit_extra` reaches the payload
    assert created["eval_start_boundary"] is True
    assert created["footprint_finalized"] is True

    second = _reserve(engine, _idea(rationale="a second, distinct question"))
    assert engine._commit_built_node(
        node_id=second.node_id, generation=0, card_id=second.card_id,
        parents=[], parent_generations={}, idea=second.idea, code="print(2)",
        files={}, deleted=[], footprint_finalized=False,
        stale_error="unused", rejected_error="unused",
        stamp_generation=True) is True
    assert _rows(engine, EV_NODE_CREATED)[-1]["generation"] == 0


def test_an_unknown_emit_key_raises_instead_of_entering_the_payload(tmp_path):
    """`**emit_extra` is not a payload escape hatch — the emitter's signature is the contract."""
    engine = _engine(tmp_path)
    reservation = _reserve(engine, _idea())
    with pytest.raises(TypeError):
        engine._commit_built_node(
            node_id=reservation.node_id, generation=0, card_id=reservation.card_id,
            parents=[], parent_generations={}, idea=reservation.idea, code="print(1)",
            files={}, deleted=[], footprint_finalized=False,
            stale_error="unused", rejected_error="unused",
            reserch_origin={"at_node": 0})               # a typo for research_origin
    assert not _rows(engine, EV_NODE_CREATED)


@pytest.mark.parametrize("strict, committed", [(True, False), (False, True)])
def test_strict_landing_is_what_separates_a_rebuild_from_a_first_creation(
        tmp_path, monkeypatch, strict, committed):
    """A rerun must see ITS generation land; a first landing only has to exist.

    The landing fold is made to report the node on another lifecycle — the shape a losing worker's
    row leaves behind. With `strict_landing` the rebuild is refused and its reservation closed;
    without it the same state is a perfectly good first creation.
    """
    engine = _engine(tmp_path)
    reservation = _reserve(engine, _idea())
    real_fold = orch.fold
    folds: list[int] = []

    def landing_fold(events):
        state = real_fold(events)
        folds.append(1)
        if len(folds) == 2:                              # the post-emit landing check
            node = state.nodes.get(reservation.node_id)
            if node is not None:
                node.attempt = 9
        return state

    monkeypatch.setattr(orch, "fold", landing_fold)
    result = engine._commit_built_node(
        node_id=reservation.node_id, generation=0, card_id=reservation.card_id,
        parents=[], parent_generations={}, idea=reservation.idea, code="print(1)",
        files={}, deleted=[], footprint_finalized=False,
        stale_error="unused",
        rejected_error="rebuilt node creation was rejected during replay",
        strict_landing=strict)

    assert result is committed
    assert len(folds) == 2                               # exactly one fence fold and one landing fold
    failed = [row for row in _rows(engine, EV_NODE_FAILED)
              if row["node_id"] == reservation.node_id]
    if strict:
        assert len(failed) == 1
        assert failed[0]["error"] == "rebuilt node creation was rejected during replay"
        assert failed[0]["reason"] == "superseded"
    else:
        assert failed == []


def test_strict_landing_refuses_a_node_carrying_another_builds_code(tmp_path, monkeypatch):
    """The code check is the half a generation compare cannot make: same lifecycle, other bytes."""
    engine = _engine(tmp_path)
    reservation = _reserve(engine, _idea())
    real_fold = orch.fold
    folds: list[int] = []

    def landing_fold(events):
        state = real_fold(events)
        folds.append(1)
        if len(folds) == 2:
            node = state.nodes.get(reservation.node_id)
            if node is not None:
                node.code = "print('somebody else')"
        return state

    monkeypatch.setattr(orch, "fold", landing_fold)
    assert engine._commit_built_node(
        node_id=reservation.node_id, generation=0, card_id=reservation.card_id,
        parents=[], parent_generations={}, idea=reservation.idea, code="print(1)",
        files={}, deleted=[], footprint_finalized=False,
        stale_error="unused", rejected_error="rebuilt node creation was rejected during replay",
        strict_landing=True) is False
    assert [row["reason"] for row in _rows(engine, EV_NODE_FAILED)] == ["superseded"]


@pytest.mark.parametrize("recover", [True, False])
def test_only_a_caller_that_asks_recovers_from_an_append_that_raises(tmp_path, monkeypatch,
                                                                    recover):
    """The operator's inject must not leave a bare `node_building`; the agent paths keep their crash.

    Both re-raise — the difference is whether the reservation is closed on the way out. The two
    agent paths deliberately let the exception reach `_create_node_guarded` (parallel) or the test
    suite (serial), which is why `append_failure_error` is opt-in rather than the default.
    """
    engine = _engine(tmp_path)
    reservation = _reserve(engine, _idea())

    def boom(**_kwargs):
        raise RuntimeError("the log refused the append")

    monkeypatch.setattr(engine, "_emit_node_created", boom)
    with pytest.raises(RuntimeError):
        engine._commit_built_node(
            node_id=reservation.node_id, generation=0, card_id=reservation.card_id,
            parents=[], parent_generations={}, idea=reservation.idea, code="print(1)",
            files={}, deleted=[], footprint_finalized=False,
            stale_error="unused", rejected_error="unused",
            **({"append_failure_error": "injected node append failed"} if recover else {}))

    failed = _rows(engine, EV_NODE_FAILED)
    if recover:
        assert len(failed) == 1
        assert failed[0]["error"] == "injected node append failed"
        assert failed[0]["reason"] == "build_crash"
    else:
        assert failed == []


def test_the_committed_payload_is_the_idea_the_caller_handed_over(tmp_path):
    """The helper owns `durable_idea_payload`/`operator`, so a caller cannot desynchronize them."""
    engine = _engine(tmp_path)
    reservation = _reserve(engine, _idea(hypothesis="a widened lr schedule beats the baseline"))
    assert engine._commit_built_node(
        node_id=reservation.node_id, generation=0, card_id=reservation.card_id,
        parents=[], parent_generations={}, idea=reservation.idea, code="print(1)",
        files={}, deleted=["old.py"], footprint_finalized=False,
        stale_error="unused", rejected_error="unused") is True
    created = _rows(engine, EV_NODE_CREATED)[-1]
    assert created["idea"] == durable_idea_payload(reservation.idea)
    assert created["operator"] == reservation.idea.operator
    assert created["deleted"] == ["old.py"]


# ------------------------------------------------------------------------------- the single-sourcing

def test_every_creation_path_commits_through_the_shared_helper():
    """The finding's own content: one epilogue, three callers, no path with its own copy.

    Positive AND negative: each path must CALL `_commit_built_node`, and none of them may still
    spell `_emit_node_created` itself — the second half is what a partial re-inlining would trip.
    """
    paths = (Engine._create_node_scoped, Engine._rerun_node, Engine._create_injected_node)
    for func in paths:
        names = called_names(func)
        assert "self._commit_built_node" in names, f"{func.__name__} does not commit through it"
        assert "self._emit_node_created" not in names, f"{func.__name__} re-spells the emit"


def test_the_shared_helper_folds_through_the_orchestrator_module_seam():
    """`monkeypatch.setattr(orch, "fold", …)` must keep covering the creation epilogue.

    A `from looplab.events.replay import fold` inside `_commit_built_node` would resolve at import
    time and silently narrow every existing interception of the creation paths (doc 25 ES-01's
    account of the same trap in the Card ledger).
    """
    tree = ast.parse(__import__("textwrap").dedent(
        __import__("inspect").getsource(Engine._commit_built_node)))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert called_names(Engine._commit_built_node).count("fold") == 3
