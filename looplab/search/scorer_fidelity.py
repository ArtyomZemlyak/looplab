"""Deterministic scorer-fidelity gate for Card-driven GreedyTree selection.

The gate is deliberately pure and self-contained: it reads no configuration, files, clocks, or
environment variables.  It evaluates a fixed, bounded matrix twice -- once through the legacy
``GreedyTree.next_actions`` authority and once through ``card_next_actions`` with ready matching and
distractor Cards -- and returns a JSON-ready report.  Action semantics are compared after removing
``_card_id``; exact Card ownership (including an explicit absence) is checked independently so a
legacy-policy result cannot masquerade as a Card-owned treatment result.  Policy audit metadata
remains part of the semantic equality contract.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from looplab.core.models import (
    Card,
    CardIdentityProvenance,
    CardSelectionProvenance,
    Idea,
    Node,
    NodeStatus,
    RunState,
)
from looplab.search.card_selection import META_CARD_ID, card_next_actions
from looplab.search.policy import GreedyTree, operator_yields


SCORER_FIDELITY_SCHEMA = "looplab.card-scorer-fidelity/v1"
SCORER_FIDELITY_CASE_COUNT = 15
SCORER_FIDELITY_CASE_NAMES = (
    "forced_pending",
    "forced_seed",
    "forced_debug",
    "forced_budget",
    "direction_min",
    "direction_max",
    "merge_every_before",
    "merge_every_at",
    "merge_every_after",
    "ablate_every_before",
    "ablate_every_at",
    "ablate_every_after",
    "bandit_untried_merge",
    "bandit_untried_ablate",
    "bandit_yield_improve",
)
__all__ = [
    "SCORER_FIDELITY_SCHEMA",
    "SCORER_FIDELITY_CASE_COUNT",
    "SCORER_FIDELITY_CASE_NAMES",
    "scorer_fidelity_gate",
]
_ACTION_DIGEST = "card-action:v1:" + "0" * 64
_MAX_CASES = 32
_MAX_ACTIONS_PER_CASE = 16
_MAX_ACTION_JSON_CHARS = 4096
_MAX_ERROR_CHARS = 240
_MAX_CARD_ID_CHARS = 256

if (
    len(SCORER_FIDELITY_CASE_NAMES) != SCORER_FIDELITY_CASE_COUNT
    or len(set(SCORER_FIDELITY_CASE_NAMES)) != SCORER_FIDELITY_CASE_COUNT
):
    raise AssertionError("scorer-fidelity canonical case matrix must contain 15 unique names")

Action = dict[str, Any]
CardActions = Callable[[RunState, object, int], Sequence[Mapping[str, Any]]]


@dataclass(frozen=True)
class _Case:
    name: str
    state: RunState
    policy: GreedyTree
    max_nodes: int
    # One entry per legacy policy action. ``None`` means _card_id must be absent, not merely null.
    expected_ownership: tuple[str | None, ...]


def _node(
    node_id: int,
    *,
    metric: float | None = 0.5,
    operator: str = "draft",
    parents: tuple[int, ...] = (),
    status: NodeStatus = NodeStatus.evaluated,
    eval_seconds: float = 1.0,
    params: Mapping[str, float] | None = None,
) -> Node:
    return Node(
        id=node_id,
        parent_ids=list(parents),
        operator=operator,
        idea=Idea(
            operator=operator,
            params=dict(params or {"x": float(node_id + 1), "y": 1.0}),
        ),
        metric=metric,
        status=status,
        eval_seconds=eval_seconds,
    )


def _ready_card(
    card_id: str,
    *,
    operator: str,
    parents: tuple[int, ...] = (),
) -> Card:
    return Card(
        id=card_id,
        statement=f"fidelity proposal {card_id}",
        seed_statement=f"fidelity proposal {card_id}",
        source="engine",
        status="proposed",
        verdict="open",
        identity=CardIdentityProvenance(
            kind="native",
            source="card_added_receipt",
            durable=True,
            receipt_valid=True,
            action_digest=_ACTION_DIGEST,
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
    )


def _state(
    nodes: Sequence[Node] = (),
    *,
    direction: str = "max",
    cards: Sequence[Card] = (),
) -> RunState:
    evaluated = [
        node for node in nodes
        if node.status is NodeStatus.evaluated and node.metric is not None and node.feasible
    ]
    best_node_id: int | None = None
    if evaluated:
        chooser = max if direction == "max" else min
        best_node_id = chooser(evaluated, key=lambda node: float(node.metric)).id
    return RunState(
        direction=direction,
        nodes={node.id: node for node in nodes},
        best_node_id=best_node_id,
        cards={card.id: card for card in cards},
    )


def _extrema_case(direction: str) -> _Case:
    best_id = 1 if direction == "max" else 0
    other_id = 1 - best_id
    state = _state(
        [_node(0, metric=0.2), _node(1, metric=0.9)],
        direction=direction,
        cards=[
            _ready_card(f"{direction}-distractor", operator="improve", parents=(other_id,)),
            _ready_card(f"{direction}-matching", operator="improve", parents=(best_id,)),
        ],
    )
    return _Case(
        name=f"direction_{direction}",
        state=state,
        policy=GreedyTree(n_seeds=1, max_nodes=8, debug_depth=0, enable_merge=False),
        max_nodes=8,
        expected_ownership=(f"{direction}-matching",),
    )


def _merge_cases() -> list[_Case]:
    roots = [_node(0, metric=0.60), _node(1, metric=0.50)]
    before_nodes = [
        *roots,
        _node(2, metric=0.70, operator="improve", parents=(0,)),
        _node(3, metric=0.80, operator="improve", parents=(2,)),
    ]
    at_nodes = [
        *before_nodes,
        _node(4, metric=0.90, operator="improve", parents=(3,)),
    ]
    after_nodes = [
        *at_nodes,
        _node(5, metric=0.95, operator="merge", parents=(4, 3)),
    ]

    def policy() -> GreedyTree:
        return GreedyTree(
            n_seeds=2,
            max_nodes=12,
            debug_depth=0,
            merge_every=3,
            ablate_every=0,
        )

    return [
        _Case(
            "merge_every_before",
            _state(before_nodes, cards=[
                _ready_card("merge-before-distractor", operator="improve", parents=(0,)),
                _ready_card("merge-before-matching", operator="improve", parents=(3,)),
            ]),
            policy(),
            12,
            ("merge-before-matching",),
        ),
        _Case(
            "merge_every_at",
            _state(at_nodes, cards=[
                _ready_card("merge-at-distractor", operator="improve", parents=(4,)),
                _ready_card("merge-at-matching", operator="merge", parents=(4, 3)),
            ]),
            policy(),
            12,
            ("merge-at-matching",),
        ),
        _Case(
            "merge_every_after",
            _state(after_nodes, cards=[
                _ready_card("merge-after-distractor", operator="improve", parents=(4,)),
                _ready_card("merge-after-matching", operator="improve", parents=(5,)),
            ]),
            policy(),
            12,
            ("merge-after-matching",),
        ),
    ]


def _ablate_cases() -> list[_Case]:
    roots = [_node(0, metric=0.50)]
    before_nodes = [
        *roots,
        _node(1, metric=0.70, operator="improve", parents=(0,)),
    ]
    at_nodes = [
        *before_nodes,
        _node(2, metric=0.80, operator="improve", parents=(1,)),
    ]
    after_nodes = [
        *at_nodes,
        _node(3, metric=0.85, operator="refine_block", parents=(2,)),
    ]

    def policy() -> GreedyTree:
        return GreedyTree(
            n_seeds=1,
            max_nodes=10,
            debug_depth=0,
            enable_merge=False,
            ablate_every=2,
        )

    return [
        _Case(
            "ablate_every_before",
            _state(before_nodes, cards=[
                _ready_card("ablate-before-distractor", operator="improve", parents=(0,)),
                _ready_card("ablate-before-matching", operator="improve", parents=(1,)),
            ]),
            policy(),
            10,
            ("ablate-before-matching",),
        ),
        _Case(
            "ablate_every_at",
            _state(at_nodes, cards=[
                # Ablation is an engine-side probe rather than a buildable Card action.  A ready
                # distractor proves the protected cadence falls back to the exact policy action.
                _ready_card("ablate-at-distractor", operator="improve", parents=(2,)),
            ]),
            policy(),
            10,
            (None,),
        ),
        _Case(
            "ablate_every_after",
            _state(after_nodes, cards=[
                _ready_card("ablate-after-distractor", operator="improve", parents=(2,)),
                _ready_card("ablate-after-matching", operator="improve", parents=(3,)),
            ]),
            policy(),
            10,
            ("ablate-after-matching",),
        ),
    ]


def _bandit_cases() -> list[_Case]:
    untried_merge_nodes = [
        _node(0, metric=0.50),
        _node(1, metric=0.40),
        _node(2, metric=0.60, operator="improve", parents=(0,)),
    ]
    untried_ablate_nodes = [
        _node(0, metric=0.50),
        _node(1, metric=0.45),
        _node(2, metric=0.60, operator="improve", parents=(0,)),
        _node(3, metric=0.61, operator="merge", parents=(2, 1)),
    ]
    yield_improve_nodes = [
        _node(0, metric=0.50),
        _node(1, metric=0.45),
        _node(2, metric=0.80, operator="improve", parents=(0,), eval_seconds=1.0),
        _node(3, metric=0.90, operator="improve", parents=(2,), eval_seconds=1.0),
        _node(4, metric=0.45, operator="merge", parents=(0, 1), eval_seconds=1.0),
    ]
    # Both legal operators have evidence, but their counts differ.  This forces _bandit_pick through
    # the UCB exploration-bonus calculation instead of either the untried fast path or equal-count
    # cancellation; improve's measured yield still wins deterministically.
    yield_counts = {
        operator: stats["n"]
        for operator, stats in operator_yields(_state(yield_improve_nodes)).items()
    }
    if yield_counts != {"improve": 2, "merge": 1}:
        raise AssertionError("bandit_yield_improve must exercise unequal nonzero operator counts")

    return [
        _Case(
            "bandit_untried_merge",
            _state(untried_merge_nodes, cards=[
                _ready_card("bandit-merge-distractor", operator="improve", parents=(2,)),
                _ready_card("bandit-merge-matching", operator="merge", parents=(2, 0)),
            ]),
            GreedyTree(
                n_seeds=2,
                max_nodes=12,
                debug_depth=0,
                operator_bandit=True,
                max_merges=3,
                ablate_every=0,
            ),
            12,
            ("bandit-merge-matching",),
        ),
        _Case(
            "bandit_untried_ablate",
            _state(untried_ablate_nodes, cards=[
                _ready_card("bandit-ablate-distractor", operator="improve", parents=(3,)),
                _ready_card("bandit-ablate-other", operator="merge", parents=(3, 2)),
            ]),
            GreedyTree(
                n_seeds=2,
                max_nodes=12,
                debug_depth=0,
                operator_bandit=True,
                max_merges=3,
                ablate_every=1,
            ),
            12,
            (None,),
        ),
        _Case(
            "bandit_yield_improve",
            _state(yield_improve_nodes, cards=[
                _ready_card("bandit-yield-distractor", operator="improve", parents=(0,)),
                _ready_card("bandit-yield-matching", operator="improve", parents=(3,)),
                _ready_card("bandit-yield-merge", operator="merge", parents=(3, 2)),
            ]),
            GreedyTree(
                n_seeds=2,
                max_nodes=12,
                debug_depth=0,
                operator_bandit=True,
                max_merges=3,
                ablate_every=0,
            ),
            12,
            ("bandit-yield-matching",),
        ),
    ]


def _cases() -> list[_Case]:
    pending_state = _state(
        [
            _node(3, metric=None, status=NodeStatus.pending),
            _node(0, metric=None, status=NodeStatus.failed),
            _node(1, metric=None, status=NodeStatus.pending),
        ],
        cards=[_ready_card("pending-distractor", operator="draft")],
    )
    seed_state = _state(cards=[
        _ready_card("seed-a", operator="draft"),
        _ready_card("seed-b", operator="draft"),
    ])
    debug_state = _state(
        [
            _node(0, metric=0.70),
            _node(1, metric=None, operator="improve", parents=(0,), status=NodeStatus.failed),
        ],
        cards=[
            _ready_card("debug-distractor", operator="improve", parents=(0,)),
            _ready_card("debug-matching", operator="debug", parents=(1,)),
        ],
    )
    budget_state = _state(
        [_node(0, metric=0.60), _node(1, metric=0.70)],
        cards=[
            _ready_card("budget-distractor", operator="improve", parents=(0,)),
            _ready_card("budget-matching-looking", operator="improve", parents=(1,)),
        ],
    )
    cases = [
        _Case(
            "forced_pending",
            pending_state,
            GreedyTree(n_seeds=0, max_nodes=0, debug_depth=1),
            0,
            (None, None),
        ),
        _Case(
            "forced_seed",
            seed_state,
            GreedyTree(n_seeds=2, max_nodes=2, debug_depth=0),
            2,
            ("seed-a", "seed-b"),
        ),
        _Case(
            "forced_debug",
            debug_state,
            GreedyTree(n_seeds=1, max_nodes=3, debug_depth=1, enable_merge=False),
            3,
            ("debug-matching",),
        ),
        _Case(
            "forced_budget",
            budget_state,
            GreedyTree(n_seeds=1, max_nodes=2, debug_depth=0),
            2,
            (),
        ),
        _extrema_case("min"),
        _extrema_case("max"),
        *_merge_cases(),
        *_ablate_cases(),
        *_bandit_cases(),
    ]
    names = tuple(case.name for case in cases)
    if names != SCORER_FIDELITY_CASE_NAMES:
        raise AssertionError("scorer-fidelity case matrix differs from its canonical 15-case order")
    if len(cases) > _MAX_CASES:  # second hard bound; never return an unbounded report
        raise AssertionError("scorer-fidelity case matrix exceeds its hard bound")
    return cases


def _canonical_actions(
    raw: object,
) -> tuple[list[Action], list[str | None], str | None]:
    """Copy bounded action semantics and extract exact Card ownership independently."""

    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return [], [], f"invalid action result: {type(raw).__name__}"[:_MAX_ERROR_CHARS]
    if len(raw) > _MAX_ACTIONS_PER_CASE:
        return [], [], f"action result exceeds {_MAX_ACTIONS_PER_CASE} items"[:_MAX_ERROR_CHARS]
    actions: list[Action] = []
    ownership: list[str | None] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            return [], [], f"action {index} is not a mapping"[:_MAX_ERROR_CHARS]
        card_id: str | None = None
        if META_CARD_ID in item:
            raw_card_id = item[META_CARD_ID]
            if (
                not isinstance(raw_card_id, str)
                or not raw_card_id
                or len(raw_card_id) > _MAX_CARD_ID_CHARS
                or not raw_card_id.isprintable()
            ):
                return [], [], f"action {index} has invalid {META_CARD_ID}"[:_MAX_ERROR_CHARS]
            card_id = raw_card_id
        action = {key: value for key, value in item.items() if key != META_CARD_ID}
        try:
            encoded = json.dumps(action, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as exc:
            return [], [], f"action {index} is not JSON-ready: {type(exc).__name__}"[:_MAX_ERROR_CHARS]
        if len(encoded) > _MAX_ACTION_JSON_CHARS:
            return [], [], f"action {index} exceeds {_MAX_ACTION_JSON_CHARS} JSON chars"[:_MAX_ERROR_CHARS]
        actions.append(action)
        ownership.append(card_id)
    return actions, ownership, None


def scorer_fidelity_gate(
    card_actions: CardActions = card_next_actions,
) -> dict[str, object]:
    """Return the bounded v1 Card/Greedy scorer-fidelity report.

    ``card_actions`` is an explicit pure test seam; the default is the production
    :func:`card_next_actions`.  A candidate exception or malformed/oversized action list is recorded
    as one mismatch instead of escaping the fail-closed report.
    """

    case_results: list[dict[str, object]] = []
    for case in _cases():
        expected_raw = case.policy.next_actions(case.state)
        expected, legacy_ownership, expected_error = _canonical_actions(expected_raw)
        expected_ownership = list(case.expected_ownership)
        if expected_error is None and any(card_id is not None for card_id in legacy_ownership):
            expected_error = "legacy policy unexpectedly returned Card ownership"
        if expected_error is None and len(expected_ownership) != len(expected):
            expected_error = "source-owned Card ownership count differs from legacy action count"
        actual_error: str | None = None
        try:
            actual_raw = card_actions(case.state, case.policy, case.max_nodes)
            actual, actual_ownership, actual_error = _canonical_actions(actual_raw)
        except Exception as exc:  # a gate must report candidate failure, never turn it into approval
            actual = []
            actual_ownership = []
            actual_error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
        error = expected_error or actual_error
        semantics_passed = error is None and expected == actual
        ownership_passed = error is None and expected_ownership == actual_ownership
        matched = semantics_passed and ownership_passed
        result: dict[str, object] = {
            "name": case.name,
            "passed": matched,
            "expected": expected,
            "actual": actual,
            "expected_ownership": expected_ownership,
            "actual_ownership": actual_ownership,
            "semantics_passed": semantics_passed,
            "ownership_passed": ownership_passed,
        }
        if error is not None:
            result["error"] = error
        case_results.append(result)

    result_names = tuple(str(result["name"]) for result in case_results)
    if result_names != SCORER_FIDELITY_CASE_NAMES:
        raise AssertionError("scorer-fidelity report does not account for every canonical case")
    mismatches = sum(not bool(result["passed"]) for result in case_results)
    return {
        "schema": SCORER_FIDELITY_SCHEMA,
        "passed": (
            len(case_results) == SCORER_FIDELITY_CASE_COUNT
            and mismatches == 0
            and all(bool(result["passed"]) for result in case_results)
        ),
        "cases": len(case_results),
        "mismatches": mismatches,
        "case_results": case_results,
    }
