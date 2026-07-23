"""Confirm phase (I12, ADR-15) for the engine — multi-seed top-k confirmation plus the
operator-forced single-node confirm — extracted from orchestrator.py as a MIXIN:
`class Engine(ConfirmPhaseMixin, …)` inherits these methods unchanged, so there is ZERO
call-site churn and `self` here IS the engine. The method bodies are verbatim moves and read
engine attributes freely (store / tracer / run_dir / _write_lock / confirm_* knobs /
_run_eval / _materialize / feasible-node state), exactly as they did inside the class.

The per-seed eval body the two confirm paths repeated verbatim now lives in
`_run_confirm_seed` (the loops differed only in seed count — a parameter — and in score
collection, which stays at the `_confirm_phase` call site); the robust-winner selection at
`_confirm_phase`'s tail is the SAME pure step `trust/confirm.py::confirm_top_k` uses, shared
via `robust_selection` so the two can never drift.

Named `confirm_phase` (not `confirm`) on purpose: the flat-import shim in looplab/__init__.py
already maps `looplab.confirm` to trust/confirm.py. Layering: no runtime import of the
orchestrator (TYPE_CHECKING only) and never serve — only trust, events, core and stdlib."""
from __future__ import annotations

import threading
import time

import anyio

from looplab.core.models import NodeStatus, RunState
from looplab.events.replay import fold
from looplab.events.types import (EV_BEST_CONFIRMED, EV_CONFIRM_DONE, EV_CONFIRM_EVAL,
                                  EV_NODE_CONFIRMED, EV_SPEC_DRIFT)
from looplab.runtime.sandbox import GpuPinUnenforceable
from looplab.trust.confirm import robust_selection
from looplab.trust.cv import cv_summary


_CONFIRM_RETRYABLE = object()


class ConfirmPhaseMixin:
    """The engine's confirm-phase cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    @staticmethod
    def _already_confirmed(state: RunState) -> bool:
        return state.confirmed_done  # gated on completion, not on partial progress

    def _confirmation_node_current(self, node_id: int, generation: int) -> bool:
        state = fold(self.store.read_all())
        node = state.nodes.get(node_id)
        return (not state.paused and not state.finished and not state.stop_requested
                and node is not None and node.attempt == generation
                and node.status is NodeStatus.evaluated and not node.tombstoned
                and node_id not in state.aborted_nodes)

    def _confirmation_snapshot_current(self, generations: dict[str, int]) -> bool:
        state = fold(self.store.read_all())
        current = {str(node.id): node.attempt for node in state.nodes.values()
                   if node.id not in state.aborted_nodes and not node.tombstoned}
        return current == generations

    async def _run_confirm_seed(self, nd, s: int):
        """One confirm-seed evaluation of node `nd` under seed `s`: materialize a fresh confirm
        workdir, run the FULL-profile eval, and record the `confirm_eval` (+ any `spec_drift`)
        events — the per-seed body `_confirm_phase` and `_confirm_node` each ran verbatim before
        the extraction, so the event emission here is byte-identical for both callers. Returns the
        metric when the seed run was valid, None for a completed invalid/cancelled seed, or a private
        retry sentinel when infrastructure refused the run before useful work could complete."""
        generation = nd.attempt
        if not self._confirmation_node_current(nd.id, generation):
            return None
        # Generation-specific path: an old confirm subprocess can still be winding down when reset
        # starts a fresh lifecycle. Sharing its directory would let stale predictions/checkpoints bleed
        # into the new seed even though replay correctly rejects the old event.
        workdir = self.run_dir / "confirm" / f"node_{nd.id}_g{generation}_seed_{s}"
        self._materialize(nd, workdir)
        if not self._confirmation_node_current(nd.id, generation):
            return None
        # Confirmation uses the FULL eval profile (robust check on the leaders), regardless of the
        # cheaper profile the Researcher used during search. Resource queue time is deliberately outside
        # the billed eval clock; only admitted setup/execution enters immutable ``eval_seconds``.
        # Keep the per-seed events INSIDE the span so they carry its trace/span id
        # (events<->spans UI join), consistent with the _evaluate path.
        with self.tracer.span("confirm_seed", new_trace=True, node_id=nd.id, seed=s):
            cancel = threading.Event()

            async def _watch_lifecycle() -> None:
                while not cancel.is_set():
                    current = await anyio.to_thread.run_sync(
                        self._confirmation_node_current, nd.id, generation)
                    if not current:
                        cancel.set()
                        return
                    # 1.0s, not 0.1s (F26): each check re-folds the WHOLE event log, so 10x/s per active
                    # seed was O(total-events) CPU that scaled with run length. The first check runs
                    # immediately (before the sleep); a ~1s supersede-cancel latency is imperceptible on
                    # an eval that runs seconds-to-minutes.
                    await anyio.sleep(1.0)

            resource_node = nd
            while True:
                resource_state = fold(self.store.read_all())
                live = resource_state.nodes.get(nd.id)
                if (
                    resource_state.paused
                    or resource_state.finished
                    or resource_state.stop_requested
                    or live is None
                    or live.attempt != generation
                    or live.status is not NodeStatus.evaluated
                    or live.tombstoned
                    or live.id in resource_state.aborted_nodes
                ):
                    return None
                resource_node = live
                reservation = await self._wait_reserve_node_resources(
                    resource_node,
                    resource_pin=self._card_resource_pin_for_node(
                        resource_state, resource_node),
                    wait_once=True,
                )
                if reservation is None:
                    # The finite resource tick is also the operator-control polling cadence.  Re-fold
                    # before retrying so a GPU->CPU re-pin can progress without any GPU release and a
                    # pause/stop/abort/reset cannot start an obsolete confirmation subprocess.
                    continue
                admitted = fold(self.store.read_all())
                current = admitted.nodes.get(nd.id)
                if (
                    admitted.paused
                    or admitted.finished
                    or admitted.stop_requested
                    or current is None
                    or current.attempt != generation
                    or current.status is not NodeStatus.evaluated
                    or current.tombstoned
                    or current.id in admitted.aborted_nodes
                ):
                    self._release_gpus(reservation.get("gpu_ids"))
                    return None
                if not self._node_resource_reservation_is_current(
                    admitted, current, reservation,
                ):
                    self._release_gpus(reservation.get("gpu_ids"))
                    resource_node = current
                    continue
                break
            _t0 = time.time()
            try:
                confirm_env = self._resource_eval_env(
                    reservation, base={"LOOPLAB_EVAL_SEED": str(s)})
            except GpuPinUnenforceable as exc:
                # A zero-device inventory is retryable infrastructure state, not a completed seed memo.
                # Record an audit row, but replay deliberately excludes this reason from
                # ``confirm_seed_results`` so a later re-pin/runtime repair can retry the same seed.
                try:
                    async with self._write_lock:
                        self.store.append(EV_CONFIRM_EVAL, {
                            "node_id": nd.id, "generation": generation, "seed": s,
                            "eval_seconds": 0.0, "metric": None,
                            "reason": "gpu_unavailable", "error": str(exc)[:400]})
                finally:
                    self._release_gpus(reservation.get("gpu_ids"))
                return _CONFIRM_RETRYABLE

            def _run():
                return self._run_eval(
                    nd, str(workdir), confirm_env, "full", cancel)

            try:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(_watch_lifecycle)
                    try:
                        res = await anyio.to_thread.run_sync(_run)
                    except GpuPinUnenforceable as exc:
                        # A durable resource pin the Docker daemon/runtime cannot enforce must NOT crash
                        # the run() spine (confirm has no surrounding try — it would abort the engine and
                        # re-crash on every resume, since the operator pin is durable). Audit THIS attempt,
                        # but do not memoize it as a completed seed: a later runtime repair/re-pin must retry.
                        # Catch INSIDE the task group around the await — a bare `except` OUTSIDE it would
                        # never match the ExceptionGroup anyio wraps a task-group body error in (dead code).
                        # Shield the write-lock append so scope cancellation cannot preempt the checkpoint;
                        # `_release_gpus` still runs in the finally below (do not double-release).
                        cancel.set()
                        tg.cancel_scope.cancel()
                        still_current = self._confirmation_node_current(nd.id, generation)
                        with anyio.CancelScope(shield=True):
                            async with self._write_lock:
                                self.store.append(EV_CONFIRM_EVAL, {
                                    "node_id": nd.id, "generation": generation, "seed": s,
                                    "eval_seconds": round(time.time() - _t0, 3), "metric": None,
                                    "reason": "gpu_unpinnable", "error": str(exc)[:400],
                                    **({"superseded": True} if not still_current else {})})
                        return _CONFIRM_RETRYABLE
                    cancel.set()
                    tg.cancel_scope.cancel()
            finally:
                self._release_gpus(reservation.get("gpu_ids"))

            current = self._confirmation_node_current(nd.id, generation)
            valid = (current and res.metric is not None
                     and res.exit_code == 0 and not res.timed_out)
            async with self._write_lock:            # confirm-seed eval cost (#2) + memo (#0)
                self.store.append(EV_CONFIRM_EVAL, {
                    "node_id": nd.id, "generation": generation, "seed": s,
                    "eval_seconds": round(time.time() - _t0, 3),
                    "metric": res.metric if valid else None,
                    **({"superseded": True} if not current else {})})
                if current and res.drift is not None:  # Phase 4: drop + audit drifted seeds
                    self.store.append(EV_SPEC_DRIFT,
                                      {"node_id": nd.id, "seed": s, **res.drift,
                                       "generation": generation})
        return res.metric if valid else None

    async def _confirm_phase(self, state: RunState) -> None:
        """Re-run the top-k evaluated nodes under `confirm_seeds` seeds. Selection picks
        the robust winner (best confirmed MEAN), demoting any seed-lucky leader; the
        variance gate records whether that demotion is statistically significant.

        Resume-safe: nodes already confirmed (from an earlier crashed attempt) are reused, and a
        completed pass emits `best_confirmed` even when every actual seed run returns an invalid
        metric. A retryable infrastructure refusal deliberately leaves the pass open."""
        # Only confirm BREEDABLE leaders (#5, §2.2): spending the expensive full-profile seed budget on
        # a constraint-violating OR trust-gated node is wasted — a gate-flagged cheater can never be
        # promoted to best, so it must not take a confirm slot from an honest node either.
        evaluated = sorted(state.breedable_nodes(), key=lambda n: (n.metric, n.id),
                           reverse=(state.direction == "max"))
        topk = evaluated[: self.confirm_top_k]
        # Snapshot EVERY extant lifecycle, not only top-k: a reset of an unconfirmed/pending node
        # while this pass runs still changes the candidate epoch and must invalidate completion.
        generations = {str(nd.id): nd.attempt for nd in state.nodes.values()
                       if nd.id not in state.aborted_nodes and not nd.tombstoned}
        if not topk:
            if not self._confirmation_snapshot_current(generations):
                return
            async with self._write_lock:
                self.store.append(EV_BEST_CONFIRMED,
                                  {"node_id": None, "significant": False,
                                   "search_epoch": state.search_epoch,
                                   "generations": generations})
            return

        summaries: list[dict] = []
        # Eval-budget guard for the confirm phase. Confirmation is the run's FINAL robustness gate
        # (multi-seed-confirm the top leaders, demote a seed-lucky #1), so it must NOT be skipped
        # wholesale: on an eval-seconds-bounded run the search normally consumes the whole budget, so
        # `total_eval_seconds >= max_es` is ALREADY true when confirm starts. A naive per-node break
        # there confirmed ZERO nodes yet still emitted `best_confirmed` (→ confirmed_done True),
        # permanently disabling confirmation even across a budget-extending resume, and kept the
        # un-demoted seed-lucky leader. So ALWAYS confirm at least the top `min(2, len(topk))` leaders
        # (enough for a demotion decision — a bounded, one-time overrun); the budget break applies only
        # to the lower-ranked tail. `spent` is read from the immutable charged total after each small
        # top-k item. This is slightly more replay work than wall-clock accrual, but it excludes resource
        # queue time and keeps the budget contract exact even when an audited attempt is retryable.
        max_es = state.budget_overrides.get("max_eval_seconds", self.max_eval_seconds)
        spent = fold(self.store.read_all()).total_eval_seconds
        must_confirm = min(2, len(topk))
        for i, nd in enumerate(topk):
            if (not self._confirmation_snapshot_current(generations)
                    or not self._confirmation_node_current(nd.id, nd.attempt)):
                return
            if nd.confirmed_mean is not None:  # reuse a prior (crashed) attempt's result — FREE, no eval
                # Use the REAL seed count from that attempt, not confirm_seeds — some
                # seeds may have failed, and inflating n shrinks the SE in the variance
                # gate, overstating significance. Harvested BEFORE the budget break below: reusing an
                # already-confirmed mean costs ZERO eval time, so a budget-exhausted resume must still
                # include it in robust-winner selection (otherwise an already-confirmed lower-metric node
                # whose confirmed mean is actually the most robust could never win, and the champion
                # silently reverts to a less-robust, un-demoted seed-lucky leader).
                summaries.append({"node_id": nd.id, "mean": nd.confirmed_mean,
                                  "std": nd.confirmed_std or 0.0,
                                  "n": nd.confirmed_seeds or self.confirm_seeds})
                continue
            # The budget cutoff applies ONLY to UNCONFIRMED tail nodes (a fresh per-seed eval costs time);
            # every already-confirmed node is harvested for free above regardless of budget. Use `continue`,
            # NOT `break`: a `break` would also stop harvesting already-confirmed nodes ORDERED AFTER the
            # first unconfirmed budget-broken node (e.g. topk = [A✓, B✓, C✗(broke), D✓] would drop D's free
            # confirmed mean) — the same wrong-champion bug this guard exists to prevent.
            if i >= must_confirm and max_es is not None and spent >= max_es:
                continue                                  # skip only this unconfirmed node's paid eval; a resume extends it
            # Per-seed resume (#0): reuse seeds already run in a prior (crashed) attempt instead
            # of re-executing every expensive full-profile seed. `done` maps seed -> metric|None.
            done = state.confirm_seed_results.get(nd.id, {})
            scores: list[float] = [m for m in done.values() if m is not None]
            # D1 seed-holdout: confirm seeds start at confirm_seed_base (default 1) so every
            # confirm split is DISJOINT from the search's implicit seed 0 — the confirm metric
            # is a generalization signal, not a re-measurement of what the search optimized.
            for s in range(self.confirm_seed_base, self.confirm_seed_base + self.confirm_seeds):
                if s in done:                         # already evaluated this seed earlier
                    continue
                if (not self._confirmation_snapshot_current(generations)
                        or not self._confirmation_node_current(nd.id, nd.attempt)):
                    return
                m = await self._run_confirm_seed(nd, s)
                if (not self._confirmation_snapshot_current(generations)
                        or not self._confirmation_node_current(nd.id, nd.attempt)):
                    return
                if m is _CONFIRM_RETRYABLE:
                    # Infrastructure refusal is neither a completed seed nor confirmation completion.
                    # Leave the phase open so the same seed can run after a re-pin/runtime repair.
                    # CLAUDE REVIEW: this retry has no bound. run()'s empty-actions branch re-enters
                    # _confirm_phase on the very next iteration (orchestrator._run, "if not actions"),
                    # every attempt re-raises GpuPinUnenforceable instantly (required_unavailable
                    # returns without waiting) and appends another confirm_eval row, and replay
                    # excludes gpu_unavailable/gpu_unpinnable from the seed memo — so a host that can
                    # never satisfy the pin (CPU box resume, driver loss) hot-spins at 100% CPU with
                    # unbounded events.jsonl growth. Add backoff or a consecutive-refusal cap that
                    # pauses/terminalizes the pass, and dedupe the audit row per (node, seed, reason).
                    return
                if m is not None:
                    scores.append(m)
            spent = fold(self.store.read_all()).total_eval_seconds
            if scores:
                summ = cv_summary(scores)
                summaries.append({"node_id": nd.id, **summ})
                async with self._write_lock:
                    if (not self._confirmation_snapshot_current(generations)
                            or not self._confirmation_node_current(nd.id, nd.attempt)):
                        return
                    self.store.append(EV_NODE_CONFIRMED, {
                        "node_id": nd.id, "generation": nd.attempt, "mean": summ["mean"],
                        "std": summ["std"], "seeds": len(scores),
                    })

        if summaries:
            sel = robust_selection(summaries, topk[0].id, state.direction)
            chosen, significant = sel["robust"]["node_id"], sel["significant"]
        else:
            chosen, significant = topk[0].id, False  # all seeds failed -> keep leader
        if not self._confirmation_snapshot_current(generations):
            return
        async with self._write_lock:
            if not self._confirmation_snapshot_current(generations):
                return
            self.store.append(EV_BEST_CONFIRMED, {
                "node_id": chosen, "significant": significant,
                "search_epoch": state.search_epoch, "generations": generations})

    async def _confirm_node(self, nd) -> None:
        """Operator-forced multi-seed confirmation of ONE node (force_confirm). Records the per-seed
        results (for the UI Metrics/Trust tabs) + a `confirm_done` gate, but deliberately does NOT
        emit `node_confirmed` — that would put this node into the robust-selection pool and could
        promote an otherwise-worse node to best. So a forced confirm informs the operator without
        altering deterministic best-selection. Replay-safe (gated on confirm_done + per-seed memo)."""
        generation = nd.attempt
        if not self._confirmation_node_current(nd.id, generation):
            return
        state = fold(self.store.read_all())
        seeds = max(self.confirm_seeds, 3)
        done = state.confirm_seed_results.get(nd.id, {})
        for s in range(self.confirm_seed_base, self.confirm_seed_base + seeds):
            if s in done:
                continue
            if not self._confirmation_node_current(nd.id, generation):
                return
            result = await self._run_confirm_seed(nd, s)
            if not self._confirmation_node_current(nd.id, generation):
                return
            if result is _CONFIRM_RETRYABLE:
                return
        async with self._write_lock:
            if not self._confirmation_node_current(nd.id, generation):
                return
            self.store.append(EV_CONFIRM_DONE,
                              {"node_id": nd.id, "generation": generation})  # fulfillment gate
