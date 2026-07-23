"""Focused Layer-5 pure producer/counterfactual selection contracts.

These tests intentionally do not exercise the concurrent engine spine; they pin the deterministic seam
that the main task calls before emitting requests and immediately before GPU admission.
"""
from __future__ import annotations

import pytest

from looplab.core.models import (
    Card,
    CardIdentityProvenance,
    CardSelectionProvenance,
    Idea,
    Node,
    NodeStatus,
    RunState,
    effective_card_footprint,
)
from looplab.search.card_selection import (
    CARD_FRESHNESS_SUPERSEDED_ERROR,
    CardResourceEnvelope,
    card_budget_used,
    card_fits_resource_envelope,
    card_selection_set,
    speculative_card_actions,
    speculative_card_is_fresh,
    speculative_card_selection_set,
    speculative_raw_actions,
)
from looplab.search.policy import ASHAPolicy, GreedyTree


_DIGEST = "card-action:v1:" + "5" * 64


def _node(
    node_id: int,
    *,
    parents: tuple[int, ...] = (),
    operator: str = "draft",
    status: NodeStatus = NodeStatus.evaluated,
    metric: float | None = 0.5,
    card_id: str | None = None,
    footprint: dict | None = None,
    attempt: int = 0,
) -> Node:
    return Node(
        id=node_id,
        parent_ids=list(parents),
        operator=operator,
        idea=Idea(
            operator=operator,
            card_id=card_id,
            hypothesis=f"hypothesis {card_id or node_id}",
            footprint=footprint,
        ),
        status=status,
        metric=metric,
        attempt=attempt,
    )


def _ready_card(
    card_id: str,
    *,
    operator: str = "improve",
    parents: tuple[int, ...] = (0,),
    best: int | None = 0,
    concepts: tuple[str, ...] = (),
    footprint: dict | None = None,
    resource_pin: dict | None = None,
    pinned: bool = False,
) -> Card:
    return Card(
        id=card_id,
        statement=f"proposal {card_id}",
        seed_statement=f"proposal {card_id}",
        source="engine",
        status="proposed",
        verdict="open",
        identity=CardIdentityProvenance(
            kind="native",
            source="card_added_receipt",
            durable=True,
            receipt_valid=True,
            action_digest=_DIGEST,
        ),
        selection_provenance=CardSelectionProvenance(
            action_source="card_added",
            action_owner_count=1,
            action_complete=True,
            freshness="current",
            owner_state="none",
        ),
        selection_blockers=[],
        selection_ready=True,
        operator=operator,
        parent_id=parents[0] if parents else None,
        parent_ids=list(parents),
        parent_generations={str(parent): 0 for parent in parents},
        scored_against=best,
        scored_against_generation=0 if best is not None else None,
        scored_against_empty=best is None,
        concept_tags=list(concepts),
        footprint=footprint,
        resource_pin=resource_pin,
        pinned=pinned,
    )


def _owned(card: Card, node_id: int) -> Card:
    return card.model_copy(deep=True, update={
        "status": "running",
        "verdict": "testing",
        "evidence": [node_id],
        "selection_provenance": card.selection_provenance.model_copy(
            update={"owner_state": "in_flight"},
        ),
        "selection_blockers": ["work_in_flight"],
        "selection_ready": False,
    })


class _PopulationPolicy:
    n_seeds = 0
    debug_depth = 0
    card_select_k = 2

    def next_actions(self, _state):
        return [{"kind": "improve", "parent_id": 0}]

    def card_score(self, _state, card, *, scoring):
        del scoring
        return 0, (2.0 if card.id == "rank-one" else 1.0,)


class _RankedPopulationPolicy(_PopulationPolicy):
    def card_score(self, _state, card, *, scoring):
        del scoring
        return 0, ({"ready-top": 3.0, "owned-b": 2.0, "owned-c": 1.0}[card.id],)


class _SerialCompatibilityPolicy:
    n_seeds = 0
    debug_depth = 0

    def next_actions(self, _state):
        return [{"kind": "draft"}]


def test_custom_policy_without_card_scorer_keeps_raw_create_on_serial_spine():
    state = RunState()
    policy = _SerialCompatibilityPolicy()

    assert policy.next_actions(state) == [{"kind": "draft"}]
    assert speculative_raw_actions(state, policy, 4) == []


def test_producer_masks_acknowledged_initial_session_pending_and_preserves_actions():
    state = RunState(
        nodes={
            0: _node(0, metric=0.9),
            # No durable speculative marker: the session may acknowledge the initial serial batch too.
            1: _node(
                1, parents=(0,), operator="improve", status=NodeStatus.pending,
                metric=None, card_id="already-built",
            ),
        },
        best_node_id=0,
        cards={"next": _ready_card("next")},
    )
    policy = GreedyTree(n_seeds=1, max_nodes=8, debug_depth=0)

    assert card_selection_set(state, policy, 8) == []
    assert speculative_card_selection_set(state, policy, 8) == []
    assert speculative_card_selection_set(
        state, policy, 8, ignored_pending_node_ids={1},
    ) == ["next"]
    assert speculative_card_actions(
        state, policy, 8, ignored_pending_node_ids={1},
    ) == [{
        "kind": "improve", "parent_id": 0, "_card_id": "next",
        "_scores": {0: 0.9}, "_chosen": 0, "_reason": "exploit best",
    }]


def test_forced_staged_seed_bootstraps_producer_before_first_node_exists():
    state = RunState(cards={
        "seed-a": _ready_card("seed-a", operator="draft", parents=(), best=None),
        "seed-b": _ready_card("seed-b", operator="draft", parents=(), best=None),
    })
    policy = GreedyTree(n_seeds=2, max_nodes=2, debug_depth=0)

    assert speculative_card_actions(state, policy, 2) == [
        {"kind": "draft", "_card_id": "seed-a"},
        {"kind": "draft", "_card_id": "seed-b"},
    ]


def test_forced_seed_ignores_reserved_request_then_fail_closes_remaining_lane_exactly():
    subject = _owned(
        _ready_card("0-subject", operator="draft", parents=(), best=None), 0)
    state = RunState(
        nodes={
            0: _node(
                0, operator="draft", status=NodeStatus.pending, metric=None,
                card_id="0-subject",
            ),
        },
        cards={
            "0-subject": subject,
            "1-requested": _ready_card(
                "1-requested", operator="draft", parents=(), best=None),
            "2-later": _ready_card(
                "2-later", operator="draft", parents=(), best=None),
        },
    )
    policy = GreedyTree(n_seeds=3, max_nodes=4, debug_depth=0)

    # The outstanding request owns one effective-budget slot and must not veto the next receipt.  The
    # already-coded subject remains fresh under the same counterfactual forced seed lane.
    assert speculative_card_selection_set(
        state,
        policy,
        4,
        excluded_card_ids={"1-requested"},
        ignored_pending_node_ids={0},
    ) == ["2-later"]
    assert speculative_card_is_fresh(
        state,
        policy,
        4,
        card_id="0-subject",
        node_id=0,
        excluded_card_ids={"0-subject", "1-requested"},
        ignored_pending_node_ids={0},
    ) is True


def test_outstanding_request_is_excluded_and_reserves_uncommitted_budget_slot():
    state = RunState(
        nodes={0: _node(0, metric=0.9)},
        best_node_id=0,
        cards={
            "requested": _ready_card("requested"),
            "next": _ready_card("next", concepts=("different",)),
        },
    )
    policy = GreedyTree(n_seeds=1, max_nodes=8, debug_depth=0)

    assert speculative_card_selection_set(
        state, policy, 2, excluded_card_ids={"requested"},
    ) == []
    assert speculative_card_selection_set(
        state, policy, 3, excluded_card_ids={"requested"},
    ) == ["next"]


def test_include_owned_freshness_uses_population_set_not_strict_rank_one_without_mutation():
    subject = _owned(_ready_card("subject", concepts=("subject",)), 2)
    state = RunState(
        nodes={
            0: _node(0, metric=0.9),
            2: _node(
                2, parents=(0,), operator="improve", status=NodeStatus.pending,
                metric=None, card_id="subject",
            ),
        },
        best_node_id=0,
        cards={
            "subject": subject,
            "rank-one": _ready_card("rank-one", concepts=("other",)),
        },
    )
    before = state.model_copy(deep=True)

    assert speculative_card_selection_set(
        state,
        _PopulationPolicy(),
        5,
        ignored_pending_node_ids={2},
        include_owned_card_id="subject",
        include_owned_node_id=2,
    ) == ["rank-one", "subject"]
    assert speculative_card_is_fresh(
        state,
        _PopulationPolicy(),
        5,
        card_id="subject",
        node_id=2,
        ignored_pending_node_ids={2},
    ) is True
    assert state == before


def test_owned_population_uses_one_common_counterfactual_selection_set():
    owned_b = _owned(_ready_card("owned-b", concepts=("b",)), 2)
    owned_c = _owned(_ready_card("owned-c", concepts=("c",)), 3)
    pending_b = _node(
        2, parents=(0,), operator="improve", status=NodeStatus.pending,
        metric=None, card_id="owned-b",
    ).model_copy(update={"speculative": True, "card_build_generation": 7})
    pending_c = _node(
        3, parents=(0,), operator="improve", status=NodeStatus.pending,
        metric=None, card_id="owned-c",
    ).model_copy(update={"speculative": True, "card_build_generation": 8})
    state = RunState(
        nodes={0: _node(0, metric=0.9), 2: pending_b, 3: pending_c},
        best_node_id=0,
        cards={
            "owned-b": owned_b,
            "owned-c": owned_c,
            "ready-top": _ready_card("ready-top", concepts=("new",)),
        },
        speculative_nodes={
            2: {"card_id": "owned-b", "generation": 7},
            3: {"card_id": "owned-c", "generation": 8},
        },
    )
    before = state.model_copy(deep=True)
    kwargs = {
        "excluded_card_ids": {"owned-b", "owned-c"},
        "ignored_pending_node_ids": {2, 3},
    }

    assert speculative_card_selection_set(
        state, _RankedPopulationPolicy(), 5,
        include_owned_card_id="owned-c", include_owned_node_id=3, **kwargs,
    ) == ["ready-top", "owned-b"]
    assert speculative_card_is_fresh(
        state, _RankedPopulationPolicy(), 5,
        card_id="owned-b", node_id=2, **kwargs,
    ) is True
    assert speculative_card_is_fresh(
        state, _RankedPopulationPolicy(), 5,
        card_id="owned-c", node_id=3, **kwargs,
    ) is False
    assert state == before


def test_consumed_speculative_sibling_stays_masked_from_next_prefetch_population():
    class _OneSlotPolicy:
        n_seeds = 0
        debug_depth = 0
        card_select_k = 1

        def next_actions(self, _state):
            return [{"kind": "improve", "parent_id": 0}]

        def card_score(self, _state, card, *, scoring):
            del scoring
            return 0, (2.0 if card.id == "consumed" else 1.0,)

    consumed = _node(
        2, parents=(0,), operator="improve", status=NodeStatus.pending,
        metric=None, card_id="consumed",
    ).model_copy(update={"speculative": True, "card_build_generation": 7})
    prefetched = _node(
        3, parents=(0,), operator="improve", status=NodeStatus.pending,
        metric=None, card_id="prefetched",
    ).model_copy(update={"speculative": True, "card_build_generation": 8})
    state = RunState(
        nodes={0: _node(0, metric=0.9), 2: consumed, 3: prefetched},
        best_node_id=0,
        cards={
            "consumed": _owned(_ready_card("consumed"), 2),
            "prefetched": _owned(_ready_card("prefetched"), 3),
        },
        speculative_nodes={
            2: {"card_id": "consumed", "generation": 7},
            3: {"card_id": "prefetched", "generation": 8},
        },
    )
    kwargs = {
        "card_id": "prefetched",
        "node_id": 3,
        "excluded_card_ids": {"consumed", "prefetched"},
        "ignored_pending_node_ids": {2, 3},
    }

    # With no consumer admission both unconsumed siblings share K=1 and the stronger Card wins.
    assert speculative_card_is_fresh(state, _OneSlotPolicy(), 5, **kwargs) is False
    # Once that stronger lifecycle is already being consumed, it cannot also occupy the next slot.
    assert speculative_card_is_fresh(
        state, _OneSlotPolicy(), 5, consumed_inflight={(2, 0)}, **kwargs,
    ) is True
    # Admission is attempt-scoped: a stale generation cannot mask a reset lifecycle with the same id.
    assert speculative_card_is_fresh(
        state, _OneSlotPolicy(), 5, consumed_inflight={(2, 1)}, **kwargs,
    ) is False


@pytest.mark.parametrize(
    "terminal_exclusion",
    ["aborted", "tombstoned", "dropped", "dropped_no_reason", "merged", "merged_absent"])
def test_terminally_excluded_speculative_sibling_does_not_poison_common_population(
    terminal_exclusion,
):
    subject = _owned(_ready_card("subject", concepts=("subject",)), 2)
    excluded = _owned(_ready_card("excluded", concepts=("excluded",)), 3)
    rank_one = _ready_card("rank-one", concepts=("new",))
    # A sibling whose CARD is administratively dead (operator-dropped or merged) must NOT poison the
    # subject's freshness counterfactual — it is terminalized by the gate on its own turn. The node
    # itself stays pending in these cases (only the Card carries the closure). `dropped_no_reason` is the
    # regression guard for the reachable folded state a card_dropped event with an empty/missing reason
    # produces (status="dropped" but dropped_reason=None): a reason-keyed skip would miss it, so the gate
    # must key on status. `merged` exercises the present-with-merged_into disjunct; `merged_absent` is the
    # PRODUCTION merged shape (the alias row is collapsed OUT of state.cards but its id is in a canonical
    # Card's `aliases`) — the skip must fire on that proven merge receipt.
    cards = {"subject": subject, "excluded": excluded, "rank-one": rank_one}
    if terminal_exclusion == "dropped":
        cards["excluded"] = excluded.model_copy(
            update={"status": "dropped", "dropped_reason": "operator dropped"})
    elif terminal_exclusion == "dropped_no_reason":
        cards["excluded"] = excluded.model_copy(update={"status": "dropped", "dropped_reason": None})
    elif terminal_exclusion == "merged":
        cards["excluded"] = excluded.model_copy(update={"merged_into": "subject"})
    elif terminal_exclusion == "merged_absent":
        del cards["excluded"]                                   # collapsed out of state.cards
        # merge receipt: "excluded" folded INTO a canonical Card (which is therefore not selection_ready)
        cards["canonical"] = _ready_card("canonical", concepts=("new",)).model_copy(
            update={"selection_ready": False, "aliases": ["excluded"]})
    subject_node = _node(
        2, parents=(0,), operator="improve", status=NodeStatus.pending,
        metric=None, card_id="subject",
    ).model_copy(update={"speculative": True, "card_build_generation": 7})
    excluded_node = _node(
        3, parents=(0,), operator="improve", status=NodeStatus.pending,
        metric=None, card_id="excluded",
    ).model_copy(update={
        "speculative": True,
        "card_build_generation": 8,
        "tombstoned": terminal_exclusion == "tombstoned",
    })
    state = RunState(
        nodes={0: _node(0, metric=0.9), 2: subject_node, 3: excluded_node},
        best_node_id=0,
        cards=cards,
        aborted_nodes={3} if terminal_exclusion == "aborted" else set(),
        speculative_nodes={
            2: {"card_id": "subject", "generation": 7},
            3: {"card_id": "excluded", "generation": 8},
        },
    )

    assert speculative_card_is_fresh(
        state, _PopulationPolicy(), 5,
        card_id="subject", node_id=2,
        excluded_card_ids={"subject", "excluded"},
        ignored_pending_node_ids={2, 3},
    ) is True


def _stale_owned(card_id: str) -> Card:
    """An in-flight speculative sibling that is stale on its OWN account: its parent was
    aborted/tombstoned/reset, so the fold gives it blockers {"freshness_stale", "work_in_flight"}."""
    base = _owned(_ready_card(card_id, concepts=(card_id,)), 3)
    return base.model_copy(update={
        "selection_provenance": base.selection_provenance.model_copy(update={"freshness": "stale"}),
        "selection_blockers": ["freshness_stale", "work_in_flight"],
    })


def _stale_sibling_state(sibling: Card) -> RunState:
    subject = _owned(_ready_card("subject", concepts=("subject",)), 2)
    subject_node = _node(
        2, parents=(0,), operator="improve", status=NodeStatus.pending,
        metric=None, card_id="subject",
    ).model_copy(update={"speculative": True, "card_build_generation": 7})
    sibling_node = _node(
        3, parents=(0,), operator="improve", status=NodeStatus.pending,
        metric=None, card_id="excluded",
    ).model_copy(update={"speculative": True, "card_build_generation": 8})
    return RunState(
        nodes={0: _node(0, metric=0.9), 2: subject_node, 3: sibling_node},
        best_node_id=0,
        cards={"subject": subject, "excluded": sibling, "rank-one": _ready_card("rank-one")},
        speculative_nodes={
            2: {"card_id": "subject", "generation": 7},
            3: {"card_id": "excluded", "generation": 8},
        },
    )


def test_alive_but_stale_speculative_sibling_does_not_collapse_the_healthy_lane():
    """An alive-but-STALE sibling (blockers {"freshness_stale", "work_in_flight"}) restores cleanly
    EXCEPT for its own staleness. It is terminalized by the freshness gate on its OWN turn, so it must
    be skipped like an administratively-dead sibling — NOT poison the counterfactual and spuriously
    supersede the healthy subject (the docs/23 §12.5 lane collapse)."""
    state = _stale_sibling_state(_stale_owned("excluded"))

    assert speculative_card_is_fresh(
        state, _PopulationPolicy(), 5,
        card_id="subject", node_id=2,
        excluded_card_ids={"subject", "excluded"},
        ignored_pending_node_ids={2, 3},
    ) is True


def test_stale_sibling_with_an_extra_blocker_still_fails_the_counterfactual_closed():
    """The stale-skip is NARROW: it fires only when staleness is the SOLE extra blocker. A sibling that
    also carries an unproven shape (here an incomplete action receipt) is not a clean drop-on-its-own-
    turn case, so the counterfactual still fails CLOSED rather than proceed on an unproven population."""
    base = _stale_owned("excluded")
    corrupt_stale = base.model_copy(update={
        "selection_provenance": base.selection_provenance.model_copy(update={"action_complete": False}),
        "selection_blockers": ["action_receipt_incomplete", "freshness_stale", "work_in_flight"],
    })
    state = _stale_sibling_state(corrupt_stale)

    assert speculative_card_is_fresh(
        state, _PopulationPolicy(), 5,
        card_id="subject", node_id=2,
        excluded_card_ids={"subject", "excluded"},
        ignored_pending_node_ids={2, 3},
    ) is False


def test_corrupt_absent_speculative_sibling_fails_the_counterfactual_closed():
    """A sibling whose speculative marker names a Card that is ABSENT and is NOT a known merge alias is a
    corrupt/partial ownership chain — the freshness counterfactual must fail CLOSED (subject not fresh)
    rather than silently skip it and proceed on an unproven common population."""
    subject = _owned(_ready_card("subject", concepts=("subject",)), 2)
    subject_node = _node(
        2, parents=(0,), operator="improve", status=NodeStatus.pending,
        metric=None, card_id="subject",
    ).model_copy(update={"speculative": True, "card_build_generation": 7})
    corrupt_node = _node(
        3, parents=(0,), operator="improve", status=NodeStatus.pending,
        metric=None, card_id="ghost",
    ).model_copy(update={"speculative": True, "card_build_generation": 8})
    state = RunState(
        nodes={0: _node(0, metric=0.9), 2: subject_node, 3: corrupt_node},
        best_node_id=0,
        cards={"subject": subject, "rank-one": _ready_card("rank-one", concepts=("new",))},
        speculative_nodes={
            2: {"card_id": "subject", "generation": 7},
            3: {"card_id": "ghost", "generation": 8},        # names a Card that never existed
        },
    )

    assert speculative_card_is_fresh(
        state, _PopulationPolicy(), 5,
        card_id="subject", node_id=2,
        excluded_card_ids={"subject", "ghost"},
        ignored_pending_node_ids={2, 3},
    ) is False


def test_include_owned_keeps_exact_parent_generation_fence():
    subject = _owned(_ready_card("subject"), 2)
    state = RunState(
        nodes={
            0: _node(0, metric=0.9, attempt=1),
            2: _node(
                2, parents=(0,), operator="improve", status=NodeStatus.pending,
                metric=None, card_id="subject",
            ),
        },
        best_node_id=0,
        cards={"subject": subject},
    )

    assert speculative_card_is_fresh(
        state, GreedyTree(n_seeds=1, max_nodes=8, debug_depth=0), 8,
        card_id="subject", node_id=2, ignored_pending_node_ids={2},
    ) is False


@pytest.mark.parametrize(
    ("parents", "breed_excluded"),
    [((0, 2), set()), ((0, 1), {1})],
)
def test_merge_freshness_requires_exact_metric_top_two_and_both_breedable(
    parents, breed_excluded,
):
    card = _owned(_ready_card("merge", operator="merge", parents=parents), 3)
    state = RunState(
        direction="max",
        nodes={
            0: _node(0, metric=0.9),
            1: _node(1, metric=0.8),
            2: _node(2, metric=0.7),
            3: _node(
                3, parents=parents, operator="merge", status=NodeStatus.pending,
                metric=None, card_id="merge",
            ),
        },
        best_node_id=0,
        breed_excluded=breed_excluded,
        cards={"merge": card},
    )

    assert speculative_card_is_fresh(
        state, _PopulationPolicy(), 8,
        card_id="merge", node_id=3, ignored_pending_node_ids={3},
    ) is False


def test_effective_resource_clamp_keeps_overdeclarations_but_invalid_card_fails_closed():
    state = RunState(
        nodes={0: _node(0, metric=0.9)},
        best_node_id=0,
        cards={
            "wide": _ready_card(
                "wide", footprint={"gpus": 2, "gpu_mem_mib": 32_000},
            ),
            # A valid operator pin must not launder a malformed immutable declaration.
            "invalid": _ready_card(
                "invalid",
                footprint={"gpus": "many", "gpu_mem_mib": 8_000},
                resource_pin={"gpus": 0, "pinned_by": "operator"},
            ),
        },
    )
    policy = GreedyTree(n_seeds=1, max_nodes=8, debug_depth=0)
    one_gpu = CardResourceEnvelope(gpu_count=1, gpu_memory_mib=(16_000,))

    assert speculative_card_selection_set(
        state, policy, 8, resource_envelope=one_gpu,
    ) == ["wide"]

    assert card_fits_resource_envelope(
        _ready_card("memory-only", footprint={"gpu_mem_mib": 8_000}),
        CardResourceEnvelope(gpu_count=0),
    ) is True

    owned = _owned(_ready_card("compiled", footprint={"gpus": 1}), 2)
    compiled_state = RunState(
        nodes={
            0: _node(0, metric=0.9),
            2: _node(
                2, parents=(0,), operator="improve", status=NodeStatus.pending,
                metric=None, card_id="compiled",
                footprint={"gpus": 2, "gpu_mem_mib": 32_000},
            ),
        },
        best_node_id=0,
        cards={"compiled": owned},
    )
    assert speculative_card_is_fresh(
        compiled_state, policy, 8,
        card_id="compiled", node_id=2, ignored_pending_node_ids={2},
        resource_envelope=one_gpu,
    ) is True


def test_zero_gpu_envelope_preserves_positive_requirement_and_fails_freshness_closed():
    zero_gpu = CardResourceEnvelope(gpu_count=0)
    positive = _ready_card(
        "positive", footprint={"gpus": 2, "gpu_mem_mib": 32_000},
    )
    cpu_only = _ready_card(
        "cpu-only", footprint={"gpus": 0, "gpu_mem_mib": 32_000},
    )

    assert effective_card_footprint(
        positive.footprint, None, gpu_count=0,
    ) == {"gpus": 2, "gpu_mem_mib": 32_000}
    assert effective_card_footprint(
        cpu_only.footprint, None, gpu_count=0,
    ) == {"gpus": 0}
    assert card_fits_resource_envelope(positive, zero_gpu) is False
    assert card_fits_resource_envelope(cpu_only, zero_gpu) is True

    owned = _owned(positive, 2)
    state = RunState(
        nodes={
            0: _node(0, metric=0.9),
            2: _node(
                2, parents=(0,), operator="improve", status=NodeStatus.pending,
                metric=None, card_id="positive",
                footprint={"gpus": 2, "gpu_mem_mib": 32_000},
            ),
        },
        best_node_id=0,
        cards={"positive": owned},
    )
    assert speculative_card_is_fresh(
        state, GreedyTree(n_seeds=1, max_nodes=8, debug_depth=0), 8,
        card_id="positive", node_id=2, ignored_pending_node_ids={2},
        resource_envelope=zero_gpu,
    ) is False


def test_budget_refunds_only_proven_zero_cost_freshness_drop_not_ordinary_superseded():
    ordinary = _node(0, status=NodeStatus.failed, metric=None)
    ordinary.error_reason = "superseded"
    ordinary.error = CARD_FRESHNESS_SUPERSEDED_ERROR
    ordinary.eval_seconds = 0
    speculative = _node(1, status=NodeStatus.failed, metric=None, card_id="spec-card")
    speculative.error_reason = "superseded"
    speculative.error = CARD_FRESHNESS_SUPERSEDED_ERROR
    speculative.eval_seconds = 0
    speculative.speculative = True
    speculative.card_build_generation = 7
    state = RunState(
        nodes={0: ordinary, 1: speculative},
        speculative_nodes={1: {"card_id": "spec-card", "generation": 7}},
    )

    assert card_budget_used(state) == 1
    mismatched = state.model_copy(deep=True, update={
        "speculative_nodes": {1: {"card_id": "spec-card", "generation": 8}},
    })
    assert card_budget_used(mismatched) == 2

    reset_lifecycle = state.model_copy(deep=True)
    reset_lifecycle.nodes[1].attempt = 1
    assert card_budget_used(reset_lifecycle) == 2


def test_asha_can_fill_same_survivor_lane_but_does_not_cross_pending_rung_boundary():
    roots = {node_id: _node(node_id, metric=1.0 - node_id / 10) for node_id in range(4)}
    roots[4] = _node(
        4, parents=(0,), operator="improve", status=NodeStatus.pending,
        metric=None, card_id="built-a",
    )
    built_a = _owned(_ready_card("built-a", parents=(0,)), 4)
    state = RunState(
        direction="max",
        nodes=roots,
        best_node_id=0,
        cards={
            "built-a": built_a,
            "same-rung-b": _ready_card("same-rung-b", parents=(1,)),
        },
    )
    policy = ASHAPolicy(n_seeds=4, max_nodes=12, eta=2, debug_depth=0)

    assert speculative_card_selection_set(
        state, policy, 12,
        excluded_card_ids={"built-a"}, ignored_pending_node_ids={4},
    ) == ["same-rung-b"]

    state.nodes[5] = _node(
        5, parents=(1,), operator="improve", status=NodeStatus.pending,
        metric=None, card_id="built-b",
    )
    state.cards["built-b"] = _owned(_ready_card("built-b", parents=(1,)), 5)
    state.cards["duplicate-a"] = _ready_card("duplicate-a", parents=(0,))

    assert speculative_card_selection_set(
        state, policy, 12,
        excluded_card_ids={"built-a", "built-b"},
        ignored_pending_node_ids={4, 5},
    ) == []


def test_asha_never_masks_unresolved_rung_zero_roots_into_replacement_drafts():
    state = RunState(
        nodes={
            0: _node(0, status=NodeStatus.pending, metric=None, card_id="root-a"),
            1: _node(1, status=NodeStatus.pending, metric=None, card_id="root-b"),
            2: _node(2, status=NodeStatus.pending, metric=None, card_id="root-c"),
            3: _node(3, status=NodeStatus.pending, metric=None, card_id="root-d"),
        },
        cards={
            "replacement-root": _ready_card(
                "replacement-root", operator="draft", parents=(), best=None,
            ),
        },
    )
    policy = ASHAPolicy(n_seeds=4, max_nodes=12, eta=2, debug_depth=0)

    assert speculative_card_selection_set(
        state,
        policy,
        12,
        ignored_pending_node_ids={0, 1, 2, 3},
    ) == []


def test_asha_reserves_non_spec_pending_promotion_action_without_excluded_card_id():
    nodes = {node_id: _node(node_id, metric=1.0 - node_id / 10) for node_id in range(4)}
    nodes[4] = _node(
        4,
        parents=(0,),
        operator="improve",
        status=NodeStatus.pending,
        metric=None,
        card_id="non-spec-a",
    )
    state = RunState(
        direction="max",
        nodes=nodes,
        best_node_id=0,
        cards={
            "non-spec-a": _owned(_ready_card("non-spec-a", parents=(0,)), 4),
            "duplicate-a": _ready_card("duplicate-a", parents=(0,)),
            "sibling-b": _ready_card("sibling-b", parents=(1,)),
        },
    )
    policy = ASHAPolicy(n_seeds=4, max_nodes=12, eta=2, debug_depth=0)

    assert speculative_card_selection_set(
        state,
        policy,
        12,
        ignored_pending_node_ids={4},
    ) == ["sibling-b"]
