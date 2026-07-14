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
from looplab.events.types import (EV_CONCEPT_COVERAGE_SNAPSHOT, EV_COVERAGE_SNAPSHOT,
                                  EV_NODE_VERIFIED, EV_STRATEGY_DECISION)
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
        is swapped only between sequential _create_node calls.

        EVERY knob application is gated on the governance matrix (`_agent_may("strategist", <knob>)`),
        so the documented contract actually holds (architecture-review M4): a knob whose grant an
        operator removes from `agent_control` is genuinely LOCKED against the autonomous Strategist,
        not merely a decorative UI pill. The default matrix grants the Strategist every knob it applies
        (see Settings.agent_control), so default behaviour is unchanged. The gate is deterministic and
        applied both when the decision is recorded AND on resume (`_reentry_repin`), so a blocked knob
        stays blocked identically on replay — the recorded active_strategy and the live engine agree."""
        # PER-FIELD operator provenance: `_pinned` lists exactly the fields the operator pinned via a
        # `set_strategy` CONTROL_EVENT. Those are EXEMPT from the strategist's grant (the human "can
        # always change it via the UI/snapshot"); every OTHER field — including a strategist-decided
        # field that rides in a record whose top-level source is "operator" — stays gated. Whole-dict
        # `source=="operator"` was too coarse: a later autonomous-consult merge flattened an
        # operator-pinned field's provenance (reverting it on resume), and an operator pin of one field
        # blanket-exempted strategist-decided fields the matrix had locked (mega-review). `_pinned` is
        # not in _strategy_core, so it never affects change-detection; it survives fold as plain data.
        pinned = set(strat.get("_pinned") or [])

        def may(k):
            return k in pinned or self._agent_may("strategist", k)
        if may("novelty_stance") and strat.get("novelty_stance") in NOVELTY_STANCES:
            self._novelty_stance = strat["novelty_stance"]   # Strategist's novelty dial (slice 2)
        ops = strat.get("operators") or {}
        if "ablate_every" in ops and may("ablate_every"):
            self._ablate_every = int(ops["ablate_every"])
        if "merge_mode" in ops and may("merge_mode"):
            self._merge_mode = ops["merge_mode"]
        if "complexity_cue" in ops and may("complexity_cue"):
            self._complexity_cue = bool(ops["complexity_cue"])
        if "ablate_code_blocks" in ops and may("ablate_code_blocks"):
            self._ablate_code_blocks = bool(ops["ablate_code_blocks"])
        if "prefer_sweep" in ops and may("prefer_sweep"):
            self._prefer_sweep = bool(ops["prefer_sweep"])
        # Resource budgets the Strategist may retune live (gated by the governance matrix). self.timeout
        # is read fresh per eval and self.max_parallel rebuilds the CapacityLimiter each batch, so a
        # mid-run change takes effect on the next node without any rewiring.
        if "timeout" in strat and may("timeout"):
            try:
                self.timeout = max(0.1, float(strat["timeout"]))
            except (TypeError, ValueError):
                pass
        if "max_parallel" in strat and may("max_parallel"):
            try:
                self.max_parallel = max(1, int(strat["max_parallel"]))
            except (TypeError, ValueError):
                pass
        # The policy NAME and its `policy_params` are gated INDEPENDENTLY: an operator can pin
        # `policy_params` ALONE (with `policy` locked out of the strategist's grant) and that pin must
        # still take effect — the `_pinned` exemption promises "a human can always change it via the
        # UI/snapshot". So rebuild the policy when the NAME may change OR when the params may change,
        # keeping the CURRENT policy name when only the name is locked. The old `if pol and may("policy")`
        # dropped an operator's params-only pin as a permanent no-op whenever `policy` was locked (an
        # M4 regression: locking the name silently also blocked the EXEMPT params pin — code-review).
        pol = strat.get("policy")
        name_ok = bool(pol) and may("policy")
        params_ok = bool(strat.get("policy_params")) and may("policy_params")
        base = pol if name_ok else self._policy_name
        if base and (name_ok or params_ok):
            try:
                # Only consume policy_params when the params change is AUTHORIZED (params_ok). When the
                # NAME may change but params_ok is False, rebuild the new policy from its OWN defaults —
                # never smuggle the raw operator/strategist params past the lock. The old code built
                # `pp` from the raw policy_params regardless of params_ok, so a name-granted +
                # params-LOCKED grant still applied {c: 9} to MCTSPolicy (a governance bypass —
                # arch-review §4 P1-11: name and params are gated independently, so the MUTATION must
                # also consume only the authorized fields).
                raw_pp = (strat.get("policy_params") or {}) if params_ok else {}
                # Strip the names make_policy takes as explicit kwargs: a policy_params entry like
                # {"n_seeds": 4} would otherwise raise "multiple values for keyword argument",
                # silently dropping the whole switch (recorded decision diverging from live policy).
                pp = {k: v for k, v in raw_pp.items()
                      if k not in ("n_seeds", "max_nodes", "ablate_every",
                                   "debug_depth", "operator_bandit")}
                self.policy = make_policy(base, n_seeds=self.n_seeds, max_nodes=self.max_nodes,
                                          ablate_every=self._ablate_every,
                                          debug_depth=self._debug_depth,
                                          operator_bandit=self._operator_bandit, **pp)
                self.policy.ablation_capable = getattr(self, "_ablation_capable", True)  # re-stamp: a repo/eval-spec run must not propose ablate (see orchestrator init)
                self._base_max_nodes = getattr(self.policy, "max_nodes", self.max_nodes)  # new base for the live override
                # A3 BOHB = ASHA racing + the surrogate proposer. make_policy only builds the racing
                # half; wire the surrogate now so a mid-run switch to bohb isn't bare ASHA.
                if base == "bohb":
                    self._ensure_surrogate()
                self._policy_name = base
            except (ValueError, TypeError):
                pass    # keep the current policy on a bad spec (validate_strategy already whitelisted)
        fid = strat.get("fidelity")
        if may("fidelity"):
            if fid in ("smoke", "full"):
                self._strategy_fidelity = fid
            elif fid == "adaptive":
                self._strategy_fidelity = None
        dev = strat.get("developer")
        # Unified mode: researcher IS developer (one agent). A live developer-backend swap would
        # replace `self.developer` with a different object, desyncing it from `self.researcher` (and
        # the factory, still seeing unified_agent=True, would build a whole new agent). The unified
        # agent owns its own implement stage — skip the swap rather than fracture the identity (R1).
        if dev and may("developer") and self.developer_factory is not None \
                and dev != self._developer_name and not self.unified_agent:
            try:
                self.developer = self.developer_factory(dev)
                self._developer_name = dev
            except Exception:  # noqa: BLE001 — a bad backend swap must never abort the run
                pass

    @staticmethod
    def _already_covered_at(state: RunState, n: int) -> bool:
        return any((c or {}).get("at_node") == n for c in state.coverage_snapshots)

    @staticmethod
    def _autonomous_strategy_already_recorded_at(state: RunState, n: int) -> bool:
        """Whether the latest strategy action at this node count was autonomous.

        A cadence can re-enter repeatedly without creating a node (for example, a strategy decision
        requests Deep Research, whose completion appends another event).  Without a durable gate that
        re-entry consults again at the same ``n``; a strategist that alternates two valid decisions then
        appends forever.  ``strategy_history`` is replayed from the log, so the gate survives resume.

        Operator pin re-assertions deliberately do *not* consume the autonomous slot.  Compare ordering,
        not mere presence: if an operator decision is newer than an earlier autonomous decision at the
        same ``n``, allow exactly one subsequent autonomous consult against the newly pinned state.
        """
        latest_operator = -1
        latest_autonomous = -1
        for index, entry in enumerate(state.strategy_history):
            if (entry or {}).get("at_node") != n:
                continue
            strategy = (entry or {}).get("strategy") or {}
            if strategy.get("source") == "operator":
                latest_operator = index
            else:
                latest_autonomous = index
        return latest_autonomous > latest_operator

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

    def _maybe_snapshot_concept_coverage(self, state: RunState) -> RunState:
        """PART IV Phase 2a: record a compact concept-graph coverage + uncovered-region snapshot at the
        strategist cadence when `concept_pivot` is on. Deterministic (heuristic tagger over the task-type
        skeleton) -> replay-reproducible; audit-only + the source of the explore-stance pivot directive.
        Same at_node idempotence gate as `_maybe_snapshot_coverage`; no-op off-cadence / mid-eval / when
        the flag is off / when the task has no curated concept skeleton (so it never perturbs a generic
        task). Never affects selection."""
        if not getattr(self, "_concept_pivot", False) or not self._should_consult(state):
            return state
        n = len(state.nodes)
        # CODEX AGENT: Node count is not a lifecycle identity; reset/re-propose/abort can change the
        # graph and steering cues without changing n, leaving this snapshot permanently stale.
        if any((c or {}).get("at_node") == n for c in state.concept_coverage_snapshots):
            return state
        snap = self._concept_coverage_snapshot(state)
        if snap is None:
            return state
        self.store.append(EV_CONCEPT_COVERAGE_SNAPSHOT, {"at_node": n, **snap})
        return fold(self.store.read_all())

    def _concept_coverage_snapshot(self, state: RunState) -> Optional[dict]:
        """The compact concept-coverage record (uncovered regions + top-concentration + lock-in).

        AGENTIC + UNIVERSAL when a reflect client is wired (§21.13): the LLM agent BUILDS the concept graph
        from the actual experiments (`build_concept_map`, works on ANY task — no curated skeleton needed),
        and the uncovered-region directive comes from the per-task DERIVED importance
        (`derive_reference_concepts`), not a hardcoded `key=True` list. This is produced ONCE per strategist
        cadence and RECORDED as an event; `fold` only reads it (the at_node gate makes resume idempotent), so
        replay stays deterministic even though the producer is impure — the established memo/lessons pattern.
        Deterministic FALLBACK (no client): the alias heuristic over the task-type skeleton (curated pack
        required) — None when neither a client nor a skeleton is available (nothing to steer on)."""
        import contextlib

        from looplab.search.concept_graph import (build_concept_map, concept_coverage, skeleton_for,
                                                  uncovered_regions)
        from looplab.search.lock_in import lock_in_signal
        # Defensive: a bare/None `self` (e.g. a unit test calling this as a pure helper) has no reflect
        # client -> deterministic fallback, unchanged behaviour. Real engines get the agentic path.
        _rc = getattr(self, "_reflect_client", None)
        client = _rc() if callable(_rc) else None
        seed = skeleton_for(state.task_id or "")
        seed = seed if seed.concepts() else None
        # ENTIRE computation is guarded: an audit-only snapshot must NEVER perturb the run (the LLM build,
        # the consolidation, AND the pure rollups over an arbitrary grown graph all sit under one try).
        try:
            graph = tags = cov = None
            important: list = []
            mode = "heuristic"
            if client is not None:
                parser = getattr(getattr(self, "deep_researcher", None), "parser", "tool_call")
                # CODEX AGENT: This retags the full history on every cadence (quadratic total LLM calls),
                # checkpoints nothing until completion, and tools=None prevents the claimed code/log reads.
                # Span-scope the concept-map LLM generations (agentic tagging + consolidation + importance)
                # so they file under a `concept_coverage` op, not the ambient/next-node trace (mirrors
                # `_maybe_consult_strategist`'s `strategist_consult` span). nullcontext on a spanless self.
                _span = getattr(self, "_op_span", None)
                with (_span("concept_coverage") if callable(_span) else contextlib.nullcontext()):
                    cmap = build_concept_map(state, task_goal=state.goal or "", client=client, tools=None,
                                             seed_graph=seed, parser=parser)
                # Reuse the coverage build_concept_map already computed (no second O(nodes) rollup).
                graph, tags = cmap["graph"], cmap["tags"]
                cov, important = cmap["coverage"], cmap["important_uncovered"]
                mode = cmap.get("mode", "llm")
            if graph is None:                   # deterministic fallback needs a curated skeleton
                if seed is None:
                    return None
                graph, tags, mode = seed, None, "heuristic"
                cov = concept_coverage(state, graph, tags)
            if not graph.concepts():
                return None
            lock = lock_in_signal(state, graph, tags=tags)
            top = cov.get("top_concept") or {}
            # UNIVERSAL uncovered-region: prefer the LLM-DERIVED importance (any task); else the skeleton's
            # hardcoded key-concept alarm (deterministic fallback).
            keys = [str((m or {}).get("concept_id")) for m in (important or [])
                    if (m or {}).get("concept_id")][:8]
            if keys:
                directive = ("0 coverage in {" + ", ".join(keys[:6]) + "} across all "
                             f"{cov['experiments']} experiments — direct the next proposals there "
                             "(not just 'broaden').")
                fired, uncovered_key, uncovered_axes = True, keys, cov["uncovered_axes"]
            else:
                alarm = uncovered_regions(state, graph, tags)
                fired, uncovered_key = alarm["fired"], alarm["uncovered_key"]
                uncovered_axes, directive = alarm["uncovered_axes"], alarm["directive"]
        except Exception:  # noqa: BLE001 — never let an audit snapshot crash the cadence / the run
            return None
        return {
            "fired": fired,
            "uncovered_key": uncovered_key,
            "uncovered_axes": uncovered_axes,
            "directive": directive,
            "experiments": cov["experiments"],
            "top_concept": top.get("id"),
            "top_concept_frac": top.get("frac", 0.0),
            "locked_axis": lock["locked_axis"],
            "streak": lock["streak"],                  # longest same-lever run (diagnostic)
            # current_streak = the same-lever run ENDING at the latest experiment; recent_axis = the axis
            # the last few experiments concentrate on. The capability-expansion directive gates on
            # current_streak (not the longest-ever streak) and names recent_axis, so a successful pivot to
            # a different axis drops both and CLEARS the "expand the action space" cue — it fires only while
            # the search is STILL locked in right now, not forever after a past lock-in.
            "current_streak": lock["current_streak"],
            "recent_axis": lock["recent_axis"],
            "tag_mode": mode,
        }

    # --- R1-c: calibrated-verifier metric-tie-break -------------------------------------------------
    def _maybe_verify_ties(self, state: RunState) -> RunState:
        """R1-c: the calibrated §12-verifier metric-tie-break (opt-in). Find eligible nodes that TIE on
        the ranked scalar (`robust_metric`) where the tie is not yet resolvable — a group of ≥2 nodes with
        ≥1 lacking a `verifier_score` — and verify the unscored ones (grounded on their realized result,
        `selection_criteria`) so the fold's mean pick can break the tie by soundness. Lazy (only real,
        exact ties), bounded per cadence, best-effort (no client / any failure -> skip). Emits
        `node_verified` (generation-scoped); the fold reads it ONLY as a tie-break — it can never override
        a strictly-better metric (§21.7). No-op when `select_verifier` is off. Runs in the sync cadence
        (like the Strategist consult), so a blocking LLM call here matches the established pattern."""
        if not getattr(self, "_select_verifier", False):
            return state
        groups = self._metric_tie_groups(state)
        if not groups:
            return state
        try:
            client = self._reflect_client()
        except Exception:  # noqa: BLE001
            client = None
        if client is None:
            return state
        # Process-local FAILURE guard: a (node, generation) whose verify returned None is recorded so a
        # degraded client can't re-verify the same tie every cadence (a success sets verifier_score, which
        # _metric_tie_groups already excludes). In-memory only (verify is live, never replayed); a fresh
        # process on resume may retry, which is fine (bounded).
        attempted = getattr(self, "_verify_attempted", None)
        if attempted is None:
            attempted = self._verify_attempted = set()
        budget = 8                       # per-cadence NODE cap so a big tie cluster can't burst cost
        done = False
        for group in groups:
            # ATOMIC per group: score EVERY unscored member of a tie or NONE of it. A half-scored group
            # would leave an unscored sibling at the neutral 0.5 midpoint, which could outrank a
            # verified-but-low member — deciding the tie by verify TIMING/BUDGET rather than soundness. So
            # a group with a prior FAILED member (can never be fully scored) is skipped entirely (its tie
            # falls back to the id tie-break), and a group larger than the cadence budget is left for a
            # later cadence (a group larger than the cap is never verified — honest + bounded).
            if any((n.id, n.attempt) in attempted for n in group):
                continue
            todo = [n for n in group if n.verifier_score is None]
            if not todo or len(todo) > budget:
                continue
            verdicts, failed = [], False
            for n in todo:
                v = self._verifier_soundness(state, n, client)
                budget -= 1
                if v is None:
                    attempted.add((n.id, n.attempt))   # a failure abstains the WHOLE group hereafter
                    failed = True
                    break
                verdicts.append((n, v))
            if not failed:                              # atomic commit: every member scored
                for n, v in verdicts:
                    # Persist the score + provenance (n_samples, agreement) so a selection-affecting
                    # decision is auditable; the fold reads only `score` (the rest is audit-only).
                    self.store.append(EV_NODE_VERIFIED, {
                        "node_id": n.id, "generation": n.attempt, "score": round(v["score"], 4),
                        "n_samples": v["n_samples"], "agreement": v["agreement"]})
                done = True
            if budget <= 0:
                break
        return fold(self.store.read_all()) if done else state

    def _metric_tie_groups(self, state: RunState) -> list:
        """Eligible-node groups that share an EXACT `robust_metric` and still contain an unscored node —
        the metric-ties the verifier could resolve. Deterministic order (by each group's lowest node id)
        so the per-cadence budget picks stably. Pure read over folded state.

        Mirrors `_select_best`'s mean-pick pool EXACTLY: when any eligible node is confirmed, only the
        confirmed subset is ranked (and its tie-break read), so grouping over the full eligible set would
        burn verifier LLM calls on unconfirmed nodes the fold never consults. Group within the same pool."""
        from looplab.core.fitness import SearchFitness
        from looplab.events.replay import flagged_node_ids
        flagged = flagged_node_ids(state)
        eligible = [n for n in state.evaluated_nodes()
                    if SearchFitness.eligible(n, flagged, state.aborted_nodes)]
        confirmed = [n for n in eligible if n.confirmed_mean is not None]
        pool = confirmed if confirmed else eligible
        by_metric: dict = {}
        for n in pool:
            by_metric.setdefault(n.robust_metric, []).append(n)
        groups = [nodes for nodes in by_metric.values()
                  if len(nodes) >= 2 and any(n.verifier_score is None for n in nodes)]
        groups.sort(key=lambda nodes: min(n.id for n in nodes))
        return groups

    def _verifier_soundness(self, state: RunState, node, client) -> Optional[dict]:
        """The calibrated §12-verifier soundness verdict for a node's REALIZED result, or None on any
        failure / too-noisy a verdict. Returns `{score, n_samples, agreement}` — `score` is the
        `result_sound` criterion mean in [0,1] (grounded on the node's idea + metric + confirm/holdout
        signals); the provenance rides on the audit event. Best-effort — never raises.

        ABSTAINS (None) when cross-sample AGREEMENT is below a majority (only measurable with >1 sample):
        a high-variance verdict — the single-shot noise §21.12 measured — must not decide a tie. Evidence
        is scalar-summary only (the hard leakage/gaming/overfit signals stay the job of the trust layer's
        reward-hack / leakage detectors); this advisory tie-break asks only "does the reported result look
        sound", and abstains rather than over-claiming when the judgment is unstable."""
        try:
            from looplab.trust.verifier import selection_criteria, verify
            r = getattr(self, "researcher", None)
            parser = next((p for o in (r, getattr(r, "inner", None), getattr(r, "fallback", None),
                                       getattr(self, "developer", None)) if (p := getattr(o, "parser", None))),
                          "tool_call")
            subject = (f"Experiment #{node.id} reported metric={node.metric} on the task (optimize "
                       f"direction: {state.direction}); its result is genuinely sound and will hold up.")
            evidence = (f"What it did: {' '.join((getattr(node.idea, 'rationale', '') or '').split())[:250]}\n"
                        f"Metric: {node.metric}"
                        + (f"; confirmed mean over {node.confirmed_seeds} seeds: {node.confirmed_mean}"
                           if node.confirmed_mean is not None else "")
                        + (f"; holdout metric: {node.holdout_metric}" if node.holdout_metric is not None else "")
                        + (f"; generalization gap: {node.generalization_gap}"
                           if node.generalization_gap is not None else ""))
            samples = getattr(self, "_select_verifier_samples", 3)
            rep = verify(subject, evidence, selection_criteria(), client=client,
                         samples=samples, parser=parser)
            if rep is None or rep.method == "unavailable":
                return None
            crit = (rep.per_criterion or {}).get("result_sound") or {}
            m = crit.get("mean")
            score = float(m) if m is not None else (float(rep.score) if rep.score is not None else None)
            if score is None:
                return None
            # Reject a too-noisy verdict (variance is only measurable across >1 sample): if fewer than half
            # the samples agree on the modal verdict, the judgment is unstable — abstain so a coin-flip
            # can't decide the tie (which then falls back to the id tie-break for the whole group).
            if samples > 1 and rep.agreement < 0.5:
                return None
            return {"score": score, "n_samples": rep.n_samples, "agreement": rep.agreement}
        except Exception:  # noqa: BLE001 — advisory tie-break: any failure just skips (id tie-break stands)
            return None

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
        n = len(state.nodes)
        consulting = (self.strategist is not None and self._should_consult(state)
                      and not self._autonomous_strategy_already_recorded_at(state, n))
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
                strat["_pinned"] = sorted(pin_fields)   # per-field operator provenance (see may())
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
                    # Record the decision MERGED onto the live/active strategy (mirror the operator-pin
                    # path above). A strategist decision is a PARTIAL dict — only the fields the model
                    # changed — and `_apply_strategy` never resets an omitted field, so the live engine
                    # ACCUMULATES knobs across consults. Recording the bare partial made fold replace
                    # active_strategy wholesale, so a resumed run reverted every omitted knob (policy,
                    # fidelity, operators, …) to the config default — a silent divergence of the search
                    # machinery from the pre-crash live state (architecture-review M3). `operators` is
                    # applied field-by-field too, so it must DEEP-merge, not replace the whole sub-dict.
                    prev = state.active_strategy or {}
                    merged = {**prev, **strat}
                    prev_ops, new_ops = prev.get("operators") or {}, strat.get("operators") or {}
                    if prev_ops or new_ops:
                        merged["operators"] = {**prev_ops, **new_ops}
                    # request_research is a ONE-SHOT trigger (it fires a single Deep-Research stage at
                    # THIS node via _maybe_deep_research), NOT accumulated machinery. Carrying it forward
                    # from active_strategy would latch it True and re-fire the expensive Deep-Research at
                    # every later consult — so honour it only when THIS decision set it (the pre-merge
                    # semantics: the flag clears on the next recorded decision).
                    merged.pop("request_research", None)
                    if strat.get("request_research"):
                        merged["request_research"] = True
                    merged = validate_strategy(merged, ctx) or strat
                    # Carry the CURRENT operator-pinned field set (not the strategist's decision, which
                    # owns no fields) so resume-time _apply_strategy still exempts the operator's knobs
                    # even though this record's top-level source is the strategist's (mega-review).
                    merged["_pinned"] = sorted(pin_fields)
                    if self._strategy_core(merged) != self._strategy_core(state.active_strategy):
                        self._record_strategy(merged, state, ctx)
                        return fold(self.store.read_all())
        return state
