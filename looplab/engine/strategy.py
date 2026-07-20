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
from looplab.core.concepts import MAX_MATERIALIZED_CONCEPTS, normalize_concept_id
from looplab.core.fitness import (VERIFIER_SELECTION_CONTRACT, verifier_evidence_digest,
                                  verifier_evidence_snapshot)
from looplab.core.models import (NODE_CONCEPT_PROVENANCE_AUTHORED, NODE_CONCEPT_PROVENANCE_CLASSIFIER,
                                  NODE_CONCEPT_PROVENANCE_OPERATOR, RunState,
                                  node_concept_event_provenance)
from looplab.engine.costs import bind_cost_accountants
from looplab.engine.governance_health import GovernanceLedgerUnavailable
from looplab.events.replay import fold
from looplab.events.types import (EV_CONCEPT_CONSOLIDATION, EV_CONCEPT_COVERAGE_SNAPSHOT,
                                  EV_CONCEPT_EDGE, EV_COVERAGE_SNAPSHOT, EV_HYPOTHESIS_CONCEPTS,
                                  EV_NODE_CONCEPTS, EV_RUN_CONCEPTS, EV_STRATEGY_DECISION,
                                  EV_VERIFIER_GROUP_SCORED)
from looplab.search.coverage import (analytics_projection_token, coverage_signal,
                                     snapshot_matches_analytics_projection)
from looplab.search.policy import available_policies, make_policy, operator_yields
from looplab.trust.cross_run import (cross_run_text, same_live_direction,
                                     sanitize_cross_run_projection, valid_live_direction)

# HT (§21.18): max hypotheses to agentically tag per strategist cadence, so a large board (the rubertlite
# run hit ~150) tags incrementally over a few cadences instead of exploding one cadence's LLM budget.
_HYP_TAG_CAP = 60
# B1 (§21.18): a node whose tags were made against < _RETAG_GROWTH of the latest vocabulary is "stale" and
# gets re-tagged against the grown vocab; at most _RETAG_CAP such nodes per cadence (bounds the LLM cost,
# the rest refresh over subsequent cadences).
_RETAG_GROWTH = 0.7
_RETAG_CAP = 20


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
        # `timeout`/`max_parallel` are here because _apply_strategy applies them (resource-budget retune);
        # omitting them meant a decision that changed ONLY one of them was never seen as a change, so it
        # was never recorded or applied (the P8 live-budget retune silently no-op'd unless bundled with a
        # change to another tracked field).
        return {k: s.get(k) for k in ("policy", "policy_params", "developer", "operators", "fidelity",
                                      "novelty_stance", "request_research", "timeout", "max_parallel",
                                      "parallel_build")}

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
        cross_run_note = self._cross_run_note_for_ctx(state)
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
            cross_run_note=cross_run_note,
            cross_run_receipt=getattr(self, "_cross_run_note_receipt", {}),
        )

    def _cross_run_note_for_ctx(
            self, state: Optional[RunState] = None, *,
            _governance: Optional[dict] = None) -> str:
        """PART V §22 — a bounded live cross-run observation note for the Strategist's brief.

        It reports returned run/concept and mixed-evidence counts, not corpus coverage or proposition truth.
        On only under `cross_run_advisory` + a memory dir; best-effort ("" on any hiccup or empty store), so
        it never blocks the cadence. Advisory prose; honors operator claim decisions.
        """
        if not getattr(self, "_cross_run_advisory", False) or not getattr(self, "memory_dir", ""):
            self._cross_run_note_receipt = {}
            return ""
        current_direction = getattr(state, "direction", None) if state is not None else None
        # CODEX AGENT: the Strategist consumes this as live decision context. No state or an invalid
        # current objective must yield no guidance rather than a portfolio-wide legacy projection.
        if not valid_live_direction(current_direction):
            self._cross_run_note_receipt = {}
            return ""
        try:
            import hashlib
            import json
            from pathlib import Path

            from looplab.engine.claims import (
                _filter_claim_source_rows,
                _safe_claim_source_summary,
                atlas_for_memory,
                load_claim_lessons,
                load_research_claims,
            )
            from looplab.engine.memory import (ConceptCapsuleStore, _capsule_source_summary,
                                               _filter_capsule_rows)
            base = Path(self.memory_dir)
            if _governance is None:
                from looplab.engine.governance_health import project_governed_sources

                return project_governed_sources(
                    base,
                    lambda governance: self._cross_run_note_for_ctx(
                        state, _governance=governance),
                    include_concepts=True,
                    source_names=(
                        "concept_capsules.jsonl", "lessons.jsonl", "research_claims.jsonl"),
                )
            cp = base / "concept_capsules.jsonl"
            lessons = load_claim_lessons(base)
            caps = ConceptCapsuleStore(cp).all() if cp.exists() else []
            research = load_research_claims(base)
            task_id = str(getattr(state, "task_id", "") or "") if state is not None else ""
            run_id = str(getattr(state, "run_id", "") or "") if state is not None else ""
            def _visible(row):
                return (same_live_direction(current_direction, row.get("direction"))
                        and bool(task_id) and str(row.get("task_id") or "") == task_id
                        and (not run_id or str(row.get("run_id") or "") != run_id))
            lessons = _filter_claim_source_rows(lessons, _visible, research=False)
            caps = _filter_capsule_rows(caps, _visible)
            research = _filter_claim_source_rows(research, _visible, research=True)
            capsule_source = _capsule_source_summary(caps)
            # Use the same scope+polarity-safe projection as the Researcher advisory while
            # retaining the already-filtered, current-run-excluding snapshot used for the audit receipt.
            a = atlas_for_memory(
                base,
                lessons=lessons,
                capsules=caps,
                research_claims=research,
                structured=getattr(self, "_cross_run_structured_claims", False),
                _governance=_governance,
            )
            claim_source = _safe_claim_source_summary(a.get("claim_source"))
            if (not lessons and not caps and not research
                    and capsule_source.get("source_complete") is True
                    and claim_source is not None
                    and claim_source.get("source_complete") is True):
                self._cross_run_note_receipt = {}
                return ""
            raw_source = a.get("concept_source")
            if not isinstance(raw_source, dict):
                context_pack = a.get("context_pack") if isinstance(a.get("context_pack"), dict) else {}
                raw_source = (context_pack.get("coverage")
                              if isinstance(context_pack.get("coverage"), dict) else {})

            def _receipt_count(key: str) -> int:
                value = raw_source.get(key)
                return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

            receipt_keys = (
                "partial_capsules", "source_unknown_capsules",
                "source_concepts_omitted", "source_outcomes_omitted",
                "source_rows_total", "source_rows_quarantined", "source_malformed_rows",
                "source_invalid_capsule_rows", "source_duplicate_run_rows",
            )
            counts_valid = all(
                isinstance(raw_source.get(key), int)
                and not isinstance(raw_source.get(key), bool)
                and raw_source.get(key) >= 0 for key in receipt_keys
            )
            partial_count = _receipt_count("partial_capsules")
            receipt_known = (
                type(raw_source.get("source_complete")) is bool
                and counts_valid
                and _receipt_count("source_unknown_capsules") <= partial_count
                and type(raw_source.get("source_store_complete")) is bool
                and raw_source.get("source_store_complete") == (
                    _receipt_count("source_rows_quarantined") == 0)
                and raw_source.get("source_complete") == (
                    partial_count == 0 and _receipt_count("source_rows_quarantined") == 0)
                and (partial_count > 0
                     or (_receipt_count("source_concepts_omitted") == 0
                         and _receipt_count("source_outcomes_omitted") == 0))
            )
            concept_source = {
                "receipt_known": receipt_known,
                "source_complete": receipt_known and raw_source.get("source_complete") is True,
                "source_store_complete": (
                    receipt_known and raw_source.get("source_store_complete") is True),
                **{key: _receipt_count(key) for key in receipt_keys},
            }
            parts = [f"{a['n_runs']} returned run(s), {a['n_concepts']} observed concept(s), "
                     f"{a['n_contested']} mixed-evidence claim record(s)"]
            source_part = (
                "concept source receipt: "
                f"known={str(concept_source['receipt_known']).lower()}, "
                f"complete={str(concept_source['source_complete']).lower()}, "
                f"partial_capsules={concept_source['partial_capsules']}, "
                f"concepts_known_omitted={concept_source['source_concepts_omitted']}, "
                f"outcomes_known_omitted={concept_source['source_outcomes_omitted']}, "
                f"legacy_unknown_totals={concept_source['source_unknown_capsules']}, "
                f"quarantined_rows={concept_source['source_rows_quarantined']}"
            )
            if not concept_source["source_complete"]:
                source_part += "; concept observations/counts are retained lower bounds only"
            # CODEX AGENT: the source receipt is model-visible AND part of both semantic/render digests.
            # Partial→complete with identical retained rows must create a different advisory identity.
            parts.append(source_part)
            if claim_source is None:
                claim_receipt = {}
                claim_part = (
                    "claim source receipt: known=false, complete=false; retained claim counts and absence "
                    "are lower bounds only"
                )
            else:
                claim_receipt = claim_source
                claim_part = (
                    "claim source receipt: "
                    f"known={str(claim_source['receipt_known']).lower()}, "
                    f"complete={str(claim_source['source_complete']).lower()}, "
                    f"lessons_quarantined={claim_source['lessons']['rows_quarantined']}, "
                    f"research_quarantined={claim_source['research']['rows_quarantined']}"
                )
                if not claim_source["source_complete"]:
                    claim_part += (
                        "; retained claim counts, zero mixed-evidence, and absence are lower bounds only"
                    )
            # CODEX AGENT: zero mixed-evidence records is exact only when both evidence stores and the D8
            # producer denominator are complete. Keep that authority bit inside both model-visible digests.
            parts.append(claim_part)
            def _safe(value, limit):
                return cross_run_text(
                    value, max_chars=limit, single_line=True, entropy=True).strip()
            if a["thin_coverage"]:
                parts.append("observed in one returned run (not a gap): "
                             + ", ".join(repr(_safe(x, 80)) for x in a["thin_coverage"][:6]))
            if a["contradictions"]:
                parts.append("mixed-evidence records: "
                             + "; ".join(repr(_safe(c.get("statement"), 120))
                                          for c in a["contradictions"][:2]))
            note = cross_run_text(
                "UNTRUSTED_MEMORY_SUMMARY=" + repr(" | ".join(parts)),
                max_chars=8_000, single_line=False, entropy=True)
            corpus_projection = sanitize_cross_run_projection(
                {"parts": parts}, max_chars=16_000, max_items=64, max_total_items=256)
            corpus = json.dumps(corpus_projection,
                                ensure_ascii=False, sort_keys=True, default=str,
                                separators=(",", ":")).encode("utf-8")
            self._cross_run_note_receipt = {
                "v": 2,
                "scope_task": cross_run_text(
                    task_id, max_chars=500, single_line=True, entropy=False),
                "excluded_run": cross_run_text(
                    run_id, max_chars=500, single_line=True, entropy=False),
                "n_lessons": len(lessons), "n_capsules": len(caps), "n_research": len(research),
                "concept_source": concept_source,
                "claim_source": claim_receipt,
                "corpus_digest": hashlib.sha256(corpus).hexdigest(),
                "render_digest": hashlib.sha256(note.encode("utf-8")).hexdigest(),
            }
            return note
        except GovernanceLedgerUnavailable as exc:
            self._cross_run_note_receipt = {
                "v": 2, "status": "unavailable", "complete": False,
                "governance": exc.public_receipt(),
            }
            return ""
        except Exception:  # noqa: BLE001 — advisory context, never blocks the strategist cadence
            self._cross_run_note_receipt = {}
            return ""

    def _coverage_for_ctx(self, state: RunState) -> dict:
        """The breadth read-model for the Strategist's decision context. On the cadence path the
        snapshot `_maybe_snapshot_coverage` just recorded (it runs FIRST in `_run_cadences`) already
        sits in `state` at this node-count — reuse it instead of recomputing the O(nodes) signal
        twice; an off-cadence pin_drift consult (no snapshot at this n) computes fresh. Empty when
        coverage_context is off."""
        if not self._coverage_context:
            return {}
        snaps = state.coverage_snapshots
        if snaps and snapshot_matches_analytics_projection(state, snaps[-1]):
            return {k: v for k, v in snaps[-1].items() if k not in {"at_node", "projection_token"}}
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

    def _should_consult_concepts(self, state: RunState) -> bool:
        """PART V (F1): the concept CLASSIFIER re-tag / consolidation cadence — DECOUPLED from
        `strategist_every`. The LLM concept map is heavier and slower-moving than a strategy consult, so it
        refreshes on its OWN `concept_retag_every` interval (default 30) rather than every consult.
        Researcher-authored `idea.concepts` still fold into node_concepts at node_created (immediate UI
        freshness); this only paces the classifier-EVIDENCE + consolidation refresh and the concept-coverage
        pivot snapshot. Same shape/guards as `_should_consult` (creation decision point, seed boundary, then
        every interval; modulo guarded for the `0` kwarg case)."""
        if state.pending_nodes():
            return False
        n = len(state.nodes)
        if n == 0:
            return False
        every = getattr(self, "concept_retag_every", 0) or self.strategist_every
        return n == self.n_seeds or (every > 0 and n % every == 0)

    def _record_strategy(self, strat: dict, state: RunState,
                         ctx: Optional[StrategyContext] = None) -> None:
        self.store.append(EV_STRATEGY_DECISION, {
            "strategy": strat,
            "at_node": len(state.nodes),
            "ctx": (ctx.model_dump(include={"phase", "eval_budget_remaining", "failure_rate",
                                            "cross_run_receipt"})
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
        # parallel_build: concurrent node BUILDS. 0 = AUTO -> resolved max_parallel (build exactly as
        # many seeds as you can concurrently eval). Rebuilt role pool is lazy (_build_role_pairs), so a
        # mid-run change takes effect on the next draft batch; clamps to 1 without a role_factory.
        if "parallel_build" in strat and may("parallel_build"):
            try:
                self.parallel_build = self._resolve_parallel_build(int(strat["parallel_build"]))
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
                # Bind the replacement between calls, before its first implementation request.
                bind_cost_accountants(self)
                self._developer_name = dev
            except Exception:  # noqa: BLE001 — a bad backend swap must never abort the run
                pass

    @staticmethod
    def _already_covered_at(state: RunState, n: int) -> bool:
        return any((c or {}).get("at_node") == n
                   and snapshot_matches_analytics_projection(state, c)
                   for c in state.coverage_snapshots)

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
        over the log / replayable historically (fold -> coverage_signal). The fold reducer never
        selects a node from this record, but a live Strategist can consume the signal and change later
        search policy. It is folded so the at_node gate makes resume idempotent (each node-count
        decision point is reached once). No-op when coverage_context is off, off-cadence, mid-eval,
        or already snapshotted at this node-count."""
        n = len(state.nodes)
        if (not self._coverage_context or not self._should_consult(state)
                or self._already_covered_at(state, n)):
            return state
        self.store.append(EV_COVERAGE_SNAPSHOT, {
            "at_node": n,
            "projection_token": analytics_projection_token(state),
            **coverage_signal(state, resolution=self.archive_resolution)})
        return fold(self.store.read_all())

    def _maybe_snapshot_concept_coverage(self, state: RunState) -> RunState:
        """PART IV Phase 2a: record a compact concept-graph coverage + uncovered-region snapshot at the
        strategist cadence when `concept_pivot` is on. The producer is LLM-backed when a client is wired,
        with a deterministic heuristic fallback; recording the result makes replay deterministic. The
        folded record does not select a node directly, but its explore-stance directive can change future
        Researcher candidates. Same at_node idempotence gate as `_maybe_snapshot_coverage`; no-op
        off-cadence / mid-eval / when the flag is off / when neither a client nor fallback skeleton works."""
        if not getattr(self, "_concept_pivot", False) or not self._should_consult_concepts(state):
            return state
        n = len(state.nodes)
        if any((c or {}).get("at_node") == n
               and snapshot_matches_analytics_projection(state, c)
               for c in state.concept_coverage_snapshots):
            return state
        snap = self._concept_coverage_snapshot(state)
        if snap is None:
            return state
        # The agentic producer may have persisted fresh tags/consolidation while building ``snap``. Bind the
        # snapshot to that post-write projection, not the stale state object passed into the cadence.
        current = fold(self.store.read_all())
        if any((c or {}).get("at_node") == n
               and snapshot_matches_analytics_projection(current, c)
               for c in current.concept_coverage_snapshots):
            return current
        self.store.append(EV_CONCEPT_COVERAGE_SNAPSHOT, {
            "at_node": n, "projection_token": analytics_projection_token(current), **snap})
        return fold(self.store.read_all())

    def _maybe_seed_run_base_concepts(self, state: RunState) -> RunState:
        """PART V (B): seed `run_base_concepts` ONCE from the first evaluated node's AUTHORED concepts when
        `concept_run_base` is on. Idempotent + replay-safe: the gate is "base is empty", so once the
        EV_RUN_CONCEPTS is in the log every resume folds a populated base and this never re-emits. Only the
        main task appends it (invariant 1). No-op while off or until a node has authored concepts."""
        if not getattr(self, "_concept_run_base", False):
            return state
        from looplab.core.concepts import (
            CONCEPT_DELTA_MISSING_RUN_BASE_REASON,
            normalized_concept_materialization_receipt,
        )
        raw_base_receipt = state.run_base_concept_receipt
        base_receipt = normalized_concept_materialization_receipt(raw_base_receipt)
        derived_missing_base = bool(
            base_receipt is not None
            and CONCEPT_DELTA_MISSING_RUN_BASE_REASON in base_receipt["reasons"]
        )
        if state.run_base_concepts or (raw_base_receipt is not None and not derived_missing_base):
            # A partial/unavailable empty base is still an authored base event. Never overwrite it with
            # a later apparently exact seed; operator repair remains an explicit last-write-wins action.
            # CODEX AGENT: the fold-derived missing-base receipt is the one exception: it proves that no
            # EV_RUN_CONCEPTS exists, so a later exact evaluated full node may still perform the intended seed.
            return state
        prov = getattr(state, "node_concept_provenance", None) or {}
        receipts = getattr(state, "node_concept_materialization_receipts", None) or {}
        # First evaluated node whose concepts the RESEARCHER authored (not a classifier tag) — the run's
        # common tech stack. Deterministic (id-sorted). Downstream nodes then author only deltas vs this.
        for node in sorted(state.evaluated_nodes(), key=lambda x: x.id):
            concepts = state.node_concepts.get(node.id)
            if (node.id in state.aborted_nodes or not concepts
                    or prov.get(node.id) != NODE_CONCEPT_PROVENANCE_AUTHORED
                    or node.id in receipts):
                # CODEX AGENT: a bounded/invalid/otherwise partial membership cannot become an exact
                # run base merely because the next event contains only its retained valid subset.
                continue
            # Normalize EXACTLY as _on_run_concepts folds it (drop empty/dup), and only seed a NON-EMPTY
            # base. Otherwise `["" ]`-style concepts would fold to an empty run_base_concepts, the "base is
            # empty" gate would never clear, and this cadence would re-emit EV_RUN_CONCEPTS every pass.
            seen: set[str] = set()
            base: list[str] = []
            for c in concepts:
                s = str(c)
                if s and s not in seen:
                    seen.add(s)
                    base.append(s)
            if base:
                self.store.append(EV_RUN_CONCEPTS, {"concepts": base})
                return fold(self.store.read_all())
        return state

    def _concept_coverage_snapshot(self, state: RunState) -> Optional[dict]:
        """The compact concept-coverage record (uncovered regions + top-concentration + lock-in).

        AGENTIC + UNIVERSAL when a reflect client is wired (§21.13): the LLM agent BUILDS the concept graph
        from the actual experiments' RECORDED text (`build_concept_map` with `tools=None`, mode="llm" — it
        tags from each node's captured idea/params/result/log excerpts, works on ANY task, no curated
        skeleton needed; wiring run tools would make it fully agentic with live code/log reads but heavier),
        and the uncovered-region directive comes from the per-task DERIVED importance
        (`derive_reference_concepts`), not a hardcoded `key=True` list. This is produced ONCE per strategist
        cadence and RECORDED as an event; `fold` only reads it (the at_node gate makes resume idempotent), so
        replay stays deterministic even though the producer is impure — the established memo/lessons pattern.
        Deterministic FALLBACK (no client): the alias heuristic over the task-type skeleton (curated pack
        required) — None when neither a client nor a skeleton is available (nothing to steer on)."""
        import contextlib

        from looplab.search.concept_graph import (build_concept_map, concept_coverage, skeleton_for,
                                                  stale_tagged_nodes, uncovered_regions)
        from looplab.search.lock_in import lock_in_signal
        # Defensive: a bare/None `self` (e.g. a unit test calling this as a pure helper) has no reflect
        # client -> deterministic fallback, unchanged behaviour. Real engines get the agentic path.
        _rc = getattr(self, "_reflect_client", None)
        client = _rc() if callable(_rc) else None
        seed = skeleton_for(state.task_id or "")
        seed = seed if seed.concepts() else None
        # CODEX AGENT: guard the whole producer so a failed snapshot cannot perturb the run. A successfully
        # recorded snapshot is deliberately behavioral: its uncovered-region cue can steer later proposals.
        try:
            graph = tags = cov = None
            important: list = []
            mode = "heuristic"
            if client is not None:
                parser = getattr(getattr(self, "deep_researcher", None), "parser", "tool_call")
                # INCREMENTAL (§21.16, Phase 2c): reuse per-node tags already recorded as `node_concepts`
                # events and only LLM-tag NEW nodes, so per-run tagging is ~O(nodes) not ~O(nodes × cadences).
                # Bare-library EngineOptions keeps `_concept_pivot` off, while product `Settings` is ON;
                # cadence, bounded per-pass work, and incremental reuse therefore bound product cost.
                # The snapshot producer uses `tools=None` and tags from the node's
                # RECORDED text (idea/params/result/log excerpts in state), i.e. mode="llm", NOT a live
                # per-node tool-loop (passing run tools would make it fully agentic but far heavier).
                # Span-scope the concept-map LLM generations (tagging + consolidation + importance) so they
                # file under a `concept_coverage` op, not the ambient/next-node trace. nullcontext if spanless.
                provenance = getattr(state, "node_concept_provenance", None) or {}
                # CODEX AGENT: Researcher-authored Idea.concepts are visible read-model claims, not an
                # independent tagging result. Reusing them as known_tags would prevent the classifier from
                # ever examining that node and would later let the claim masquerade as classifier evidence.
                # Operator-edited tags ARE authoritative for THIS node (a human/assistant asserted them via
                # the concept_tag_edited control event), so treat them as known too — otherwise the cadence
                # re-tags an operator node every pass forever (the fold guard rejects the re-tag, so it never
                # converges: wasted LLM calls + no-op log growth). This keeps operator tags out of the
                # classifier-only cross-run/novelty EVIDENCE channel (still gated on CLASSIFIER provenance).
                receipts = getattr(state, "node_concept_materialization_receipts", None) or {}
                all_known = {
                    int(k): v
                    for k, v in (getattr(state, "node_concepts", None) or {}).items()
                    if (provenance.get(int(k)) == NODE_CONCEPT_PROVENANCE_OPERATOR
                        or (provenance.get(int(k)) == NODE_CONCEPT_PROVENANCE_CLASSIFIER
                            and int(k) not in receipts))
                }
                at_vocab = {int(k): int(v)
                            for k, v in (getattr(state, "node_concepts_at_vocab", None) or {}).items()}
                # B1 (§21.18): re-tag the most-stale nodes — those tagged against a much smaller vocabulary
                # than the latest (a concept minted later may now apply to them). Bounded per cadence, and
                # a strict no-op until the vocabulary has actually grown. Excluding a stale node from `known`
                # makes build_concept_map re-tag it against the grown vocab. Staleness is a CLASSIFIER-only
                # notion — only classifier receipts carry an `at_vocab`. An operator-edited node has its
                # at_vocab receipt popped (fold), so passing it here would read as at_vocab=0 (maximally
                # stale) and re-tag it every cadence — a re-tag the fold then rejects (never converges).
                # Restrict the stale candidates to classifier nodes so operator tags are left untouched.
                classifier_known = [nid for nid in all_known
                                    if provenance.get(nid) == NODE_CONCEPT_PROVENANCE_CLASSIFIER]
                stale = set(stale_tagged_nodes(classifier_known, at_vocab,
                                               growth=_RETAG_GROWTH, cap=_RETAG_CAP))
                known = {nid: v for nid, v in all_known.items() if nid not in stale}
                known_renames = {str(k): str(v)
                                 for k, v in (getattr(state, "concept_consolidation", None) or {}).items()}
                _span = getattr(self, "_op_span", None)
                with (_span("concept_coverage") if callable(_span) else contextlib.nullcontext()):
                    cmap = build_concept_map(state, task_goal=state.goal or "", client=client, tools=None,
                                             seed_graph=seed, parser=parser, known_tags=known,
                                             known_renames=known_renames)
                # B3 (§21.18): record only the NEW consolidation decisions so later cadences keep them FIXED
                # (stable vocabulary, no flapping). Accumulated in the fold; emit-only-if-new -> no churn.
                new_renames = {k: v for k, v in (cmap.get("consolidated") or {}).items()
                               if known_renames.get(k) != v}
                if new_renames:
                    self.store.append(EV_CONCEPT_CONSOLIDATION,
                                      {"rename": new_renames, "mode": cmap.get("mode", "llm")})
                # Reuse the coverage build_concept_map already computed (no second O(nodes) rollup).
                graph, tags = cmap["graph"], cmap["tags"]
                cov, important = cmap["coverage"], cmap["important_uncovered"]
                mode = cmap.get("mode", "llm")
                v_now = len(graph.concepts())    # the vocabulary size THESE tags were produced against
                raw_tag_modes = cmap.get("raw_tag_modes") or {}
                # Record a node's RAW tags + at_vocab when it is NEW/re-tagged (not in `known`) OR its tags
                # changed. Re-recording a staleness-refreshed node even when its tags are unchanged updates
                # its at_vocab, so it isn't flagged stale (and re-tagged) every cadence — no churn.
                for nid, ft in (cmap.get("raw_tags") or {}).items():
                    nid = int(nid)
                    node = state.nodes.get(nid)
                    if node is None:
                        continue
                    # CODEX AGENT: one classifier batch may contain per-node heuristic fallbacks. Stamp
                    # the actual producer so those rows cannot enter admission/capsules as independent
                    # classifier evidence merely because their successful siblings used the LLM.
                    producer_mode = raw_tag_modes.get(
                        nid, raw_tag_modes.get(str(nid), mode))
                    # Canonicalize and bound tags before the durable node_concepts trust boundary.
                    raw_ids = list(ft)
                    normalized = [normalize_concept_id(c) for c in raw_ids]
                    classifier_row = (node_concept_event_provenance({"mode": producer_mode})
                                      == NODE_CONCEPT_PROVENANCE_CLASSIFIER)
                    if classifier_row and (
                            any(cid is None for cid in normalized)
                            or len(raw_ids) > MAX_MATERIALIZED_CONCEPTS):
                        # CODEX AGENT: a classifier row outside its reviewed schema is only a bounded
                        # display fallback. Never persist a filtered subset as independent evidence.
                        producer_mode = "offline-heuristic"
                    new_ids = sorted({cid for cid in normalized if cid})[:MAX_MATERIALIZED_CONCEPTS]
                    final_classifier_row = (node_concept_event_provenance({"mode": producer_mode})
                                            == NODE_CONCEPT_PROVENANCE_CLASSIFIER)
                    if not new_ids and not final_classifier_row:
                        continue
                    if nid not in known or known.get(nid) != new_ids:
                        self.store.append(EV_NODE_CONCEPTS, {"node_id": nid, "concepts": new_ids,
                                                             "mode": producer_mode, "at_vocab": v_now,
                                                             "generation": node.attempt})
                # CODEX AGENT: do not persist co-tag edges. They can decrease or disappear after
                # re-tagging, while the commutative max ledger can only ratchet upward and therefore
                # leaves ghost co-occurrences. ConceptFrame derives that relation from its exact bounded
                # membership snapshot for online, offline and legacy runs. Explicit is_a assertions remain
                # durable: path/curated parent structure is still the typed graph's audit substrate.
                try:
                    prior_edges = getattr(state, "concept_edges", None) or {}
                    fresh_edges: list[dict] = []
                    seen: set[str] = set()
                    for concept in graph.concepts():
                        if concept.id.endswith("/*"):
                            continue
                        prefix_parent = (
                            concept.id.rsplit("/", 1)[0] if "/" in concept.id else None)
                        parents = set(graph.parents_of(concept.id))
                        if prefix_parent:
                            parents.add(prefix_parent)
                        for parent in sorted(parents):
                            if not parent or parent == concept.id:
                                continue
                            key = "\t".join((concept.id, "is_a", parent))
                            if key in seen or key in prior_edges:
                                continue
                            seen.add(key)
                            fresh_edges.append({
                                "src": concept.id, "rel": "is_a", "dst": parent,
                                "provenance": "asserted", "confidence": 1.0,
                            })
                    if fresh_edges:
                        self.store.append(EV_CONCEPT_EDGE, {"edges": fresh_edges, "mode": mode})
                except Exception:  # noqa: BLE001 - audit enrichment must not break the cadence
                    pass
                # HT (§21.18): agentically tag any UNtagged hypotheses against the SAME graph and record
                # them, so taxonomy dedup reuses the agentic tags instead of the tag_text alias heuristic.
                # Incremental (skip already-tagged) + capped per cadence, so a big board tags over a few
                # cadences instead of exploding one. Isolated try: a tagging hiccup must not lose the snapshot.
                try:
                    from looplab.search.concept_graph import tag_text_llm
                    known_h = getattr(state, "hypothesis_concepts", None) or {}
                    h_at_vocab = getattr(state, "hypothesis_concepts_at_vocab", None) or {}
                    v_now = len(graph.concepts())
                    # B1-ext (§21.18): re-tag the most-STALE hypotheses (tagged against a much smaller vocab)
                    # in addition to UNtagged ones — same at_vocab staleness rule as nodes, bounded per cadence.
                    stale_h = set(stale_tagged_nodes(list(known_h), h_at_vocab,
                                                     growth=_RETAG_GROWTH, cap=_RETAG_CAP))
                    tagged_this_cadence = 0
                    for h in (state.hypotheses or {}).values():
                        if not getattr(h, "statement", ""):
                            continue
                        if h.id in known_h and h.id not in stale_h:   # already tagged & fresh -> skip
                            continue
                        if tagged_this_cadence >= _HYP_TAG_CAP:
                            break
                        htags = sorted(tag_text_llm(h.statement, graph, client, parser=parser,
                                                    allow_plural=True))
                        self.store.append(EV_HYPOTHESIS_CONCEPTS, {"hyp_id": str(h.id), "concepts": htags,
                                                                   "mode": mode, "at_vocab": v_now})
                        tagged_this_cadence += 1
                except Exception:  # noqa: BLE001 — hypothesis tagging is best-effort audit enrichment
                    pass
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
        """R1-c: the calibrated §12-verifier metric-tie-break (opt-in). Find the complete selector-reachable
        exact/CI tie that is not yet resolvable and re-score every member against one evidence revision
        (`selection_criteria`) so the fold can break it by soundness. Lazy, bounded per cadence, and
        best-effort (no client / any failure -> skip). Emits one atomic
        `verifier_group_scored` record; the fold reads it ONLY as a tie-break — it can never override
        a strictly-better metric (§21.7). No-op when `select_verifier` is off. Runs in the sync cadence
        (like the Strategist consult), so a blocking LLM call here matches the established pattern."""
        if not state.select_verifier_tiebreak:
            return state
        # The producer and replay validator must agree on the selection contract before any paid
        # verification work starts.  A future/unknown recorded contract is intentionally fail-closed:
        # this process cannot safely emit a v1 treatment for selection rules it does not understand.
        if state.select_verifier_contract != VERIFIER_SELECTION_CONTRACT:
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
        # Process-local FAILURE guard: record a (node, generation, evidence revision) whose verify returned
        # None so a degraded client can't re-verify the same tie every cadence (a success sets
        # verifier_score, which _metric_tie_groups already excludes). In-memory only (verify is live, never
        # replayed); a fresh
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
            attempted_keys = {
                n.id: (n.id, n.attempt, verifier_evidence_digest(state.direction, n)) for n in group}
            if any(attempted_keys[n.id] in attempted for n in group):
                continue
            # Re-score the complete current tie. Carrying an older member score into a newly expanded group
            # would mix treatments and let one node influence two incompatible evidence snapshots.
            todo = list(group)
            if len(todo) > budget:
                continue
            verdicts, failed = [], False
            for n in todo:
                v = self._verifier_soundness(state, n, client)
                budget -= 1
                if v is None:
                    attempted.add(attempted_keys[n.id])  # failure abstains this evidence revision hereafter
                    failed = True
                    break
                verdicts.append((n, v))
            if not failed:
                # Publish the complete selector-reachable tie group in one durable event;
                # per-node appends expose crash prefixes that can change the winner during replay.
                self.store.append(EV_VERIFIER_GROUP_SCORED, {
                    "v": 1, "contract": VERIFIER_SELECTION_CONTRACT,
                    "requested_samples": state.select_verifier_samples,
                    "members": [{
                        "node_id": n.id, "generation": n.attempt,
                        "score": round(v["score"], 4), "n_samples": v["n_samples"],
                        "agreement": v["agreement"], "method": v["method"],
                        "evidence_digest": verifier_evidence_digest(state.direction, n),
                    } for n, v in verdicts],
                })
                done = True
            if budget <= 0:
                break
        return fold(self.store.read_all()) if done else state

    def _metric_tie_groups(self, state: RunState) -> list:
        """The sole complete tie-set that can affect `_select_best`'s final champion.

        The replay helper owns pool/holdout/CI precedence as one pure contract shared by the event producer
        and validator. Recorded run state is authoritative here: live engine fields may not silently change
        selection semantics after resume or a config edit.
        """
        # Use folded run flags and the validator's helper; live engine config must not produce
        # a treatment that replay rejects or select a tie shadowed by the final holdout selector.
        from looplab.events.replay import verifier_tie_groups
        return verifier_tie_groups(state)

    def _verifier_soundness(self, state: RunState, node, client) -> Optional[dict]:
        """The calibrated §12-verifier soundness verdict for a node's REALIZED result, or None on any
        failure / too-noisy a verdict. Returns `{score, n_samples, agreement}` — `score` is the
        `result_sound` criterion mean in [0,1] (grounded on the node's idea + metric + confirm/holdout
        signals); the provenance rides on the audit event. Best-effort — never raises.

        ABSTAINS (None) when cross-sample AGREEMENT is not a strict majority (only measurable with >1 sample):
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
            snapshot = verifier_evidence_snapshot(state.direction, node)
            subject = (f"Experiment #{node.id} reported metric={snapshot['metric']} on the task (optimize "
                       f"direction: {state.direction}); its result is genuinely sound and will hold up.")
            evidence = (f"What it did: {snapshot['rationale']}\n"
                        f"Metric: {snapshot['metric']}"
                        + (f"; confirmed mean over {snapshot['confirmed_seeds']} seeds: "
                           f"{snapshot['confirmed_mean']}" if snapshot['confirmed_mean'] is not None else "")
                        + (f"; holdout metric: {snapshot['holdout_metric']}"
                           if snapshot['holdout_metric'] is not None else "")
                        + (f"; generalization gap: {snapshot['generalization_gap']}"
                           if snapshot['generalization_gap'] is not None else ""))
            samples = state.select_verifier_samples
            rep = verify(subject, evidence, selection_criteria(), client=client,
                         samples=samples, parser=parser)
            if rep is None or rep.method == "unavailable":
                return None
            crit = (rep.per_criterion or {}).get("result_sound") or {}
            m = crit.get("mean")
            score = float(m) if m is not None else (float(rep.score) if rep.score is not None else None)
            if score is None or score != score or not 0.0 <= score <= 1.0:
                return None
            # Repeated verification needs a strict majority of the REQUESTED samples to
            # survive parsing as well as a strict modal majority. One lucky parsed answer out of three is
            # not a repeated verdict and must not become selection-affecting evidence.
            if (rep.n_samples > samples or rep.n_samples * 2 <= samples
                    or (samples > 1 and rep.agreement <= 0.5)):
                return None
            return {"score": score, "n_samples": rep.n_samples, "agreement": rep.agreement,
                    "method": str(rep.method or "")[:80]}
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
