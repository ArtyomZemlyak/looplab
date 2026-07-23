"""Layer-3 pure Card selection contract (orchestrator wiring is intentionally separate)."""
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
)
from looplab.search.card_selection import (
    CardScoring,
    card_action,
    card_budget_used,
    card_next_actions,
    card_selection_set,
    eligible_cards,
    forced_card_actions,
    normalize_card_scoring,
)
from looplab.search.policy import ASHAPolicy, EvolutionaryPolicy, GreedyTree, MCTSPolicy


_DIGEST = "card-action:v1:" + "0" * 64


def _node(
    node_id: int,
    *,
    operator: str = "draft",
    parents: tuple[int, ...] = (),
    status: NodeStatus = NodeStatus.evaluated,
    metric: float | None = 0.5,
    feasible: bool = True,
    tombstoned: bool = False,
    concepts: tuple[str, ...] = (),
    params: dict[str, float] | None = None,
) -> Node:
    return Node(
        id=node_id,
        parent_ids=list(parents),
        operator=operator,
        idea=Idea(operator=operator, concepts=list(concepts), params=dict(params or {})),
        status=status,
        metric=metric,
        feasible=feasible,
        tombstoned=tombstoned,
    )


def _ready_card(
    card_id: str,
    *,
    operator: str = "improve",
    parents: tuple[int, ...] = (0,),
    concepts: tuple[str, ...] = (),
    confidence: float | None = None,
    novelty_level: int | None = None,
    pinned: bool = False,
) -> Card:
    parent_id = parents[0] if parents else None
    novelty = (
        {"grade": "test", "level": novelty_level, "near_node": None, "recommendation": "allow"}
        if novelty_level is not None else None
    )
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
        parent_id=parent_id,
        parent_ids=list(parents),
        concept_tags=list(concepts),
        confidence=confidence,
        novelty_verdict=novelty,
        pinned=pinned,
    )


class _FallbackPolicy:
    n_seeds = 0
    debug_depth = 0

    def __init__(self, actions):
        self.actions = actions
        self.calls = 0

    def next_actions(self, _state):
        self.calls += 1
        return [dict(action) for action in self.actions]


class _ScoringPolicy(_FallbackPolicy):
    card_select_k = 4

    def __init__(self, actions):
        super().__init__(actions)
        self.scored: list[str] = []

    def card_score(self, _state, card, *, scoring):
        assert isinstance(scoring, CardScoring)
        self.scored.append(card.id)
        return 0, (1.0,)


def test_forced_prefix_evaluates_every_pending_node_before_budget_or_debug():
    state = RunState(nodes={
        3: _node(3, status=NodeStatus.pending, metric=None),
        0: _node(0, status=NodeStatus.failed, metric=None),
        1: _node(1, status=NodeStatus.pending, metric=None),
    })
    policy = GreedyTree(n_seeds=0, max_nodes=0, debug_depth=1)

    assert forced_card_actions(state, policy, max_nodes=0) == [
        {"kind": "evaluate", "node_id": 1},
        {"kind": "evaluate", "node_id": 3},
    ]


def test_forced_seed_prefix_consumes_staged_draft_cards_before_raw_width():
    state = RunState(cards={
        "card-a": _ready_card("card-a", operator="draft", parents=()),
        "card-b": _ready_card("card-b", operator="draft", parents=()),
    })
    policy = GreedyTree(n_seeds=3, max_nodes=3, debug_depth=0)

    # Never mix authority kinds in one turn: consume the two concrete receipts first; after they
    # become Nodes a fresh fold may ask the Researcher for the one remaining raw seed.
    assert forced_card_actions(state, policy, max_nodes=3) == [
        {"kind": "draft", "_card_id": "card-a"},
        {"kind": "draft", "_card_id": "card-b"},
    ]


def test_forced_debug_precedes_card_scoring_and_reuses_matching_ready_card_id():
    state = RunState(
        nodes={
            0: _node(0, metric=0.8),
            1: _node(1, operator="improve", parents=(0,), status=NodeStatus.failed, metric=None),
        },
        best_node_id=0,
        cards={"debug-card": _ready_card("debug-card", operator="debug", parents=(1,))},
    )
    policy = GreedyTree(n_seeds=1, max_nodes=3, debug_depth=1)

    assert card_next_actions(state, policy, max_nodes=3) == [
        {"kind": "debug", "parent_id": 1, "_card_id": "debug-card"},
    ]


def test_tombstoned_failed_node_is_not_a_forced_debug_and_does_not_steal_capacity():
    state = RunState(
        nodes={
            0: _node(0, status=NodeStatus.failed, metric=None, tombstoned=True),
            1: _node(1, metric=0.8),
        },
        best_node_id=1,
    )
    policy = GreedyTree(n_seeds=1, max_nodes=2, debug_depth=1)

    assert forced_card_actions(state, policy, max_nodes=2) is None
    assert card_next_actions(state, policy, max_nodes=2) == [{
        "kind": "improve",
        "parent_id": 1,
        "_scores": {1: 0.8},
        "_chosen": 1,
        "_reason": "exploit best",
    }]


def test_debug_card_runtime_anchor_is_the_first_eligible_failed_leaf_not_breedable():
    state = RunState(
        nodes={
            0: _node(0),
            1: _node(1, operator="improve", parents=(0,), status=NodeStatus.failed, metric=None),
            2: _node(2, operator="improve", parents=(0,), status=NodeStatus.failed, metric=None),
        },
        cards={
            "first": _ready_card("first", operator="debug", parents=(1,)),
            "second": _ready_card("second", operator="debug", parents=(2,)),
        },
    )

    assert [card.id for card in eligible_cards(
        state, GreedyTree(n_seeds=0, max_nodes=8, debug_depth=1),
    )] == ["first"]


def test_tombstoned_earlier_failure_cannot_mask_the_live_debug_card():
    state = RunState(
        nodes={
            0: _node(0, status=NodeStatus.failed, metric=None, tombstoned=True),
            1: _node(1, operator="improve", parents=(2,), status=NodeStatus.failed, metric=None),
            2: _node(2, metric=0.8),
        },
        best_node_id=2,
        cards={"live-debug": _ready_card("live-debug", operator="debug", parents=(1,))},
    )
    policy = GreedyTree(n_seeds=1, max_nodes=3, debug_depth=1)

    assert [card.id for card in eligible_cards(state, policy)] == ["live-debug"]
    assert forced_card_actions(state, policy, max_nodes=3) == [{
        "kind": "debug", "parent_id": 1, "_card_id": "live-debug",
    }]


def test_budget_denominator_excludes_tombstoned_constraint_and_trust_gated_nodes():
    state = RunState(
        nodes={
            0: _node(0),
            1: _node(1, tombstoned=True),
            2: _node(2, feasible=False),
            3: _node(3),
        },
        best_node_id=0,
        breed_excluded={3},
    )
    policy = GreedyTree(n_seeds=0, max_nodes=2, debug_depth=0)

    assert card_budget_used(state) == 1
    assert forced_card_actions(state, policy, max_nodes=1) == []
    # The flag-on fallback sees the same effective denominator and preserves normal policy intent.
    assert card_next_actions(state, policy, max_nodes=2) == [{
        "kind": "improve", "parent_id": 0,
        "_scores": {0: 0.5}, "_chosen": 0, "_reason": "exploit best",
    }]


def test_policy_fallback_uses_effective_nodes_without_mutating_flag_off_state():
    state = RunState(
        direction="max",
        nodes={
            0: _node(0, metric=0.9),
            1: _node(1, operator="improve", parents=(0,), tombstoned=True),
            2: _node(2, operator="improve", parents=(0,), feasible=False),
            3: _node(3, operator="improve", parents=(0,), metric=0.8),
        },
        best_node_id=0,
        breed_excluded={3},
        cards={"hot": _ready_card("hot", parents=(0,))},
    )
    policy = GreedyTree(n_seeds=1, max_nodes=2, debug_depth=0)
    before = state.model_dump(mode="json")

    # Raw/flag-off policy semantics are untouched: its historical denominator still sees four nodes.
    assert policy.next_actions(state) == []
    assert card_next_actions(state, policy, max_nodes=2) == [{
        "kind": "improve",
        "parent_id": 0,
        "_card_id": "hot",
        "_scores": {0: 0.9},
        "_chosen": 0,
        "_reason": "exploit best",
    }]
    assert state.model_dump(mode="json") == before


def test_seed_phase_is_wide_and_uses_the_effective_budget_count():
    state = RunState(nodes={0: _node(0), 1: _node(1, tombstoned=True)})
    policy = GreedyTree(n_seeds=3, max_nodes=4, debug_depth=0)

    assert card_next_actions(state, policy, max_nodes=4) == [
        {"kind": "draft"}, {"kind": "draft"},
    ]


def test_asha_seed_phase_counts_effective_roots_not_nonroot_attempts():
    state = RunState(nodes={
        0: _node(0),
        1: _node(1, operator="improve", parents=(0,)),
    })
    policy = ASHAPolicy(n_seeds=3, max_nodes=6, eta=2, debug_depth=0)

    assert card_next_actions(state, policy, max_nodes=6) == [
        {"kind": "draft"}, {"kind": "draft"},
    ]


def test_eligibility_requires_selection_ready_and_current_operator_anchors():
    ready = _ready_card("ready", parents=(0,))
    not_ready = _ready_card("not-ready", parents=(0,)).model_copy(update={
        "selection_ready": False,
        "selection_blockers": ["freshness_stale"],
    })
    poisoned_gated = _ready_card("gated", parents=(0,)).model_copy(update={
        "status": "gated",
        "selection_ready": False,
        "selection_blockers": ["card_terminal"],
    })
    missing_anchor = _ready_card("missing", parents=(99,))
    state = RunState(
        nodes={0: _node(0)},
        cards={card.id: card for card in (ready, not_ready, poisoned_gated, missing_anchor)},
    )

    assert [card.id for card in eligible_cards(state, _FallbackPolicy([]))] == ["ready"]


def test_merge_card_requires_both_current_top_two_anchors():
    state = RunState(
        direction="max",
        nodes={
            0: _node(0, metric=0.9),
            1: _node(1, metric=0.8),
            2: _node(2, metric=0.7),
        },
        cards={
            "fresh": _ready_card("fresh", operator="merge", parents=(0, 1)),
            "stale-second": _ready_card("stale-second", operator="merge", parents=(0, 2)),
        },
    )

    assert [card.id for card in eligible_cards(state, _FallbackPolicy([]))] == ["fresh"]


def test_dropped_gated_and_not_ready_cards_are_never_sent_to_a_policy_scorer():
    ready = _ready_card("ready")
    gated = _ready_card("gated").model_copy(update={
        "status": "gated",
        "selection_ready": False,
        "selection_blockers": ["card_terminal"],
    })
    dropped = _ready_card("dropped").model_copy(update={
        "status": "dropped",
        "dropped_reason": "duplicate",
        "selection_ready": False,
        "selection_blockers": ["card_terminal"],
    })
    not_ready = _ready_card("not-ready").model_copy(update={"selection_ready": False})
    state = RunState(
        nodes={0: _node(0)},
        best_node_id=0,
        cards={card.id: card for card in (ready, gated, dropped, not_ready)},
    )
    policy = _ScoringPolicy([{"kind": "improve", "parent_id": 0}])

    assert [card.id for card in card_selection_set(state, policy, 8)] == ["ready"]
    assert policy.scored == ["ready"]


def test_policy_without_card_score_falls_back_once_even_when_a_ready_card_exists():
    state = RunState(
        nodes={0: _node(0)},
        best_node_id=0,
        cards={"ready": _ready_card("ready")},
    )
    policy = _FallbackPolicy([{"kind": "improve", "parent_id": 0}])

    assert card_next_actions(state, policy, 8) == [{"kind": "improve", "parent_id": 0}]
    assert policy.calls == 1


def test_non_forced_empty_fallback_cannot_finish_while_budget_remains():
    state = RunState(nodes={0: _node(0)}, best_node_id=0)
    policy = _FallbackPolicy([])

    assert card_next_actions(state, policy, 2) == [{"kind": "draft"}]
    assert card_next_actions(state, policy, 1) == []


def test_custom_fallback_creation_batch_is_clipped_to_effective_remaining_budget():
    state = RunState(nodes={0: _node(0)}, best_node_id=0)
    policy = _FallbackPolicy([{"kind": "draft"} for _ in range(8)])

    assert card_next_actions(state, policy, 3) == [
        {"kind": "draft"}, {"kind": "draft"},
    ]


def test_greedy_single_hot_card_matches_the_legacy_best_anchor_and_carries_card_id():
    state = RunState(
        direction="max",
        nodes={0: _node(0, metric=0.4), 1: _node(1, metric=0.9)},
        best_node_id=1,
        cards={
            "cold": _ready_card("cold", parents=(0,), novelty_level=5),
            "hot": _ready_card("hot", parents=(1,)),
        },
    )
    policy = GreedyTree(n_seeds=1, max_nodes=8, debug_depth=0)

    assert policy.next_actions(state)[0]["parent_id"] == 1
    assert card_next_actions(state, policy, 8) == [
        {
            "kind": "improve", "parent_id": 1, "_card_id": "hot",
            "_scores": {0: 0.4, 1: 0.9}, "_chosen": 1, "_reason": "exploit best",
        },
    ]


def test_due_merge_and_ablate_cadence_cannot_be_replaced_by_unpinned_open_cards():
    merge_state = RunState(
        direction="max",
        nodes={
            0: _node(0, metric=0.9),
            1: _node(1, metric=0.8),
            2: _node(2, operator="improve", parents=(0,), metric=0.7),
            3: _node(3, operator="improve", parents=(0,), metric=0.6),
            4: _node(4, operator="improve", parents=(0,), metric=0.5),
        },
        best_node_id=0,
        cards={"open-improve": _ready_card("open-improve", parents=(0,))},
    )
    merge_policy = GreedyTree(
        n_seeds=2, max_nodes=10, debug_depth=0, merge_every=3, ablate_every=0,
    )
    assert card_next_actions(merge_state, merge_policy, 10) == [{
        "kind": "merge",
        "parent_ids": [0, 1],
        "_scores": {0: 0.9, 1: 0.8},
        "_chosen": 0,
        "_reason": "merge top-2",
    }]

    ablate_state = RunState(
        direction="max",
        nodes={
            0: _node(0, metric=0.8, params={"a": 1.0, "b": 2.0}),
            1: _node(
                1, operator="improve", parents=(0,), metric=0.9,
                params={"a": 2.0, "b": 3.0},
            ),
        },
        best_node_id=1,
        cards={"open-improve": _ready_card("open-improve", parents=(1,))},
    )
    ablate_policy = GreedyTree(
        n_seeds=1, max_nodes=8, debug_depth=0, enable_merge=False, ablate_every=1,
    )
    assert card_next_actions(ablate_state, ablate_policy, 8) == [{
        "kind": "ablate",
        "parent_id": 1,
        "_scores": {0: 0.8, 1: 0.9},
        "_chosen": 1,
        "_reason": "ablate highest-impact param",
    }]


def test_operator_pin_may_explicitly_override_due_ablate_cadence():
    state = RunState(
        direction="max",
        nodes={
            0: _node(0, metric=0.8, params={"a": 1.0, "b": 2.0}),
            1: _node(
                1, operator="improve", parents=(0,), metric=0.9,
                params={"a": 2.0, "b": 3.0},
            ),
        },
        best_node_id=1,
        cards={"operator-pin": _ready_card("operator-pin", parents=(0,), pinned=True)},
    )
    policy = GreedyTree(
        n_seeds=1, max_nodes=8, debug_depth=0, enable_merge=False, ablate_every=1,
    )

    assert card_next_actions(state, policy, 8) == [{
        "kind": "improve", "parent_id": 0, "_card_id": "operator-pin",
    }]


def test_bandit_chosen_parent_cannot_be_replaced_by_an_unpinned_same_operator_card():
    class _BanditDecisionGreedy(GreedyTree):
        def next_actions(self, _state):
            return [{
                "kind": "improve", "parent_id": 1,
                "_scores": {0: 0.4, 1: 0.9}, "_chosen": 1,
                "_reason": "bandit: exploit best",
            }]

    state = RunState(
        nodes={0: _node(0, metric=0.4), 1: _node(1, metric=0.9)},
        best_node_id=1,
        cards={"wrong-parent": _ready_card("wrong-parent", parents=(0,))},
    )
    policy = _BanditDecisionGreedy(n_seeds=1, max_nodes=8, debug_depth=0)

    assert card_next_actions(state, policy, 8) == [{
        "kind": "improve", "parent_id": 1,
        "_scores": {0: 0.4, 1: 0.9}, "_chosen": 1,
        "_reason": "bandit: exploit best",
    }]


def test_non_string_optional_policy_metadata_is_ignored_without_crashing():
    class _HostileMetadataGreedy(GreedyTree):
        def next_actions(self, _state):
            return [{
                "kind": "improve", "parent_id": 0,
                "_reason": "custom policy", 7: "not a metadata key",
            }]

    state = RunState(
        nodes={0: _node(0)},
        best_node_id=0,
        cards={"ready": _ready_card("ready", parents=(0,))},
    )
    policy = _HostileMetadataGreedy(n_seeds=1, max_nodes=8, debug_depth=0)

    assert card_next_actions(state, policy, 8) == [{
        "kind": "improve", "parent_id": 0, "_card_id": "ready",
        "_reason": "custom policy",
    }]


def test_operator_pinned_card_owns_the_top_band_over_the_policy_hot_card():
    state = RunState(
        direction="max",
        nodes={0: _node(0, metric=0.4), 1: _node(1, metric=0.9)},
        best_node_id=1,
        cards={
            "pinned": _ready_card("pinned", parents=(0,), pinned=True),
            "hot": _ready_card("hot", parents=(1,)),
        },
    )

    assert card_next_actions(
        state, GreedyTree(n_seeds=1, max_nodes=8, debug_depth=0), 8,
    ) == [{"kind": "improve", "parent_id": 0, "_card_id": "pinned"}]


def test_explore_vs_exploit_stance_changes_only_the_open_band_order():
    state = RunState(
        nodes={0: _node(0, concepts=("seen",))},
        node_concepts={0: ["seen"]},
        best_node_id=0,
        cards={
            "exploit": _ready_card(
                "exploit", concepts=("seen",), confidence=1.0, novelty_level=0,
            ),
            "explore": _ready_card(
                "explore", concepts=("new",), confidence=0.0, novelty_level=5,
            ),
        },
    )
    policy = GreedyTree(n_seeds=1, max_nodes=8, debug_depth=0)

    exploit = card_next_actions(
        state, policy, 8,
        scoring={"stance": "exploit", "novelty_weight": 1.0, "coverage_weight": 0.0},
    )
    explore = card_next_actions(
        state, policy, 8,
        scoring={"stance": "explore", "novelty_weight": 1.0, "coverage_weight": 0.0},
    )
    assert exploit[0]["_card_id"] == "exploit"
    assert explore[0]["_card_id"] == "explore"


def test_equal_scores_use_card_id_as_the_stable_final_tie_break():
    state = RunState(
        nodes={0: _node(0)},
        best_node_id=0,
        cards={
            "z-card": _ready_card("z-card"),
            "a-card": _ready_card("a-card"),
        },
    )
    policy = GreedyTree(n_seeds=1, max_nodes=8, debug_depth=0)

    assert [card.id for card in card_selection_set(state, policy, 8)] == ["a-card"]
    assert [card.id for card in card_selection_set(state, policy, 8)] == ["a-card"]


def test_population_lane_is_top_k_and_diversifies_duplicate_action_concept_niches():
    state = RunState(
        direction="max",
        nodes={
            0: _node(0, metric=4.0),
            1: _node(1, metric=3.0),
            2: _node(2, metric=2.0),
            # One prior mutation puts EvolutionaryPolicy on its improve (not due-merge) turn.
            3: _node(3, operator="improve", parents=(0,), metric=1.0),
        },
        best_node_id=0,
    )
    cards = (
        _ready_card("10-alpha", parents=(0,), concepts=("axis/alpha",)),
        _ready_card("11-alpha-duplicate", parents=(0,), concepts=("axis/alpha",)),
        _ready_card("20-beta", parents=(0,), concepts=("axis/beta",)),
        _ready_card("30-gamma", parents=(0,), concepts=("axis/gamma",)),
    )
    state.cards = {card.id: card for card in cards}
    policy = EvolutionaryPolicy(pop=4, max_nodes=10, elite=3, debug_depth=0)

    assert [card.id for card in card_selection_set(state, policy, 10)] == [
        "10-alpha", "20-beta", "30-gamma",
    ]


def test_mcts_card_lane_keeps_ucb_hot_parent_and_widens_to_seed_count():
    state = RunState(
        direction="max",
        nodes={
            0: _node(0, metric=0.9),
            1: _node(1, metric=0.7),
            2: _node(2, metric=0.5),
        },
        best_node_id=0,
        cards={
            "00-hot": _ready_card("00-hot", parents=(0,), concepts=("hot",)),
            "10-open": _ready_card("10-open", parents=(1,), concepts=("open-a",)),
            "20-open": _ready_card("20-open", parents=(2,), concepts=("open-b",)),
            "30-overflow": _ready_card("30-overflow", parents=(1,), concepts=("open-c",)),
        },
    )
    policy = MCTSPolicy(n_seeds=3, max_nodes=8, c=1.4, debug_depth=0)

    assert policy.next_actions(state)[0]["parent_id"] == 0
    assert [card.id for card in card_selection_set(state, policy, 8)] == [
        "00-hot", "10-open", "20-open",
    ]
    assert [action["_card_id"] for action in card_next_actions(state, policy, 8)] == [
        "00-hot", "10-open", "20-open",
    ]


def test_asha_lane_uses_only_current_survivors_and_stamps_every_promotion():
    roots = {node_id: _node(node_id, metric=1.0 - node_id / 10) for node_id in range(4)}
    state = RunState(direction="max", nodes=roots, best_node_id=0)
    cards = (
        _ready_card("survivor-a", parents=(0,), concepts=("a",)),
        _ready_card("survivor-b", parents=(1,), concepts=("b",)),
        _ready_card("non-survivor", parents=(2,), concepts=("c",)),
    )
    state.cards = {card.id: card for card in cards}
    policy = ASHAPolicy(n_seeds=4, max_nodes=12, eta=2, debug_depth=0)

    assert [card.id for card in card_selection_set(state, policy, 12)] == [
        "survivor-a", "survivor-b",
    ]
    assert card_next_actions(state, policy, 12) == [
        {
            "kind": "improve", "parent_id": 0, "_card_id": "survivor-a",
            "_scores": {0: 1.0, 1: 0.9, 2: 0.8, 3: 0.7},
            "_chosen": 0, "_reason": "promote rung 1",
            "_rung": 1, "_promoted": [0, 1],
        },
        {
            "kind": "improve", "parent_id": 1, "_card_id": "survivor-b",
            "_scores": {0: 1.0, 1: 0.9, 2: 0.8, 3: 0.7},
            "_chosen": 1, "_reason": "promote rung 1",
            "_rung": 1, "_promoted": [0, 1],
        },
    ]


def test_asha_excludes_expanded_and_non_survivors_from_current_rung():
    nodes = {node_id: _node(node_id, metric=1.0 - node_id / 10) for node_id in range(4)}
    nodes[4] = _node(4, operator="improve", parents=(0,), metric=0.95)
    state = RunState(direction="max", nodes=nodes, best_node_id=0)
    cards = (
        _ready_card("already-expanded", parents=(0,)),
        _ready_card("still-legal", parents=(1,)),
        _ready_card("non-survivor", parents=(2,)),
        _ready_card("future-rung", parents=(4,)),
    )
    state.cards = {card.id: card for card in cards}
    policy = ASHAPolicy(n_seeds=4, max_nodes=12, eta=2, debug_depth=0)

    assert [card.id for card in card_selection_set(state, policy, 12)] == ["still-legal"]
    assert card_next_actions(state, policy, 12) == [{
        "kind": "improve", "parent_id": 1, "_card_id": "still-legal",
        "_scores": {0: 1.0, 1: 0.9, 2: 0.8, 3: 0.7},
        "_chosen": 1, "_reason": "promote rung 1",
        "_rung": 1, "_promoted": [0, 1],
    }]


def test_asha_higher_rung_uses_that_rungs_survivor_width_not_rung_zero_width():
    nodes = {
        0: _node(0, metric=1.0),
        1: _node(1, metric=0.9),
        2: _node(2, metric=0.8),
        3: _node(3, metric=0.7),
        4: _node(4, operator="improve", parents=(0,), metric=0.95),
        5: _node(5, operator="improve", parents=(1,), metric=0.85),
    }
    state = RunState(direction="max", nodes=nodes, best_node_id=0)
    state.cards = {
        "rung-two-survivor": _ready_card("rung-two-survivor", parents=(4,)),
        "rung-two-cut": _ready_card("rung-two-cut", parents=(5,)),
    }
    policy = ASHAPolicy(n_seeds=4, max_nodes=12, eta=2, debug_depth=0)

    assert [card.id for card in card_selection_set(state, policy, 12)] == [
        "rung-two-survivor",
    ]
    assert card_next_actions(state, policy, 12) == [{
        "kind": "improve", "parent_id": 4, "_card_id": "rung-two-survivor",
        "_scores": {4: 0.95, 5: 0.85},
        "_chosen": 4, "_reason": "promote rung 2",
        "_rung": 2, "_promoted": [4],
    }]


@pytest.mark.parametrize(("operator", "parents", "expected"), [
    ("draft", (), {"kind": "draft", "_card_id": "card"}),
    ("improve", (1,), {"kind": "improve", "parent_id": 1, "_card_id": "card"}),
    ("expand", (1,), {"kind": "improve", "parent_id": 1, "_card_id": "card"}),
    ("debug", (1,), {"kind": "debug", "parent_id": 1, "_card_id": "card"}),
    ("merge", (1, 2), {"kind": "merge", "parent_ids": [1, 2], "_card_id": "card"}),
])
def test_card_action_is_bounded_and_preserves_internal_claim_identity(operator, parents, expected):
    assert card_action(_ready_card("card", operator=operator, parents=parents)) == expected


def test_action_key_merge_is_order_independent():
    # Regression: `_action_key` keyed merge on the RAW parent order, so a merge Card whose parent_ids
    # were stored reversed from the policy fallback's rank order missed `exact_match` and, on a due-merge
    # tick, the `due_key ==` filter dropped it — orphaning the ready Card. A merge is symmetric in its
    # parents (and `_live_card_action` already accepts it order-independently), so the key must too.
    from looplab.search.card_selection import _action_key
    forward = _action_key({"kind": "merge", "parent_ids": [1, 2]})
    reverse = _action_key({"kind": "merge", "parent_ids": [2, 1]})
    assert forward == reverse == ("merge", (1, 2))
    # single-parent kinds are unaffected
    assert _action_key({"kind": "improve", "parent_id": 3}) == ("improve", (3,))


def test_scoring_normalization_is_bounded_and_fail_closed():
    assert normalize_card_scoring({
        "stance": "unknown",
        "novelty_weight": float("nan"),
        "coverage_weight": 99,
    }) == CardScoring(stance="balanced", novelty_weight=0.5, coverage_weight=1.0)
