"""The EVAL NOISE FLOOR (doc 52 row 11) — the run's own repeated-seed spread, as a MIXIN:
`class Engine(NoiseFloorMixin, …)`, so `self` here IS the engine, exactly like `confirm_phase.py`
beside it.

WHAT IT ANSWERS. LoopLab's selection gate (`trust/gate.py::one_se_better`) and its Protocol
Validity pair (`engine/champion_caveats.py::mislead_gap`) are both statements about whether a
DIFFERENCE between two numbers is real. Neither could be checked, because no run had ever recorded
what one candidate's metric does when nothing about the candidate changes — AIRA₂'s reading of the
field's "overfitting" is that it was evaluation noise. This phase re-evaluates ONE candidate under
`Settings.eval_noise_seeds` repeats and records the spread: the metrics, the mean, the sample std,
the range, and `sem` — the SAME quantity `one_se_better` compares a margin against
(`core/fitness.py::standard_error_difference(std, n, 0.0, 0)`), so the >1-SE rule and the
Mislead gap are on the floor's scale by construction rather than by a reader's arithmetic.

WHAT IT IS NOT. It is not confirmation, even though both run seeds:

  * confirm re-evaluates the TOP-K at the FULL profile from `confirm_seed_base` (1), deliberately
    DISJOINT from the search's implicit seed 0, and its mean SELECTS — that is a generalization
    signal about a different split (D1);
  * the probe re-evaluates ONE candidate — the champion, the node whose margin is the question —
    under the SEARCH's own protocol: the node's own `idea.eval_profile`, seeds 0..N-1, so the first
    repeat re-measures the exact configuration the search scored. It selects nothing.

  It is also not a node TERMINAL. Every repeat writes `eval_noise_seed`, never
  `node_evaluated`/`node_failed` — invariant #2 is one terminal per node, and a candidate that
  minted a second one could win twice off one build.

WHAT READS IT: nothing that decides, on purpose. An instrument that also moved a champion could not
be used to judge the champions it moved. It is on `RunState.eval_noise_floor` and in the log, for
`looplab replay`, for a reviewer, and for whatever eventually consults a noise floor deliberately.

WHAT IS STILL OWED: the NUMBER on a real task. This module is the mechanism — a run CAN record its
own spread, and does when asked. What the spread IS on a GPU-graded task, and whether any champion
margin this repo has ever published exceeds it, is a box measurement (doc 52 row 11's arm).

Layering: engine-level, and the same import set `confirm_phase.py` has — `core`, `events`,
`runtime.sandbox`, `trust.cv` and stdlib, with no runtime import of the orchestrator (the mixin gets
everything off `self`)."""
from __future__ import annotations

import time

import anyio

from looplab.core.containment import contain
from looplab.core.fitness import standard_error_difference
from looplab.core.models import RunState
from looplab.events.replay import fold
from looplab.events.types import EV_EVAL_NOISE_FLOOR, EV_EVAL_NOISE_SEED
from looplab.runtime.sandbox import GpuPinUnenforceable
from looplab.trust.cv import cv_summary


# A BOUNDED wait for the eval resource, per repeat. The probe runs at the end of the run, where the
# local pool is normally idle, but the host GPU-pool lease is one file per OS user
# (`engine/resources.py`) and a co-hosted run can hold it for hours. `_confirm_phase` waits for that
# forever because its result SELECTS; an instrument may not — it would hold the loop thread with
# nothing at stake. 120 ticks of the 0.5 s resource condition is a generous, finite minute, after
# which the repeat abstains and the summary says how many seeds it actually got.
_NOISE_RESOURCE_TICKS = 120


def noise_floor_summary(metrics: list[float]) -> dict:
    """The spread of one candidate's repeated metrics, as a statable rule with a truth table.

    A free function and not an inline block because it is the instrument's whole claim, and the one
    thing a reader has to be able to check without running an engine. `mean`/`std`/`n` come from
    `trust/cv.py::cv_summary` — the SAME sample (Bessel) std the confirm certificate reports, so the
    two spreads on one run are comparable — and `sem` from `core/fitness.py::standard_error_
    difference(std, n, 0.0, 0)`, which is literally the number `trust/gate.py::one_se_better` puts
    on one side of its comparison. `spread` is the plain range, for the reader who wants the worst
    case rather than an estimator.

    Fewer than two usable metrics has no spread and says so: `std`/`sem`/`spread` are None, NOT 0.0
    — a floor of zero is the strongest possible claim about an evaluation ("every margin is real"),
    and it must never be minted by a probe that measured nothing.
    """
    usable = [float(m) for m in metrics if m is not None]
    n = len(usable)
    if n < 2:
        return {"n": n, "mean": (usable[0] if n == 1 else None),
                "std": None, "sem": None, "spread": None}
    summ = cv_summary(usable)
    std = float(summ["std"])
    return {"n": n, "mean": float(summ["mean"]), "std": std,
            "sem": standard_error_difference(std, n, 0.0, 0),
            "spread": max(usable) - min(usable)}


class NoiseFloorMixin:
    """The engine's eval-noise-floor cluster. See the module docstring; `self` is the Engine."""

    def _noise_floor_due(self, state: RunState) -> bool:
        """Is this run's noise floor still unmeasured, and did the operator ask for it?

        `eval_noise_seeds` is already clamped in `Engine.__init__` (0 and 1 both mean off — one
        number has no spread), so OFF is one comparison and the phase is never entered: a run that
        did not ask for the probe is byte-identical to one built before it existed."""
        return self.eval_noise_seeds > 0 and state.eval_noise_floor is None

    async def _run_noise_seed(self, nd, s: int):
        """One repeat of node `nd`'s evaluation under seed `s`, recorded as `eval_noise_seed`.

        A deliberately smaller sibling of `_run_confirm_seed`: no per-seed GPU-refusal ladder and no
        durable auto-pause, because an instrument that cannot get its resource ABSTAINS rather than
        pausing the operator's run. Returns the metric, or None for a repeat that did not produce a
        usable one (a failed/timed-out eval, a resource it never got, a lifecycle that moved)."""
        generation = nd.attempt
        # `_confirmation_node_current` is the confirm cluster's rule and it is exactly the one this
        # phase needs — "the run is live and THIS lifecycle is still the evaluated, un-reset,
        # un-aborted node" — so it is consulted rather than restated. A second copy of that
        # predicate is a second thing to keep true.
        if not self._confirmation_node_current(nd.id, generation):
            return None
        # Generation-specific, like the confirm workdir: an old subprocess winding down after a
        # reset must not share a directory with a fresh repeat.
        workdir = self.run_dir / "noise" / f"node_{nd.id}_g{generation}_seed_{s}"
        self._materialize(nd, workdir)
        if not self._confirmation_node_current(nd.id, generation):
            return None
        with self.tracer.span("eval_noise_seed", new_trace=True, node_id=nd.id,
                              generation=generation, seed=s):
            # The pin is read ONCE, not per tick. `_confirm_phase` re-folds on every tick because a
            # Card re-pinned GPU->CPU must be able to progress during an unbounded wait; this wait is
            # bounded to a minute, and 120 whole-log folds to notice a re-pin inside it is a worse
            # trade than the probe abstaining and the next run measuring under the new pin.
            pin = self._card_resource_pin_for_node(fold(self.store.read_all()), nd)
            reservation = None
            for _ in range(_NOISE_RESOURCE_TICKS):
                if not self._confirmation_node_current(nd.id, generation):
                    return None
                reservation = await self._wait_reserve_node_resources(
                    nd, resource_pin=pin, wait_once=True)
                if reservation is not None:
                    break
            if reservation is None:
                return None                 # the bounded wall above: abstain, record nothing
            _t0 = time.time()
            try:
                env = self._resource_eval_env(
                    reservation, base={"LOOPLAB_EVAL_SEED": str(s)})
            except GpuPinUnenforceable as exc:
                # A pin the runtime cannot enforce ends the PROBE, not the run: unlike confirm
                # (whose seeds decide a champion) there is nothing here worth retrying, pausing or
                # auto-repairing for. Contained so the abstention is countable on the span.
                contain("eval_noise_seed_unpinnable", exc)
                self._release_gpus(reservation.get("gpu_ids"))
                return None
            try:
                # `profile=None` on purpose: `_run_eval` then derives the node's OWN
                # `idea.eval_profile`, which is what makes this a re-measurement of the search's
                # protocol rather than a second confirm at the full profile.
                res = await anyio.to_thread.run_sync(
                    lambda: self._run_eval(nd, str(workdir), env, None, None))
            finally:
                self._release_gpus(reservation.get("gpu_ids"))
            current = self._confirmation_node_current(nd.id, generation)
            valid = bool(current and res.metric is not None
                         and res.exit_code == 0 and not res.timed_out)
            async with self._write_lock:
                self.store.append(EV_EVAL_NOISE_SEED, {
                    "node_id": nd.id, "generation": generation, "seed": s,
                    "eval_seconds": round(time.time() - _t0, 3),
                    "metric": res.metric if valid else None,
                    **({"superseded": True} if not current else {})})
        return res.metric if valid else None

    async def _noise_floor_phase(self, state: RunState) -> None:
        """Measure this run's evaluation noise floor once, on the champion, and record it.

        ONE PASS, ALWAYS TERMINATING. The summary row is both the record and the phase's completion
        gate, and it is appended at the end of every pass that reaches its end — including a pass
        that measured nothing — because `_handle_no_actions` re-enters this ladder step until the
        gate lands. A pass abandoned by a halt intent (pause/stop/finish) appends nothing and needs
        no gate: the run loop breaks on that intent at the top of its next iteration.

        RESUME. Repeats already recorded are read out of `state.eval_noise_seed_results` and are not
        re-run, so a crash mid-pass costs the seeds it had not paid for and nothing else — the same
        per-seed memo discipline the confirm phase uses, for the same reason (a repeat is a full
        evaluation, and the run has already bought this one)."""
        nd = state.best()
        seeds = list(range(self.eval_noise_seeds))
        if nd is None or nd.metric is None:
            # Nothing to be noisy about. Record the honest empty measurement so the pass completes
            # and the ladder moves on, instead of re-entering a probe that has no subject.
            await self._append_noise_floor(None, None, seeds, [], None, None, "no_candidate")
            return
        generation = nd.attempt
        done = dict(state.eval_noise_seed_results.get(nd.id, {}))
        for s in seeds:
            if s in done:
                continue
            if self._run_halt_intent():
                return                     # the loop breaks on the intent; no gate is owed
            try:
                await self._run_noise_seed(nd, s)
            except Exception as exc:  # noqa: BLE001 — an INSTRUMENT may not end the run it measures.
                # This is the run SPINE: `_handle_no_actions` has no surrounding try, so anything
                # raised here (a materialize failure, a resource pin the runtime cannot enforce,
                # a sandbox that dies on the way up) would abort a finished search over a number
                # nothing selects on — and, because the probe is durable-gated, re-abort on every
                # resume. Contained, counted on the span, and the seed simply drops out of `n`;
                # `contain` re-raises `BudgetExceeded` first, so a spend stop still stops.
                contain("eval_noise_seed_failed", exc)
        if self._run_halt_intent():
            return
        # Read the metrics back off the FOLD rather than off what this pass happened to run: a
        # resumed pass and a fresh one then summarize the same rows in the same order, and the
        # summary can never disagree with the per-seed record it claims to summarize.
        recorded = fold(self.store.read_all()).eval_noise_seed_results.get(nd.id, {})
        metrics = [recorded.get(s) for s in seeds]
        reason = None if self._confirmation_node_current(nd.id, generation) else "superseded"
        await self._append_noise_floor(
            nd.id, generation, seeds, metrics, nd.metric,
            getattr(nd.idea, "eval_profile", None), reason)

    async def _append_noise_floor(self, node_id, generation, seeds, metrics, search_metric,
                                  profile, reason) -> None:
        """The ONE writer of `eval_noise_floor`, so the summary and its arithmetic cannot drift."""
        summary = noise_floor_summary(metrics)
        async with self._write_lock:
            self.store.append(EV_EVAL_NOISE_FLOOR, {
                "node_id": node_id, "generation": generation,
                "seeds": [int(s) for s in seeds], "metrics": list(metrics),
                "n": summary["n"], "mean": summary["mean"], "std": summary["std"],
                "sem": summary["sem"], "spread": summary["spread"],
                "search_metric": search_metric, "profile": profile,
                **({"reason": reason} if reason else {})})
