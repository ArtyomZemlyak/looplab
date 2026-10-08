"""LANE_QUEUE_APPENDABLE: the live upstream lane's queue is a DECLARED writer beside a live engine.

Engine invariant #1 names who may append FOLDED events while the engine runs. The live upstream lane
(doc 73 §2.5) added one more and declared it nowhere: `UpstreamLane._queue_if_live` appends
`lane_op_requested` from the API's upstream routes, the Assistant's upstream tools and MCP while an
engine is ALIVE (critic 2026-10-08). It is not a control intent — the lane validates the body itself
under its own lock — so it is registered on its own, and these tests turn the prose argument into red
tests:

  1. registry sanity — registered, folded (not diagnostic), and disjoint from every other writer seam;
  2. the writers — re-derived by AST over the whole package: the ONE writer is `_queue_if_live`, and
     its append names the registered constant;
  3. splice neutrality — folding the SAME log with the request spliced at every position its own
     receipt allows yields the identical state.
"""
from __future__ import annotations

import ast
from pathlib import Path

from looplab.core.models import Event
from looplab.events.replay import fold
from looplab.events.types import (ALL_EVENT_TYPES, ASSISTANT_APPENDABLE, BACKGROUND_APPENDABLE,
                                   DIAGNOSTIC_EVENTS, EV_LANE_OP_REQUESTED, LANE_QUEUE_APPENDABLE,
                                   NON_CARD_SELECTION_BACKGROUND_APPENDABLE, SETUP_THREAD_APPENDABLE)
from looplab.serve.protocol import CONTROL_EVENTS

PKG = Path(__file__).resolve().parents[1] / "looplab"


def test_the_registry_is_the_queue_row_alone_and_disjoint_from_every_other_seam():
    assert LANE_QUEUE_APPENDABLE == frozenset({EV_LANE_OP_REQUESTED})
    assert LANE_QUEUE_APPENDABLE <= ALL_EVENT_TYPES
    assert not LANE_QUEUE_APPENDABLE & DIAGNOSTIC_EVENTS, "a FOLDED row — that is why it is declared"
    for other in (CONTROL_EVENTS, ASSISTANT_APPENDABLE, BACKGROUND_APPENDABLE,
                  SETUP_THREAD_APPENDABLE, NON_CARD_SELECTION_BACKGROUND_APPENDABLE):
        assert not LANE_QUEUE_APPENDABLE & set(other)


def _writers() -> set[str]:
    """`<module>::<function>` of every call whose first argument names the queue row — the
    `EV_LANE_OP_REQUESTED` constant or its literal — by AST, so a comment can satisfy nothing."""
    found = set()
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in {"append", "append_many"} and node.args):
                    continue
                first = node.args[0]
                if ((isinstance(first, ast.Name) and first.id == "EV_LANE_OP_REQUESTED")
                        or (isinstance(first, ast.Constant) and first.value == EV_LANE_OP_REQUESTED)):
                    found.add(f"{path.relative_to(PKG).as_posix()}::{fn.name}")
    return found


def test_the_one_writer_is_the_lanes_queue_and_it_asserts_its_membership():
    assert _writers() == {"engine/upstream.py::_queue_if_live"}
    source = (PKG / "engine" / "upstream.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(source))
              if isinstance(n, ast.FunctionDef) and n.name == "_queue_if_live")
    asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
    assert any(isinstance(a.test, ast.Compare) and isinstance(a.test.left, ast.Name)
               and a.test.left.id == "EV_LANE_OP_REQUESTED"
               and any(isinstance(c, ast.Name) and c.id == "LANE_QUEUE_APPENDABLE"
                       for c in a.test.comparators) for a in asserts), "membership asserted at the site"


def _log(request_at: int) -> list[Event]:
    """A run with two served queue entries, then a third REQUEST spliced at `request_at` among the
    rows after the second receipt (its earliest legal position) — nodes created and evaluated, a
    control intent and an upstream row around it."""
    head = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "min"}),
        (EV_LANE_OP_REQUESTED, {"op": "check", "action_id": "a", "request_hash": "0" * 64,
                                "body": {"action_id": "a"}}),
        ("lane_op_done", {"idx": 0, "op": "check", "action_id": "a", "outcome": "refused",
                          "code": "upstream_proposal_missing"}),
        (EV_LANE_OP_REQUESTED, {"op": "advance", "action_id": "b", "request_hash": "1" * 64,
                                "body": {"action_id": "b"}}),
        ("lane_op_done", {"idx": 1, "op": "advance", "action_id": "b", "outcome": "succeeded"}),
    ]
    tail = [
        ("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                          "idea": {"operator": "draft", "params": {"x": 1.0}}, "code": ""}),
        ("node_evaluated", {"node_id": 0, "metric": 5.0, "eval_seconds": 0.1}),
        ("hint", {"text": "try smaller x", "kind": "steer"}),
        ("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                          "idea": {"operator": "improve", "params": {"x": 2.0}}, "code": ""}),
        ("upstream_auto_set", {"enabled": False}),
        ("node_evaluated", {"node_id": 1, "metric": 1.0, "eval_seconds": 0.1}),
    ]
    request = (EV_LANE_OP_REQUESTED, {"op": "propose", "action_id": "c", "request_hash": "2" * 64,
                                      "proposal_id": "up_c", "request_path": "upstream/requests/c"})
    rows = head + tail[:request_at] + [request] + tail[request_at:]
    return [Event(seq=i, ts=float(i), type=t, data=d) for i, (t, d) in enumerate(rows)]


def _decided(state) -> dict:
    """What the run DECIDES by, with every row's own log position (`seq`) set aside: a splice shifts
    the positions of the rows after it, which is true of any append and keys nothing here."""
    def unseq(value):
        if isinstance(value, dict):
            return {k: unseq(v) for k, v in value.items() if not str(k).endswith("seq")}
        if isinstance(value, list):
            return [unseq(v) for v in value]
        return value
    return unseq(state.model_dump())


def test_the_request_folds_the_same_wherever_it_lands():
    reference = fold(_log(0))
    assert reference.lane_ops_done == 2 and len(reference.lane_op_requests) == 3
    assert reference.best_node_id == 1 and reference.upstream_auto_paused is True
    for at in range(1, 7):
        state = fold(_log(at))
        assert state.lane_op_requests == reference.lane_op_requests, f"spliced at {at}"
        assert _decided(state) == _decided(reference), f"spliced at {at}"
