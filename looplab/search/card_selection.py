"""Pure, feature-gate-ready Card selection primitives (docs/23, Layer 3).

This module deliberately owns no configuration and writes no events.  The orchestrator can call
``card_next_actions`` from the future ``card_driven_selection`` arm and later atomically claim the
selected work item by the private ``_card_id`` carried on each returned action.

The queue boundary is fail closed: only folded ``Card.selection_ready`` work items are considered,
and their operator-specific anchors are checked again against the current state.  A policy extension
is optional.  Built-in policies use the deterministic scorer below; an unknown policy must provide a
``card_score(state, card, *, scoring=...)`` hook or selection falls back to ``next_actions``.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from looplab.core.models import Card, Node, NodeStatus, RunState
from looplab.search.policy import _ASHA_MAX_FAILED_PROMOTIONS, debug_action, rank_by_metric


META_CARD_ID = "_card_id"

Action = dict[str, Any]
CardScore = tuple[float, tuple[float, ...]]


@dataclass(frozen=True)
class CardScoring:
    """Bounded scorer inputs that ``Strategy.card_scoring`` can populate later.

    The Strategist integration owns strict validation.  This pure boundary still normalizes values
    defensively so malformed resume/state data can only fall back to deterministic defaults.
    """

    stance: Literal["explore", "balanced", "exploit"] = "balanced"
    novelty_weight: float = 0.5
    coverage_weight: float = 0.5


def _unit_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number):
        return default
    return min(1.0, max(0.0, number))


def normalize_card_scoring(value: CardScoring | Mapping[str, object] | None) -> CardScoring:
    """Return a deterministic bounded scoring treatment for an optional strategy payload."""

    if isinstance(value, CardScoring):
        raw_stance: object = value.stance
        raw_novelty: object = value.novelty_weight
        raw_coverage: object = value.coverage_weight
    elif isinstance(value, Mapping):
        raw_stance = value.get("stance", "balanced")
        raw_novelty = value.get("novelty_weight", 0.5)
        raw_coverage = value.get("coverage_weight", 0.5)
    else:
        raw_stance, raw_novelty, raw_coverage = "balanced", 0.5, 0.5
    stance = raw_stance if raw_stance in {"explore", "balanced", "exploit"} else "balanced"
    return CardScoring(
        stance=stance,
        novelty_weight=_unit_float(raw_novelty, 0.5),
        coverage_weight=_unit_float(raw_coverage, 0.5),
    )


def node_counts_toward_card_budget(state: RunState, node: Node) -> bool:
    """Whether a node consumes the L3 creation budget.

    Tombstones and both kinds of current gate exclusion do not steal future search capacity:
    constraint-gated nodes have ``feasible=False`` and trust-gated nodes are in ``breed_excluded``.
    Failed and aborted attempts still count unless separately tombstoned; they consumed a real build.
    L5 may extend this predicate for freshness-dropped speculative attempts.
    """

    return (
        not node.tombstoned
        and node.feasible
        and node.id not in state.breed_excluded
    )


def card_budget_used(state: RunState) -> int:
    """L3 node-budget denominator, excluding tombstoned and gated nodes."""

    return sum(
        1 for node in state.nodes.values()
        if node_counts_toward_card_budget(state, node)
    )


def _effective_policy_state(state: RunState) -> RunState:
    """Give flag-on policy intent the same node universe as the L3 budget.

    Built-in policies historically use ``len(state.nodes)`` for their budget/seed gates and iterate
    the same mapping for merge/ablate cadence.  Passing the unfiltered state after L3 excluded a
    tombstoned or gated node from its own denominator made those two halves disagree.  A shallow
    model copy keeps the original folded state and its Nodes untouched while hiding exactly the
    nodes that do not consume the Card budget from the policy view.
    """

    effective_nodes = {
        node_id: node
        for node_id, node in state.nodes.items()
        if node_counts_toward_card_budget(state, node)
    }
    if len(effective_nodes) == len(state.nodes):
        return state

    best_node_id = state.best_node_id
    if best_node_id not in effective_nodes:
        candidates = [
            node for node in state.breedable_nodes()
            if node.id in effective_nodes
        ]
        ranked = rank_by_metric(state, candidates) if candidates else []
        best_node_id = ranked[0].id if ranked else None
    return state.model_copy(update={
        "nodes": effective_nodes,
        "best_node_id": best_node_id,
    })


def _bounded_nonnegative_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, number)


def _seed_target(policy: object) -> int:
    # ASHA's explicit rung-0 width wins; the remaining names mirror each built-in policy's seed gate.
    rung0 = _bounded_nonnegative_int(getattr(policy, "rung0", 0))
    if rung0:
        return rung0
    n_seeds = _bounded_nonnegative_int(getattr(policy, "n_seeds", 0))
    if n_seeds:
        return n_seeds
    return _bounded_nonnegative_int(getattr(policy, "pop", 0))


def _debug_depth(policy: object) -> int:
    return _bounded_nonnegative_int(getattr(policy, "debug_depth", 1), 1)


def _card_parent_ids(card: Card) -> tuple[int, ...] | None:
    parents = list(card.parent_ids or [])
    if card.parent_id is not None:
        if parents and parents[0] != card.parent_id:
            return None
        if not parents:
            parents = [card.parent_id]
    if any(isinstance(parent, bool) or not isinstance(parent, int) or parent < 0 for parent in parents):
        return None
    if len(parents) != len(set(parents)):
        return None
    return tuple(parents)


def card_action(card: Card) -> Action | None:
    """Project one immutable Card to the macro action used by the engine.

    Proposal payload is intentionally not copied into the action.  ``_card_id`` is the authority: the
    future writer must re-fold and atomically claim that exact Card before reconstructing its bounded
    Idea.  Returning ``None`` for a malformed shape keeps the queue fail closed.
    """

    parents = _card_parent_ids(card)
    if parents is None:
        return None
    if card.operator == "draft" and not parents:
        return {"kind": "draft", META_CARD_ID: card.id}
    if card.operator in {"improve", "expand"} and len(parents) == 1:
        return {"kind": "improve", "parent_id": parents[0], META_CARD_ID: card.id}
    if card.operator == "debug" and len(parents) == 1:
        return {"kind": "debug", "parent_id": parents[0], META_CARD_ID: card.id}
    if card.operator == "merge" and len(parents) == 2:
        return {"kind": "merge", "parent_ids": list(parents), META_CARD_ID: card.id}
    return None


def _strictly_selection_ready(card: Card) -> bool:
    """Reassert the public fail-closed seam before reading any scoring metadata."""

    provenance = card.selection_provenance
    identity = card.identity
    return bool(
        card.selection_ready
        and not card.selection_blockers
        and identity.kind == "native"
        and identity.durable
        and identity.receipt_valid
        and provenance.action_source == "card_added"
        and provenance.action_owner_count == 1
        and provenance.action_complete
        and provenance.freshness == "current"
        and provenance.owner_state == "none"
        and card.status == "proposed"
        and card.verdict == "open"
        and not card.evidence
        and card.dropped_reason is None
        and card.merged_into is None
    )


def _live_card_action(
    card: Card,
    *,
    breedable_ids: set[int],
    top_two_ids: set[int],
    forced_debug: Action | None,
) -> bool:
    parents = _card_parent_ids(card)
    if parents is None:
        return False
    if card.operator == "draft":
        return not parents
    if card.operator in {"improve", "expand"}:
        return len(parents) == 1 and parents[0] in breedable_ids
    if card.operator == "merge":
        # Both prospective merge anchors must still be the current policy top-2.  The receipt's
        # scored-against fence covers the best parent, but without this pure recheck the second parent
        # could silently drift between proposal and selection.
        return (
            len(parents) == 2
            and len(top_two_ids) == 2
            and set(parents) == top_two_ids
            and all(parent in breedable_ids for parent in parents)
        )
    if card.operator == "debug":
        # A failed debug anchor is intentionally not breedable.  It is live only when it is the SAME
        # first failed leaf selected by the policy's forced repair gate.  Replay currently applies the
        # breedable rule to debug too; this recheck is ready for that projection bug to be fixed without
        # ever broadening selection beyond selection_ready in the meantime.
        return bool(
            len(parents) == 1
            and forced_debug is not None
            and forced_debug.get("kind") == "debug"
            and forced_debug.get("parent_id") == parents[0]
        )
    return False


def eligible_cards(state: RunState, policy: object) -> list[Card]:
    """Return current executable Cards in stable id order.

    ``actionable`` is never consulted.  The fold's ``selection_ready`` receipt is necessary, then
    operator-specific live anchors are rechecked to close score-to-claim drift as much as a pure read can.
    The eventual writer must still repeat these checks under its id lock.
    """

    breedable = state.breedable_nodes()
    breedable_ids = {node.id for node in breedable}
    top_two_ids = {node.id for node in rank_by_metric(state, breedable)[:2]}
    forced_debug = debug_action(_effective_policy_state(state), _debug_depth(policy))
    return [
        card for _, card in sorted(state.cards.items())
        if _strictly_selection_ready(card)
        and _live_card_action(
            card,
            breedable_ids=breedable_ids,
            top_two_ids=top_two_ids,
            forced_debug=forced_debug,
        )
    ]


def _matching_ready_debug_card(state: RunState, action: Action) -> Card | None:
    parent_id = action.get("parent_id")
    for _, card in sorted(state.cards.items()):
        parents = _card_parent_ids(card)
        if (
            _strictly_selection_ready(card)
            and card.operator == "debug"
            and parents == (parent_id,)
        ):
            return card
    return None


def forced_card_actions(
    state: RunState,
    policy: object,
    max_nodes: int,
) -> list[Action] | None:
    """Apply the exact policy forced-prefix.

    ``None`` means discretionary Card scoring may run.  An empty list is therefore unambiguously the
    forced budget terminal, never "no Card happened to score".
    """

    pending = state.pending_nodes()
    if pending:
        return [{"kind": "evaluate", "node_id": node.id} for node in pending]

    used = card_budget_used(state)
    limit = _bounded_nonnegative_int(max_nodes)

    # Debug is checked before the terminal gate, while the remaining-budget guard prevents an overrun.
    # Use the effective policy view so a tombstoned/gated failed node cannot be rediscovered forever as
    # a bare debug action even though it no longer consumes capacity.
    if used < limit:
        repair = debug_action(_effective_policy_state(state), _debug_depth(policy))
        if repair is not None:
            matching = _matching_ready_debug_card(state, repair)
            if matching is not None:
                repair = {**repair, META_CARD_ID: matching.id}
            return [repair]

    if used >= limit:
        return []

    seed_target = _seed_target(policy)
    # ASHA fills a rung-0 ROOT population, not a total-node prefix. A failed/gated non-root attempt must
    # not make it skip a missing root; effective budget accounting still caps the resulting wide batch.
    if _builtin_policy_name(policy) == "ASHAPolicy":
        seeded = sum(
            1 for node in state.nodes.values()
            if not node.parent_ids and node_counts_toward_card_budget(state, node)
        )
    else:
        seeded = used
    if seeded < seed_target:
        width = min(seed_target - seeded, limit - used)
        return [{"kind": "draft"} for _ in range(width)]
    return None


def _explored_concepts(state: RunState) -> set[str]:
    explored: set[str] = set()
    for node_id, node in state.nodes.items():
        if not node_counts_toward_card_budget(state, node):
            continue
        tags = state.node_concepts.get(node_id)
        if tags is None:
            tags = node.idea.concepts
        explored.update(tag for tag in tags or [] if isinstance(tag, str) and tag)
    return explored


def _novelty_signal(card: Card) -> float:
    verdict = card.novelty_verdict
    if not isinstance(verdict, Mapping):
        return 0.0
    recommendation = verdict.get("recommendation")
    if isinstance(recommendation, str) and recommendation.lower() in {
        "block", "drop", "reject", "supersede",
    }:
        return 0.0
    level = verdict.get("level")
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        return 0.0
    number = float(level)
    if not math.isfinite(number):
        return 0.0
    return min(1.0, max(0.0, number / 5.0))


def _coverage_signal(card: Card, explored: set[str]) -> float:
    concepts = sorted({tag for tag in card.concept_tags if isinstance(tag, str) and tag})
    if not concepts:
        return 0.0
    return sum(tag not in explored for tag in concepts) / len(concepts)


def _priority_signal(card: Card) -> float:
    priority = card.priority
    if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
        return 0.0
    return 1.0 / (priority + 1.0)


def _action_key(action: Mapping[str, object]) -> tuple[str, tuple[int, ...]] | None:
    kind = action.get("kind")
    if not isinstance(kind, str):
        return None
    if kind in {"draft"}:
        return kind, ()
    if kind in {"improve", "debug", "ablate"}:
        parent = action.get("parent_id")
        if isinstance(parent, bool) or not isinstance(parent, int):
            return None
        return kind, (parent,)
    if kind == "merge":
        parents = action.get("parent_ids")
        if not isinstance(parents, (list, tuple)) or any(
            isinstance(parent, bool) or not isinstance(parent, int) for parent in parents
        ):
            return None
        return kind, tuple(parents)
    return None


def card_score(
    state: RunState,
    card: Card,
    *,
    scoring: CardScoring | Mapping[str, object] | None = None,
    policy_actions: Sequence[Mapping[str, object]] = (),
) -> CardScore:
    """Score an eligible open Card by ``(band, exploration_key)``.

    Higher tuples win.  Pinned operator work is the top band; an exact legacy-policy action is the hot
    band; same-operator and other open work follow.  Within a band, the Strategist's stance shapes the
    bounded novelty/coverage vs foresight trade-off.  Card id is deliberately absent from the score and
    is the final stable ascending tie-break in ``card_selection_set``.
    """

    treatment = normalize_card_scoring(scoring)
    projected = card_action(card)
    card_key = _action_key(projected or {})
    policy_keys = [key for action in policy_actions if (key := _action_key(action)) is not None]
    exact_match = card_key is not None and card_key in policy_keys
    same_operator = card_key is not None and any(key[0] == card_key[0] for key in policy_keys)
    band = 3.0 if card.pinned else 2.0 if exact_match else 1.0 if same_operator else 0.0

    novelty = _novelty_signal(card)
    coverage = _coverage_signal(card, _explored_concepts(state))
    total_weight = treatment.novelty_weight + treatment.coverage_weight
    exploration = (
        (treatment.novelty_weight * novelty + treatment.coverage_weight * coverage) / total_weight
        if total_weight > 0.0 else 0.0
    )
    confidence = _unit_float(card.confidence, 0.0)
    priority = _priority_signal(card)
    foresight = 0.65 * confidence + 0.35 * priority
    if treatment.stance == "explore":
        primary, secondary = exploration, foresight
    elif treatment.stance == "exploit":
        primary, secondary = foresight, exploration
    else:
        primary, secondary = (exploration + foresight) / 2.0, exploration
    key = tuple(round(value, 12) for value in (
        primary, secondary, priority, confidence, novelty, coverage,
    ))
    return band, key


_BUILTIN_POLICY_NAMES = frozenset({
    "GreedyTree", "EvolutionaryPolicy", "MCTSPolicy", "ASHAPolicy",
})


def _builtin_policy_name(policy: object) -> str | None:
    for base in type(policy).__mro__:
        if base.__module__ == "looplab.search.policy" and base.__name__ in _BUILTIN_POLICY_NAMES:
            return base.__name__
    return None


@dataclass(frozen=True)
class _ASHALane:
    """The one currently promotable ASHA rung, derived from policy output plus live state."""

    action_keys: frozenset[tuple[str, tuple[int, ...]]]
    width: int


def _asha_lane(state: RunState, fallback: Sequence[Mapping[str, object]]) -> _ASHALane:
    """Turn ASHA's current decision into a bounded legal Card lane.

    ``ASHAPolicy.next_actions`` is the authority for which rung is current and which members survived.
    The policy returns one deterministic promotion plus the full survivor receipt.  L3 may widen that
    one action only across the still-unexpanded survivors from that same receipt; it must never infer a
    rung from whichever stale Card happens to have the shallowest DAG depth.
    """

    if len(fallback) != 1:
        return _ASHALane(frozenset(), 0)
    template = fallback[0]
    exact_key = _action_key(template)
    if exact_key is None:
        return _ASHALane(frozenset(), 0)

    raw_rung = template.get("_rung")
    raw_survivors = template.get("_promoted")
    if raw_rung is None:
        # Seed/debug/budget are handled by the forced prefix.  The only remaining no-rung ASHA action
        # is its collapsed-bracket fallback, which stays exactly one policy action.
        return _ASHALane(frozenset({exact_key}), 1)
    if (
        isinstance(raw_rung, bool)
        or not isinstance(raw_rung, int)
        or raw_rung < 1
        or not isinstance(raw_survivors, list)
        or not raw_survivors
    ):
        return _ASHALane(frozenset(), 0)

    survivors: list[int] = []
    for parent_id in raw_survivors:
        if (
            isinstance(parent_id, bool)
            or not isinstance(parent_id, int)
            or parent_id < 0
            or parent_id in survivors
        ):
            return _ASHALane(frozenset(), 0)
        survivors.append(parent_id)

    has_live_child: set[int] = set()
    failed_children: dict[int, int] = {}
    for node in state.nodes.values():
        if node.status is NodeStatus.failed:
            for parent_id in node.parent_ids:
                failed_children[parent_id] = failed_children.get(parent_id, 0) + 1
        else:
            has_live_child.update(node.parent_ids)
    retired = {
        parent_id
        for parent_id, count in failed_children.items()
        if count >= _ASHA_MAX_FAILED_PROMOTIONS and parent_id not in has_live_child
    }
    breedable_ids = {node.id for node in state.breedable_nodes()}
    legal_survivors = [
        parent_id for parent_id in survivors
        if (
            parent_id in breedable_ids
            and parent_id not in has_live_child
            and parent_id not in retired
        )
    ]
    chosen = template.get("parent_id")
    if (
        template.get("kind") != "improve"
        or isinstance(chosen, bool)
        or not isinstance(chosen, int)
        or chosen not in legal_survivors
    ):
        return _ASHALane(frozenset(), 0)
    keys = frozenset(("improve", (parent_id,)) for parent_id in legal_survivors)
    return _ASHALane(keys, len(keys))


def _protected_due_action(
    fallback: Sequence[Mapping[str, object]],
) -> tuple[str, tuple[int, ...]] | None:
    """Policy cadence that an ordinary unpinned Card may not silently replace."""

    if len(fallback) != 1:
        return None
    action = fallback[0]
    key = _action_key(action)
    if key is None:
        return None
    reason = action.get("_reason")
    bandit_due = isinstance(reason, str) and reason.startswith("bandit:")
    return key if key[0] in {"merge", "ablate"} or bandit_due else None


def _coerce_card_score(value: object) -> CardScore | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return (0.0, (number,)) if math.isfinite(number) else None
    if not isinstance(value, tuple) or len(value) != 2:
        return None
    band, raw_key = value
    if isinstance(band, bool) or not isinstance(band, (int, float)):
        return None
    band_number = float(band)
    if not math.isfinite(band_number):
        return None
    if isinstance(raw_key, (int, float)) and not isinstance(raw_key, bool):
        values: Sequence[object] = (raw_key,)
    elif isinstance(raw_key, (list, tuple)):
        values = raw_key
    else:
        return None
    key: list[float] = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        key.append(number)
    return band_number, tuple(key)


def _score_for_policy(
    policy: object,
    state: RunState,
    card: Card,
    *,
    scoring: CardScoring,
    policy_actions: Sequence[Mapping[str, object]],
) -> CardScore | None:
    hook = getattr(policy, "card_score", None)
    if callable(hook):
        try:
            return _coerce_card_score(hook(state, card, scoring=scoring))
        except Exception:  # optional policy extension: fail closed to legacy next_actions
            return None
    if _builtin_policy_name(policy) is None:
        return None
    return card_score(state, card, scoring=scoring, policy_actions=policy_actions)


def _lane_limit(policy: object, remaining: int) -> int:
    explicit = getattr(policy, "card_select_k", getattr(policy, "card_lane_width", None))
    if explicit is not None:
        return min(remaining, max(1, _bounded_nonnegative_int(explicit, 1)))
    name = _builtin_policy_name(policy)
    if name == "GreedyTree":
        width = 1
    elif name == "EvolutionaryPolicy":
        width = _bounded_nonnegative_int(getattr(policy, "elite", 1), 1) or 1
    elif name == "MCTSPolicy":
        width = _bounded_nonnegative_int(getattr(policy, "n_seeds", 1), 1) or 1
    elif name == "ASHAPolicy":
        # ASHA's width is derived per current rung by `_asha_lane`; never reuse rung-0 width later.
        width = 1
    else:
        width = 1
    return min(remaining, width)


def _diversity_key(card: Card) -> tuple[str | None, tuple[int, ...], tuple[str, ...]]:
    return card.operator, (_card_parent_ids(card) or ()), tuple(sorted(set(card.concept_tags)))


def _default_select(ranked: list[Card], limit: int) -> list[Card]:
    """Take one representative per action/concept niche, then fill remaining slots by score."""

    selected: list[Card] = []
    seen_ids: set[str] = set()
    seen_niches: set[tuple[str | None, tuple[int, ...], tuple[str, ...]]] = set()
    for card in ranked:
        niche = _diversity_key(card)
        if niche in seen_niches:
            continue
        selected.append(card)
        seen_ids.add(card.id)
        seen_niches.add(niche)
        if len(selected) >= limit:
            return selected
    for card in ranked:
        if card.id not in seen_ids:
            selected.append(card)
            seen_ids.add(card.id)
            if len(selected) >= limit:
                break
    return selected


def _select_one_per_action(ranked: Sequence[Card], limit: int) -> list[Card]:
    """ASHA spends at most one promotion slot per survivor in the current rung."""

    selected: list[Card] = []
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for card in ranked:
        key = _action_key(card_action(card) or {})
        if key is None or key in seen:
            continue
        selected.append(card)
        seen.add(key)
        if len(selected) >= limit:
            break
    return selected


def _hook_select(
    policy: object,
    state: RunState,
    ranked: list[Card],
    limit: int,
) -> list[Card] | None:
    hook = getattr(policy, "card_select", None)
    if not callable(hook):
        return None
    try:
        raw = hook(state, list(ranked), max_cards=limit)
    except Exception:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    eligible_by_id = {card.id: card for card in ranked}
    selected: list[Card] = []
    seen: set[str] = set()
    for item in raw:
        card_id = item.id if isinstance(item, Card) else item
        if not isinstance(card_id, str) or card_id in seen or card_id not in eligible_by_id:
            return []
        selected.append(eligible_by_id[card_id])
        seen.add(card_id)
        if len(selected) >= limit:
            break
    return selected


def _selection_after_forced_gates(
    state: RunState,
    policy: object,
    max_nodes: int,
    scoring: CardScoring | Mapping[str, object] | None,
) -> tuple[list[Card], list[Action]]:
    """Return selected Cards plus the already-computed legacy fallback."""

    # SearchPolicy is a pure required seam.  Do not conceal a policy failure as an exploratory draft;
    # only the optional Card hooks fail closed to this legacy result.  The policy view uses the SAME
    # effective node universe as L3's forced budget gate, but is a copy: flag-off policy calls and the
    # caller's folded RunState remain byte-for-byte untouched.
    policy_state = _effective_policy_state(state)
    raw_fallback = policy.next_actions(policy_state)  # type: ignore[attr-defined]
    fallback = [dict(action) for action in raw_fallback if isinstance(action, Mapping)] \
        if isinstance(raw_fallback, Sequence) and not isinstance(raw_fallback, (str, bytes)) else []

    cards = eligible_cards(state, policy)
    if not cards:
        return [], fallback

    asha_lane: _ASHALane | None = None
    if _builtin_policy_name(policy) == "ASHAPolicy":
        asha_lane = _asha_lane(policy_state, fallback)
        cards = [
            card for card in cards
            if _action_key(card_action(card) or {}) in asha_lane.action_keys
        ]
    else:
        due_key = _protected_due_action(fallback)
        # Operator pins are an explicit override band.  Without one, a due merge/ablate/bandit action
        # must either claim its exact Card or execute through the unchanged legacy fallback; unrelated
        # open-band Cards cannot silently erase the policy cadence.
        if due_key is not None and not any(card.pinned for card in cards):
            cards = [
                card for card in cards
                if _action_key(card_action(card) or {}) == due_key
            ]
    if not cards:
        return [], fallback

    remaining = max(0, _bounded_nonnegative_int(max_nodes) - card_budget_used(state))
    if remaining <= 0:
        return [], fallback
    treatment = normalize_card_scoring(scoring)
    scored: list[tuple[Card, CardScore]] = []
    for card in cards:
        score = _score_for_policy(
            policy, state, card, scoring=treatment, policy_actions=fallback,
        )
        if score is not None:
            scored.append((card, score))
    if not scored:
        return [], fallback

    # Stable id ascending is the final tie-break; the second stable sort makes score descending primary.
    scored.sort(key=lambda pair: pair[0].id)
    scored.sort(key=lambda pair: pair[1], reverse=True)
    ranked = [card for card, _ in scored]

    limit = min(
        len(ranked),
        remaining,
        asha_lane.width if asha_lane is not None else _lane_limit(policy, remaining),
    )
    if limit <= 0:
        return [], fallback
    custom = _hook_select(policy, state, ranked, limit)
    if asha_lane is not None:
        selected = _select_one_per_action(
            custom if custom is not None else ranked,
            limit,
        )
    else:
        selected = custom if custom is not None else _default_select(ranked, limit)
    return selected, fallback


def card_selection_set(
    state: RunState,
    policy: object,
    max_nodes: int,
    *,
    scoring: CardScoring | Mapping[str, object] | None = None,
) -> list[Card]:
    """Return the current deterministic Card selection set, empty during any forced phase."""

    if forced_card_actions(state, policy, max_nodes) is not None:
        return []
    selected, _ = _selection_after_forced_gates(state, policy, max_nodes, scoring)
    return selected


def _policy_metadata_for_card_action(
    policy: object,
    action: Mapping[str, object],
    fallback: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Preserve exact policy audit data and stamp every widened ASHA promotion."""

    action_key = _action_key(action)
    exact = next(
        (legacy for legacy in fallback if _action_key(legacy) == action_key),
        None,
    )
    template = exact
    asha_promotion = bool(
        _builtin_policy_name(policy) == "ASHAPolicy"
        and len(fallback) == 1
        and fallback[0].get("_rung") is not None
        and action_key is not None
        and action_key[0] == "improve"
    )
    if asha_promotion:
        template = fallback[0]
    if template is None:
        return {}
    metadata = {
        key: value for key, value in template.items()
        if isinstance(key, str) and key.startswith("_") and key != META_CARD_ID
    }
    if asha_promotion:
        # The survivor receipt/rung/scores are common to the lane, but every promotion chose its own
        # parent.  Stamping that exact parent keeps policy_decision and rung_promoted audit truthful.
        metadata["_chosen"] = action.get("parent_id")
    return metadata


def card_next_actions(
    state: RunState,
    policy: object,
    max_nodes: int,
    *,
    scoring: CardScoring | Mapping[str, object] | None = None,
) -> list[Action]:
    """Card-driven ``next_actions`` with the legacy empty-actions/liveness contract.

    Forced gates always win.  Otherwise selected Card actions carry ``_card_id`` for a later atomic
    claim.  No eligible score, an unsupported policy, or a bad optional hook falls back to the policy's
    already-computed ``next_actions``.  A buggy non-forced empty fallback cannot finish the run early:
    while effective budget remains, a draft keeps the loop live.
    """

    forced = forced_card_actions(state, policy, max_nodes)
    if forced is not None:
        return forced

    selected, fallback = _selection_after_forced_gates(state, policy, max_nodes, scoring)
    actions: list[Action] = []
    for card in selected:
        action = card_action(card)
        if action is None:
            continue
        action.update(_policy_metadata_for_card_action(policy, action, fallback))
        actions.append(action)
    if actions:
        return actions
    remaining = max(0, _bounded_nonnegative_int(max_nodes) - card_budget_used(state))
    if fallback:
        # An optional/custom policy can return a wide creation batch. Never let that fallback overrun
        # the L3 effective budget in one iteration.
        creates = {"draft", "improve", "debug", "merge", "ablate"}
        bounded: list[Action] = []
        claimed = 0
        for action in fallback:
            if action.get("kind") in creates:
                if claimed >= remaining:
                    continue
                claimed += 1
            bounded.append(action)
        if bounded:
            return bounded
    if remaining > 0:
        return [{"kind": "draft"}]
    return []
