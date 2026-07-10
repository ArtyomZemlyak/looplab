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

import time

import anyio

from looplab.core.models import RunState
from looplab.events.replay import fold
from looplab.events.types import (EV_BEST_CONFIRMED, EV_CONFIRM_DONE, EV_CONFIRM_EVAL,
                                  EV_NODE_CONFIRMED, EV_SPEC_DRIFT)
from looplab.trust.confirm import robust_selection
from looplab.trust.cv import cv_summary


class ConfirmPhaseMixin:
    """The engine's confirm-phase cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    @staticmethod
    def _already_confirmed(state: RunState) -> bool:
        return state.confirmed_done  # gated on completion, not on partial progress

    async def _run_confirm_seed(self, nd, s: int):
        """One confirm-seed evaluation of node `nd` under seed `s`: materialize a fresh confirm
        workdir, run the FULL-profile eval, and record the `confirm_eval` (+ any `spec_drift`)
        events — the per-seed body `_confirm_phase` and `_confirm_node` each ran verbatim before
        the extraction, so the event emission here is byte-identical for both callers. Returns
        the metric when the seed run was valid, else None (valid implies a non-None metric)."""
        workdir = self.run_dir / "confirm" / f"node_{nd.id}_seed_{s}"
        self._materialize(nd, workdir)
        # Confirmation uses the FULL eval profile (robust check on the leaders),
        # regardless of the cheaper profile the Researcher used during search.
        _t0 = time.time()
        # Keep the per-seed events INSIDE the span so they carry its trace/span id
        # (events<->spans UI join), consistent with the _evaluate path.
        with self.tracer.span("confirm_seed", new_trace=True, node_id=nd.id, seed=s):
            res = await anyio.to_thread.run_sync(
                self._run_eval, nd, str(workdir), {"LOOPLAB_EVAL_SEED": str(s)}, "full",
            )
            valid = res.metric is not None and res.exit_code == 0 and not res.timed_out
            async with self._write_lock:            # confirm-seed eval cost (#2) + memo (#0)
                self.store.append(EV_CONFIRM_EVAL, {
                    "node_id": nd.id, "seed": s,
                    "eval_seconds": round(time.time() - _t0, 3),
                    "metric": res.metric if valid else None})
                if res.drift is not None:           # Phase 4: drop + audit drifted seeds
                    self.store.append(EV_SPEC_DRIFT, {"node_id": nd.id, "seed": s, **res.drift})
        return res.metric if valid else None

    async def _confirm_phase(self, state: RunState) -> None:
        """Re-run the top-k evaluated nodes under `confirm_seeds` seeds. Selection picks
        the robust winner (best confirmed MEAN), demoting any seed-lucky leader; the
        variance gate records whether that demotion is statistically significant.

        Resume-safe: nodes already confirmed (from an earlier crashed attempt) are
        reused, and a `best_confirmed` event is ALWAYS emitted to mark completion — so a
        confirm pass where every seed run fails can't loop forever."""
        # Only confirm BREEDABLE leaders (#5, §2.2): spending the expensive full-profile seed budget on
        # a constraint-violating OR trust-gated node is wasted — a gate-flagged cheater can never be
        # promoted to best, so it must not take a confirm slot from an honest node either.
        evaluated = sorted(state.breedable_nodes(), key=lambda n: (n.metric, n.id),
                           reverse=(state.direction == "max"))
        topk = evaluated[: self.confirm_top_k]
        if not topk:
            async with self._write_lock:
                self.store.append(EV_BEST_CONFIRMED, {"node_id": None, "significant": False})
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
        # to the lower-ranked tail. `spent` is folded ONCE (re-folding the whole log per node was
        # O(topk×events) on a repo run whose node_created events embed full file sets) then accrued from
        # each node's wall-clock confirm cost, so a large confirm_top_k can't overshoot unbounded.
        max_es = state.budget_overrides.get("max_eval_seconds", self.max_eval_seconds)
        spent = fold(self.store.read_all()).total_eval_seconds
        must_confirm = min(2, len(topk))
        for i, nd in enumerate(topk):
            if i >= must_confirm and max_es is not None and spent >= max_es:
                break                                     # leaders confirmed; a resume extends the tail
            if nd.confirmed_mean is not None:  # reuse a prior (crashed) attempt's result
                # Use the REAL seed count from that attempt, not confirm_seeds — some
                # seeds may have failed, and inflating n shrinks the SE in the variance
                # gate, overstating significance.
                summaries.append({"node_id": nd.id, "mean": nd.confirmed_mean,
                                  "std": nd.confirmed_std or 0.0,
                                  "n": nd.confirmed_seeds or self.confirm_seeds})
                continue
            # Per-seed resume (#0): reuse seeds already run in a prior (crashed) attempt instead
            # of re-executing every expensive full-profile seed. `done` maps seed -> metric|None.
            done = state.confirm_seed_results.get(nd.id, {})
            scores: list[float] = [m for m in done.values() if m is not None]
            # D1 seed-holdout: confirm seeds start at confirm_seed_base (default 1) so every
            # confirm split is DISJOINT from the search's implicit seed 0 — the confirm metric
            # is a generalization signal, not a re-measurement of what the search optimized.
            _t0 = time.time()
            for s in range(self.confirm_seed_base, self.confirm_seed_base + self.confirm_seeds):
                if s in done:                         # already evaluated this seed earlier
                    continue
                m = await self._run_confirm_seed(nd, s)
                if m is not None:
                    scores.append(m)
            spent += time.time() - _t0        # accrue this node's confirm cost (avoids the O(n²) re-fold)
            if scores:
                summ = cv_summary(scores)
                summaries.append({"node_id": nd.id, **summ})
                async with self._write_lock:
                    self.store.append(EV_NODE_CONFIRMED, {
                        "node_id": nd.id, "mean": summ["mean"],
                        "std": summ["std"], "seeds": len(scores),
                    })

        if summaries:
            sel = robust_selection(summaries, topk[0].id, state.direction)
            chosen, significant = sel["robust"]["node_id"], sel["significant"]
        else:
            chosen, significant = topk[0].id, False  # all seeds failed -> keep leader
        async with self._write_lock:
            self.store.append(EV_BEST_CONFIRMED, {"node_id": chosen, "significant": significant})

    async def _confirm_node(self, nd) -> None:
        """Operator-forced multi-seed confirmation of ONE node (force_confirm). Records the per-seed
        results (for the UI Metrics/Trust tabs) + a `confirm_done` gate, but deliberately does NOT
        emit `node_confirmed` — that would put this node into the robust-selection pool and could
        promote an otherwise-worse node to best. So a forced confirm informs the operator without
        altering deterministic best-selection. Replay-safe (gated on confirm_done + per-seed memo)."""
        state = fold(self.store.read_all())
        seeds = max(self.confirm_seeds, 3)
        done = state.confirm_seed_results.get(nd.id, {})
        for s in range(self.confirm_seed_base, self.confirm_seed_base + seeds):
            if s in done:
                continue
            await self._run_confirm_seed(nd, s)
        async with self._write_lock:
            self.store.append(EV_CONFIRM_DONE, {"node_id": nd.id})   # fulfill the request (gate)
