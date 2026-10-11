"""An operator-ABORTED speculative node is not the Card freshness gate's to terminalize.

`_skip_if_aborted` owns an aborted node's terminal: it writes the `reason: "aborted"` row when
dispatch reaches the node. Until then the node folds `pending` and sits in `aborted_nodes`, and the
fold IGNORES any other terminal for it (`replay.py::_on_node_failed`). `_drop_stale_speculation`
used to treat such a node like any stale prefetch: it appended a "superseded by Card freshness gate"
row the fold discarded and returned True ("force a fresh fold") — so the very next turn found the
same pending, still-stale node and did it again, forever. On MiniOneRec v14 (2026-10-11) an aborted
agent prefetch (node 16) wrote ~16.7k ignored node_failed rows and starved dispatch for 1.6 h, with
the GPUs idle, until the engine was killed. These tests drive that state through a real engine.
"""
from __future__ import annotations

import anyio

import looplab.engine.speculation as speculation_module
from looplab.core.models import NodeStatus
from looplab.events.replay import fold
from looplab.events.types import EV_NODE_ABORT, EV_NODE_FAILED

from tests.test_card_speculation_engine import (  # noqa: F401 — the receipt fixture is autouse
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _commit_speculative_node,
    _engine,
    _start,
)


def _aborted_stale_prefetch(tmp_path, monkeypatch):
    engine, _producer = _engine(tmp_path / "aborted-prefetch")
    engine._base_max_nodes = 8
    engine.policy.max_nodes = 8
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    node_id = _commit_speculative_node(engine)
    engine.store.append(EV_NODE_ABORT, {"node_id": node_id, "generation": 0})
    state = fold(engine.store.read_all())
    assert node_id in state.aborted_nodes
    assert state.nodes[node_id].status is NodeStatus.pending
    # Stale on every question asked: before the fix this is the livelock's trigger.
    monkeypatch.setattr(
        speculation_module, "speculative_card_is_fresh", lambda *_a, **_k: False)
    return engine, node_id


def _failed_rows(engine, node_id):
    return [e for e in engine.store.read_all()
            if e.type == EV_NODE_FAILED and (e.data or {}).get("node_id") == node_id]


def test_freshness_gate_leaves_an_aborted_prefetch_to_its_abort_terminal(tmp_path, monkeypatch):
    engine, node_id = _aborted_stale_prefetch(tmp_path, monkeypatch)

    # Asked again and again — as the loop would — it neither claims progress nor writes a row.
    for _ in range(3):
        assert anyio.run(engine._drop_stale_speculation) is False
    assert _failed_rows(engine, node_id) == []
    state = fold(engine.store.read_all())
    assert state.nodes[node_id].status is NodeStatus.pending
    assert node_id in state.aborted_nodes


def test_the_abort_terminal_still_lands_and_folds(tmp_path, monkeypatch):
    """The node is not stranded: dispatch's own abort path terminalizes it, and the fold takes it."""
    engine, node_id = _aborted_stale_prefetch(tmp_path, monkeypatch)
    assert anyio.run(engine._drop_stale_speculation) is False

    state = fold(engine.store.read_all())
    assert engine._skip_if_aborted({"node_id": node_id}, state) is True
    rows = _failed_rows(engine, node_id)
    assert [r.data.get("reason") for r in rows] == ["aborted"]
    after = fold(engine.store.read_all())
    assert after.nodes[node_id].status is NodeStatus.failed
    assert after.nodes[node_id].error_reason == "aborted"
    # And once terminal it is no longer a pending prefetch the gate could revisit.
    assert anyio.run(engine._drop_stale_speculation) is False
    assert len(_failed_rows(engine, node_id)) == 1
