"""The operator's forced steering, served from its durable request queues (review 2026-09-22,
ENG1-04 step 4b).

A fork, an injected node, a forced ablation and a confirm request are INTENTS in the log
(`fork_requests`, `inject_requests`, `ablate_request_generations`, `confirm_request_generations`),
served one per loop turn by `_serve_forced_requests` and each gated on the domain event it produces
(`fork_done`, `inject_done`, an ablation event, `node_confirmed`), so a resume never repeats one
(invariant #3). A head that would create a node waits DURABLY for node budget
(`_defer_for_node_budget`: the request itself stays the queue authority, so the wait survives a
restart), and a head the terminal gate would strand is closed first
(`_close_node_creating_forced_request_before_terminal_gate`).

Moved out of `orchestrator.py` byte for byte; `Engine` inherits the mixin and no call site changed.
What stays there: the run loop that decides WHEN a request is served. What these controls start is
reached through `self` — the builds (`_offload_node_build`, `_create_injected_node`), the ablation
and the confirm phase — and so is every fold, through the engine's one seam below.
"""
from __future__ import annotations

import functools
import logging
import time
from typing import Optional

import anyio

from looplab.core.containment import contain
from looplab.core.errors import budget_stop_leaf
from looplab.core.llm import BudgetExceeded
from looplab.core.models import NodeStatus, RunState
from looplab.engine.card_reservation import RESERVATION_VERDICTS
# Through the ENGINE's fold seam, not `replay.fold` directly — see `shared.py::engine_fold`.
from looplab.engine.shared import engine_fold as fold
from looplab.engine.speculation import notify_producer
from looplab.events.eventstore import retry_tail_cas
from looplab.events.finalize_scope import incomplete_finalize_scope
from looplab.events.types import (EV_ABLATE, EV_FORK_DONE, EV_FORK_UNFULFILLED, EV_INJECT_DONE,
                                  EV_INJECT_FAILED)

# Poll geometry for a durable node-creating control head parked on an exhausted node budget
# (`Engine._defer_for_node_budget`). Starts at the historical tick and doubles up to the ceiling, so
# a multi-hour wait costs O(log(wait)) full-log refolds instead of two per second.
_LOG = logging.getLogger(__name__)

_BUDGET_WAIT_MIN_S = 0.5
_BUDGET_WAIT_MAX_S = 4.0


class ForcedRequestsMixin:
    """The operator's node-creating and node-judging controls, served one queue head at a time."""

    async def _defer_for_node_budget(self, state: RunState) -> bool:
        """Keep a durable node-creating control head live until ``budget_extend`` admits it.

        Returning immediately as "not served" would let the empty-action branch finalize with a
        stranded fork/inject/ablation request. Returning immediately as "served" would tight-spin.
        One bounded poll turn keeps the engine responsive to abort/pause/budget controls and survives
        process restart because the request itself remains the append-only queue authority.
        """

        if self._node_reservation_slots_remaining(state) >= 1:
            self._budget_wait_s = _BUDGET_WAIT_MIN_S      # the wait ended; the next one starts short
            return False
        # BACK OFF geometrically. Each tick makes the main loop re-read and re-fold the ENTIRE event
        # log (plus the per-turn ack/mirror re-reads), and this head can wait hours for an operator's
        # budget_extend — a fixed 0.5s tick meant hours of O(total-events) busy-polling on a long log,
        # the same cost class the resource-wait comment in _dispatch_evals flags. The ceiling is
        # small enough that abort/pause/budget controls are still observed within a few seconds,
        # which is what "keeps the engine responsive" above actually requires.
        delay = getattr(self, "_budget_wait_s", _BUDGET_WAIT_MIN_S)
        await anyio.sleep(delay)
        self._budget_wait_s = min(delay * 2.0, _BUDGET_WAIT_MAX_S)
        return True

    @staticmethod
    def _pending_forced_ablation(state: RunState) -> Optional[dict]:
        """Return the first exact forced-ablation lifecycle not yet acknowledged."""

        forced = next((r for r in state.ablate_request_generations
                       if r.get("node_id") in state.nodes
                       and r.get("node_id") not in state.aborted_nodes
                       and not state.nodes[r["node_id"]].tombstoned
                       and state.nodes[r["node_id"]].attempt == r.get("generation")
                       and not any(a.get("parent_id") == r["node_id"]
                                   and a.get("generation") == r.get("generation")
                                   for a in state.ablations)), None)
        if forced is not None:
            return dict(forced)
        legacy = next((parent_id for parent_id in state.ablate_requests
                       if parent_id in state.nodes
                       and parent_id not in state.aborted_nodes
                       and not state.nodes[parent_id].tombstoned
                       and not any(a.get("parent_id") == parent_id
                                   for a in state.ablations)), None)
        if legacy is None:
            return None
        return {
            "node_id": legacy,
            "generation": state.nodes[legacy].attempt,
        }

    def _append_inject_failure(
        self,
        state: RunState,
        *,
        error: str,
        reason: str,
    ) -> bool:
        """Atomically append one positional inject failure and its replay gate."""

        request_idx = state.injects_done

        def _plan(events, tail) -> bool:
            current = fold(events)
            if current.injects_done > request_idx:
                return True
            if (
                current.injects_done != request_idx
                or len(current.inject_requests) <= request_idx
            ):
                return False
            self.store.append_many([
                (EV_INJECT_FAILED, {
                    "idx": request_idx,
                    "error": str(error)[:500],
                    "reason": reason,
                }),
                (EV_INJECT_DONE, {
                    "idx": request_idx,
                    "skipped": reason,
                }),
            ], expected_last_seq=tail)
            return True

        # The counter pair did not advance, so the request is still open and the next turn retries it.
        return retry_tail_cas(self.store, _plan, on_exhaust=lambda: False)

    def _close_node_creating_forced_request_before_terminal_gate(
        self,
        state: RunState,
        *,
        reason: str,
    ) -> bool:
        """Durably skip one forced Node creator before a stronger terminal budget wins.

        Wall/eval ceilings intentionally outrank operator work, but finalizing without a matching
        acknowledgement leaves a replay-visible queue head stranded in a finished run. Close one head
        per turn, then re-fold before the terminal CAS. A node-budget-only wait never calls this helper
        and therefore remains resumable via ``budget_extend{add_nodes}``.
        """

        if len(state.fork_requests) > state.forks_done:
            request = state.fork_requests[state.forks_done]
            self.store.append(EV_FORK_DONE, {
                # The POSITION is the receipt's identity (`_advance_request_cursor`): `from_node_id`
                # cannot separate two queued forks of the same parent, which is the ordinary pattern.
                "idx": state.forks_done,
                "from_node_id": request.get("from_node_id"),
                "generation": request.get("generation"),
                "skipped": reason,
            })
            return True
        if len(state.inject_requests) > state.injects_done:
            self._append_inject_failure(
                state,
                error=f"not executed: terminal {reason} gate won",
                reason=reason,
            )
            # A lost tail CAS still means this head blocks finalization. Re-fold and retry next turn.
            return True
        forced_ablate = self._pending_forced_ablation(state)
        if forced_ablate is not None:
            self.store.append(EV_ABLATE, {
                "parent_id": forced_ablate["node_id"],
                "generation": forced_ablate["generation"],
                "impacts": {},
                "eval_seconds": 0.0,
                "skipped": reason,
            })
            return True
        return False

    async def _serve_forced_requests(self, state: RunState) -> bool:
        # Operator-forced steering (Phase 5), one per iteration then re-fold. Each is gated on
        # the domain event it produces (fork_done / an ablate event / node_confirmed), so a
        # resume never repeats it — deterministic under replay. Returns True when a request was
        # served OR deliberately left pending for node budget (the caller re-folds via `continue`);
        # False lets the loop fall through. The pending branch performs its own bounded wait.
        if len(state.fork_requests) > state.forks_done:
            req = state.fork_requests[state.forks_done]
            pid = req.get("from_node_id")
            generation = req.get("generation")
            current = state.nodes.get(pid)
            # Unstamped queued-before-create requests are historical and bind when their node appears.
            # Every modern producer stamps, so explicit generations remain strict CAS.
            served = (current is not None and not current.tombstoned
                      and pid not in state.aborted_nodes
                      and (generation is None or current.attempt == generation))
            if served:
                # A valid fork remains the durable queue head while the physical Node ceiling is full.
                # Do not append fork_done: a later budget_extend must be able to serve this same intent.
                if await self._defer_for_node_budget(state):
                    return True
                generation = current.attempt
                # CLAIM THE REQUEST BEFORE THE PAID PRODUCER — the same at-most-once boundary
                # `_claim_paid_finalize_step` states ("persist the boundary before dispatching a
                # paid/external effect"). `_create_node` runs the Researcher + Developer (real spend) and
                # durably appends `node_created`; with the receipt written AFTER it, a crash in that gap
                # left the request still at the queue head, so resume re-served it and minted a SECOND
                # paid child for the same fork. Ordering the receipt first makes the failure mode
                # at-most-once: a crash in the gap loses ONE queued fork intent instead of duplicating an
                # experiment and its spend — and an operator can simply re-request the fork, whereas a
                # duplicate is silent, already charged, and pollutes the tree.
                # Fold-safe: `_on_fork_done` advances only the fork cursor and `_on_node_created` only
                # the node table, so the swap is order-tolerant. It is NOT byte-identical on every old
                # log: `_on_fork` drops a request whose parent is tombstoned/aborted at that point in
                # the replay, and the cursor is now bounded by the queue it indexes, so a historical
                # receipt for a request the current fold declines no longer advances past it.
                self.store.append(EV_FORK_DONE, {
                    "idx": state.forks_done, "from_node_id": pid, "generation": generation})
                # Beyond the crash-in-the-gap above, `_create_node` can also decline SILENTLY in a
                # live process: `_reserve_node_build` returns None on a lost proposal-authority CAS, a
                # slot race or `paused`, and the novelty/card-contract gate can drop the proposal — it
                # then simply returns. The receipt is already spent, so the operator's request used to
                # vanish with the Researcher call paid and NOTHING in the log saying the fork produced
                # nothing. Record that. Fold-ignored (see EV_FORK_UNFULFILLED), so the cursor and every
                # selection input are untouched and the append stays splice-neutral by construction;
                # this only makes the drop legible. Re-fold rather than trusting a cached state
                # (invariant 4) and look for a node PARENTED ON `pid` past the pre-call ceiling: a
                # concurrent parallel-build sibling can add an unrelated node in the same window, and
                # miscounting that as success is the safe direction (it only stays quiet).
                before = {n.id for n in fold(self.store.read_all()).nodes.values()}
                await self._offload_node_build({"kind": "improve", "parent_id": pid,
                                                "parent_generations": {str(pid): generation}})
                after = fold(self.store.read_all()).nodes
                if not any(nid not in before and pid in (getattr(nd, "parent_ids", None) or [])
                           for nid, nd in after.items()):
                    self.store.append(EV_FORK_UNFULFILLED, {
                        "idx": state.forks_done, "from_node_id": pid, "generation": generation})
            else:
                self.store.append(EV_FORK_DONE, {
                    "idx": state.forks_done, "from_node_id": pid, "generation": generation,
                    "skipped": "stale_generation"})        # advance the gate past an unservable head
            return True
        # Operator-authored experiment (manual tree edit): the human hand-adds a node (an idea
        # + optional parent + optional ready-made code). Materialize it into a real pending node;
        # the policy then evaluates it next (pending nodes are scheduled first). Gated on
        # `inject_done` so a resume never re-creates it — deterministic under replay.
        if len(state.inject_requests) > state.injects_done:
            req = state.inject_requests[state.injects_done]
            # Reject a structurally impossible durable row before waiting for Node capacity.
            prepared = self._prepare_inject_head(state, req)
            if prepared is None:
                return True
            # Unlike malformed input, temporary budget exhaustion is not a failed inject. Leave the
            # request unacknowledged so an additive budget extension can admit it exactly once.
            if await self._defer_for_node_budget(state):
                return True
            reservation = self._reserve_and_claim_inject(state, prepared)
            if reservation is None:
                return True
            await self._materialize_claimed_inject(state, req, reservation)
            # A Developer crash inside the offloaded materializer queued its run-global pause
            # (`_request_create_pause`); THIS task appends it, and the caller's re-fold then sees it.
            self._drain_create_pause()
            return True
        forced_ablate = self._pending_forced_ablation(state)
        if forced_ablate is not None:
            # Ablation probes culminate in one new refine_block Node. Avoid both the paid probes and a
            # false completion while that physical reservation has no budget slot.
            if await self._defer_for_node_budget(state):
                return True
            await self._ablate(forced_ablate["node_id"],
                               expected_generation=forced_ablate["generation"])
            return True
        forced_confirm = next((r for r in state.confirm_request_generations
                               if r.get("node_id") in state.nodes
                               and r.get("node_id") not in state.aborted_nodes
                               and not state.nodes[r["node_id"]].tombstoned
                               and state.nodes[r["node_id"]].attempt == r.get("generation")
                               and state.nodes[r["node_id"]].status is NodeStatus.evaluated
                               and r not in state.confirmed_forced_generations), None)
        if forced_confirm is None:
            legacy_confirm = next((nid for nid in state.confirm_requests
                                   if nid in state.nodes
                                   and nid not in state.aborted_nodes
                                   and not state.nodes[nid].tombstoned
                                   and state.nodes[nid].status is NodeStatus.evaluated
                                   and nid not in state.confirmed_forced), None)
            if legacy_confirm is not None:
                forced_confirm = {"node_id": legacy_confirm,
                                  "generation": state.nodes[legacy_confirm].attempt}
        if forced_confirm is not None:
            await self._confirm_node(state.nodes[forced_confirm["node_id"]])
            return True
        return False

    def _reserve_and_claim_inject(self, state: RunState, prepared) -> Optional[object]:
        """The FREE half of serving the inject queue head: reserve, then spend its `inject_done`.

        Split out of `_serve_forced_requests` (2026-09-24) so the Card session can serve the same
        head through the same rule while a long producer build holds the outer loop
        (`_card_phase_serve_operator_inject`). Returns the reservation the PAID half builds on, or
        None when the head was spent with its diagnosis or left queued on a race. MAIN TASK ONLY:
        every append here is folded and run-global.
        """
        # RESERVE FIRST — the Card and `node_building`, on THIS main task, before the receipt
        # (review 2026-09-22, ENG1-07). The receipt used to come first and the reservation inside
        # the offloaded materializer, so a reservation that lost a race — a pause landing, a slot
        # taken, the proposal-authority CAS lost — raised "could not reserve", was recorded as
        # `materialization_failed`, and SPENT the operator's request although nothing had been
        # paid and nothing built (driven: `injects_done` 1, zero nodes, on a pause and on a slot
        # race). The reservation costs nothing, so it may precede the claim: a crash between the
        # two leaves a bare `node_building` that `_recover_interrupted_builds` closes at the next
        # entry, and the still-queued request is then served once, on a fresh id and Card.
        #
        # THE RULE FOR A REFUSAL, by the code `_reserve_node_build` names
        # (`card_reservation.py::RESERVATION_REFUSALS`): a RACE leaves the request queued,
        # because the next turn's own gate re-decides it — the loop head pauses or settles a
        # `halted` run, `_defer_for_node_budget` waits out `no_slot`, the validator above refuses a
        # parent that moved for good (it checks the reservation's exact build action, so a
        # `stale_parents` cannot recur on a request it admits), and a fresh fold re-reads the
        # anchor and re-runs the CAS. A VERDICT — the Card contract refuses the idea, or an
        # identical Card's work is already in flight — SPENDS it with `EV_INJECT_FAILED` naming
        # the code, since the same bytes are refused the same way every turn and retrying them
        # would spin the forced-request queue, and with it every eval dispatch, forever. A
        # refusal with NO code (a patched reservation) is spent too: what nobody can name,
        # nobody can promise a later turn will change.
        refusal: list[str] = []
        try:
            reservation = self._reserve_injected_node(state, prepared, refusal=refusal)
        except (TypeError, ValueError, OverflowError) as exc:
            # A hostile row the validator admitted and the Card writer cannot represent — the
            # triple `_plan_native_card` itself contains. Nothing was paid and nothing reserved
            # (the reservation's append is the last statement before it returns), so the request
            # is spent with its diagnosis instead of crash-looping the engine on a durable queue
            # head: the materializer's own rule, below. Anything else from this FREE half is a
            # bug or a broken store, and surfaces exactly as it does on the serial build path.
            self._append_inject_failure(
                state,
                error=str(exc),
                reason="materialization_failed",
            )
            return None
        if reservation is None:
            code = refusal[-1] if refusal else None
            if code is None or code in RESERVATION_VERDICTS:
                self._append_inject_failure(
                    state,
                    error=(f"not materialized: the Card reservation refused the idea ({code}) "
                           "— a verdict on the idea, not a race; re-inject a changed idea, or "
                           "this one once identical in-flight work has finished" if code else
                           "not materialized: the Card reservation refused the idea and named "
                           "no reason"),
                    reason=code or "reservation_refused",
                )
            return None
        # CLAIM THE REQUEST BEFORE THE PAID PRODUCER, exactly as the fork branch above does and
        # for the same reason. `_create_injected_node` can run a Developer session (real spend)
        # and durably appends `node_created`; with the receipt written AFTER it, a crash inside
        # that call left this request at the queue head, so resume re-served it and bought the
        # session again — and a crash between the durable `node_created` and the receipt re-served
        # too, where Card dedup then closed the SUCCEEDED inject as "materialization_failed" or
        # minted a duplicate node. Receipt-first makes the failure at-most-once: a crash in the
        # gap loses ONE queued inject intent instead of duplicating an already-charged experiment,
        # and the operator can simply re-request it. Fold-safe: `_on_inject_done` advances only
        # the inject cursor and `_on_node_created` only the node table, so the swap is
        # order-tolerant (invariant #3 — the side effect is gated on its event).
        # Still before the PAID producer, and now after the free reservation above: the paid half
        # stays at-most-once, and only a reservation that holds can spend the request.
        self.store.append(EV_INJECT_DONE, {"idx": state.injects_done})
        return reservation

    async def _materialize_claimed_inject(self, state: RunState, req: dict, reservation, *,
                                          lane: bool = False) -> None:
        """The PAID half of an inject whose receipt `_reserve_and_claim_inject` already spent.

        ``lane=False`` is the outer loop's serial serve: the spend ceiling propagates, and a failure
        sweeps every surviving build marker (nothing else is building on that path). A worker-queued
        pause is drained by the CALLER on the main task (`_drain_create_pause`).

        ``lane=True`` is the Card session's concurrent inject (`_card_phase_serve_operator_inject`),
        which differs in exactly the two ways a lane beside a live producer must: the ceiling is
        PARKED on the run's deferred-stop sink (`speculation.py::_run_isolated_producer`'s rule)
        instead of raised into the session's task group, and a failure closes only THIS
        reservation's marker, never the producer's.
        """
        try:
            # OFF THE LOOP THREAD, like every other build (review 2026-09-22, ENG1-07). An inject
            # with no ready-made code runs a paid Developer session, and this call used to run it
            # ON the event loop: measured, the inject's `implement` ticked the loop ZERO times
            # during a 0.3 s call while the ordinary builds beside it ticked 28-30 — no eval
            # watcher, abort/reset detection, train-monitor kill or control ACK for as long as
            # the session spends. The writes stay where invariant #1 wants them: the reservation
            # is made above on this task, `node_created` is the node's own licensed worker
            # append, and a crash pause is QUEUED and drained below.
            # `_create_injected_node(req, reservation=…)` stays the one seam this branch hands
            # the paid half to (tests patch it).
            await self._offload_build(functools.partial(
                self._create_injected_node, req, reservation=reservation))
        except BudgetExceeded as exc:
            # The run's stop, not a request that failed to materialize: the handler below used
            # to record the spend ceiling as `materialization_failed` and let the run go on
            # spending. The materializer has already closed its own reservation.
            if not lane:
                raise
            self._park_lane_budget_stop(exc)
        except Exception as e:  # noqa: BLE001 - a malformed operator/API inject must not
            # crash-loop the engine: the gate has already advanced, so this only records WHY the
            # (already-spent) request produced nothing. Terminalize any surviving build marker
            # first: the failure happened inside this same invocation, so unlike an escaping
            # serial build exception there may be no resume boundary to clean a partial
            # reservation.
            stop = budget_stop_leaf(e) if lane else None
            if stop is not None:
                self._park_lane_budget_stop(stop)
                return
            failed_state = fold(self.store.read_all())
            if lane:
                if reservation.node_id in failed_state.buildings:
                    self._fail_reserved_build(
                        node_id=reservation.node_id, card_id=reservation.card_id, generation=0,
                        error="injected node build raised before node creation",
                        reason="build_crash")
            elif failed_state.buildings:
                self._recover_interrupted_builds(failed_state)
            # NOT `_append_inject_failure`: that helper appends the failure AND the gate as one
            # atomic pair, and returns early when the gate has already moved — which it has,
            # three lines up. Append the diagnosis alone. `EV_INJECT_FAILED` is DIAGNOSTIC
            # (fold-ignored), so it changes no state either way; it carries `idx` so the log,
            # `looplab replay` and the trace still say which request produced nothing.
            self.store.append(EV_INJECT_FAILED, {
                "idx": state.injects_done,
                "error": str(e)[:500],
                "reason": "materialization_failed",
            })

    def _prepare_inject_head(self, state: RunState, req: dict):
        """Validate the inject queue head, or SPEND it as `invalid_request` and return None.

        The validator is pure/bounded and mirrors materialization; no Developer/LLM work occurs.
        """
        try:
            return self._prepare_injected_node(state, req)
        except Exception as exc:  # noqa: BLE001 - legacy/hand-authored event rows are untrusted
            self._append_inject_failure(
                state,
                error=str(exc),
                reason="invalid_request",
            )
            return None

    # ------------------------------------------------------------------ controls ON THE FLY
    #
    # THE DEFECT (minionerec-backbones-v8/v9, 2026-09-24). The server appends an operator control
    # intent (`inject_node`, `budget_extend`, `node_reset`, …) to the log at once, and its command
    # record then waits for the engine's causal `command_ack` (postcondition `engine_ack`). Both the
    # ack and every effect were drained ONLY at the run loop's head (`_ack_commands` +
    # `_apply_control_overrides` + `_serve_forced_requests`), and the loop head is not reached while
    # the main task sits inside one long step — a Card session whose isolated producer runs a
    # 10-40 min Developer session, a serial build, deep research. Measured on v9: an `inject_node`
    # appended at t=564 s was neither acked nor served 1,700 s later — the server record went
    # `timed_out` — because `_run_card_session` never returned (an eval was burning and it elected
    # card-2 the moment card-1 committed), and `budget_extend{parallel_build: 3}` could not help,
    # since the width it sets was read only at that same head and the session never serves injects.
    #
    # THREE PARTS, each replay-safe because each effect is still a pure function of the log:
    #   1. `_control_watch_loop`: a task on the engine's own event loop that, once the loop head has
    #      been absent for `_CONTROL_WATCH_GRACE_S`, re-applies `_apply_control_overrides` from a
    #      fresh fold (the same idempotent function the head runs every turn — widths, timeouts, the
    #      broker total, the per-eval budget) and appends the `command_ack` rows. `command_ack` is a
    #      DIAGNOSTIC event, the class invariant #1 lets a concurrent task append; nothing folded is
    #      written here, and a resume derives the same overrides from the same `budget_overrides`.
    #   2. `_card_phase_serve_operator_inject`: the Card session serves the inject queue head itself
    #      when the live build width (`_llm_parallel`, which part 1 just raised) has a free slot
    #      beside the producer — the free half (reservation + `inject_done`) on the session's main
    #      task, the paid half in a lane of the session's task group, exactly the serial rule.
    #   3. `_operator_node_request_ready`: otherwise the session stops electing new producer work
    #      while a node-creating operator request waits and hands the outer loop its turn as soon as
    #      the producer lane is idle, instead of starving the request behind card after card.
    _CONTROL_WATCH_POLL_S = 1.0
    _CONTROL_WATCH_GRACE_S = 3.0

    def _start_control_watch(self, task_group) -> None:
        """Start the on-the-fly control watcher in ``task_group`` under its own cancel scope."""
        scope = anyio.CancelScope()
        self._control_watch_scope = scope

        async def _watch() -> None:
            with scope:
                await self._control_watch_loop()

        task_group.start_soon(_watch)

    def _stop_control_watch(self) -> None:
        self._control_watch_armed = False
        scope = getattr(self, "_control_watch_scope", None)
        if scope is not None:
            scope.cancel()
        self._control_watch_scope = None

    def _mark_loop_head(self) -> None:
        """The run loop's head just acked on a stable prefix: the watcher owes nothing for a while."""
        self._control_watch_armed = True
        self._loop_head_monotonic = time.monotonic()

    async def _control_watch_loop(self) -> None:
        while True:
            await anyio.sleep(self._CONTROL_WATCH_POLL_S)
            try:
                self._control_watch_tick()
            except Exception as exc:  # noqa: BLE001 - a watcher fault must never cancel the run's evals
                # The loop head re-does everything this tick does on its next turn — the watcher is
                # only EARLIER, never the authority — so a failure here costs latency, not state.
                contain("control_watch_tick", exc)
                _LOG.warning("on-the-fly control drain failed (the loop head will retry): %s", exc)

    def _unacked_command_suffix(self, events) -> bool:
        """Is there a marked command intent past `_ack_commands`' cursor? Cheap: no fold."""
        start = 0
        if getattr(self, "_command_ack_initialized", False):
            cursor = int(getattr(self, "_command_ack_cursor", 0))
            if cursor <= len(events) and (
                    cursor == 0 or (events and events[0] is getattr(
                        self, "_command_ack_first_event", None))):
                start = cursor
        return any((event.data or {}).get("_command_id") for event in events[start:])

    def _control_watch_tick(self) -> bool:
        """Apply live-safe control effects and ACK durable intents while the loop head is away.

        Returns True when it acted. Runs on the event-loop thread between awaits of the main task,
        so it never interleaves with a synchronous section of the loop (the head's read → fold →
        `_ack_commands` has no await in it, which is what keeps the two ack writers from racing on
        `_ack_commands`' cursor).
        """
        if not getattr(self, "_control_watch_armed", False):
            return False
        idle = time.monotonic() - float(getattr(self, "_loop_head_monotonic", 0.0))
        if idle < self._CONTROL_WATCH_GRACE_S:
            return False
        events = self.store.read_all()
        if not self._unacked_command_suffix(events):
            return False
        state = fold(events)
        # Never inside a wrap-up: the quiet finalization suffix is a positional contract
        # (`events/finalize_protocol.py`), and a terminal run has no effects left to apply.
        if (state.finished or state.finalization_pending()
                or incomplete_finalize_scope(events) is not None):
            return False
        # The head re-checks the calibrated authority before it ACKs; so does this. A refusal
        # raises into the containment above and the head raises it for real on its next turn.
        self._require_pinned_speculation_receipt(state)
        if not getattr(self, "_speculation_gate_calibration", False):
            # The SAME function the head runs every turn, from the same fold: idempotent, and a
            # resume re-derives exactly these values from `state.budget_overrides`. What it sets —
            # `_eval_parallel`, `_llm_parallel`, `timeout`, `_eval_timeout_override`, the broker
            # total — is read live by the next admission/dispatch, so it takes effect mid-step. The
            # wall/eval CEILINGS it returns are gates the loop head evaluates; they stay there.
            self._apply_control_overrides(state)
        self._ack_commands(events)
        return True

    def _operator_node_request_ready(self, state: RunState) -> bool:
        """A node-creating operator request (fork/inject) is queued and a node slot can take it."""
        pending = (len(state.fork_requests) > state.forks_done
                   or len(state.inject_requests) > state.injects_done)
        return bool(pending and not state.halted
                    and self._node_reservation_slots_remaining(state) >= 1)

    def _build_lanes_busy(self) -> int:
        """Build slots this session holds right now: the Card producer, a raw proposal, inject lanes."""
        return (len(getattr(self, "_spec_build_inflight", None) or ())
                + int(bool(getattr(self, "_spec_raw_stage_inflight", False)))
                + int(getattr(self, "_inject_lanes_inflight", 0)))

    def _card_phase_serve_operator_inject(self, session) -> bool:
        """Serve the inject queue head BESIDE a running producer when the live build width allows.

        Only while producer work is in flight — with nothing in flight the session hands the request
        to the outer loop instead (`_operator_node_request_ready` in the exit decision), which serves
        it through `_serve_forced_requests` exactly as before. Never while a paid RAW proposal is in
        flight: its receipt fence reads the node-id ceiling this reservation would move, and the run
        has already paid for that proposal.
        """
        if (getattr(self, "_pending_create_pause", None)
                and not getattr(self, "_inject_lanes_inflight", 0)):
            # A finished lane's Developer crash queued the run-global pause; append it HERE, on the
            # session's main task (the seam `_drain_create_pause` exists for).
            self._drain_create_pause()
            session.progressed = True
            return True
        busy = self._build_lanes_busy()
        if (busy == 0 or getattr(self, "_spec_raw_stage_inflight", False)
                or busy >= max(1, int(self._llm_parallel))):
            return False
        state = self._session_state()
        if (len(state.inject_requests) <= state.injects_done or state.halted
                or self._session_gates(state, session).stopping
                or self._node_reservation_slots_remaining(state) < 1):
            return False
        req = state.inject_requests[state.injects_done]
        prepared = self._prepare_inject_head(state, req)
        session.progressed = True
        if prepared is None:
            return True
        reservation = self._reserve_and_claim_inject(state, prepared)
        session.progressed = True
        if reservation is None:
            return True
        self._inject_lanes_inflight = getattr(self, "_inject_lanes_inflight", 0) + 1
        try:
            session.task_group.start_soon(self._inject_lane, state, req, reservation, session.notify)
        except BaseException:
            # `start_soon` raises only at task-group teardown; the spent receipt then stands as the
            # serial path's crash-in-the-gap does (at-most-once), and the entry sweep closes the
            # bare marker.
            self._inject_lanes_inflight -= 1
            raise
        return True

    async def _inject_lane(self, state: RunState, req: dict, reservation, notify) -> None:
        """The paid half of a session-served inject, as a lane beside the Card producer."""
        try:
            await self._materialize_claimed_inject(state, req, reservation, lane=True)
        finally:
            self._inject_lanes_inflight -= 1
            notify_producer(notify, ("inject", state.injects_done))

    def _park_lane_budget_stop(self, stop: BaseException) -> None:
        """The run's deferred-stop sink, first stop wins (`_run_isolated_producer`'s rule)."""
        if self._eval_budget_stop is None:
            self._eval_budget_stop = stop
        self._eval_boundary_owed = True
