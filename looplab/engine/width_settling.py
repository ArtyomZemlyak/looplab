"""The Engine members that MOVE a running run's concurrency width (review 2026-09-22, ENG1-04 step 1).

`orchestrator.py` had grown to 8,437 lines. This cluster folds nothing — each member below takes the
folded state its caller already holds (invariant #4) — so moving it cannot narrow any test's `fold`
patch, which is what made it step 1 of the split; step 0, `shared.py::engine_fold`, is what makes
the later clusters, which DO fold, safe to move as well.

WHAT LIVES HERE is the LIVE half of the width contract: what may change the width a run is already
executing at, after launch. `_repin_settled_widths` (docs/29 F1) names the order the three layers
resolve in — pin < proposals < operator — and this module holds the top two plus the ceiling that
follows them:

* the PROPOSALS' re-pin — `_settle_proposal_width`, over `_proposal_footprints` — which appends the
  durable `run_width_settled` row and returns True so its caller re-folds before any gate reads it;
* the OPERATOR's `budget_extend` — `_apply_control_overrides`, re-applied on every turn — which
  applies the run's other live ceilings (`max_seconds`, `max_eval_seconds`, `timeout`) as well;
* the shared provider-call ceiling that FOLLOWS the canonical build width —
  `_reconfigure_llm_broker`, which both of the above and a granted Strategist retune
  (`strategy.py::_apply_strategy`) call.

The bottom layer — the `run_started` pin and its re-entry reconciliation (`_run_start_settled_widths`,
`_repin_settled_widths`, `_recorded_settled_width`) — stays with the other re-entry pins in
`orchestrator.py` for now: it is the re-entry cluster (step 2), and splitting a re-entry boundary
across two modules would be the wrong seam. The STARTUP resolvers (`_resolve_llm_parallel`,
`_resolve_speculation_depth`) stay beside `Engine.__init__`, their only caller, and so does the role
probe they share (`_build_calls_an_llm`), which `_settle_proposal_width` reaches through `self`.

WHY A SIBLING OF `widths.py` AND NOT A CLASS INSIDE IT. `widths.py` is the settling RULES as pure
functions — it imports `math` and `typing` and nothing else, and `tests/test_width_settling.py`
drives it as a truth table. The members below APPLY those rules to the Engine: they read and write
its live width attributes, append an event and reconfigure the broker. Putting them in the same file
would make every importer of a pure rule (`strategy.py` at module level, the tests) import the event
registry and the broker too, for nothing.

In a mixin `self` IS the Engine, so every call site kept its spelling (`self._apply_control_overrides`
…) and none of them changed. The bodies are byte-for-byte what `orchestrator.py` held. The one
observable difference is the logger: like every other mixin, this module logs under its own name
(`logging.getLogger(__name__)`), so the width re-pin's WARNING is now attributed to
`looplab.engine.width_settling` rather than to the orchestrator.
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from looplab.core.llm_broker import LLMConcurrencyBroker, default_llm_lane_limits
from looplab.core.models import RunState, effective_card_footprint
from looplab.engine.widths import (EVAL_WIDTH_MAX, LLM_WIDTH_MAX, proposal_derived_width,
                                   settle_width)
from looplab.events.types import EV_RUN_WIDTH_SETTLED

_LOG = logging.getLogger(__name__)


class WidthSettlingMixin:
    """The live width settle: the proposals' re-pin, the operator's override, the broker ceiling."""

    def _proposal_footprints(self, state: RunState) -> list[Optional[int]]:
        """The declared `gpus` of every OPEN proposal — one entry per Card the run could run next.

        `selection_ready` is the population, and it is the right one because it is the SAME predicate
        the Card queue selects from (`card_reservation.py::_reserve_node_build`): the width is being
        derived from work this run can actually dispatch, not from every Card the board has ever held.
        A card already owned by a node, terminal, stale or in flight is not a proposal to run one more
        experiment concurrently — it is an experiment already running or already done, and counting it
        would let the board's history inflate the run's width.

        The operator's `card_resource_pinned` override is merged in through `effective_card_footprint`
        exactly as admission merges it, so the width is derived from the footprint the SCHEDULER will
        see rather than from the Researcher's original declaration — the two differ precisely when an
        operator has intervened, and the operator's number is the one that will be reserved.
        `gpu_count` is deliberately NOT passed: that clamps a declaration to the live pool, and this
        caller wants the raw demand so `proposal_derived_width` can see that the proposals asked for
        more than the box has instead of being handed a pre-clamped number that hides it.
        """
        out: list[Optional[int]] = []
        for card in (getattr(state, "cards", None) or {}).values():
            if not getattr(card, "selection_ready", False):
                continue
            merged = effective_card_footprint(getattr(card, "footprint", None),
                                              getattr(card, "resource_pin", None))
            out.append((merged or {}).get("gpus"))
        return out

    def _settle_proposal_width(self, state: RunState) -> bool:
        """Re-pin the run's width from what the research proposed (docs/29 F1). True = a row landed.

        THE ASK: *"if I want to run one experiment per card — who decides that and how? Ideally
        automatically, from the propose."* Today AUTO means one experiment per detected GPU, so the
        width is a fact about the BOX; a per-Card footprint moves that node's reservation and nothing
        lets the proposals move the RUN. This is the missing half, and the reason it is a durable
        append rather than a resolver change is invariant #6 — see `_repin_settled_widths`.

        Modelled on `speculation.py::_settle_speculation_depth`, which is the existing shape for
        "a settled scalar the run is allowed to move mid-log": decide on a stable decision prefix,
        append the durable row, mutate the live attribute, say so at WARNING, and return True so the
        caller re-folds before any gate reads the new treatment.

        EVERY GATE BELOW IS A REFUSAL TO ACT, and each one is load-bearing:

        * **off by setting.** `proposal_width` is the operator's switch; a run that wants its width
          decided by the box alone keeps today's behaviour byte for byte.
        * **calibration never re-pins.** A calibration lane is launch-only and its complete execution
          envelope is receipt-bound — the same refusal `_apply_control_overrides` makes for
          `budget_extend`, one lane over. The AUTO gate below already excludes it (the profile SPELLS
          all four widths as `1`, so no axis is AUTO), but a refusal that depends on another rule
          holding is not a refusal; `search/speculation_quality.py` also refuses a log carrying this
          row, so the two ends agree.
        * **no Card queue, no proposals.** `_card_inventory_enabled` is the flag that decides whether
          this run maintains a Card board at all. Without it `state.cards` is a compatibility shadow
          over nodes, not a set of open proposals, and deriving a width from it would read the run's
          own history back as demand.
        * **AUTO axes only.** An explicitly spelled width is the operator's number, and the codebase's
          standing rule is that an explicit number is always honoured (`configuration.md`). AUTO is a
          request to let something else decide; this changes WHAT decides, from the box to the work.
        * **an axis a `budget_extend` owns is the operator's.** They said it last and it is re-applied
          every turn, so a row here would be durable noise that changes nothing — the same reasoning,
          and the same key list, as `_repin_settled_widths`'s own stand-aside clause.
        * **QUIESCENT ONLY.** Nothing in flight, no build, no head request — the same precondition
          `_settle_speculation_depth` takes, and for a sharper reason: `_dispatch_evals` sizes a
          per-batch `anyio.Semaphore` from the width and a Semaphore cannot be resized, so a width
          that moves under a live batch leaves the batch's own concurrency at the old value while the
          aged-head escape hatch compares against the new one. That comparison is now made against the
          batch's captured total, so this is defence in depth rather than the only thing holding it —
          but the honest statement is that a width change belongs BETWEEN batches, and between batches
          is free: the loop reaches this point once per turn.

        `speculation_depth` is deliberately NOT re-derived from the new width, even though AUTO depth
        resolves off the eval width at startup. It is a `run_started` pin, it is `CalibrationRuntime`
        field #6, the calibrated replay lane re-checks `admitted_depth == engine.speculation_depth`,
        and `_require_pinned_speculation_receipt` raises `SpeculationAuthorizationError` when a
        resolved depth disagrees with the recorded one — so moving it here would make a re-pinned run
        unresumable through the guarded path, at the NEXT re-entry rather than at this decision. The
        residual is a depth that may exceed the width after a narrowing (a prefetch backlog deeper
        than the lanes it feeds). That is the same benign desync a Strategist width change has always
        produced, it costs a prefetch that waits rather than a wrong result, and the depth's OWN
        one-way ratchet is the thing licensed to move it.
        """
        if not getattr(self, "_proposal_width", False):
            return False
        if self._speculation_gate_calibration:
            return False
        if not self._card_inventory_enabled():
            return False
        if (self._head_request(state) is not None
                or state.buildings
                or self._evals_inflight()
                or getattr(self, "_spec_build_inflight", None)
                or getattr(self, "_spec_builds", None)):
            return False
        overrides = getattr(state, "budget_overrides", None) or {}
        # THE CEILING IS THE LAUNCH PIN, ALWAYS, and it is read from exactly one place for that
        # reason. Two rules meet here and only one spelling satisfies both:
        #
        #   * a resume onto a bigger box must not widen past what this run was authorized to run at,
        #     because `_eval_parallel_launched` on a resumed process describes the NEW box
        #     (invariant #6) — so the LOG outranks this process;
        #   * a re-pin must never become its own ceiling, or the width ratchets monotonically down
        #     and one wide proposal is permanent — so the pin outranks the re-pin.
        #
        # `_recorded_settled_width` answers the OPPOSITE of the second rule by contract: its whole
        # docstring is "the re-pin wins over the pin", because it answers "what width were the events
        # after this row produced under?". That is the right question for resume reconciliation and
        # the wrong one for a ceiling, so this reads `state.eval_parallel` — the `run_started` pin —
        # directly. A log with no valid pin predates the pin entirely, and there the only launch
        # treatment that exists is this process's own resolution.
        launch_pin = getattr(state, "eval_parallel", 0)
        ceiling = (launch_pin if type(launch_pin) is int and 1 <= launch_pin <= EVAL_WIDTH_MAX
                   else self._eval_parallel_launched)
        if not (self._eval_parallel_startup_auto
                and not any(key in overrides for key in ("max_parallel", "eval_parallel"))):
            return False
        # ONE reading of the board and of the pool, shared by the DECISION and by the RECEIPT it
        # writes. `_proposal_footprints` walks every Card in `state.cards` and runs
        # `effective_card_footprint` per selection-ready one, so asking twice per turn paid for that
        # walk twice — and, worse, let the receipt describe footprints the width was NOT derived
        # from if anything moved the board between the two calls. Evidence that does not come from
        # the decision's own inputs is not evidence.
        pool = len(getattr(self, "_gpu_ids", None) or [])
        footprints = self._proposal_footprints(state)
        settled = {}
        width = proposal_derived_width(pool, footprints, ceiling=ceiling)
        if width is not None and width != self._eval_parallel:
            settled["eval_parallel"] = width
        if not settled:
            return False
        # llm_parallel FOLLOWS, but only where it was itself AUTO — AUTO llm width IS the settled eval
        # width (`_resolve_llm_parallel`), so leaving it behind would fan builds out wider than the run
        # can now evaluate, which is the exact coupling that resolver exists to maintain. Recorded in
        # the SAME row rather than re-derived at re-entry so replay needs no resolver at all.
        # …and only where AUTO actually RESOLVED to the eval width. `_llm_parallel_startup_auto`
        # records what the operator ASKED for (it mirrors `_resolve_llm_parallel`'s post-`int()`
        # test, by construction), not what that resolver ANSWERED: an AUTO build width on a run whose
        # build calls no LLM settles to serial 1 on purpose, because fan-out exists to overlap
        # provider latency and a templated build has none — that is CLAUDE.md invariant #1's
        # byte-identical event order, and `_resolve_llm_parallel` owns the rule. Following the eval
        # width here without re-asking would widen exactly that run back out, silently, through an
        # operator's `card_resource_pinned` footprint on a toy board. Asking the resolver's own
        # predicate keeps the two spellings of "what AUTO means for builds" from drifting apart.
        if (self._llm_parallel_startup_auto
                and self._build_calls_an_llm()
                and not any(key in overrides for key in ("parallel_build", "llm_parallel"))):
            follow = min(LLM_WIDTH_MAX, settled["eval_parallel"])
            if follow != self._llm_parallel:
                settled["llm_parallel"] = follow
        payload = {
            **settled,
            "previous": {"eval_parallel": self._eval_parallel, "llm_parallel": self._llm_parallel},
            "reason": ("the open proposals declare a per-experiment footprint the run's box-derived "
                       "width cannot serve concurrently"),
            # The decision's whole input, so `looplab inspect` and the report can show WHY and the fold
            # never has to re-derive anything from the box it is replaying on.
            "evidence": {
                "gpu_pool": pool,
                "open_proposals": len(footprints),
                "widest_declared_gpus": max((f for f in footprints
                                             if type(f) is int and f >= 0), default=None),
                "undeclared_proposals": sum(1 for f in footprints if type(f) is not int),
                "launch_ceiling": ceiling,
            },
        }
        self.store.append(EV_RUN_WIDTH_SETTLED, payload)
        for axis, value in settled.items():
            setattr(self, f"_{axis}", value)
        if "llm_parallel" in settled:
            # The shared provider-call ceiling follows the canonical width exactly as a canonical
            # `budget_extend` makes it follow, or the broker would keep admitting at the old total
            # while the build spine fanned out at the new one.
            self._reconfigure_llm_broker(settled["llm_parallel"])
        # SAY SO, at WARNING, for the same reason the depth ratchet and the GPU-pool lease wait are:
        # the run has changed its EXECUTION treatment, and a silent change gets debugged as something
        # else — here, as "why is my 4-GPU box running one experiment".
        _LOG.warning(
            "eval_parallel %d -> %d (llm_parallel -> %s): %d open proposal(s) on a pool of %d GPU(s), "
            "widest declared footprint %s. Recorded as run_width_settled in the event log, so replay "
            "and resume reproduce this width instead of re-deriving one from their own box. The "
            "surplus proposals QUEUE — the width never oversubscribes the pool, because the "
            "per-experiment GPU budget the Researcher is quoted is derived from it. To decide the "
            "width by the box alone, LAUNCH with `-s proposal_width=false` or spell the width.",
            payload["previous"]["eval_parallel"], settled["eval_parallel"],
            settled.get("llm_parallel", "unchanged"), len(footprints), pool,
            payload["evidence"]["widest_declared_gpus"])
        return True

    def _apply_control_overrides(self, state: RunState) -> tuple[Optional[float], Optional[float]]:
        # Effective budgets: an operator may raise (or lower) them live via a `budget_extend`
        # control event (folded into state.budget_overrides), e.g. "keep going for 600s more".
        # max_seconds ("keep going 600s more") is a first-class operator budget extension via the
        # budget_extend control event, not an agent_control-governed knob — applied as-is.
        _bo = state.budget_overrides
        if self._speculation_gate_calibration and _bo:
            raise RuntimeError(
                "Card speculation calibration forbids runtime budget/resource overrides; "
                "max_nodes and the complete execution envelope are receipt-bound")

        def _finite_ceiling(key: str, fallback: Optional[float]) -> Optional[float]:
            raw = _bo.get(key)
            if raw is None or isinstance(raw, bool):
                return fallback
            try:
                value = float(raw)
            except (TypeError, ValueError, OverflowError):
                return fallback
            return value if math.isfinite(value) and value > 0 else fallback

        # apply stays total even for a manually constructed/forward-version RunState;
        # replay normally sanitizes these first, but a poison ceiling must never disable a budget.
        max_s = _finite_ceiling("max_seconds", self.max_seconds)
        # A `budget_extend` is a HUMAN control intent, NOT an agent decision: CONTROL_EVENTS are
        # UI/CLI-authored (see the engine-writer invariant), and the boss action-builder
        # (serve/routers/boss.py::_Action) can ONLY ever emit `add_nodes` — it carries no field for
        # any resource ceiling. So the budget fields below reach the log ONLY from an operator via the
        # /control endpoint. Apply them AS-IS ("a human can always change it via the UI/snapshot").
        # Gating them on `_agent_may("boss", …)` (as an earlier M4 pass did) protected against nothing
        # — no agent authors them — and only ever DROPPED the operator's OWN override, silently pinning
        # the run to the old cap. Agent-authored resource retunes (the Strategist's timeout/max_parallel)
        # remain governed by the matrix in `_apply_strategy`, which is where the M4 lock genuinely lives.
        max_es = _finite_ceiling("max_eval_seconds", self.max_eval_seconds)
        if "timeout" in _bo and not isinstance(_bo["timeout"], bool):
            try:
                _timeout = float(_bo["timeout"])
                if math.isfinite(_timeout) and _timeout > 0:
                    self.timeout = max(0.1, _timeout)
            except (TypeError, ValueError, OverflowError):
                pass
        # Legacy first, canonical last: a modern command carrying both spellings is deterministic.
        # Live 0 settles to serial width 1; only launch-time Settings retain hardware/eval AUTO.
        for _key in ("max_parallel", "eval_parallel"):
            if _key in _bo:
                _settled = settle_width(_bo[_key], EVAL_WIDTH_MAX)
                if _settled is not None:
                    self._eval_parallel = _settled
        for _key in ("parallel_build", "llm_parallel"):
            if _key in _bo:
                _settled = settle_width(_bo[_key], LLM_WIDTH_MAX)
                if _settled is not None:
                    self._llm_parallel = _settled
        # A canonical live control opts into the shared provider-call ceiling. Replay may also retain
        # the last canonical total beside a newer legacy build-only override so resume cannot silently
        # change broker behavior; legacy-only historical controls remain unbounded for compatibility.
        if "llm_broker_total" in _bo or "llm_parallel" in _bo:
            self._reconfigure_llm_broker(
                _bo.get("llm_broker_total", _bo.get("llm_parallel")))
        return max_s, max_es

    def _reconfigure_llm_broker(self, value) -> None:
        """Apply one live canonical total without replacing a broker held by active borrowers."""
        # THE ONE SETTLING RULE, not a fifth spelling of it (review 2026-09-22, ENG1-10): this body
        # wrote out `widths.py::settle_width` clause for clause — the bool, the non-integral or
        # non-finite float, the `int()` failure, the `0..64` refusal and the `max(1, …)` floor —
        # which is exactly how the four control-path copies had drifted before they were folded
        # (`tests/test_width_settling.py`). Live Strategist/operator zero is a finite safety floor
        # (1), not startup AUTO: that matches the canonical runtime contract and avoids surprising
        # GPU-count re-resolution. This method is also a defensive resume boundary for manually-
        # constructed or forward-version state, so an invalid/huge value is REFUSED (None) and
        # never turned into a different valid paid-call cap.
        total = settle_width(value, LLM_WIDTH_MAX)
        if total is None:
            return
        broker = getattr(self, "_llm_broker", None)
        if broker is None:
            self._llm_broker = LLMConcurrencyBroker(
                total=total, lane_limits=default_llm_lane_limits(total),
                budget=getattr(self, "_llm_budget", None))
            return
        snapshot = broker.snapshot()
        current_lanes = snapshot["lane_limits"]
        # A total-only live delta must not erase a prior Strategist/operator lane allocation (and a
        # persistent budget override is re-applied every loop). Recompute the work-conserving defaults
        # only until a validated Strategy has explicitly owned the split; otherwise retain that split.
        next_lanes = (current_lanes if getattr(self, "_llm_lane_limits_explicit", False)
                      else default_llm_lane_limits(total))
        broker.reconfigure(total=total, lane_limits=next_lanes)
