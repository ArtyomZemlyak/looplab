"""The Layer-5 node-budget refund: a speculative build that NEVER RAN spends no slot.

A speculative build the freshness gate discards before dispatch costs exactly one Developer call —
no sandbox, no GPU, no eval seconds. Charging it a node-budget slot is budget theft from the
experiments the run still has to execute. These tests pin the four properties that make the refund
safe: it applies only to a build proven never to have run, a speculative build that DID evaluate
keeps its slot, an ordinary failed node is untouched, and the number is a pure function of the
folded event log so replay reaches it again.
"""
from __future__ import annotations

import anyio

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


def test_legacy_freshness_receipt_refunds_logs_written_before_the_marker():
    """Old logs carry no `never_evaluated` field; their exact freshness receipt still proves it."""

    legacy = _spec_node(
        0,
        never_evaluated=False,
        error=CARD_FRESHNESS_SUPERSEDED_ERROR,
        reason="superseded",
    )
    state = _state(legacy)

    assert is_unevaluated_speculative_discard(state, legacy) is True
    assert card_budget_used(state) == 0


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

    # A speculative failure with NEITHER the marker nor the legacy freshness receipt is charged: a
    # bare reason="superseded" is also what an ordinary build/reset race writes.
    unproven = _spec_node(3, never_evaluated=False, error="superseded by node reset")
    assert card_budget_used(_state(unproven)) == 1


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
