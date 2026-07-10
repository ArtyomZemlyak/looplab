"""Strategist cadence (A7) for the engine — the bounded-cadence meta-controller consult plus the
coverage-snapshot cadence — extracted from orchestrator.py as a MIXIN: `class Engine(…,
StrategyCadenceMixin)` inherits these methods unchanged, so there is ZERO call-site churn and
`self` here IS the engine. The method bodies are verbatim moves and read engine attributes freely
(`store` / `strategist` / `policy` / `researcher` / `developer_factory` / the `_policy_name`,
`_ablate_every`, `_coverage_context`, `strategist_every`, `n_seeds` … knobs), exactly as they did
inside the class.

`_op_span` deliberately stays on the Engine: it is a generic new-trace span helper the research /
hypothesis-merge / lessons clusters use too, not strategist-specific. The moved methods call it as
`self._op_span(...)` — resolved on the Engine instance, unchanged.

Layering: no runtime import of the orchestrator (TYPE_CHECKING only) and never serve — only core,
events, search, agents and stdlib (SurrogateResearcher / cli PRESETS stay lazy, method-local)."""
from __future__ import annotations

from typing import Optional

from looplab.agents.strategist import (NOVELTY_STANCES, StrategyContext, failure_rate,
                                       improves_since_best, is_numeric_space, run_phase,
                                       validate_strategy)
from looplab.core.models import RunState
from looplab.events.replay import fold
from looplab.events.types import EV_COVERAGE_SNAPSHOT, EV_STRATEGY_DECISION
from looplab.search.coverage import coverage_signal
from looplab.search.policy import available_policies, make_policy, operator_yields


class StrategyCadenceMixin:
    """The engine's strategist-cadence cluster. See the module docstring for the mixin convention
    (`self` is the Engine; `_op_span` stays on the Engine)."""

    # -------------------------------------------------- strategist cadence (A7)
    @staticmethod
    def _strategy_core(s: Optional[dict]) -> dict:
        """The decision-relevant subset of a Strategy (ignores rationale/source) — used to detect a
        REAL change so the engine doesn't re-record/re-apply an identical strategy every iteration."""
        if not s:
            return {}
        return {k: s.get(k) for k in ("policy", "policy_params", "developer", "operators", "fidelity", "novelty_stance", "request_research")}

    def _available_developers(self) -> list[str]:
        from looplab.agents.cli_agent import PRESETS
        names = ["default", "llm", *PRESETS]
        return names if self.developer_factory is not None else names[:1]

    def _strategy_ctx(self, state: RunState) -> StrategyContext:
        max_es = state.budget_overrides.get("max_eval_seconds", self.max_eval_seconds)
        rem = (max_es - state.total_eval_seconds) if max_es is not None else None
        defaults = {"policy": self._policy_name, "operators": {"ablate_every": self._ablate_every}}
        if max_es:
            defaults["_budget_frac"] = max(0.0, (rem or 0.0) / max_es)
        # Mean per-node eval cost so far — the cost signal the Strategist uses to bias toward an
        # intra-node sweep (amortizing data load / warm-up pays off when each eval is expensive).
        ev = [n.eval_seconds for n in state.nodes.values() if n.eval_seconds]
        avg_es = (sum(ev) / len(ev)) if ev else None
        return StrategyContext(
            node_count=len(state.nodes),
            phase=run_phase(state, self.n_seeds),
            eval_budget_remaining=rem,
            failure_rate=failure_rate(state),
            improves_since_best=improves_since_best(state),
            is_numeric_space=is_numeric_space(state),
            avg_eval_seconds=avg_es,
            node_budget_frac=(len(state.nodes) / self.policy.max_nodes
                              if getattr(self.policy, "max_nodes", 0) else 0.0),  # P2 endgame reserve
            current_policy=self._policy_name,   # D3: lets the rule switch BACK to greedy post-stall
            available_policies=available_policies(),
            available_developers=self._available_developers(),
            defaults=defaults,
            coverage=self._coverage_for_ctx(state),
            # Signal-delivery (§1): the folded per-operator yield so the Strategist tunes operator
            # cadences from the run's own evidence, not priors. Computed on the (rare) consult
            # cadence only — O(nodes) is fine here, not on the per-proposal path.
            operator_yields=operator_yields(state),
        )

    def _coverage_for_ctx(self, state: RunState) -> dict:
        """The breadth read-model for the Strategist's decision context. On the cadence path the
        snapshot `_maybe_snapshot_coverage` just recorded (it runs FIRST in `_run_cadences`) already
        sits in `state` at this node-count — reuse it instead of recomputing the O(nodes) signal
        twice; an off-cadence pin_drift consult (no snapshot at this n) computes fresh. Empty when
        coverage_context is off."""
        if not self._coverage_context:
            return {}
        snaps = state.coverage_snapshots
        if snaps and snaps[-1].get("at_node") == len(state.nodes):
            return {k: v for k, v in snaps[-1].items() if k != "at_node"}
        return coverage_signal(state, resolution=self.archive_resolution)

    def _should_consult(self, state: RunState) -> bool:
        """Bounded, deterministic cadence: only at a creation decision point (no pending evals),
        at the seed boundary or every `strategist_every` created nodes."""
        if state.pending_nodes():
            return False
        n = len(state.nodes)
        if n == 0:
            return False
        # `strategist_every` is `ge=1` via Settings, but the Engine kwarg / EngineOptions accept 0, and
        # this cadence is reused for coverage snapshots even with NO strategist wired
        # (`_maybe_snapshot_coverage`) — so guard the modulo like the deep-research cadence does, or
        # `Engine(strategist_every=0, coverage_context=True)` raises ZeroDivisionError mid-loop.
        return n == self.n_seeds or (self.strategist_every > 0 and n % self.strategist_every == 0)

    def _record_strategy(self, strat: dict, state: RunState,
                         ctx: Optional[StrategyContext] = None) -> None:
        self.store.append(EV_STRATEGY_DECISION, {
            "strategy": strat,
            "at_node": len(state.nodes),
            "ctx": (ctx.model_dump(include={"phase", "eval_budget_remaining", "failure_rate"})
                    if ctx is not None else None),
        })
        self._apply_strategy(strat)

    def _ensure_surrogate(self) -> None:
        """Wrap the Researcher in a SurrogateResearcher if it isn't already (idempotent). Used when a
        mid-run strategy switch turns BOHB on: BOHB is ASHA's racing schedule PLUS the surrogate
        proposer, and the proposer is only wired at startup for policy=bohb/surrogate_proposer — so a
        Strategist switching to bohb would otherwise run bare ASHA. Needs numeric bounds; if the
        Researcher (or its inner/fallback) exposes none, this is a no-op (bohb degrades to ASHA)."""
        from looplab.search.surrogate import SurrogateResearcher
        # Unified mode: re-wrapping `self.researcher` here would desync it from `self.developer`
        # (the same agent object) — the cli already skips the startup surrogate wrap for the same
        # reason (R1). A mid-run switch to bohb degrades to bare ASHA, which is acceptable.
        if self.unified_agent or isinstance(self.researcher, SurrogateResearcher):
            return
        bounds = (getattr(self.researcher, "bounds", None)
                  or getattr(getattr(self.researcher, "inner", None), "bounds", None)
                  or getattr(getattr(self.researcher, "fallback", None), "bounds", None))
        if bounds:
            self.researcher = SurrogateResearcher(bounds, fallback=self.researcher,
                                                  explore=self._surrogate_explore)

    def _apply_strategy(self, strat: dict) -> None:
        """Rebuild the live search machinery from a Strategy (pure wiring, no events). Policies share
        the action vocabulary and are pure, so swapping between loop iterations is safe; the Developer
        is swapped only between sequential _create_node calls."""
        if strat.get("novelty_stance") in NOVELTY_STANCES:
            self._novelty_stance = strat["novelty_stance"]   # Strategist's novelty dial (slice 2)
        ops = strat.get("operators") or {}
        if "ablate_every" in ops:
            self._ablate_every = int(ops["ablate_every"])
        if "merge_mode" in ops:
            self._merge_mode = ops["merge_mode"]
        if "complexity_cue" in ops:
            self._complexity_cue = bool(ops["complexity_cue"])
        if "ablate_code_blocks" in ops:
            self._ablate_code_blocks = bool(ops["ablate_code_blocks"])
        if "prefer_sweep" in ops:
            self._prefer_sweep = bool(ops["prefer_sweep"])
        # Resource budgets the Strategist may retune live (gated by the governance matrix). self.timeout
        # is read fresh per eval and self.max_parallel rebuilds the CapacityLimiter each batch, so a
        # mid-run change takes effect on the next node without any rewiring.
        if "timeout" in strat and self._agent_may("strategist", "timeout"):
            try:
                self.timeout = max(0.1, float(strat["timeout"]))
            except (TypeError, ValueError):
                pass
        if "max_parallel" in strat and self._agent_may("strategist", "max_parallel"):
            try:
                self.max_parallel = max(1, int(strat["max_parallel"]))
            except (TypeError, ValueError):
                pass
        pol = strat.get("policy")
        if pol:
            try:
                # Strip the names make_policy takes as explicit kwargs: a policy_params entry like
                # {"n_seeds": 4} would otherwise raise "multiple values for keyword argument",
                # silently dropping the whole switch (recorded decision diverging from live policy).
                pp = {k: v for k, v in (strat.get("policy_params") or {}).items()
                      if k not in ("n_seeds", "max_nodes", "ablate_every",
                                   "debug_depth", "operator_bandit")}
                self.policy = make_policy(pol, n_seeds=self.n_seeds, max_nodes=self.max_nodes,
                                          ablate_every=self._ablate_every,
                                          debug_depth=self._debug_depth,
                                          operator_bandit=self._operator_bandit, **pp)
                self._base_max_nodes = getattr(self.policy, "max_nodes", self.max_nodes)  # new base for the live override
                # A3 BOHB = ASHA racing + the surrogate proposer. make_policy only builds the racing
                # half; wire the surrogate now so a mid-run switch to bohb isn't bare ASHA.
                if pol == "bohb":
                    self._ensure_surrogate()
                self._policy_name = pol
            except (ValueError, TypeError):
                pass    # keep the current policy on a bad spec (validate_strategy already whitelisted)
        fid = strat.get("fidelity")
        if fid in ("smoke", "full"):
            self._strategy_fidelity = fid
        elif fid == "adaptive":
            self._strategy_fidelity = None
        dev = strat.get("developer")
        # Unified mode: researcher IS developer (one agent). A live developer-backend swap would
        # replace `self.developer` with a different object, desyncing it from `self.researcher` (and
        # the factory, still seeing unified_agent=True, would build a whole new agent). The unified
        # agent owns its own implement stage — skip the swap rather than fracture the identity (R1).
        if dev and self.developer_factory is not None and dev != self._developer_name \
                and not self.unified_agent:
            try:
                self.developer = self.developer_factory(dev)
                self._developer_name = dev
            except Exception:  # noqa: BLE001 — a bad backend swap must never abort the run
                pass

    @staticmethod
    def _already_covered_at(state: RunState, n: int) -> bool:
        return any((c or {}).get("at_node") == n for c in state.coverage_snapshots)

    def _maybe_snapshot_coverage(self, state: RunState) -> RunState:
        """Record a `coverage_snapshot` (breadth read-model) at the strategist cadence, then re-fold.
        Recorded even when NO Strategist is wired, so the run's narrowing curve is always queryable
        over the log / replayable historically (fold -> coverage_signal). Audit-only — it never
        affects node selection; folded only so the at_node gate makes a resume idempotent (each
        node-count decision point is reached once across the run's lifetime). No-op when
        coverage_context is off, off-cadence, mid-eval, or already snapshotted at this node-count."""
        n = len(state.nodes)
        if (not self._coverage_context or not self._should_consult(state)
                or self._already_covered_at(state, n)):
            return state
        self.store.append(EV_COVERAGE_SNAPSHOT, {
            "at_node": n, **coverage_signal(state, resolution=self.archive_resolution)})
        return fold(self.store.read_all())

    def _maybe_consult_strategist(self, state: RunState) -> RunState:
        """Operator/boss pin first (HITL parity), then the bounded-cadence Strategist consult.
        Records a `strategy_decision` and re-folds only when the strategy actually changes.

        An operator/boss `set_strategy` pin owns ONLY the fields it names (policy / policy_params /
        fidelity); those stay in force for the rest of the run (until re-pinned), while the
        autonomous Strategist keeps tuning everything else. The pin is MERGED onto the live strategy
        (not reset to the bare pin) and re-asserted only when a pinned field actually drifts — that,
        plus overlaying the pinned fields onto the Strategist's own decision below, is what stops the
        pin and the Strategist from thrashing (the old "reset to bare pin on any divergence"
        oscillated the policy every consult and dropped the Strategist's fidelity/operators)."""
        pin = state.pending_strategy or {}
        raw_pin = {k: pin[k] for k in ("policy", "policy_params", "fidelity")
                   if pin.get(k) is not None}
        consulting = self.strategist is not None and self._should_consult(state)
        active_core = self._strategy_core(state.active_strategy)
        # Cheap pre-check (no ctx/validate): a pin "drifts" if a raw pinned field differs from what's
        # active. For an INVALID pin this is a false alarm (it can never become active), so we still
        # validate below before acting on it.
        pin_drift = bool(raw_pin) and any(active_core.get(k) != v for k, v in raw_pin.items())
        if not pin_drift and not consulting:
            return state
        ctx = self._strategy_ctx(state)
        # Validate the pin against the SAME whitelist the engine applies, keeping only the pinned
        # fields that survive. The boss `strategy` action carries free-text policy/fidelity (server
        # `_Action.policy/fidelity`, unvalidated), so an out-of-whitelist value would otherwise be
        # overlaid RAW onto the recorded strategy below — diverging from the live policy that
        # make_policy silently rejects — and, never matching active_strategy, re-assert (and starve
        # the autonomous Strategist + spam the log) on every consult. Dropping it here makes an
        # invalid pin a harmless no-op.
        vpin = validate_strategy({**raw_pin, "source": "operator"}, ctx) if raw_pin else None
        pin_fields = {k: vpin[k] for k in raw_pin if vpin and k in vpin}
        # 1. Re-assert the pin only if a VALID pinned field isn't currently in force (merge onto active).
        if pin_fields and any(active_core.get(k) != v for k, v in pin_fields.items()):
            strat = validate_strategy({**(state.active_strategy or {}), **pin_fields,
                                       "source": "operator"}, ctx)
            if strat:
                strat.setdefault("rationale", "operator-pinned strategy")
                self._record_strategy(strat, state, ctx)
                return fold(self.store.read_all())
        # 2. Bounded-cadence Strategist consult — but the pin wins over it for the pinned fields.
        # Its own trace (new_trace) so the strategy_decision event — appended INSIDE via _record_strategy
        # — is stamped with THIS operation's trace_id (eventstore auto-stamps current_ids()), letting the
        # UI show only the strategist's own reasoning trace under that event, not the whole node's trace.
        if consulting:
            # No node_id on the op span: stamping it would file the strategist's LLM generations under
            # the NEXT node (id == len(nodes)) in /trace, polluting that node's Trace tab. The event still
            # gets THIS span's trace_id (current_ids), which is how the UI scopes it via by_trace.
            with self._op_span("strategist_consult"):
                strat = validate_strategy(self.strategist.decide(state, ctx), ctx)
                if strat:
                    strat.update(pin_fields)   # pinned (validated) policy/fidelity are non-negotiable
                    if self._strategy_core(strat) != self._strategy_core(state.active_strategy):
                        self._record_strategy(strat, state, ctx)
                        return fold(self.store.read_all())
        return state
