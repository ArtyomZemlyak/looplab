"""Request-driven Card speculation (docs/23, Layers 5a/5b).

The append-only log remains the queue.  Background producer work may only return an in-memory
``SpecBuildResult``; every selection-affecting event and every speculative ``node_created`` is written
by the main engine task.  The mixin is inert unless both Card selection and a positive, run-pinned
``speculation_depth`` are enabled.
"""
from __future__ import annotations

import functools
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import anyio

from looplab.core.advisory_payloads import bounded_cross_run_advisory_receipt
from looplab.core.models import (
    Idea,
    NodeStatus,
    RunState,
    card_ownership_receipt,
    durable_idea_payload,
)
from looplab.core.llm_broker import in_llm_lane
from looplab.events.eventstore import EventStoreConcurrencyError
from looplab.events.replay import fold
from looplab.events.types import (
    EV_CARD_ADDED,
    EV_CARD_BUILD_DONE,
    EV_CARD_BUILD_REQUESTED,
    EV_LLM_COST,
    EV_LLM_USAGE,
    EV_NODE_BUILDING,
    EV_NODE_FAILED,
    EV_PAUSE,
    EV_POLICY_DECISION,
)
from looplab.search.card_selection import (
    CARD_FRESHNESS_SUPERSEDED_ERROR,
    META_CARD_ID,
    CardResourceEnvelope,
    card_budget_used,
    speculative_card_actions,
    speculative_card_is_fresh,
    speculative_raw_actions,
)


@dataclass(frozen=True)
class SpecBuildResult:
    """One isolated producer result.  It is never serialized or treated as queue authority."""

    card_id: str
    generation: int
    action: dict[str, Any]
    success: bool
    idea: Optional[Idea] = None
    code: str = ""
    files: dict[str, str] = field(default_factory=dict)
    deleted: tuple[str, ...] = ()
    footprint_finalized: bool = False
    cross_run_receipt: dict[str, Any] = field(default_factory=dict)
    roles: Optional[tuple[Any, Any]] = field(default=None, compare=False, repr=False)
    error: str = ""

    @property
    def key(self) -> tuple[str, int]:
        return self.card_id, self.generation


@dataclass(frozen=True)
class SpecRawStageResult:
    """One isolated raw-policy proposal awaiting a short main-task Card commit."""

    generation: int
    action: dict[str, Any]
    proposal_state: RunState = field(compare=False, repr=False)
    proposal_authority_seq: int
    proposal_node_ceiling: int
    at_node: int
    source: str
    cue_fence: bytes
    success: bool
    idea: Optional[Idea] = None
    steering_context: tuple[Any, ...] = ()
    cross_run_receipt: dict[str, Any] = field(default_factory=dict)
    audit_events: tuple[tuple[str, dict, Optional[str], Optional[str]], ...] = ()
    error: str = ""


class SpeculationMixin:
    """Execution helpers inherited by :class:`looplab.engine.orchestrator.Engine`."""

    def _speculation_enabled(self) -> bool:
        return bool(
            getattr(self, "card_driven_selection", False)
            and int(getattr(self, "speculation_depth", 0) or 0) > 0
        )

    @staticmethod
    def _proposal_authority_seq(events: list) -> int:
        """Latest selection-authority seq, ignoring worker-owned LLM accounting telemetry."""

        return max(
            (
                event.seq for event in events
                if event.type not in {EV_LLM_USAGE, EV_LLM_COST}
                and type(event.seq) is int
            ),
            default=-1,
        )

    def _ensure_speculation_state(self) -> None:
        # Focused tests often construct Engine through __new__; keep every live-only field lazy.
        if not hasattr(self, "_spec_builds"):
            self._spec_builds: dict[tuple[str, int], SpecBuildResult] = {}
        if not hasattr(self, "_spec_build_inflight"):
            self._spec_build_inflight: set[tuple[str, int]] = set()
        if not hasattr(self, "_spec_role_pair"):
            self._spec_role_pair: Optional[tuple[Any, Any]] = None
        if not hasattr(self, "_spec_raw_stage_inflight"):
            self._spec_raw_stage_inflight = False
        if not hasattr(self, "_spec_raw_stage_result"):
            self._spec_raw_stage_result: Optional[SpecRawStageResult] = None
        if not hasattr(self, "_spec_force_outer"):
            self._spec_force_outer = False

    def _producer_role_pair(self) -> Optional[tuple[Any, Any]]:
        """Lease one non-primary pair from the Layer-2 role pool.

        ``_build_role_pairs(1)`` is intentionally not used: it returns the primary roles whose
        per-build output slots are shared with repairs and ordinary builds.  The surrounding Card
        session never overlaps a normal build batch, so the cached pool pair is exclusively leased
        for the session and can be safely reused by its single producer.
        """

        self._ensure_speculation_state()
        if self._spec_role_pair is not None:
            return self._spec_role_pair
        if getattr(self, "role_factory", None) is None:
            return None
        pairs = self._build_role_pairs(2)
        if len(pairs) < 2:
            return None
        pair = pairs[1]
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or pair[0] is getattr(self, "researcher", None)
            or pair[1] is getattr(self, "developer", None)
        ):
            return None
        self._spec_role_pair = pair
        return pair

    @staticmethod
    def _request_key(request: object) -> Optional[tuple[str, int]]:
        if not isinstance(request, Mapping):
            return None
        card_id = request.get("card_id")
        generation = request.get("generation")
        if (
            not isinstance(card_id, str)
            or not card_id
            or type(generation) is not int
            or generation < 0
        ):
            return None
        return card_id, generation

    @staticmethod
    def _outstanding_requests(state: RunState) -> list[dict]:
        done = max(0, min(int(state.card_builds_done), len(state.card_build_requests)))
        return [dict(request) for request in state.card_build_requests[done:]
                if isinstance(request, Mapping)]

    @classmethod
    def _head_request(cls, state: RunState) -> Optional[dict]:
        outstanding = cls._outstanding_requests(state)
        return outstanding[0] if outstanding else None

    @staticmethod
    def _developer_sentinel(node) -> bool:
        return bool(
            node is not None
            and isinstance(getattr(node, "code", None), str)
            and node.code.startswith("(developer error:")
        )

    @staticmethod
    def _has_exact_developer_pause(
        events,
        *,
        node_id: int,
        generation: int,
        after_seq: int,
    ) -> bool:
        """Whether this exact failed lifecycle has already owned an auto-pause.

        Raw history, rather than folded ``state.paused``, is authoritative: a later resume clears
        the folded pause but must not make recovery append the same scoped pause again. A pause
        before the terminal is not an acknowledgement because replay rejects it while the Node is
        pending, hence the strict sequence boundary.
        """

        return any(
            event.type == EV_PAUSE
            and event.seq > after_seq
            and isinstance(event.data, Mapping)
            and type(event.data.get("node_id")) is int
            and event.data.get("node_id") == node_id
            and type(event.data.get("generation")) is int
            and event.data.get("generation") == generation
            for event in events
        )

    def _resource_envelope(self) -> CardResourceEnvelope:
        ids = list(getattr(self, "_gpu_ids", []) or [])
        memory_map = getattr(self, "_gpu_mem", {}) or {}
        memory = tuple(
            int(memory_map[gpu]) for gpu in ids
            if type(memory_map.get(gpu)) is int and memory_map[gpu] >= 0
        )
        return CardResourceEnvelope(
            gpu_count=len(ids),
            gpu_memory_mib=memory if len(memory) == len(ids) else (),
        )

    @staticmethod
    def _speculative_link_matches(state: RunState, node) -> bool:
        if node is None or getattr(node, "speculative", False) is not True:
            return False
        generation = getattr(node, "card_build_generation", None)
        link = state.speculative_nodes.get(node.id)
        return bool(
            node.attempt == 0
            and type(generation) is int
            and isinstance(link, Mapping)
            and link.get("card_id") == node.idea.card_id
            and link.get("generation") == generation
        )

    @classmethod
    def _speculative_pending_nodes(cls, state: RunState) -> list:
        return [
            node for node in state.pending_nodes()
            if cls._speculative_link_matches(state, node)
        ]

    @classmethod
    def _speculation_depth_used(
        cls,
        state: RunState,
        *,
        consumed_inflight: set[tuple[int, int]] | frozenset[tuple[int, int]] = frozenset(),
    ) -> int:
        """Count prefetched work not already being consumed by this exact eval session.

        The public depth contract counts outstanding requests plus committed/unevaluated speculative
        Nodes.  During the live overlap window, however, a Node already admitted to the consumer is no
        longer prefetch inventory: retaining it in the count makes depth=1 strictly serial.  Subtract
        only exact ``(id, attempt)`` pairs whose Nodes also carry the durable speculative marker+done
        link; arbitrary pending ids can never relax the outer or resume gate.
        """

        consumed = {
            key for key in consumed_inflight
            if (isinstance(key, tuple) and len(key) == 2
                and type(key[0]) is int and type(key[1]) is int)
        }
        pending = sum(
            1 for node in cls._speculative_pending_nodes(state)
            if (node.id, node.attempt) not in consumed
        )
        return len(cls._outstanding_requests(state)) + pending

    @classmethod
    def _speculative_card_ids(cls, state: RunState) -> set[str]:
        ids = {
            key[0] for request in cls._outstanding_requests(state)
            if (key := cls._request_key(request)) is not None
        }
        ids.update(
            node.idea.card_id for node in state.pending_nodes()
            if isinstance(node.idea.card_id, str)
        )
        return ids

    @staticmethod
    def _terminal_intent(state: RunState) -> bool:
        return bool(state.paused or state.finished or state.stop_requested)

    def _discard_spec_result(self, result: Optional[SpecBuildResult]) -> None:
        if result is None or result.roles is None:
            return
        self._discard_node_build_telemetry(
            researcher=result.roles[0], developer=result.roles[1],
        )

    def _discard_orphaned_spec_results(self, state: RunState) -> None:
        """Release role side channels for buffers whose durable request has already closed."""

        self._ensure_speculation_state()
        outstanding = {
            key for request in self._outstanding_requests(state)
            if (key := self._request_key(request)) is not None
        }
        for key in list(self._spec_builds):
            if key not in outstanding:
                self._discard_spec_result(self._spec_builds.pop(key, None))

    @classmethod
    def _acknowledged_pending_ids(cls, state: RunState) -> set[int]:
        """Pending work owned by the session consumer, not a license to erase it from budget/cadence."""

        return {
            node.id for node in state.pending_nodes()
            if not cls._developer_sentinel(node)
        }

    def _producer_card_reservation(self, request: Mapping[str, Any]):
        """Purely reconstruct the exact requested Card/Idea; append no event."""

        key = self._request_key(request)
        if key is None:
            return None, None, {}
        card_id, generation = key
        events = self.store.read_all()
        state = fold(events)
        head = self._head_request(state)
        if self._request_key(head) != key or generation != state.search_epoch:
            return None, None, {}
        card = state.cards.get(card_id)
        if card is None:
            return None, None, {}
        from looplab.search.card_selection import card_action
        action = card_action(card)
        if action is None or action.get(META_CARD_ID) != card_id:
            return None, None, {}
        reservation = self._prepare_existing_card_claim(
            events,
            state,
            action,
            card,
            self._node_id_ceiling(events, state),
        )
        receipt = {}
        registrations = [
            event.data for event in events
            if event.type == EV_CARD_ADDED and event.data.get("id") == card_id
        ]
        if len(registrations) == 1:
            registration = registrations[0]
            # Use the same canonical immutable action projection as the exact claim boundary. Keeping
            # a second hand-written projection here would silently lose proposal provenance whenever
            # either receipt schema gained a field.
            ownership_action = self._card_claim_receipt_action(card)
            expected = card_ownership_receipt(
                card_id, card.seed_statement, ownership_action,
            )
            if (
                expected is not None
                and registration.get("statement") == card.seed_statement
                and registration.get("ownership_receipt") == expected
                and card.identity.action_digest == expected["action_digest"]
            ):
                # The proposal may have happened in an earlier process. Recover provenance only
                # from its unique durable ownership registration; a live role attribute would be
                # both lossy on resume and vulnerable to stale producer state.
                receipt = bounded_cross_run_advisory_receipt(
                    registration.get("cross_run_receipt")
                )
        return action, reservation, receipt

    @in_llm_lane("build")
    def _build_requested_card(
        self,
        request: Mapping[str, Any],
        roles: tuple[Any, Any],
    ) -> SpecBuildResult:
        """Worker-thread producer: compute only, with no folded event writes."""

        key = self._request_key(request)
        if key is None:
            return SpecBuildResult("", 0, {}, False, error="malformed request")
        card_id, generation = key
        researcher, developer = roles
        # The isolated pair is reused sequentially. Clear every per-build side channel before even
        # validating the durable request so a stale predecessor can never annotate this Card.
        self._discard_node_build_telemetry(researcher=researcher, developer=developer)
        action, reservation, cross_run_receipt = self._producer_card_reservation(request)
        if action is None or reservation is None or reservation.idea is None:
            return SpecBuildResult(
                card_id, generation, {}, False, roles=roles,
                error="requested Card is no longer buildable",
            )
        state = reservation.state
        idea = reservation.idea.model_copy(deep=True)
        kind = reservation.kind
        try:
            self._reset_developer_footprint(developer)
            if kind == "draft":
                code = developer.implement(
                    self._directed_idea(idea.model_copy(deep=True), state)
                )
            elif kind == "merge":
                parents = [state.nodes[node_id] for node_id in reservation.parent_ids]
                implement_from = getattr(developer, "implement_from", None)
                directed = self._directed_idea(idea.model_copy(deep=True), state)
                code = (
                    implement_from(directed, parents[0])
                    if self._merge_mode == "ensemble" and callable(implement_from) and parents
                    else developer.implement(directed)
                )
            elif kind == "debug":
                parent = state.nodes[action["parent_id"]]
                repair = getattr(developer, "repair", None)
                if callable(repair) and parent.error and (
                    parent.code or parent.files or self._repo_spec
                ):
                    error = self._repair_error_context(
                        parent.error_reason, parent.error, state=state, node=parent,
                    )
                    code = self._repair(parent, error, state, developer=developer)
                else:
                    code = self._implement(
                        self._directed_idea(idea.model_copy(deep=True), state),
                        parent,
                        developer=developer,
                    )
            else:
                parent = state.nodes[action["parent_id"]]
                code = self._implement(
                    self._directed_idea(idea.model_copy(deep=True), state),
                    parent,
                    developer=developer,
                )
            idea, finalized = self._finalize_developer_footprint(idea, developer, code)
            files = dict(getattr(developer, "last_files", {}) or {})
            deleted = tuple(getattr(developer, "last_deleted", []) or [])
            return SpecBuildResult(
                card_id=card_id,
                generation=generation,
                action=dict(action),
                success=True,
                idea=idea,
                code=code,
                files=files,
                deleted=deleted,
                footprint_finalized=bool(finalized),
                # This Card may have been authored in an earlier process; its unique durable
                # registration, not the current producer role, owns the advisory provenance.
                cross_run_receipt=cross_run_receipt,
                roles=roles,
            )
        except Exception as exc:  # one producer failure must become an explicit give-up result
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)
            return SpecBuildResult(
                card_id, generation, dict(action), False, roles=roles,
                error=f"{type(exc).__name__}: {exc}"[:2_048],
            )

    def _research_origin_for_node(self, state: RunState, node_id: int) -> Optional[dict]:
        if not state.research:
            return None
        memo = state.research[-1]
        at_node = memo.get("at_node") if isinstance(memo, Mapping) else None
        if type(at_node) is not int or not at_node <= node_id < at_node + 2:
            return None
        from looplab.core.advisory_payloads import valid_advisory_ref
        memo_id = memo.get("memo_id")
        return {
            "at_node": at_node,
            "trigger": memo.get("trigger"),
            **({"memo_id": memo_id} if valid_advisory_ref(memo_id, "memo") else {}),
        }

    def _create_precoded_node(
        self,
        action: dict,
        reserved,
        result: SpecBuildResult,
        *,
        max_eval_seconds: Optional[float] = None,
    ) -> None:
        """Main-task-only commit of one producer result through the ordinary Node lifecycle."""

        if (
            reserved is None
            or not result.success
            or result.idea is None
            or result.roles is None
            or reserved.card_id != result.card_id
            or result.idea.card_id != result.card_id
            or reserved.kind != action.get("kind")
            or type(result.generation) is not int
            or result.generation < 0
        ):
            if reserved is not None:
                self._fail_reserved_build(
                    node_id=reserved.node_id,
                    card_id=reserved.card_id,
                    generation=0,
                    error="invalid precoded Card result",
                    reason="superseded",
                )
            if result.roles is not None:
                self._discard_node_build_telemetry(
                    researcher=result.roles[0], developer=result.roles[1],
                )
            return

        researcher, developer = result.roles
        state = reserved.state
        node_id = reserved.node_id
        idea = result.idea.model_copy(deep=True)
        with self.tracer.span("materialize_node", node_id=node_id, operator=reserved.kind):
            created_event = False
            for _attempt in range(64):
                events = self.store.read_all()
                latest = fold(events)
                latest_card = latest.cards.get(result.card_id)
                if (
                    latest.paused
                    or latest.finished
                    or latest.stop_requested
                    or latest.search_epoch != result.generation
                    or node_id in latest.aborted_nodes
                    or latest_card is None
                    or latest_card.dropped_reason is not None
                    or latest_card.merged_into is not None
                    or (
                        max_eval_seconds is not None
                        and latest.total_eval_seconds >= max_eval_seconds
                    )
                    or any(
                        parent_id not in latest.nodes
                        or latest.nodes[parent_id].attempt != parent_generation
                        or latest.nodes[parent_id].tombstoned
                        or parent_id in latest.aborted_nodes
                        for parent_id, parent_generation in (
                            (int(parent_id), generation)
                            for parent_id, generation in reserved.parent_generations.items()
                        )
                    )
                ):
                    self._fail_reserved_build(
                        node_id=node_id,
                        card_id=reserved.card_id,
                        generation=0,
                        error="speculative build became stale before commit",
                        reason="superseded",
                    )
                    self._discard_node_build_telemetry(
                        researcher=researcher, developer=developer,
                    )
                    return
                tail = events[-1].seq if events else -1
                try:
                    self._emit_node_created(
                        node_id=node_id,
                        parent_ids=list(reserved.parent_ids),
                        operator=idea.operator,
                        idea=durable_idea_payload(idea),
                        code=result.code,
                        files=dict(result.files),
                        deleted=list(result.deleted),
                        research_origin=self._research_origin_for_node(state, node_id),
                        cross_run_receipt=dict(result.cross_run_receipt),
                        **({"parent_generations": reserved.parent_generations}
                           if reserved.parent_generations else {}),
                        **({"footprint_finalized": True}
                           if result.footprint_finalized else {}),
                        speculative=True,
                        card_build_generation=result.generation,
                        expected_last_seq=tail,
                    )
                    created_event = True
                    break
                except EventStoreConcurrencyError:
                    continue
            if not created_event:
                self._fail_reserved_build(
                    node_id=node_id,
                    card_id=reserved.card_id,
                    generation=0,
                    error="speculative node commit lost its event-tail CAS",
                    reason="superseded",
                )
                self._discard_node_build_telemetry(
                    researcher=researcher, developer=developer,
                )
                return
            created = fold(self.store.read_all()).nodes.get(node_id)
            if (
                created is None
                or created.idea.card_id != result.card_id
                or created.speculative is not True
                or created.card_build_generation != result.generation
            ):
                self._fail_reserved_build(
                    node_id=node_id,
                    card_id=reserved.card_id,
                    generation=0,
                    error="speculative node creation was rejected during replay",
                    reason="superseded",
                )
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
            if isinstance(result.code, str) and result.code.startswith("(developer error:"):
                # The terminal and its circuit-breaker are one event-log transaction. A process
                # crash may leave the preceding node_created durable, but can never leave a new
                # developer_crash terminal without its matching pause. Tail CAS keeps a concurrent
                # operator control either wholly before or wholly after the pair.
                for _attempt in range(64):
                    terminal_events = self.store.read_all()
                    terminal_state = fold(terminal_events)
                    terminal_node = terminal_state.nodes.get(node_id)
                    if (
                        terminal_node is None
                        or terminal_node.attempt != created.attempt
                        or not self._developer_sentinel(terminal_node)
                        or terminal_node.status is not NodeStatus.pending
                    ):
                        break
                    tail = terminal_events[-1].seq if terminal_events else -1
                    try:
                        self.store.append_many([
                            (EV_NODE_FAILED, {
                                "node_id": node_id,
                                "generation": terminal_node.attempt,
                                "error": result.code,
                                "reason": "developer_crash",
                                "eval_seconds": 0.0,
                            }),
                            (EV_PAUSE, {
                                "node_id": node_id,
                                "generation": terminal_node.attempt,
                                "reason": "auto-paused: a Developer session crashed (LLM unreachable "
                                          "or a hard error, unresolved within the node) — resume once "
                                          "it's fixed",
                            }),
                        ], expected_last_seq=tail)
                        self._create_paused = True
                        break
                    except EventStoreConcurrencyError:
                        continue
        try:
            self._emit_agent_report(node_id, developer=developer)
            self._emit_hypothesis_ranked(node_id, 0, researcher=researcher)
            self._emit_foresight_selected(
                node_id, 0, researcher=researcher, developer=developer,
            )
        finally:
            # `_emit_agent_report` does not consume `last_report`; make pair reuse explicit.
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)

    def _append_card_build_done(
        self,
        request: Mapping[str, Any],
        *,
        node_id: Optional[int] = None,
        skipped: Optional[str] = None,
    ) -> bool:
        """Close only the exact folded head, retrying a moving tail without skipping requests."""

        key = self._request_key(request)
        if key is None or (node_id is None) == (skipped is None):
            return False
        card_id, generation = key
        if skipped is not None and skipped not in {"producer_failed", "stale"}:
            return False
        payload: dict[str, Any] = {"card_id": card_id, "generation": generation}
        if skipped is not None:
            payload["skipped"] = skipped
        else:
            payload.update({"node_id": node_id, "speculative": True})
        for _attempt in range(64):
            events = self.store.read_all()
            state = fold(events)
            if self._request_key(self._head_request(state)) != key:
                # Another main-task path may already have closed it.
                return state.card_builds_done >= len(state.card_build_requests)
            tail = events[-1].seq if events else -1
            try:
                with self._id_lock:
                    self.store.append(
                        EV_CARD_BUILD_DONE, payload, expected_last_seq=tail,
                    )
                return True
            except EventStoreConcurrencyError:
                continue
        return False

    def _matching_created_speculation(
        self, state: RunState, request: Mapping[str, Any],
    ):
        key = self._request_key(request)
        if key is None:
            return None
        card_id, generation = key
        matches = [
            node for node in state.nodes.values()
            if node.id not in state.speculative_nodes
            and node.idea.card_id == card_id
            and node.speculative is True
            and node.card_build_generation == generation
        ]
        return min(matches, key=lambda node: node.id) if matches else None

    def _refresh_speculation_budget(self, state: RunState) -> None:
        used = card_budget_used(state)
        self.policy.max_nodes = max(
            used,
            self._base_max_nodes + int(state.budget_overrides.get("add_nodes", 0) or 0),
        )

    def _speculative_selection_node_limit(self, state: RunState) -> int:
        """Compensate the pure selector for request slots already removed from the live denominator.

        Engine's translated ``policy.max_nodes`` excludes every unmaterialized durable request so the
        Strategist and ordinary selectors cannot advertise an owned slot. The pure speculative selector
        independently subtracts excluded requests (and a claim temporarily reopens its exact head), so
        add those receipts back at this call boundary to avoid charging them twice.
        """

        return max(0, int(self.policy.max_nodes)) + self._unmaterialized_card_reservations(state)

    @staticmethod
    def _producer_failed_card_ids(state: RunState) -> set[str]:
        """Replay-accepted give-ups that must next use the serial compatibility path."""

        return {
            card_id for card_id in state.card_build_producer_failed
            if isinstance(card_id, str) and card_id
        }

    def _card_requires_serial_fallback(self, card_id: object) -> bool:
        state = fold(self.store.read_all())
        return bool(
            isinstance(card_id, str)
            and card_id in self._producer_failed_card_ids(state)
        )

    def _request_card_build(
        self,
        *,
        consumed_inflight: set[tuple[int, int]] | frozenset[tuple[int, int]] = frozenset(),
    ) -> bool:
        """Main-task election + durable compute gate, with all slow scoring outside ``_id_lock``."""

        if not self._speculation_enabled() or self._producer_role_pair() is None:
            return False
        events = self.store.read_all()
        state = fold(events)
        if (
            state.paused
            or state.finished
            or state.stop_requested
            or self._head_request(state) is not None
            or self._speculation_depth_used(
                state, consumed_inflight=consumed_inflight) >= self.speculation_depth
        ):
            return False
        self._refresh_speculation_budget(state)
        if self._node_reservation_slots_remaining(state, events=events) < 1:
            return False
        excluded = self._speculative_card_ids(state)
        excluded.update(self._producer_failed_card_ids(state))
        actions = speculative_card_actions(
            state,
            self.policy,
            self._speculative_selection_node_limit(state),
            scoring=getattr(self, "_card_scoring", None),
            excluded_card_ids=excluded,
            ignored_pending_node_ids=self._acknowledged_pending_ids(state),
            resource_envelope=self._resource_envelope(),
        )
        if not actions:
            return False
        action = actions[0]
        card_id = action.get(META_CARD_ID)
        if not isinstance(card_id, str) or not card_id:
            return False
        tail = events[-1].seq if events else -1
        try:
            # The lock protects only the short CAS append.  Fold, policy and role calls above are all
            # outside it, so a producer/parallel-build/reset stress cannot stall the event loop here.
            with self._id_lock:
                self.store.append(
                    EV_CARD_BUILD_REQUESTED,
                    {"card_id": card_id, "generation": state.search_epoch},
                    expected_last_seq=tail,
                )
            return True
        except EventStoreConcurrencyError:
            return False

    def _claim_requested_card_build(
        self,
        request: Mapping[str, Any],
        result: SpecBuildResult,
        max_eval_seconds: Optional[float] = None,
    ) -> tuple[str, Optional[int]]:
        """Reserve and commit an exact head result; never consult the ready-only serial claim."""

        key = self._request_key(request)
        if key is None or result.key != key or not result.success or result.idea is None:
            return "producer_failed", None
        card_id, generation = key
        events = self.store.read_all()
        state = fold(events)
        if self._request_key(self._head_request(state)) != key:
            return "closed", None
        if (
            generation != state.search_epoch
            or self._terminal_intent(state)
            or (
                max_eval_seconds is not None
                and state.total_eval_seconds >= max_eval_seconds
            )
        ):
            return "stale", None
        self._refresh_speculation_budget(state)
        # The exact request head already owns one durable future slot. Convert that ownership into
        # node_building without double-charging it, but never cross a ceiling that was already full
        # when the request arrived (legacy/corrupt prefixes remain pending for budget_extend).
        if self._node_reservation_slots_remaining(
            state, events=events, consume_request=True,
        ) < 1:
            return "budget", None
        selection_limit = self._speculative_selection_node_limit(state)
        if card_budget_used(state) >= selection_limit:
            return "stale", None

        excluded = self._speculative_card_ids(state)
        excluded.discard(card_id)
        selected_actions = speculative_card_actions(
            state,
            self.policy,
            selection_limit,
            scoring=getattr(self, "_card_scoring", None),
            excluded_card_ids=excluded,
            ignored_pending_node_ids=self._acknowledged_pending_ids(state),
            resource_envelope=self._resource_envelope(),
        )
        selected_action = next(
            (
                action for action in selected_actions
                if action.get(META_CARD_ID) == card_id
            ),
            None,
        )
        if selected_action is None:
            return "stale", None
        card = state.cards.get(card_id)
        if card is None:
            return "stale", None
        from looplab.search.card_selection import card_action
        current_action = card_action(card)
        if current_action is None or current_action != result.action:
            return "stale", None
        commit_action = {
            **current_action,
            **{
                name: value for name, value in selected_action.items()
                if isinstance(name, str) and name.startswith("_") and name != META_CARD_ID
            },
        }
        node_id = self._node_id_ceiling(events, state)
        reservation = self._prepare_existing_card_claim(
            events, state, commit_action, card, node_id,
        )
        if reservation is None or reservation.idea is None:
            return "stale", None
        if (
            reservation.idea.card_id != result.idea.card_id
            or reservation.idea.operator != result.idea.operator
            or reservation.idea.params != result.idea.params
            or reservation.idea.space != result.idea.space
            or reservation.idea.eval_profile != result.idea.eval_profile
            or reservation.idea.eval_timeout != result.idea.eval_timeout
        ):
            return "stale", None

        tail = events[-1].seq if events else -1
        try:
            with self._id_lock:
                self.store.append(
                    EV_NODE_BUILDING,
                    {
                        "node_id": reservation.node_id,
                        "operator": reservation.kind,
                        "parent_ids": reservation.parent_ids,
                        "card_id": reservation.card_id,
                        "speculative": True,
                        "card_build_generation": generation,
                    },
                    expected_last_seq=tail,
                )
        except EventStoreConcurrencyError:
            return "retry", None
        if "_scores" in commit_action:
            self.store.append(EV_POLICY_DECISION, {
                "scores": commit_action["_scores"],
                "chosen": commit_action.get("_chosen"),
                "reason": commit_action.get("_reason"),
            })
        self._append_rung_promotion(commit_action)
        try:
            self._create_node(
                commit_action,
                reserved=reservation,
                precoded=result,
                precoded_max_eval_seconds=max_eval_seconds,
            )
        except Exception as exc:
            # A telemetry failure after node_created still means the durable build committed.  A
            # pre-create exception owns a bare marker and must close it before the request advances.
            latest = fold(self.store.read_all())
            committed = latest.nodes.get(reservation.node_id)
            if (
                committed is not None
                and committed.idea.card_id == card_id
                and committed.speculative is True
                and committed.card_build_generation == generation
            ):
                return "created", committed.id
            if reservation.node_id in latest.buildings:
                self._fail_reserved_build(
                    node_id=reservation.node_id,
                    card_id=reservation.card_id,
                    generation=0,
                    error=f"speculative node commit failed: {type(exc).__name__}: {exc}"[:2_048],
                    reason="build_interrupted",
                )
            return "stale", None
        committed = fold(self.store.read_all()).nodes.get(reservation.node_id)
        if (
            committed is None
            or committed.idea.card_id != card_id
            or committed.speculative is not True
            or committed.card_build_generation != generation
        ):
            return "stale", None
        return "created", committed.id

    def _serve_card_builds(
        self,
        max_eval_seconds: Optional[float] = None,
        *,
        allow_commit: bool = True,
    ) -> bool:
        """Crash-recovery-first main-task service of one durable request."""

        self._ensure_speculation_state()
        state = fold(self.store.read_all())
        request = self._head_request(state)
        key = self._request_key(request)
        if request is None or key is None:
            return False
        recovered = self._matching_created_speculation(state, request)
        if recovered is not None:
            self._discard_spec_result(self._spec_builds.pop(key, None))
            return self._append_card_build_done(request, node_id=recovered.id)
        budget_exhausted = bool(
            max_eval_seconds is not None
            and state.total_eval_seconds >= max_eval_seconds
        )
        if (
            key[1] != state.search_epoch
            or self._terminal_intent(state)
            or budget_exhausted
            or not allow_commit
        ):
            self._discard_spec_result(self._spec_builds.pop(key, None))
            return self._append_card_build_done(request, skipped="stale")
        result = self._spec_builds.get(key)
        if result is None:
            return False
        if not result.success:
            self._discard_spec_result(self._spec_builds.pop(key, None))
            closed = self._append_card_build_done(request, skipped="producer_failed")
            if closed:
                self._spec_force_outer = True
            return closed
        outcome, node_id = self._claim_requested_card_build(
            request, result, max_eval_seconds,
        )
        if outcome == "retry":
            return False
        if outcome == "closed":
            self._discard_spec_result(self._spec_builds.pop(key, None))
            return True
        if outcome == "budget":
            # Keep both the durable head and its isolated result alive. A later add_nodes extension can
            # commit the exact paid result without rebuilding it or acknowledging the request as stale.
            return False
        self._discard_spec_result(self._spec_builds.pop(key, None))
        if outcome == "created" and node_id is not None:
            return self._append_card_build_done(request, node_id=node_id)
        return self._append_card_build_done(request, skipped="stale")

    def _close_card_build_before_terminal_gate(
        self,
        state: RunState,
        max_eval_seconds: Optional[float] = None,
    ) -> bool:
        """Attempt to settle one durable request before a pause/finish decision.

        The return value means a head existed, not that this single CAS attempt succeeded. Callers
        must restart the outer loop either way, so tail churn can never let finalization overtake an
        unacknowledged request. A crash prefix with an already-created Node records the success link;
        every other terminal-gated head is explicitly skipped.
        """

        if not self._speculation_enabled() or self._head_request(state) is None:
            return False
        self._serve_card_builds(max_eval_seconds, allow_commit=False)
        return True

    async def _produce_card_build(
        self,
        request: Mapping[str, Any],
        roles: tuple[Any, Any],
        notify,
    ) -> None:
        key = self._request_key(request)
        if key is None:
            return
        try:
            try:
                result = await anyio.to_thread.run_sync(
                    functools.partial(self._build_requested_card, dict(request), roles),
                    abandon_on_cancel=False,
                )
            except Exception as exc:  # the main task must still advance the durable gate
                result = SpecBuildResult(
                    key[0], key[1], {}, False, roles=roles,
                    error=f"{type(exc).__name__}: {exc}"[:2_048],
                )
            self._discard_spec_result(self._spec_builds.get(key))
            self._spec_builds[key] = result
        finally:
            self._spec_build_inflight.discard(key)
            # Notifications are only hints. Never let a full/closing stream block task-group teardown.
            try:
                notify.send_nowait(("producer", key))
            except (anyio.WouldBlock, anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass

    @in_llm_lane("build")
    def _prepare_raw_card_stage(
        self,
        action: Mapping[str, Any],
        proposal_events: list,
        proposal_state: RunState,
        proposal_node_ceiling: int,
        cue_fence: bytes,
        roles: tuple[Any, Any],
    ) -> SpecRawStageResult:
        """Worker-only proposal half: no selection-affecting event may escape this call."""

        raw_action = dict(action)
        generation = proposal_state.search_epoch
        proposal_authority_seq = self._proposal_authority_seq(proposal_events)
        researcher, developer = roles
        source = "engine" if raw_action.get("kind") == "merge" else "researcher"
        self._discard_node_build_telemetry(researcher=researcher, developer=developer)
        audit_events: list[tuple[str, dict, Optional[str], Optional[str]]] = []
        try:
            with self._capture_proposal_events() as captured:
                idea = self._prepare_node_idea(
                    raw_action,
                    proposal_state,
                    researcher=researcher,
                    prospective_node_id=proposal_node_ceiling,
                    source=source,
                    proposal_events=proposal_events,
                )
                audit_events.extend(captured)
            steering = tuple(getattr(researcher, "_steering_context", []) or [])
            receipt = bounded_cross_run_advisory_receipt(
                getattr(researcher, "_cross_run_advisory_receipt", {}) or {}
            )
            return SpecRawStageResult(
                generation=generation,
                action=raw_action,
                proposal_state=proposal_state,
                proposal_authority_seq=proposal_authority_seq,
                proposal_node_ceiling=proposal_node_ceiling,
                at_node=proposal_node_ceiling,
                source=source,
                cue_fence=cue_fence,
                success=idea is not None,
                idea=idea,
                steering_context=steering,
                cross_run_receipt=receipt,
                audit_events=tuple(audit_events),
                error="proposal rejected" if idea is None else "",
            )
        except Exception as exc:
            return SpecRawStageResult(
                generation=generation,
                action=raw_action,
                proposal_state=proposal_state,
                proposal_authority_seq=proposal_authority_seq,
                proposal_node_ceiling=proposal_node_ceiling,
                at_node=proposal_node_ceiling,
                source=source,
                cue_fence=cue_fence,
                success=False,
                audit_events=tuple(audit_events),
                error=f"{type(exc).__name__}: {exc}"[:2_048],
            )
        finally:
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)

    async def _produce_raw_card_stage(
        self,
        action: Mapping[str, Any],
        proposal_events: list,
        proposal_state: RunState,
        proposal_node_ceiling: int,
        cue_fence: bytes,
        roles: tuple[Any, Any],
        notify,
    ) -> None:
        try:
            try:
                result = await anyio.to_thread.run_sync(
                    functools.partial(
                        self._prepare_raw_card_stage,
                        dict(action),
                        proposal_events,
                        proposal_state,
                        proposal_node_ceiling,
                        cue_fence,
                        roles,
                    ),
                    abandon_on_cancel=False,
                )
            except Exception as exc:
                # Mirror the request-driven producer guard: one raw proposal fault yields a consumed,
                # non-staged result instead of tearing down the task group and cancelling live evals.
                try:
                    self._discard_node_build_telemetry(
                        researcher=roles[0], developer=roles[1],
                    )
                except Exception:
                    pass
                result = SpecRawStageResult(
                    generation=proposal_state.search_epoch,
                    action=dict(action),
                    proposal_state=proposal_state,
                    proposal_authority_seq=self._proposal_authority_seq(proposal_events),
                    proposal_node_ceiling=proposal_node_ceiling,
                    at_node=proposal_node_ceiling,
                    source="engine" if action.get("kind") == "merge" else "researcher",
                    cue_fence=cue_fence,
                    success=False,
                    error=f"{type(exc).__name__}: {exc}"[:2_048],
                )
            self._spec_raw_stage_result = result
        finally:
            self._spec_raw_stage_inflight = False
            try:
                notify.send_nowait(("raw_proposal", proposal_state.search_epoch))
            except (anyio.WouldBlock, anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass

    def _serve_raw_card_stage(self) -> tuple[bool, bool]:
        """Main-task-only commit of one prepared proposal and its buffered audit intents."""

        result = self._spec_raw_stage_result
        if result is None:
            return False, False
        self._spec_raw_stage_result = None
        if not result.success or result.idea is None:
            return True, False
        card_id = self._stage_prepared_card(
            result.action,
            result.idea,
            proposal_state=result.proposal_state,
            proposal_authority_seq=result.proposal_authority_seq,
            proposal_node_ceiling=result.proposal_node_ceiling,
            at_node=result.at_node,
            source=result.source,
            steering_context=result.steering_context,
            cross_run_receipt=result.cross_run_receipt,
            proposal_cue_fence=result.cue_fence,
        )
        if card_id is None:
            return True, False
        for event_type, data, trace_id, span_id in result.audit_events:
            self.store.append(
                event_type,
                data,
                trace_id=trace_id,
                span_id=span_id,
            )
        return True, True

    async def _close_developer_sentinel_once(self) -> bool:
        """Recover one sentinel lifecycle without ever re-pausing an acknowledged crash."""

        events = self.store.read_all()
        state = fold(events)
        pending = next(
            (candidate for candidate in state.pending_nodes()
             if self._developer_sentinel(candidate)),
            None,
        )
        records: list[tuple[str, dict[str, Any]]]
        if pending is not None:
            node = pending
            records = [
                (EV_NODE_FAILED, {
                    "node_id": node.id,
                    "generation": node.attempt,
                    "error": node.code,
                    "reason": "developer_crash",
                    "eval_seconds": 0.0,
                }),
                (EV_PAUSE, {
                    "node_id": node.id,
                    "generation": node.attempt,
                    "reason": "auto-paused: recovered a Developer crash before GPU dispatch",
                }),
            ]
        else:
            # A legacy writer (or a crash in the old two-append path) may already have made the
            # sentinel terminal while losing only its pause. Folded ``paused`` cannot distinguish
            # that gap from a pause which was appended and then explicitly resumed, so inspect the
            # exact node/generation history after the terminal sequence.
            node = next(
                (
                    candidate for candidate in state.nodes.values()
                    if self._developer_sentinel(candidate)
                    and candidate.status is NodeStatus.failed
                    and candidate.error_reason == "developer_crash"
                    and candidate.id not in state.aborted_nodes
                    and not candidate.tombstoned
                    and type(candidate.terminal_event_seq) is int
                    and not self._has_exact_developer_pause(
                        events,
                        node_id=candidate.id,
                        generation=candidate.attempt,
                        after_seq=candidate.terminal_event_seq,
                    )
                ),
                None,
            )
            if node is None:
                return False
            records = [
                (EV_PAUSE, {
                    "node_id": node.id,
                    "generation": node.attempt,
                    "reason": "auto-paused: recovered a terminal Developer crash",
                }),
            ]
        tail = events[-1].seq if events else -1
        try:
            async with self._write_lock:
                self.store.append_many(records, expected_last_seq=tail)
            self._create_paused = True
            return True
        except EventStoreConcurrencyError:
            return True

    async def _drop_stale_speculation(
        self,
        *,
        eval_inflight: set[tuple[int, int]] | frozenset[tuple[int, int]] = frozenset(),
    ) -> bool:
        """Drop at most one stale, not-yet-running speculative node from a fresh fold."""

        if not self._speculation_enabled():
            return False
        events = self.store.read_all()
        state = fold(events)
        if self._terminal_intent(state):
            return False
        self._refresh_speculation_budget(state)
        excluded = self._speculative_card_ids(state)
        ignored_pending = self._acknowledged_pending_ids(state)
        envelope = self._resource_envelope()
        for node in self._speculative_pending_nodes(state):
            if (node.id, node.attempt) in eval_inflight:
                continue  # burn-to-terminal once GPU dispatch has started
            card_id = node.idea.card_id
            if not isinstance(card_id, str):
                continue
            if speculative_card_is_fresh(
                state,
                self.policy,
                self._speculative_selection_node_limit(state),
                card_id=card_id,
                node_id=node.id,
                scoring=getattr(self, "_card_scoring", None),
                excluded_card_ids=excluded,
                ignored_pending_node_ids=ignored_pending,
                resource_envelope=envelope,
                consumed_inflight=eval_inflight,
            ):
                continue
            tail = events[-1].seq if events else -1
            try:
                async with self._write_lock:
                    self.store.append(
                        EV_NODE_FAILED,
                        {
                            "node_id": node.id,
                            "generation": node.attempt,
                            "error": CARD_FRESHNESS_SUPERSEDED_ERROR,
                            "reason": "superseded",
                            "eval_seconds": 0.0,
                        },
                        expected_last_seq=tail,
                    )
                return True
            except EventStoreConcurrencyError:
                return True  # force a fresh fold before any scorer consult
        return False

    async def _run_card_session(
        self,
        evals: list,
        state: RunState,
        max_es: Optional[float],
        wall_deadline: Optional[float] = None,
    ) -> None:
        """Continuously overlap the folded-log consumer with one isolated Card producer."""

        if not self._speculation_enabled():
            await self._dispatch_evals(evals, state, max_es)
            return
        self._ensure_speculation_state()
        eval_inflight: set[tuple[int, int]] = set()
        research_spawned = bool(evals)
        consumer_completed = False
        yield_outer = False
        send, receive = anyio.create_memory_object_stream(256)

        def _budget_exhausted(current: RunState) -> bool:
            return bool(
                (max_es is not None and current.total_eval_seconds >= max_es)
                or (wall_deadline is not None and time.time() >= wall_deadline)
            )

        def _needs_outer_rebuild(node) -> bool:
            return node.rerun_from in {"implement", "propose"}

        def _admissible(node, current: RunState) -> bool:
            return bool(
                node.id not in {node_id for node_id, _generation in eval_inflight}
                and not self._developer_sentinel(node)
                and node.id not in current.aborted_nodes
                and not _needs_outer_rebuild(node)
                # A speculative node is not consumer-owned until the matching durable done-link
                # exists. If its append raced, crash recovery keeps retrying the request head first.
                and (
                    not node.speculative
                    or node.attempt != 0
                    or self._speculative_link_matches(current, node)
                )
            )

        async def _eval_one(node_id: int, generation: int, reservation: Optional[dict]) -> None:
            nonlocal consumer_completed
            try:
                await self._evaluate(node_id, anyio.CapacityLimiter(1), max_es)
            finally:
                # One terminal/attempt boundary closes this admitted batch. Existing children still
                # burn to terminal, but no later scorer/admission may bypass outer controls/cadences.
                consumer_completed = True
                if reservation is not None:
                    self._clear_eval_resource_reservation(node_id, generation)
                    self._release_gpus(reservation.get("gpu_ids"))
                eval_inflight.discard((node_id, generation))
                try:
                    send.send_nowait(("eval", (node_id, generation)))
                except (anyio.WouldBlock, anyio.ClosedResourceError, anyio.BrokenResourceError):
                    pass

        async with anyio.create_task_group() as bg_tg:
            if evals:
                self._spawn_research(bg_tg, state)
            try:
                async with send, receive, anyio.create_task_group() as task_group:
                    def _start_head_producer(current: RunState) -> bool:
                        """Start the exact durable head in the same turn that elected it.

                        Waiting for the next loop turn leaves a request visible but not yet executing.
                        A fast admitted eval can then cross the search-epoch boundary first and make a
                        depth-one prefetch spuriously stale. Registering the producer before the next
                        checkpoint preserves the documented live-backlog overlap without changing the
                        durable request/commit authority.
                        """

                        nonlocal yield_outer
                        head = self._head_request(current)
                        key = self._request_key(head)
                        if (
                            head is None
                            or key is None
                            or key in self._spec_build_inflight
                            or key in self._spec_builds
                        ):
                            return False
                        if self._node_reservation_slots_remaining(
                            current, consume_request=True,
                        ) < 1:
                            return False
                        roles = self._producer_role_pair()
                        if roles is None:
                            if self._append_card_build_done(
                                head, skipped="producer_failed",
                            ):
                                yield_outer = True
                                return True
                            return False
                        self._spec_build_inflight.add(key)
                        try:
                            task_group.start_soon(
                                self._produce_card_build,
                                dict(head),
                                roles,
                                send,
                            )
                        except BaseException:
                            self._spec_build_inflight.discard(key)
                            raise
                        return True

                    while True:
                        progressed = False
                        if await self._close_developer_sentinel_once():
                            progressed = True
                        raw_consumed, raw_staged = self._serve_raw_card_stage()
                        if raw_consumed:
                            progressed = True
                            if raw_staged and not consumer_completed and not yield_outer:
                                if self._request_card_build(
                                    consumed_inflight=eval_inflight,
                                ):
                                    _start_head_producer(fold(self.store.read_all()))
                                else:
                                    # A durable request, not Card reuse alone, is the success boundary.
                                    # Return to the outer selector instead of repeating a paid proposal.
                                    yield_outer = True
                            else:
                                yield_outer = True
                        current = fold(self.store.read_all())
                        self._discard_orphaned_spec_results(current)
                        aborted = next(
                            (
                                node for node in current.pending_nodes()
                                if node.id in current.aborted_nodes
                                and node.id not in {
                                    node_id for node_id, _generation in eval_inflight
                                }
                            ),
                            None,
                        )
                        if aborted is not None and self._skip_if_aborted(
                            {"node_id": aborted.id}, current,
                        ):
                            progressed = True
                            current = fold(self.store.read_all())

                        outer_rebuild = any(
                            _needs_outer_rebuild(node) for node in current.pending_nodes()
                        )
                        terminal_gate = self._terminal_intent(current)
                        budget_exhausted = _budget_exhausted(current)
                        if (
                            not terminal_gate
                            and not budget_exhausted
                            and not outer_rebuild
                            # An eval terminal closes this admitted batch.  Leave its already-built next
                            # Node untouched for the outer control/Strategist/cadence boundary; freshness
                            # will re-run from that fresh outer turn.  A pre-decided serial fallback has
                            # the same boundary semantics while its admitted eval burns to terminal.
                            and not consumer_completed
                            and not yield_outer
                            and await self._drop_stale_speculation(
                                eval_inflight=eval_inflight,
                            )
                        ):
                            # The gate drops one Node per CAS. Drain the whole stale prefix before any
                            # later Card scorer consult sees a partially-clean selection state.
                            await anyio.sleep(0)
                            continue

                        current = fold(self.store.read_all())
                        head = self._head_request(current)
                        key = self._request_key(head)
                        if head is not None and key is not None:
                            if self._serve_card_builds(
                                max_es,
                                allow_commit=not (
                                    terminal_gate or budget_exhausted or outer_rebuild
                                ),
                            ):
                                progressed = True
                                if self._spec_force_outer:
                                    yield_outer = True
                                    self._spec_force_outer = False
                            else:
                                current = fold(self.store.read_all())
                                head = self._head_request(current)
                                key = self._request_key(head)
                                if (
                                    head is not None
                                    and key is not None
                                    and not terminal_gate
                                    and not budget_exhausted
                                    and not outer_rebuild
                                    and key not in self._spec_build_inflight
                                    and key not in self._spec_builds
                                ):
                                    if _start_head_producer(current):
                                        progressed = True

                        current = fold(self.store.read_all())
                        outer_rebuild = any(
                            _needs_outer_rebuild(node) for node in current.pending_nodes()
                        )
                        terminal_gate = self._terminal_intent(current)
                        budget_exhausted = _budget_exhausted(current)
                        if (
                            not terminal_gate
                            and not budget_exhausted
                            and not outer_rebuild
                            and not consumer_completed
                            and not yield_outer
                        ):
                            selection_changed = False
                            while len(eval_inflight) < max(1, int(self.max_parallel)):
                                current = fold(self.store.read_all())
                                if self._terminal_intent(current) or _budget_exhausted(current):
                                    budget_exhausted = True
                                    break
                                if any(
                                    _needs_outer_rebuild(node) for node in current.pending_nodes()
                                ):
                                    outer_rebuild = True
                                    break
                                candidates = [node for node in current.pending_nodes()
                                              if _admissible(node, current)]
                                if not candidates:
                                    break
                                chosen = None
                                reservation = None
                                for candidate in candidates:
                                    got = self._try_reserve_node_resources(
                                        candidate,
                                        resource_pin=self._card_resource_pin_for_node(
                                            current, candidate),
                                    )
                                    if got is not None:
                                        chosen, reservation = candidate, got
                                        break
                                if chosen is None:
                                    break
                                admission = fold(self.store.read_all())
                                live = admission.nodes.get(chosen.id)
                                if (
                                    self._terminal_intent(admission)
                                    or _budget_exhausted(admission)
                                    or any(
                                        _needs_outer_rebuild(node)
                                        for node in admission.pending_nodes()
                                    )
                                    or live is None
                                    or live.attempt != chosen.attempt
                                    or live.status is not NodeStatus.pending
                                    or not _admissible(live, admission)
                                ):
                                    self._release_gpus(reservation.get("gpu_ids"))
                                    break
                                current = admission
                                chosen = live
                                if not self._node_resource_reservation_is_current(
                                    current, chosen, reservation,
                                ):
                                    # An operator may change the Card pin between the fit scan and this
                                    # fresh admission fold. Never launch with a reservation formed for
                                    # the old quantities; release it and rescan against current truth.
                                    self._release_gpus(reservation.get("gpu_ids"))
                                    progressed = True
                                    selection_changed = True
                                    break
                                # Freshness was checked above, but a resource wait/earlier admission may
                                # have moved selection. Re-check immediately before the GPU child starts.
                                if self._speculative_link_matches(current, chosen):
                                    fresh = speculative_card_is_fresh(
                                        current,
                                        self.policy,
                                        self._speculative_selection_node_limit(current),
                                        card_id=chosen.idea.card_id,
                                        node_id=chosen.id,
                                        scoring=getattr(self, "_card_scoring", None),
                                        excluded_card_ids=self._speculative_card_ids(current),
                                        ignored_pending_node_ids=self._acknowledged_pending_ids(current),
                                        resource_envelope=self._resource_envelope(),
                                        consumed_inflight=eval_inflight,
                                    )
                                    if not fresh:
                                        self._release_gpus(reservation.get("gpu_ids"))
                                        if await self._drop_stale_speculation(
                                            eval_inflight=eval_inflight,
                                        ):
                                            progressed = True
                                            selection_changed = True
                                        break
                                if not research_spawned:
                                    self._spawn_research(bg_tg, current)
                                    research_spawned = True
                                self._register_eval_resource_reservation(
                                    chosen.id, chosen.attempt, reservation,
                                )
                                eval_inflight.add((chosen.id, chosen.attempt))
                                try:
                                    task_group.start_soon(
                                        _eval_one, chosen.id, chosen.attempt, reservation,
                                    )
                                except BaseException:
                                    eval_inflight.discard((chosen.id, chosen.attempt))
                                    self._clear_eval_resource_reservation(
                                        chosen.id, chosen.attempt,
                                    )
                                    self._release_gpus(reservation.get("gpu_ids"))
                                    raise
                                progressed = True
                            if selection_changed:
                                await anyio.sleep(0)
                                continue

                        current = fold(self.store.read_all())
                        outer_rebuild = any(
                            _needs_outer_rebuild(node) for node in current.pending_nodes()
                        )
                        terminal_gate = self._terminal_intent(current)
                        budget_exhausted = _budget_exhausted(current)
                        consumer_active = bool(
                            eval_inflight
                            or any(_admissible(node, current) for node in current.pending_nodes())
                        )
                        if (
                            consumer_active
                            and not terminal_gate
                            and not budget_exhausted
                            and not outer_rebuild
                            and not consumer_completed
                            and not yield_outer
                            and self._head_request(current) is None
                            and not self._spec_build_inflight
                            and not self._spec_raw_stage_inflight
                            and self._spec_raw_stage_result is None
                            and self._speculation_depth_used(
                                current,
                                consumed_inflight=eval_inflight,
                            ) < self.speculation_depth
                        ):
                            # `_request_card_build` consults the Card scorer. Drain any newly-stale
                            # speculative prefix immediately before that consult, not just per session turn.
                            if await self._drop_stale_speculation(
                                eval_inflight=eval_inflight,
                            ):
                                await anyio.sleep(0)
                                continue
                            requested = self._request_card_build(
                                consumed_inflight=eval_inflight,
                            )
                            if not requested:
                                # No durable Card owns the counterfactual next action. Propose and stage
                                # that raw lane in the main task while GPU children continue in worker
                                # threads; then request the exact receipt from a fresh fold. Card staging
                                # owns its own tail/generation/parent CAS and may safely decline a stale
                                # proposal if an eval changes the search state during the paid call.
                                # Selection and proposal share one immutable log snapshot.  A second
                                # read here would let an old raw action inherit a newer best/parent/cue
                                # fence and make the main-task commit validate the wrong authority.
                                proposal_events = self.store.read_all()
                                proposal_state = fold(proposal_events)
                                if (
                                    self._head_request(proposal_state) is None
                                    and self._speculation_depth_used(
                                        proposal_state,
                                        consumed_inflight=eval_inflight,
                                    ) < self.speculation_depth
                                ):
                                    raw_actions = speculative_raw_actions(
                                        proposal_state,
                                        self.policy,
                                        self._speculative_selection_node_limit(proposal_state),
                                        scoring=getattr(self, "_card_scoring", None),
                                        excluded_card_ids=self._speculative_card_ids(
                                            proposal_state),
                                        ignored_pending_node_ids=self._acknowledged_pending_ids(
                                            proposal_state),
                                        resource_envelope=self._resource_envelope(),
                                    )
                                    roles = self._producer_role_pair()
                                    if raw_actions and roles is not None:
                                        proposal_node_ceiling = self._node_id_ceiling(
                                            proposal_events, proposal_state,
                                        )
                                        self._spec_raw_stage_inflight = True
                                        task_group.start_soon(
                                            self._produce_raw_card_stage,
                                            dict(raw_actions[0]),
                                            proposal_events,
                                            proposal_state,
                                            proposal_node_ceiling,
                                            self._proposal_cue_fence(proposal_state),
                                            roles,
                                            send,
                                        )
                                        progressed = True
                                    else:
                                        # Unsupported raw interception (or no isolated pair) must
                                        # degrade at the outer serial boundary, never poll/re-propose.
                                        yield_outer = True
                            if requested:
                                _start_head_producer(fold(self.store.read_all()))
                                progressed = True

                        current = fold(self.store.read_all())
                        self._discard_orphaned_spec_results(current)
                        outer_rebuild = any(
                            _needs_outer_rebuild(node) for node in current.pending_nodes()
                        )
                        terminal_gate = self._terminal_intent(current)
                        budget_exhausted = _budget_exhausted(current)
                        pending_ready = any(
                            _admissible(node, current) for node in current.pending_nodes()
                        )
                        outstanding = bool(self._outstanding_requests(current))
                        building = bool(current.buildings)
                        memory_pending = bool(
                            self._spec_build_inflight
                            or self._spec_builds
                            or self._spec_raw_stage_inflight
                            or self._spec_raw_stage_result is not None
                        )
                        if terminal_gate or budget_exhausted:
                            if not any((eval_inflight, outstanding, building, memory_pending)):
                                break
                        elif outer_rebuild:
                            if not any((eval_inflight, outstanding, building, memory_pending)):
                                break
                        elif consumer_completed or yield_outer:
                            if not any((eval_inflight, outstanding, building, memory_pending)):
                                break
                        elif not any((
                            eval_inflight, pending_ready, outstanding, building, memory_pending,
                        )):
                            break

                        if progressed:
                            await anyio.sleep(0)
                            continue
                        # Notifications are only wake-ups.  The next turn always re-folds the log and
                        # derives truth again; a finite poll also observes operator events, which do not
                        # write into this process-local wake-up stream.
                        with anyio.move_on_after(0.5):
                            await receive.receive()
            finally:
                if getattr(self, "_concurrent_research_repeat", False):
                    bg_tg.cancel_scope.cancel()
