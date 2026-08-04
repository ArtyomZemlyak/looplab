"""The Layer-5 node-budget refund: a speculative build that NEVER RAN spends no slot.

A speculative build the freshness gate discards before dispatch costs exactly one Developer call —
no sandbox, no GPU, no eval seconds. Charging it a node-budget slot is budget theft from the
experiments the run still has to execute. These tests pin the four properties that make the refund
safe: it applies only to a build proven never to have run, a speculative build that DID evaluate
keeps its slot, an ordinary failed node is untouched, and the number is a pure function of the
folded event log so replay reaches it again.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.card_selection import (
    CARD_FRESHNESS_SUPERSEDED_ERROR,
    card_budget_used,
    is_unevaluated_speculative_discard,
    node_counts_toward_card_budget,
    refunded_card_budget_node_ids,
    refunded_node_reservations,
)

from tests.test_card_speculation_engine import (  # noqa: F401 — the receipt fixture is autouse
    _add_ready_draft,
    _admit_unit_speculation_receipt,
    _commit_speculative_node,
    _engine,
    _start,
)
import looplab.engine.speculation as speculation_module


def _spec_node(
    node_id: int,
    *,
    card_id: str = "card-a",
    speculative: bool = True,
    generation: int | None = 0,
    never_evaluated: bool = True,
    error: str = "speculative build became stale before commit",
    reason: str = "superseded",
    eval_seconds: float = 0.0,
    stages: list | None = None,
    status: NodeStatus = NodeStatus.failed,
    attempt: int = 0,
    eval_start_boundary: bool = True,
    eval_started: bool = False,
) -> Node:
    node = Node(
        id=node_id,
        operator="draft",
        idea=Idea(operator="draft", card_id=card_id, hypothesis=f"h {card_id}"),
        status=status,
        metric=None,
        attempt=attempt,
    )
    node.speculative = speculative
    node.card_build_generation = generation
    node.never_evaluated = never_evaluated
    node.eval_start_boundary = eval_start_boundary
    node.eval_started = eval_started
    node.error = error
    node.error_reason = reason
    node.eval_seconds = eval_seconds
    node.stages = stages or []
    return node


def _state(*nodes: Node, links: dict[int, dict] | None = None) -> RunState:
    if links is None:
        links = {
            node.id: {
                "card_id": node.idea.card_id,
                "generation": node.card_build_generation,
            }
            for node in nodes
            if node.speculative and type(node.card_build_generation) is int
        }
    return RunState(
        nodes={node.id: node for node in nodes},
        speculative_nodes=links,
    )


# --------------------------------------------------------------- (1) never ran -> no budget spent

def test_speculative_discard_that_never_evaluated_does_not_spend_budget():
    discarded = _spec_node(0)
    state = _state(discarded)

    assert is_unevaluated_speculative_discard(state, discarded) is True
    assert node_counts_toward_card_budget(state, discarded) is False
    assert card_budget_used(state) == 0
    assert refunded_card_budget_node_ids(state) == frozenset({0})


def test_freshness_receipt_alone_still_proves_it_when_the_boundary_promise_is_there():
    """The exact zero-cost freshness receipt substitutes for `never_evaluated`, not for the boundary."""

    legacy_marker = _spec_node(
        0,
        never_evaluated=False,
        error=CARD_FRESHNESS_SUPERSEDED_ERROR,
        reason="superseded",
    )
    state = _state(legacy_marker)

    assert is_unevaluated_speculative_discard(state, legacy_marker) is True
    assert card_budget_used(state) == 0


def test_a_log_that_promised_no_eval_start_boundary_is_never_refunded():
    """FAIL CLOSED on a log written before the boundary existed.

    Such a log cannot distinguish a build discarded before dispatch from one whose sandbox ran for
    forty minutes before the process was killed: eval seconds are charged only by a terminal and
    `stage_finished` rows are appended inside the terminal's own write-lock block, so BOTH look like
    `eval_seconds=0, stages=[]`. Silence is not evidence, so the slot stays charged — the refund is
    the thing that gets given up, never the compute accounting.
    """

    unpromised = _spec_node(0, eval_start_boundary=False)
    state = _state(unpromised)

    assert is_unevaluated_speculative_discard(state, unpromised) is False
    assert card_budget_used(state) == 1
    assert refunded_card_budget_node_ids(state) == frozenset()

    # Same node, same terminal, but written by an engine that promised the boundary and never had to
    # append one: now the absence IS evidence and the slot comes back.
    promised = _spec_node(1, eval_start_boundary=True)
    assert card_budget_used(_state(promised)) == 0


def test_refund_still_requires_both_durable_speculative_receipts():
    """The marker alone is not enough: an unlinked/mismatched build is not a committed producer result."""

    unlinked = _spec_node(0)
    assert card_budget_used(_state(unlinked, links={})) == 1

    mismatched_generation = _state(_spec_node(0), links={
        0: {"card_id": "card-a", "generation": 9},
    })
    assert card_budget_used(mismatched_generation) == 1

    mismatched_card = _state(_spec_node(0), links={
        0: {"card_id": "card-other", "generation": 0},
    })
    assert card_budget_used(mismatched_card) == 1

    reset_lifecycle = _state(_spec_node(0, attempt=1))
    assert card_budget_used(reset_lifecycle) == 1


# ------------------------------------------------------- (2) it DID evaluate -> the slot stays spent

def test_speculative_node_that_consumed_an_evaluation_still_spends_budget():
    """Only work that never ran is free. Any evidence of execution outvotes the marker."""

    # Charged eval seconds: the sandbox ran, whatever the terminal says.
    charged = _spec_node(0, eval_seconds=91.5, reason="crash", error="boom")
    assert card_budget_used(_state(charged)) == 1

    # A finished pipeline stage is durable proof the eval started, even at zero charged seconds and
    # even if a writer wrongly stamped the pre-dispatch marker.
    staged = _spec_node(
        1, stages=[{"name": "train", "status": "ok", "exit_code": 0, "seconds": 3.0}],
    )
    assert card_budget_used(_state(staged)) == 1

    # A successful speculative experiment is an ordinary evaluated node.
    evaluated = _spec_node(
        2, status=NodeStatus.evaluated, never_evaluated=False, error="", reason="",
        eval_seconds=4.25,
    )
    evaluated.metric = 0.5
    assert card_budget_used(_state(evaluated)) == 1

    # A speculative failure with NEITHER the marker nor the exact freshness receipt is charged: a
    # bare reason="superseded" is also what an ordinary build/reset race writes.
    unproven = _spec_node(3, never_evaluated=False, error="superseded by node reset")
    assert card_budget_used(_state(unproven)) == 1

    # The durable eval-START boundary outvotes the marker even when NOTHING else does: this is the
    # crash-interrupted prefetch, whose terminal is written by a resumed process with an empty
    # in-memory `eval_inflight` set and whose forty GPU-minutes left no other trace in the log.
    interrupted = _spec_node(4, eval_started=True)
    assert is_unevaluated_speculative_discard(_state(interrupted), interrupted) is False
    assert card_budget_used(_state(interrupted)) == 1


# ------------------------------------------------- (3) ordinary (non-speculative) nodes are untouched

def test_ordinary_failed_or_aborted_node_is_unaffected():
    """The change is strictly scoped to speculative discards; a plain failure consumed a real build."""

    plain_failed = _spec_node(
        0, speculative=False, generation=None, never_evaluated=False,
        reason="crash", error="traceback",
    )
    # Even wearing the exact freshness receipt AND the pre-dispatch marker, a node with no
    # speculative provenance can never be refunded.
    impersonating = _spec_node(
        1, speculative=False, generation=None,
        error=CARD_FRESHNESS_SUPERSEDED_ERROR, reason="superseded",
    )
    state = _state(plain_failed, impersonating, links={})

    assert card_budget_used(state) == 2
    assert refunded_card_budget_node_ids(state) == frozenset()


def test_refund_is_bounded_by_one_whole_operator_ceiling():
    """Anti-livelock floor: a refunded slot is re-spendable, so the total refund stays finite."""

    state = _state(*[_spec_node(node_id, card_id=f"card-{node_id}") for node_id in range(5)])

    assert len(refunded_card_budget_node_ids(state)) == 5
    assert refunded_node_reservations(state, 2) == 2
    assert refunded_node_reservations(state, 8) == 5
    assert refunded_node_reservations(state, 0) == 0


# ------------------------------------------------------------------ (4) identical after a replay

def test_discarded_speculative_build_refunds_its_slot_and_survives_replay(
    tmp_path, monkeypatch,
):
    """End to end: the engine discards a real speculative build, then a FRESH process re-folding the
    same bytes reaches the identical budget, ceiling and remaining-slot numbers."""

    run_dir = tmp_path / "refund-replay"
    engine, _producer = _engine(run_dir)
    engine._base_max_nodes = 2
    engine.policy.max_nodes = 2
    _start(engine)
    _add_ready_draft(engine, "card-1", x=0.2)
    _add_ready_draft(engine, "card-2", x=0.8)
    dropped_node = _commit_speculative_node(engine)

    monkeypatch.setattr(
        speculation_module, "speculative_card_is_fresh", lambda *_a, **_k: False,
    )
    assert anyio.run(engine._drop_stale_speculation) is True

    live = fold(engine.store.read_all())
    node = live.nodes[dropped_node]
    assert node.status is NodeStatus.failed
    assert node.never_evaluated is True
    assert node.eval_seconds == 0
    # The durable marker is on the node's single terminal — the refund is proven from the event log,
    # never from the absence of a node workdir (replay cannot see the filesystem).
    terminals = [
        event for event in engine.store.read_all()
        if event.type == "node_failed" and event.data.get("node_id") == dropped_node
    ]
    assert len(terminals) == 1
    assert terminals[0].data["never_evaluated"] is True
    assert not (run_dir / "nodes" / f"node_{dropped_node}").exists()

    live_budget = card_budget_used(live)
    live_limit = engine._hard_node_reservation_limit(live)
    live_remaining = engine._node_reservation_slots_remaining(live)
    assert live_budget == 0
    assert live_limit == 3            # 2 operator slots + the one refunded reservation
    assert live_remaining == 2

    # Replay: a fresh store + a fresh Engine over the same bytes, with no live state carried over.
    replayed = fold(EventStore(str(run_dir / "events.jsonl")).read_all())
    resumed, _unused = _engine(run_dir)
    resumed._base_max_nodes = 2
    resumed.policy.max_nodes = 2

    assert card_budget_used(replayed) == live_budget
    assert refunded_card_budget_node_ids(replayed) == frozenset({dropped_node})
    assert resumed._hard_node_reservation_limit(replayed) == live_limit
    assert resumed._node_reservation_slots_remaining(replayed) == live_remaining


# ------------------------------------------------- (5) a build that BURNED compute survives a crash

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _crash_a_speculative_evaluation(tmp_path) -> tuple[Path, int]:
    """SIGKILL a real process while a speculative node's sandbox is genuinely executing.

    The interrupted node is left exactly as a crash leaves it: `pending`, no terminal, no charged
    eval seconds and no `stage_finished` row — byte-indistinguishable, before this test existed,
    from a build that was never dispatched at all.  Returns the run dir and the node id.
    """

    run_dir = tmp_path / "crash-run"
    sentinel = tmp_path / "sandbox-running"
    env = dict(os.environ)
    env["LOOPLAB_MEMORY_DIR"] = str(tmp_path / "_mem")
    env["LOOPLAB_KNOWLEDGE_DIR"] = str(tmp_path / "_knowledge")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests._speculation_crash_driver", str(run_dir), str(sentinel)],
        cwd=str(tmp_path), env=env, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.time() + 180
        while not sentinel.exists() and time.time() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail(f"crash driver exited early: {out}\n{err}")
            time.sleep(0.05)
        assert sentinel.exists(), "the speculative node's sandbox never started"
        # The whole session: the engine process AND the sandbox subprocess it is waiting on. This is
        # the 40-GPU-minute kill from the review, compressed.
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        out, _err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:                     # never leave a blocked sandbox behind
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.communicate(timeout=60)
    node_id = int(
        next(line for line in out.splitlines() if line.startswith("speculative_node="))
        .split("=", 1)[1]
    )
    return run_dir, node_id


def test_crash_interrupted_speculative_eval_is_never_refunded(tmp_path, monkeypatch):
    """A killed-mid-evaluation prefetch must keep its slot — the compute is already spent.

    Before the durable eval-start boundary this was unprovable: `eval_inflight` is in-memory and a
    resumed process starts with an empty one, so the stale-drop wrote `never_evaluated: true` over a
    node whose sandbox had really run and the slot came back for free.
    """

    run_dir, node_id = _crash_a_speculative_evaluation(tmp_path)

    crashed = fold(EventStore(str(run_dir / "events.jsonl")).read_all())
    interrupted = crashed.nodes[node_id]
    # Exactly the prefix the reviewer described: nothing in the log looks like an evaluation...
    assert interrupted.status is NodeStatus.pending
    assert not interrupted.stages
    assert not interrupted.eval_seconds
    # ...except the durable start boundary, which is the whole point.
    assert interrupted.eval_started is True

    resumed, _producer = _engine(run_dir)
    monkeypatch.setattr(
        speculation_module, "speculative_card_is_fresh", lambda *_a, **_k: False,
    )
    # A resumed process has an EMPTY in-memory in-flight set — the default the main loop passes.
    assert anyio.run(resumed._drop_stale_speculation) is True

    after = fold(resumed.store.read_all())
    dropped = after.nodes[node_id]
    assert dropped.status is NodeStatus.failed
    assert dropped.error_reason == "superseded"
    # The terminal must NOT claim the build never ran, and the slot must NOT come back.
    assert dropped.never_evaluated is False
    assert is_unevaluated_speculative_discard(after, dropped) is False
    assert refunded_card_budget_node_ids(after) == frozenset()
    assert card_budget_used(after) == 1
    assert resumed._hard_node_reservation_limit(after) == resumed._base_max_nodes
