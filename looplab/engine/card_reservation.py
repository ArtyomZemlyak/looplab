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

import collections
import functools
import logging

import anyio

import hashlib
from collections.abc import Collection
from typing import NamedTuple, Optional

import orjson

from looplab.agents.roles import is_researcher_fallback
from looplab.core.advisory_payloads import bounded_cross_run_advisory_receipt
from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import (CARD_IDEA_CONCEPT_FIELDS, CARD_STATEMENT_MAX_CHARS,
                                 Idea, NodeStatus, RunState,
                                 card_action_digest,
                                 card_ownership_receipt, durable_idea_payload,
                                 hypothesis_statement_digest, idea_proposal_ref,
                                 normalize_researcher_footprint)
from looplab.engine.proposal_cues import normalize_steering_context
from looplab.events.eventstore import EventStoreConcurrencyError, retry_tail_cas
from looplab.events.card_ledger import _drop_author
from looplab.events.types import (EV_CARD_ADDED, EV_CARD_AUTO_DROPPED, EV_CARD_DROPPED, EV_CARD_REOPENED,
                                  EV_CARD_MERGED, EV_HYPOTHESIS_MERGED, EV_NODE_BUILDING,
                                  PROGRESS_STAGE_BUILD,
                                  EV_NODE_CREATED, EV_NOVELTY_REJECTED)

_LOG = logging.getLogger(__name__)

# WHICH FENCE REFUSED A STAGED PROPOSAL, named rather than collapsed into one silent `None`.
#
# `_stage_prepared_card._plan` compares a FRESH fold against the snapshot the proposal was authored
# against, and any of eight conjuncts refuses. THE REFUSAL IS THE DESIGNED ANSWER to moved authority
# — a proposal authored against an old search state must never be relabelled as current work — and
# nothing here changes it. What changes is that the loss was invisible: every refusal returned a
# bare `None` and the caller simply dropped it.
#
# THE BATCH LANE IS WHY IT MATTERS NOW. Since the paid batch propose moved off the event-loop thread
# (`56764cbd`) there is a minutes-long SUSPENSION between the authority fold and the staging loop,
# so one best-IMPROVING eval terminal, or one `research_completed`/`hint`/strategy row — all
# BACKGROUND_APPENDABLE, all hashed by `_proposal_cue_fence` — refuses EVERY idea of the batch at
# once. Pre-offload the loop was frozen and no fence input could move mid-propose, so this was
# unreachable; the occupancy-paced create the batch branch's own comment advertises makes it
# routine. N paid ideas at once, where the per-action lane risks one.
#
# A typo'd slug does not fail: it lands on an in-process seam a caller reads and on a log line an
# operator greps, so `tests/test_card_stage_refusals.py` re-derives this set from `_plan`'s own AST
# in BOTH directions — the way `CARD_BUILD_SKIP_REASONS` is guarded one module over.
CARD_STAGE_REFUSALS = (
    "authority_seq_moved",   # a non-diagnostic row landed inside the proposal's equality window
    "search_epoch_moved",    # the search epoch rotated under the proposal
    "run_stopping",          # paused / finished / stop requested
    "best_moved",            # a best-IMPROVING terminal landed — the anchor this was scored against
    "cues_moved",            # a steering cue changed (research memo, hint, strategy)
    "node_ceiling_moved",    # the future node-slot ceiling advanced
    "parent_moved",          # the parent snapshot (kind/parents/generations) changed
    "score_moved",           # the score snapshot of the anchor changed
)

from looplab.search.card_selection import (META_CARD_ID, SpeculativeSelectionContext,
                                           card_action as projected_card_action,
                                           card_budget_used, card_selection_set, eligible_cards,
                                           forced_card_actions,
                                           speculative_card_selection_set)


# The additive `node_building` field an ATTACH claim carries, and the whole reason it exists: a
# reservation that MINTED its card may close by dropping it, a reservation that merely joined
# somebody else's may not. Written by `_reserve_node_build`, read by `_reservation_minted_card`.
# Deliberately spelled the "unusual" way round (present only on an attach) because the fold's
# `_on_node_building` copies a FIXED key set into `RunState.buildings` — an absent flag therefore
# means "an ordinary mint/reuse claim", which is what every log written before 2026-08-12 is.
CLAIM_ATTACHED_FIELD = "card_attached"


def scored_anchor(state) -> tuple[Optional[int], Optional[int]]:
    """The `(best_node_id, attempt)` a proposal is scored under, read from ONE fold.

    Both halves of the card's score fence must come from the same `state` object. They used to be
    read from two: the caller passed `scored_against=state.best_node_id` from its own fold, and
    `_card_score_snapshot` then took `node.attempt` from the FRESH fold `_reserve_node_build._plan`
    takes under the CAS. That was unreachable-by-construction while the propose ran on the loop
    thread — the loop was frozen, so the two folds were the same log — and `_await_batch_proposal`
    offloading the paid propose to a worker opened the window to the propose's whole duration.

    The pair matters more than either half. `cards.py::card_score_fence_state` answers `stale`
    exactly on `scored_against_generation != anchor_attempt`, so an anchor that RE-RAN mid-propose
    got the OLD id beside its NEW attempt and read `current` — the one case the generation is in
    the receipt to catch. The stale id alone is not a defect and must not be "fixed": the ladder
    narrowed champion-equality away on 2026-08-13, deliberately, and `card_selection` asks only
    that the anchor be live. Both readers want the champion the proposal was scored under.

    `(None, None)` on an empty board is the honest answer, not a refusal — `_card_score_snapshot`
    turns a `None` id into the `scored_against_empty` triple.
    """
    node_id = state.best_node_id
    if node_id is None:
        return None, None
    node = state.nodes.get(node_id)
    return node_id, (None if node is None else node.attempt)


def _fold(events):
    """Fold THROUGH the orchestrator module attribute — see the module docstring.

    The deferred import is the point: binding `orchestrator.fold` at import time would snapshot the
    real function and make `monkeypatch.setattr(orch, "fold", …)` a silent no-op for this cluster.
    """
    from looplab.engine import orchestrator
    return orchestrator.fold(events)


# The hypothesis carried on a DISCARD receipt, bounded. A discarded proposal's whole value to a
# reader is "what was the idea, so I can tell whether losing it mattered" — so the field is the
# hypothesis and not the rationale, which is the long half and the one a reader can re-derive from
# the card that won. 400 chars matches the bound `replay._on_hypothesis_added` already applies to a
# `rationale`, so an audit row can never grow past what the fold is willing to keep beside it.
#
# Never raises: a receipt may not cost a build its refusal. Anything unreadable becomes "" and the
# row still carries the disposition, which is the part that makes the discard countable.
_DISCARDED_PROPOSAL_TEXT_MAX = 400


def _discarded_proposal_text(idea) -> str:
    try:
        text = getattr(idea, "hypothesis", "") or ""
        if not isinstance(text, str):
            return ""
        text = text.strip()
    except Exception:  # noqa: BLE001 — a receipt must never raise into the reservation path
        return ""
    return text[:_DISCARDED_PROPOSAL_TEXT_MAX]


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
    """Pure result of resolving one exact native Card identity against the journal.

    ``attach`` is the ONE disposition that names an existing Card without an exact action match:
    a repair re-attempt of the SAME research question becomes another node under the card that
    already asks it, instead of a byte-identical twin work item (see ``_retry_attach_card``).
    Like ``reuse`` it carries no payload — nothing is minted, only the ``node_building`` claim.

    That "nothing is minted" is exactly why the claim it commits carries
    ``CLAIM_ATTACHED_FIELD``: every bare-reservation close path drops the card its marker names
    (``orchestrator.py::_fail_reserved_build``), and for an attach that card belongs to the PARENT.
    See ``_reservation_minted_card``.
    """

    disposition: str  # mint | reuse | attach | duplicate | invalid
    card_id: Optional[str]
    idea: Optional[Idea]
    payload: Optional[dict]


class CardReservationMixin:
    def _record_dropped_batch_cards(self, dropped) -> None:
        """Give every rejected proposal in a batch its node-less closed Card.

        The reason string is TRUNCATED and defaulted here rather than at three call sites: a card
        whose reason silently became the empty string reads on the board as a drop with no cause.

        A DEGRADED FALLBACK is skipped, because it is not a rejected proposal — it is the ABSENCE of
        one (`agents/roles.py::is_researcher_fallback`; the whole rule is in
        `tests/test_proposal_provider_crash.py`). Recording it here would put the transport error on
        the durable Card board as a hypothesis STATEMENT, which is the exact poisoning
        `_refuse_degraded_proposal` exists to stop — five such rows per turn against a dead provider,
        measured. It was unreachable while the only caller staged one action at a time (a one-action
        lane never enters `_consume_batch_proposal`); widening the prefetch-off lane to the whole
        batch on 2026-08-07 made it reachable, so the guard belongs on the writer, not on the caller.
        """
        for drop in dropped or []:
            if isinstance(drop, dict) and isinstance(drop.get("idea"), Idea):
                if is_researcher_fallback(drop["idea"]):
                    continue
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
        if (not statement or len(statement) > CARD_STATEMENT_MAX_CHARS
                or not statement.isprintable()
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
    def _authored_card_concepts(idea: Idea) -> Optional[dict]:
        """The proposal-time concept ENVELOPE a `card_added` row carries, or None for "no claim".

        The Researcher authors `idea.concepts` and the whole Part IV/V subsystem — the run's concept
        tree, the board's `Card.concept_tags`, the cross-run shelf, `concept_run_base`'s seed — reads
        it from `node_created`. The Card lane used to lose it: `_card_added_payload`'s idea block is
        deliberately narrow (a durable action the ownership digest covers), and `_rebuilt_claim_idea`
        reconstructs the executed Idea from exactly that block, so a node BUILT FROM A CARD was born
        with no concepts at all. Measured across the run corpus 2026-08-12, and the split is total:

          | run                        | nodes | built from a Card | node_created carrying concepts |
          | rubertlite-dr-unified-v4   |    11 |                 0 |                             11 |
          | rubert-dr-0807             |    14 |                14 |                              0 |
          | rubertlite-dr-unified-v2   |     7 |                 7 |                              0 |
          | rubertlite-dr-unified-v5   |     1 |                 1 |                              0 |

        v5 node 0's Researcher emitted `concept_mode="full"` with three ids (`spans.jsonl`, the
        `emit` tool call); the `card_added` row it minted recorded five idea keys and none of them
        was `concepts`.

        EVERY PROPOSAL MODE, and what makes that possible is that the envelope travels UNRESOLVED.
        `2acdb825` carried a `full` membership only, because `concept_mode`/`concepts_added`/
        `concepts_removed` were not in `card_ledger.py::_CARD_ADDED_ACTION_FIELDS` and writing them
        made replay read the whole action as a lossy future schema — the Card stopped being
        `selection_ready`, which is worse than the missing tags. That was a missing ROW in an
        allow-list, not a property of the design: the four keys are now one shared tuple
        (`core/cards.py::CARD_IDEA_CONCEPT_FIELDS`) that this writer emits and the ledger decodes and
        admits, so the two cannot drift apart again.

        WHAT A DELTA IS A DELTA AGAINST, and why it is NOT resolved here. `delta` states a CHANGE
        against an inheritance base: the run base (`EV_RUN_CONCEPTS`) at a root, else the union of
        the node's parents' effective memberships, resolved topologically over the whole folded DAG
        by `replay.py::_materialize_concept_deltas`. That base exists only in folded state, and this
        function runs at MINT time — before the node exists, possibly many turns before the claim.
        Resolving it here and persisting a `full` set was the obvious shape and it does not survive:

          * the resolution would depend on a parent snapshot the immutable Card does not bind, while
            the base itself keeps moving — the classifier cadence re-tags parents, an operator can
            edit tags, a consolidation renames ids. The Card lane would then answer a question
            differently from the non-Card lane for the same authored delta on the same run;
          * a mint-time resolution is not stable across a restart, and `_card_event_matches`
            byte-compares the whole writer payload, so a crash-prefix orphan would stop being
            reusable — the duplicate-work-item shape the ledger exists to prevent;
          * a parent's own membership may be an unresolved delta with an `unavailable` receipt, and
            the only `full` set expressible for that is `[]` — which replay reads as an authoritative
            KNOWN-EMPTY membership. That is a lie in place of an honest receipt;
          * a root card may legitimately be minted before `EV_RUN_CONCEPTS` exists (the fold is
            order-tolerant and `_materialize_concept_deltas` fails closed on it); a mint-time
            resolution has nothing to fail closed with.

        So the delta stays a delta, and the claim rebuilds the SAME envelope onto the executed Idea.
        The node then folds exactly as an unmediated Researcher proposal does, through one resolver,
        at fold time — which is also what makes it replay-identical: nothing about the membership is
        recomputed from live state at claim time, so the durable log folds to the same node concepts
        and the same Card on every future replay. The node's parents are the Card's parents
        (`_prepare_existing_card_claim` re-fences `parent_generations` against the Card), so the base
        the fold resolves against is the lineage the proposal was authored for.

        The four spellings this returns, and why `full` is asymmetric:
          * `delta`  -> `{concept_mode, concepts_added, concepts_removed}` — the mode is REQUIRED,
            because an explicit zero delta (both lists empty, "inherit unchanged") is a real claim
            and is indistinguishable from an absent envelope without it;
          * `full` with a non-empty membership -> `{concepts}` and NO mode, byte-identical to what
            `2acdb825` already writes. Not stylistic: `_card_event_matches` compares writer bytes, so
            adding a redundant key here would decline reuse of every crash-prefix orphan minted by
            the current writer. `_claim_concept_envelope` stamps the mode back on;
          * `full` with an EMPTY membership -> `{concept_mode, concepts: []}`, an authoritative
            known-empty set. It used to record nothing, which replay reads as ABSENT — a different
            fact;
          * an absent mode (legacy/mechanical producers) -> `{concepts}` when non-empty, else None.

        Returning the RAW lists is deliberate: the fold bounds them through
        `bounded_raw_concept_values` and stamps its own overflow/invalid flags, so an over-long or
        malformed membership records as an honestly incomplete `CardConceptSource` instead of a
        silently truncated exact one.
        """
        mode = getattr(idea, "concept_mode", None)
        if mode == "delta":
            return {
                "concept_mode": "delta",
                "concepts_added": [str(c) for c in (getattr(idea, "concepts_added", None) or [])],
                "concepts_removed": [
                    str(c) for c in (getattr(idea, "concepts_removed", None) or [])],
            }
        concepts = [str(c) for c in (getattr(idea, "concepts", None) or [])]
        if concepts:
            # No `concept_mode` here, and NOT a tidiness choice — see the docstring: this is the one
            # spelling a live writer already emits, `_card_event_matches` compares writer BYTES, and a
            # redundant key would decline reuse of every crash-prefix orphan minted since 2026-08-12.
            return {"concepts": concepts}
        if mode == "full":
            return {"concept_mode": "full", "concepts": []}
        return None

    @classmethod
    def _claim_concept_envelope(cls, recorded_idea) -> dict:
        """The `Idea` concept kwargs a Card claim rebuilds with, from the durable `card_added` block.

        The argument is the RECORDED idea block (or the fragment `_authored_card_concepts` just
        produced for it — the fragment is that block's concept subset, so one function serves the
        mint's round-trip proof and the real claim). Every key is untrusted durable input: a
        malformed operand yields no envelope at all rather than a half-read one, and `Idea`'s own
        validators bound and drop whatever survives.

        `concept_mode` is what makes `replay.py::_fold_node_concept_envelope` treat the result as
        authoritative and stamp `NODE_CONCEPT_PROVENANCE_AUTHORED`: `full` as an exact replacement,
        `delta` as an operand pair for the fold's materialization post-pass. A bare `concepts` list
        with no mode folds as `full` today too, but only through a second, weaker branch of that
        gate, so the mode is stamped explicitly here.
        """
        if not isinstance(recorded_idea, dict):
            return {}
        mode = recorded_idea.get("concept_mode")
        if mode == "delta":
            added = recorded_idea.get("concepts_added")
            removed = recorded_idea.get("concepts_removed")
            if not isinstance(added, list) or not isinstance(removed, list):
                # A delta with no operands is not a zero delta — it is a row this reader cannot
                # honestly execute. Fall through to no envelope, i.e. an ABSENT membership.
                return {}
            return {"concept_mode": "delta",
                    "concepts_added": [str(c) for c in added],
                    "concepts_removed": [str(c) for c in removed]}
        concepts = recorded_idea.get("concepts")
        if not isinstance(concepts, list) or (not concepts and mode != "full"):
            return {}
        return {"concept_mode": "full", "concepts": [str(c) for c in concepts]}

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

        # The membership the durable row will carry, resolved ONCE and handed to both halves of the
        # round trip below, exactly as `action` already is. Proving the mint against a rebuild that
        # skipped the concept envelope is what let the claim quietly execute a different Idea.
        card_concepts = cls._authored_card_concepts(idea)
        rebuilt = cls._rebuilt_claim_idea(
            card_id, statement, action, rationale,
            concepts=cls._claim_concept_envelope(card_concepts))
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
            # The concept envelope is the ONE exception, and it is not one invented at this call site:
            # `CARD_ACTION_DIGEST_V2_FIELDS` excludes every member of `CARD_IDEA_CONCEPT_FIELDS` (so
            # the receipt and every already-minted Card's digest are byte-identical either way), while
            # `card_ledger.py` decodes and admits exactly that tuple and `_card_added_snapshot` turns a
            # FULL membership into `Card.concept_tags` + a `kind="card_added"` `CardConceptSource`. A
            # delta contributes no membership here by design — it is resolved for the NODE, at fold
            # time, against a base only folded state has. See `_authored_card_concepts`.
            "idea": {
                "operator": action["operator"],
                "params": action["params"],
                "space": action["space"],
                "eval_profile": action["eval_profile"],
                "eval_timeout": action["eval_timeout"],
                **(card_concepts or {}),
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
            # The research DIRECTION this work item serves — the ONE card->card edge that is not a
            # retry. Emitted only when the Researcher named one, so a run whose proposals name no
            # direction writes byte-identical payloads to the ones already on disk (which is also
            # what keeps `_card_event_matches` able to recognise a pre-upgrade crash-prefix mint).
            # Deliberately OUTSIDE `action` and therefore outside every digest: filing an experiment
            # under a direction must not change its executable identity, or two proposals that are
            # the same experiment would stop deduping the moment one of them named its parent.
            **({"parent_card_id": idea.parent_card_id} if idea.parent_card_id else {}),
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
        if data == expected:
            return True
        # …and the one BACKWARD tolerance, which is not the same rule as the forward one below.
        # `_authored_card_concepts` started writing a concept envelope into the mint payload on
        # 2026-08-12 (`concepts`) and widened it to every proposal mode after. An orphan `card_added`
        # written by a PRE-upgrade writer therefore has NO envelope at all, and after an upgrade
        # nothing could ever reuse it again: a run killed between `card_added` and its claim came
        # back, declined its own crash-prefix row, minted a SECOND card for the same proposal and
        # left the orphan selectable — i.e. the upgrade reintroduced the exact duplicate-work-item
        # shape the Card ledger exists to prevent.
        # Admitting it is provably identity-preserving rather than merely convenient:
        # `CARD_ACTION_DIGEST_V2_FIELDS` excludes every `CARD_IDEA_CONCEPT_FIELDS` member, so the
        # `ownership_receipt` compared above is BYTE-IDENTICAL with or without them; `proposal_ref`
        # (which DOES digest all four) is compared and still pins this proposal's authored envelope
        # exactly, so a row that agrees here agrees about the membership too; and the reuse re-claims
        # through `_claim_concept_envelope`, which reads the RECORDED row (absent -> no membership)
        # rather than this proposal's. Narrow on purpose: only a WHOLLY absent envelope, only when
        # everything else is already exactly equal — a row whose envelope DISAGREES is a different
        # author's claim and still declines.
        expected_idea = expected.get("idea")
        recorded_idea = data.get("idea")
        if (isinstance(expected_idea, dict) and isinstance(recorded_idea, dict)
                and any(key in expected_idea for key in CARD_IDEA_CONCEPT_FIELDS)
                and not any(key in recorded_idea for key in CARD_IDEA_CONCEPT_FIELDS)):
            pre_upgrade = dict(expected)
            pre_upgrade["idea"] = {key: value for key, value in expected_idea.items()
                                   if key not in CARD_IDEA_CONCEPT_FIELDS}
            if data == pre_upgrade:
                return True
        # This is intentionally a writer-prefix matcher, not a loose semantic comparison. A future
        # additive mint field must make an old writer decline reuse until that field is reviewed.
        return False

    @staticmethod
    def _card_score_snapshot(
            state: RunState,
            requested: Optional[int],
            requested_attempt: Optional[int] = None,
            ) -> Optional[tuple[Optional[int], Optional[int], bool]]:
        """Identity of the node a card is scored against: `(id, attempt, empty)`, or None to REFUSE.

        The two falsy-looking outcomes are different answers and both are load-bearing. A bare
        ``None`` means the request is not scorable (out-of-range id, or a tombstoned/aborted node) —
        callers must abandon the reservation. The ``(None, None, True)`` triple means there is
        legitimately nothing to score against yet (no best node); that is a valid snapshot and it
        compares equal across two folds, which is what the pre-launch freshness fence needs.
        Every caller therefore checks ``is None`` BEFORE unpacking.

        ``requested_attempt`` EXISTS BECAUSE THE ID AND THE ATTEMPT MUST COME FROM ONE FOLD. This
        runs inside `_reserve_node_build._plan`, under a fresh `_fold(events)`, while `requested`
        comes from whatever the caller folded — and since the batch propose was offloaded to a
        thread, the loop keeps running and those two folds can be minutes apart. Reading the
        attempt from the fresh state while taking the id from the old one mints a pair the
        proposal was never scored against: if the anchor RE-RAN mid-propose, the receipt records
        the new attempt beside the old id, and `cards.py::card_score_fence_state` compares exactly
        that field to the live attempt — so the card reads ``current`` precisely when the metric it
        was scored against no longer exists. That comparison is the whole reason the generation is
        in the receipt ("the metric the proposal was scored against no longer exists even though
        the id does"). Callers that name an anchor therefore name its attempt too, and the fence
        can then honestly answer ``stale``. The STALE ID ITSELF IS NOT THE DEFECT: the freshness
        ladder narrowed away champion-equality on 2026-08-13 on purpose, and `card_selection`
        only checks the anchor is live — both want the champion the proposal was scored under.
        """
        score_id = state.best_node_id if requested is None else requested
        if score_id is None:
            return None, None, True
        if type(score_id) is not int or not 0 <= score_id <= (1 << 31) - 1:
            return None
        node = state.nodes.get(score_id)
        if node is None or node.tombstoned or score_id in state.aborted_nodes:
            return None
        attempt = node.attempt if requested_attempt is None else requested_attempt
        return score_id, attempt, False

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
    def _retry_attach_card(cls, state: RunState, idea: Idea, statement: str, parents: list[int],
                           *, superseded_card_id: Optional[str] = None,
                           excluded=()) -> Optional[str]:
        """The EXISTING Card a repair re-attempt belongs under, or None to mint a fresh work item.

        THE DEFECT THIS CLOSES, measured in `runs/rubertlite-dr-unified-v5`: node 0 was built for
        card-0 and failed (`no_metric`); the policy then planned `{"kind": "debug", "parent_id": 0}`;
        `orchestrator.py::_prepare_node_idea` answered it with the PARENT'S OWN IDEA, verbatim, with
        only `operator` flipped to "debug" — no Researcher call at all (the run's `spans.jsonl` has
        three `propose` spans, one per draft, and none for this) — and `_plan_native_card` saw a
        different action digest and minted card-3 with a statement BYTE-IDENTICAL to card-0's. Two
        rows, one research question. `runs/rubert-dr-0807` is the same defect a run earlier.

        `belief_id`/`retry_of` (see `core/cards.py`) made that visible on the board and stopped
        there, deliberately: the action digest must keep binding the executable action EXACTLY, and a
        debug build genuinely IS a different action. What that reasoning missed is that a Card is a
        WORK ITEM carrying several NODES — the card/node split is exactly the room a re-attempt
        needs — so the second row was never required to record the second action. The node's own
        `idea` records it; the Card records the question. So the repair now claims card-0 and the
        board reads "card-0: two attempts", which is what the operator asked to see.

        Fail-closed and NARROW. Only `debug` attaches: `improve`/`merge` also name parent nodes but
        propose a NEW point in the space, and folding those in would claim every child re-runs its
        parent's question. Only ONE parent, matching the shape `_card_action_has_live_anchors` admits
        for `debug`. The owner is resolved through the parent NODE ROW's own `idea.card_id` — its
        work item, never `Card.evidence`, which can name a node attached by the legacy statement-hash
        join — and only a live, singly-registered NATIVE card qualifies. Finally the seed statements
        must be the SAME BELIEF: `Card.belief_id` is the digest the fold publishes
        (`card_ledger.py::_apply_card_belief_lineage`), read here rather than re-derived so the mint
        and the board provably group on one spelling. That last check is what keeps this from
        collapsing a genuine re-scope: a repair whose statement was rewritten is a different question
        and still mints. It is also why "same wording" alone is never enough to attach anything else —
        the toy adapter's "random seed point" names three DIFFERENT param points in
        `runs/spec-live-0804`, and only the retry EDGE distinguishes them.

        A FAILED LEAF, and both halves of that are checked. The first version checked neither, so an
        `improve`-shaped `debug` planned against a node still PENDING — or against a card whose
        second node was mid-eval — attached anyway, and the board reported two live attempts at one
        question with no way to tell which one the operator was watching. A repair exists to retry
        work that ENDED: the parent must be terminally `failed` (never tombstoned, never aborted),
        and nothing else under the owner card may still be in flight — no pending node of its own,
        no live `node_building` marker naming it. Both are read off the SAME folded snapshot the
        reservation commits against, so the window is the tail CAS and nothing wider.
        """
        # UNREACHABLE SINCE 2026-08-13 (F5), and kept exactly as it is rather than deleted. `debug`
        # is the only operator that attaches, and nothing mints a `debug` Idea any more — the Debug
        # node was removed, so this returns `None` on the first line for every live call. It stays
        # because it is the FAIL-CLOSED half of a rule that already cost two byte-identical twin
        # Cards (card-0/card-3 in rubertlite-dr-unified-v5, card-0/card-1 in rubert-dr-0807): if a
        # retry operator is ever reintroduced under a different name, the right outcome is that it
        # lands on this gate rather than on the absence of one. Deleting it would also be a live risk
        # for no gain — `_reserve_node_build` has five callers and only one may attach at all.
        if idea.operator != "debug" or len(parents) != 1:
            return None
        parent = state.nodes.get(parents[0])
        if (parent is None or parent.status is not NodeStatus.failed
                or parent.tombstoned or parent.id in state.aborted_nodes):
            return None
        parent_idea = getattr(parent, "idea", None)
        owner_id = cls._canonical_card_id(getattr(parent_idea, "card_id", None))
        if owner_id is None or owner_id == superseded_card_id:
            return None
        if any(owner_id == cls._canonical_card_id(value) for value in excluded):
            return None
        owner = state.cards.get(owner_id)
        # `state.cards` is keyed canonically, so a merged-away alias simply is not here; the
        # `merged_into`/`merged_work_items` checks cover a lingering row and a surviving canonical
        # whose own work items were consolidated. Attaching into either would put a fresh node under
        # an identity consolidation already closed.
        if owner is None or owner.id != owner_id or owner.merged_into:
            return None
        if owner.identity.kind != "native" or not owner.identity.receipt_valid:
            return None
        if "merged_work_items" in owner.selection_blockers:
            return None
        if (owner.status in {"dropped", "gated"} or owner.verdict == "abandoned"
                or owner.dropped_reason is not None):
            return None
        if not statement or owner.belief_id != hypothesis_statement_digest(statement):
            return None
        # …and the LEAF half. A card whose question is being attempted RIGHT NOW does not need a
        # second simultaneous attempt; joining one would also make `_fail_reserved_build`'s
        # "is this card mine?" question ambiguous for both reservations at once.
        for node in state.nodes.values():
            if node.idea is None or cls._canonical_card_id(node.idea.card_id) != owner_id:
                continue
            if node.tombstoned or node.id in state.aborted_nodes:
                continue
            if node.status is NodeStatus.pending:
                return None
        for marker in (getattr(state, "buildings", None) or {}).values():
            if (isinstance(marker, dict)
                    and cls._canonical_card_id(marker.get("card_id")) == owner_id):
                return None
        return owner_id

    @classmethod
    def _reservation_minted_card(cls, events, node_id: int, card_id) -> bool:
        """May the reservation for ``node_id`` close by DROPPING ``card_id``? Only if it minted it.

        THE DEFECT THIS CLOSES, and it is strictly worse than the twin it replaced. A bare
        `node_building` reservation records the card it claimed, and every close path hands that
        recorded id to `orchestrator.py::_fail_reserved_build` with `drop_card=True` — because until
        the `attach` disposition existed, a claim's card was ALWAYS one the same reservation had just
        minted. An attach breaks that assumption at the one site that commits it: its marker names
        the PARENT's card. So a single SIGKILL between `node_building` and `node_created` made
        `_recover_interrupted_builds` classify the survivor as "a propose-reset owning a newly minted
        Card" (`node is None`) and administratively drop card-0 — the card carrying the parent node's
        own evidence — with `reason="build_interrupted"`. Driven on a copy of a real run: card-0 went
        `building/evidence=[0]` -> `dropped/actionable=False`, and the retry then minted the twin the
        attach existed to prevent.

        Two independent proofs, and the second is why this is not merely a flag read. The flag
        (`CLAIM_ATTACHED_FIELD`) is exact and covers every claim written from 2026-08-12 on. The
        `node_created` scan is what covers logs written BEFORE it and any future path that hands a
        reservation a card somebody else already owns: a card another NODE's durable idea names is
        that node's evidence row, and no reservation may take it down. Both read the raw journal
        rather than the fold, deliberately — a tombstoned or aborted node's card is still not this
        reservation's to drop, and the question here is ownership, not eligibility.

        Fail CLOSED: an unnameable id answers False (nothing is dropped), because the cost of a
        skipped drop is a card that resurrects as proposed inventory, and the cost of a wrong drop is
        somebody else's finished work item deleted from the board.
        """
        canonical = cls._canonical_card_id(card_id)
        if canonical is None:
            return False
        for event in events:
            data = event.data or {}
            if event.type == EV_NODE_BUILDING:
                if (data.get(CLAIM_ATTACHED_FIELD) is True
                        and data.get("node_id") == node_id
                        and cls._canonical_card_id(data.get("card_id")) == canonical):
                    return False
            elif event.type == EV_NODE_CREATED:
                idea = data.get("idea")
                if (isinstance(idea, dict)
                        and cls._canonical_card_id(idea.get("card_id")) == canonical
                        and data.get("node_id") != node_id):
                    return False
        return True

    @classmethod
    def _plan_native_card(cls, events, state: RunState, idea: Idea, *, parents: list[int],
                          parent_generations: dict[str, int], scored_against: Optional[int],
                          source: str, at_node: int,
                          scored_against_attempt: Optional[int] = None,
                          implementation_ref: Optional[str] = None, excluded=(),
                          steering_context=(), cross_run_receipt=None,
                          superseded_card_id: Optional[str] = None,
                          retry_attach: bool = False) -> _CardReservationPlan:
        """Resolve exact live dedupe, crash-prefix reuse, a retry attach, or a fresh engine id.

        `retry_attach` is OPT-IN per call site, not a global policy, because only a caller that can
        COMMIT an attach may ask for one — an `attach` plan mints nothing, so a site that appends
        `card_added` for `mint` and nothing otherwise would silently reserve a node under a card it
        never wrote. `_reserve_node_build` therefore FORWARDS this flag rather than hardcoding it:
        it has five callers, three of which must never attach, and hardcoding it there made the
        opt-in claim in this docstring false the day it was written. Who opts in, and why:

          * `orchestrator.py::_create_node_scoped` -> `_reserve_node_build(retry_attach=True)` —
            the ordinary build spine, and the one site that COMMITS an attach.
          * `orchestrator.py::_prepare_node_idea._link` — the proposal half of that same spine; it
            must agree with the commit or the `idea.card_id != plan.card_id` fence refuses the
            build (see there for the one disagreement that is a RACE rather than a bug).
          * `_stage_prepared_card` — asks, then REFUSES, and names the refusal (see there).

        …and who deliberately does not:

          * `orchestrator.py::_create_injected_node` — an operator-authored experiment. It carries
            `source="operator"` and an `implementation_ref` binding ready-made code, and an attach
            discards BOTH (the durable receipt already exists and is immutable). Folding an
            operator's injected `debug` into the Researcher's card would file human work under an
            agent's receipt and lose the executable identity two injections must not share.
          * `ablation.py::_build_refine_block_child` — `refine_block`, an engine-authored probe.
          * the parallel batch lane in `_handle_create_actions` — pre-proposed drafts that never
            reach `_link`, so nothing there could agree with an attach.
          * the re-proposal reset path — it `_drop_card_once`s the card it supersedes, and an attach
            there would hand it the PARENT's card to drop, taking the parent's own evidence with it.
        """
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
        score_snapshot = cls._card_score_snapshot(state, scored_against, scored_against_attempt)
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
        if retry_attach and not reusable:
            # AFTER the exact-match resolution, never before it: an exact crash-prefix twin of THIS
            # very reservation is the idempotent retry of the same commit and must keep winning, and
            # a `duplicate`/`unsafe_match` verdict must keep refusing. Only when nothing exact
            # applies is the question "is this a re-attempt of a question already on the board?".
            attach_card_id = cls._retry_attach_card(
                state, idea, statement, parents,
                superseded_card_id=superseded_card_id, excluded=excluded)
            if attach_card_id is not None:
                attached_idea = idea.model_copy(
                    deep=True, update={"card_id": attach_card_id})
                # THE PROPOSAL CONTRACT STILL HAS TO HOLD, and the first version of this branch
                # returned above the block below and so skipped it entirely. `_card_added_payload` is
                # not only a payload builder: it is where a proposal is PROVED to be a fixed point of
                # its own validators (`_clamp_fill`'s swept-key clamp), where a malformed
                # `implementation_ref`, an out-of-range `at_node` and a non-printable `source` are
                # refused, and every caller turns that refusal into `invalid` + a `novelty_rejected`
                # receipt. On `mint`/`reuse` all of that applied; on `attach` none of it did, so a
                # node was reserved and BUILT from an idea replay would rebuild differently — the
                # exact unclaimable-Card shape `_prepare_existing_card_claim` calls "the Card and its
                # ownership digest disagree", except with no Card to blame it on.
                #
                # The payload is then DISCARDED, and that is the whole point: the durable `card_added`
                # receipt already exists and is IMMUTABLE. Re-minting one under this card's id would
                # be a second registration and would make the fold refuse the very card it attaches
                # to. So this proves the proposal and writes nothing.
                try:
                    cls._card_added_payload(
                        attach_card_id, statement, action, attached_idea, source=source,
                        at_node=at_node, implementation_ref=implementation_ref,
                        steering_context=steering_context, cross_run_receipt=cross_run_receipt,
                    )
                except (TypeError, ValueError, OverflowError):
                    return _CardReservationPlan("invalid", None, None, None)
                return _CardReservationPlan("attach", attach_card_id, attached_idea, None)
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
                            scored_against_attempt: Optional[int] = None,
                            source: str = "researcher",
                            implementation_ref: Optional[str] = None,
                            steering_context=(), cross_run_receipt=None,
                            retry_attach: bool = False):
        """Reserve one native Card and its node-building owner under one log-tail CAS.

        The final Idea must already exist: the immutable statement and exact action receipt cannot be
        minted honestly before proposal. A new ``card_added`` and its ``node_building{card_id}`` claim
        are one bounded EventStore batch, so another process can land before or after them, never between.
        Legacy orphan registrations remain reusable by an exact retry. ``idea`` remains optional only
        for historical internal callers/tests; production creation paths always supply it.

        ``retry_attach`` is FORWARDED, never hardcoded — see `_plan_native_card` for the table of who
        opts in and what each of the four sites that do not would lose. This is the one site that can
        COMMIT an attach (the append below already writes the claim alone for ``reuse``), which is
        exactly why it must not decide FOR its callers that they wanted one.
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
                    scored_against=scored_against,
                    scored_against_attempt=scored_against_attempt,
                    source=source, at_node=node_id,
                    implementation_ref=implementation_ref, steering_context=steering_context,
                    cross_run_receipt=cross_run_receipt,
                    retry_attach=retry_attach,
                )
                if plan.disposition == "invalid":
                    self._append_proposal_event(EV_NOVELTY_REJECTED, {
                        "node_id": node_id, "generation": 0, "kind": "card_contract",
                        "reason": "proposal cannot form a bounded native Card action",
                        "action": "dropped",
                    })
                    return None
                if plan.disposition == "duplicate":
                    # SILENT HERE, DELIBERATELY, and the receipt lives one pass up instead.
                    # This function is also the batch pre-reservation entry point, reached with a
                    # ready-made Idea and NO paid propose behind it: `_reserve_node_build` called
                    # twice with the same idea is the idempotent retry of one action, and
                    # `test_card_writer_lifecycle::test_batch_prereservations_mint_on_main_thread_
                    # and_dedupe_exact_active_work` pins that it appends NOTHING. A discard row here
                    # would count a phantom loss on every exact twin.
                    #
                    # The loss that IS real — a fully paid propose refused because the board is busy
                    # — lands in `orchestrator._prepare_node_idea._link`, which runs immediately
                    # after the proposal call and nowhere else. That is where the receipt is
                    # written; see it for the measurement.
                    return None
                if plan.disposition not in {"mint", "reuse", "attach"} \
                        or plan.card_id is None or plan.idea is None:
                    return None
                # A proposal-bound sidecar may already name this Card. Main-task-only minting means
                # planner and commit must agree; never silently rebind its digest.
                #
                # ONE disagreement is a RACE, not a bug, and it used to cost the whole turn in
                # silence. `_prepare_node_idea._link` plans the attach OUTSIDE `_id_lock`; a
                # `card_dropped`/`card_merged`/terminal landing between the two passes makes one of
                # them attach and the other mint, and the strict fence then returned None — no
                # reservation, no node, no terminal, no event saying why, and the runaway counter
                # charging a turn nobody could explain. THIS pass is the authority (it holds
                # `_id_lock` and commits under the tail CAS), so when either side named an ATTACH it
                # wins. The two shapes are distinguishable without guessing: a linked id that is
                # already a live Card was an attach target, while a sidecar-bound id is one that does
                # not exist YET and is about to be minted under exactly that spelling — and rebinding
                # THAT is what this fence exists to refuse, so it still does.
                if idea.card_id is not None and idea.card_id != plan.card_id:
                    linked_is_live = self._canonical_card_id(idea.card_id) in state.cards
                    if not (retry_attach
                            and (plan.disposition == "attach" or linked_is_live)):
                        return None
                card_id = plan.card_id
                reserved_idea = plan.idea
                claim = (EV_NODE_BUILDING, {
                    "node_id": node_id,
                    "operator": kind,
                    "parent_ids": parents,
                    "card_id": card_id,
                    # See `_reservation_minted_card`: this reservation did NOT mint `card_id`, so no
                    # close path may drop it. Written only on an attach; its absence is what every
                    # pre-2026-08-12 claim means and is read as "mine to close".
                    **({CLAIM_ATTACHED_FIELD: True} if plan.disposition == "attach" else {}),
                })
                if plan.disposition == "mint":
                    self.store.append_many(
                        [(EV_CARD_ADDED, plan.payload), claim],
                        expected_last_seq=tail,
                    )
                else:
                    # `reuse` and `attach` both name a card the log already registered, so the claim
                    # alone is the whole commit. For `attach` that claim is the ENTIRE fix: the fold
                    # links this node to the existing card through `node_created.idea.card_id`
                    # (`card_ledger.py::_link_cards_to_nodes`), giving one card two evidence rows
                    # instead of two cards one each.
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

        Sets ``_card_stage_attached_to`` when the refusal was the PERMANENT one (the proposal is a
        repair of a question a live Card already owns); see the `attach` branch below. Cleared here
        so a caller reading it after this call is reading THIS call.
        """
        self._card_stage_attached_to = None
        # Cleared here for the same reason `_card_stage_attached_to` is: a caller reading it after
        # this call must be reading THIS call.
        self._card_stage_refusal = None
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

        def _refuse(reason: str):
            """Record WHICH fence moved, then refuse exactly as before (`None`).

            The comparisons and their ORDER below are unchanged — this is the same fence, saying
            which half of it moved."""
            self._card_stage_refusal = reason
            return None

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
                return _refuse("authority_seq_moved")
            state = _fold(events)
            if state.search_epoch != proposal_state.search_epoch:
                return _refuse("search_epoch_moved")
            if state.paused or state.finished or state.stop_requested:
                return _refuse("run_stopping")
            if state.best_node_id != proposal_state.best_node_id:
                return _refuse("best_moved")
            if self._proposal_cue_fence(state) != expected_cues:
                return _refuse("cues_moved")
            if self._node_id_ceiling(events, state) != proposal_node_ceiling:
                return _refuse("node_ceiling_moved")
            if self._build_parent_snapshot(state, action) != expected_parent:
                return _refuse("parent_moved")
            if self._card_score_snapshot(state, proposal_state.best_node_id) != expected_score:
                return _refuse("score_moved")
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
                # ASKED FOR, then REFUSED below — and the pair is the point. Without it this staging
                # lane plans with attach OFF, mints the duplicate itself, and the fix in
                # `_reserve_node_build` never runs: `_stage_card_creates` is the ONLY writer of Card
                # INVENTORY, and in `runs/rubertlite-dr-unified-v5` it is the lane that wrote card-3
                # (a `card_added` with no `node_building` beside it, then `card_build_requested`).
                retry_attach=True,
            )
            if plan.disposition == "attach":
                # A re-attempt is not INVENTORY. Staging exists to publish a selection-ready Card the
                # next fold can elect, and the attach target never can be: it already owns a terminal
                # work item, so the fold gives it `work_terminal` and `selection_ready=False` forever
                # (`card_ledger.py::_apply_card_selection_readiness`). Returning its id here would
                # stage a Card that no turn can ever select.
                #
                # NAMING the refusal is the whole difference between this branch and the generic
                # `return None` four lines down, and without the name this branch was PROVABLY dead
                # code: deleting it and leaving the comment kept all 17 tests in
                # `tests/test_card_retry_attaches.py` green, because an `attach` falls through the
                # `reuse`/`mint` tests to the same `None`. A refusal nobody can observe is also a
                # refusal nobody can bound — the earlier comment here cited
                # `_note_card_claim_refusal`, which is charged ONLY when `_claim_existing_card_builds`
                # returns None and therefore never sees this lane at all. `_card_claim_refusal` is
                # what `_create_stall_diagnosis` reads, so a run that stalls here now says why
                # instead of reporting "N action(s) planned … without creating a node".
                #
                # The refusal is also PERMANENT for this action, unlike every stale-fence `None`
                # above it: the attach target does not become stageable by waiting. So the caller
                # must degrade to the serial boundary rather than re-propose —
                # `_handle_create_actions` gives the lane "one ordinary serial compatibility try"
                # (`_create_node` -> `_prepare_node_idea` -> `_reserve_node_build(retry_attach=True)`,
                # which is exactly where the attach is committed), and `speculation.py`'s raw lane
                # reads `_card_stage_attached_to` to yield outer instead of paying for the same
                # proposal again. The node still gets built; only the twin is gone.
                self._card_stage_attached_to = plan.card_id
                self._card_claim_refusal = (
                    f"{plan.card_id} already owns this research question — a repair attaches to it "
                    f"as another node and can never be staged as new selectable inventory")
                return None
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
    async def _stage_card_creates(self, actions: list[dict], state: RunState) -> list[str]:
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
                # OFF THE EVENT-LOOP THREAD, through the SAME helper the other batch call site
                # uses — see `orchestrator.py::_await_batch_proposal` for why the sink is not
                # optional and why the beacon travels with it. This branch is reachable whenever a
                # card run stages a multi-draft lane, e.g. an occupancy-paced create fired precisely
                # BECAUSE an eval is in flight, and while it ran no eval terminal, watchdog tick or
                # timer could land. On the shipped default width it is the path a run takes.
                #
                # THE FENCE CAN MOVE ACROSS THIS AWAIT, and since 2026-08-31 that is COUNTED. This
                # is the batch branch's first suspension point between the authority fold above and
                # the staging loop, and `_stage_prepared_card._plan` compares the fresh fold against
                # that snapshot — so one best-IMPROVING eval terminal, or one
                # `research_completed`/`hint`/strategy row (all BACKGROUND_APPENDABLE, all hashed by
                # `_proposal_cue_fence`), landing during the minutes-long paid propose refuses EVERY
                # idea of the batch at staging. Pre-offload this was unreachable: the frozen loop
                # meant no fence input could move mid-propose. The occupancy-paced create this very
                # comment advertises makes it routine, and one moved fence discards N paid ideas
                # where the per-action lane risks one.
                #
                # The refusal itself is the designed answer to moved authority and is unchanged;
                # what was missing is that the loss was unattributable. Each conjunct now names
                # itself (`CARD_STAGE_REFUSALS`) and the staging loop logs the count and the slug.
                ideas, telemetry, dropped_batch = await self._await_batch_proposal(
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
                    # The per-action lane of `_stage_card_creates` (the batch lane's sibling — see
                    # the note below on why the two do not share `_link`). One paid Researcher call
                    # per action, run serially, so without a beacon a width-4 stage is four
                    # invisible waits in a row that read as one hang.
                    with self._progress(PROGRESS_STAGE_BUILD, "propose",
                                        node_id=proposal_node_ceiling + offset, prospective=True,
                                        operator=action.get("kind")):
                        # OFF THE EVENT-LOOP THREAD, and only this half. `_prepare_node_idea` is a
                        # paid Researcher call — minutes of provider latency with no `await` in it —
                        # and it ran as ONE event-loop callback, so nothing else on the loop could
                        # progress for its whole duration. Measured on the live engine with py-spy:
                        # asyncio's own `_run_once` sat BELOW a `threading.join` with no coroutine
                        # frame between, and a node whose training had already died waited 62 minutes
                        # for its terminal while both H200s idled.
                        #
                        # THE STAGING HALF DELIBERATELY STAYS HERE. The module's own contract is that
                        # "every selection-affecting event ... is written by the main engine task",
                        # and the loop below says why in its own words ("MAIN TASK: both callers of
                        # `_stage_card_creates` ... run there, so the pause is appended immediately").
                        # `_prepare_node_idea` is the right thing to move precisely because it writes
                        # NOTHING: an AST pass over it finds zero `store.append` calls, and its audit
                        # rows go through `_append_proposal_event`, whose `_PROPOSAL_EVENT_SINK`
                        # contextvar survives `to_thread.run_sync` (driven, not assumed).
                        # The "writes NOTHING" claim above was FALSE ONE CALL DEEP until 2026-08-29
                        # (shipped by the sink below): `_prepare_node_idea._link` and
                        # `_apply_novelty_gate` reach `_append_proposal_event`, which falls through
                        # to `self.store.append` whenever no sink is installed — and the one
                        # installer was `speculation.py::_prepare_raw_card_stage` (Layer 5), never
                        # this lane. The guard `test_propose_does_not_freeze_the_loop::
                        # test_the_offloaded_call_writes_no_events` walks only `_prepare_node_idea`'s
                        # own AST and cannot see an append one helper down, which is why nothing was
                        # red for it.
                        #
                        # THE SINK IS THE FIX, and it is Layer 5's own discipline rather than a second
                        # one: `_capture_proposal_events` buffers every `_append_proposal_event` into
                        # a list instead of writing, and the MAIN TASK publishes them below. Without
                        # it `_prepare_node_idea._link` and `_apply_novelty_gate` reach
                        # `self.store.append` from this worker with EV_NOVELTY_REJECTED /
                        # EV_NOVELTY_GRADED / EV_CROSS_RUN_PRIOR — all FOLDED, and named by none of
                        # `events/types.py`'s three thread-append registries, so invariant #1's
                        # sole-writer rule was breached on a DEFAULT card lane, unregistered and
                        # unproven. Verified by driving it: `novelty_rejected` is in neither
                        # BACKGROUND_APPENDABLE, SETUP_THREAD_APPENDABLE,
                        # NON_CARD_SELECTION_BACKGROUND_APPENDABLE nor DIAGNOSTIC_EVENTS.
                        #
                        # It is not only a registry violation. These rows are AUTHORITY-BEARING for
                        # `speculation.py::_proposal_authority_seq`, the fence that discards a paid
                        # proposal when any non-diagnostic row lands inside its max-seq equality
                        # window — the same hazard invariant #1 records for `train_monitor_alert`. A
                        # worker-thread append lands at instants the main-task ordering excluded, so
                        # the loss it can cause is a proposal the run already paid for.
                        captured: list = []
                        try:
                            with self._capture_proposal_events() as captured:
                                idea = await anyio.to_thread.run_sync(
                                    functools.partial(
                                        self._prepare_node_idea,
                                        action,
                                        proposal_state,
                                        researcher=self.researcher,
                                        prospective_node_id=proposal_node_ceiling + offset,
                                        source=source,
                                        proposal_events=proposal_events,
                                    )
                                )
                            # PUBLISHED FROM THE MAIN TASK, and published WHETHER OR NOT the idea formed.
                            # A refused proposal is exactly when the receipt matters most: the discard
                            # receipt (`bd182357`) exists because a paid propose that produced no card
                            # left no trace at all, and dropping the intents on `idea is None` would
                            # restore that silence for the case it was written for. Layer 5 drops them
                            # only when it ABANDONS and re-makes the proposal, which this lane never does.
                        # …AND PUBLISHED ON THE WAY OUT, since 2026-08-31, because a RAISE from the offloaded
                        # call discarded every buffered row. `_reject_and_repropose` appends `budget_exceeded`
                        # through this very sink and then RE-RAISES — its own docstring says "appended BEFORE
                        # re-raising so the rejection is on the log even though the run is ending" — and both
                        # shipped researchers propagate it. Pre-offload every row was durable at emit time;
                        # buffering made the publish conditional on a clean return without anyone deciding that.
                        # `store.append` is sync and legal during unwind, so the `finally` costs nothing and keeps
                        # the promise the sink was introduced to keep.
                        finally:
                            for _event_type, _data, _trace_id, _span_id in captured:
                                self.store.append(_event_type, _data,
                                                  trace_id=_trace_id, span_id=_span_id)
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
            refused: collections.Counter = collections.Counter()
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
                else:
                    refused[getattr(self, "_card_stage_refusal", None) or "unnamed"] += 1

            # THE LOSS IS COUNTED AND SAID, since 2026-08-31. Every refusal above returned a bare
            # `None` and this loop dropped it, so a batch whose fence moved during the minutes-long
            # paid propose discarded N ideas with NOTHING on the record — no receipt, no line, and
            # then one more paid serial try plus a re-paid batch next turn.
            #
            # NOT AN EVENT, and that is a decision. A folded row here would move
            # `_proposal_authority_seq` — the very fence this reports on — and a diagnostic row per
            # refused idea is an append per staging turn for a fact that repeats. The refusal is
            # already the DESIGNED answer to moved authority; what was missing was only that nobody
            # could count it. `_admissible_beliefs`' "Not silent" logging is the precedent one
            # cadence over.
            if refused:
                _LOG.warning(
                    "card staging refused %d of %d prepared proposal(s) — the fence moved during "
                    "the paid propose: %s", sum(refused.values()), len(prepared),
                    ", ".join(f"{name}={count}" for name, count in sorted(refused.items())))

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
            # The claim half of the concept round trip (`_authored_card_concepts`). `registrations[0]`
            # is the single mint row whose `ownership_receipt` was just proved equal to the one this
            # Card's action digest mints, so it is the exact writer row — but the digest deliberately
            # does NOT cover the concept envelope, and that is fine here rather than merely tolerated:
            # replay stamps whatever is rebuilt `NODE_CONCEPT_PROVENANCE_AUTHORED`, which
            # `classifier_verified_node_concepts` already refuses as independent evidence. It reaches
            # display read-models and `concept_run_base`'s seed, never admission or cross-run trust.
            # A `delta` envelope rebuilds UNRESOLVED and is materialized by the fold against this
            # node's own parents — which are the Card's parents, re-fenced generation-for-generation a
            # few lines up, so the base is the lineage the proposal was authored for.
            # Calibration keeps precedence: its synthetic envelope is part of a byte-stable fixture.
            calibration_concepts = ({
                "concept_mode": "full",
                "concepts": [
                    f"operator/{card.operator}",
                    "objective/quadratic",
                    "space/two-dimensional",
                ],
            } if self._speculation_gate_calibration else self._claim_concept_envelope(
                registrations[0].data.get("idea")))
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
        self, actions: list[dict], *, ignored_pending_node_ids: Collection[int] = (),
    ) -> Optional[list[_BuildReservation]]:
        """Atomically claim the complete Card lane selected from one fresh snapshot.

        A population policy may select several Cards at once. Claiming and building them one-by-one
        would make the first pending node engage the evaluate-all forced gate and invalidate its
        siblings. The whole lane is therefore revalidated under ``_id_lock`` and its ``node_building``
        owners are appended as one tail-CAS group before any slow Developer work begins.

        Every refusal path names itself through `_refuse_card_claim`; see there for why.

        `ignored_pending_node_ids` is the OCCUPANCY-PACED caller's mask (backlog F1g), and it must be
        the same one the selection it is claiming was made under. The revalidation below re-derives
        the lane, and re-deriving it unmasked while an evaluation runs answers a different question:
        the running Node is still `pending`, so `forced_card_actions` returns its evaluate lane, the
        comparison reads `['card-1'] -> []`, and after `_CARD_CLAIM_RETIRE_AFTER` turns the Card is
        durably `card_auto_dropped` as "unclaimable". Measured on a toy-backend run of that shape:
        every Card minted during an evaluation was retired within three turns and the node was still
        built serially after the terminal — the two copies of the rule disagreeing is precisely the
        failure `CardSessionGates`' docstring was written about, one lane over. The mask is exactly
        the in-flight set, so the evaluate-all discipline still holds for every pending Node that has
        NOT been started.
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
                if ignored_pending_node_ids:
                    # ONE query, the same one the occupancy-paced turn asked, through the same
                    # production entry point the speculative producer uses. Not a second rule: the
                    # masked selection already carries every receipt, generation, anchor, trust,
                    # cadence, scorer, lane-width, pin and budget gate the unmasked pair below does,
                    # forced phases included — `_speculative_selection` runs `_forced_card_actions`
                    # itself, with the mask applied.
                    current_ids = speculative_card_selection_set(
                        state, self.policy, max_nodes,
                        context=SpeculativeSelectionContext(
                            scoring=getattr(self, "_card_scoring", None),
                            ignored_pending_node_ids=ignored_pending_node_ids,
                            resource_envelope=self._resource_envelope(),
                        ),
                    )
                elif (forced := forced_card_actions(state, self.policy, max_nodes)) is not None:
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
            # IDEMPOTENCE IS "IS A DROP STANDING", NOT "WAS ONE EVER APPENDED", and keying it on the
            # latter made an operator reopen permanently un-retirable by the engine. The fold has
            # resolved drop/reopen LAST-RECEIPT-WINS since `dccad06f` (`events/card_ledger.py`), so
            # after drop -> reopen the board shows the card LIVE while this scan still saw the
            # historical drop and returned without appending. Every later engine retirement then
            # silently no-opped while its caller believed the card retired: `_retire_unclaimable_
            # cards` resets its counters and re-enters the same refuse/retire cycle, and the
            # node-reset re-propose leaves the superseded twin live beside its replacement — the
            # leak `_exhausted` below exists to refuse, made silent. The ledger's own comment names
            # the state ("permanently un-droppable by its owner") and blocks only the laundering
            # path; the legitimate operator reopen reached it too.
            #
            # THE RULE IS THE FOLD'S, REPLAYED OVER RAW EVENTS RATHER THAN RE-INVENTED, and the
            # author test is why `_drop_author` is IMPORTED: its own docstring says "ONE spelling,
            # because three readers ask it and they must not drift", and this is now a fourth. A
            # reopen may only undo an OPERATOR's drop — an engine `card_auto_dropped` stands whatever
            # follows it, which is what stops a reopen from laundering a rejected proposal back onto
            # the selectable board (`_record_node_less_card` mints and auto-drops in one
            # `append_many` precisely so the audit row is never live).
            #
            # Order is the log's own, so no `_event_index` is needed here: the CAS hands this plan
            # the prefix in append order, and "later" is simply "further along `events`".
            standing = False
            standing_author = "engine"
            for event in events:
                if self._canonical_card_id(event.data.get("id")) != card_id:
                    continue
                if event.type in {EV_CARD_AUTO_DROPPED, EV_CARD_DROPPED}:
                    standing = True
                    standing_author = _drop_author(event.data or {})
                elif event.type == EV_CARD_REOPENED and standing and standing_author == "operator":
                    standing = False
            if standing:
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
            def _plan(events, tail):
                state = _fold(events)
                # Same funnel rule as `_plan_native_card`: this writer builds a real action and a real
                # receipt, so its Idea must reach `_card_added_payload` as a fixed point too or the
                # round-trip proof there would silently cost every rejected proposal its audit Card.
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
                self.store.append_many([
                    (EV_CARD_ADDED, payload),
                    (EV_CARD_AUTO_DROPPED, {
                        "id": card_id, "reason": reason, "dropped_by": "engine",
                    }),
                ], expected_last_seq=tail)
                return card_id

            def _exhausted():
                raise RuntimeError("could not append node-less Card after concurrent log movement")

            return retry_tail_cas(self.store, _plan, on_exhaust=_exhausted)

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
                        and len(statement.strip()) <= CARD_STATEMENT_MAX_CHARS
                        and statement.strip().isprintable()):
                    payload["statement"] = statement.strip()
                self.store.append(EV_CARD_MERGED, payload)
                wrote = True
        return _fold(self.store.read_all()) if wrote else state
