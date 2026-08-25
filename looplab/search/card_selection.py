"""Pure Card-selection primitives for the active ``card_driven_selection`` arm (docs/23).

This module deliberately owns no configuration and writes no events. The orchestrator calls
``card_next_actions`` and its claim path re-folds and reserves the selected work item identified by
the private ``_card_id`` before reconstructing the bounded Idea.

The queue boundary is fail closed: only folded ``Card.selection_ready`` work items are considered,
and their operator-specific anchors are checked again against the current state.  A policy extension
is optional.  Built-in policies use the deterministic scorer below; an unknown policy must provide a
``card_score(state, card, *, scoring=...)`` hook or selection falls back to ``next_actions``.
"""
from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
# REVIEW 2026-08-25 (cleanup): `Collection` is imported twice — the `typing` spelling below rebinds
# the `collections.abc` one imported above (they alias the same ABC today, but `typing.Collection`
# is the deprecated alias and the duplicate reads as if two different names were meant). Keep one.
from typing import Collection, Any, Literal

from looplab.core.models import (
    CARD_FRESHNESS_SUPERSEDED_ERROR,
    Card,
    Node,
    NodeStatus,
    RunState,
    _durable_speculative_lifecycle,
    card_score_fence_state,
    effective_card_footprint,
    is_unevaluated_speculative_discard,
    node_counts_toward_card_budget,
    normalize_researcher_footprint,
)
from looplab.search.concept_projection import (
    canonical_recorded_concept, current_concept_projection)
from looplab.search.policy import (META_RUNG, asha_expansion,
                                   rank_by_metric)


META_CARD_ID = "_card_id"
# BACK-COMPAT re-export seam. `CARD_FRESHNESS_SUPERSEDED_ERROR`, `_durable_speculative_lifecycle`,
# `is_unevaluated_speculative_discard` and `node_counts_toward_card_budget` were defined HERE until
# 2026-08-05 and moved DOWN to `core/models.py` so the FOLD could reuse them (`events` may not import
# `search`). They are imported above rather than reimplemented, so `card_selection.<name>` and
# `core.models.<name>` are the SAME object and every existing import site / patch seam keeps working.
# `node_counts_toward_card_budget` followed the other three the same day and for the same reason,
# after moving only the discard predicate left the fold disagreeing with `_effective_policy_state`
# about the OTHER three classes it hides (tombstoned / infeasible / breed_excluded) — see its
# docstring. Everything in this module still calls it by the historical local name.
# (Do not "clean up" the unused-looking `_durable_speculative_lifecycle` import. It has no caller in
# this module any more, and that is exactly why it is here: it was spellable as
# `card_selection._durable_speculative_lifecycle` yesterday, so dropping it would turn a monkeypatch
# on the historical path into a silent no-op instead of an AttributeError. The edge is declared in
# `tests/test_cross_package_private_seams.py` and pinned by `tests/test_card_budget_refund.py`.)

Action = dict[str, Any]
CardScore = tuple[float, tuple[float, ...]]


@dataclass(frozen=True)
class CardResourceEnvelope:
    """Permanent GPU capacity used by the pure Layer-5 freshness predicate.

    This is deliberately the machine envelope, not the momentary free-device set.  A busy GPU makes
    a Card wait; only a declaration that cannot fit on this machine makes speculative work stale.
    An empty ``gpu_memory_mib`` tuple means memory inventory is unknown and therefore degrades to the
    same count-only decision as Layer 4.
    """

    gpu_count: int
    gpu_memory_mib: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.gpu_count, bool) or not isinstance(self.gpu_count, int):
            raise TypeError("gpu_count must be an integer")
        if self.gpu_count < 0:
            raise ValueError("gpu_count must be non-negative")
        if any(
            isinstance(memory, bool) or not isinstance(memory, int) or memory < 0
            for memory in self.gpu_memory_mib
        ):
            raise ValueError("gpu_memory_mib must contain non-negative integers")


def card_fits_resource_envelope(
    card: Card,
    envelope: CardResourceEnvelope | None,
    *,
    node: Node | None = None,
) -> bool:
    """Whether current Card/compiled-node declarations can ever fit the permanent envelope.

    ``None``/UNSPECIFIED footprints retain the historical admission behavior.  When a compiled Node
    is supplied, both its Developer-finalized declaration and the current Card declaration (including
    a later operator pin) must fit.  Partial/unknown memory inventory cannot prove a miss and therefore
    falls back to the count-only check.
    """

    if envelope is None:
        return True

    def _valid_source(raw: object, *, kind: str) -> bool:
        if raw is None:
            return True
        if not isinstance(raw, dict) or not raw:
            return False
        metadata = {
            "card": {"proposed_by": "researcher", "finalized_by": "developer"},
            "pin": {"pinned_by": "operator"},
            "node": {},
        }[kind]
        if not set(raw) <= {"gpus", "gpu_mem_mib", *metadata}:
            return False
        normalized = normalize_researcher_footprint(raw) or {}
        # The tolerant durable normalizer intentionally drops malformed individual fields. Freshness
        # is an execution boundary, so every field that was present must survive instead of letting a
        # valid sibling quantity launder it (for example gpus="many" beside valid memory).
        if any(key in raw and key not in normalized for key in ("gpus", "gpu_mem_mib")):
            return False
        if any(key in raw and raw[key] != value for key, value in metadata.items()):
            return False
        return bool(normalized or any(key in raw for key in metadata))

    # An operator override changes quantities; it cannot launder a malformed immutable declaration
    # (or a malformed replayed pin) into an executable Card.  Validate each source independently
    # before merging them into the effective scheduler request.
    if not _valid_source(card.footprint, kind="card"):
        return False
    if not _valid_source(card.resource_pin, kind="pin"):
        return False
    if node is not None and not _valid_source(node.idea.footprint, kind="node"):
        return False

    def _fits(raw: object) -> bool:
        if raw is None:
            return True
        # ``effective_card_footprint`` can reduce a valid memory-only declaration to an empty
        # request on a zero-GPU host.  ResourceSchedulingMixin treats that as the historical
        # unspecified/CPU path, so freshness must not manufacture a permanent miss here.
        if raw == {}:
            return True
        footprint = normalize_researcher_footprint(raw)
        if footprint is None:
            return False
        declared_gpus = footprint.get("gpus")
        if isinstance(declared_gpus, int):
            if declared_gpus == 0:
                return True
            if envelope.gpu_count == 0:
                # A positive explicit declaration remains an unsatisfied GPU request on a GPU-less
                # host. Only a genuine ``gpus=0`` declaration may select the CPU path.
                return False
            # Layer 4 persists/admits the effective clamped request; an over-declaration is not a
            # permanent miss and must not make Layer 5 drop work the scheduler will legally run.
            required = min(declared_gpus, envelope.gpu_count)
        else:
            # A memory-only declaration keeps the legacy GPU-count behavior.  It needs at most one
            # device when a GPU pool exists, but lack of a pool is not itself a permanent miss for an
            # otherwise-unpinned legacy branch.
            required = 1 if envelope.gpu_count else 0
        requested_memory = footprint.get("gpu_mem_mib")
        if not isinstance(requested_memory, int) or required == 0:
            return True
        memory = envelope.gpu_memory_mib
        if len(memory) != envelope.gpu_count:
            return True
        # `required >= 1` here: the guard above returned True for a 0 requirement.
        effective_memory = min(requested_memory, sorted(memory, reverse=True)[required - 1])
        return sum(available >= effective_memory for available in memory) >= required

    memory = envelope.gpu_memory_mib
    effective_card = effective_card_footprint(
        card.footprint,
        card.resource_pin,
        gpu_count=envelope.gpu_count,
        gpu_memory_mib=memory,
    )
    effective_node = (
        None if node is None else effective_card_footprint(
            node.idea.footprint,
            card.resource_pin,
            gpu_count=envelope.gpu_count,
            gpu_memory_mib=memory,
        )
    )
    return _fits(effective_card) and (node is None or _fits(effective_node))


@dataclass(frozen=True)
class CardScoring:
    """Bounded scorer inputs populated from the current ``Strategy.card_scoring`` treatment.

    The Strategist integration owns strict validation.  This pure boundary still normalizes values
    defensively so malformed resume/state data can only fall back to deterministic defaults.
    """

    stance: Literal["explore", "balanced", "exploit"] = "balanced"
    novelty_weight: float = 0.5
    coverage_weight: float = 0.5
    # How much of the FORESIGHT term the ranker's self-reported confidence carries; the remainder goes
    # to the rank it actually chose. Historically a hardcoded 0.65, which is what `0.65` still restores
    # exactly. It defaults to 0.0 — see `_foresight_signal` for the measurement that moved it.
    confidence_weight: float = 0.0


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
        raw_confidence: object = value.confidence_weight
    elif isinstance(value, Mapping):
        raw_stance = value.get("stance", "balanced")
        raw_novelty = value.get("novelty_weight", 0.5)
        raw_coverage = value.get("coverage_weight", 0.5)
        raw_confidence = value.get("confidence_weight", 0.0)
    else:
        raw_stance, raw_novelty, raw_coverage, raw_confidence = "balanced", 0.5, 0.5, 0.0
    stance = raw_stance if raw_stance in {"explore", "balanced", "exploit"} else "balanced"
    return CardScoring(
        stance=stance,
        novelty_weight=_unit_float(raw_novelty, 0.5),
        coverage_weight=_unit_float(raw_coverage, 0.5),
        confidence_weight=_unit_float(raw_confidence, 0.0),
    )


def card_budget_used(state: RunState) -> int:
    """L3/L5 node-budget denominator, including ordinary superseded attempts."""

    return sum(
        1 for node in state.nodes.values()
        if node_counts_toward_card_budget(state, node)
    )


def refunded_card_budget_node_ids(state: RunState) -> frozenset[int]:
    """Node ids whose physical reservation was refunded because the build never ran.

    ``card_budget_used`` removes these from the L3 denominator; the id ALLOCATOR
    (``Engine._node_id_ceiling``) cannot, because node ids are monotonic and never reused.  So the
    physical ceiling extends itself by exactly this set instead — same predicate, one accounting.
    Bare ``node_building`` reservations that never became a Node are intentionally NOT here: they own
    no Node row, and inventing a second Node-less refund ledger is what this helper exists to avoid.
    """

    return frozenset(
        node_id for node_id, node in state.nodes.items()
        if is_unevaluated_speculative_discard(state, node)
    )


def refunded_node_reservations(state: RunState, limit: int) -> int:
    """Refunded physical reservations, bounded by one whole operator budget.

    The bound is an anti-livelock floor, not accounting: a refunded slot is re-spent on another
    speculative build, which can itself be discarded, so an unbounded refund would let a pathological
    freshness loop mint Developer calls forever with no eval budget to stop it (a discarded build
    charges no eval seconds and no wall clock).  Capping the total refund at the configured ceiling
    keeps the worst case at 2x reservations — finite, deterministic, and identical under replay.
    """

    return min(len(refunded_card_budget_node_ids(state)), max(0, _bounded_nonnegative_int(limit)))


def _effective_policy_state(state: RunState) -> RunState:
    """Give flag-on policy intent the same node universe as the L3 budget.

    Built-in policies historically use ``len(state.nodes)`` for their budget/seed gates and iterate
    the same mapping for merge/ablate cadence.  Passing the unfiltered state after L3 excluded a
    tombstoned or gated node from its own denominator made those two halves disagree.  A shallow
    model copy keeps the original folded state and its Nodes untouched while hiding exactly the
    nodes that do not consume the Card budget from the policy view.

    This IS the Card lane's node universe, so anything that has to predict what the policy will
    propose must hide the same set.  The fold's debug anchor is the one such reader outside this
    package (``events/card_ledger.py::_card_debug_leaf_children``); it reads the predicate directly from
    ``core/models.py`` rather than reimplementing the filter, because a replay-side copy of it is
    what re-opened the debug-Card runaway twice.
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


def _node_id_set(values: Collection[int]) -> frozenset[int]:
    return frozenset(
        value for value in values
        if type(value) is int and value >= 0
    )


def _node_attempt_set(
    values: Collection[tuple[int, int]],
) -> frozenset[tuple[int, int]]:
    """Bound exact live-consumer identities without letting a bare id mask a reset attempt."""

    return frozenset(
        (value[0], value[1]) for value in values
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and type(value[0]) is int
            and value[0] >= 0
            and type(value[1]) is int
            and value[1] >= 0
        )
    )


def _card_id_set(values: Collection[str]) -> frozenset[str]:
    return frozenset(
        value for value in values
        if isinstance(value, str) and value
    )


class _PendingMaskedPolicyState:
    """Read-only policy facade that masks only pending work acknowledged by the L5 session.

    The Nodes remain in ``nodes`` so budget, cadence, child-count, concepts and trust fences observe
    reality.  Only the legacy policy's forced ``pending -> evaluate`` prefix is hidden.  Attribute and
    helper reads delegate to the immutable model copy supplied at construction.
    """

    __slots__ = ("_state", "_ignored")

    def __init__(self, state: RunState, ignored: frozenset[int]) -> None:
        self._state = state
        self._ignored = ignored

    def __getattr__(self, name: str) -> Any:
        return getattr(self._state, name)

    def pending_nodes(self) -> list[Node]:
        return [node for node in self._state.pending_nodes() if node.id not in self._ignored]


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


# `_debug_depth` lived here and read the policy's now-inert `debug_depth`. Gone with the Debug node
# (F5): the Card lane no longer has a debug action to bound, and a live reader of a dead knob is how
# an operator ends up tuning something that does nothing.


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
    engine writer must re-fold and claim that exact Card before reconstructing its bounded
    Idea.  Returning ``None`` for a malformed shape keeps the queue fail closed.
    """

    parents = _card_parent_ids(card)
    if parents is None:
        return None
    if card.operator == "draft" and not parents:
        return {"kind": "draft", META_CARD_ID: card.id}
    if card.operator in {"improve", "expand"} and len(parents) == 1:
        return {"kind": "improve", "parent_id": parents[0], META_CARD_ID: card.id}
    # No `debug` branch (F5). A `debug` Card cannot be authored any more, and a HISTORICAL one must
    # not be turned back into an action by a resumed run — it falls through to `None`, which is the
    # fail-closed answer this function already gives every malformed shape.
    if card.operator == "merge" and len(parents) == 2:
        return {"kind": "merge", "parent_ids": list(parents), META_CARD_ID: card.id}
    return None


def unconsumed_card_inventory(state: RunState, *, exclude: "Collection[str]" = ()) -> int:
    """How many Cards the board is HOLDING and nothing has consumed — prefetch inventory, counted.

    THE OMISSION THIS CLOSES. `engine/speculation.py::_speculation_depth_used` is the ceiling on how
    much unconsumed prefetch a run may hold, and its own contract says it "counts outstanding
    requests plus committed/unevaluated speculative NODES". But the lane it gates
    (`speculative_raw_actions` -> `_stage_prepared_card`) deliberately produces a CARD and no Node —
    its call site says so: "Author their concrete Ideas and durable Cards now, but deliberately
    leave every Node slot unowned". A Card is not a pending Node until something builds it, so the
    thing the gate buys was invisible to the number that limits the buying.

    Measured live on `runs/e5small-dr-unified-v4`: `_speculation_depth_used()` returned **1** against
    a pinned ceiling of 2 while **88** cards sat unbuilt — `1 < 2` every turn, forever. Nothing could
    consume them either: while any node is pending, `card_next_actions` returns `forced_card_actions`
    (an `evaluate` for the running node) and never reaches card SELECTION, which the engine documents
    at `_occupancy_paced_creates`.

    `exclude` is what the caller has already counted — outstanding requests and the cards owned by
    pending Nodes — so the two halves of the ceiling cannot double-count the same work.

    DELIBERATELY COARSER THAN `_strictly_selection_ready`, and the direction is the argument. That
    predicate answers "may this Card be CLAIMED right now" and re-checks receipt identity, action
    provenance and generation freshness — the right bar for a claim, the wrong one for a COUNT: a
    Card failing a freshness fence this turn is still inventory the board holds. Over-counting only
    makes the run prefetch LESS, which is the safe direction; under-counting is the bug being fixed.
    The four fields read here are the board's own terminal vocabulary, the same ones
    `open_research_cards` keys on.
    """
    skip = set(exclude or ())
    total = 0
    for card in state.cards.values():
        if card.id in skip:
            continue                       # already counted by the caller's other half
        if getattr(card, "status", None) != "proposed":
            continue                       # built, running, dropped: no longer waiting
        if getattr(card, "verdict", None) != "open":
            continue
        if getattr(card, "dropped_reason", None) is not None:
            continue
        if getattr(card, "merged_into", None) is not None:
            continue
        total += 1
    return total


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


def _card_generation_fences_current(state: RunState, card: Card) -> bool:
    """Recheck the receipt's exact parent/score lifecycle fences on the fresh state copy.

    This is the Layer-5 twin of the fold's ``_card_action_freshness``: the fold decides
    ``selection_ready`` on the state it folded, and the producer/freshness gate re-decides it on the
    fresh copy it is about to elect from.  The score half is the SAME rule, spelled once in
    ``card_score_fence_state`` — narrowing only the fold would leave every prefetch election still
    refusing a card whose champion had moved, and the freshness drain still terminalizing it.
    """

    parents = _card_parent_ids(card)
    if parents is None or card.parent_generations is None:
        return False
    if set(card.parent_generations) != {str(parent_id) for parent_id in parents}:
        return False
    for parent_id in parents:
        parent = state.nodes.get(parent_id)
        if (
            parent is None
            or parent.tombstoned
            or parent_id in state.aborted_nodes
            or card.parent_generations.get(str(parent_id)) != parent.attempt
        ):
            return False

    scored = None if card.scored_against is None else state.nodes.get(card.scored_against)
    return card_score_fence_state(
        card.scored_against,
        card.scored_against_generation,
        card.scored_against_empty,
        anchor_live=(
            scored is not None
            and not scored.tombstoned
            and card.scored_against not in state.aborted_nodes
        ),
        anchor_attempt=None if scored is None else scored.attempt,
        board_empty=state.best_node_id is None,
    ) == "current"


def _live_card_action(
    card: Card,
    *,
    breedable_ids: set[int],
    top_two_ids: set[int],
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
    # `debug` falls through to False (F5). It used to be live only while it matched the policy's
    # forced repair gate; that gate is gone, so a historical `debug` Card is never executable again —
    # which is the same answer the removed recheck would now give, reached by the default branch
    # rather than by a special case nobody can trigger.
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
    return [
        card for _, card in sorted(state.cards.items())
        if _strictly_selection_ready(card)
        and _live_card_action(
            card,
            breedable_ids=breedable_ids,
            top_two_ids=top_two_ids,
        )
    ]


# `_matching_ready_debug_card` bound a bare forced debug action to an already-ready `debug` Card so
# the lane did not author a second one for the same work. Both halves are gone with F5 — there is no
# forced debug action to bind and no `debug` Card to bind it to.


def _forced_card_actions(
    state: RunState,
    policy: object,
    max_nodes: int,
    *,
    ignored_pending_node_ids: frozenset[int] = frozenset(),
) -> list[Action] | None:
    """Apply the exact policy forced-prefix.

    ``None`` means discretionary Card scoring may run.  An empty list is therefore unambiguously the
    forced budget terminal, never "no Card happened to score".
    """

    pending = [
        node for node in state.pending_nodes()
        if node.id not in ignored_pending_node_ids
    ]
    if pending:
        return [{"kind": "evaluate", "node_id": node.id} for node in pending]

    used = card_budget_used(state)
    limit = _bounded_nonnegative_int(max_nodes)

    # THE FORCED DEBUG PREFIX IS GONE (F5). It sat here, ahead of the terminal gate: a failed leaf
    # pre-empted every other action so the lane would open a fresh node to have another go at the
    # experiment that had just failed. That node is exactly what the operator deleted — the failure is
    # fixed inside the one node now, bounded by the judgment in `engine/repair_judgment.py` rather
    # than by the count that used to hand control to this prefix.

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
        # A staged draft Card is already the concrete seed proposal.  Consume those durable receipts
        # before asking the Researcher for another raw draft, otherwise Layer 5 can mint an inventory
        # that the exact forced seed prefix permanently steps around (and serial Card mode needlessly
        # re-proposes the same slot after a crash-prefix ``card_added``).  Return only one authority
        # kind per turn: a partial ready lane is consumed first, then a later fresh fold asks for the
        # remaining raw width.  This preserves the existing all-Card/all-raw atomic-claim boundary.
        ready_drafts: list[Action] = []
        for card in eligible_cards(state, policy):
            if card.operator != "draft":
                continue
            action = card_action(card)
            if action is not None:
                ready_drafts.append(action)
            if len(ready_drafts) >= width:
                break
        if ready_drafts:
            return ready_drafts
        return [{"kind": "draft"} for _ in range(width)]
    return None


def forced_card_actions(
    state: RunState,
    policy: object,
    max_nodes: int,
) -> list[Action] | None:
    """Public Layer-3 forced prefix; pending work is never masked on the serial path."""

    return _forced_card_actions(state, policy, max_nodes)


def _coverage_inputs(state: RunState) -> tuple[set[str], dict[str, str | None]]:
    """Return one shared canonical identity view for explored and candidate Card concepts."""

    projection = current_concept_projection(state)
    explored = {
        concept
        for node_id, concepts in projection.trusted_memberships.items()
        if (node := state.nodes.get(node_id)) is not None
        and node_counts_toward_card_budget(state, node)
        for concept in concepts
    }
    return explored, projection.rename


# §21.4 grades are a CLASSIFICATION, not a monotone novelty ORDER: level 0 is `novel` and level 5 is
# `wrongly_abandoned` (a direction this run already FAILED), so scoring `level / 5` ranked the grades
# exactly backwards. Credit each grade by how much of the space it leaves unexplored instead
# (search/graded_novelty.py::_LEVELS is the taxonomy).
_NOVELTY_LEVEL_CREDIT = {
    0: 1.0,      # novel — no overlap with any tried direction
    1: 0.0,      # identical params to a tried node
    2: 0.1,      # near-duplicate in this run (same concept set, trivially-close params)
    3: 0.6,      # tried in a PRIOR run only — still new HERE, with an outcome to beat
    4: 0.4,      # same direction as a tried node, materially different implementation
    5: 0.3,      # re-opens a direction this run already failed
}
# A card the novelty machinery never spoke about carries NO evidence either way: `graded_novelty` may
# be off, or the grade was 0-3 and DEFERRED to the flat gate, which records nothing (engine/novelty.py
# emits `novelty_graded` for levels 4/5 ONLY). Absence is not proof of non-novelty, so it sits at the
# neutral midpoint rather than the floor — pinning it at 0.0 let the only two grades that ARE recorded,
# both LESS novel than an unremarked proposal, outrank every genuinely new region under stance="explore".
_UNGRADED_NOVELTY = 0.5


def _novelty_signal(card: Card) -> float:
    verdict = card.novelty_verdict
    if not isinstance(verdict, Mapping):
        return _UNGRADED_NOVELTY
    recommendation = verdict.get("recommendation")
    if isinstance(recommendation, str) and recommendation.lower() in {
        "block", "drop", "reject", "supersede",
    }:
        return 0.0
    level = verdict.get("level")
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        # a verdict that IS present but carries no usable grade is a `novelty_rejected` audit row —
        # the flat gate found a near-duplicate. That is real evidence of non-novelty, so it keeps the
        # floor; only a wholly ABSENT verdict is uninformative.
        return 0.0
    number = float(level)
    if not math.isfinite(number) or number != int(number):
        return _UNGRADED_NOVELTY
    # an unrecognized (future) grade is unreadable evidence, not a novelty claim — stay neutral.
    return _NOVELTY_LEVEL_CREDIT.get(int(number), _UNGRADED_NOVELTY)


# The ceiling an UNVERIFIED coverage claim may reach — the same neutral midpoint `_UNGRADED_NOVELTY`
# uses, and for the same stated reason: unverified is unknown, not disproved.
_UNVERIFIED_COVERAGE_CEILING = 0.5
# The concept provenances produced by something OTHER than the Card's own proposer. `offline-heuristic`
# is deliberately absent: it is a coarse display-only fallback the classifier is expected to upgrade,
# and `_on_node_created` already excludes it from the cadence's known-tag cache for that reason.
# `researcher-authored` is the gameable case, and `untrusted-source` is the malformed one.
_INDEPENDENT_CONCEPT_PROVENANCE = frozenset({"classifier", "operator-edited"})


def _independently_classified(card: Card) -> bool:
    """True when ``card.concept_tags`` carries a COMPLETE receipt from a non-proposer source.

    Every clause is load-bearing, and `CardConceptSource` exists to make them checkable:
    ``receipt_valid`` rejects a malformed/forged envelope, ``membership_present`` distinguishes "no
    membership was recorded" from "an empty one was", ``complete`` rejects a lossy or partial
    materialization (a delta whose ancestry did not resolve), and ``provenance`` is what says the
    tags came from somewhere other than the proposer competing for this selection.

    The clauses look redundant against a VALIDATED ``CardConceptSource`` — its ``_coherent_owner``
    validator already enforces ``complete => membership_present and receipt_valid``, so the
    under-complete combinations are unconstructible through the model. They are not redundant on the
    path this gate has to survive: ``card_score`` is a documented public scoring hook, so an external
    policy can hand it a source object no validator ever saw, and checking ``complete`` alone would
    then trust a hand-built receipt that says complete while admitting it is invalid. Read through
    ``getattr`` for the same reason, so an absent or foreign receipt fails CLOSED rather than raising.
    """
    source = getattr(card, "concept_source", None)
    if source is None:
        return False
    return bool(getattr(source, "receipt_valid", False)
                and getattr(source, "membership_present", False)
                and getattr(source, "complete", False)
                and getattr(source, "provenance", None) in _INDEPENDENT_CONCEPT_PROVENANCE)


def _coverage_signal(
    card: Card,
    explored: set[str],
    rename: dict[str, str | None],
) -> float:
    # Card proposal tags preserve their bounded raw spelling for audit. Compare them only after the
    # SAME normalization/consolidation projection that produced ``trusted_memberships``; otherwise
    # ``Loss X``/``loss-x`` or a retired alias receives a false uncovered bonus.
    concepts = sorted({
        concept
        for tag in card.concept_tags
        if (concept_and_problem := canonical_recorded_concept(tag, rename))[1] is None
        if (concept := concept_and_problem[0]) is not None
    })
    if not concepts:
        return 0.0
    fraction = sum(tag not in explored for tag in concepts) / len(concepts)
    if _independently_classified(card):
        return fraction
    # doc 25 SE-11. Until this cap, a Card's coverage bonus was computed from `concept_tags` ALONE —
    # which, before the node is built, is the taxonomy the proposing Researcher wrote about its own
    # proposal, while competing for selection. Minting a plausible new slug put every tag outside
    # `explored` and returned 1.0: the MAXIMAL exploration bonus, earned by naming, before any
    # independent classification exists. That is a gameable input to a default-on selection path
    # (`Settings.card_driven_selection` is True).
    #
    # The cap is a CEILING, not a replacement, and the asymmetry is the point: the exploit only runs
    # UPWARD. A card that honestly reports tags already explored still scores its true low fraction —
    # nobody games themselves down — while an unverified claim of new ground can earn at most the
    # neutral midpoint. Replacing the value with 0.5 outright would have INFLATED exactly the honest
    # case this is meant to protect.
    #
    # The midpoint (rather than 0.0) follows `_UNGRADED_NOVELTY` above, for the same reason recorded
    # there: an unverified claim is UNKNOWN, not disproved, and flooring it would let the signals that
    # ARE recorded outrank every genuinely new region. An independent receipt — a classifier pass or
    # an operator edit — lifts the cap and restores the full range.
    return min(fraction, _UNVERIFIED_COVERAGE_CEILING)


def _priority_signal(card: Card) -> float:
    priority = card.priority
    if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
        return 0.0
    return 1.0 / (priority + 1.0)


def _foresight_signal(confidence: float, priority: float, confidence_weight: float) -> float:
    """Blend the ranker's RANK with its self-reported confidence in that rank (doc 25 SE-11).

    These are two different things and only one of them is evidence. `priority` is the order the
    foresight ranker actually CHOSE; `confidence` is the same model's self-assessment of that choice,
    and this repository's own §21.12 measurement records it as Pearson≈0 with realized outcome —
    `search/foresight.py::_verifier_confidence` exists specifically to REPLACE it with a calibrated
    §12-verifier score, but only on the idea path, never on the board ranking that stamps
    `Card.confidence` (`engine/audit.py` copies `last_hyp_priority["confidence"]` straight through).

    It nevertheless carried a hardcoded 65% of this term, on a selection path that is ON by default
    (`Settings.card_driven_selection`). A number measured to be uncorrelated is not a defensible
    default majority of an active selection signal, so the weight now defaults to 0.0 and the rank
    stands alone. Confidence is NOT discarded — it remains a tie-break component of the score key,
    which is what the finding's own alternative asked for.

    Setting `confidence_weight` to 0.65 restores the historical blend exactly, which is why the shape
    is a weight rather than a deletion: the old behaviour has to stay expressible for anyone who wants
    to A/B it. It is reachable through the public `card_score(..., scoring=...)` hook ONLY —
    `Engine._card_scoring` is assigned solely from `strategist.py::validate_card_scoring`, whose
    exact-set match rejects a map naming this field. That closes the operator's `set_strategy` path
    along with the Strategist's, because one validator serves both; re-opening it for operators means
    a second validated path, not a wider `_CARD_SCORING_FIELDS`.
    """
    return confidence_weight * confidence + (1.0 - confidence_weight) * priority


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
        # A merge is symmetric in its parents (merging {a,b} == merging {b,a}), and `_live_card_action`
        # already accepts a merge Card order-INDEPENDENTLY (`set(parents) == top_two`). Key on the SORTED
        # parents so this key agrees: without it, a merge Card whose parent_ids are stored in the reverse
        # of the policy fallback's rank order missed `exact_match` (band 2.0 -> 1.0) and, on a due-merge
        # tick, the `due_key ==` filter dropped it entirely — orphaning the ready Card while the engine
        # ran a redundant legacy merge. Both sides of every `_action_key` comparison canonicalize here.
        return kind, tuple(sorted(parents))
    return None


def card_score(
    state: RunState,
    card: Card,
    *,
    scoring: CardScoring | Mapping[str, object] | None = None,
    policy_actions: Sequence[Mapping[str, object]] = (),
    coverage_inputs: tuple[set[str], dict[str, str | None]] | None = None,
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
    # `_coverage_inputs` rebuilds the COMPLETE concept projection over every Node, and card_score runs
    # once per candidate — so recomputing it here made one election O(cards x nodes*concepts) (doc 25
    # SE-04). The selection loop now computes it ONCE per snapshot and threads it in. The argument is
    # optional because `card_score` is also the documented public scoring hook: an external policy
    # calling it with two arguments still gets a correct, if slower, score.
    explored, rename = coverage_inputs if coverage_inputs is not None else _coverage_inputs(state)
    coverage = _coverage_signal(card, explored, rename)
    total_weight = treatment.novelty_weight + treatment.coverage_weight
    exploration = (
        (treatment.novelty_weight * novelty + treatment.coverage_weight * coverage) / total_weight
        if total_weight > 0.0 else 0.0
    )
    confidence = _unit_float(card.confidence, 0.0)
    priority = _priority_signal(card)
    foresight = _foresight_signal(confidence, priority, treatment.confidence_weight)
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

    # The policy is the authority on ASHA expansion/retirement; this lane must not re-derive it.
    has_live_child, retired = asha_expansion(state)
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
    coverage_inputs: tuple[set[str], dict[str, str | None]] | None = None,
) -> CardScore | None:
    hook = getattr(policy, "card_score", None)
    if callable(hook):
        try:
            return _coerce_card_score(hook(state, card, scoring=scoring))
        except Exception:  # optional policy extension: fail closed to legacy next_actions
            return None
    if _builtin_policy_name(policy) is None:
        return None
    # The external hook keeps its published two-argument shape; only the built-in scorer, which we
    # own, receives the pre-computed snapshot.
    return card_score(state, card, scoring=scoring, policy_actions=policy_actions,
                      coverage_inputs=coverage_inputs)


def card_lane_width(policy: object) -> int:
    """How many Cards ONE selection turn may retain, before the remaining budget narrows it.

    Split out of ``_lane_limit`` (which is still the only caller that applies the budget) because a
    SECOND question needs the same number and cannot ask it through a budget: the Layer-5 producer
    election has to know how many committed-but-unconsumed prefetches the freshness gate is willing
    to keep at once.  That ceiling is this width — ``speculative_card_is_fresh`` proves membership in
    a set this wide, and `_counterfactual_owned_selection_state`'s docstring says so from the other
    side ("that can retain more than ``card_select_k`` Cards", which is the thing it exists to stop).
    Electing past it buys a Developer call whose result the gate is then obliged to discard.
    """

    explicit = getattr(policy, "card_select_k", getattr(policy, "card_lane_width", None))
    if explicit is not None:
        return max(1, _bounded_nonnegative_int(explicit, 1))
    name = _builtin_policy_name(policy)
    if name == "GreedyTree":
        return 1
    if name == "EvolutionaryPolicy":
        return _bounded_nonnegative_int(getattr(policy, "elite", 1), 1) or 1
    if name == "MCTSPolicy":
        return _bounded_nonnegative_int(getattr(policy, "n_seeds", 1), 1) or 1
    if name == "ASHAPolicy":
        # ASHA's width is derived per current rung by `_asha_lane`; never reuse rung-0 width later.
        return 1
    return 1


def _lane_limit(policy: object, remaining: int) -> int:
    return min(remaining, card_lane_width(policy))


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
    *,
    policy_state: Any | None = None,
    candidate_cards: Sequence[Card] | None = None,
) -> tuple[list[Card], list[Action]]:
    """Return selected Cards plus the already-computed legacy fallback."""

    # SearchPolicy is a pure required seam.  Do not conceal a policy failure as an exploratory draft;
    # only the optional Card hooks fail closed to this legacy result.  The policy view uses the SAME
    # effective node universe as L3's forced budget gate, but is a copy: flag-off policy calls and the
    # caller's folded RunState remain byte-for-byte untouched.
    policy_state = _effective_policy_state(state) if policy_state is None else policy_state
    raw_fallback = policy.next_actions(policy_state)  # type: ignore[attr-defined]
    fallback = [dict(action) for action in raw_fallback if isinstance(action, Mapping)] \
        if isinstance(raw_fallback, Sequence) and not isinstance(raw_fallback, (str, bytes)) else []

    cards = list(candidate_cards) if candidate_cards is not None else eligible_cards(state, policy)
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
    # ONE projection per election, not one per candidate. It is an immutable view of `state`, which
    # does not change while the lane is scored, so every candidate must see exactly the same one —
    # recomputing it per card was not only slow, it made the ranking depend on work that is identical
    # by construction.
    coverage = _coverage_inputs(state)
    scored: list[tuple[Card, CardScore]] = []
    for card in cards:
        score = _score_for_policy(
            policy, state, card, scoring=treatment, policy_actions=fallback,
            coverage_inputs=coverage,
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


def _counterfactual_owned_card_state(
    state: RunState,
    card_id: str,
    node_id: int,
) -> RunState | None:
    """Remove exactly one Card's in-flight ownership without relaxing any other blocker.

    The copied state represents the instant immediately before this Card was claimed.  A committed
    pending Node is removed because its budget, child edge and concept coverage are effects of the
    subject itself; every sibling speculative Node remains.  A build reservation has no Node yet, so
    only its exact marker is removed.  Any ambiguous/multi-owner shape fails closed.
    """

    if not isinstance(card_id, str) or not card_id or type(node_id) is not int or node_id < 0:
        return None
    card = state.cards.get(card_id)
    if card is None:
        return None
    provenance = card.selection_provenance
    blockers = set(card.selection_blockers)
    if (
        provenance.owner_state != "in_flight"
        or blockers != {"work_in_flight"}
        or card.dropped_reason is not None
        or card.merged_into is not None
    ):
        return None

    node = state.nodes.get(node_id)
    marker = state.buildings.get(node_id)
    if node is not None:
        if (
            node.status is not NodeStatus.pending
            or node.attempt != 0
            or node.tombstoned
            or node.id in state.aborted_nodes
            or node.idea.card_id != card_id
            or card.evidence != [node_id]
            or card.status not in {"coded", "running"}
        ):
            return None
    else:
        if (
            not isinstance(marker, Mapping)
            or marker.get("card_id") != card_id
            or card.evidence
            or card.status != "building"
        ):
            return None

    restored = card.model_copy(deep=True, update={
        "status": "proposed",
        "verdict": "open",
        "actionable": True,
        "evidence": [],
        "best_delta": None,
        "selection_provenance": provenance.model_copy(update={"owner_state": "none"}),
        "selection_blockers": [],
        "selection_ready": True,
    })
    if not _strictly_selection_ready(restored):
        return None

    cards = dict(state.cards)
    cards[card_id] = restored
    nodes = dict(state.nodes)
    nodes.pop(node_id, None)
    buildings = dict(state.buildings)
    buildings.pop(node_id, None)
    building = state.building
    if isinstance(building, Mapping) and building.get("node_id") == node_id:
        building = None
    return state.model_copy(update={
        "cards": cards,
        "nodes": nodes,
        "buildings": buildings,
        "building": building,
    })


def merged_alias_ids(state: RunState) -> frozenset[str]:
    """Every Card id that was folded INTO a canonical Card (doc 25 SE-06).

    The fold collapses a merged Card OUT of ``state.cards`` and records only its id in the
    canonical's ``.aliases`` — ``Card.merged_into`` is never actually assigned by the fold. So a
    merged id is proven merged by ALIAS MEMBERSHIP, not by a present `merged_into` row, and this set
    is the only way to tell "merged away" from "missing".

    That distinction is fail-closed-critical for the callers. An absent sibling id that IS here was
    legitimately merged and may be skipped; an absent id that is NOT here is a corrupt or partial
    ownership chain and must make the whole counterfactual fail closed rather than be silently passed
    over. Two sites derived this set independently, each with its own copy of that explanation.
    """
    return frozenset(
        alias for card in state.cards.values()
        for alias in (getattr(card, "aliases", None) or [])
        if isinstance(alias, str) and alias
    )


def _card_administratively_dead(card) -> bool:
    """Whether a PRESENT Card is closed to further work: operator/engine/freshness/novelty dropped
    (``status == "dropped"``). Deliberately keys on the FOLDED status, not ``dropped_reason`` — a
    reason-less card_dropped folds to status="dropped" with dropped_reason=None, so a reason-keyed
    check would let it slip through. The ``merged_into`` disjunct is DEFENSIVE only: the fold collapses
    a merged Card OUT of ``state.cards`` (absent, tracked via its canonical's ``aliases``) and never
    assigns ``merged_into``, so a merged Card never reaches this helper as a present row — callers
    exclude a merged id by alias membership, not here."""
    return card.status == "dropped" or card.merged_into is not None


def _counterfactual_owned_selection_state(
    state: RunState,
    card_id: str,
    node_id: int,
    *,
    consumed_inflight: Collection[tuple[int, int]] = (),
) -> tuple[RunState, frozenset[str], frozenset[int]] | None:
    """Reopen the subject and every exact sibling in the same speculative population.

    Freshness is SET membership, so reopening only the subject would give each owned Card a different
    counterfactual lane.  That can retain more than ``card_select_k`` Cards: every subject sees itself
    beside the newly-ready leaders while its stronger speculative siblings stay excluded.  Start with
    the explicitly proven subject, then reopen each other pending Node carrying an exact durable
    speculative marker.  Exact lifecycles already admitted by the consumer remain masked/excluded:
    they are the work being consumed, not candidates for the next-prefetch lane. Any marker whose
    ownership shape cannot be proven fails closed.
    """

    consumed = _node_attempt_set(consumed_inflight)
    subject = state.nodes.get(node_id)
    if subject is not None and (node_id, subject.attempt) in consumed:
        # A GPU-admitted lifecycle burns to terminal and is never reconsidered by freshness.
        return None
    requested = (card_id, node_id)
    pairs = [requested]
    merged_aliases = merged_alias_ids(state)
    for sibling_id, sibling in sorted(state.nodes.items()):
        if (
            sibling_id == node_id
            or sibling.status is not NodeStatus.pending
            or sibling.tombstoned
            or sibling_id in state.aborted_nodes
            # An admitted sibling is already being consumed, not part of the next-prefetch population.
            # Keep it masked/excluded in the counterfactual instead of reopening it to compete with the
            # subject.  The exact attempt prevents a reset lifecycle from inheriting an old admission.
            or (sibling_id, sibling.attempt) in consumed
        ):
            continue
        sibling_card_id = sibling.idea.card_id
        generation = sibling.card_build_generation
        link = state.speculative_nodes.get(sibling_id)
        if not (
            sibling.speculative is True
            and sibling.attempt == 0
            and isinstance(sibling_card_id, str)
            and sibling_card_id
            and type(generation) is int
            and isinstance(link, Mapping)
            and link.get("card_id") == sibling_card_id
            and type(link.get("generation")) is int
            and link.get("generation") == generation
        ):
            continue
        # An administratively-dead sibling (operator-dropped or merged, so its Card is absent/closed)
        # will itself be terminalized by the freshness gate on its own turn. Including it here would make
        # `_counterfactual_owned_card_state` return None and fail the WHOLE counterfactual closed —
        # spuriously superseding a HEALTHY sibling in the same speculative population (the L6-drop /
        # card_merged x L5a-speculation interaction). Skip only PROVEN-dead siblings so the fail-closed
        # contract still bites on a corrupt chain:
        #  - present + status=="dropped": the canonical folded closed state (match orchestrator's
        #    projection check), NOT `dropped_reason` — a reason-less card_dropped folds to
        #    status="dropped" with dropped_reason=None (`dropped_reason = reason or None`), so a
        #    reason-keyed check would let it slip through and reintroduce the bug;
        #  - present + merged_into set, OR absent but its id is in `merged_aliases` (folded INTO a
        #    canonical): a proven merge receipt.
        # An ABSENT id that is NOT a known merge alias is a corrupt/partial ownership chain — do NOT skip
        # it; let it enter `pairs` so the counterfactual fails closed rather than proceeding on an
        # unproven common population.
        sibling_card = state.cards.get(sibling_card_id)
        if sibling_card is not None:
            if _card_administratively_dead(sibling_card):
                continue
            # An alive-but-STALE sibling (its own parent aborted/tombstoned/reset, so the fold gives
            # its card blockers {"freshness_stale", "work_in_flight"}) would restore cleanly EXCEPT for
            # its own staleness. Including it makes `_counterfactual_owned_card_state` reject any blocker
            # set beyond {"work_in_flight"} and null the WHOLE counterfactual, so `speculative_card_is_fresh`
            # reports a HEALTHY subject as not-fresh and `_drop_stale_speculation` terminalizes it — at
            # depth>=2 one stale member spuriously supersedes every fresh member (the lane collapse
            # docs/23 §12.5 says this gate must avoid). It is terminalized by the freshness gate on its
            # OWN turn, exactly like the administratively-dead case, so skip it here. The guard mirrors
            # that helper's node-branch acceptance so ONLY a stale-sole failure is skipped; any other
            # unproven shape still enters `pairs` and fails the counterfactual closed.
            if (
                sibling_card.dropped_reason is None
                and sibling_card.merged_into is None
                and sibling_card.selection_provenance.owner_state == "in_flight"
                and sibling_card.evidence == [sibling_id]
                and sibling_card.status in {"coded", "running"}
                and set(sibling_card.selection_blockers) == {"freshness_stale", "work_in_flight"}
            ):
                continue
        elif sibling_card_id in merged_aliases:
            continue
        pairs.append((sibling_card_id, sibling_id))

    selection_state = state
    reopened_card_ids: set[str] = set()
    reopened_node_ids: set[int] = set()
    for owned_card_id, owned_node_id in pairs:
        if owned_card_id in reopened_card_ids or owned_node_id in reopened_node_ids:
            return None
        counterfactual = _counterfactual_owned_card_state(
            selection_state, owned_card_id, owned_node_id,
        )
        if counterfactual is None:
            return None
        selection_state = counterfactual
        reopened_card_ids.add(owned_card_id)
        reopened_node_ids.add(owned_node_id)
    return selection_state, frozenset(reopened_card_ids), frozenset(reopened_node_ids)


def _speculative_policy_state(
    state: RunState,
    ignored_pending_node_ids: frozenset[int],
    policy: object,
) -> Any:
    """Hide the speculative pending prefix while preserving policy cadence.

    ASHA is the one exception to retaining session-owned pending Nodes in the policy view: freezing its
    view at the pre-promotion bracket lets the producer fill remaining survivors from the SAME lane,
    but makes the set empty instead of crossing into a future rung whose metrics do not exist yet.
    The real selection/budget state still retains those Nodes.
    """

    effective = _effective_policy_state(state)
    ignored = frozenset(
        node_id for node_id in ignored_pending_node_ids
        if (
            (node := effective.nodes.get(node_id)) is not None
            and node.status is NodeStatus.pending
            and not node.tombstoned
        )
    )
    if not ignored:
        return effective
    if _builtin_policy_name(policy) == "ASHAPolicy":
        return effective.model_copy(update={
            "nodes": {
                node_id: node for node_id, node in effective.nodes.items()
                if node_id not in ignored
            },
        })
    return _PendingMaskedPolicyState(effective, ignored)


def _reserved_speculative_slots(state: RunState, excluded_card_ids: frozenset[str]) -> int:
    """Count request/build reservations not represented by a Node budget row yet."""

    reserved = 0
    merged_aliases = merged_alias_ids(state)
    # `excluded_card_ids` also carries durable producer-failed ids (append-only). A live Card with no Node
    # budget row yet IS a genuine outstanding request/build reservation, so it counts. But an
    # administratively-DEAD Card owns no live request/build: operator-dropped / status=="dropped" (a
    # PRESENT row, caught by `_card_administratively_dead`), OR merged away (ABSENT, its id in
    # `merged_aliases`). A producer-failed id later dropped/merged would otherwise be counted as an
    # outstanding reservation FOREVER and monotonically starve speculative capacity. Exclude both shapes.
    for card_id in excluded_card_ids:
        card = state.cards.get(card_id)
        if card is not None and _card_administratively_dead(card):
            continue
        if card is None and card_id in merged_aliases:
            continue                                # merged into a canonical — folded, not outstanding
        if card is None or not card.evidence:
            reserved += 1
    return reserved


@dataclass(frozen=True)
class SpeculativeSelectionContext:
    """The SESSION-owned half of a speculative selection query (doc 25 SE-14).

    Five entry points re-declared and forwarded the same bundle, so adding one field meant editing
    five signatures — and one already HAD been added asymmetrically (`consumed_inflight`), which is
    the failure mode: a difference that is meaningful lives as an absence in a parameter list nobody
    reads side by side.

    What belongs here is what one producer/consumer session holds constant across every query it
    makes. The per-call SUBJECT — which owned Card/Node a query is about — deliberately stays an
    ordinary argument, because it changes on every call and burying it here would hide the one thing
    that distinguishes the entry points from each other.

    `consumed_inflight` defaults to empty, and the two ELECTION entry points leave it that way ON
    PURPOSE: election runs BEFORE the consumer admits an attempt, the freshness gate runs after. That
    is now one visible unset field at a caller instead of a missing keyword in two of five signatures.
    """

    scoring: "CardScoring | Mapping[str, object] | None" = None
    excluded_card_ids: Collection[str] = ()
    ignored_pending_node_ids: Collection[int] = ()
    resource_envelope: "CardResourceEnvelope | None" = None
    consumed_inflight: Collection[tuple[int, int]] = ()


# The "no session state" query: every field at its default. A module-level instance rather than a
# `None` sentinel so the entry points never have to spell the empty case twice.
NO_SPECULATIVE_CONTEXT = SpeculativeSelectionContext()


def _asha_mask_is_unsound(
    state: RunState,
    policy: object,
    ignored_pending: frozenset[int],
    reopened_node_ids: frozenset[int],
) -> bool:
    """Whether hiding `ignored_pending` from an ASHA POLICY VIEW would make that view answer for a
    bracket it cannot know yet.

    THIS IS A PROPERTY OF THE MASKED VIEW, NOT OF THE QUERY, and stating it that way is the whole of
    backlog F1g's remaining defect. Both conditions below were `return [], []` at the TOP of
    `_speculative_selection` until 2026-08-20, so an ASHA-family run answered NOTHING to any
    speculative query while one of its own in-flight Nodes was masked — including the occupancy-paced
    production query, whose entire purpose is to produce while an in-flight Node holds a slot. The
    two facts that make that a defect rather than a conservative choice:

      * MEASURED. `runs/e5small-dr-unified-v4` (the Strategist moved it to `bohb`, which is
        `ASHAPolicy`) held GPU 0 dark for 5 h 19 m — 09:45:42 to 15:04:57 on 2026-08-20 — with node
        1 evaluating on GPU 1, node 0 terminal, node 2 killed by the freshness gate and NOTHING
        selectable. `occupancy_due(inflight=1, queued=0, width=2)` read True for the whole of it and
        `_occupancy_paced_creates` returned `[]` on all 130 of the loop's turns in that window. The
        run then built node 3 in the same MINUTE as node 1's terminal. Replayed over the corpus:
        8.03 starved hours in the five runs that had this pace at all, of which 5.94 are this
        predicate firing, 2.08 the promotion clause below, and **0.00 in every GreedyTree and
        EvolutionaryPolicy run** — the lanes always answered there.
      * THE HAZARD IS REAL BUT NARROWER THAN THE GUARD WAS. What it protects against is the POLICY
        being asked to choose over a board it cannot see: `_speculative_policy_state` literally
        deletes the masked Nodes out of `nodes` for ASHA, so an unresolved rung-0 root looks like a
        rung-0 vacancy and the policy answers with a replacement draft. That view is built at ONE
        place, the discretionary lane. Every lane above it answers from the UNMASKED board — and the
        forced SEED prefix in particular computes its ASHA census as
        `sum(1 for node in state.nodes.values() if not node.parent_ids and ...)`, over the whole
        board, so the masked root is STILL COUNTED and the prefix cannot overfill rung 0 by masking.
        On the run above that census read 2 seeded against a target of 3: the one draft it was
        refusing to authorise was the seed the run was missing, not a duplicate of the one running.

    So the guard keeps its meaning and loses its reach: consulted where the masked view is built,
    not where the unmasked board answers. Nothing here widens what any lane may DECIDE — the forced
    seed prefix is the same authority `card_next_actions` uses on the serial path, asked the same
    way — and the promotion clause is unchanged in force.

    Two conditions, kept separate because they are different unsoundnesses:

    1. AN UNRESOLVED RUNG-0 ROOT IS MASKED. It has no metric, so ASHA cannot know the survivor set,
       and the masked view reads its slot as free. `reopened_node_ids` exempts the freshness caller,
       which deliberately reopens its own subject into the population and is therefore not masking
       it. Promotion children have parents and may still fill other already-decided same-rung slots.
    2. A MASKED PROMOTION HAS NO EXACT DURABLE ACTION. Masking an unresolved promotion whose Card
       does not carry `("improve", its parents)` would make the parent look unexpanded and permit a
       duplicate same-rung child.

    OPEN[asha-promotion-mask-blocks-all-production] clause 2 still refuses the WHOLE discretionary
    lane, so an ASHA run whose seeding is complete and whose in-flight promotion carries no exact
    durable action produces nothing at all while a slot is free — measured at 2.08 starved hours
    over 6 intervals on `runs/rubertlite-dr-unified-v8`, the entire residue after the clause-1 fix.
    Whether an UNMASKED policy query plus a filter over the masked node's own action is sound here
    is not measured, and refusing on an unmeasured alternative is the correct default; what is not
    correct is leaving the number unrecorded.
    proof:present:asha_mask_unsound@looplab/search/card_selection.py
    """
    if _builtin_policy_name(policy) != "ASHAPolicy":
        return False
    if not reopened_node_ids and any(
        (node := state.nodes.get(node_id)) is not None
        and node.status is NodeStatus.pending
        and not node.parent_ids
        for node_id in ignored_pending
    ):
        return True
    for node_id in ignored_pending:
        node = state.nodes.get(node_id)
        if node is None or node.status is not NodeStatus.pending or not node.parent_ids:
            continue
        card_id = node.idea.card_id
        card = state.cards.get(card_id) if isinstance(card_id, str) else None
        key = _action_key(card_action(card) or {}) if card is not None else None
        if key != ("improve", tuple(node.parent_ids)):
            return True
    return False


def _speculative_selection(
    state: RunState,
    policy: object,
    max_nodes: int,
    *,
    context: SpeculativeSelectionContext,
    include_owned_card_id: str | None,
    include_owned_node_id: int | None,
) -> tuple[list[Card], list[Action]]:
    scoring = context.scoring
    resource_envelope = context.resource_envelope
    consumed_inflight = context.consumed_inflight
    excluded = _card_id_set(context.excluded_card_ids)
    ignored_pending = _node_id_set(context.ignored_pending_node_ids)
    selection_state = state
    reopened_card_ids: frozenset[str] = frozenset()
    reopened_node_ids: frozenset[int] = frozenset()
    if (include_owned_card_id is None) != (include_owned_node_id is None):
        return [], []
    if include_owned_card_id is not None and include_owned_node_id is not None:
        counterfactual = _counterfactual_owned_selection_state(
            state,
            include_owned_card_id,
            include_owned_node_id,
            consumed_inflight=consumed_inflight,
        )
        if counterfactual is None:
            return [], []
        selection_state, reopened_card_ids, reopened_node_ids = counterfactual
        excluded = frozenset(
            owned_card_id for owned_card_id in excluded
            if owned_card_id not in reopened_card_ids
        )
        ignored_pending = frozenset(
            owned_node_id for owned_node_id in ignored_pending
            if owned_node_id not in reopened_node_ids
        )

    # WHETHER THE MASKED POLICY VIEW WOULD ANSWER UNSOUNDLY. Computed here, where the mask is
    # settled, and CONSULTED below at the one lane that builds that view — see
    # `_asha_mask_is_unsound` for why it is not a return.
    asha_mask_unsound = _asha_mask_is_unsound(
        selection_state, policy, ignored_pending, reopened_node_ids)
    # Outstanding requests and build markers reserve capacity before they become Node rows.  Committed
    # excluded Cards already have evidence and are therefore already included in card_budget_used.
    effective_limit = max(
        0,
        _bounded_nonnegative_int(max_nodes)
        - _reserved_speculative_slots(state, excluded),
    )
    forced = _forced_card_actions(
        selection_state,
        policy,
        effective_limit,
        ignored_pending_node_ids=ignored_pending,
    )
    if forced is not None:
        # Pending/debug/budget/raw-seed gates remain absolute.  The sole executable forced prefix is
        # a lane of already-staged Card receipts (not a discretionary score): letting the producer
        # consume it is what bootstraps a run before the first coded Node exists.  Any mixed, malformed,
        # stale or excluded lane fails closed rather than silently weakening the forced order.
        if not forced:
            return [], []
        forced_ids: list[str] = []
        raw_forced = False
        for action in forced:
            card_id = action.get(META_CARD_ID) if isinstance(action, Mapping) else None
            if card_id is None and isinstance(action, Mapping) and META_CARD_ID not in action:
                raw_forced = True
                continue
            if not isinstance(card_id, str) or not card_id or card_id in forced_ids:
                return [], []
            forced_ids.append(card_id)
        if raw_forced:
            # A forced raw seed/debug lane is the producer's next proposal authority. Never mix it
            # with receipt-owned Cards in one turn; the main task stages it durably before requesting.
            return ([], forced) if not forced_ids else ([], [])
        # An outstanding request has already reserved its effective-budget slot and is deliberately
        # excluded from another election.  Remove only those exact ids before validating the rest of
        # the forced lane; every non-excluded member must still survive as the same ready Card.
        forced_ids = [card_id for card_id in forced_ids if card_id not in excluded]
        if not forced_ids:
            return [], forced
        eligible_by_id = {
            card.id: card for card in eligible_cards(selection_state, policy)
            if card.id not in excluded
            and _card_generation_fences_current(selection_state, card)
            and card_fits_resource_envelope(card, resource_envelope)
        }
        if any(card_id not in eligible_by_id for card_id in forced_ids):
            return [], []
        return [eligible_by_id[card_id] for card_id in forced_ids], forced

    # THE DISCRETIONARY LANE, and the ONE lane that hands the POLICY a masked view. Everything above
    # answered from the UNMASKED board; from here down `_speculative_policy_state` deletes the masked
    # Nodes out of what ASHA reads, which is precisely the unsoundness `_asha_mask_is_unsound` names.
    if asha_mask_unsound:
        return [], []

    reserved_asha_actions: set[tuple[str, tuple[int, ...]]] = set()
    if _builtin_policy_name(policy) == "ASHAPolicy":
        reserved_card_ids = set(excluded)
        # A session also masks its initial, non-speculative pending batch from ASHA's policy view.
        # Reserve those exact actions here even when a caller did not redundantly list their Card ids
        # in `excluded_card_ids`, otherwise a survivor appears unexpanded and can be built twice.
        reserved_card_ids.update(
            node.idea.card_id
            for node_id in ignored_pending
            if (node := selection_state.nodes.get(node_id)) is not None
            if isinstance(node.idea.card_id, str)
        )
        reserved_asha_actions = {
            key for card_id in reserved_card_ids
            if (card := selection_state.cards.get(card_id)) is not None
            if (key := _action_key(card_action(card) or {})) is not None
        }
    policy_state = _speculative_policy_state(selection_state, ignored_pending, policy)
    candidates = [
        card for card in eligible_cards(selection_state, policy)
        if card.id not in excluded
        and _action_key(card_action(card) or {}) not in reserved_asha_actions
        and _card_generation_fences_current(selection_state, card)
        and card_fits_resource_envelope(card, resource_envelope)
    ]
    return _selection_after_forced_gates(
        selection_state,
        policy,
        effective_limit,
        scoring,
        policy_state=policy_state,
        candidate_cards=candidates,
    )


def speculative_card_selection_set(
    state: RunState,
    policy: object,
    max_nodes: int,
    *,
    context: SpeculativeSelectionContext = NO_SPECULATIVE_CONTEXT,
    include_owned_card_id: str | None = None,
    include_owned_node_id: int | None = None,
) -> list[str]:
    """Return Card ids in the Layer-5 producer/freshness selection SET.

    Producer callers pass outstanding/committed ids in ``excluded_card_ids`` and every pending Node
    already owned by that producer/consumer session (including its initial non-spec eval batch) in
    ``ignored_pending_node_ids``.  Freshness callers additionally name one exact owned Card/Node pair;
    that subject and every exact durable, not-yet-consumed speculative sibling are reopened into one
    common population. Exact ``consumed_inflight`` attempts stay masked/excluded. Every receipt,
    generation, anchor, trust, cadence, scorer, lane-width, pin and budget gate remains.
    """

    selected, _ = _speculative_selection(
        state, policy, max_nodes, context=context,
        include_owned_card_id=include_owned_card_id,
        include_owned_node_id=include_owned_node_id,
    )
    return [card.id for card in selected]


def speculative_card_actions(
    state: RunState,
    policy: object,
    max_nodes: int,
    *,
    context: SpeculativeSelectionContext = NO_SPECULATIVE_CONTEXT,
    include_owned_card_id: str | None = None,
    include_owned_node_id: int | None = None,
) -> list[Action]:
    """Action projection of ``speculative_card_selection_set`` for producer ELECTION.

    Election runs BEFORE the consumer admits an attempt, so it reads the context's
    `consumed_inflight` at its empty default and callers leave it unset — see
    `SpeculativeSelectionContext`. Passing a populated one here would mask attempts that have not
    happened yet from this vantage point.
    """

    selected, fallback = _speculative_selection(
        state, policy, max_nodes, context=context,
        include_owned_card_id=include_owned_card_id,
        include_owned_node_id=include_owned_node_id,
    )
    actions: list[Action] = []
    for card in selected:
        action = card_action(card)
        if action is None:
            continue
        action.update(_policy_metadata_for_card_action(policy, action, fallback))
        actions.append(action)
    return actions


def speculative_raw_actions(
    state: RunState,
    policy: object,
    max_nodes: int,
    *,
    context: SpeculativeSelectionContext = NO_SPECULATIVE_CONTEXT,
) -> list[Action]:
    """Return the counterfactual raw proposal lane only when no durable Card owns it.

    This is the steady-state producer seam: while evals are in flight, the main task may materialize
    the policy's next raw action into a Card, then elect that exact receipt. Existing ready Cards always
    win, and evaluate/finalization actions are never converted into proposals.
    """

    # A custom policy without the built-in scorer contract may intentionally fall back to raw
    # ``next_actions`` even after a Card exists. Intercepting that lane would repeatedly stage/reuse an
    # item the producer can never elect. Preserve L3's documented compatibility by building it serially.
    if _builtin_policy_name(policy) is None:
        return []

    excluded = _card_id_set(context.excluded_card_ids)
    selected, fallback = _speculative_selection(
        state, policy, max_nodes, context=context,
        include_owned_card_id=None, include_owned_node_id=None,
    )
    if selected or not fallback:
        return []
    # Ablation is executed by the outer orchestrator before Card creation.  It has no concrete Card
    # projection and cannot be request-built, so intercepting it here would mis-stage an improve Card
    # that the due-ablation guard can never elect.
    creates = {"draft", "improve", "debug", "merge"}
    if any(
        not isinstance(action, Mapping)
        or META_CARD_ID in action
        or action.get("kind") not in creates
        for action in fallback
    ):
        return []
    remaining = max(
        0,
        _bounded_nonnegative_int(max_nodes)
        - card_budget_used(state)
        - _reserved_speculative_slots(state, excluded),
    )
    return [dict(action) for action in fallback[:remaining]]


def speculative_card_is_fresh(
    state: RunState,
    policy: object,
    max_nodes: int,
    *,
    card_id: str,
    node_id: int,
    context: SpeculativeSelectionContext = NO_SPECULATIVE_CONTEXT,
) -> bool:
    """Whether one committed speculative subject remains in the current selection SET.

    The orchestrator must first prove the Node's durable speculative marker + matching done link.
    This pure predicate then proves the exact Card/Node ownership pair, permanent resource fit, and
    counterfactual SET membership. Exact consumer-admitted siblings do not compete for a future backlog
    slot. It deliberately does not require strict rank 1.
    """

    node = state.nodes.get(node_id) if type(node_id) is int else None
    card = state.cards.get(card_id) if isinstance(card_id, str) else None
    if (
        node is None
        or card is None
        or node.status is not NodeStatus.pending
        or node.attempt != 0
        or node.idea.card_id != card_id
        or not card_fits_resource_envelope(card, context.resource_envelope, node=node)
    ):
        return False
    return card_id in speculative_card_selection_set(
        state, policy, max_nodes, context=context,
        include_owned_card_id=card_id, include_owned_node_id=node_id,
    )


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
        # META_RUNG, not a bare "_rung": the meta-key constants live in search/policy.py, and a
        # literal that drifts from the constant silently no-ops (the ASHA promotion metadata would
        # just stop being stamped, with no test going red).
        and fallback[0].get(META_RUNG) is not None
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
