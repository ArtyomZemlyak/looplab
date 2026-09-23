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
from typing import Optional

import anyio

from looplab.core.llm import BudgetExceeded
from looplab.core.models import NodeStatus, RunState
from looplab.engine.card_reservation import RESERVATION_VERDICTS
# Through the ENGINE's fold seam, not `replay.fold` directly — see `shared.py::engine_fold`.
from looplab.engine.shared import engine_fold as fold
from looplab.events.eventstore import retry_tail_cas
from looplab.events.types import (EV_ABLATE, EV_FORK_DONE, EV_FORK_UNFULFILLED, EV_INJECT_DONE,
                                  EV_INJECT_FAILED)

# Poll geometry for a durable node-creating control head parked on an exhausted node budget
# (`Engine._defer_for_node_budget`). Starts at the historical tick and doubles up to the ceiling, so
# a multi-hour wait costs O(log(wait)) full-log refolds instead of two per second.
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
            # Reject a structurally impossible durable row before waiting for Node capacity. The
            # validator is pure/bounded and mirrors materialization; no Developer/LLM work occurs.
            try:
                prepared = self._prepare_injected_node(state, req)
            except Exception as exc:  # noqa: BLE001 - legacy/hand-authored event rows are untrusted
                self._append_inject_failure(
                    state,
                    error=str(exc),
                    reason="invalid_request",
                )
                return True
            # Unlike malformed input, temporary budget exhaustion is not a failed inject. Leave the
            # request unacknowledged so an additive budget extension can admit it exactly once.
            if await self._defer_for_node_budget(state):
                return True
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
                return True
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
                return True
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
            except BudgetExceeded:
                # The run's stop, not a request that failed to materialize: the handler below used
                # to record the spend ceiling as `materialization_failed` and let the run go on
                # spending. The materializer has already closed its own reservation.
                raise
            except Exception as e:  # noqa: BLE001 - a malformed operator/API inject must not
                # crash-loop the engine: the gate has already advanced, so this only records WHY the
                # (already-spent) request produced nothing. Terminalize any surviving build marker
                # first: the failure happened inside this same invocation, so unlike an escaping
                # serial build exception there may be no resume boundary to clean a partial
                # reservation.
                failed_state = fold(self.store.read_all())
                if failed_state.buildings:
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
