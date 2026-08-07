"""Native-Card reservation & receipt ledger — extracted from orchestrator.py as a MIXIN
(doc 25 ES-01): `class Engine(CardReservationMixin, …)` inherits these methods unchanged, so there
is ZERO call-site churn and `self` here IS the engine.

This is the cluster ES-01 measured as "a cohesive reservation/receipt subsystem larger than most
existing mixins": the two monotonic id ALLOCATORS (`_node_id_ceiling`, `_card_id_ceiling` /
`_next_available_card_id`) and everything that mints, claims, stages, refuses and closes a native
Card's build reservation. The two reservation records the cluster hands between the main task and a
build worker (`_BuildReservation`, `_CardReservationPlan`) moved with it; orchestrator.py imports
`_BuildReservation` back, so `looplab.orchestrator._BuildReservation` keeps resolving.

WHY `fold` IS NOT IMPORTED HERE — read this before "simplifying" `_fold` away
---------------------------------------------------------------------------
`node_build.py` records the general rule: the module-global `fold` in orchestrator.py is a
monkeypatch SEAM, and moving a fold-caller out of that module silently detaches it. ES-01 offered
"import fold from the canonical home (as evaluate.py does)" as one option, and named "the two
fold-monkeypatching tests" to re-verify. What that instruction is actually worth here was measured
rather than read, by breaking `_fold` into a direct import on a throwaway copy of the tree:

  * FOUR files patch the seam through the orchestrator module, not two —
    `tests/test_continuous_dispatch.py`, `tests/test_gpu_resources.py`,
    `tests/test_creation_runaway_guard.py`, `tests/test_hypothesis_merge.py`.
  * How far each actually reaches this cluster (counted by wrapping `_fold`): the first two drive
    `Engine._dispatch_evals` on a STUB host, which owns none of these methods, so zero.
    `test_hypothesis_merge` also zero — its scar comment ("the orchestrator patch alone stopped
    reaching it after the mixin extraction") is precedent for the CLASS of bug, not an instance of
    it here. `test_creation_runaway_guard` runs a real `Engine` and reaches `_fold` exactly once.
  * And ALL FIFTY-NINE of those tests stay GREEN with the ledger folding through its own import.

So the suite has no coverage of this at all, which is the reason for `_fold` rather than an
argument against it: a direct import would silently SHRINK what `monkeypatch.setattr(orch, "fold",
…)` means — the seam would stop covering ~1,100 lines of engine — and nothing would go red to say
so. `test_creation_runaway_guard`'s own reasoning about the Card lane under an empty fold ("every
later turn plans a create the Card lane can no longer reserve") silently stops being true of what
it runs, even though its assertion survives. `_fold` resolves `orchestrator.fold` as a module
ATTRIBUTE at call time, so the seam keeps the scope it had; `tests/test_card_reservation_fold_seam.py`
is what makes that falsifiable, and it is driven (a real run, watched interceptions), not pinned.
"""
from __future__ import annotations

import hashlib
from typing import NamedTuple, Optional

import orjson

from looplab.core.advisory_payloads import bounded_cross_run_advisory_receipt
from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import (Idea, RunState, card_action_digest, card_ownership_receipt,
                                 durable_idea_payload, idea_proposal_ref,
                                 normalize_researcher_footprint)
from looplab.engine.proposal_cues import normalize_steering_context
from looplab.events.eventstore import EventStoreConcurrencyError, retry_tail_cas
from looplab.events.types import (EV_CARD_ADDED, EV_CARD_AUTO_DROPPED, EV_CARD_DROPPED,
                                  EV_CARD_MERGED, EV_HYPOTHESIS_MERGED, EV_NODE_BUILDING,
                                  EV_NOVELTY_REJECTED)
from looplab.search.card_selection import (META_CARD_ID, card_action as projected_card_action,
                                           card_budget_used, card_selection_set, eligible_cards,
                                           forced_card_actions)


def _fold(events):
    """Fold THROUGH the orchestrator module attribute — see the module docstring.

    The deferred import is the point: binding `orchestrator.fold` at import time would snapshot the
    real function and make `monkeypatch.setattr(orch, "fold", …)` a silent no-op for this cluster.
    """
    from looplab.engine import orchestrator
    return orchestrator.fold(events)


class _BuildReservation(NamedTuple):
    """Durable node/card reservation handed from the main task to one build worker.

    The first five positions preserve the historical internal tuple layout, so focused tests and
    integrations that only read ``reservation[1]`` (the node id) keep working.  The final two fields
    carry the exact native Card identity and already-prepared Idea; no worker is allowed to mint or
    re-propose after the main task has committed the reservation.
    """

    state: RunState
    node_id: int
    kind: str
    parent_ids: list[int]
    parent_generations: dict[str, int]
    card_id: Optional[str]
    idea: Optional[Idea]


class _CardReservationPlan(NamedTuple):
    """Pure result of resolving one exact native Card identity against the journal."""

    disposition: str  # mint | reuse | duplicate | invalid
    card_id: Optional[str]
    idea: Optional[Idea]
    payload: Optional[dict]


class CardReservationMixin:
    def _record_dropped_batch_cards(self, dropped) -> None:
        """Give every rejected proposal in a batch its node-less closed Card.

        The reason string is TRUNCATED and defaulted here rather than at three call sites: a card
        whose reason silently became the empty string reads on the board as a drop with no cause.
        """
        for drop in dropped or []:
            if isinstance(drop, dict) and isinstance(drop.get("idea"), Idea):
                self._record_node_less_card(
                    drop["idea"],
                    reason=str(drop.get("reason") or "proposal_rejected")[:160],
                    steering_context=drop.get("steering_context", []),
                )

    @staticmethod
    def _node_id_ceiling(events, state) -> int:
        """The next unique, monotonic node id = 1 + max of every id EVER reserved (a `node_building` event)
        OR created (`state.nodes`). A `node_building` folds to the transient single `st.building` marker,
        NOT `st.nodes`, so a plain `max(state.nodes)+1` would hand concurrent builds the same id. Every
        site that MINTS a node (draft build, ablation refine_block, forced inject) computes the id from
        this helper AND commits (node_building/node_created) under `_id_lock`, so parallel builds never
        collide. Replay-deterministic (ids follow the log's node_building order); a failed reservation
        leaves a harmless id gap."""
        _max_building = max((e.data.get("node_id", -1) for e in events
                             if e.type == EV_NODE_BUILDING and isinstance(e.data.get("node_id"), int)),
                            default=-1)
        return max(max(state.nodes, default=-1), _max_building) + 1

    @staticmethod
    def _canonical_card_id(value) -> Optional[str]:
        """Mirror replay's bounded Card-id canonicalization without copying hostile strings."""
        if not isinstance(value, str) or len(value) > 256:
            return None
        bounded = value.strip()
        return bounded if bounded and bounded.isprintable() else None

    @classmethod
    def _engine_card_number(cls, value) -> Optional[int]:
        """Return ``k`` only for the writer-owned canonical spelling ``card-{k}``."""
        card_id = cls._canonical_card_id(value)
        if card_id is None or value != card_id or not card_id.startswith("card-"):
            return None
        suffix = card_id[5:]
        if (not suffix or not suffix.isascii() or not suffix.isdecimal()
                or (len(suffix) > 1 and suffix.startswith("0"))):
            return None
        number = int(suffix)
        return number if card_id == f"card-{number}" else None

    @classmethod
    def _card_id_ceiling(cls, events) -> int:
        """Next monotonic ``card-{k}`` suffix from every raw card_added receipt in the log.

        Folded Cards are intentionally unsuitable for allocation: conflicts, merges and malformed
        registrations may suppress them. The append-only log remains the durable reservation ledger.
        Canonicalize whitespace exactly like replay before scanning, and reject oversized input before
        ``int`` so a corrupt 5,000-digit suffix cannot trip Python's conversion guard.
        """
        ceiling = 0
        for event in events:
            if event.type != EV_CARD_ADDED:
                continue
            raw = cls._canonical_card_id(event.data.get("id"))
            if raw is None or not raw.startswith("card-"):
                continue
            suffix = raw[5:]
            if not suffix or not suffix.isascii() or not suffix.isdecimal():
                continue
            ceiling = max(ceiling, int(suffix) + 1)
        if len(f"card-{ceiling}") > 256:
            raise RuntimeError("native card id space is exhausted")
        return ceiling

    @staticmethod
    def _card_statement(idea: Idea) -> Optional[str]:
        """Return one lossless bounded seed statement, or ``None`` when it cannot be owned safely.

        The node-side join uses ``Idea.hypothesis.strip()``. Silently collapsing whitespace, deleting
        controls, or truncating here creates two seed identities under one explicit card id and makes
        the fail-closed projection suppress the Card. Choose the first actually non-empty source and
        reject an unrepresentable identity instead.
        """
        hypothesis = idea.hypothesis.strip() if isinstance(idea.hypothesis, str) else ""
        rationale = idea.rationale.strip() if isinstance(idea.rationale, str) else ""
        statement = hypothesis or rationale or f"{idea.operator} experiment"
        if (not statement or len(statement) > 2_048 or not statement.isprintable()
                or statement != statement.strip()):
            return None
        return statement

    @staticmethod
    def _implementation_ref(*, code=None, files=None, deleted=None) -> Optional[str]:
        """Exact bounded digest of operator-supplied implementation material.

        Ordinary Researcher/Developer builds pass no material and return ``None``. Inject requests may
        carry ready code/files; folding two such requests merely because their Idea matches would lose
        executable work, so the crash-prefix matcher also binds this digest.
        """
        if code in (None, "") and not files and not deleted:
            return None
        if code is not None and not isinstance(code, str):
            raise ValueError("injected code must be text")
        if files is None:
            files = {}
        if (not isinstance(files, dict)
                or any(not isinstance(key, str) or not isinstance(value, str)
                       for key, value in files.items())):
            raise ValueError("injected files must be a text mapping")
        if deleted is None:
            deleted = []
        if (not isinstance(deleted, list)
                or any(not isinstance(value, str) for value in deleted)):
            raise ValueError("injected deleted paths must be a text list")
        encoded = orjson.dumps(
            {"code": code or "", "files": files, "deleted": deleted},
            option=orjson.OPT_SORT_KEYS,
        )
        if len(encoded) > 16 * 1024 * 1024:
            raise ValueError("injected implementation identity is oversized")
        return "implementation:v1:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _build_parent_snapshot(state: RunState, action: dict):
        """Validate and snapshot the exact parent generations named by one build action."""
        kind = action.get("kind")
        if not isinstance(kind, str) or not kind:
            return None
        raw_parents = action.get("parent_ids")
        if raw_parents:
            if not isinstance(raw_parents, list):
                return None
            parents = list(raw_parents)
        else:
            parents = [action["parent_id"]] if action.get("parent_id") is not None else []
        if (len(parents) > 64 or len(set(parents)) != len(parents)
                or any(type(pid) is not int or pid < 0 for pid in parents)):
            return None
        raw_expected = action.get("parent_generations")
        if raw_expected is not None and not isinstance(raw_expected, dict):
            return None
        parent_generations: dict[str, int] = {}
        for pid in parents:
            parent = state.nodes.get(pid)
            if parent is None or parent.tombstoned or pid in state.aborted_nodes:
                return None
            if raw_expected is not None:
                expected = raw_expected.get(str(pid), raw_expected.get(pid))
                if isinstance(expected, bool):
                    return None
                try:
                    expected = int(expected)
                except (TypeError, ValueError, OverflowError):
                    return None
                if expected != parent.attempt:
                    return None
            parent_generations[str(pid)] = parent.attempt
        if raw_expected is not None and len(raw_expected) != len(parent_generations):
            return None
        return kind, parents, parent_generations

    @staticmethod
    def _fixed_point_idea(idea: Idea) -> Idea:
        """Re-run an Idea through its OWN validators, so what the Card binds is a fixed point of them.

        `Idea.model_config` is empty (`core/models.py`) — there is no `validate_assignment` — and the
        two proposal funnels admit an existing instance WITHOUT re-validating it
        (`orchestrator.py::_prepare_node_idea._link` and `novelty.py::_propose_batch._link_card` both
        spell `candidate if isinstance(candidate, Idea) else Idea.model_validate(candidate)`). So any
        producer that ASSIGNS onto an Idea escapes every validator on the way to the mint. Two live
        instances so far — `agents/roles.py::_clamp_fill`'s bounds clamp on a swept key, and
        `engine/novelty.py`'s numeric nudge (`out.params = nudged` onto a `model_copy()`) — and neither
        was the cause: the cause is the unvalidated funnel, which is why the repair is here rather than
        at each producer.

        This is the HEALING half, and deliberately NOT the guarantee. It re-runs the PRODUCER's own
        validators (`type(idea)`), while a claim rebuilds a base `Idea` from the durable ACTION — so a
        subclass whose validators differ from the durable schema (pydantic lets a subclass replace a
        validator by name, and a plugin schema that opts out of the space clamp round-trips to itself)
        is healed to something the claim still will not reproduce. Re-validation that RAISES is
        swallowed here too. `_card_added_payload` therefore still PROVES the round trip and refuses;
        this only keeps the healable majority — a value nudged just outside its own declared grid,
        which is both live producers — from being thrown away with it.

        It re-validates through `durable_idea_payload`, NOT a raw `model_dump()`, and that is not a
        stylistic choice: `durable_idea_payload` is the boundary `node_created` writes and the fold
        rebuilds from, so this returns exactly the Idea replay will produce. A raw `model_dump()`
        materializes the three concept-list defaults, which puts them in `model_fields_set` and turns
        an ABSENT legacy concept envelope into an authored empty one — a different `idea_proposal_ref`
        (measured: it broke exact crash-prefix Card reuse, which compares the whole writer payload).

        A subclass that cannot rebuild itself from its own durable payload falls through unchanged and
        is refused by that proof — never crashing the proposal path on the way there.
        """
        try:
            return type(idea).model_validate(durable_idea_payload(idea))
        except Exception:  # noqa: BLE001 - the mint's round-trip proof below is the fail-closed half
            return idea

    @staticmethod
    def _rebuilt_claim_idea(card_id: str, statement: str, action: dict, rationale: str,
                            concepts: Optional[dict] = None) -> Idea:
        """Rebuild the Idea a claim will execute, from the immutable action its digest covers.

        ONE spelling, called from both ends of the same round trip: `_card_added_payload` proves the
        mint is a fixed point of it, `_prepare_existing_card_claim` runs it for real when the Card is
        claimed. A second hand-synced copy would let the mint prove a rebuild the claim no longer
        performs — the guard would go quietly vacuous instead of red, which is exactly the failure it
        exists to stop.
        """
        return Idea(
            operator=action["operator"],
            params=dict(action.get("params") or {}),
            space={key: list(values) for key, values in (action.get("space") or {}).items()},
            rationale=rationale,
            eval_profile=action.get("eval_profile"),
            eval_timeout=action.get("eval_timeout"),
            hypothesis=statement,
            card_id=card_id,
            footprint=normalize_researcher_footprint(action.get("footprint")),
            **(concepts or {}),
        )

    @staticmethod
    def _card_action(idea: Idea, parents: list[int], parent_generations: dict[str, int],
                     scored_against: Optional[int], scored_against_generation: Optional[int],
                     *, scored_against_empty: bool) -> dict:
        footprint = normalize_researcher_footprint(idea.footprint)
        return {
            "operator": idea.operator,
            "params": dict(idea.params or {}),
            "space": {key: list(values) for key, values in (idea.space or {}).items()},
            "eval_profile": idea.eval_profile,
            "eval_timeout": idea.eval_timeout,
            "parent_id": parents[0] if parents else None,
            "parent_ids": list(parents),
            "parent_generations": dict(parent_generations),
            "scored_against": scored_against,
            "scored_against_generation": scored_against_generation,
            "scored_against_empty": scored_against_empty,
            "footprint": footprint,
        }

    @classmethod
    def _card_added_payload(cls, card_id: str, statement: str, action: dict, idea: Idea, *,
                            source: str, at_node: int,
                            implementation_ref: Optional[str] = None,
                            steering_context=(), cross_run_receipt=None) -> dict:
        receipt = card_ownership_receipt(card_id, statement, action)
        proposal_ref = idea_proposal_ref(idea)
        bounded_steering = normalize_steering_context(steering_context)
        if (receipt is None or proposal_ref is None
                or bounded_steering is None
                or not isinstance(source, str) or not source or len(source) > 64
                or source != source.strip() or not source.isprintable()
                or type(at_node) is not int or not 0 <= at_node <= (1 << 31) - 1):
            raise ValueError("prepared idea cannot form a bounded native card receipt")
        rationale = (idea.rationale or "")[:400]
        # THE ROUND TRIP, PROVED WHERE THE RECEIPT IS MINTED — the one invariant that makes a Card
        # claimable at all. `receipt` above digests THIS `action`; `_prepare_existing_card_claim`
        # re-derives that digest by rebuilding the Idea from the durable Card and calling
        # `_card_action` on it AGAIN. When the two disagree the digest can never match, so the Card is
        # unclaimable from the instant it is written: the selector keeps electing it, the claim keeps
        # refusing, and the create lane spins forever. Both live producers of that state assigned onto
        # an existing Idea and so skipped `Idea`'s validators (see `_fixed_point_idea`), and the digest
        # is computed from the REBUILT form — which is why the check belongs here, at the single place
        # an ownership receipt is created, and not at either producer.
        #
        # It fails CLOSED, before a Card exists: every caller turns the refusal into an "invalid"
        # disposition (`_plan_native_card`) or a declined reuse (`_card_event_matches`). A `params`
        # that the digest itself rejects (NaN, inf, 65 keys, an oversized key) is already refused by
        # `card_ownership_receipt` above and never reaches this rebuild.
        rebuilt = cls._rebuilt_claim_idea(card_id, statement, action, rationale)
        rebuilt_action = cls._card_action(
            rebuilt, list(action.get("parent_ids") or []),
            dict(action.get("parent_generations") or {}),
            action.get("scored_against"), action.get("scored_against_generation"),
            scored_against_empty=bool(action.get("scored_against_empty")),
        )
        if cls._card_statement(rebuilt) != statement or rebuilt_action != action:
            raise ValueError(
                "prepared idea is not a fixed point of its own validators: the card action cannot "
                "be rebuilt from the receipt it would be minted under")
        if (implementation_ref is not None
                and (not isinstance(implementation_ref, str)
                     or not implementation_ref.startswith("implementation:v1:")
                     or len(implementation_ref) != len("implementation:v1:") + 64)):
            raise ValueError("invalid implementation identity")
        advisory_receipt = bounded_cross_run_advisory_receipt(cross_run_receipt)
        return {
            "id": card_id,
            "statement": statement,
            "source": source,
            "at_node": at_node,
            "rationale": rationale,
            # Deliberately narrow: replay treats any future executable member in this block as an
            # incomplete v1 action rather than silently blessing lossy semantics.
            # the production writer also drops Idea.concepts, while novelty/card-enriched
            # signals gain a Card subject only after a Node exists. Such a Card is then work-owned and
            # no longer selection-ready, so real selectable Cards reach novelty/coverage scoring empty.
            # Persist bounded proposal-time scoring receipts, or remove these terms from live ranking.
            "idea": {
                "operator": action["operator"],
                "params": action["params"],
                "space": action["space"],
                "eval_profile": action["eval_profile"],
                "eval_timeout": action["eval_timeout"],
            },
            "parent_id": action["parent_id"],
            "parent_ids": action["parent_ids"],
            "parent_generations": action["parent_generations"],
            "scored_against": action["scored_against"],
            "scored_against_generation": action["scored_against_generation"],
            "scored_against_empty": action["scored_against_empty"],
            "footprint": action["footprint"],
            "steering_context": bounded_steering,
            "ownership_receipt": receipt,
            # Full normalized Idea identity is a separate crash-reuse/dedupe proof. The receipt-bound
            # Card action deliberately stays compact, but two repo rationales or implementation budgets must not
            # collapse merely because their params/profile happen to match.
            "proposal_ref": proposal_ref,
            **({"implementation_ref": implementation_ref} if implementation_ref else {}),
            **({"cross_run_receipt": advisory_receipt} if advisory_receipt else {}),
        }

    @classmethod
    def _card_event_matches(cls, data: dict, idea: Idea, action: dict, *, source: str,
                            at_node: int, implementation_ref: Optional[str],
                            steering_context=(), cross_run_receipt=None) -> bool:
        """True only for the exact writer shape used by a crash-prefix card reservation."""
        card_id = data.get("id")
        if cls._engine_card_number(card_id) is None:
            return False
        rebound = idea.model_copy(deep=True, update={"card_id": card_id})
        statement = cls._card_statement(rebound)
        if statement is None:
            return False
        expected = cls._card_added_payload(
            card_id, statement, action, rebound, source=source, at_node=at_node,
            implementation_ref=implementation_ref, steering_context=steering_context,
            cross_run_receipt=cross_run_receipt,
        )
        if data == expected:
            return True
        # ``at_node`` is allocation-time provenance, not executable proposal identity. A second
        # pre-reservation naturally sees a later node-id ceiling; everything else in the immutable
        # writer receipt must still match exactly so active work dedupes without weakening source,
        # action, steering, implementation or advisory identity.
        recorded_at_node = data.get("at_node")
        if (type(recorded_at_node) is not int
                or not 0 <= recorded_at_node <= (1 << 31) - 1):
            return False
        expected["at_node"] = recorded_at_node
        # This is intentionally a writer-prefix matcher, not a loose semantic comparison. A future
        # additive mint field must make an old writer decline reuse until that field is reviewed.
        return data == expected

    @staticmethod
    def _card_score_snapshot(
            state: RunState,
            requested: Optional[int]) -> Optional[tuple[Optional[int], Optional[int], bool]]:
        """Identity of the node a card is scored against: `(id, attempt, empty)`, or None to REFUSE.

        The two falsy-looking outcomes are different answers and both are load-bearing. A bare
        ``None`` means the request is not scorable (out-of-range id, or a tombstoned/aborted node) —
        callers must abandon the reservation. The ``(None, None, True)`` triple means there is
        legitimately nothing to score against yet (no best node); that is a valid snapshot and it
        compares equal across two folds, which is what the pre-launch freshness fence needs.
        Every caller therefore checks ``is None`` BEFORE unpacking.
        """
        score_id = state.best_node_id if requested is None else requested
        if score_id is None:
            return None, None, True
        if type(score_id) is not int or not 0 <= score_id <= (1 << 31) - 1:
            return None
        node = state.nodes.get(score_id)
        if node is None or node.tombstoned or score_id in state.aborted_nodes:
            return None
        return score_id, node.attempt, False

    @classmethod
    def _next_available_card_id(cls, events, state: RunState, excluded=()) -> str:
        """Allocate from the raw log ceiling, skipping only exact namespace collisions.

        Node-only/marker ids are not allocator authority (a stray ``card-99`` must not jump the
        ceiling to 100), but the exact next spelling cannot be reused without joining unrelated legacy
        evidence to a newly-native Card.
        """
        used = {
            card_id
            for event in events if event.type == EV_CARD_ADDED
            if (card_id := cls._canonical_card_id(event.data.get("id"))) is not None
        }
        used.update(
            card_id for node in state.nodes.values() if node.idea is not None
            if (card_id := cls._canonical_card_id(node.idea.card_id)) is not None
        )
        used.update(
            card_id for marker in state.buildings.values() if isinstance(marker, dict)
            if (card_id := cls._canonical_card_id(marker.get("card_id"))) is not None
        )
        used.update(card_id for card_id in state.cards
                    if cls._canonical_card_id(card_id) is not None)
        used.update(card_id for value in excluded
                    if (card_id := cls._canonical_card_id(value)) is not None)
        number = cls._card_id_ceiling(events)
        while f"card-{number}" in used:
            number += 1
            if len(f"card-{number}") > 256:
                raise RuntimeError("native card id space is exhausted")
        return f"card-{number}"

    @classmethod
    def _plan_native_card(cls, events, state: RunState, idea: Idea, *, parents: list[int],
                          parent_generations: dict[str, int], scored_against: Optional[int],
                          source: str, at_node: int,
                          implementation_ref: Optional[str] = None, excluded=(),
                          steering_context=(), cross_run_receipt=None,
                          superseded_card_id: Optional[str] = None) -> _CardReservationPlan:
        """Resolve exact live dedupe, crash-prefix reuse, or a fresh engine id without appending."""
        # The funnel every native mint passes through, and therefore the place to heal a proposal that
        # reached it un-revalidated (see `_fixed_point_idea`). Doing it HERE rather than at the two
        # `_link`/`_link_card` callers covers all five call sites at once, and covers the ones that
        # mutate AFTER the funnel too — `_stage_prepared_card` re-clamps the footprint through
        # `model_copy(update=…)`, which is another validator bypass by construction. Healing is not a
        # behaviour change for a healthy proposal: `model_validate` of a fixed point returns an equal
        # Idea, so the minted bytes are identical. For an unhealthy one it costs nothing either — the
        # NODE's Idea is rebuilt through the same validators at every fold, so the un-healed value was
        # already not what replay saw, only what the Developer was handed.
        idea = cls._fixed_point_idea(idea)
        score_snapshot = cls._card_score_snapshot(state, scored_against)
        if score_snapshot is None:
            return _CardReservationPlan("invalid", None, None, None)
        score_id, score_generation, score_empty = score_snapshot
        statement = cls._card_statement(idea)
        if statement is None:
            return _CardReservationPlan("invalid", None, None, None)
        action = cls._card_action(
            idea, parents, parent_generations, score_id, score_generation,
            scored_against_empty=score_empty,
        )

        registrations: dict[str, int] = {}
        matches: list[str] = []
        for event in events:
            if event.type != EV_CARD_ADDED:
                continue
            cid = cls._canonical_card_id(event.data.get("id"))
            if cid is not None:
                registrations[cid] = registrations.get(cid, 0) + 1
            try:
                if cls._card_event_matches(
                        event.data, idea, action, source=source, at_node=at_node,
                        implementation_ref=implementation_ref,
                        steering_context=steering_context,
                        cross_run_receipt=cross_run_receipt):
                    matches.append(cid)
            except (TypeError, ValueError, OverflowError):
                continue

        reusable: list[str] = []
        unsafe_match = False
        merged_aliases = {
            alias
            for receipt in (getattr(state, "cards_merged", None) or [])
            if isinstance(receipt, dict)
            for raw_alias in (receipt.get("aliases") or [])
            if (alias := cls._canonical_card_id(raw_alias)) is not None
            and alias != cls._canonical_card_id(receipt.get("canonical"))
        }
        for cid in matches:
            if cid == superseded_card_id:
                continue
            if (cid is None or cls._engine_card_number(cid) is None
                    or registrations.get(cid) != 1 or cid in excluded):
                unsafe_match = True
                continue
            if cid in merged_aliases:
                # The immutable alias registration remains in the raw journal but its work item was
                # explicitly closed by consolidation. It is not reusable and must not permanently ban a
                # deliberate fresh retry of the same proposal under a new monotonic Card id.
                continue
            projected = state.cards.get(cid)
            if projected is None or projected.identity.kind != "native":
                unsafe_match = True
                continue
            owner_state = projected.selection_provenance.owner_state
            if owner_state in {"in_flight", "mixed", "unknown"}:
                return _CardReservationPlan("duplicate", None, None, None)
            if (projected.status == "dropped" or projected.merged_into
                    or "merged_work_items" in projected.selection_blockers
                    or projected.dropped_reason is not None or projected.evidence):
                # Closed/historical work is immutable but does not ban a deliberate future retry.
                continue
            reusable.append(cid)

        if unsafe_match or len(set(reusable)) > 1:
            return _CardReservationPlan("duplicate", None, None, None)
        card_id = reusable[0] if reusable else cls._next_available_card_id(
            events, state, excluded)
        reserved_idea = idea.model_copy(deep=True, update={"card_id": card_id})
        try:
            payload = cls._card_added_payload(
                card_id, statement, action, reserved_idea, source=source, at_node=at_node,
                implementation_ref=implementation_ref, steering_context=steering_context,
                cross_run_receipt=cross_run_receipt,
            )
        except (TypeError, ValueError, OverflowError):
            return _CardReservationPlan("invalid", None, None, None)
        return _CardReservationPlan(
            "reuse" if reusable else "mint", card_id, reserved_idea, payload)

    def _reserve_node_build(self, action: dict, idea: Optional[Idea] = None, *,
                            scored_against: Optional[int] = None,
                            source: str = "researcher",
                            implementation_ref: Optional[str] = None,
                            steering_context=(), cross_run_receipt=None):
        """Reserve one native Card and its node-building owner under one log-tail CAS.

        The final Idea must already exist: the immutable statement and exact action receipt cannot be
        minted honestly before proposal. A new ``card_added`` and its ``node_building{card_id}`` claim
        are one bounded EventStore batch, so another process can land before or after them, never between.
        Legacy orphan registrations remain reusable by an exact retry. ``idea`` remains optional only
        for historical internal callers/tests; production creation paths always supply it.
        """
        if idea is not None and not isinstance(idea, Idea):
            idea = Idea.model_validate(idea)
        with self._id_lock:
            proposal_authority_seq = None

            def _plan(events, tail):
                nonlocal proposal_authority_seq
                authority_seq = self._proposal_authority_seq(events)
                if proposal_authority_seq is None:
                    proposal_authority_seq = authority_seq
                elif authority_seq != proposal_authority_seq:
                    # A control/research/lifecycle event won the CAS. The caller must return to the
                    # selection boundary; silently minting a replacement for a just-dropped orphan
                    # would defeat the operator's stop intent. LLM accounting alone may be retried.
                    return None
                state = _fold(events)
                if state.paused or state.finished or state.stop_requested:
                    return None
                if self._node_reservation_slots_remaining(state, events=events) < 1:
                    return None
                parent_snapshot = self._build_parent_snapshot(state, action)
                if parent_snapshot is None:
                    return None
                kind, parents, parent_generations = parent_snapshot
                node_id = self._node_id_ceiling(events, state)
                if idea is None:
                    # Compatibility seam for callers that reserve only a node id. No production path
                    # uses this branch once writer-side Card minting is enabled.
                    self.store.append(EV_NODE_BUILDING, {
                        "node_id": node_id, "operator": kind, "parent_ids": parents,
                    }, expected_last_seq=tail)
                    return _BuildReservation(
                        state, node_id, kind, parents, parent_generations, None, None)

                plan = self._plan_native_card(
                    events, state, idea, parents=parents,
                    parent_generations=parent_generations,
                    scored_against=scored_against, source=source, at_node=node_id,
                    implementation_ref=implementation_ref, steering_context=steering_context,
                    cross_run_receipt=cross_run_receipt,
                )
                if plan.disposition == "invalid":
                    self._append_proposal_event(EV_NOVELTY_REJECTED, {
                        "node_id": node_id, "generation": 0, "kind": "card_contract",
                        "reason": "proposal cannot form a bounded native Card action",
                        "action": "dropped",
                    })
                    return None
                if plan.disposition == "duplicate":
                    return None
                if plan.disposition not in {"mint", "reuse"} \
                        or plan.card_id is None or plan.idea is None:
                    return None
                # A proposal-bound sidecar may already name this Card. Main-task-only minting means
                # planner and commit must agree; never silently rebind its digest.
                if idea.card_id is not None and idea.card_id != plan.card_id:
                    return None
                card_id = plan.card_id
                reserved_idea = plan.idea
                claim = (EV_NODE_BUILDING, {
                    "node_id": node_id,
                    "operator": kind,
                    "parent_ids": parents,
                    "card_id": card_id,
                })
                if plan.disposition == "mint":
                    self.store.append_many(
                        [(EV_CARD_ADDED, plan.payload), claim],
                        expected_last_seq=tail,
                    )
                else:
                    self.store.append(*claim, expected_last_seq=tail)
                return _BuildReservation(
                    state, node_id, kind, parents, parent_generations,
                    card_id, reserved_idea)

            # No reservation was made, so nothing leaks; the caller returns to the selection boundary.
            return retry_tail_cas(self.store, _plan, on_exhaust=lambda: None)

    @staticmethod
    def _proposal_cue_fence(state: RunState) -> bytes:
        """Bounded proposal authority that may move without changing the search epoch."""

        return orjson.dumps({
            "pending_hints": state.pending_hints,
            "research_count": len(state.research),
            "latest_research": state.research[-1] if state.research else None,
            "pending_strategy": state.pending_strategy,
            "active_strategy": state.active_strategy,
        }, option=orjson.OPT_SORT_KEYS)

    def _stage_prepared_card(self, action: dict, idea: Idea, *, proposal_state: RunState,
                             proposal_node_ceiling: int, at_node: int, source: str,
                             steering_context=(), cross_run_receipt=None,
                             proposal_cue_fence: Optional[bytes] = None,
                             proposal_authority_seq: Optional[int] = None) -> Optional[str]:
        """Commit one concrete proposal as a ready Card, without reserving a Node.

        Layer 5 needs durable inventory *before* it can elect a request-driven producer.  Proposal is
        slow and therefore happens outside ``_id_lock``; this short commit re-folds and accepts the
        result only while its epoch, parents, best anchor and future node-slot ceiling are unchanged.
        Serial callers may retry harmless tail churn; isolated RAW callers additionally fence every
        non-LLM-telemetry event. A lifecycle move returns to the outer loop so a proposal authored
        against an old search state can never be relabelled as current work.
        """
        if not isinstance(idea, Idea):
            try:
                idea = Idea.model_validate(idea)
            except Exception:
                return None
        if (type(proposal_node_ceiling) is not int or proposal_node_ceiling < 0
                or type(at_node) is not int or at_node < proposal_node_ceiling):
            return None
        if (proposal_authority_seq is not None
                and (type(proposal_authority_seq) is not int
                     or proposal_authority_seq < -1)):
            return None
        bounded_steering = normalize_steering_context(steering_context)
        if bounded_steering is None:
            return None
        expected_parent = self._build_parent_snapshot(proposal_state, action)
        expected_score = self._card_score_snapshot(
            proposal_state, proposal_state.best_node_id)
        expected_cues = (
            self._proposal_cue_fence(proposal_state)
            if proposal_cue_fence is None else proposal_cue_fence
        )
        if expected_parent is None or expected_score is None:
            return None

        # A Researcher declaration is persisted as the effective, schedulable request.  In particular,
        # an over-declared GPU count must not become an immutable receipt that Layer 4 later clamps to a
        # different action.  The writer owns card_id, so discard the provisional planner sidecar too.
        clean = idea.model_copy(deep=True, update={
            "card_id": None,
            "footprint": self._clamp_resource_footprint(idea.footprint),
        })

        def _plan(events, tail):
            # The log scan, fold, lifecycle fences and duplicate/id plan are intentionally outside
            # `_id_lock`: they scale with run history and may invoke bounded hashing/validation.  The
            # append's tail CAS is the authority for the snapshot.  If another reservation or control
            # wins after this plan, the CAS loses and the next turn recomputes every derived value.
            # The isolated RAW worker is authorized by one exact semantic proposal prefix. LLM usage
            # telemetry is worker-owned and may advance the physical tail, but every other event is
            # authority-bearing. Serial outer batches omit this optional fence and retain CAS retries.
            if (proposal_authority_seq is not None
                    and self._proposal_authority_seq(events) != proposal_authority_seq):
                return None
            state = _fold(events)
            if (state.search_epoch != proposal_state.search_epoch
                    or state.paused or state.finished or state.stop_requested
                    or state.best_node_id != proposal_state.best_node_id
                    or self._proposal_cue_fence(state) != expected_cues
                    or self._node_id_ceiling(events, state) != proposal_node_ceiling
                    or self._build_parent_snapshot(state, action) != expected_parent
                    or self._card_score_snapshot(
                        state, proposal_state.best_node_id) != expected_score):
                return None
            kind, parents, parent_generations = expected_parent
            del kind
            plan = self._plan_native_card(
                events,
                state,
                clean,
                parents=parents,
                parent_generations=parent_generations,
                scored_against=proposal_state.best_node_id,
                source=source,
                at_node=at_node,
                steering_context=bounded_steering,
                cross_run_receipt=cross_run_receipt,
            )
            if plan.disposition == "reuse":
                # Reuse mutates nothing.  Its eventual request/claim always re-folds and revalidates
                # the Card, so it needs no writer lock or synthetic event merely to stabilize the tail.
                return plan.card_id
            if plan.disposition != "mint" or plan.card_id is None or plan.payload is None:
                return None
            with self._id_lock:
                self.store.append(EV_CARD_ADDED, plan.payload, expected_last_seq=tail)
            return plan.card_id

        # Nothing was staged, so the Card simply does not exist yet; the next proposal turn re-plans it.
        return retry_tail_cas(self.store, _plan, on_exhaust=lambda: None)

    def _card_inventory_enabled(self) -> bool:
        """Whether this run MAINTAINS a Card queue and selects from it.

        Deliberately NOT `_speculation_enabled()`, and the difference is the whole point.
        `_stage_card_creates` below is the only writer that puts a Card into durable INVENTORY — a
        `card_added` with no `node_building` beside it, i.e. the only shape that can ever satisfy
        `_strictly_selection_ready`. Until 2026-08-07 its one production call site sat inside
        `if self._speculation_enabled():`, so with prefetch off every `card_added` was minted by
        `_reserve_node_build` in the same crash-atomic batch as the `node_building` that claimed it:
        work-owned the instant it existed, never selectable, and `card_next_actions` fell through to
        `policy.next_actions`. MEASURED over the corpus by folding every observable prefix boundary:
        the seven runs with `card_driven_selection=true` and no pinned depth produced **0**
        `selection_ready` cards across 27 nodes, while the four speculation runs produced 24.

        That coupling was invisible because `speculation_depth` ships as `-1` (AUTO) and AUTO usually
        resolves positive. But AUTO settles ITSELF to 0 in three documented cases — a build whose
        roles call no LLM, any policy other than `greedy`, and a run directory with no run id — and
        the one-way ratchet can move it to 0 mid-run. In each of those the run silently changed
        SELECTOR while `run_started` still pinned `card_driven_selection: true`, with no event saying
        so. `docs/guide/configuration.md` and `docs/guide/concepts.md` both promise the Card queue
        owns macro-action selection whenever this flag is true, with no mention of the depth; this
        predicate is what makes that true.

        The gate is exactly the flag `_select_actions` reads. Nothing else belongs here: minting a
        Card is main-task work in the capped `build` LLM lane, needs no isolated role pair, no
        calibration receipt and no second Developer — all of which are prefetch concerns.
        """
        return bool(getattr(self, "card_driven_selection", False))

    @in_llm_lane("build")
    def _stage_card_creates(self, actions: list[dict], state: RunState) -> list[str]:
        """Turn raw policy creates into durable, selection-ready Card receipts only.

        No ``node_building`` is written here.  A later fresh fold must select the Card, after which the
        isolated producer is gated by ``card_build_requested``.  Multi-seed drafts retain the existing
        shared-Researcher diversity pass; non-draft actions reuse the exact ordinary proposal helper.
        """
        raw = [dict(action) for action in actions
               if isinstance(action, dict) and META_CARD_ID not in action]
        if not raw:
            return []
        proposal_events = self.store.read_all()
        proposal_state = _fold(proposal_events)
        proposal_node_ceiling = self._node_id_ceiling(proposal_events, proposal_state)
        prepared: list[tuple[dict, Idea, str, int, list, dict]] = []
        dropped_batch: list[dict] = []
        try:
            if len(raw) > 1 and all(action.get("kind") == "draft" for action in raw):
                ideas, telemetry, dropped_batch = self._consume_batch_proposal(
                    proposal_state, len(raw))
                for offset, (action, idea, record) in enumerate(
                        zip(raw, ideas, telemetry)):
                    steering = ((record or {}).get("_steering_context", [])
                                if isinstance(record, dict) else [])
                    advisory_receipt = bounded_cross_run_advisory_receipt(
                        (record or {}).get("_cross_run_advisory_receipt", {})
                        if isinstance(record, dict) else {}
                    )
                    prepared.append((
                        action, idea, "researcher",
                        proposal_node_ceiling + offset, steering, advisory_receipt,
                    ))
            else:
                for offset, action in enumerate(raw):
                    source = "engine" if action.get("kind") == "merge" else "researcher"
                    idea = self._prepare_node_idea(
                        action,
                        proposal_state,
                        researcher=self.researcher,
                        prospective_node_id=proposal_node_ceiling + offset,
                        source=source,
                        proposal_events=proposal_events,
                    )
                    if idea is None:
                        continue
                    prepared.append((
                        action,
                        idea,
                        source,
                        proposal_node_ceiling + offset,
                        list(getattr(self.researcher, "_steering_context", []) or []),
                        bounded_cross_run_advisory_receipt(getattr(
                            self.researcher, "_cross_run_advisory_receipt", {}) or {}),
                    ))

            staged: list[str] = []
            for action, idea, source, at_node, steering, advisory_receipt in prepared:
                # The BATCH lane reaches here without passing `_prepare_node_idea`'s `_link` funnel
                # (`_consume_batch_proposal` hands its Ideas straight to the stager), so the proposal
                # circuit breaker is repeated for it. MAIN TASK: both callers of `_stage_card_creates`
                # — the create branch and `_run_card_session`'s raw lane — run there, so the pause is
                # appended immediately and the very next fold stops the run before another paid
                # proposal. Live: this is the lane that authored `/tmp/ll-s4b/run`'s poisoned Cards.
                if self._refuse_degraded_proposal(idea, main_task=True):
                    break
                card_id = self._stage_prepared_card(
                    action,
                    idea,
                    proposal_state=proposal_state,
                    proposal_node_ceiling=proposal_node_ceiling,
                    at_node=at_node,
                    source=source,
                    steering_context=steering,
                    cross_run_receipt=advisory_receipt,
                )
                if card_id is not None:
                    staged.append(card_id)

            # Preserve the existing audit treatment for batch proposals rejected before Node ownership.
            # Accepted staged Cards land first, so rejected receipts allocate fresh ids after them.
            self._record_dropped_batch_cards(dropped_batch)
            return staged
        finally:
            self._pending_batch_dropped = []
            self._pending_batch_telemetry = []
            self._pending_batch_novelty_gated = []
            # The single-action lane reaches the proposal circuit breaker through
            # `_prepare_node_idea`'s `_link`, which uses the WORKER discipline (it cannot know which
            # task it is on) and therefore only QUEUES the run-global pause. Both callers of this
            # method — the create branch and `_run_card_session`'s raw lane — are the MAIN task, so
            # this is where that queue becomes durable. Without it the pause is dropped on the next
            # loop turn's queue reset and the run keeps paying for proposals against a dead provider.
            if getattr(self, "_pending_create_pause", None):
                self._drain_create_pause()
            # Node-oriented telemetry cannot truthfully be emitted until a Node exists.  Clear the
            # primary pair so it cannot leak onto a later repair/legacy build; the staged Card already
            # owns its immutable proposal and steering receipts.
            self._discard_node_build_telemetry(
                researcher=self.researcher, developer=self.developer)

    @staticmethod
    def _card_claim_receipt_action(card) -> dict:
        """Rebuild the exact immutable action whose digest makes a native Card selectable."""
        return {
            "operator": card.operator,
            "params": dict(card.params or {}),
            "space": {key: list(values) for key, values in (card.space or {}).items()},
            "eval_profile": card.eval_profile,
            "eval_timeout": card.eval_timeout,
            "parent_id": card.parent_id,
            "parent_ids": list(card.parent_ids or []),
            "parent_generations": (
                dict(card.parent_generations) if isinstance(card.parent_generations, dict) else None
            ),
            "scored_against": card.scored_against,
            "scored_against_generation": card.scored_against_generation,
            "scored_against_empty": card.scored_against_empty,
            "footprint": normalize_researcher_footprint(card.footprint),
        }

    def _prepare_existing_card_claim(self, events, state: RunState, action: dict, card,
                                     node_id: int) -> Optional[_BuildReservation]:
        """Validate and reconstruct one Card claim against an already-fenced snapshot."""
        raw_card_id = action.get(META_CARD_ID)
        card_id = self._canonical_card_id(raw_card_id)
        if card_id is None or raw_card_id != card_id:
            return None
        if card.id != card_id or not card.selection_ready:
            return None

        expected_macro = projected_card_action(card)
        if expected_macro is None or expected_macro.get(META_CARD_ID) != card_id:
            return None
        if any(
                not isinstance(key, str)
                or (key not in expected_macro and not key.startswith("_"))
                for key in action):
            return None
        if any(action.get(key) != value for key, value in expected_macro.items()):
            return None

        # A modern selectable action always carries the complete generation fence, including an
        # explicit empty map for drafts. Rechecking it through the ordinary reservation validator
        # closes the score-to-claim race for resets, tombstones, aborts and parent replacement.
        if not isinstance(card.parent_generations, dict):
            return None
        claim_action = {**expected_macro, "parent_generations": card.parent_generations}
        parent_snapshot = self._build_parent_snapshot(state, claim_action)
        if parent_snapshot is None:
            return None
        kind, parents, parent_generations = parent_snapshot
        if parent_generations != card.parent_generations:
            return None

        receipt_action = self._card_claim_receipt_action(card)
        digest = card_action_digest(card.id, card.seed_statement, receipt_action)
        expected_receipt = card_ownership_receipt(card.id, card.seed_statement, receipt_action)
        if (digest is None or expected_receipt is None
                or digest != card.identity.action_digest):
            return None
        registrations = [
            event for event in events
            if event.type == EV_CARD_ADDED
            and self._canonical_card_id(event.data.get("id")) == card_id
        ]
        if (len(registrations) != 1
                or registrations[0].data.get("id") != card_id
                or registrations[0].data.get("statement") != card.seed_statement
                or registrations[0].data.get("ownership_receipt") != expected_receipt):
            return None

        try:
            calibration_concepts = ({
                "concept_mode": "full",
                "concepts": [
                    f"operator/{card.operator}",
                    "objective/quadratic",
                    "space/two-dimensional",
                ],
            } if self._speculation_gate_calibration else {})
            # THE rebuild — and the same one `_card_added_payload` proves the mint against, by
            # construction rather than by hand-syncing two copies of this constructor. `receipt_action`
            # is `_card_claim_receipt_action(card)`, i.e. exactly the action shape the mint digested.
            idea = self._rebuilt_claim_idea(
                card.id, card.seed_statement, receipt_action, card.rationale,
                concepts=calibration_concepts)
        except Exception:  # hostile/future Card data cannot escape the closed Idea schema
            return None
        rebuilt_action = self._card_action(
            idea, parents, parent_generations,
            card.scored_against, card.scored_against_generation,
            scored_against_empty=card.scored_against_empty,
        )
        if (self._card_statement(idea) != card.seed_statement
                or rebuilt_action != receipt_action):
            # PERMANENT, not transient — and the only refusal here that is. Every check above tests
            # this claim against a MOVING world (a reset, an abort, a replaced parent); this one tests
            # the Card against ITSELF, so it answers the same way on every future turn. The live
            # instance: `_clamp_fill` bounds-clamped a param that its own `space` grid also names, so
            # the durable action was not a FIXED POINT of `Idea`'s validators and rebuilding the Idea
            # from the Card produced a different `params`. Naming it is what turns the resulting
            # forever-refusal into a bounded retirement instead of a spin (`_note_card_claim_refusal`).
            return self._refuse_card_claim(
                f"{card_id}'s durable action cannot be rebuilt from its own receipt "
                f"(the Card and its ownership digest disagree) — it can never be claimed")
        return _BuildReservation(
            state, node_id, kind, parents, parent_generations, card_id, idea)

    def _refuse_card_claim(self, reason: str) -> None:
        """Record WHY the serial Card claim refused, then refuse (always ``None``).

        The claim has ~10 refusal exits and every one of them used to be an anonymous `return None`.
        The caller's only recourse was to re-select and try again next turn, which is right for the
        transient refusals (a lost tail CAS, a selection that moved under a concurrent control) and a
        SPIN for the permanent ones. The engine knew the difference and threw it away; this keeps it,
        so `_handle_create_actions` can retire a lane that will never be claimable and the terminal
        can name a cause the operator can act on instead of "no node was created".
        """
        self._card_claim_refusal = reason
        return None

    def _claim_existing_card_builds(
        self, actions: list[dict],
    ) -> Optional[list[_BuildReservation]]:
        """Atomically claim the complete Card lane selected from one fresh snapshot.

        A population policy may select several Cards at once. Claiming and building them one-by-one
        would make the first pending node engage the evaluate-all forced gate and invalidate its
        siblings. The whole lane is therefore revalidated under ``_id_lock`` and its ``node_building``
        owners are appended as one tail-CAS group before any slow Developer work begins.

        Every refusal path names itself through `_refuse_card_claim`; see there for why.
        """
        if not actions:
            return []
        self._card_claim_refusal = None
        with self._id_lock:
            events = self.store.read_all()
            state = _fold(events)
            self._refresh_speculation_budget(state, events=events)
            if self._node_reservation_slots_remaining(state, events=events) < len(actions):
                return self._refuse_card_claim(
                    "the node-reservation budget has no physical slot left for this lane")
            try:
                max_nodes = max(0, int(self.policy.max_nodes))
            except (TypeError, ValueError, OverflowError):
                return self._refuse_card_claim("the policy's node ceiling is not a usable integer")
            remaining = max_nodes - card_budget_used(state)
            if remaining < len(actions):
                return self._refuse_card_claim("the Card node budget is spent")

            requested_ids: list[str] = []
            for action in actions:
                raw_card_id = action.get(META_CARD_ID)
                card_id = self._canonical_card_id(raw_card_id)
                if card_id is None or raw_card_id != card_id or card_id in requested_ids:
                    return self._refuse_card_claim(
                        f"the selected lane names an unusable or duplicated Card id ({raw_card_id!r})")
                requested_ids.append(card_id)

            try:
                live = {card.id: card for card in eligible_cards(state, self.policy)}
                forced = forced_card_actions(state, self.policy, max_nodes)
                if forced is not None:
                    current_ids = [
                        candidate.get(META_CARD_ID) for candidate in forced
                        if isinstance(candidate, dict) and META_CARD_ID in candidate
                    ]
                else:
                    treatment = getattr(self, "_card_scoring", None)
                    current_ids = [
                        candidate.id for candidate in card_selection_set(
                            state, self.policy, max_nodes, scoring=treatment)
                    ]
            except Exception:  # policy/Card hooks must never weaken the ownership boundary
                return self._refuse_card_claim("the Card selector raised while revalidating the lane")
            if requested_ids != current_ids:
                return self._refuse_card_claim(
                    f"selection moved between scoring and claiming ({requested_ids} -> {current_ids})")

            first_node_id = self._node_id_ceiling(events, state)
            reservations: list[_BuildReservation] = []
            for offset, (action, card_id) in enumerate(zip(actions, requested_ids)):
                card = live.get(card_id)
                if card is None:
                    return self._refuse_card_claim(f"{card_id} is no longer a live selectable Card")
                reservation = self._prepare_existing_card_claim(
                    events, state, action, card, first_node_id + offset)
                if reservation is None:
                    if not self._card_claim_refusal:
                        self._card_claim_refusal = (
                            f"{card_id} failed claim revalidation against the current snapshot")
                    return None
                reservations.append(reservation)

            records = [
                (EV_NODE_BUILDING, {
                    "node_id": reservation.node_id,
                    "operator": reservation.kind,
                    "parent_ids": reservation.parent_ids,
                    "card_id": reservation.card_id,
                })
                for reservation in reservations
            ]
            try:
                self.store.append_many(
                    records, expected_last_seq=events[-1].seq if events else -1)
            except EventStoreConcurrencyError:
                return self._refuse_card_claim("the event-log tail moved during the claim (retryable)")
            self._card_claim_refusal = None
            return reservations

    def _claim_existing_card_build(self, action: dict):
        """Compatibility wrapper for callers that claim one selected Card."""
        reservations = self._claim_existing_card_builds([action])
        return reservations[0] if reservations else None

    # How many CONSECUTIVE turns the same serial Card lane may be refused before the engine retires
    # it. Not one: a refusal is legitimately transient (a lost tail CAS, a selection that moved under
    # a concurrent operator control), and giving up on the first would throw away real work items.
    # Small, though — the runaway cap is `max_nodes*3 + 50` turns, and burning all of them re-scoring
    # a lane that answers the same way every time is exactly the 74-turns-in-one-second spin. Three
    # rides out a CAS race and still bounds the stall at three turns instead of ~75.
    _CARD_CLAIM_RETIRE_AFTER = 3

    def _note_card_claim_refusal(self, card_ids: list[str]) -> bool:
        """Count one refusal of this exact lane; retire the lane once it is provably not transient.

        The anti-spin half of the `producer_failed` defect. A Card the isolated producer gave up on is
        barred from speculative re-election (`speculation.py::_election_excluded_card_ids`) and must be
        built by the ordinary serial Developer — but if the serial claim ALSO refuses, nothing removes
        the Card from selection, so the next turn re-selects it, re-refuses, and the loop free-spins
        until the no-mint runaway cap trips and reports "stuck: N action(s) planned … without creating
        a node". Measured live: 74 loop turns inside one second, a run that reached 2 of 8 nodes where
        the same command with `speculation_depth=0` reached 8 of 8.

        Retirement is a durable `card_auto_dropped` carrying the refusal reason, so the board loses the
        unbuildable work item, selection moves to the next Card, and the operator can read WHY in the
        log. Returns True when the lane was retired (the caller has made progress and must re-fold).
        """
        key = tuple(card_ids)
        if getattr(self, "_card_claim_refusal_lane", None) != key:
            self._card_claim_refusal_lane = key
            self._card_claim_refusal_turns = 0
        self._card_claim_refusal_turns += 1
        if self._card_claim_refusal_turns < self._CARD_CLAIM_RETIRE_AFTER:
            return False
        reason = (getattr(self, "_card_claim_refusal", None)
                  or "the serial Card claim refused it repeatedly")
        for card_id in card_ids:
            self._drop_card_once(
                card_id,
                reason=f"unclaimable after {self._card_claim_refusal_turns} turns: {reason}",
            )
        self._card_claim_refusal_lane = None
        self._card_claim_refusal_turns = 0
        return True

    def _create_stall_diagnosis(self, creates: list[dict], state: RunState) -> str:
        """Name what is actually blocking a create lane that plans work and mints nothing.

        The terminal used to say only "N action(s) planned for M consecutive loop turns without
        creating a node" — a SYMPTOM, and one the operator cannot act on, while the very same log's
        `budget.speculation` recorded `producer_failed: 1`. The engine knew the cause and published
        the symptom. This assembles the cause from the same folded state the budget summary reads.
        """
        notes: list[str] = []
        card_ids = [
            card_id for action in creates
            if (card_id := self._canonical_card_id(action.get(META_CARD_ID))) is not None
        ]
        gave_up = [card_id for card_id in card_ids
                   if card_id in self._producer_failed_card_ids(state)]
        if gave_up:
            notes.append(
                f"the isolated speculative producer gave up on {', '.join(gave_up)} "
                "(card_build_done:producer_failed), so only the serial Developer can build it")
        if (refusal := getattr(self, "_card_claim_refusal", None)):
            notes.append(f"the serial Card claim refused: {refusal}")
        # `_card_inventory_enabled`, not `_speculation_enabled`: the raw-proposal staging lane runs
        # for any Card-driven run since 2026-08-07, so a prefetch-off run that stalls there must get
        # the same named cause instead of an empty reason string.
        if not card_ids and self._card_inventory_enabled():
            notes.append("the raw-proposal staging lane produced no durable Card")
        return "; ".join(notes)

    def _drop_card_once(self, card_id: Optional[str], *, reason: str,
                        dropped_by: str = "engine") -> None:
        if not card_id:
            return
        # This helper is called both inside and outside `_id_lock`, so nesting that non-reentrant lock is
        # unsafe. Use the EventStore's atomic tail CAS instead: concurrent callers either observe the
        # first drop or lose the CAS and retry against its prefix.
        def _plan(events, tail) -> None:
            if any(
                    event.type in {EV_CARD_AUTO_DROPPED, EV_CARD_DROPPED}
                    and self._canonical_card_id(event.data.get("id")) == card_id
                    for event in events):
                return None
            self.store.append(EV_CARD_AUTO_DROPPED, {
                "id": card_id,
                "reason": reason,
                "dropped_by": dropped_by,
            }, expected_last_seq=tail)
            return None

        def _exhausted():
            # Unlike the receipt appends above, a failed drop LEAKS: the caller has already given up
            # on this Card, so returning quietly would leave it live on the board with no owner. Raise.
            raise RuntimeError(
                "could not append an idempotent card drop after concurrent log movement")

        retry_tail_cas(self.store, _plan, on_exhaust=_exhausted)

    def _record_node_less_card(self, idea: Idea, *, reason: str,
                               steering_context=(), source: str = "researcher") -> Optional[str]:
        """Mint and immediately close one rejected proposal with no Node owner.

        Unlike ordinary reservation this deliberately permits an exact live sibling: the point is to
        retain the discarded proposal and its reason without confusing it with the accepted work item.
        Accepted batch Cards are committed first, so this fresh id cannot invalidate their preplanned ids.
        """
        with self._id_lock:
            events = self.store.read_all()
            state = _fold(events)
            # Same funnel rule as `_plan_native_card`: this writer builds a real action and a real
            # receipt (it pops the receipt only at the end, so the closed Card cannot be resurrected as
            # executable work), so its Idea must reach `_card_added_payload` as a fixed point too or
            # the round-trip proof there would silently cost every rejected proposal its audit Card.
            clean = self._fixed_point_idea(idea).model_copy(deep=True, update={"card_id": None})
            statement = self._card_statement(clean)
            score_snapshot = self._card_score_snapshot(state, state.best_node_id)
            bounded_steering = normalize_steering_context(steering_context)
            if statement is None or score_snapshot is None or bounded_steering is None:
                return None
            score_id, score_generation, score_empty = score_snapshot
            card_id = self._next_available_card_id(events, state)
            reserved = clean.model_copy(deep=True, update={"card_id": card_id})
            action = self._card_action(
                reserved, [], {}, score_id, score_generation,
                scored_against_empty=score_empty,
            )
            try:
                payload = self._card_added_payload(
                    card_id, statement, action, reserved, source=source,
                    at_node=self._node_id_ceiling(events, state),
                    steering_context=bounded_steering,
                )
            except (TypeError, ValueError, OverflowError):
                return None
            # This Card is rejected before it can ever own a Node. If the process dies after the first
            # append, an otherwise-valid receipt would resurrect it as a selectable proposal with no
            # recovery marker. Reserve the id with an intrinsically non-executable registration, then
            # append the normal terminal override. The full two-event prefix remains visible/auditable.
            payload.pop("ownership_receipt", None)
            self.store.append(EV_CARD_ADDED, payload)
            self.store.append(EV_CARD_AUTO_DROPPED, {
                "id": card_id, "reason": reason, "dropped_by": "engine",
            })
            return card_id

    def _mirror_hypothesis_card_merges(self, state: RunState) -> RunState:
        """Main-task durable Card receipts for background-safe Hypothesis consolidations.

        The LLM consolidation step may append ``hypothesis_merged`` from the research-overlap worker,
        while every Card lifecycle event is main-task-only. Reconcile by source event seq at the next
        decision boundary. Replay already understands statement-hash aliases, so this is additive audit
        durability and idempotent across resume; no model call or selection decision occurs here.
        """
        with self._id_lock:
            events = self.store.read_all()
            mirrored = {
                event.data.get("source_event_seq")
                for event in events if event.type == EV_CARD_MERGED
                if type(event.data.get("source_event_seq")) is int
            }
            wrote = False
            for event in events:
                if event.type != EV_HYPOTHESIS_MERGED or event.seq in mirrored:
                    continue
                canonical = self._canonical_card_id(event.data.get("canonical"))
                raw_aliases = event.data.get("aliases")
                if canonical is None or not isinstance(raw_aliases, list):
                    continue
                aliases: list[str] = []
                for raw_alias in raw_aliases[:256]:
                    alias = self._canonical_card_id(raw_alias)
                    if alias is not None and alias != canonical and alias not in aliases:
                        aliases.append(alias)
                if not aliases:
                    continue
                payload = {
                    "canonical": canonical,
                    "aliases": aliases,
                    "source_event_seq": event.seq,
                    "merged_by": "engine",
                }
                statement = event.data.get("statement")
                if (isinstance(statement, str) and statement.strip()
                        and len(statement.strip()) <= 2_048 and statement.strip().isprintable()):
                    payload["statement"] = statement.strip()
                self.store.append(EV_CARD_MERGED, payload)
                wrote = True
        return _fold(self.store.read_all()) if wrote else state
